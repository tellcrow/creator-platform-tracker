"""Visible Chrome collection of Xiaohongshu / Douyin content lists only.

The persistent profile belongs to this application. No cookies or credentials
are copied from the user's Chrome profile. Login is always performed by the user.
"""
from __future__ import annotations

from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .collector import AccountChanged, CollectionCancelled, CollectionStopped, LoginRequired
from .social_dom import ACCOUNT_JS, SCROLL_JS, SNAPSHOT_JS
from .social_parser import is_platform_url, parse_snapshot

HOME_URLS = {
    'xiaohongshu': 'https://creator.xiaohongshu.com/new/home',
    'douyin': 'https://creator.douyin.com/creator-micro/home',
}
LIST_URLS = {
    'xiaohongshu': 'https://creator.xiaohongshu.com/new/note-manager',
    'douyin': 'https://creator.douyin.com/creator-micro/content/manage',
}


class SocialCollector:
    def __init__(self, profile_dir, platform, log=lambda text: None, *,
                 max_pages=2000, pace_seconds=0.5, timeout_ms=20000):
        if platform not in HOME_URLS:
            raise ValueError('不支持的内容平台。')
        self.profile_dir = Path(profile_dir)
        self.platform = platform
        self.log = log
        self.max_pages = max(1, int(max_pages))
        self.pace_seconds = max(0.25, float(pace_seconds))
        self.timeout_ms = max(1000, int(timeout_ms))
        self._playwright = self._context = self._page = self._identity_page = None
        self._thread_id = None
        self._active_account = None
        self._active_identity_id = None
        self._last_identity_id = None

    def _check_thread(self):
        current = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current
        elif current != self._thread_id:
            raise RuntimeError('浏览器操作必须在同一个工作线程执行。')

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CollectionCancelled('采集已取消。')

    def _pause(self, cancel_event, duration=None):
        self._check_cancel(cancel_event)
        duration = self.pace_seconds if duration is None else duration
        if cancel_event is not None:
            if cancel_event.wait(duration):
                self._check_cancel(cancel_event)
        else:
            time.sleep(duration)

    def open_login(self):
        self._check_thread()
        if self._context is not None:
            try:
                if self._page and not self._page.is_closed():
                    self._page.bring_to_front()
                    self.log('采集浏览器已打开，请自行确认登录状态。')
                    return {'detected_account': None, 'login_required': True}
            except Exception:
                pass
            self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError('缺少 Playwright，请重新安装完整程序。') from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir / 'chrome'), channel='chrome',
                headless=False, viewport={'width': 1440, 'height': 1000},
                accept_downloads=False,
            )
            self._context.set_default_timeout(min(5000, self.timeout_ms))
            self._context.set_default_navigation_timeout(self.timeout_ms)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.goto(HOME_URLS[self.platform], wait_until='domcontentloaded', timeout=self.timeout_ms)
        except Exception as exc:
            self.close()
            raise RuntimeError('无法打开 Chrome 采集窗口。请确认已安装 Chrome，并关闭重复运行的采集器。') from exc
        self.log('已打开独立 Chrome。请自行登录；不会读取或复制日常浏览器的 Cookie。')
        return {'detected_account': None, 'login_required': True}

    def close(self):
        self._check_thread()
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
        finally:
            self._context = self._page = self._identity_page = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    def _validate_page(self, page):
        if page is None or page.is_closed():
            raise CollectionStopped('采集浏览器已关闭，请重新打开登录窗口。')
        if not is_platform_url(page.url, self.platform):
            raise CollectionStopped('页面已离开所选平台的创作后台，已停止采集。')
        if 'login' in urlsplit(page.url).path.lower():
            raise LoginRequired('平台登录已失效，请重新登录后再采集。')

    def _account_page(self, cancel_event):
        if self.platform == 'xiaohongshu':
            return self._page
        # Douyin's management header does not display its account name. Read the
        # actual creator homepage in the same dedicated browser context instead.
        if self._identity_page is None or self._identity_page.is_closed():
            self._identity_page = self._context.new_page()
        self._check_cancel(cancel_event)
        self._identity_page.goto(HOME_URLS[self.platform], wait_until='domcontentloaded', timeout=self.timeout_ms)
        return self._identity_page

    def _detect_account(self, cancel_event):
        page = self._account_page(cancel_event)
        end = time.monotonic() + self.timeout_ms / 1000
        while True:
            self._check_cancel(cancel_event)
            self._validate_page(page)
            identity = page.evaluate(ACCOUNT_JS, self.platform)
            if identity.get('login_required'):
                raise LoginRequired('平台页面要求重新登录，本轮已停止。')
            names = identity.get('names', [])
            names = {str(name).strip() for name in names if isinstance(name, str) and name.strip()}
            ids = {value for value in identity.get('ids', []) if isinstance(value, str) and value.strip()}
            if len(names) > 1:
                raise AccountChanged('页面出现多个账号名称，无法确认数据归属；本轮未保存。')
            if len(ids) > 1:
                raise AccountChanged('页面出现多个账号标识，无法确认数据归属；本轮未保存。')
            if names and (self.platform != 'douyin' or len(ids) == 1):
                self._last_identity_id = next(iter(ids), None)
                return names.pop()
            if time.monotonic() >= end:
                raise LoginRequired('未能从平台页面核对账号名称。请登录，并将软件账号名称填写为平台显示的名称。')
            self._pause(cancel_event, 0.25)

    def _verify_account(self, expected_account, cancel_event):
        account = self._detect_account(cancel_event)
        if account != expected_account:
            raise AccountChanged('当前登录账号与选定的数据账号不一致；本轮未保存，请先核对账号名称。')
        if self.platform == 'douyin':
            if not self._last_identity_id:
                raise LoginRequired('未能核对抖音号，本轮未保存。')
            if self._active_identity_id and self._active_identity_id != self._last_identity_id:
                raise AccountChanged('检测到抖音号切换，即使昵称相同也不会合并采集，本轮未保存。')
            self._active_identity_id = self._last_identity_id
        return account

    def _navigate_list(self, cancel_event):
        self._check_cancel(cancel_event)
        self._validate_page(self._page)
        target = LIST_URLS[self.platform]
        current = urlsplit(self._page.url)
        if current.path == urlsplit(target).path and not current.query:
            self._page.reload(wait_until='domcontentloaded', timeout=self.timeout_ms)
        else:
            self._page.goto(target, wait_until='domcontentloaded', timeout=self.timeout_ms)
        self._page.bring_to_front()

    def _snapshot(self, cancel_event):
        end = time.monotonic() + self.timeout_ms / 1000
        while True:
            self._check_cancel(cancel_event)
            self._validate_page(self._page)
            raw = self._page.evaluate(SNAPSHOT_JS, self.platform)
            result = parse_snapshot(raw, self.platform)
            meta = result['metadata']
            if meta['login_required']:
                raise LoginRequired('平台页面要求重新登录，本轮已停止。')
            if not meta['loading'] and (meta['raw_count'] > 0 or
                                       (meta['total'] == 0 and meta['empty_confirmed'])):
                return result
            if time.monotonic() >= end:
                raise CollectionStopped('内容列表加载超时，未把未加载的数据当作零值保存。')
            self._pause(cancel_event, 0.25)

    def _scroll(self, cancel_event):
        self._check_cancel(cancel_event)
        self._page.evaluate(SCROLL_JS, self.platform)
        self._pause(cancel_event)

    @staticmethod
    def _new_result():
        return {'observations': [], 'warnings': [], 'page_type': 'live',
                'partial': False, 'cancelled': False, 'blocked': False,
                'pages': 0, 'detected_account': None, 'login_required': False,
                'details_complete': True, 'details_read': 0, 'details_skipped': 0}

    def collect(self, cancel_event, progress=None, expected_account=None, *,
                detail_state=None, checkpoint=None, on_batch=None, force_details=False):
        """Refresh the full list and save one observed snapshot per work.

        A partial scroll is never a successful full round. Account identity is
        checked again before any observations or batch callbacks leave this class.
        """
        self._check_thread()
        result = self._new_result()
        emit = progress or self.log
        pending = {}
        account_verified = False
        failure = None
        try:
            self._check_cancel(cancel_event)
            if not isinstance(expected_account, str) or not expected_account.strip():
                raise AccountChanged('请先填写要采集的平台账号名称，不能在未核对账号的情况下采集。')
            expected_account = expected_account.strip()
            if self._context is None or self._page is None:
                raise LoginRequired('请先打开登录窗口并完成登录。')
            result['detected_account'] = self._verify_account(expected_account, cancel_event)
            self._active_account = expected_account
            self._navigate_list(cancel_event)
            previous_count = -1
            stable = 0
            expected_total = None
            last_growth_at = time.monotonic()
            for number in range(1, self.max_pages + 1):
                self._check_cancel(cancel_event)
                snap = self._snapshot(cancel_event)
                meta = snap['metadata']
                if meta.get('account_ambiguous'):
                    raise AccountChanged('内容列表显示多个账号，无法核对数据归属。')
                detected = snap.get('detected_account')
                if detected and detected != expected_account:
                    raise AccountChanged('采集过程中检测到账号切换，本轮未保存。')
                if self.platform == 'xiaohongshu' and not detected:
                    raise LoginRequired('内容列表账号名称不可见，无法安全核对账号归属。')
                if not meta['filters_default']:
                    raise CollectionStopped('当前列表不是已验证的全部作品默认筛选，请清空搜索和日期、状态筛选后重试。')
                total = meta['total']
                if total is None:
                    raise CollectionStopped('未读取到作品总数，无法确认全量采集是否完整。')
                if total == 0 and not meta.get('empty_confirmed'):
                    raise CollectionStopped('页面尚未确认空列表，未把临时零值当作采集成功。')
                if expected_total is not None and total != expected_total:
                    raise CollectionStopped('采集中作品总数发生变化，本轮仅保留已读取内容，请重新采集。')
                expected_total = total
                for row in snap['observations']:
                    pending.setdefault(row['article_key'], row)
                result['warnings'].extend(snap['warnings'])
                result['pages'] = number
                if meta['invalid_cards']:
                    raise CollectionStopped('列表中存在无法识别或重复的作品卡片，本轮采集不完整。')
                count = len(pending)
                emit(f'已读取 {count} / {total} 条内容，仅采集管理列表。')
                stable = stable + 1 if count == previous_count else 0
                if count != previous_count:
                    last_growth_at = time.monotonic()
                previous_count = count
                if count > total:
                    raise CollectionStopped('采集数量超过页面总数，可能发生标题变更或列表重排，本轮采集不完整。')
                if count == total and (meta['end_reached'] or (meta['at_bottom'] and stable >= 2)):
                    break
                if stable >= 3 and time.monotonic() - last_growth_at >= self.timeout_ms / 1000:
                    raise CollectionStopped('列表连续滚动后没有新增内容，但尚未核对完整总数和末尾，本轮采集不完整。')
                if number == self.max_pages:
                    raise CollectionStopped('已达到本轮列表读取上限，本轮采集不完整。')
                self._scroll(cancel_event)
        except Exception as exc:
            failure = exc
        # Buffered snapshots are released only after the account is rechecked.
        # An identity/login error invalidates the whole round, including batches.
        if result['detected_account'] and not isinstance(failure, (AccountChanged, LoginRequired, CollectionCancelled)):
            try:
                self._verify_account(expected_account, cancel_event)
                account_verified = True
            except Exception as exc:
                failure = exc
        if failure is not None:
            result['partial'] = True
            result['cancelled'] = isinstance(failure, CollectionCancelled)
            result['login_required'] = isinstance(failure, LoginRequired)
            result['blocked'] = not result['cancelled']
            message = str(failure) if isinstance(failure, CollectionStopped) else (
                f'浏览器读取中断（{type(failure).__name__}），请检查登录和页面状态。')
            result['warnings'].append(message)
        if account_verified:
            result['observations'] = list(pending.values())
            if result['observations'] and on_batch is not None:
                on_batch(result['observations'])
        result['warnings'] = list(dict.fromkeys(result['warnings']))
        emit(f'本轮读取 {len(result["observations"])} 条内容' + ('，采集未完整。' if result['partial'] else '，已核对全部作品数量。'))
        return result

    def collect_targets(self, targets, cancel_event, progress=None, expected_account=None, *, on_batch=None):
        """Refresh the list, then retain only selected works; no detail clicks.

        The first implementation deliberately scans the complete verified list:
        target IDs must not depend on a page position that shifts with new posts.
        """
        if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 5:
            raise ValueError('重点采集每次请选择 1 至 5 条内容。')
        wanted = set()
        for target in targets:
            item = target if isinstance(target, dict) else {'article_key': target}
            key = item.get('article_key')
            if not isinstance(key, str) or not key.strip():
                raise ValueError('重点采集缺少作品标识，请重新选择。')
            if item.get('platform', self.platform) != self.platform:
                raise ValueError('重点采集作品与所选平台不一致。')
            wanted.add(key)
        result = self.collect(cancel_event, progress=progress, expected_account=expected_account)
        result['observations'] = [row for row in result['observations'] if row['article_key'] in wanted]
        result['targets_requested'] = len(wanted)
        result['targets_found'] = len(result['observations'])
        missing = wanted - {row['article_key'] for row in result['observations']}
        if missing and not result['cancelled']:
            result['partial'] = True
            result['blocked'] = True
            result['warnings'].append(f'有 {len(missing)} 条重点内容未在当前列表找到；请核对作品状态或是否改名。')
        if result['observations'] and on_batch is not None:
            on_batch(result['observations'])
        return result
