"""Normalize rendered creator-list cards; never read private application state.

DOM adapters supply visible labels and observed IDs only. Missing/hidden values
remain None. Titles alone are never treated as an article identity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import re
from urllib.parse import urlsplit, urlunsplit

from .schema import now_iso

PLATFORM_HOSTS = {'xiaohongshu': 'creator.xiaohongshu.com',
                  'douyin': 'creator.douyin.com'}
LABELS = {'观看': 'views', '播放': 'plays', '点赞': 'likes', '评论': 'comments',
          '分享': 'shares', '收藏': 'favorites', '吸粉量': 'followers_gained',
          '详情页进入率': 'detail_entry_ratio', '完播率': 'completion_ratio',
          '划走率': 'swipe_away_ratio', '文案展开率': 'caption_expand_ratio',
          '平均浏览图片数': 'avg_images_viewed'}
PLATFORM_METRIC_KEYS = {
    'xiaohongshu': {'views', 'likes', 'comments', 'shares', 'favorites'},
    'douyin': set(LABELS.values()) - {'views'},
}
RATIOS = {'detail_entry_ratio', 'completion_ratio', 'swipe_away_ratio',
          'caption_expand_ratio'}
_CST = timezone(timedelta(hours=8))


def is_platform_url(url, platform):
    try:
        p = urlsplit(url)
        return (platform in PLATFORM_HOSTS and p.scheme == 'https'
                and p.hostname == PLATFORM_HOSTS[platform]
                and p.port in (None, 443) and not p.username and not p.password)
    except (TypeError, ValueError):
        return False


def _clean(value):
    return re.sub(r'\s+', ' ', value).strip() if isinstance(value, str) else ''


def parse_display_number(value, *, ratio=False):
    """Return (normalized value, rounded display) without coercing '-' to zero."""
    if value is None or isinstance(value, bool):
        return None, False
    raw = str(value).strip().replace(',', '').replace('，', '')
    if raw in ('', '-', '—', '–', '--', '暂无', '暂无数据'):
        return None, False
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([万亿wW]?)\s*(%?)', raw)
    if not match:
        return None, False
    number, unit, percent = match.groups()
    if ratio and not percent:
        return None, False  # A visible ratio must state its unit.
    if percent and not ratio:
        return None, False
    amount = Decimal(number) * {'': 1, '万': 10000, '亿': 100000000,
                              'w': 10000, 'W': 10000}[unit]
    if percent:
        amount /= 100
    if not amount.is_finite() or amount > (1 if ratio else 10**15):
        return None, False
    return int(amount) if amount == amount.to_integral_value() else float(amount), bool(unit)


def _published(raw):
    value = _clean(raw)
    match = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?[ T]*(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
    if not match:
        return None
    try:
        parts = [int(part or 0) for part in match.groups()]
        return datetime(*parts, tzinfo=_CST)
    except ValueError:
        return None


def _public_identity(href, platform):
    """Only persist a public URL actually present in the DOM; strip all queries."""
    try:
        p = urlsplit(href)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return '', ''
        if platform == 'xiaohongshu' and p.hostname in ('www.xiaohongshu.com', 'xiaohongshu.com'):
            m = re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{16,64})', p.path)
            if m:
                return m[1].lower(), urlunsplit(('https', 'www.xiaohongshu.com', '/explore/' + m[1].lower(), '', ''))
        if platform == 'douyin' and p.hostname in ('www.douyin.com', 'douyin.com'):
            m = re.fullmatch(r'/(?:video|note)/(\d{1,30})', p.path)
            if m:
                return m[1], urlunsplit(('https', 'www.douyin.com', p.path, '', ''))
    except (TypeError, ValueError):
        pass
    return '', ''


def parse_card(card, platform, observed_at):
    if platform not in PLATFORM_HOSTS:
        raise ValueError('不支持的内容平台。')
    warnings = []
    title = _clean(card.get('title'))
    published = _published(card.get('publish_time'))
    if not title or len(title) > 32767 or not published:
        raise ValueError('内容卡片缺少完整标题或发布时间，未保存。')
    stable_id = _clean(card.get('stable_id'))
    pattern = r'[a-fA-F0-9]{24}' if platform == 'xiaohongshu' else r'\d{1,30}'
    if stable_id and not re.fullmatch(pattern, stable_id):
        raise ValueError('作品标识格式不匹配，未保存。')
    link_id, public_url = _public_identity(card.get('public_url', ''), platform)
    if stable_id and link_id and stable_id.lower() != link_id.lower():
        raise ValueError('作品标识与公开链接不一致，未保存。')
    stable_id = stable_id or link_id
    if stable_id:
        key = platform + ':' + stable_id.lower()
        identity_basis = 'platform_id'
    else:
        if card.get('title_truncated') or re.search(r'(?:\.\.\.|…)\s*$', title):
            raise ValueError('内容标题被截断且没有稳定作品标识，未保存。')
        signature = '\n'.join((platform, published.isoformat(), title))
        key = 'article:' + sha256(signature.encode('utf-8')).hexdigest()
        identity_basis = 'publish_time_title_hash'
        warnings.append('列表未提供稳定作品 ID，暂用完整标题与发布时间识别；修改标题可能生成新作品记录。')
    source_metrics = card.get('metrics')
    if not isinstance(source_metrics, dict) or not source_metrics:
        raise ValueError('内容卡片没有可识别的指标字段，未保存。')
    metrics = {}
    for label, value in source_metrics.items():
        key_name = LABELS.get(label, label)
        if key_name not in PLATFORM_METRIC_KEYS[platform]:
            continue
        number, rounded = parse_display_number(value, ratio=key_name in RATIOS)
        metrics[key_name] = number
        if rounded:
            warnings.append(f'{label} 按页面缩写值换算，精度以平台显示为准。')
        if number is None and str(value).strip() not in ('None', '', '-', '—', '–', '--', '暂无', '暂无数据'):
            warnings.append(f'{label} 显示值无法解析，保留为空。')
    if not metrics:
        raise ValueError('内容卡片没有平台对应的指标字段，未保存。')
    row = {'article_key': key, 'title': title, 'platform': platform,
           'source': 'content_list', 'observed_at': observed_at,
           'publish_date': published.date().isoformat(),
           'publish_time': published.isoformat(timespec='seconds'),
           'time_basis': 'live', 'identity_basis': identity_basis,
           'period_label': '内容管理列表累计值（平台当前显示）',
           'status': 'published', 'metrics': metrics}
    if card.get('visibility') == 'private':
        warnings.append('作品当前为私密；页面未显示的指标保留为空。')
    if card.get('visibility') in ('private', 'public'):
        row['visibility'] = card['visibility']
    if card.get('content_type') in ('video', 'images'):
        row['content_type'] = card['content_type']
    if public_url:
        row['public_url'] = public_url
    if warnings:
        row['warnings'] = warnings
    return row


def parse_snapshot(snapshot, platform, *, observed_at=None):
    if platform not in PLATFORM_HOSTS:
        raise ValueError('不支持的内容平台。')
    if not isinstance(snapshot, dict):
        raise ValueError('内容列表读取结果无效。')
    stamp = observed_at or now_iso()
    if datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError('采集时间必须带系统时区。')
    rows, warnings, seen, invalid = [], [], set(), 0
    for card in snapshot.get('cards') or []:
        try:
            row = parse_card(card, platform, stamp)
        except (TypeError, ValueError, AttributeError) as exc:
            invalid += 1
            warnings.append(str(exc) if isinstance(exc, ValueError) else '内容卡片结构无效，未保存。')
            continue
        key = row['article_key']
        if key in seen:
            invalid += 1
            warnings.append('同一可见列表出现重复作品标识，无法确认列表完整性。')
            continue
        seen.add(key)
        rows.append(row)
        warnings.extend(row.get('warnings', []))
    total = snapshot.get('total')
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 1000000:
        total = None
    account_names = snapshot.get('account_names')
    names = {_clean(name) for name in account_names if _clean(name)} if isinstance(account_names, list) else set()
    return {'observations': rows, 'warnings': list(dict.fromkeys(warnings)),
            'page_type': 'content_list', 'detected_account': names.pop() if len(names) == 1 else None,
            'metadata': {'total': total, 'invalid_cards': invalid,
                         'raw_count': len(snapshot.get('cards') or []),
                         'at_bottom': snapshot.get('at_bottom') is True,
                         'end_reached': snapshot.get('end_reached') is True,
                         'empty_confirmed': snapshot.get('empty_confirmed') is True,
                         'login_required': snapshot.get('login_required') is True,
                         'loading': snapshot.get('loading') is True,
                         'account_ambiguous': len(names) > 1,
                         'filters_default': snapshot.get('filters_default') is True}}
