"""Visible, user-initiated browser collection through WeChat's read-only UI.

No private API routes, credentials, or saved session tokens are embedded here.
The live adapter has not been verified against a signed-in account. All public
methods must run on the same worker thread, as required by sync Playwright.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from .parser import parse_html
from .schema import article_aliases, now_iso

HOME = "https://mp.weixin.qq.com/"
CARD = ".publish_record_history .weui-desktop-mass-media"
PAGINATION = ".weui-desktop-pagination"
CURRENT_PAGE = ".weui-desktop-pagination__num_current"
DATA_BUTTON = (
    '.weui-desktop-tooltip__wrp:has(> .weui-desktop-tooltip:text-is("发表数据")) > button'
)


class CollectionStopped(Exception):
    """An orderly stop; already collected observations remain available."""


class LoginRequired(CollectionStopped):
    pass


class CollectionCancelled(CollectionStopped):
    pass


class AccountChanged(CollectionStopped):
    pass


def _is_backend_url(url):
    """Restrict collection to the exact HTTPS backend, never arbitrary links."""
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "mp.weixin.qq.com"
                and p.port in (None, 443) and not p.username and not p.password)
    except (TypeError, ValueError):
        return False


def _public_url(url):
    if not _is_backend_url(url):
        return ""
    p = urlsplit(url)
    if not p.path.startswith("/s/"):
        return ""
    return urlunsplit(("https", "mp.weixin.qq.com", p.path, "", ""))


def _backend_href(base, href):
    """Resolve an observed DOM link, including the relative form before Save As."""
    if not isinstance(href, str) or not href.strip() or href.lstrip().startswith("#"):
        return ""
    resolved = urljoin(base, href.strip())
    return resolved if _is_backend_url(resolved) else ""


def _identity(rows):
    """Only article identity, not counters, detects actual pagination."""
    keys = sorted(str(r.get("article_key", "")) for r in rows
                  if r.get("source") == "publish")
    return hashlib.sha256("\n".join(keys).encode()).hexdigest() if keys else ""


def _page_location(url):
    """Diagnostic location without query parameters, credentials or fragments."""
    try:
        p = urlsplit(url)
        if p.scheme in ('http', 'https'):
            return f'协议={p.scheme}；主机={p.hostname or "未知"}；路径={p.path[:180]}'
        return 'about:blank' if url == 'about:blank' else f'协议={p.scheme or "未知"}'
    except (TypeError, ValueError):
        return '地址不可识别'


def _matching_article(row, other):
    def msgid(item):
        raw = str(item.get('msgid') or item.get('article_key') or '')
        match = re.fullmatch(r'(?:msgid:)?(\d+_\d+)', raw)
        return match[1] if match else None
    a_id, b_id = msgid(row), msgid(other)
    if a_id and b_id:
        return a_id == b_id
    if row.get("article_key") and row.get("article_key") == other.get("article_key"):
        return True
    a, b = _public_url(row.get("public_url", "")), _public_url(other.get("public_url", ""))
    return bool(a and b and a == b)


def _is_excluded(row, excluded_aliases):
    return row.get("status") == "deleted" or not excluded_aliases.isdisjoint(article_aliases(row))


def _detail_due(article_key, state, now=None, max_age_seconds=86400):
    previous = state.get(article_key) if isinstance(state, dict) else None
    if not isinstance(previous, dict) or previous.get('source') != 'detail':
        return True
    if previous.get('imported_at') or previous.get('time_basis') != 'live':
        return True
    try:
        observed = datetime.fromisoformat(previous['observed_at'])
        if observed.tzinfo is None:
            return True
        age = ((now or datetime.now().astimezone()) - observed).total_seconds()
        return not 0 <= age < max_age_seconds
    except (KeyError, TypeError, ValueError):
        return True


class BrowserCollector:
    def __init__(self, profile_dir, log=lambda text: None, *, max_pages=1000,
                 pace_seconds=0.5, timeout_ms=20000):
        self.profile_dir = Path(profile_dir)
        self.log = log
        self.max_pages = max(1, int(max_pages))
        self.pace_seconds = max(0.25, float(pace_seconds))
        self.timeout_ms = max(1000, int(timeout_ms))
        self._playwright = None
        self._context = None
        self._page = None
        self._thread_id = None
        self._active_account = None
        self._cache_account = None
        self._article_pages = {}
        self._detail_cache = {}
        self._monitor_page = None
        self._detail_state_active = {}
        self._checkpoint_active = None
        self._batch_active = None
        self._detail_counts_active = None

    def _check_thread(self):
        current = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current
        elif self._thread_id != current:
            raise RuntimeError("浏览器操作必须在同一个工作线程执行。")

    def open_login(self):
        """Open a dedicated visible Edge/Chrome profile for manual QR login."""
        self._check_thread()
        if self._context is not None:
            try:
                if self._page and not self._page.is_closed():
                    self._page.bring_to_front()
                    self.log("浏览器已打开；请自行确认公众号登录状态。")
                    account = self._detect_account(self._page)
                    return {"detected_account": account, "login_required": not bool(account)}
            except Exception:
                pass
            self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，请重新安装完整程序。") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        for channel in ("msedge", "chrome"):
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir / channel), channel=channel,
                    headless=False, viewport={"width": 1440, "height": 1000},
                    accept_downloads=False,
                )
                break
            except Exception:
                # Raw Playwright exception text can contain a profile path or URL.
                self._context = None
        if self._context is None:
            self._playwright.stop()
            self._playwright = None
            raise RuntimeError("无法启动 Edge 或 Chrome。请安装其中一个浏览器，并关闭重复打开的采集器。")
        self._context.set_default_timeout(min(5000, self.timeout_ms))
        self._context.set_default_navigation_timeout(self.timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(HOME, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self.log("已打开独立浏览器。请扫码登录；登录完成后再点击开始采集。")
        account = self._detect_account(self._page)
        return {"detected_account": account, "login_required": not bool(account)}

    def close(self):
        self._check_thread()
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass  # The user may already have closed the browser window.
        finally:
            self._context = None
            self._page = None
            self._monitor_page = None
            self._article_pages.clear()
            self._detail_cache.clear()
            self._cache_account = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    @staticmethod
    def _detect_account(page):
        # This exact (misspelled) class occurs in the supplied backend's footer.
        names = page.locator(".acount_box-nickname")
        found = {names.nth(n).inner_text().strip() for n in range(names.count())
                 if names.nth(n).is_visible()}
        found.discard("")
        return found.pop() if len(found) == 1 else None

    def _verify_account(self, page, required=False):
        account = self._detect_account(page)
        if required and not account:
            raise LoginRequired("未识别到当前公众号名称；请登录并展开左侧菜单后重试，避免数据归入错误账号。")
        if self._active_account and account and account != self._active_account:
            raise AccountChanged("采集中检测到公众号账号已切换；已停止，避免混合不同账号的数据。")
        return account

    @staticmethod
    def _check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CollectionCancelled("采集已取消；已取得的数据仍会保存。")

    def _pause(self, cancel_event, seconds=None):
        self._check_cancel(cancel_event)
        duration = self.pace_seconds if seconds is None else seconds
        if cancel_event is not None:
            if cancel_event.wait(duration):
                self._check_cancel(cancel_event)
        else:
            time.sleep(duration)

    def _validate_page(self, page):
        if page.is_closed():
            raise CollectionStopped("浏览器页面已关闭，请重新打开登录窗口。")
        if not _is_backend_url(page.url):
            raise CollectionStopped("页面已离开公众号后台，已停止本轮采集。")
        text = page.locator("body").inner_text(timeout=min(5000, self.timeout_ms))
        account = self._detect_account(page)
        if not account and re.search(r"登录(已)?过期|登录超时|重新(扫码)?登录|扫码登录|扫描二维码登录", text):
            raise LoginRequired("登录已失效或尚未完成；请在独立浏览器中重新扫码登录。")
        if re.search(r"(请完成|需要进行|请进行).{0,12}(安全验证|身份验证)|访问过于频繁", text):
            raise CollectionStopped("后台要求验证或限制访问；已停止采集，请在浏览器中查看提示。")

    def _navigate_to_publications(self, cancel_event):
        self._check_cancel(cancel_event)
        if self._context is None or self._page is None:
            raise LoginRequired("请先打开浏览器并完成登录，再开始采集。")
        self._validate_page(self._page)
        # Only use a real menu link observed in the loaded page.
        link = self._page.locator('a[title="发表记录"]').first
        if not link.count():
            raise CollectionStopped("未找到“发表记录”菜单；请确认已登录正确的公众号后台。")
        if not link.is_visible():
            expand = self._page.locator('span[title="内容管理"]').first
            if expand.count() and expand.is_visible():
                expand.click()
        if not link.is_visible():
            raise CollectionStopped("发表记录菜单不可见；请在浏览器展开内容管理后再试。")
        href = _backend_href(self._page.url, link.get_attribute("href"))
        if not href:
            raise CollectionStopped("发表记录菜单地址无法校验，已停止采集。")
        self._page.goto(href, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self._pause(cancel_event)
        # The observed menu normally opens page 1. If not, use the visible page-1 control.
        current = self._current_page()
        if current and current != "1":
            first = self._page.locator(PAGINATION).locator("label").filter(has_text=re.compile(r"^1$"))
            if first.count() != 1 or not first.is_visible():
                raise CollectionStopped("无法回到发表记录第 1 页，已停止，避免漏掉新文章。")
            first.click()
            self._pause(cancel_event)

    def _current_page(self):
        item = self._page.locator(CURRENT_PAGE).first
        return item.inner_text().strip() if item.count() else ""

    def _snapshot(self, page, expected, cancel_event, previous=None):
        deadline = time.monotonic() + self.timeout_ms / 1000
        stable = None
        while time.monotonic() < deadline:
            self._check_cancel(cancel_event)
            self._validate_page(page)
            self._verify_account(page, required=expected == "publish")
            result = parse_html(page.content(), observed_at=now_iso(),
                                source_file="在线采集", time_basis="live")
            rows = result.get("observations", [])
            wanted = [r for r in rows if r.get("source") == expected]
            loaded = result.get("page_type") == expected and wanted and all(
                r.get("status") == "deleted" or any(v is not None for v in r.get("metrics", {}).values())
                for r in wanted)
            signature = _identity(wanted) if expected == "publish" else "detail"
            if loaded and (not previous or signature != previous):
                # Two stable readings avoid accepting a transient half-rendered page.
                digest = json.dumps([(r.get("article_key"), r.get("metrics")) for r in rows],
                                    sort_keys=True, ensure_ascii=False)
                if stable == digest:
                    return result
                stable = digest
            else:
                stable = None
            self._pause(cancel_event, 0.5)
        if previous:
            raise CollectionStopped("翻页后文章未更新或仍在加载；已停止，避免重复记录及漏页。")
        raise CollectionStopped("页面为空、尚未加载完成或结构已变化；本页没有作为成功数据保存。")

    def _has_next(self):
        next_link = self._page.locator(PAGINATION).get_by_text("下一页", exact=True)
        if not next_link.count():
            return False
        item = next_link.first
        classes = item.get_attribute("class") or ""
        return item.is_visible() and item.is_enabled() and item.get_attribute("aria-disabled") != "true" and "disabled" not in classes

    def _click_next(self, cancel_event):
        self._check_cancel(cancel_event)
        self._page.locator(PAGINATION).get_by_text("下一页", exact=True).first.click()
        self._pause(cancel_event)

    def _expected_article(self, card, rows):
        title = card.locator("a.weui-desktop-mass-appmsg__title").first
        public = _public_url(_backend_href(self._page.url, title.get_attribute("href"))) if title.count() else ""
        if public:
            for row in rows:
                if _public_url(row.get("public_url", "")) == public:
                    return row
        links = card.locator("a[href]")
        for n in range(links.count()):
            href = _backend_href(self._page.url, links.nth(n).get_attribute("href"))
            if not href:
                continue
            query = parse_qs(urlsplit(href).query)
            msg = (query.get("appmsg_id") or query.get("appmsgid") or [None])[0]
            idx = (query.get("item_idx") or query.get("idx") or [None])[0]
            if msg and idx:
                key = f"{msg}_{idx}"
                for row in rows:
                    if str(row.get("article_key")) == key:
                        return row
        return None

    def _wait_for_detail(self, page, cancel_event):
        # A popup can exist before its first navigation. The blank document may
        # already satisfy domcontentloaded, so wait for its URL as well.
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            self._check_cancel(cancel_event)
            if page.is_closed():
                raise CollectionStopped('数据详情页已关闭，未保存详情数据。')
            url = page.url
            if url not in ('', 'about:blank'):
                if not _is_backend_url(url):
                    raise CollectionStopped('数据详情未进入公众号后台（' + _page_location(url) + '）。')
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=min(500, max(1, int(
                        (deadline - time.monotonic()) * 1000))))
                except Exception as exc:
                    if type(exc).__name__ != 'TimeoutError':
                        raise
                else:
                    self._check_cancel(cancel_event)
                    if page.url == url:
                        self._validate_page(page)
                        return
            page.wait_for_timeout(100)
        raise CollectionStopped('等待数据详情加载超时（' + _page_location(page.url) + '）；未保存详情数据。')

    def _collect_details(self, rows, cancel_event, emit, observations, warnings, *,
                         target_rows=None, force=False, foreground=False, excluded_aliases=frozenset()):
        complete = True
        selected = list(target_rows) if target_rows is not None else None
        matched = set()
        list_url = self._page.url  # Observed URL stays in memory; never log/export its token.
        list_signature = _identity(rows)
        cards = self._page.locator(CARD)
        count = cards.count()
        for index in range(count):
            self._check_cancel(cancel_event)
            if self._checkpoint_active is not None:
                self._checkpoint_active()
                self._check_cancel(cancel_event)
            card = self._page.locator(CARD).nth(index)
            if "weui-desktop-mass-media_del" in (card.get_attribute("class") or "").split():
                # Deleted cards may lack the identity needed to match a row.
                continue
            expected = self._expected_article(card, rows)
            if expected is not None and _is_excluded(expected, excluded_aliases):
                continue
            if selected is not None:
                target = next((r for r in selected if expected is not None and _matching_article(expected, r)), None)
                if target is None:
                    continue
                if target['article_key'] in matched:
                    continue
                matched.add(target['article_key'])
            if expected is None:
                warnings.append("一张内容卡片缺少可核对的文章标识，未打开其数据详情。")
                complete = False
                continue
            if not force and not _detail_due(expected.get('article_key'), self._detail_state_active):
                if self._detail_counts_active is not None:
                    self._detail_counts_active['details_skipped'] += 1
                continue
            label = expected.get("title") or "未命名文章"
            button = card.locator(DATA_BUTTON)
            if button.count() != 1:
                warnings.append(f"《{label}》没有可识别的“发表数据”入口；仅保存发表记录。")
                complete = False
                continue
            card.hover()
            if not button.is_visible() or not button.is_enabled():
                warnings.append(f"《{label}》的数据详情入口不可用；仅保存发表记录。")
                complete = False
                continue
            emit(f"核对数据中心：{label}")
            popups = []
            on_popup = lambda opened: popups.append(opened)
            self._page.on("popup", on_popup)
            detail = None
            same_tab = False
            restore_list = True
            try:
                button.click()
                deadline = time.monotonic() + self.timeout_ms / 1000
                while time.monotonic() < deadline:
                    self._check_cancel(cancel_event)
                    if popups:
                        detail = popups[-1]
                        break
                    if self._page.url != list_url:
                        detail, same_tab = self._page, True
                        break
                    # Pump Playwright events while allowing cancellation between ticks.
                    self._page.wait_for_timeout(200)
                if detail is None:
                    raise CollectionStopped("详情入口没有打开可识别页面。")
                if foreground:
                    detail.bring_to_front()
                self._wait_for_detail(detail, cancel_event)
                result = self._snapshot(detail, "detail", cancel_event)
                detail_rows = result.get("observations", [])
                matching = [r for r in detail_rows if _matching_article(r, expected)
                            and r.get("source") == "detail"]
                if not matching:
                    raise CollectionStopped("详情页文章标识不匹配或没有可用指标，未合并数据。")
                accepted = [r for r in matching if not _is_excluded(r, excluded_aliases)]
                if not accepted:
                    continue
                observations.extend(accepted)
                warnings.extend(result.get("warnings", []))
                for row in accepted:
                    if row.get('source') == 'detail':
                        self._detail_state_active[expected['article_key']] = row
                        self._detail_cache[expected['article_key']] = row
                if self._detail_counts_active is not None:
                    self._detail_counts_active['details_read'] += 1
                if self._batch_active is not None:
                    self._batch_active(list(accepted))
                emit(f'数据详情已读取：{label}；正在关闭详情并返回发表记录。')
            except (LoginRequired, CollectionCancelled, AccountChanged):
                restore_list = False
                raise
            except CollectionStopped as exc:
                complete = False
                warnings.append(f"《{label}》：{exc}")
            except Exception as exc:
                complete = False
                warnings.append(f"《{label}》详情读取未完成（{type(exc).__name__}）；保留已有数据。")
            finally:
                try:
                    self._page.remove_listener("popup", on_popup)
                except Exception:
                    pass  # Closing a page must not mask login/cancellation state.
                for opened in popups:
                    try:
                        opened.close()
                    except Exception:
                        pass
                if same_tab and restore_list and not (cancel_event is not None and cancel_event.is_set()):
                    self._page.goto(list_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    restored = self._snapshot(self._page, "publish", cancel_event)
                    if _identity(restored.get("observations", [])) != list_signature:
                        raise CollectionStopped("返回发表列表后页内容不一致；已停止以避免文章错配。")
            self._pause(cancel_event)
        if selected is not None:
            missing = [r for r in selected if r['article_key'] not in matched]
            if missing:
                complete = False
                warnings.append(f'有 {len(missing)} 篇目标文章未找到可核对的详情入口；仅保留发表记录。')
        return complete

    def _prepare_account(self, expected_account, result):
        if self._context is None or self._page is None:
            raise LoginRequired('请先打开浏览器并完成登录，再开始采集。')
        self._validate_page(self._page)
        self._active_account = None
        account = self._verify_account(self._page, required=True)
        result['detected_account'] = account
        if expected_account and account != expected_account.strip():
            raise AccountChanged('当前登录的公众号与选定的数据账号不一致；本轮未采集，请先切换到正确账号。')
        self._active_account = account
        if self._cache_account != account:
            self._article_pages.clear()
            self._detail_cache.clear()
            self._cache_account = account

    @staticmethod
    def _new_result():
        return {'observations': [], 'warnings': [], 'page_type': 'live',
                'partial': False, 'cancelled': False, 'pages': 0,
                'detected_account': None, 'login_required': False,
                'details_complete': True, 'details_read': 0, 'details_skipped': 0}

    @staticmethod
    def _record_error(result, exc):
        result['partial'] = True
        if isinstance(exc, CollectionCancelled):
            result['cancelled'] = True
        else:
            result['blocked'] = True
        if isinstance(exc, LoginRequired):
            result['login_required'] = True
        message = str(exc) if isinstance(exc, CollectionStopped) else (
            f'浏览器读取中断（{type(exc).__name__}）；已保留本轮取得的数据。')
        result['warnings'].append(message)

    def collect(self, cancel_event, progress=None, expected_account=None, *,
                 detail_state=None, checkpoint=None, on_batch=None, force_details=False,
                 excluded_aliases=None):
        self._check_thread()
        excluded_aliases = frozenset(excluded_aliases or ())
        result = self._new_result()
        emit = progress if progress is not None else self.log
        seen_pages, seen_articles = set(), set()
        previous = None
        saved_hooks = (self._detail_state_active, self._checkpoint_active,
                       self._batch_active, self._detail_counts_active)
        try:
            self._check_cancel(cancel_event)
            self._prepare_account(expected_account, result)
            self._detail_state_active = dict(detail_state or {})
            self._detail_state_active.update(self._detail_cache)
            if force_details:
                self._detail_state_active.clear()
            self._checkpoint_active, self._batch_active = checkpoint, on_batch
            self._detail_counts_active = result
            self._navigate_to_publications(cancel_event)
            for number in range(1, self.max_pages + 1):
                self._check_cancel(cancel_event)
                if checkpoint is not None:
                    checkpoint()
                    self._check_cancel(cancel_event)
                emit(f"读取发表记录第 {number} 页（包含历史文章）")
                snapshot = self._snapshot(self._page, "publish", cancel_event, previous)
                actual_page = snapshot.get("metadata", {}).get("current_page")
                if actual_page is not None and actual_page != number:
                    raise CollectionStopped("发表记录当前页码与预期不一致；已停止，避免跳页或漏页。")
                rows = [r for r in snapshot.get("observations", []) if r.get("source") == "publish"]
                signature = _identity(rows)
                if not signature or signature in seen_pages:
                    raise CollectionStopped("发表记录分页重复或缺少文章标识；已停止，避免循环翻页。")
                seen_pages.add(signature)
                batch = []
                for row in rows:
                    if _is_excluded(row, excluded_aliases):
                        continue
                    key = row["article_key"]
                    self._article_pages[key] = number
                    if key not in seen_articles:
                        seen_articles.add(key)
                        result["observations"].append(row)
                        batch.append(row)
                result["warnings"].extend(w for w in snapshot.get("warnings", [])
                                          if not w.startswith("当前只解析这一页"))
                result["pages"] += 1
                if batch and on_batch is not None:
                    on_batch(batch)
                detail_complete = self._collect_details(rows, cancel_event, emit,
                    result["observations"], result["warnings"], excluded_aliases=excluded_aliases)
                if detail_complete is False:
                    result["partial"] = True
                    result["details_complete"] = False
                if not self._has_next():
                    metadata = snapshot.get("metadata", {})
                    current = metadata.get("current_page") or number
                    if (metadata.get("visible_page_count") or 0) > current:
                        raise CollectionStopped("分页显示仍有后续页面，但“下一页”不可用；本轮为部分结果。")
                    break
                if number == self.max_pages:
                    raise CollectionStopped(f"已达到每轮 {self.max_pages} 页上限；仍有后续页面，本轮为部分结果。")
                previous = signature
                self._click_next(cancel_event)
        except Exception as exc:
            self._record_error(result, exc)
        finally:
            (self._detail_state_active, self._checkpoint_active,
             self._batch_active, self._detail_counts_active) = saved_hooks
        if result["partial"]:
            result["details_complete"] = False
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        emit(f"本轮读取 {result['pages']} 页，获得 {len(result['observations'])} 条记录。")
        return result

    def _go_to_publication_page(self, number, cancel_event):
        self._check_cancel(cancel_event)
        if self._current_page() == str(number):
            return
        pagination = self._page.locator(PAGINATION)
        label = pagination.locator('label').filter(has_text=re.compile(rf'^{number}$'))
        if label.count() == 1 and label.is_visible():
            label.click()
        else:
            field = pagination.locator('input.weui-desktop-pagination__input')
            jump = pagination.get_by_text('跳转', exact=True)
            if (field.count() != 1 or jump.count() != 1 or not field.is_visible()
                    or not jump.is_visible() or not jump.is_enabled()):
                raise CollectionStopped('未找到可用的分页跳转控件；无法定位指定文章。')
            field.fill(str(number))
            jump.click()
        self._pause(cancel_event)

    def _open_monitor_page(self, cancel_event):
        if (self._monitor_page is None or self._monitor_page.is_closed()
                or not _is_backend_url(self._monitor_page.url)):
            observed_url = self._page.url
            if not _is_backend_url(observed_url):
                raise CollectionStopped('无法从当前后台页面打开文章采集窗口。')
            if self._monitor_page is not None and not self._monitor_page.is_closed():
                self._monitor_page.close()
            self._monitor_page = self._context.new_page()
            try:
                self._monitor_page.goto(observed_url, wait_until='domcontentloaded', timeout=self.timeout_ms)
            except Exception:
                try:
                    self._monitor_page.close()
                finally:
                    self._monitor_page = None
                raise
        self._page = self._monitor_page
        self._page.bring_to_front()
        self._navigate_to_publications(cancel_event)

    def collect_targets(self, targets, cancel_event, progress=None, expected_account=None, *, on_batch=None):
        """Reload selected counters and open/read/close their details every round."""
        self._check_thread()
        if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 5:
            raise ValueError('文章采集每次请选择 1 至 5 篇文章。')
        wanted = []
        for target in targets:
            item = dict(target) if isinstance(target, dict) else {'article_key': target}
            key = item.get('article_key')
            if not isinstance(key, str) or not key.strip():
                raise ValueError('待采集文章缺少稳定标识，请先采集文章总表。')
            if key not in [r['article_key'] for r in wanted]:
                wanted.append(item)
        result = self._new_result()
        result.update(target_keys=[r['article_key'] for r in wanted], found_keys=[], missing_keys=[])
        emit = progress if progress is not None else self.log
        original_page, original_account = self._page, self._active_account
        saved_hooks = (self._detail_state_active, self._checkpoint_active,
                       self._batch_active, self._detail_counts_active)
        visited, signatures, found = set(), set(), set()
        known_last = None
        previous = None
        try:
            self._check_cancel(cancel_event)
            self._prepare_account(expected_account, result)
            preferred = sorted({self._article_pages[r['article_key']] for r in wanted if r['article_key'] in self._article_pages})
            # Isolate hooks when monitoring is nested inside a full collection.
            self._detail_state_active = {}
            self._checkpoint_active, self._batch_active = None, on_batch
            self._detail_counts_active = result
            self._open_monitor_page(cancel_event)
            order = [1] + [n for n in preferred if 1 < n <= self.max_pages]
            order.extend(n for offset in (1, -1, 2, -2) for page in preferred
                         if 1 < (n := page + offset) <= self.max_pages and n not in order)
            hinted = set(order)
            order.extend(n for n in range(2, self.max_pages + 1) if n not in order)
            for number in order:
                self._check_cancel(cancel_event)
                if number in visited or (known_last is not None and number > known_last):
                    continue
                emit(f'文章采集：读取第 {number} 页，已找到 {len(found)}/{len(wanted)} 篇')
                if number != 1:
                    self._go_to_publication_page(number, cancel_event)
                current = self._snapshot(self._page, 'publish', cancel_event, previous)
                actual = current.get('metadata', {}).get('current_page')
                if actual is None:
                    displayed = self._current_page()
                    actual = int(displayed) if displayed.isdigit() else None
                if actual != number and not (actual is None and number == 1):
                    if number in hinted:
                        continue
                    raise CollectionStopped('文章采集分页未到达预期页码，本页未作为新数据保存。')
                rows = [r for r in current.get('observations', []) if r.get('source') == 'publish']
                signature = _identity(rows)
                if not signature or signature in signatures:
                    raise CollectionStopped('文章采集分页内容重复或未加载完成，本页未作为新数据保存。')
                visited.add(number)
                signatures.add(signature)
                previous = signature
                result['pages'] += 1
                batch = []
                for row in rows:
                    if row.get('status') == 'deleted':
                        continue
                    self._article_pages[row['article_key']] = number
                    for target in wanted:
                        key = target['article_key']
                        if key not in found and _matching_article(row, target):
                            found.add(key)
                            result['found_keys'].append(key)
                            result['observations'].append(row)
                            batch.append(row)
                result['warnings'].extend(w for w in current.get('warnings', []) if not w.startswith('当前只解析这一页'))
                if batch and on_batch is not None:
                    on_batch(batch)
                if batch:
                    detail_complete = self._collect_details(rows, cancel_event, emit,
                        result['observations'], result['warnings'], target_rows=batch,
                        force=True, foreground=True)
                    if detail_complete is False:
                        result['partial'] = True
                        result['details_complete'] = False
                        result['blocked'] = True
                if len(found) == len(wanted):
                    break
                if not self._has_next():
                    visible_max = current.get('metadata', {}).get('visible_page_count') or number
                    if visible_max > number:
                        raise CollectionStopped('分页显示仍有后续页面，但下一页不可用；本轮为部分结果。')
                    known_last = number
            result['missing_keys'] = [r['article_key'] for r in wanted if r['article_key'] not in found]
            if result['missing_keys']:
                result['partial'] = True
                result['blocked'] = True
                result['warnings'].append(f"有 {len(result['missing_keys'])} 篇指定文章未找到；未补写旧值或零值。")
        except Exception as exc:
            self._record_error(result, exc)
        finally:
            self._page, self._active_account = original_page, original_account
            (self._detail_state_active, self._checkpoint_active,
             self._batch_active, self._detail_counts_active) = saved_hooks
            result['missing_keys'] = [r['article_key'] for r in wanted if r['article_key'] not in found]
            for key in result['missing_keys']:
                self._article_pages.pop(key, None)
        result['warnings'] = list(dict.fromkeys(result['warnings']))
        if result['partial'] or result['cancelled']:
            result['details_complete'] = False
        emit(f"文章采集完成：读取 {result['pages']} 页，累计数据 {len(found)}/{len(wanted)} 篇，数据详情 {result['details_read']}/{len(wanted)} 篇。")
        return result
