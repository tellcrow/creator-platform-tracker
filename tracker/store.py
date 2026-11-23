"""SQLite snapshots and portable, credential-free JSON backups."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .backup_columns import ExpansionBudget, pack_observations, unpack_observations
from .backup_v4 import WECHAT_HEADERS, pack_wechat_observations, unpack_wechat_observations
from .schema import (MAX_TITLE_LENGTH, PLATFORMS, RATIO_METRICS, SOURCES, article_aliases, default_source,
                     now_iso, platform_metrics, validate_platform)
from .scheduler import (validate_monitor_config, validate_platform_schedule,
                        validate_platform_schedules)

FLAT_BACKUP_VERSION = 2
COLUMN_BACKUP_VERSION = 3
BACKUP_VERSION = 4
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_BACKUP_OBSERVATIONS = 1_000_000
GZIP_MAGIC = b'\x1f\x8b'
CURRENT_WECHAT_ROWS = """AND o.source <> 'detail_daily'
    AND (o.source <> 'detail' OR o.id = (
        SELECT latest.id FROM observations latest
        WHERE latest.article_id=o.article_id AND latest.source='detail'
        ORDER BY latest.observed_ts DESC, latest.id DESC LIMIT 1))"""
TEXT_FIELDS = {
    'article_key': 1000, 'title': MAX_TITLE_LENGTH, 'msgid': 80, 'publish_date': 10,
    'publish_time': 40, 'status': 20, 'period_start': 40, 'period_end': 40,
    'period_label': 500, 'source_updated_through': 40, 'time_basis': 100,
    'saved_at': 40, 'imported_at': 40, 'source_file': 255,
    'identity_basis': 80, 'content_type': 80, 'visibility': 80,
}
SAFE_SETTINGS = {'schedule', 'active_account', 'collection', 'window_geometry',
                 'last_scheduled_run', 'last_run_at', 'interval_minutes',
                 'schedule_enabled', 'daily_times', 'schedule_mode',
                 'schedule_account', 'schedule_next', 'schedule_blocked',
                 'monitor_config', 'monitor_next', 'active_platform', 'schedule_platform',
                 'platform_schedules'}
SECRET_KEYS = re.compile(r'cookie|token|password|passwd|secret|credential|authorization|browser_profile|storage_state|local_?storage|session_?storage|session_?id', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _text(value, limit, field):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{field} 必须是长度不超过 {limit} 的文本')
    return value


def _timestamp(value, field='observed_at'):
    _text(value, 40, field)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ValueError(f'{field} 不是有效的 ISO 时间') from None
    if parsed.tzinfo is None:
        raise ValueError(f'{field} 必须带时区')
    return parsed


def _account_key(account, platform='wechat'):
    """A NUL cannot appear in a public account name, so legacy names cannot collide."""
    validate_platform(platform)
    _text(account, 80, '账号名称')
    return account if platform == 'wechat' else '\x00' + platform + '\x00' + account


def canonical_url(value, *, platform='wechat'):
    if not value:
        return ''
    _text(value, 2000, 'public_url')
    validate_platform(platform)
    parts = urlsplit(value)
    if parts.scheme not in ('https', 'http') or parts.username or parts.password or parts.port:
        raise ValueError('文章链接必须是平台公开内容链接')
    if platform == 'xiaohongshu':
        if parts.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
            raise ValueError('笔记链接必须是小红书公开链接')
        match = re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{16,64})', parts.path)
        if match:
            return 'https://www.xiaohongshu.com/explore/' + match[1].lower()
        raise ValueError('不保存后台登录链接')
    if platform == 'douyin':
        if parts.hostname not in ('www.douyin.com', 'douyin.com'):
            raise ValueError('作品链接必须是抖音公开链接')
        if re.fullmatch(r'/(?:video|note)/\d{1,30}', parts.path):
            return 'https://www.douyin.com' + parts.path
        raise ValueError('不保存后台登录链接')
    if parts.hostname != 'mp.weixin.qq.com':
        raise ValueError('文章链接必须是微信公开文章链接')
    if re.fullmatch(r'/s/[A-Za-z0-9_-]+', parts.path):
        return 'https://mp.weixin.qq.com' + parts.path
    if parts.path == '/s':
        query = {k: v for k, v in parse_qsl(parts.query) if k in ('__biz', 'mid', 'idx', 'sn')}
        if not query.get('__biz') or not query.get('mid') or not query.get('idx'):
            raise ValueError('公开文章长链接缺少标识')
        return urlunsplit(('https', 'mp.weixin.qq.com', '/s', urlencode(sorted(query.items())), ''))
    raise ValueError('不保存后台登录链接')


def validate_observation(raw, *, platform='wechat'):
    if not isinstance(raw, dict):
        raise ValueError('采集记录必须是对象')
    validate_platform(platform)
    if 'platform' in raw and raw['platform'] != platform:
        raise ValueError('记录平台与账号平台不一致')
    result = {'platform': platform}
    for name in ('article_key', 'title', 'source', 'observed_at', 'metrics'):
        if name not in raw:
            raise ValueError(f'采集记录缺少 {name}')
    result['source'] = raw['source']
    valid_sources = set(SOURCES) - {'content_list'} if platform == 'wechat' else {'content_list'}
    if not isinstance(result['source'], str) or result['source'] not in valid_sources:
        raise ValueError('未知数据来源')
    result['observed_at'] = _timestamp(raw['observed_at']).isoformat(timespec='seconds')
    for name, limit in TEXT_FIELDS.items():
        if raw.get(name) is not None:
            result[name] = _text(raw[name], limit, name)
    if not result.get('article_key') or not result.get('title'):
        raise ValueError('文章标识及标题不能为空')
    if result.get('public_url'):
        result['public_url'] = canonical_url(result['public_url'], platform=platform)
    elif raw.get('public_url'):
        result['public_url'] = canonical_url(raw['public_url'], platform=platform)
    if result['article_key'].startswith(('http://', 'https://')):
        result['article_key'] = canonical_url(result['article_key'], platform=platform)
        result.setdefault('public_url', result['article_key'])
        if platform != 'wechat':
            result['article_key'] = platform + ':' + urlsplit(result['article_key']).path.rsplit('/', 1)[-1]
    elif platform != 'wechat' and re.fullmatch(
            r'(?:xiaohongshu:|xhs:)[a-fA-F0-9]{16,64}' if platform == 'xiaohongshu'
            else r'douyin:\d{1,30}', result['article_key']):
        result['article_key'] = platform + ':' + result['article_key'].split(':', 1)[1].lower()
    elif not (platform == 'wechat' and re.fullmatch(r'(?:msgid:)?\d{1,30}_\d{1,5}', result['article_key'])):
        # Stable parser fallback IDs may be hashes of a URL; never accept a session URL.
        if not re.fullmatch(r'(?:article:|url:|unknown:|unresolved:|deleted:|overview:|title:)?[a-f0-9]{16,64}', result['article_key']):
            raise ValueError('文章标识必须是 msgid_idx、公开链接或稳定哈希')
    if platform != 'wechat' and result.get('public_url'):
        linked_key = platform + ':' + urlsplit(result['public_url']).path.rsplit('/', 1)[-1]
        if result['article_key'].startswith(platform + ':') and result['article_key'] != linked_key:
            raise ValueError('作品标识与公开链接不一致')
    if platform != 'wechat':
        # msgid is a WeChat identity, never a cross-platform alias.
        result.pop('msgid', None)
    if result.get('identity_basis') not in (None, '', 'platform_id', 'publish_time_title_hash'):
        raise ValueError('未知作品识别依据')
    if result.get('msgid') and not re.fullmatch(r'\d{1,30}_\d{1,5}', result['msgid']):
        raise ValueError('msgid 格式应为消息编号_文章序号')
    if result.get('status', 'published') not in ('published', 'deleted'):
        raise ValueError('未知文章状态')
    result.setdefault('status', 'published')
    for name in ('publish_date', 'source_updated_through'):
        if result.get(name):
            try:
                date.fromisoformat(result[name])
            except ValueError:
                raise ValueError(f'{name} 不是有效日期') from None
    for name in ('saved_at', 'imported_at', 'publish_time'):
        if result.get(name):
            _timestamp(result[name], name)
    for name in ('period_start', 'period_end'):
        if result.get(name):
            try:
                date.fromisoformat(result[name])
            except ValueError:
                _timestamp(result[name], name)
    if not isinstance(raw['metrics'], dict):
        raise ValueError('metrics 必须是对象')
    result['metrics'] = {}
    for key, value in raw['metrics'].items():
        if key not in platform_metrics(platform):
            continue
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1e15:
                raise ValueError(f'{key} 指标必须是有限的非负数或空值')
            if key in RATIO_METRICS and value > 1:
                raise ValueError('比例指标必须在 0 到 1 之间')
        result['metrics'][key] = value
    if raw.get('warnings'):
        if not isinstance(raw['warnings'], list) or len(raw['warnings']) > 30:
            raise ValueError('warnings 格式错误')
        result['warnings'] = [_text(x, 1000, 'warning') for x in raw['warnings']]
    if result.get('source_file'):
        # Keep a filename, never a local browser profile or machine path.
        result['source_file'] = result['source_file'].replace('\\', '/').rsplit('/', 1)[-1]
    return result


def _clean_setting(value, depth=0):
    if depth > 6:
        raise ValueError('设置嵌套过深')
    if isinstance(value, dict):
        return {str(k): _clean_setting(v, depth + 1) for k, v in value.items()
                if not SECRET_KEYS.search(str(k))}
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError('设置列表过长')
        return [_clean_setting(v, depth + 1) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str) and (len(value) > 2000 or re.search(r'(?:token=|cookie=|bearer\s)', value, re.I)):
            raise ValueError('设置中包含过长文本或登录凭据')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('设置含无效数字')
        return value
    raise ValueError('设置值类型不支持')


def _legacy_platform_schedules(settings):
    """Map the v0.3 single schedule to its binding, never to the visible tab."""
    config = settings.get('schedule', {'mode': 'off'})
    if not isinstance(config, dict):
        raise ValueError('旧版定时配置格式错误。')
    platform = validate_platform(settings.get('schedule_platform', config.get('platform', 'wechat')))
    account = settings.get('schedule_account', '')
    if not account:
        return {}
    return {platform: validate_platform_schedule(platform, {
        'account': account, 'config': dict(config, platform=platform),
        'next': settings.get('schedule_next'), 'blocked': settings.get('schedule_blocked', False),
    })}


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL REFERENCES accounts(name),
            article_key TEXT NOT NULL, title TEXT NOT NULL, public_url TEXT, msgid TEXT,
            publish_date TEXT, publish_time TEXT, UNIQUE(account, article_key));
          CREATE TABLE IF NOT EXISTS aliases (
            account TEXT NOT NULL, alias TEXT NOT NULL, article_id INTEGER NOT NULL REFERENCES articles(id),
            PRIMARY KEY(account, alias));
          CREATE TABLE IF NOT EXISTS excluded_article_aliases (
            account TEXT NOT NULL REFERENCES accounts(name), alias TEXT NOT NULL,
            PRIMARY KEY(account, alias));
          CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id),
            identity TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
            observed_ts REAL NOT NULL, data TEXT NOT NULL, UNIQUE(article_id, identity));
          CREATE INDEX IF NOT EXISTS observation_lookup ON observations(article_id,source,observed_ts);
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          -- Remove the retired module's data when opening an existing database.
          DROP TABLE IF EXISTS forecasts;
        ''')
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    @staticmethod
    def _identity(row):
        parts = [row.get(k, '') for k in ('source', 'observed_at', 'period_start', 'period_end')]
        parts[1] = _timestamp(parts[1]).astimezone(timezone.utc).isoformat(timespec='seconds')
        return hashlib.sha256(_json(parts).encode()).hexdigest()

    def _merge_articles(self, target, other):
        for row in self.db.execute('SELECT * FROM observations WHERE article_id=?', (other,)).fetchall():
            existing = self.db.execute('SELECT data FROM observations WHERE article_id=? AND identity=?', (target, row['identity'])).fetchone()
            if existing:
                old, new = json.loads(existing['data']), json.loads(row['data'])
                if old['metrics'] != new['metrics'] or old.get('status') != new.get('status'):
                    ident = row['identity'] + hashlib.sha256(row['data'].encode()).hexdigest()[:12]
                    self.db.execute('INSERT OR IGNORE INTO observations(article_id,identity,source,observed_at,observed_ts,data) VALUES(?,?,?,?,?,?)',
                                    (target, ident, row['source'], row['observed_at'], row['observed_ts'], row['data']))
                self.db.execute('DELETE FROM observations WHERE id=?', (row['id'],))
            else:
                self.db.execute('UPDATE observations SET article_id=? WHERE id=?', (target, row['id']))
        self.db.execute('UPDATE aliases SET article_id=? WHERE article_id=?', (target, other))
        self.db.execute('DELETE FROM articles WHERE id=?', (other,))
        self.db.execute(
            "DELETE FROM observations WHERE article_id=? AND source='detail' AND id<>"
            "(SELECT id FROM observations WHERE article_id=? AND source='detail' "
            "ORDER BY observed_ts DESC,id DESC LIMIT 1)", (target, target))

    def _add(self, account, rows, *, platform='wechat'):
        if not isinstance(account, str) or not account.strip():
            raise ValueError('账号名称不能为空')
        excluded = self.excluded_article_aliases(account, platform=platform)
        account = _account_key(account, platform)
        self.db.execute('INSERT OR IGNORE INTO accounts(name) VALUES(?)', (account,))
        counts = {'inserted': 0, 'skipped': 0, 'conflicts': 0}
        for row in rows:
            aliases = article_aliases(row)
            if row['source'] == 'detail_daily' or not excluded.isdisjoint(aliases):
                counts['skipped'] += 1
                continue
            if platform == 'wechat':
                row = {key: value for key, value in row.items()
                       if key not in ('source_file', 'imported_at', 'saved_at', 'warnings')}
            ids = set()
            for alias in aliases:
                found = self.db.execute('SELECT article_id FROM aliases WHERE account=? AND alias=?', (account, alias)).fetchone()
                if found:
                    ids.add(found[0])
            if ids:
                article_id = min(ids)
                for other in ids - {article_id}:
                    self._merge_articles(article_id, other)
            else:
                cur = self.db.execute('INSERT INTO articles(account,article_key,title,public_url,msgid,publish_date,publish_time) VALUES(?,?,?,?,?,?,?)',
                                      (account, row['article_key'], row['title'], row.get('public_url'), row.get('msgid'), row.get('publish_date'), row.get('publish_time')))
                article_id = cur.lastrowid
            for alias in aliases:
                self.db.execute('INSERT OR IGNORE INTO aliases(account,alias,article_id) VALUES(?,?,?)', (account, alias, article_id))
            self.db.execute('UPDATE articles SET public_url=COALESCE(public_url,?), msgid=COALESCE(msgid,?), publish_date=COALESCE(publish_date,?), publish_time=COALESCE(publish_time,?) WHERE id=?',
                            (row.get('public_url'), row.get('msgid'), row.get('publish_date'), row.get('publish_time'), article_id))
            ident = self._identity(row)
            observed_ts = _timestamp(row['observed_at']).timestamp()
            if row['source'] == 'detail':
                latest = self.db.execute(
                    "SELECT id,observed_ts,data FROM observations WHERE article_id=? AND source='detail' "
                    "ORDER BY observed_ts DESC,id DESC LIMIT 1", (article_id,)).fetchone()
                if latest:
                    if latest['observed_ts'] >= observed_ts:
                        counts['skipped'] += 1
                        if latest['observed_ts'] == observed_ts and json.loads(latest['data'])['metrics'] != row['metrics']:
                            counts['conflicts'] += 1
                    else:
                        self.db.execute('UPDATE observations SET identity=?,observed_at=?,observed_ts=?,data=? WHERE id=?',
                                        (ident, row['observed_at'], observed_ts, _json(row), latest['id']))
                        counts['inserted'] += 1
                    continue
            previous = self.db.execute('SELECT data FROM observations WHERE article_id=? AND identity=?', (article_id, ident)).fetchone()
            if previous is None:
                # Compatibility with early snapshots hashed using their local offset.
                for candidate in self.db.execute('SELECT data FROM observations WHERE article_id=? AND source=? AND observed_ts=?',
                                                 (article_id, row['source'], observed_ts)):
                    if self._identity(json.loads(candidate['data'])) == ident:
                        previous = candidate
                        break
            if previous:
                counts['skipped'] += 1
                old = json.loads(previous['data'])
                if old['metrics'] != row['metrics'] or old.get('status') != row.get('status'):
                    counts['conflicts'] += 1
                continue
            self.db.execute('INSERT INTO observations(article_id,identity,source,observed_at,observed_ts,data) VALUES(?,?,?,?,?,?)',
                            (article_id, ident, row['source'], row['observed_at'], observed_ts, _json(row)))
            counts['inserted'] += 1
        return counts

    def add_observations(self, account, observations, *, platform='wechat'):
        rows = [validate_observation(x, platform=platform) for x in observations]
        with self._lock, self.db:
            return self._add(account, rows, platform=platform)

    def accounts(self, *, platform='wechat'):
        validate_platform(platform)
        prefix = '\x00' + platform + '\x00'
        with self._lock:
            names = [x[0] for x in self.db.execute('SELECT name FROM accounts ORDER BY name')]
            if platform == 'wechat':
                return [name for name in names if not name.startswith('\x00')]
            return [name[len(prefix):] for name in names if name.startswith(prefix)]

    def _result(self, row, metadata=None):
        data = json.loads(row['data'])
        data.update(article_id=row['article_id'], observation_id=row['id'])
        if metadata:
            for key in ('public_url', 'msgid', 'publish_date', 'publish_time'):
                if not data.get(key) and metadata[key]:
                    data[key] = metadata[key]
        return data

    def articles(self, account, *, platform='wechat'):
        account = _account_key(account, platform)
        with self._lock:
            result = []
            for article in self.db.execute('SELECT * FROM articles WHERE account=? ORDER BY id', (account,)).fetchall():
                row = self.db.execute("SELECT * FROM observations WHERE article_id=? AND source<>'detail_daily' ORDER BY CASE source WHEN 'publish' THEN 4 WHEN 'detail' THEN 3 WHEN 'overview' THEN 2 ELSE 1 END DESC, observed_ts DESC, id DESC LIMIT 1", (article['id'],)).fetchone()
                if row:
                    result.append(self._result(row, article))
            return sorted(result, key=lambda x: (x.get('publish_time') or x.get('publish_date') or '', x['article_id']), reverse=True)

    def article_alias_map(self, account, *, platform='wechat'):
        account = _account_key(account, platform)
        with self._lock:
            return {row['alias']: str(row['article_id']) for row in self.db.execute(
                'SELECT alias,article_id FROM aliases WHERE account=?', (account,))}

    def excluded_article_aliases(self, account, *, platform='wechat'):
        account = _account_key(account, platform)
        with self._lock:
            return {row[0] for row in self.db.execute(
                'SELECT alias FROM excluded_article_aliases WHERE account=?', (account,))}

    def detail_refresh_state(self, account, *, platform='wechat'):
        """Return actual latest detail snapshots, indexed by account-bound aliases."""
        account = _account_key(account, platform)
        with self._lock:
            sql = '''SELECT o.*, a.article_key AS stored_article_key,
                       a.public_url AS article_public_url, a.msgid AS article_msgid,
                       a.publish_date AS article_publish_date,
                       a.publish_time AS article_publish_time
                     FROM articles a JOIN observations o ON o.id=(
                       SELECT id FROM observations
                       WHERE article_id=a.id AND source='detail'
                       ORDER BY observed_ts DESC,id DESC LIMIT 1)
                     WHERE a.account=?'''
            result, by_article = {}, {}
            for row in self.db.execute(sql, (account,)):
                metadata = {key: row['article_' + key]
                            for key in ('public_url', 'msgid', 'publish_date', 'publish_time')}
                record = self._result(row, metadata)
                by_article[row['article_id']] = record
                for key in (row['stored_article_key'], record.get('article_key'),
                            record.get('msgid'), record.get('public_url')):
                    if key:
                        result[key] = record
            for row in self.db.execute('SELECT alias,article_id FROM aliases WHERE account=?', (account,)):
                if row['article_id'] in by_article:
                    result[row['alias']] = by_article[row['article_id']]
            return result

    def history(self, account, article_id, source='default', *, platform='wechat'):
        account = _account_key(account, platform)
        if source == 'default':
            source = default_source(platform)
        with self._lock:
            sql = 'SELECT o.* FROM observations o JOIN articles a ON a.id=o.article_id WHERE a.account=? AND a.id=?'
            params = [account, article_id]
            if platform == 'wechat':
                sql += ' ' + CURRENT_WECHAT_ROWS
            if source is not None:
                sql += ' AND o.source=?'
                params.append(source)
            sql += ' ORDER BY o.observed_ts,o.id'
            return [self._result(row) for row in self.db.execute(sql, params)]

    def get_setting(self, key, default=None):
        with self._lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        if not isinstance(key, str) or len(key) > 80 or SECRET_KEYS.search(key):
            raise ValueError('无效或不允许的设置名')
        value = _clean_setting(value)
        if key == 'monitor_config':
            value = validate_monitor_config(value)
        elif key == 'platform_schedules':
            value = validate_platform_schedules(value)
        elif key in ('active_platform', 'schedule_platform'):
            value = validate_platform(value)
        elif key == 'schedule' and isinstance(value, dict):
            value['platform'] = validate_platform(value.get('platform', 'wechat'))
        elif key == 'monitor_next' and value is not None:
            _timestamp(value, 'monitor_next')
        with self._lock, self.db:
            self.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, _json(value)))

    def platform_schedules(self):
        """Read plans, migrating the legacy single binding exactly once."""
        with self._lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', ('platform_schedules',)).fetchone()
            if row is not None:
                return validate_platform_schedules(json.loads(row[0]))
            legacy = {}
            for name in ('schedule', 'schedule_account', 'schedule_platform', 'schedule_next', 'schedule_blocked'):
                value = self.get_setting(name)
                if value is not None:
                    legacy[name] = value
            plans = _legacy_platform_schedules(legacy)
            self.set_setting('platform_schedules', plans)
            return plans

    def save_platform_schedule(self, platform, entry):
        """Replace one platform plan atomically, preserving every other plan."""
        clean = validate_platform_schedule(platform, _clean_setting(entry))
        with self._lock:
            plans = self.platform_schedules()
            plans[platform] = clean
            self.set_setting('platform_schedules', plans)
        return clean

    def set_platform_schedule_blocked(self, platform, account, blocked):
        validate_platform(platform)
        _text(account, 80, '账号名称')
        if not isinstance(blocked, bool):
            raise ValueError('平台定时暂停状态必须是布尔值。')
        with self._lock:
            plans = self.platform_schedules()
            entry = plans.get(platform)
            if entry is None or entry['account'] != account.strip():
                return False
            entry['blocked'] = blocked
            self.set_setting('platform_schedules', plans)
            return True

    def backup_data(self):
        """Return the validated flat snapshot before encoding for export."""
        with self._lock:
            accounts = []
            for platform, name in ((platform, name) for platform in PLATFORMS
                                   for name in self.accounts(platform=platform)):
                account = _account_key(name, platform)
                observations = []
                sql = ('SELECT o.data,a.public_url,a.msgid FROM observations o '
                       'JOIN articles a ON a.id=o.article_id WHERE a.account=?')
                if platform == 'wechat':
                    sql += ' ' + CURRENT_WECHAT_ROWS
                for row in self.db.execute(sql + ' ORDER BY o.id', (account,)):
                    record = json.loads(row['data'])
                    for key in ('public_url', 'msgid'):
                        if not record.get(key) and row[key]:
                            record[key] = row[key]
                    observations.append(validate_observation(record, platform=platform))
                accounts.append({'name': name, 'platform': platform,
                                 'observations': observations})
            settings = {row['key']: _clean_setting(json.loads(row['value'])) for row in self.db.execute('SELECT * FROM settings') if row['key'] in SAFE_SETTINGS}
            plans = settings['platform_schedules'] if 'platform_schedules' in settings else _legacy_platform_schedules(settings)
            settings['platform_schedules'] = validate_platform_schedules(plans, disabled=True)
            # Restoring data must not unexpectedly start scheduled work on another computer.
            if isinstance(settings.get('schedule'), dict):
                settings['schedule']['enabled'] = False
                settings['schedule']['mode'] = 'off'
            else:
                settings['schedule'] = {'mode': 'off'}
            settings['schedule']['platform'] = validate_platform(settings['schedule'].get('platform', 'wechat'))
            settings['active_platform'] = validate_platform(settings.get('active_platform', 'wechat'))
            settings['schedule_platform'] = validate_platform(settings.get('schedule_platform', settings['schedule']['platform']))
            if 'schedule_enabled' in settings:
                settings['schedule_enabled'] = False
            settings['schedule_next'] = None
            settings['schedule_blocked'] = False
            monitor = settings.get('monitor_config', {})
            if not isinstance(monitor, dict):
                raise ValueError('文章采集设置格式错误。')
            settings['monitor_config'] = validate_monitor_config(dict(monitor, enabled=False))
            settings['monitor_next'] = None
            settings['monitor_blocked'] = False
            if 'schedule_mode' in settings:
                settings['schedule_mode'] = 'off'
            return {'format': 'wechat-article-tracker', 'version': FLAT_BACKUP_VERSION, 'exported_at': now_iso(), 'accounts': accounts, 'settings': settings}

    def export_backup(self, path):
        data = self.backup_data()
        formatted = {'format': data['format'], 'version': BACKUP_VERSION,
                     'exported_at': data['exported_at'], '表头': WECHAT_HEADERS,
                     'settings': data['settings'], 'accounts': []}
        count = 0
        for account in data['accounts']:
            rows = account['observations']
            if account['platform'] == 'wechat':
                articles = pack_wechat_observations(rows)
                count += sum(len(totals) + int(detail is not None) for _, totals, detail in articles)
                formatted['accounts'].append({'平台': '微信公众号', '账号': account['name'], '文章数据': articles})
            else:
                count += len(rows)
                formatted['accounts'].append({**account, 'observations': pack_observations(rows)})
        path = Path(path)
        compressed = path.suffix.lower() == '.gz'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.backup-', suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                stream = gzip.GzipFile(filename='', mode='wb', compresslevel=6,
                                       fileobj=output) if compressed else output
                with io.TextIOWrapper(stream, encoding='utf-8') as writer:
                    json.dump(formatted, writer, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return count

    def import_backup(self, path):
        with Path(path).open('rb') as file:
            if os.fstat(file.fileno()).st_size > MAX_BACKUP_BYTES:
                raise ValueError('备份文件超过 256 MB，当前版本不能载入；请保留备份和原始 data 数据文件夹')
            compressed = file.read(len(GZIP_MAGIC)) == GZIP_MAGIC
            file.seek(0)
            if compressed:
                try:
                    with gzip.GzipFile(fileobj=file, mode='rb') as stream:
                        contents = stream.read(MAX_BACKUP_BYTES + 1)
                except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
                    raise ValueError('压缩备份已损坏或未下载完整，无法导入。') from exc
            else:
                contents = file.read(MAX_BACKUP_BYTES + 1)
        if len(contents) > MAX_BACKUP_BYTES:
            if compressed:
                raise ValueError('备份解压后超过 256 MB，当前版本不能载入。')
            raise ValueError('备份文件超过 256 MB，当前版本不能载入；请保留备份和原始 data 数据文件夹')
        # Validate retained observations and settings below. Retired fields in old
        # backups are ignored, including invalid values inside those fields.
        try:
            raw = json.loads(contents.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('备份不是有效的 UTF-8 JSON 文件，或文件不完整。') from exc
        del contents
        if not isinstance(raw, dict) or raw.get('format') != 'wechat-article-tracker':
            raise ValueError('不支持的备份格式或版本')
        version = raw.get('version')
        if type(version) is not int or version not in (1, FLAT_BACKUP_VERSION, COLUMN_BACKUP_VERSION, BACKUP_VERSION):
            raise ValueError('不支持的备份格式或版本')
        if not isinstance(raw.get('accounts'), list) or len(raw['accounts']) > 100:
            raise ValueError('账号列表格式错误')
        if version == BACKUP_VERSION and raw.get('表头') != WECHAT_HEADERS:
            raise ValueError('微信备份表头与固定列顺序不一致')
        batches = []
        total = 0
        expanded = ExpansionBudget() if version >= COLUMN_BACKUP_VERSION else None
        for account in raw['accounts']:
            if not isinstance(account, dict):
                raise ValueError('备份账号记录必须是对象')
            if version == BACKUP_VERSION and account.get('平台') == '微信公众号':
                name = _text(account.get('账号'), 80, '公众号名称')
                platform = 'wechat'
                obs = unpack_wechat_observations(account.get('文章数据'),
                                                limit=MAX_BACKUP_OBSERVATIONS - total, budget=expanded)
            else:
                name = _text(account.get('name'), 80, '公众号名称')
                # v1 never declared a platform. A forged platform field cannot relabel old records.
                platform = validate_platform(account.get('platform') if version != 1 else 'wechat')
                if version == BACKUP_VERSION and platform == 'wechat':
                    raise ValueError('微信账号须使用固定表头的文章数据')
                obs = account.get('observations')
                if version >= COLUMN_BACKUP_VERSION:
                    obs = unpack_observations(obs, limit=MAX_BACKUP_OBSERVATIONS - total, budget=expanded)
                elif not isinstance(obs, list):
                    raise ValueError('备份记录列表格式错误')
            total += len(obs)
            if total > MAX_BACKUP_OBSERVATIONS:
                raise ValueError('备份记录过多')
            batches.append((name, platform, [validate_observation(x, platform=platform) for x in obs]))
        settings = raw.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('备份设置格式错误')
        settings = {key: _clean_setting(value) for key, value in settings.items() if key in SAFE_SETTINGS}
        plans = settings['platform_schedules'] if 'platform_schedules' in settings else _legacy_platform_schedules(settings)
        settings['platform_schedules'] = validate_platform_schedules(plans, disabled=True)
        for key in ('active_platform', 'schedule_platform'):
            if key in settings:
                settings[key] = validate_platform(settings[key])
        if isinstance(settings.get('schedule'), dict):
            settings['schedule']['enabled'] = False
            settings['schedule']['mode'] = 'off'
        else:
            settings['schedule'] = {'mode': 'off'}
        settings['schedule']['platform'] = validate_platform(settings['schedule'].get('platform', 'wechat'))
        settings.setdefault('active_platform', 'wechat')
        settings.setdefault('schedule_platform', settings['schedule']['platform'])
        if 'schedule_enabled' in settings:
            settings['schedule_enabled'] = False
        settings['schedule_next'] = None
        settings['schedule_blocked'] = False
        monitor = settings.get('monitor_config', {})
        if not isinstance(monitor, dict):
            raise ValueError('文章采集设置格式错误。')
        settings['monitor_config'] = validate_monitor_config(dict(monitor, enabled=False))
        settings['monitor_next'] = None
        settings['monitor_blocked'] = False
        if 'schedule_mode' in settings:
            settings['schedule_mode'] = 'off'
        counts = {'inserted': 0, 'skipped': 0, 'conflicts': 0}
        with self._lock, self.db:
            for account, platform, rows in batches:
                current = self._add(account, rows, platform=platform)
                for key in counts:
                    counts[key] += current[key]
            for key, value in settings.items():
                self.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, _json(value)))
        return counts
