"""Stable publication numbering and raw-value table sorting."""
from datetime import date, datetime, time, timedelta, timezone
import math
import re
from .schema import SOURCES, platform_metrics

def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def _published(row):
    value = _timestamp(row.get('publish_time'))
    return value if value is not None else _timestamp(row.get('publish_date'))

def _identity(row):
    value = str(row.get('msgid') or row.get('article_key') or '')
    match = re.fullmatch(r'(?:msgid:)?(\d+)_(\d+)', value)
    if match:
        return (0, int(match[1]), int(match[2]), str(row.get('article_id', '')))
    return (1, value, str(row.get('article_id', '')))

def article_number_map(rows):
    def key(row):
        value = _published(row)
        return (value is None, value if value is not None else 0, _identity(row))
    return {str(row['article_id']): number
            for number, row in enumerate(sorted(rows, key=key), 1)}

def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None

def sort_article_rows(rows, key, descending=False, *, platform='wechat'):
    """Missing values stay last; callers number the whole account before filtering."""
    metrics = platform_metrics(platform)
    if key not in {'number', 'title', 'published', 'observed', 'source', 'state'} | set(metrics):
        raise ValueError('未知文章排序列：' + str(key))
    rows = list(rows)
    numbers = article_number_map(rows) if key == 'number' else {}
    def value(row):
        if key in metrics:
            return _number(row.get('metrics', {}).get(key))
        if key == 'number':
            return _number(row.get('number', numbers.get(str(row['article_id']))))
        if key == 'published':
            return _published(row)
        if key == 'observed':
            return _timestamp(row.get('observed_at'))
        if key == 'source':
            raw = SOURCES.get(row.get('source'), row.get('source'))
        elif key == 'state':
            raw = row.get('state')
            if raw is None and row.get('status'):
                raw = '已删除' if row['status'] == 'deleted' else '已发表'
                if row.get('imported_at'):
                    raw += ' · 导入'
        else:
            raw = row.get(key)
        return str(raw).casefold() if raw is not None and raw != '' else None
    decorated = [(value(row), row) for row in sorted(rows, key=_identity)]
    present = [item for item in decorated if item[0] is not None]
    missing = [item[1] for item in decorated if item[0] is None]
    present.sort(key=lambda item: item[0], reverse=bool(descending))
    return [item[1] for item in present] + missing
