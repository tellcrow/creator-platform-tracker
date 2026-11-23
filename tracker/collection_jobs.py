"""One browser owner thread per platform; storage and widgets stay on the UI thread."""
import hashlib
from pathlib import Path
import queue
import threading
import time
from .schema import PLATFORMS, now_iso, validate_platform
from .parser import parse_file


class CombinedCancel:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while not self.is_set() and time.monotonic() < end:
            self.events[0].wait(min(0.1, max(0, end - time.monotonic())))
        return self.is_set()


class CollectionMixin:
    def _platform_job(self, platform):
        return getattr(self, 'active_jobs', {}).get(platform)

    def _platform_busy(self, platform):
        return self._platform_job(platform) is not None

    def _refresh_job_controls(self):
        jobs = getattr(self, 'active_jobs', {})
        self.busy = bool(jobs)
        current = jobs.get(self._platform(), {})
        self.active_kind = current.get('kind')
        self.active_account = current.get('account')
        self.active_platform = self._platform() if current else None
        if self.closing:
            return
        self.platforms.configure(state='readonly')
        self.accounts.configure(state='normal')
        for button in self.actions:
            button.configure(state='disabled' if current else 'normal')
        if hasattr(self, 'import_backup_button'):
            self.import_backup_button.configure(state='disabled' if self.busy else 'normal')
        if hasattr(self, 'collect_all_button'):
            self.collect_all_button.configure(state='disabled' if len(jobs) == len(PLATFORMS) else 'normal')
        self.stop_button.configure(state='normal' if self.busy else 'disabled')
        self._apply_platform_view()

    def _start(self, kind, payload=None, account=None, platform=None):
        platform = validate_platform(platform or self._platform())
        if self.closing or self._platform_busy(platform) or (self.busy and self.cancel.is_set()):
            return False
        account = account or self._account()
        if not isinstance(account, str) or not account.strip() or len(account) > 80 or '\x00' in account:
            raise ValueError('请输入当前平台后台显示的实际账号名称。')
        account = account.strip()
        if kind == 'html' and platform != 'wechat':
            raise ValueError('HTML 导入仅适用于微信公众号。')
        if not hasattr(self, 'active_jobs'):
            self.active_jobs = {}
        if not self.active_jobs:
            self.cancel.clear()
        cancel_event = threading.Event()
        if kind == 'collect':
            payload = {'detail_state': self.store.detail_refresh_state(account, platform=platform),
                       'force_details': self.force_details.get() if platform == 'wechat' else False}
            if platform == 'wechat':
                payload['excluded_aliases'] = self.store.excluded_article_aliases(account, platform=platform)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload['_job_cancel'] = cancel_event
        job = {'kind': kind, 'account': account, 'cancel_event': cancel_event,
               'batch_counts': {'inserted': 0, 'skipped': 0, 'conflicts': 0}, 'batch_rows': 0}
        self.active_jobs[platform] = job
        self._refresh_job_controls()
        self.status.set(f'{PLATFORMS[platform]} / {account}：正在执行…')
        self.commands.put((kind, platform, account, payload))
        self._schedule_status()
        return True

    def stop_collection(self):
        self.cancel.set()
        for job in getattr(self, 'active_jobs', {}).values():
            job['cancel_event'].set()
        self.monitor_cancel.set()
        self.status.set('正在停止各平台采集，请稍候…')

    def _end_busy(self, platform=None):
        platform = platform or self.active_platform or self._platform()
        getattr(self, 'active_jobs', {}).pop(platform, None)
        self._refresh_job_controls()
        self._schedule_status()

    def _run_monitor(self, collector, account, payload, job_cancel=None):
        from .app import safe_error
        payload['started_at'] = now_iso()
        platform = payload.get('platform', 'wechat')
        try:
            event = payload.get('cancel_event', self.monitor_cancel)
            cancel = CombinedCancel(self.cancel, event, job_cancel or payload.get('_job_cancel') or threading.Event())
            if payload['generation'] != self.monitor_generation or cancel.is_set():
                result = {'observations': [], 'warnings': [], 'cancelled': True}
            else:
                result = collector.collect_targets(payload['targets'], cancel,
                    progress=lambda text: self.queue.put(('progress', f'{PLATFORMS[platform]}：{text}')),
                    expected_account=account)
                self._check_result_account(account, result)
            self.queue.put(('monitor_done', account, payload, result))
        except Exception as exc:
            self.queue.put(('monitor_error', account, payload, safe_error(exc)))

    @staticmethod
    def _check_result_account(account, result):
        detected = result.get('detected_account') or result.get('account_name')
        if detected and detected.strip() != account.strip():
            raise ValueError('登录账号与当前数据集不一致，请切换到正确账号后重试。')

    def _monitor_checkpoint(self, collector, account, platform='wechat', job_cancel=None):
        try:
            monitor_platform, monitor_account, payload = self.monitor_requests.get_nowait()
        except queue.Empty:
            return
        if monitor_platform != platform:
            # Another platform owns this request. Leave it for that worker.
            self.monitor_requests.put((monitor_platform, monitor_account, payload))
        elif monitor_account == account:
            self._run_monitor(collector, account, payload, job_cancel)
        else:
            payload['started_at'] = now_iso()
            self.queue.put(('monitor_error', monitor_account, payload, '重点采集与当前浏览器账号不一致。'))

    def _worker(self):
        """Dispatcher never touches a browser; each platform has a stable owner."""
        workers = {}
        while True:
            job = self.commands.get()
            if job is None:
                break
            platform = job[1]
            if platform not in workers:
                commands = queue.Queue()
                worker = threading.Thread(target=self._platform_worker, args=(platform, commands),
                                          daemon=True, name='browser-' + platform)
                workers[platform] = (commands, worker)
                worker.start()
            workers[platform][0].put(job)
        for commands, worker in workers.values():
            commands.put(None)
        for commands, worker in workers.values():
            worker.join()
        self.queue.put(('closed',))

    def _platform_worker(self, platform, commands):
        from .app import safe_error
        collector, collector_account = None, None
        log = lambda text: self.queue.put(('log', f'{PLATFORMS[platform]}：{text}'))
        while True:
            job = commands.get()
            if job is None:
                break
            kind, _, account, payload = job
            cancel_event = (payload or {}).get('_job_cancel') if isinstance(payload, dict) else None
            cancel = CombinedCancel(self.cancel, cancel_event or threading.Event())
            if kind == 'monitor':
                payload['started_at'] = now_iso()
            try:
                if cancel.is_set():
                    result = {'observations': [], 'warnings': [], 'cancelled': True, 'partial': True}
                    if kind == 'monitor':
                        self.queue.put(('monitor_done', account, payload, result))
                        continue
                elif kind == 'html':
                    if platform != 'wechat':
                        raise ValueError('HTML 导入仅适用于微信公众号。')
                    result = {'observations': [], 'warnings': []}
                    for path in payload:
                        if cancel.is_set():
                            result.update(cancelled=True, partial=True)
                            break
                        parsed = parse_file(path)
                        result['observations'].extend(parsed['observations'])
                        result['warnings'].extend(parsed.get('warnings', []))
                        log(f'{Path(path).name}：解析 {len(parsed["observations"])} 条观测')
                else:
                    if collector is None or collector_account != account:
                        if collector:
                            collector.close()
                        collector, collector_account = None, None
                        identity = account if platform == 'wechat' else platform + '\x00' + account
                        profile = self.data_dir / 'browser_profiles' / hashlib.sha256(identity.encode()).hexdigest()[:20]
                        if platform == 'wechat':
                            from .collector import BrowserCollector
                            collector = BrowserCollector(profile, log)
                        else:
                            from .social_collector import SocialCollector
                            collector = SocialCollector(profile, platform, log=log)
                        collector_account = account
                        if kind in ('collect', 'monitor'):
                            collector.open_login()
                    if kind == 'login':
                        collector.open_login()
                        result = ({'observations': [], 'warnings': [], 'cancelled': True, 'partial': True}
                                  if cancel.is_set() else None)
                    elif kind == 'monitor':
                        self._run_monitor(collector, account, payload, cancel_event)
                        continue
                    else:
                        options = {}
                        if platform == 'wechat':
                            options['excluded_aliases'] = (payload or {}).get('excluded_aliases', ())
                        result = collector.collect(cancel,
                            progress=lambda text: self.queue.put(('progress', f'{PLATFORMS[platform]}：{text}')),
                            expected_account=account,
                            detail_state=(payload or {}).get('detail_state', {}),
                            force_details=(payload or {}).get('force_details', False),
                            checkpoint=lambda: self._monitor_checkpoint(collector, account, platform, cancel_event),
                            on_batch=lambda rows: self.queue.put(('batch', platform, account, rows)),
                            **options)
                        self._check_result_account(account, result)
                self.queue.put(('done', kind, platform, account, result))
            except Exception as exc:
                if kind == 'monitor':
                    self.queue.put(('monitor_error', account, payload, safe_error(exc)))
                else:
                    self.queue.put(('error', kind, platform, account, safe_error(exc)))
        if collector:
            try:
                collector.close()
            except Exception:
                pass

    def _resume_queued_monitor(self):
        try:
            platform, account, payload = self.monitor_requests.get_nowait()
        except queue.Empty:
            return
        if (self.closing or payload['generation'] != self.monitor_generation
                or payload.get('cancel_event', self.monitor_cancel).is_set() or self.cancel.is_set()):
            self._monitor_completed(account, payload, {'cancelled': True})
        elif self._platform_busy(platform):
            self.monitor_requests.put((platform, account, payload))
        else:
            self._start('monitor', payload, account, platform)

    def _set_account_blocked(self, platform, account, blocked):
        self.store.set_platform_schedule_blocked(platform, account, blocked)
        # Keep old settings readable for legacy migration and older clients.
        if (self.store.get_setting('schedule_account') == account and
                self.store.get_setting('schedule_platform', 'wechat') == platform):
            self.store.set_setting('schedule_blocked', blocked)
        cfg = self.store.get_setting('monitor_config', {})
        if cfg.get('account') == account and cfg.get('platform', 'wechat') == platform:
            self.store.set_setting('monitor_blocked', blocked)

    def _save_result(self, kind, account, result, platform='wechat'):
        counts = self.store.add_observations(account, result.get('observations', []), platform=platform)
        if kind == 'collect':
            job = self._platform_job(platform) or {}
            counts['skipped'] = max(0, counts['skipped'] - job.get('batch_rows', 0))
            for key in counts:
                counts[key] += job.get('batch_counts', {}).get(key, 0)
        prefix = f'{PLATFORMS[platform]} / {account}：'
        self.log(prefix + f'完成：新增 {counts["inserted"]} 条，跳过重复 {counts["skipped"]} 条。')
        if counts.get('conflicts'):
            self.log(prefix + f'有 {counts["conflicts"]} 条同一时间指标冲突，保留已存记录。')
        for warning in result.get('warnings', []):
            self.log(prefix + warning)
        if kind != 'monitor' and (result.get('login_required') or result.get('blocked')):
            self._set_account_blocked(platform, account, True)
            self.log(prefix + '采集需人工检查，相关定时已暂停；请查看浏览器及运行记录。')
        self.status.set(prefix + f'本次新增 {counts["inserted"]} 条观测' +
            (' · 采集未完整，请查看运行记录' if result.get('partial') or result.get('cancelled') else ''))
        self.refresh()
        if kind == 'collect':
            if platform == 'wechat':
                self.log(f"数据中心：本次读取 {result.get('details_read', 0)} 篇，跳过 24 小时内已采集 {result.get('details_skipped', 0)} 篇。")

    def _pump(self):
        from .app import safe_error
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == 'closed':
                    self.store.close()
                    self.root.destroy()
                    return
                platform = account = None
                try:
                    if item[0] in ('log', 'progress'):
                        self.log(item[1])
                        self.status.set(safe_error(item[1]))
                    elif item[0] == 'batch':
                        _, platform, account, rows = item
                        job = self._platform_job(platform)
                        if job and job.get('save_failed'):
                            continue
                        counts = self.store.add_observations(account, rows, platform=platform)
                        if job:
                            job['batch_rows'] += len(rows)
                            for key in counts:
                                job['batch_counts'][key] += counts[key]
                        if time.monotonic() - getattr(self, '_batch_refresh_at', 0) >= 3:
                            self.refresh()
                            self._batch_refresh_at = time.monotonic()
                    elif item[0] in ('monitor_done', 'monitor_error'):
                        _, account, payload, value = item
                        platform = payload.get('platform', 'wechat')
                        if item[0] == 'monitor_done':
                            if not (self._platform_job(platform) or {}).get('save_failed'):
                                self._save_result('monitor', account, value, platform)
                            self._monitor_completed(account, payload, value)
                        else:
                            self.log(f'{PLATFORMS[platform]}：{value}')
                            self._monitor_completed(account, payload, error=value)
                        if (self._platform_job(platform) or {}).get('kind') == 'monitor':
                            self._end_busy(platform)
                    elif item[0] in ('done', 'error'):
                        _, kind, platform, account, value = item
                        if item[0] == 'error':
                            self.log(f'{PLATFORMS[platform]}：{value}')
                            self.status.set(f'{PLATFORMS[platform]}操作未完成，详情见运行记录。')
                            if kind == 'collect':
                                self._set_account_blocked(platform, account, True)
                        elif (self._platform_job(platform) or {}).get('save_failed'):
                            self.status.set(f'{PLATFORMS[platform]}保存失败，已停止该平台本轮采集。')
                        elif value is not None:
                            self._save_result(kind, account, value, platform)
                        else:
                            self._set_account_blocked(platform, account, False)
                            self.log(f'{PLATFORMS[platform]}采集浏览器已打开，请手动登录。')
                            self.status.set(f'请在{PLATFORMS[platform]}浏览器中完成登录，然后开始采集。')
                        self._end_busy(platform)
                        self._resume_queued_monitor()
                        self._monitor_status()
                except Exception as exc:
                    job = self._platform_job(platform) if platform else None
                    if job:
                        job['cancel_event'].set()
                        job['save_failed'] = True
                    self.log(f'{PLATFORMS.get(platform, "")}保存结果失败：{safe_error(exc)}')
                    self.status.set('保存失败，已停止该平台本轮采集；请查看运行记录。')
                    if platform and account:
                        try:
                            self._set_account_blocked(platform, account, True)
                        except Exception as blocked_exc:
                            # A full/read-only disk can also prevent saving the
                            # paused flag. Keep draining other platforms' events.
                            self.log('无法保存定时暂停状态，请检查存储空间或写入权限：' + safe_error(blocked_exc))
                    if item[0] in ('done', 'error'):
                        self._end_busy(platform)
                        self._resume_queued_monitor()
                    elif item[0].startswith('monitor_'):
                        self._monitor_completed(account, payload, error='保存结果失败')
                        if job and job.get('kind') == 'monitor':
                            self._end_busy(platform)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)
