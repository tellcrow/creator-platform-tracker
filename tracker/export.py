"""User-initiated Excel export from the desktop application."""
from __future__ import annotations

from pathlib import Path

from .schema import PLATFORMS, RATIO_METRICS, SOURCES, platform_metrics


def safe_cell(value):
    if isinstance(value, str):
        value = ''.join(c for c in value if ord(c) >= 32 or c in '\t\n\r')
        if value.lstrip().startswith(('=', '+', '-', '@')):
            value = "'" + value
        return value[:32767]
    return value


def export_excel(store, account, path, *, platform='wechat'):
    """Export current rows and retained observations."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    metrics = platform_metrics(platform)

    workbook = Workbook()
    current = workbook.active
    current.title = '文章最新数据'
    history = workbook.create_sheet('历史采集明细')
    help_sheet = workbook.create_sheet('口径说明')
    common = ['文章标题', '发表时间', '文章状态', '数据来源', '采集或快照时间',
              '时间依据', '页面数据更新至', '统计开始', '统计结束', '统计范围']
    trailing = ['公开文章链接', '消息标识']
    if platform != 'wechat':
        trailing += ['源文件', '导入时间', '记录说明', '作品类型', '可见范围', '作品识别依据']
    headers = common + list(metrics.values()) + trailing
    for sheet in (current, history):
        sheet.append(headers)

    def record_values(row):
        values = [row.get('title'), row.get('publish_time') or row.get('publish_date'),
                  '已删除' if row.get('status') == 'deleted' else '已发表',
                  SOURCES.get(row.get('source'), row.get('source')), row.get('observed_at'),
                  row.get('time_basis'), row.get('source_updated_through'),
                  row.get('period_start'), row.get('period_end'), row.get('period_label')]
        values += [row.get('metrics', {}).get(key) for key in metrics]
        values += [row.get('public_url'), row.get('msgid')]
        if platform != 'wechat':
            values += [row.get('source_file'), row.get('imported_at'), '；'.join(row.get('warnings', [])),
                       row.get('content_type'), row.get('visibility'), row.get('identity_basis')]
        return [safe_cell(value) for value in values]

    articles = store.articles(account, platform=platform)
    snapshot_count = 0
    for article in articles:
        current.append(record_values(article))
        for observation in store.history(account, article['article_id'], source=None, platform=platform):
            history.append(record_values(observation))
            snapshot_count += 1
    help_sheet.append(['项目', '说明'])
    for row in [
        ('平台', PLATFORMS[platform]),
        ('账号', account),
        ('空白值', '表示未提供或不可获取，不表示零。'),
        ('数据来源', '发表记录保留每次累计观测。每篇文章只保留最近一次数据中心详情，按原统计窗口查看。' if platform == 'wechat' else '仅记录内容管理列表页面展示的累计指标，不进入单篇详情。'),
        ('最新数据', '优先展示最新发表记录；没有发表记录的文章展示其他来源，并保留来源标签。' if platform == 'wechat' else '展示最近一次成功采集的列表数据，平台未提供的指标保留为空白。'),
        ('指标口径', '累计去重人数的差值称为累计人数净变化，不能当作每日去重人数；多篇文章人数相加也不是账号总去重人数。' if platform == 'wechat' else '观看量、播放量和互动数沿用页面口径，不等于去重人数。比例以百分比显示，平均浏览图片数不是累计数量。不同作品类型可能提供不同指标。'),
        ('时间', '离线网页的文件修改时间只是快照时间估计。表中分别列出采集时间和网页更新至日期。' if platform == 'wechat' else '离线网页的文件修改时间只是快照时间估计；采集时间、网页更新至日期和导入时间分别保留。'),
        ('采集空档', '保留真实采样间隔，不补造每日数据。累计数下降可能是平台修正，按实测值保存。'),
        ('备份', '此表用于查看；完整迁移请使用程序的 JSON 备份。登录状态不随备份迁移。'),
    ]:
        help_sheet.append([safe_cell(value) for value in row])
    for sheet in workbook:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        sheet.row_dimensions[1].height = 30
        for cell in sheet[1]:
            cell.fill = PatternFill('solid', fgColor='164E63')
            cell.font = Font(color='FFFFFF', bold=True)
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        for column in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(column)].width = 21 if column > 1 else 48
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=False)
            if row[0].row % 2 == 0:
                for cell in row:
                    cell.fill = PatternFill('solid', fgColor='F1F5F9')
        if sheet in (current, history):
            for offset, metric in enumerate(metrics, len(common) + 1):
                number_format = ('0.00%' if metric in RATIO_METRICS else
                                 '0.00' if metric in ('donation', 'avg_images_viewed') else None)
                if number_format:
                    for row in range(2, sheet.max_row + 1):
                        sheet.cell(row, offset).number_format = number_format
    help_sheet.column_dimensions['B'].width = 110
    for row in help_sheet.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical='top')
        help_sheet.row_dimensions[row[0].row].height = 34
    path = Path(path)
    temporary = path.with_name(path.stem + '.tmp.xlsx')
    workbook.save(temporary)
    temporary.replace(path)
    return {'articles': len(articles), 'observations': snapshot_count}
