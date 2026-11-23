"""Compact WeChat backup tables with lifetime samples and one detail snapshot."""
from __future__ import annotations

from datetime import date, datetime
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from .backup_columns import ExpansionBudget
from .schema import MAX_TITLE_LENGTH


WECHAT_HEADERS = {
    '文章信息': ['文章标题', '发表时间', '文章状态', '时间依据', '公开文章链接', '消息标识'],
    '发表记录（累计）': ['采集或快照时间', '阅读人数', '点赞人数', '分享人数', '推荐人数',
                   '留言条数', '划线人数', '赞赏金额（元）', '被转载次数', '投票人数', '收藏人数'],
    '发表后30天内（数据中心）': ['采集或快照时间', '页面数据更新至', '统计开始', '统计结束',
                       '阅读后关注人数', '平均阅读时长（秒）', '读完率'],
}
CUMULATIVE_METRICS = ('read', 'like', 'share', 'recommend', 'comment', 'underline',
                      'donation', 'repost', 'vote', 'collection')
DETAIL_METRICS = ('follow', 'avg_read_seconds', 'finish_ratio')
TIME_BASIS = {
    'live': '系统采集时间', 'file_mtime': '文件修改时间（估计）',
    'file_mtime_estimate': '文件修改时间（估计）', 'saved_file_mtime': '文件修改时间（估计）',
    'manual': '手动指定时间', 'user': '手动指定时间',
}
DECODE_TIME_BASIS = {'系统采集时间': 'live', '文件修改时间（估计）': 'file_mtime_estimate',
                     '手动指定时间': 'manual'}
MSGID = re.compile(r'(?:msgid:)?(\d{1,30}_\d{1,5})\Z')
FALLBACK_ID = re.compile(r'(?:article:|url:|unknown:|unresolved:|deleted:|overview:|title:)?[a-f0-9]{16,64}\Z')


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('采集时间必须是带时区的 ISO 时间。')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('采集时间格式错误。') from exc
    if result.tzinfo is None:
        raise ValueError('采集时间必须带时区。')
    return result


def _text(value, maximum, field, *, required=False):
    if value is None and not required:
        return
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (required and not value):
        raise ValueError(field + '格式错误。')


def _public_url(value):
    _text(value, 2000, '公开文章链接')
    if not value:
        return
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('https', 'http') or parts.hostname != 'mp.weixin.qq.com'
                or parts.username or parts.password or parts.port):
            raise ValueError
        if re.fullmatch(r'/s/[A-Za-z0-9_-]+', parts.path):
            if parts.query or parts.fragment:
                raise ValueError
            return
        query = parse_qs(parts.query)
        if (parts.path != '/s' or not set(query) <= {'__biz', 'mid', 'idx', 'sn'}
                or not all(query.get(key) for key in ('__biz', 'mid', 'idx'))):
            raise ValueError
    except ValueError as exc:
        raise ValueError('备份中的文章链接必须是公开文章链接。') from exc


def _identity(value):
    _text(value, 1000, '文章标识', required=True)
    match = MSGID.fullmatch(value)
    if match:
        return match[1]
    if value.startswith(('http://', 'https://')):
        _public_url(value)
    elif not FALLBACK_ID.fullmatch(value):
        raise ValueError('文章标识格式错误。')
    return value


def _strong_id(row):
    for value in (row.get('msgid'), row.get('article_key')):
        match = MSGID.fullmatch(value) if isinstance(value, str) else None
        if match:
            return match[1]
    return None


def _row_aliases(row):
    return {row[name] for name in ('article_key', 'public_url') if row.get(name)}


def pack_wechat_observations(rows) -> list:
    """Keep every publish sample, the latest detail, and latest article metadata."""
    rows = [row for row in rows if row.get('source') in ('publish', 'detail')]
    alias_ids = {}
    for row in rows:
        strong = _strong_id(row)
        if strong:
            for alias in _row_aliases(row):
                alias_ids.setdefault(alias, set()).add(strong)
    groups = {}
    for position, row in enumerate(rows):
        strong = _strong_id(row)
        if strong is None:
            candidates = set().union(*(alias_ids.get(alias, set()) for alias in _row_aliases(row)))
            strong = next(iter(candidates)) if len(candidates) == 1 else None
        key = strong or _identity(row.get('article_key'))
        groups.setdefault(key, []).append((position, row))

    articles = []
    for key, records in groups.items():
        def rank(item):
            position, row = item
            return (_stamp(row['observed_at']), row['source'] == 'publish', position)
        latest = max(records, key=rank)[1]
        publications = sorted((item for item in records if item[1]['source'] == 'publish'),
                              key=lambda item: (_stamp(item[1]['observed_at']), item[0]))
        details = [item for item in records if item[1]['source'] == 'detail']
        precise = [item for item in records if item[1].get('publish_time')]
        published = max(precise, key=rank)[1]['publish_time'] if precise else latest.get('publish_date') or None
        linked = [item for item in records if item[1].get('public_url')]
        public_url = max(linked, key=rank)[1]['public_url'] if linked else None
        basis = latest.get('time_basis')
        meta = [latest['title'], published, '已删除' if latest.get('status') == 'deleted' else '已发表',
                TIME_BASIS.get(basis, basis), public_url, key]
        cumulative = [[row['observed_at'], *[row['metrics'].get(metric) for metric in CUMULATIVE_METRICS]]
                      for _, row in publications]
        snapshot = None
        if details:
            detail = max(details, key=rank)[1]
            snapshot = [detail.get(field) or None for field in
                        ('observed_at', 'source_updated_through', 'period_start', 'period_end')]
            snapshot.extend(detail['metrics'].get(metric) for metric in DETAIL_METRICS)
        articles.append([meta, cumulative, snapshot])
    return articles


def _number(value, *, ratio=False):
    if value is None:
        return
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= (1 if ratio else 1e15)):
        raise ValueError('备份指标必须是有限的非负数或空值。')


def _date_or_stamp(value, *, date_only=False):
    if value is None:
        return
    if isinstance(value, str) and len(value) == 10:
        try:
            date.fromisoformat(value)
            return
        except ValueError:
            pass
    if date_only:
        raise ValueError('统计日期格式错误。')
    _stamp(value)


def unpack_wechat_observations(articles, *, limit, budget=None) -> list[dict]:
    if (not isinstance(articles, list) or type(limit) is not int or limit < 0
            or len(articles) > limit):
        raise ValueError('微信备份文章数据格式错误或记录过多。')
    if budget is None:
        budget = ExpansionBudget()
    decoded = []
    identities = set()
    total = 0
    for article in articles:
        if not isinstance(article, list) or len(article) != 3:
            raise ValueError('微信备份文章应有信息、累计和详情三个区块。')
        meta, cumulative, snapshot = article
        if not isinstance(meta, list) or len(meta) != 6 or not isinstance(cumulative, list):
            raise ValueError('微信备份文章信息或累计记录的列数错误。')
        title, published, state, basis, public_url, key = meta
        _text(title, MAX_TITLE_LENGTH, '文章标题', required=True)
        _date_or_stamp(published)
        if state not in ('已发表', '已删除'):
            raise ValueError('文章状态格式错误。')
        _text(basis, 100, '时间依据')
        _public_url(public_url)
        key = _identity(key)
        if key in identities:
            raise ValueError('微信备份文章标识重复。')
        identities.add(key)
        if snapshot is not None and (not isinstance(snapshot, list) or len(snapshot) != 7):
            raise ValueError('微信备份详情统计的列数错误。')
        count = len(cumulative) + (snapshot is not None)
        total += count
        if not count or total > limit:
            raise ValueError('微信备份没有观测记录或记录过多。')

        common = {'article_key': key, 'title': title,
                  'status': 'deleted' if state == '已删除' else 'published'}
        if MSGID.fullmatch(key):
            common['msgid'] = key
        if public_url:
            common['public_url'] = public_url
        if published:
            common['publish_date'] = published[:10]
            if len(published) != 10:
                common['publish_time'] = published
        base_size = len(json.dumps(common, ensure_ascii=False).encode('utf-8'))
        budget.reserve((base_size + 512) * count)
        rows = []
        previous = None
        for values in cumulative:
            if not isinstance(values, list) or len(values) != 11:
                raise ValueError('发表累计记录必须有固定的11列。')
            observed = _stamp(values[0])
            if previous is not None and observed < previous:
                raise ValueError('发表累计记录未按采集时间排序。')
            previous = observed
            for value in values[1:]:
                _number(value)
            rows.append({**common, 'source': 'publish', 'observed_at': values[0],
                         'period_label': '发表记录累计值（后台实时展示）',
                         'metrics': dict(zip(CUMULATIVE_METRICS, values[1:]))})
        if snapshot is not None:
            _stamp(snapshot[0])
            _date_or_stamp(snapshot[1], date_only=True)
            for value in snapshot[2:4]:
                _date_or_stamp(value)
            for i, value in enumerate(snapshot[4:]):
                _number(value, ratio=i == 2)
            detail = {**common, 'source': 'detail', 'observed_at': snapshot[0],
                      'metrics': dict(zip(DETAIL_METRICS, snapshot[4:]))}
            for name, value in zip(('source_updated_through', 'period_start', 'period_end'), snapshot[1:4]):
                if value is not None:
                    detail[name] = value
            rows.append(detail)
        if basis is not None:
            representative = max(rows, key=lambda row: (_stamp(row['observed_at']), row['source'] == 'publish'))
            representative['time_basis'] = DECODE_TIME_BASIS.get(basis, basis)
        decoded.extend(rows)
    return decoded
