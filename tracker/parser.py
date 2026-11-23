"""Parse saved/current Official Account HTML without executing page scripts.

Only article identities, displayed aggregates and collection provenance leave this
module. Backend URLs, session tokens, preview keys and unrelated page state never
become observations.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from .schema import now_iso

_CST = timezone(timedelta(hours=8))
_PUBLISH_METRICS = {
    '阅读人数': 'read', '点赞人数': 'like', '分享人数': 'share',
    '推荐人数': 'recommend', '留言条数': 'comment', '划线人数': 'underline',
    '赞赏金额': 'donation', '被转载次数': 'repost', '投票人数': 'vote',
}
_DETAIL_METRICS = {
    'read_uv': 'read', 'like_cnt': 'like', 'share_uv': 'share',
    'zaikan_cnt': 'recommend', 'comment_cnt': 'comment',
    'collection_uv': 'collection', 'follow_after_read_uv': 'follow',
    'avg_article_read_time': 'avg_read_seconds',
    'finished_read_pv_ratio': 'finish_ratio',
}
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
         'link', 'meta', 'param', 'source', 'track', 'wbr'}


class _Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag='', attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.parent = parent

    def has_class(self, name):
        return name in (self.attrs.get('class') or '').split()

    def nodes(self):
        pending = [self]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed([x for x in node.children if isinstance(x, _Node)]))

    def text(self):
        parts = []
        pending = [self]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in {'script', 'style'}:
                pending.extend(reversed(item.children))
        return re.sub(r'\s+', ' ', ''.join(parts)).strip()

    def first(self, class_name):
        return next((n for n in self.nodes() if n.has_class(class_name)), None)


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        child = _Node(tag, attrs, self.current)
        self.current.children.append(child)
        if tag not in _VOID:
            self.current = child

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(_Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _clean_text(value, maximum=500):
    return re.sub(r'\s+', ' ', unescape(str(value or ''))).strip()[:maximum]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) and 0 <= value <= 1e15 else None
    text = str(value).strip().replace(',', '').replace('，', '')
    text = text.removeprefix('¥').removeprefix('￥').strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    value = float(text) if '.' in text else int(text)
    return value if math.isfinite(value) and value <= 1e15 else None


def _url_parts(value):
    try:
        value = unescape(value or '')
        if value.startswith('/') and not value.startswith('//'):
            value = 'https://mp.weixin.qq.com' + value
        parts = urlsplit(value)
        if parts.scheme not in {'http', 'https'} or parts.hostname != 'mp.weixin.qq.com':
            return None
        if parts.username or parts.password or parts.port not in (None, 80, 443):
            return None
        return parts
    except (ValueError, AttributeError):
        return None


def canonical_public_url(value):
    """Return a short public article URL; drop every query/fragment component."""
    parts = _url_parts(value)
    if parts and re.fullmatch(r'/s/[A-Za-z0-9_-]{5,200}/?', parts.path):
        return 'https://mp.weixin.qq.com' + parts.path.rstrip('/')
    return None


def _identity_links(node):
    public_url, msgid, sent_at = None, None, None
    for child in node.nodes():
        value = child.attrs.get('href', '')
        if not public_url:
            public_url = canonical_public_url(value)
        parts = _url_parts(value)
        if not parts:
            continue
        query = parse_qs(parts.query)
        def first(*names):
            return next((query[name][0] for name in names if query.get(name)), '')
        combined = first('msgid')
        if re.fullmatch(r'\d{5,20}_\d{1,3}', combined):
            msgid = msgid or combined
        app_id = first('appmsg_id', 'appmsgid', 'mid')
        if parts.path == '/cgi-bin/appmsgcopyright':
            app_id = app_id or first('id')
        idx = first('item_idx', 'idx')
        if re.fullmatch(r'\d{5,20}', app_id) and re.fullmatch(r'[1-9]\d{0,2}', idx):
            msgid = msgid or app_id + '_' + idx
        seconds = first('send_time')
        if re.fullmatch(r'\d{10}', seconds):
            try:
                candidate = datetime.fromtimestamp(int(seconds), _CST)
                if 2012 <= candidate.year <= 2100:
                    sent_at = sent_at or candidate
            except (OverflowError, OSError, ValueError):
                pass
    return public_url, msgid, sent_at


def _date(value):
    if not isinstance(value, str):
        return None
    match = re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', value)
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def _reference_time(observed_at):
    try:
        return datetime.fromisoformat(observed_at)
    except (TypeError, ValueError):
        return datetime.now().astimezone()


def _group_date(card, reference):
    group = card.parent
    while group is not None:
        if group.has_class('weui-desktop-mass__content') or group.has_class('publish_hover_content'):
            stamp = group.first('weui-desktop-mass__time')
            if stamp:
                raw = stamp.text()
                explicit = _date(raw)
                if explicit:
                    return explicit, False
                match = re.search(r'(\d{1,2})月(\d{1,2})日', raw)
                if match:
                    month, day = map(int, match.groups())
                    for year in (reference.year, reference.year - 1):
                        try:
                            candidate = date(year, month, day)
                        except ValueError:
                            continue
                        if candidate <= reference.date():
                            return candidate.isoformat(), True
                if raw in {'今天', '昨日', '昨天'}:
                    offset = 0 if raw == '今天' else 1
                    return (reference.date() - timedelta(days=offset)).isoformat(), True
            break
        group = group.parent
    return None, False


def _provenance(observed_at, source_file, time_basis, saved_at):
    # Accept a display filename, never persist a full path or backend URL.
    source_file = str(source_file or '')
    if '://' in source_file or '?' in source_file:
        source_file = '在线页面'
    else:
        source_file = source_file.replace('\\', '/').rsplit('/', 1)[-1][:255]
    item = {'observed_at': observed_at, 'time_basis': time_basis, 'source_file': source_file}
    if saved_at:
        item['saved_at'] = saved_at
    if time_basis != 'live':
        item['imported_at'] = now_iso()
    return item


def _publish(root, provenance):
    observations, warnings = [], []
    reference = _reference_time(provenance.get('saved_at') or provenance['observed_at'])
    for card in root.nodes():
        if not card.has_class('weui-desktop-mass-media'):
            continue
        title_node = card.first('weui-desktop-mass-appmsg__title')
        if not title_node:
            warnings.append('一张发表卡片缺少标题，已跳过。')
            continue
        title_span = next((x for x in title_node.children if isinstance(x, _Node) and x.tag == 'span'), None)
        title = _clean_text(title_span.text() if title_span else title_node.text())
        public_url, msgid, send_time = _identity_links(card)
        publish_date, inferred = _group_date(card, reference)
        item_warnings = []
        if send_time and (not publish_date or send_time.strftime('%m-%d') == publish_date[5:]):
            publish_date = send_time.date().isoformat()
            inferred = False
        elif send_time:
            send_time = None
            item_warnings.append('链接中的发表时间与日期标签不一致，未采用该时间。')
        if inferred:
            item_warnings.append('页面未提供年份，发表日期年份由保存/采集日期估计。')
        status = 'deleted' if card.has_class('weui-desktop-mass-media_del') else 'published'
        key = msgid or public_url
        if not key:
            # Keep an orphan archive entry; do not join it to an existing title.
            deletion_date = re.search(r'删除时间\s*(20\d{2}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})', card.text())
            fingerprint = '\0'.join([title, publish_date or '', status,
                                     deletion_date.group(1) if deletion_date else ''])
            key = 'unresolved:' + sha256(fingerprint.encode('utf-8')).hexdigest()[:24]
            item_warnings.append('未找到稳定文章标识，保留为待关联记录；不按标题自动合并。')
        metrics = {metric: None for metric in _PUBLISH_METRICS.values()}
        if status != 'deleted':
            data_list = card.first('weui-desktop-mass-media__data-list')
            if data_list:
                for wrapper in data_list.nodes():
                    if not wrapper.has_class('weui-desktop-tooltip__wrp'):
                        continue
                    label_node = wrapper.first('weui-desktop-tooltip')
                    number_node = wrapper.first('weui-desktop-mass-media__data__inner')
                    label = label_node.text() if label_node else ''
                    metric = _PUBLISH_METRICS.get(label)
                    if metric and number_node:
                        raw = number_node.text()
                        metrics[metric] = _number(raw)
                        if metrics[metric] is None and raw not in {'', '-', '--', '—'}:
                            item_warnings.append(f'{label}不是完整数字，保留为空。')
            else:
                item_warnings.append('发表卡片缺少数据区域，指标保留为空。')
        item = {**provenance, 'article_key': key, 'title': title, 'source': 'publish',
                'metrics': metrics, 'status': status, 'publish_date': publish_date,
                'period_label': '发表记录累计值（后台实时展示）', 'warnings': item_warnings}
        if send_time:
            item['publish_time'] = send_time.isoformat(timespec='seconds')
        if public_url:
            item['public_url'] = public_url
        if msgid:
            item['msgid'] = msgid
        observations.append(item)
    text = root.text()
    count_match = re.search(r'共发表了\s*(\d+)\s*次', text)
    pages = []
    current_page = None
    for node in root.nodes():
        if node.has_class('weui-desktop-pagination__num') and node.text().isdigit():
            pages.append(int(node.text()))
        if node.has_class('weui-desktop-pagination__num_current') and node.text().isdigit():
            current_page = int(node.text())
    metadata = {'publication_count': int(count_match.group(1)) if count_match else None,
                'current_page': current_page, 'visible_page_count': max(pages) if pages else None,
                'article_count': len(observations)}
    if metadata['visible_page_count'] and metadata['visible_page_count'] > 1:
        warnings.append(f"当前只解析这一页；发表记录显示至少 {metadata['visible_page_count']} 页，需要逐页采集。")
    return observations, warnings, metadata


def _embedded_json(html, key):
    decoder = json.JSONDecoder(parse_constant=lambda value: None)
    pattern = r'(?<![\w$])(?:[\"\']?' + re.escape(key) + r'[\"\']?)\s*[:=]\s*'
    for match in re.finditer(pattern, html):
        try:
            value, _ = decoder.raw_decode(html, match.end())
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _detail(root, html, provenance):
    data = _embedded_json(html, 'articleData')
    if not data:
        return [], ['单篇页面缺少可读取的 articleData 数据，请重新保存完整页面。']
    title, msgid = _clean_text(data.get('title')), str(data.get('msgid', ''))
    if not title or not re.fullmatch(r'\d{5,20}_[1-9]\d{0,2}', msgid):
        return [], ['单篇页面缺少有效的文章标识，未按标题猜测对应关系。']
    publish_date = _date(data.get('publish_date'))
    full_text = root.text()
    updated_match = re.search(r'页面数据更新至\s*(20\d{2}[/年-]\d{1,2}[/月-]\d{1,2})', full_text)
    updated = _date(updated_match.group(1)) if updated_match else None
    thirty_day = bool(re.search(r'仅统计发表后\s*30\s*天', full_text))
    period_end = updated
    if thirty_day and publish_date:
        cutoff = (date.fromisoformat(publish_date) + timedelta(days=29)).isoformat()
        period_end = min(updated, cutoff) if updated else cutoff
    metrics_data = data.get('article_data_new')
    metrics_data = metrics_data if isinstance(metrics_data, dict) else {}
    metrics = {dest: _number(metrics_data.get(src)) for src, dest in _DETAIL_METRICS.items()}
    cents = _number(metrics_data.get('praise_money'))
    metrics['donation'] = round(cents / 100, 2) if cents is not None else None
    if metrics.get('finish_ratio') is not None and metrics['finish_ratio'] > 1:
        metrics['finish_ratio'] = None
    common = {**provenance, 'article_key': msgid, 'msgid': msgid, 'title': title,
              'publish_date': publish_date, 'status': 'published',
              'source_updated_through': updated}
    snapshot = {**common, 'source': 'detail', 'metrics': metrics,
                'period_start': publish_date, 'period_end': period_end,
                'period_label': '发表后30天内（数据中心）' if thirty_day else '数据中心页面统计窗口',
                'warnings': []}
    if not updated:
        snapshot['warnings'].append('未找到页面数据更新截止日期。')
    return [snapshot], []


def parse_html(html, observed_at=None, source_file='', time_basis='live', saved_at=None):
    """Return sanitized observations from a single page of UTF-8 HTML.

    ``observed_at`` is when this snapshot was observed, not its reporting period.
    Keep the reporting period separate from the observation timestamp.
    """
    if not isinstance(html, str):
        raise TypeError('HTML 内容必须是字符串')
    if len(html) > 30_000_000:
        raise ValueError('页面过大，请单独保存公众号页面（最大 30 MB）')
    observed_at = observed_at or now_iso()
    tree = _Tree()
    tree.feed(html)
    tree.close()
    root = tree.root
    provenance = _provenance(observed_at, source_file, time_basis, saved_at)
    cards = any(node.has_class('weui-desktop-mass-media') for node in root.nodes())
    if cards:
        observations, warnings, metadata = _publish(root, provenance)
        page_type = 'publish'
        if metadata['publication_count'] is None and '昨日阅读' in root.text() and '总用户数' in root.text():
            page_type = 'home'
            warnings.append('首页下方仅展示最近部分发表记录；这些记录已导入，完整列表仍需进入发表记录逐页采集。')
        return {'observations': observations, 'warnings': warnings, 'page_type': page_type,
                'metadata': metadata}
    if _embedded_json(html, 'articleData') is not None or '本页面仅统计发表后' in root.text():
        observations, warnings = _detail(root, html, provenance)
        return {'observations': observations, 'warnings': warnings, 'page_type': 'detail'}
    text = root.text()
    if '内容分析' in text and ('内容标题' in text or '流量来源' in text):
        return {'observations': [], 'page_type': 'overview', 'warnings': [
            '内容分析汇总页受筛选范围限制；请导入发表记录和单篇数据页，以文章标识关联，不按标题猜测。']}
    if '昨日阅读' in text and '总用户数' in text:
        return {'observations': [], 'page_type': 'home', 'warnings': [
            '这是公众号首页概览，没有逐篇数据；请导入发表记录或单篇数据中心页面。']}
    return {'observations': [], 'page_type': 'unknown', 'warnings': [
        '未识别到发表记录或单篇数据中心；页面可能未加载完整，或后台结构已经变化。']}


def parse_file(path, observed_at=None):
    """Import a saved page, labelling mtime as an estimate rather than live data."""
    path = Path(path)
    if path.stat().st_size > 30_000_000:
        raise ValueError('保存的页面过大（最大 30 MB）')
    raw = path.read_bytes()
    try:
        html = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            html = raw.decode('gb18030')
        except UnicodeDecodeError as exc:
            raise ValueError('无法识别网页编码，请以完整 HTML 重新保存。') from exc
    saved_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec='seconds')
    result = parse_html(html, observed_at=observed_at or saved_at, source_file=path.name,
                        time_basis='manual' if observed_at else 'file_mtime_estimate', saved_at=saved_at)
    if not observed_at and result['observations']:
        result['warnings'].insert(0, '导入网页：采集时间暂以文件修改时间估计；复制或修改文件可能改变该时间。')
    return result
