"""Shared, credential-free observation contract."""
from datetime import datetime

# A creator caption can be much longer than a WeChat title. Preserve it intact
# in storage and portable backups, within the Excel cell text limit.
MAX_TITLE_LENGTH = 32767

METRICS = {
    'read': '阅读人数', 'like': '点赞人数', 'share': '分享人数',
    'recommend': '推荐人数', 'comment': '留言条数', 'underline': '划线人数',
    'donation': '赞赏金额（元）', 'repost': '被转载次数', 'vote': '投票人数',
    'collection': '收藏人数', 'follow': '阅读后关注人数',
    'avg_read_seconds': '平均阅读时长（秒）', 'finish_ratio': '读完率',
}
PLATFORMS = {'wechat': '微信公众号', 'xiaohongshu': '小红书', 'douyin': '抖音'}
_PLATFORM_METRICS = {
    'wechat': METRICS,
    'xiaohongshu': {
        'views': '观看量', 'likes': '点赞数', 'comments': '评论数',
        'favorites': '收藏数', 'shares': '分享数',
    },
    'douyin': {
        'plays': '播放量', 'likes': '点赞数', 'comments': '评论数',
        'shares': '分享数', 'favorites': '收藏数', 'followers_gained': '吸粉量',
        'detail_entry_ratio': '详情页进入率', 'completion_ratio': '完播率',
        'swipe_away_ratio': '划走率', 'caption_expand_ratio': '文案展开率',
        'avg_images_viewed': '平均浏览图片数',
    },
}
RATIO_METRICS = frozenset({'finish_ratio', 'detail_entry_ratio', 'completion_ratio',
                           'swipe_away_ratio', 'caption_expand_ratio'})
SOURCES = {'publish': '发表记录（累计）', 'detail': '数据中心（页面统计窗口）',
           'detail_daily': '数据中心（按日）', 'overview': '内容分析（所选窗口）',
           'content_list': '内容管理列表（累计）'}


def validate_platform(platform):
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise ValueError('未知内容平台')
    return platform


def platform_metrics(platform='wechat'):
    """Return only metrics with the selected platform's verified meaning."""
    return dict(_PLATFORM_METRICS[validate_platform(platform)])


def metric_labels(platform='wechat'):
    return platform_metrics(platform)


def default_source(platform='wechat'):
    return 'publish' if validate_platform(platform) == 'wechat' else 'content_list'


def primary_metric(platform='wechat'):
    return {'wechat': 'read', 'xiaohongshu': 'views', 'douyin': 'plays'}[validate_platform(platform)]


def article_aliases(row):
    return {row[key] for key in ('article_key', 'msgid', 'public_url') if row.get(key)}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')

# Parser returns {'observations': [dict], 'warnings': [str], 'page_type': str}.
# Observation required keys: article_key, title, source, observed_at, metrics.
# Optional: public_url, msgid, publish_date, status ('published'/'deleted'),
# period_start, period_end, period_label, source_updated_through, time_basis,
# saved_at, imported_at, source_file, warnings (list[str]), identity_basis,
# content_type, visibility. Platform-scoped calls default to legacy WeChat.
# metrics uses platform_metrics(platform) keys with number or None. Missing is never zero.
# article_key = msgid_idx when known, else canonical public /s/ URL.
# store.add_observations(account, observations) -> {'inserted': n, 'skipped': n}
# store.articles(account) -> latest publishing rows with article_id and all keys;
# other source used only when no publishing snapshot exists.
# store.history(account, article_id, source='publish') -> chronological observations.
# store.accounts() -> list[str]. store.export_backup(path), import_backup(path).
# store.get_setting(key, default=None), set_setting(key,value), close().
