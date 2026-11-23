"""Group backup observations by stable article key, source, and fields.

Each series stores fixed values once and puts variable values in positional
rows. The sequence consists of [series index, count] pairs, with series numbered
in article order, so the original account's observation order is recoverable.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .schema import MAX_TITLE_LENGTH, PLATFORMS, SOURCES, platform_metrics


MAX_EXPANDED_BYTES = 512 * 1024 * 1024
METRIC_PREFIX = 'metrics.'
METRIC_COLUMNS = frozenset(
    METRIC_PREFIX + name for platform in PLATFORMS for name in platform_metrics(platform)
)
TEXT_COLUMNS = frozenset({
    'title', 'observed_at', 'platform', 'msgid', 'public_url', 'publish_date',
    'publish_time', 'status', 'period_start', 'period_end', 'period_label',
    'source_updated_through', 'time_basis', 'saved_at', 'imported_at', 'source_file',
    'identity_basis', 'content_type', 'visibility',
})
OBSERVATION_COLUMNS = TEXT_COLUMNS | {'warnings'}
ALLOWED_COLUMNS = OBSERVATION_COLUMNS | METRIC_COLUMNS
ARTICLE_COLUMNS = TEXT_COLUMNS - {'observed_at'}
REQUIRED_COLUMNS = {'title', 'observed_at'}
GROUP_FIELDS = {'article_key', 'source', 'metrics'}


class ExpansionBudget:
    """Share a limit on reconstructed UTF-8 data across imported accounts."""

    def __init__(self, max_bytes=MAX_EXPANDED_BYTES):
        self.remaining = max_bytes

    def reserve(self, size):
        if size > self.remaining:
            raise ValueError('备份恢复为完整观测后超过大小限制。')
        self.remaining -= size


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _same_value(a, b):
    if type(a) is not type(b) or a != b:
        return False
    if isinstance(a, float) and a == 0.0:
        return math.copysign(1.0, a) == math.copysign(1.0, b)
    return True


def _check_series_identity(article_key, source):
    if not isinstance(article_key, str) or not 0 < len(article_key) <= 1000:
        raise ValueError('备份文章标识格式错误。')
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError('备份观测来源格式错误。')


def pack_observations(rows: list[dict]) -> dict:
    groups = {}
    order = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('备份观测格式错误。')
        article_key, source = row.get('article_key'), row.get('source')
        _check_series_identity(article_key, source)
        metrics = row.get('metrics')
        if not isinstance(metrics, dict):
            raise ValueError('备份观测指标格式错误。')
        values = {name: value for name, value in row.items() if name not in GROUP_FIELDS}
        if not REQUIRED_COLUMNS <= values.keys() <= OBSERVATION_COLUMNS:
            raise ValueError('备份观测列名称错误。')
        for name, value in metrics.items():
            if not isinstance(name, str):
                raise ValueError('备份指标名称格式错误。')
            values[METRIC_PREFIX + name] = value
        shape = frozenset(values)
        if not shape <= ALLOWED_COLUMNS:
            raise ValueError('备份观测列名称错误。')
        key = (article_key, source, shape)
        groups.setdefault(article_key, {}).setdefault((source, shape), []).append(values)
        if order and order[-1][0] == key:
            order[-1][1] += 1
        else:
            order.append([key, 1])

    articles, indexes = [], {}
    for article_key, article in groups.items():
        series = []
        for (source, shape), records in article.items():
            indexes[(article_key, source, shape)] = len(indexes)
            common, columns = {}, []
            for column in sorted(shape):
                first = records[0][column]
                if all(_same_value(first, record[column]) for record in records[1:]):
                    common[column] = first
                else:
                    columns.append(column)
            series.append({'source': source, 'common': common, 'columns': columns,
                           'rows': [[record[column] for column in columns] for record in records]})
        article_common = {}
        for name, value in series[0]['common'].items():
            if name in ARTICLE_COLUMNS and all(name in item['common'] and _same_value(value, item['common'][name])
                                               for item in series[1:]):
                article_common[name] = value
        for item in series:
            for name in article_common:
                del item['common'][name]
        articles.append({'article_key': article_key, 'common': article_common, 'series': series})
    return {'articles': articles, 'sequence': [[indexes[key], count] for key, count in order]}


def _is_object(value, keys):
    return isinstance(value, dict) and value.keys() == keys


def _check_value(column, value):
    if column in METRIC_COLUMNS:
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1e15):
            raise ValueError('备份观测指标必须是有限的非负数或空值。')
    elif column == 'warnings':
        if value is not None and (not isinstance(value, list) or len(value) > 30
                                  or any(not isinstance(item, str) or len(item) > 1000 for item in value)):
            raise ValueError('备份观测备注格式错误。')
    elif value is None:
        if column in REQUIRED_COLUMNS:
            raise ValueError('备份观测缺少文章标题或观测时间。')
    elif not isinstance(value, str) or len(value) > MAX_TITLE_LENGTH:
        raise ValueError('备份观测文本列格式错误。')


def _set_value(row, column, value):
    if column.startswith(METRIC_PREFIX):
        row['metrics'][column[len(METRIC_PREFIX):]] = value
    else:
        row[column] = value.copy() if isinstance(value, list) else value


@dataclass
class _Series:
    common: dict
    columns: list
    rows: list

    def observation(self, index):
        record = {**self.common, 'metrics': self.common['metrics'].copy()}
        if isinstance(record.get('warnings'), list):
            record['warnings'] = record['warnings'].copy()
        for column, value in zip(self.columns, self.rows[index]):
            _set_value(record, column, value)
        return record


def _read_series(article_key, article_common, raw, seen_shapes, limit, budget):
    if not _is_object(raw, {'source', 'common', 'columns', 'rows'}):
        raise ValueError('备份观测分组格式错误。')
    source, common, columns, rows = (raw[name] for name in ('source', 'common', 'columns', 'rows'))
    _check_series_identity(article_key, source)
    if not isinstance(common, dict) or not isinstance(columns, list):
        raise ValueError('备份观测列定义格式错误。')
    if len(article_common) + len(common) + len(columns) > len(ALLOWED_COLUMNS):
        raise ValueError('备份观测列过多。')
    names = [*article_common, *common, *columns]
    if (any(not isinstance(name, str) or name not in ALLOWED_COLUMNS for name in names)
            or len(set(names)) != len(names) or not REQUIRED_COLUMNS <= set(names)):
        raise ValueError('备份观测存在未知或重复列，或缺少必需列。')
    shape = (source, frozenset(names))
    if shape in seen_shapes:
        raise ValueError('备份观测存在重复分组。')
    seen_shapes.add(shape)
    if not isinstance(rows, list) or not rows:
        raise ValueError('备份观测分组的行列表格式错误。')
    if len(rows) > limit:
        raise ValueError('备份记录过多。')

    base = {'article_key': article_key, 'source': source, 'metrics': {}}
    for name, value in {**article_common, **common}.items():
        _check_value(name, value)
        _set_value(base, name, value)

    # Charge shared fields once per expanded row. Count one colon and comma
    # per variable column and one separator per observation as an upper bound.
    column_overhead = sum(_size(name.removeprefix(METRIC_PREFIX)) + 2 for name in columns)
    budget.reserve((_size(base) + column_overhead + 1) * len(rows))
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError('备份观测行的值数与列数不一致。')
        for name, value in zip(columns, row):
            _check_value(name, value)
            budget.reserve(_size(value))
    return _Series(base, columns, rows)


def unpack_observations(packed: dict, *, limit: int, budget: ExpansionBudget | None = None) -> list[dict]:
    if not _is_object(packed, {'articles', 'sequence'}):
        raise ValueError('备份观测格式错误。')
    articles, sequence = packed['articles'], packed['sequence']
    if not isinstance(articles, list) or not isinstance(sequence, list):
        raise ValueError('备份观测分组或顺序格式错误。')
    if type(limit) is not int or limit < 0 or len(articles) > limit or len(sequence) > limit:
        raise ValueError('备份记录过多。')
    if budget is None:
        budget = ExpansionBudget()

    series = []
    seen_articles = set()
    for article in articles:
        if not _is_object(article, {'article_key', 'common', 'series'}):
            raise ValueError('备份文章分组格式错误。')
        article_key, common, article_series = article['article_key'], article['common'], article['series']
        if (not isinstance(article_key, str) or article_key in seen_articles
                or not isinstance(article_series, list) or not article_series):
            raise ValueError('备份文章分组存在重复或格式错误。')
        if not isinstance(common, dict) or not common.keys() <= ARTICLE_COLUMNS:
            raise ValueError('备份文章固定列定义格式错误。')
        seen_articles.add(article_key)
        seen_shapes = set()
        for raw in article_series:
            decoded = _read_series(article_key, common, raw, seen_shapes, limit, budget)
            series.append(decoded)
            limit -= len(decoded.rows)

    # Validate the whole order before allocating reconstructed observations.
    consumed = [0] * len(series)
    previous = None
    for run in sequence:
        if not isinstance(run, list) or len(run) != 2:
            raise ValueError('备份观测顺序格式错误。')
        index, count = run
        if (type(index) is not int or not 0 <= index < len(series) or index == previous
                or type(count) is not int or not 0 < count <= len(series[index].rows) - consumed[index]):
            raise ValueError('备份观测顺序重复、越界或格式错误。')
        consumed[index] += count
        previous = index
    if any(count != len(item.rows) for count, item in zip(consumed, series)):
        raise ValueError('备份观测顺序未覆盖全部记录。')

    offsets = [0] * len(series)
    observations = []
    for index, count in sequence:
        start = offsets[index]
        observations.extend(series[index].observation(i) for i in range(start, start + count))
        offsets[index] += count
    return observations
