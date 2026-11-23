"""Local wall-clock scheduling: restart catches up once, never replays a backlog."""
from datetime import datetime, timedelta
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .schema import validate_platform


def _monitor_target(value, platform='wechat'):
    if not isinstance(value, str) or not value or len(value) > 1000 or '\x00' in value:
        raise ValueError('文章采集目标必须是稳定的文章标识。')
    if platform == 'wechat' and re.fullmatch(r'(?:msgid:)?\d{1,30}_\d{1,5}', value):
        return value
    if platform == 'xiaohongshu' and re.fullmatch(r'(?:xiaohongshu:|xhs:)[a-fA-F0-9]{16,64}', value):
        return 'xiaohongshu:' + value.split(':', 1)[1].lower()
    if platform == 'douyin' and re.fullmatch(r'douyin:\d{1,30}', value):
        return value
    if re.fullmatch(r'(?:article:|url:|unknown:|unresolved:|deleted:|overview:|title:)?[a-f0-9]{16,64}', value):
        if value.isdecimal():
            raise ValueError('文章采集目标不能使用本机数据库序号。')
        return value
    if value.startswith(('http://', 'https://')):
        parts = urlsplit(value)
        if parts.username or parts.password or parts.port:
            raise ValueError('文章采集目标不允许含登录凭据或非标准端口。')
        if platform == 'xiaohongshu' and parts.hostname in ('www.xiaohongshu.com', 'xiaohongshu.com'):
            match = re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{16,64})', parts.path)
            if match and not parts.query and not parts.fragment:
                return 'https://www.xiaohongshu.com/explore/' + match[1].lower()
        if platform == 'douyin' and parts.hostname in ('www.douyin.com', 'douyin.com'):
            if re.fullmatch(r'/(?:video|note)/\d{1,30}', parts.path) and not parts.query and not parts.fragment:
                return 'https://www.douyin.com' + parts.path
        if platform == 'wechat' and parts.hostname == 'mp.weixin.qq.com':
            if re.fullmatch(r'/s/[A-Za-z0-9_-]+', parts.path) and not parts.query and not parts.fragment:
                return 'https://mp.weixin.qq.com' + parts.path
            query = dict(parse_qsl(parts.query))
            if (parts.path == '/s' and not parts.fragment
                    and set(query) <= {'__biz', 'mid', 'idx', 'sn'}
                    and all(query.get(k) for k in ('__biz', 'mid', 'idx'))):
                return urlunsplit(('https', 'mp.weixin.qq.com', '/s', urlencode(sorted(query.items())), ''))
    raise ValueError('文章采集目标必须是 msgid_idx、公开文章链接或稳定哈希。')


def validate_monitor_config(config):
    if not isinstance(config, dict):
        raise ValueError('文章采集设置格式错误。')
    enabled = config.get('enabled', False)
    platform = validate_platform(config.get('platform', 'wechat'))
    if not isinstance(enabled, bool):
        raise ValueError('文章采集启用状态必须是布尔值。')
    account = config.get('account', '')
    if not isinstance(account, str) or len(account) > 80 or '\x00' in account:
        raise ValueError('文章采集公众号名称无效。')
    account = account.strip()
    interval = config.get('interval_seconds', 60)
    if isinstance(interval, str) and re.fullmatch(r'\d{1,5}', interval.strip()):
        interval = int(interval.strip())
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError('文章采集间隔应为 30 至 86400 秒的整数（最长 1 天）。')
    raw_targets = config.get('targets', [])
    if not isinstance(raw_targets, (list, tuple)) or len(raw_targets) > 100:
        raise ValueError('文章采集目标列表格式错误。')
    targets = list(dict.fromkeys(_monitor_target(item, platform) for item in raw_targets))
    if len(targets) > 5 or (enabled and not targets):
        raise ValueError('文章采集每次请选择 1 至 5 篇文章。')
    if enabled and not account:
        raise ValueError('启用文章采集前请选择公众号。')
    return {'enabled': enabled, 'account': account, 'platform': platform,
            'targets': targets, 'interval_seconds': interval}


def next_monitor_due(config, after=None):
    config = validate_monitor_config(config)
    if not config['enabled']:
        return None
    return (after or datetime.now().astimezone()) + timedelta(seconds=config['interval_seconds'])

def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('定时设置格式错误。')
    mode = config.get('mode', 'off')
    if mode not in ('off', 'interval', 'daily'):
        raise ValueError('请选择关闭、间隔或每日固定时间。')
    result = {'mode': mode, 'platform': validate_platform(config.get('platform', 'wechat'))}
    if mode == 'interval':
        raw_hours = config.get('hours', 6)
        if isinstance(raw_hours, bool):
            raise ValueError('采集间隔应为 0.25 至 720 小时。')
        try:
            hours = float(raw_hours)
        except (TypeError, ValueError):
            raise ValueError('采集间隔应为 0.25 至 720 小时。') from None
        if not 0.25 <= hours <= 720:
            raise ValueError('采集间隔应为 0.25 至 720 小时。')
        result['hours'] = hours
    if mode == 'daily':
        raw = config.get('times', '08:00,20:00')
        values = raw.replace('，', ',').split(',') if isinstance(raw, str) else raw
        if not isinstance(values, (list, tuple)):
            raise ValueError('每日时间列表格式错误。')
        times = sorted(set(str(x).strip() for x in values))
        if not times or len(times) > 24:
            raise ValueError('请输入 1 至 24 个时间，以逗号分隔。')
        for t in times:
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t):
                raise ValueError('固定时间格式为 08:00,20:00。')
        result['times'] = times
    return result


def validate_platform_schedule(platform, entry, *, disabled=False):
    """Validate one independently bound schedule without retaining extra data."""
    platform = validate_platform(platform)
    if not isinstance(entry, dict):
        raise ValueError('平台定时设置必须是对象。')
    account = entry.get('account', '')
    if not isinstance(account, str) or len(account) > 80 or '\x00' in account:
        raise ValueError('平台定时账号名称无效。')
    account = account.strip()
    raw_config = entry.get('config', {'mode': 'off'})
    if not isinstance(raw_config, dict):
        raise ValueError('平台定时配置格式错误。')
    if raw_config.get('platform', platform) != platform:
        raise ValueError('定时配置与所属平台不一致。')
    config = validate_config(dict(raw_config, platform=platform))
    # Keep the user's interval/time choices when a plan is disabled for a
    # portable backup, while still validating dormant fields before storage.
    if config['mode'] == 'off':
        if 'hours' in raw_config:
            config['hours'] = validate_config({'mode': 'interval', 'hours': raw_config['hours']})['hours']
        if 'times' in raw_config:
            config['times'] = validate_config({'mode': 'daily', 'times': raw_config['times']})['times']
    if config['mode'] != 'off' and not account:
        raise ValueError('启用平台定时前请填写账号名称。')
    blocked = entry.get('blocked', False)
    if not isinstance(blocked, bool):
        raise ValueError('平台定时暂停状态必须是布尔值。')
    due = entry.get('next')
    if due is not None:
        if not isinstance(due, str) or len(due) > 40:
            raise ValueError('平台下次采集时间格式错误。')
        try:
            moment = datetime.fromisoformat(due.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('平台下次采集时间格式错误。') from None
        if moment.tzinfo is None:
            raise ValueError('平台下次采集时间必须带时区。')
    if disabled:
        config['mode'] = 'off'
        blocked = False
    if config['mode'] == 'off':
        due = None
    return {'account': account, 'config': config, 'next': due, 'blocked': blocked}


def validate_platform_schedules(value, *, disabled=False):
    if not isinstance(value, dict):
        raise ValueError('各平台定时设置必须是对象。')
    return {validate_platform(platform): validate_platform_schedule(platform, entry, disabled=disabled)
            for platform, entry in value.items()}

def next_due(config, after=None):
    config = validate_config(config)
    after = after or datetime.now().astimezone()
    if config['mode'] == 'off':
        return None
    if config['mode'] == 'interval':
        return after + timedelta(hours=config['hours'])
    candidates = []
    for offset in (0, 1):
        day = after + timedelta(days=offset)
        for t in config['times']:
            hour, minute = map(int, t.split(':'))
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > after:
                candidates.append(candidate)
    return min(candidates)

def is_due(due_iso, now=None, busy=False):
    if busy or not due_iso:
        return False
    try:
        due = datetime.fromisoformat(due_iso)
        if due.tzinfo is None:
            return False
        return (now or datetime.now().astimezone()) >= due
    except (ValueError, TypeError):
        return False
