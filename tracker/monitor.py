"""Independent short-interval article monitoring; all browser work stays on its owner thread."""
from datetime import datetime, timedelta
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .schema import PLATFORMS, primary_metric, now_iso
from .scheduler import validate_monitor_config, is_due


class MonitorMixin:
    def _init_monitor(self):
        self.monitor_requests = queue.Queue()
        self.monitor_cancel = threading.Event()
        self.monitor_generation = 0
        self.monitor_pending = False
        self.monitor_keys = []
        self.monitor_view_account = None
        self.monitor_search = tk.StringVar()
        self.monitor_interval = tk.StringVar(value='60')
        self.monitor_unit = tk.StringVar(value='秒')
        self.monitor_note = tk.StringVar()
        self.monitor_selection_note = tk.StringVar(value='已选 0 / 5 篇')
        self.active_kind = self.active_account = self.active_platform = None
        self._finish_after_monitor = False
        self._batch_counts = {'inserted': 0, 'skipped': 0, 'conflicts': 0}

    def _build_monitor(self, frame):
        ttk.Label(frame, text='重点文章采集', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
        self.monitor_help = ttk.Label(frame, style='Sub.TLabel', wraplength=1100)
        self.monitor_help.pack(anchor='w', pady=8)
        filters = ttk.Frame(frame); filters.pack(fill='x', pady=5)
        ttk.Label(filters, text='搜索标题').pack(side='left')
        ttk.Entry(filters, textvariable=self.monitor_search, width=40).pack(side='left', padx=8)
        self.monitor_search.trace_add('write', lambda *_: self._render_monitor_articles())
        ttk.Button(filters, text='加入观察（最多 5 篇）', command=self._add_monitor_articles).pack(side='left')
        content = ttk.Frame(frame); content.pack(fill='both', expand=True)
        left = ttk.Frame(content); left.pack(side='left', fill='both', expand=True)
        self.monitor_tree = self._tree(left, ['number', 'title', 'published', 'read'],
                                       ['序号', '文章标题', '发布时间', '阅读人数'], [55, 360, 150, 100], height=8)
        self.monitor_tree.configure(selectmode='extended')
        self.monitor_tree.bind('<Double-1>', lambda e: self._add_monitor_articles())
        right = ttk.Frame(content, padding=(12, 0)); right.pack(side='right', fill='y')
        ttk.Label(right, textvariable=self.monitor_selection_note).pack(anchor='w')
        self.monitor_chosen = tk.Listbox(right, width=34, height=8, exportselection=False,
                                        font=('Microsoft YaHei UI', 10), relief='flat')
        self.monitor_chosen.pack(fill='y', expand=True, pady=8)
        ttk.Button(right, text='移除选中文章', command=self._remove_monitor_article).pack(fill='x')
        controls = ttk.Frame(frame); controls.pack(fill='x', pady=14)
        ttk.Label(controls, text='采集间隔').pack(side='left')
        ttk.Entry(controls, textvariable=self.monitor_interval, width=9).pack(side='left', padx=8)
        ttk.Combobox(controls, textvariable=self.monitor_unit, values=['秒', '分钟', '小时', '天'],
                     state='readonly', width=6).pack(side='left', padx=(0, 16))
        ttk.Button(controls, text='保存并开始', command=self.start_monitor, style='Accent.TButton').pack(side='left', padx=4)
        ttk.Button(controls, text='暂停', command=self.pause_monitor).pack(side='left', padx=4)
        ttk.Button(controls, text='立即采集所选', command=self.collect_monitor_once).pack(side='left', padx=4)
        ttk.Label(frame, textvariable=self.monitor_note, wraplength=1100).pack(fill='x', pady=8)
        self.monitor_footer = ttk.Label(frame, style='Sub.TLabel', wraplength=1100)
        self.monitor_footer.pack(fill='x')

    def _load_monitor(self):
        try:
            cfg = validate_monitor_config(self.store.get_setting('monitor_config', {}))
        except (ValueError, TypeError):
            cfg = validate_monitor_config({})
            self.store.set_setting('monitor_config', cfg)
        if not cfg['enabled']:
            self.store.set_setting('monitor_next', None)
        elif not self.store.get_setting('monitor_next'):
            self.store.set_setting('monitor_next', now_iso())
        self.monitor_view_account = None
        self._render_monitor_articles()
        self._monitor_status()

    def _render_monitor_articles(self):
        if not hasattr(self, 'monitor_tree'):
            return
        from .app import display, stamp
        account = self.account.get().strip()
        platform = self._platform()
        view = (platform, account)
        if view != self.monitor_view_account:
            cfg = self.store.get_setting('monitor_config', {})
            self.monitor_keys = list(cfg.get('targets', [])) if (cfg.get('platform', 'wechat'), cfg.get('account')) == view else []
            self.monitor_interval.set(str(cfg.get('interval_seconds', 60)))
            self.monitor_unit.set('秒')
            self.monitor_view_account = view
        selection = self.monitor_tree.selection()
        self.monitor_tree.delete(*self.monitor_tree.get_children())
        needle = self.monitor_search.get().strip().casefold()
        for aid, row in sorted(self.rows.items(), key=lambda pair: pair[1].get('number', 0), reverse=True):
            if row.get('status') == 'deleted' or (needle and needle not in row.get('title', '').casefold()):
                continue
            self.monitor_tree.insert('', 'end', iid=aid, values=[row.get('number', ''), row.get('title', ''),
                stamp(row.get('publish_time') or row.get('publish_date')), display(row.get('metrics', {}).get(primary_metric(platform)))])
        visible = [aid for aid in selection if self.monitor_tree.exists(aid)]
        if visible:
            self.monitor_tree.selection_set(visible)
        self._render_monitor_chosen()

    def _render_monitor_chosen(self):
        aliases = self.store.article_alias_map(self.account.get().strip(), platform=self._platform())
        lookup = {key: self.rows.get(aid) for key, aid in aliases.items()}
        self.monitor_chosen.delete(0, 'end')
        for key in self.monitor_keys:
            row = lookup.get(key)
            self.monitor_chosen.insert('end', f"{row.get('number', '')}. {row['title']}" if row else f'{key}（本机未找到）')
        self.monitor_selection_note.set(f'已选 {len(self.monitor_keys)} / 5 篇')

    def _add_monitor_articles(self):
        chosen = [self.rows[aid]['article_key'] for aid in self.monitor_tree.selection() if aid in self.rows]
        keys = list(dict.fromkeys(self.monitor_keys + chosen))
        if len(keys) > 5:
            messagebox.showinfo('最多 5 篇', '重点采集最多选择 5 篇文章，请先移除部分已选文章。')
            return
        self.monitor_keys = keys
        self._render_monitor_chosen()

    def _remove_monitor_article(self):
        for index in reversed(self.monitor_chosen.curselection()):
            self.monitor_keys.pop(index)
        self._render_monitor_chosen()

    def _monitor_form_config(self):
        factor = {'秒': 1, '分钟': 60, '小时': 3600, '天': 86400}[self.monitor_unit.get()]
        seconds = float(self.monitor_interval.get()) * factor
        if not seconds.is_integer():
            raise ValueError('采集间隔需要换算为整数秒。')
        return validate_monitor_config({'enabled': True, 'account': self._account(), 'platform': self._platform(),
                    'targets': list(self.monitor_keys), 'interval_seconds': int(seconds)})

    def _monitor_payload(self, cfg):
        platform = cfg.get('platform', 'wechat')
        by_id = {str(row['article_id']): row for row in self.store.articles(cfg['account'], platform=platform)}
        rows = {key: by_id[aid] for key, aid in self.store.article_alias_map(cfg['account'], platform=platform).items() if aid in by_id}
        missing = [key for key in cfg['targets'] if key not in rows or rows[key].get('status') == 'deleted']
        if missing:
            raise ValueError('观察列表中有未找到或已删除的文章，请重新选择。')
        targets = {str(rows[key]['article_id']): rows[key] for key in cfg['targets']}
        return {'targets': list(targets.values()), 'generation': self.monitor_generation,
                'platform': platform, 'interval_seconds': cfg['interval_seconds'], 'cancel_event': self.monitor_cancel}

    def start_monitor(self):
        from .app import safe_error
        try:
            cfg = self._monitor_form_config()
            self._monitor_payload(cfg)
            self.monitor_generation += 1
            self.monitor_cancel.set()
            self.monitor_cancel = threading.Event()
            self.store.set_setting('monitor_config', cfg)
            self.store.set_setting('monitor_blocked', False)
            self.store.set_setting('monitor_next', now_iso())
            self._monitor_status()
            self.log(f"重点采集已开启：{len(cfg['targets'])} 篇，每 {cfg['interval_seconds']} 秒。")
        except Exception as exc:
            messagebox.showerror('无法开始重点采集', safe_error(exc))

    def pause_monitor(self):
        cfg = self.store.get_setting('monitor_config', {})
        cfg['enabled'] = False
        self.store.set_setting('monitor_config', cfg)
        self.store.set_setting('monitor_next', None)
        self.monitor_generation += 1
        self.monitor_cancel.set()
        self._monitor_status()
        self.log('重点采集已暂停。')

    def collect_monitor_once(self):
        from .app import safe_error
        try:
            if self.monitor_pending:
                self.status.set('已有一次重点采集正在执行或等待页面切换。')
                return
            cfg = self._monitor_form_config()
            if self._platform_busy(cfg.get('platform', 'wechat')) and not self._monitor_matches_active(cfg):
                self.status.set('当前任务完成后可手动采集这些文章。')
                return
            self.monitor_cancel = threading.Event()
            payload = self._monitor_payload(cfg)
            self._enqueue_monitor(cfg['account'], payload, platform=cfg.get('platform', 'wechat'))
        except Exception as exc:
            messagebox.showerror('无法采集文章', safe_error(exc))

    def _monitor_matches_active(self, cfg):
        job = self._platform_job(cfg.get('platform', 'wechat')) or {}
        return job.get('kind') == 'collect' and job.get('account') == cfg['account']

    def _enqueue_monitor(self, account, payload, platform=None):
        platform = platform or payload.get('platform', 'wechat')
        self.monitor_pending = True
        if self._platform_busy(platform):
            self.monitor_requests.put((platform, account, payload))
        else:
            self._start('monitor', payload, account, platform=platform)
        self._monitor_status()

    def _monitor_tick(self):
        if self.monitor_pending or self.closing:
            return
        try:
            cfg = validate_monitor_config(self.store.get_setting('monitor_config', {}))
            if not cfg['enabled'] or self.store.get_setting('monitor_blocked', False):
                return
            if self._platform_busy(cfg.get('platform', 'wechat')) and not self._monitor_matches_active(cfg):
                return
            if is_due(self.store.get_setting('monitor_next')):
                self.monitor_cancel = threading.Event()
                self._enqueue_monitor(cfg['account'], self._monitor_payload(cfg), platform=cfg.get('platform', 'wechat'))
        except (ValueError, TypeError) as exc:
            self.store.set_setting('monitor_blocked', True)
            self.log(f'重点采集已暂停：{exc}')
        self._monitor_status()

    def _monitor_completed(self, account, payload, result=None, error=None):
        self.monitor_pending = False
        # A completed job must never resurrect settings paused or replaced mid-run.
        cfg = self.store.get_setting('monitor_config', {})
        platform = payload.get('platform', 'wechat')
        if (payload.get('generation') == self.monitor_generation and cfg.get('account') == account
                and cfg.get('platform', 'wechat') == platform):
            if cfg.get('enabled') and cfg.get('account') == account:
                interval = cfg['interval_seconds']
                started = datetime.fromisoformat(payload.get('started_at') or now_iso())
                now = datetime.now().astimezone()
                due = started + timedelta(seconds=interval)
                if due <= now:
                    due = now + timedelta(seconds=interval)
                self.store.set_setting('monitor_next', due.isoformat())
            if result and (result.get('login_required') or result.get('blocked')):
                self.store.set_setting('monitor_blocked', True)
                if (result.get('login_required') and self.store.get_setting('schedule_account') == account
                        and self.store.get_setting('schedule_platform', 'wechat') == platform):
                    self.store.set_setting('schedule_blocked', True)
                if result.get('login_required'):
                    self.store.set_platform_schedule_blocked(platform, account, True)
            if error:
                # Stop unattended retries on account mismatches, login issues or changed pages.
                self.store.set_setting('monitor_blocked', True)
            if result and result.get('observations'):
                self.store.set_setting('monitor_last', max(row['observed_at'] for row in result['observations']))
            elapsed = (datetime.now().astimezone() - datetime.fromisoformat(payload.get('started_at') or now_iso())).total_seconds()
            self.store.set_setting('monitor_last_duration', round(elapsed, 1))
        self._monitor_status()

    def _monitor_status(self):
        from .app import stamp
        cfg = self.store.get_setting('monitor_config', {})
        state = '等待页面切换 / 正在采集' if self.monitor_pending else '等待下次采集' if cfg.get('enabled') else '已暂停'
        if self.monitor_pending and not cfg.get('enabled'):
            state = '单次采集执行中 / 定时已暂停'
        if self.store.get_setting('monitor_blocked'):
            state = '已暂停，请查看运行记录并重新登录或保存设置'
        platform = cfg.get('platform', 'wechat')
        self.monitor_note.set(f"{state}　绑定账号：{PLATFORMS.get(platform, platform)} / {cfg.get('account') or '—'}　"
            f"间隔：{cfg.get('interval_seconds', 60)} 秒\n"
            f"最近成功：{stamp(self.store.get_setting('monitor_last')) or '—'}　"
            f"上次耗时：{self.store.get_setting('monitor_last_duration', '—')} 秒　"
            f"下次：{stamp(self.store.get_setting('monitor_next')) or '—'}")

