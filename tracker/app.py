"""Local desktop interface with independent platform collection controls."""
import hashlib
import math
import os
from pathlib import Path
import queue
import re
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import __version__
from .schema import (METRICS, SOURCES, PLATFORMS, RATIO_METRICS, platform_metrics, default_source,
                     primary_metric, metric_labels, now_iso)
from .store import Store
from .parser import parse_file
from .scheduler import validate_config, next_due, is_due

MAIN_METRICS = ['read', 'like', 'share', 'recommend', 'comment', 'underline', 'donation', 'repost']
BG, INK, MUTED, TEAL = '#f3f6f8', '#173544', '#657984', '#008577'

def safe_error(error):
    value = str(error)
    value = re.sub(r'https?://\S+', '[网页地址已隐藏]', value)
    value = re.sub(r'(?i)(token|cookie|password|authorization|ticket|key)\s*[=:]\s*[^\s,;]+', r'\1=[已隐藏]', value)
    return value[:1200]

def display(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    return f'{value:,}' if isinstance(value, int) else str(value)

def display_metric(metric, value, delta=False):
    if value is None:
        return '—'
    if metric in RATIO_METRICS:
        return f'{value * 100:,.2f}'.rstrip('0').rstrip('.') + (' 个百分点' if delta else '%')
    return display(value)

def stamp(value):
    return (value or '').replace('T', ' ')[:19]

from .widgets import FlatTabs
from .monitor import MonitorMixin
from .collection_jobs import CollectionMixin
from .article_view import ArticleViewMixin
from .article_table import article_number_map

class App(MonitorMixin, CollectionMixin, ArticleViewMixin):
    def __init__(self, root, data_dir):
        self.root, self.data_dir = root, Path(data_dir)
        self.store = Store(self.data_dir / 'tracker.sqlite')
        self.queue, self.commands = queue.Queue(), queue.Queue()
        self.cancel = threading.Event()
        self.busy, self.closing = False, False
        self.active_jobs = {}
        self.rows, self.selected_id = {}, None
        initial_platform = self.store.get_setting('active_platform', 'wechat')
        if initial_platform not in PLATFORMS:
            initial_platform = 'wechat'
        self.platform = tk.StringVar(value=PLATFORMS[initial_platform])
        self._view_platform = initial_platform
        legacy_account = self.store.get_setting('active_account', '我的公众号') if initial_platform == 'wechat' else '我的账号'
        self.account = tk.StringVar(value=self.store.get_setting('active_account:' + initial_platform, legacy_account))
        self.search = tk.StringVar()
        self.status = tk.StringVar(value='就绪 · 导入保存的网页，或打开采集浏览器扫码登录。')
        self.overview = tk.StringVar(value='暂无记录')
        self.chart_source = tk.StringVar(value=SOURCES[default_source(initial_platform)])
        self.chart_metric = tk.StringVar(value=metric_labels(initial_platform)[primary_metric(initial_platform)])
        self.detail_title = tk.StringVar(value='在文章表格中选择一篇文章')
        self.detail_note = tk.StringVar(value='在文章总表中选择一篇文章。')
        self.schedule_note = tk.StringVar()
        self.force_details = tk.BooleanVar(value=False)
        self.sort_key, self.sort_desc = 'number', True
        self._init_monitor()
        self._build()
        self.worker = threading.Thread(target=self._worker, daemon=True, name='browser-worker')
        self.worker.start()
        self.refresh()
        self._load_schedule()
        self._load_monitor()
        self.root.after(150, self._pump)
        self.root.after(1000, self._tick)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def _build(self):
        self.root.title(f'创作平台长期数据记录器 · v{__version__}')
        self.root.geometry('1320x860')
        self.root.minsize(1000, 680)
        self.root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 21, 'bold'))
        style.configure('Sub.TLabel', foreground=MUTED)
        style.configure('TButton', padding=(12, 7))
        style.configure('Accent.TButton', background=TEAL, foreground='white')
        style.map('Accent.TButton', background=[('active', '#006d62')])
        style.configure('Treeview', rowheight=32, background='white', fieldbackground='white', foreground=INK)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'), padding=7)
        style.map('Treeview', background=[('selected', '#d1ece7')], foreground=[('selected', INK)])
        header = ttk.Frame(self.root, padding=(24, 18, 24, 10)); header.pack(fill='x')
        ttk.Label(header, text='创作平台长期数据记录器', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='本机保存  /  长期曲线  /  可携带备份', style='Sub.TLabel').pack(side='right')
        toolbar = ttk.Frame(self.root, padding=(24, 8)); toolbar.pack(fill='x')
        ttk.Label(toolbar, text='平台').pack(side='left')
        self.platforms = ttk.Combobox(toolbar, textvariable=self.platform, values=list(PLATFORMS.values()), state='readonly', width=10)
        self.platforms.pack(side='left', padx=(8, 14))
        self.platforms.bind('<<ComboboxSelected>>', self._change_platform)
        ttk.Label(toolbar, text='账号名称').pack(side='left')
        self.accounts = ttk.Combobox(toolbar, textvariable=self.account, width=16)
        self.accounts.pack(side='left', padx=(8, 12))
        self.accounts.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        self.accounts.bind('<Return>', lambda e: self.refresh())
        self.accounts.bind('<FocusOut>', lambda e: self.refresh() if not self._platform_busy(self._platform()) else None)
        overview_bar = ttk.Frame(self.root, padding=(24, 6))
        overview_bar.pack(fill='x')
        overview_bar.columnconfigure(0, weight=1)
        overview_label = ttk.Label(overview_bar, textvariable=self.overview, style='Sub.TLabel', wraplength=650)
        overview_label.grid(row=0, column=0, sticky='w')
        import_tools = ttk.Frame(overview_bar)
        import_tools.grid(row=0, column=1, sticky='e')
        self.actions = []
        for name, label, func, accent in [
                ('login_button', '① 扫码登录', self.login, False),
                ('collect_button', '② 采集当前', self.collect, True),
                ('collect_all_button', '一键全部采集', self.collect_all, True),
                ('html_button', '导入 HTML', self.import_html, False),
                ('import_backup_button', '导入备份', self.import_backup, False)]:
            parent = import_tools if name in ('html_button', 'import_backup_button') else toolbar
            button = ttk.Button(parent, text=label, command=func, style='Accent.TButton' if accent else 'TButton')
            button.pack(side='left', padx=4)
            setattr(self, name, button)
            if name != 'collect_all_button':
                self.actions.append(button)
        self.stop_button = ttk.Button(toolbar, text='停止全部', command=self.stop_collection, state='disabled')
        self.stop_button.pack(side='left', padx=4)
        self.tabs = FlatTabs(self.root); self.tabs.pack(fill='both', expand=True, padx=24, pady=(0, 12))
        articles, monitor, trend, settings, logs = [ttk.Frame(self.tabs, padding=14) for _ in range(5)]
        self._trend_tab = trend
        for frame, label in zip((articles, monitor, trend, settings, logs), ('文章总表', '文章采集', '历史曲线与明细', '定时采集与备份', '运行记录')):
            self.tabs.add(frame, text=label)
        filters = ttk.Frame(articles); filters.pack(fill='x', pady=(0, 10))
        ttk.Label(filters, text='搜索标题').pack(side='left')
        ttk.Entry(filters, textvariable=self.search, width=36).pack(side='left', padx=8)
        self.search.trace_add('write', lambda *_: self.render_articles())
        ttk.Button(filters, text='导出 Excel', command=self.export_excel).pack(side='right')
        ttk.Button(filters, text='查看曲线', command=lambda: self.tabs.select(trend)).pack(side='right', padx=8)
        cols = ['number', 'title', 'published', *MAIN_METRICS, 'observed', 'source', 'state', 'action']
        heads = ['序号', '文章标题', '发布时间', *[METRICS[x] for x in MAIN_METRICS], '最近观测时间', '来源', '状态', '操作']
        self.article_heads = dict(zip(cols, heads))
        self.article_tree = self._tree(articles, cols, heads, [65, 480, 150, *([95]*8), 160, 170, 100, 95], branches=True)
        self.article_tree.tag_configure('detail', background='#f0f7f5')
        for key in cols[:-1]:
            self.article_tree.heading(key, command=lambda k=key: self._sort_articles(k))
        self.article_tree.bind('<<TreeviewSelect>>', self.select_article)
        self.article_tree.bind('<<TreeviewOpen>>', lambda e: self._expand_article())
        self.article_tree.bind('<Button-1>', self._article_click)
        self.article_tree.bind('<Double-1>', self._article_double_click)
        self._build_monitor(monitor)
        self.articles_note = ttk.Label(articles, style='Sub.TLabel', wraplength=1120)
        self.articles_note.pack(anchor='w', pady=(10, 0))
        ttk.Label(trend, textvariable=self.detail_title, font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        choices = ttk.Frame(trend); choices.pack(fill='x', pady=10)
        for label, variable, values in [('数据来源', self.chart_source, [SOURCES[key] for key in self._history_sources()]), ('指标', self.chart_metric, list(METRICS.values()))]:
            ttk.Label(choices, text=label).pack(side='left', padx=(0, 8))
            c = ttk.Combobox(choices, textvariable=variable, values=values, state='readonly', width=27)
            c.pack(side='left', padx=(0, 20)); c.bind('<<ComboboxSelected>>', lambda e: self.draw_history())
            if label == '数据来源':
                self.source_choices = c
            else:
                self.metric_choices = c
        self.canvas = tk.Canvas(trend, height=260, bg='white', highlightthickness=0)
        self.canvas.pack(fill='x'); self.canvas.bind('<Configure>', lambda e: self.draw_chart())
        ttk.Label(trend, textvariable=self.detail_note, style='Sub.TLabel', wraplength=1100).pack(fill='x', pady=8)
        self.history_tree = self._tree(trend, ['observed', 'metric', 'change', 'period', 'basis', 'updated'],
            ['观测时间', '指标值', '与上次净变化', '统计日期 / 窗口', '时间依据', '平台更新至'], [180, 110, 130, 290, 170, 130], height=6)
        self.chart_points = []
        self._build_settings(settings)
        self.logbox = tk.Text(logs, wrap='word', relief='flat', bg='white', fg=INK, font=('Microsoft YaHei UI', 10), state='disabled')
        self.logbox.pack(fill='both', expand=True)
        ttk.Button(logs, text='查看使用说明', command=self.help).pack(anchor='e', pady=(10, 0))
        ttk.Label(self.root, textvariable=self.status, background='#e3ecee', foreground=INK, padding=(24, 10)).pack(fill='x')
        self._apply_platform_view()

    def _tree(self, parent, cols, heads, widths, height=12, branches=False):
        frame = ttk.Frame(parent); frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=cols, show='tree headings' if branches else 'headings', height=height, selectmode='browse')
        if branches:
            tree.column('#0', width=32, minwidth=32, stretch=False)
            tree.heading('#0', text='')
        for key, title, width in zip(cols, heads, widths):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=360 if key == 'title' else min(width, 70), stretch=False)
        sy = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        sx = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree.grid(row=0, column=0, sticky='nsew'); sy.grid(row=0, column=1, sticky='ns'); sx.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        return tree

    def _build_settings(self, frame):
        # Keep every setting reachable on smaller Win10 displays.
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        self.settings_canvas = canvas
        frame = content
        ttk.Label(frame, text='各平台独立定时采集', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='在左上角切换平台，为微信、小红书、抖音分别保存间隔或每日时间，互不覆盖。电脑保持唤醒且程序持续运行时执行；错过的任务只补采一次。', style='Sub.TLabel', wraplength=1100).pack(anchor='w', pady=10)
        self.schedule_form_title = tk.StringVar()
        ttk.Label(frame, textvariable=self.schedule_form_title, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        form = ttk.Frame(frame); form.pack(fill='x', pady=8)
        self.schedule_mode = tk.StringVar(value='关闭')
        self.schedule_hours = tk.StringVar(value='6')
        self.schedule_times = tk.StringVar(value='08:00,20:00')
        ttk.Label(form, text='执行方式').grid(row=0, column=0, sticky='w', pady=8)
        ttk.Combobox(form, textvariable=self.schedule_mode, values=['关闭', '按间隔', '每日固定时间'], state='readonly', width=18).grid(row=0, column=1, sticky='w', padx=12)
        ttk.Label(form, text='间隔（小时）').grid(row=1, column=0, sticky='w', pady=8)
        ttk.Entry(form, textvariable=self.schedule_hours, width=20).grid(row=1, column=1, sticky='w', padx=12)
        ttk.Label(form, text='每日时间').grid(row=2, column=0, sticky='w', pady=8)
        ttk.Entry(form, textvariable=self.schedule_times, width=30).grid(row=2, column=1, sticky='w', padx=12)
        ttk.Label(form, text='例如 08:00,20:00；使用电脑本地时间', style='Sub.TLabel').grid(row=2, column=2, sticky='w')
        ttk.Button(frame, text='保存定时设置（绑定当前平台和账号）', command=self.save_schedule, style='Accent.TButton').pack(anchor='w', pady=12)
        ttk.Label(frame, textvariable=self.schedule_note, wraplength=1100).pack(anchor='w', pady=8)
        data_options = ttk.Frame(frame)
        data_options.pack(anchor='w', fill='x')
        self.detail_options = ttk.Frame(data_options)
        self.detail_options.pack(anchor='w', fill='x')
        ttk.Checkbutton(self.detail_options, text='全量采集时强制刷新每篇数据中心（不使用 24 小时缓存）',
                        variable=self.force_details).pack(anchor='w', pady=8)
        ttk.Label(self.detail_options, text='默认每轮刷新全部发表记录；数据中心详情 24 小时内跳过重复读取。需要立即核对详情时勾选上方选项。',
                  style='Sub.TLabel', wraplength=1100).pack(anchor='w')
        self.list_options_note = ttk.Label(data_options, text='小红书和抖音每轮刷新内容管理列表并记录当前数值，不进入单篇详情。', style='Sub.TLabel', wraplength=1100)
        self.list_options_note.pack(anchor='w', pady=8)
        ttk.Separator(frame).pack(fill='x', pady=18)
        ttk.Label(frame, text='换电脑：导出备份 → 带走文件 → 新电脑导入', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='备份包含各平台账号和历史观测，不包含扫码登录凭据。新电脑需重新登录。导入会合并去重。', style='Sub.TLabel', wraplength=1100).pack(anchor='w', pady=10)
        backup_buttons = ttk.Frame(frame)
        backup_buttons.pack(anchor='w')
        self.backup_json_button = ttk.Button(backup_buttons, text='导出 JSON',
                                            command=lambda: self.backup(compressed=False))
        self.backup_gzip_button = ttk.Button(backup_buttons, text='导出 gzip',
                                            command=lambda: self.backup(compressed=True))
        self.backup_json_button.pack(side='left')
        self.backup_gzip_button.pack(side='left', padx=(8, 0))
        ttk.Label(frame, text=f'本机数据位置：{self.data_dir}', style='Sub.TLabel', wraplength=1100).pack(anchor='w', pady=18)

    def _platform(self):
        value = getattr(self, 'platform', None)
        label = value.get() if value is not None else PLATFORMS['wechat']
        return next((key for key, name in PLATFORMS.items() if name == label), 'wechat')

    def _main_metrics(self):
        return MAIN_METRICS if self._platform() == 'wechat' else list(platform_metrics(self._platform()))

    def _change_platform(self, event=None):
        previous = getattr(self, '_view_platform', 'wechat')
        platform = self._platform()
        if platform == previous:
            return
        self.store.set_setting('active_account:' + previous, self.account.get().strip())
        fallback = self.store.get_setting('active_account', '我的公众号') if platform == 'wechat' else '我的账号'
        names = self.store.accounts(platform=platform)
        account = self.store.get_setting('active_account:' + platform, names[0] if names else fallback)
        self._view_platform = platform
        self.account.set(account)
        self.store.set_setting('active_platform', platform)
        self.selected_id, self.rows, self.chart_points = None, {}, []
        self.sort_key, self.sort_desc = 'number', True
        self.article_tree.delete(*self.article_tree.get_children())
        self.search.set('')
        self.monitor_search.set('')
        self.chart_source.set(SOURCES[default_source(platform)])
        self.chart_metric.set(metric_labels(platform)[primary_metric(platform)])
        self._apply_platform_view()
        self.refresh()
        self._load_schedule()
        self._monitor_status()
        self._refresh_job_controls()

    def _apply_platform_view(self):
        platform = self._platform()
        labels = metric_labels(platform)
        metrics = self._main_metrics()
        cols = ['number', 'title', 'published', *metrics, 'observed', 'source', 'state', 'action']
        heads = ['序号', '文章标题' if platform == 'wechat' else '作品标题', '发布时间',
                 *[labels[key] for key in metrics], '最近观测时间', '来源', '状态', '操作']
        self.article_heads = dict(zip(cols, heads))
        # Do not reconfigure existing columns after every job: preserve user widths.
        if tuple(self.article_tree['columns']) != tuple(cols):
            self.article_tree.configure(columns=cols)
            widths = [65, 480, 150, *([110] * len(metrics)), 160, 170, 100, 95]
            for key, head, width in zip(cols, heads, widths):
                self.article_tree.heading(key, text=head)
                self.article_tree.column(key, width=width, minwidth=360 if key == 'title' else min(width, 70), stretch=False)
                if key != 'action':
                    self.article_tree.heading(key, command=lambda k=key: self._sort_articles(k))
        sources = [SOURCES[key] for key in self._history_sources()]
        self.source_choices.configure(values=sources)
        self.metric_choices.configure(values=list(labels.values()))
        if self.chart_source.get() not in sources:
            self.chart_source.set(SOURCES[default_source(platform)])
        if self.chart_metric.get() not in labels.values():
            self.chart_metric.set(labels[primary_metric(platform)])
        self.html_button.configure(state='normal' if platform == 'wechat' and not self._platform_busy(platform) else 'disabled')
        if platform == 'wechat':
            self.detail_options.pack(anchor='w', fill='x')
            self.list_options_note.pack_forget()
        else:
            self.detail_options.pack_forget()
            # The setting explanation stays in the same location when platforms switch.
            self.list_options_note.pack(anchor='w', pady=8)
        self.articles_note.configure(text='“—”表示不可得；' + ('总表优先展示发表记录，不同来源分开保留。' if platform == 'wechat' else '只记录内容管理列表，比例与计数分别显示。') + '点击三角展开快照；点击表头排序，双击标题或点击“历史曲线”查看历史。')
        metric_name = labels[primary_metric(platform)]
        if hasattr(self, 'monitor_help'):
            self.monitor_help.configure(text='选择 1–5 篇文章，每轮刷新累计数据，并逐篇打开“发表数据”详情，读取后关闭。间隔 30 秒至 1 天。' if platform == 'wechat' else '选择 1–5 篇作品，每轮刷新内容管理列表并保存所选作品数据，不进入详情。间隔 30 秒至 1 天。')
            self.monitor_tree.heading('read', text=metric_name)
            self.monitor_footer.configure(text='每条记录保存实际系统采集时间；平台更新可能滞后于采集间隔。页面慢时会延后，请在运行记录查看异常。')

    def _account(self):
        value = self.account.get().strip()
        if not value or len(value) > 80:
            raise ValueError('请输入当前平台账号的实际名称（最多 80 个字符），用于区分本机数据。')
        return value

    def refresh(self):
        try:
            account = self._account()
            platform = self._platform()
            self.store.set_setting('active_account:' + platform, account)
            if platform == 'wechat':
                self.store.set_setting('active_account', account)
            self.accounts['values'] = self.store.accounts(platform=platform)
            self.rows = {str(row['article_id']): row for row in self.store.articles(account, platform=platform)}
            numbers = article_number_map(self.rows.values())
            for aid, row in self.rows.items():
                row['number'] = numbers[aid]
                row['state'] = ('已删除' if row.get('status') == 'deleted' else '已发表') + (' / 导入' if row.get('time_basis') != 'live' else '')
            self.render_articles()
            self._render_monitor_articles()
            self.overview.set(f'{PLATFORMS[platform]} / {account}  ·  已记录 {len(self.rows)} 篇作品  ·  数据保存在本机  ·  每轮保留系统采集时间')
            self.schedule_form_title.set(f'当前设置：{PLATFORMS[platform]} / {account}')
            if self.selected_id not in self.rows:
                self.selected_id = None
            self.select_article()
        except Exception as exc:
            self.log(safe_error(exc))

    def select_article(self, event=None):
        chosen = self.article_tree.selection()
        if chosen:
            self.selected_id = self.article_tree.parent(chosen[0]) or chosen[0]
        row = self.rows.get(self.selected_id)
        self.detail_title.set(row['title'] if row else '在文章总表中选择一篇文章')
        self.draw_history()

    def draw_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        self.chart_points = []
        if not self.selected_id:
            self.detail_note.set('在文章总表中选择一篇文章。')
            self.draw_chart(); return
        platform = self._platform()
        cumulative = default_source(platform)
        source = next((key for key in self._history_sources()
                       if SOURCES[key] == self.chart_source.get()), cumulative)
        metric = next(k for k, v in metric_labels(platform).items() if v == self.chart_metric.get())
        rows = self.store.history(self._account(), int(self.selected_id), source, platform=platform)
        previous = None
        for row in rows:
            value = row['metrics'].get(metric)
            diff = None if previous is None or value is None or source != cumulative else value - previous
            if value is not None:
                previous = value
            period = ' 至 '.join(filter(None, [row.get('period_start'), row.get('period_end')]))
            period = ' · '.join(filter(None, [period, row.get('period_label')])) or '累计观测'
            basis = {'live': '系统采集时间', 'file_mtime': '文件修改时间（估计）', 'file_mtime_estimate': '文件修改时间（估计）', 'saved_file_mtime': '文件修改时间（估计）', 'manual': '手动指定时间', 'user': '手动指定时间'}.get(row.get('time_basis'), row.get('time_basis', '未知'))
            self.history_tree.insert('', 'end', values=[stamp(row['observed_at']), display_metric(metric, value), display_metric(metric, diff, delta=True), period, basis, row.get('source_updated_through') or '—'])
            if source == cumulative and value is not None:
                try:
                    dt = datetime.fromisoformat(row['observed_at'])
                    self.chart_points.append((dt.timestamp(), value, dt.strftime('%m-%d %H:%M')))
                except (TypeError, ValueError):
                    pass
        source_note = {
            'publish': '累计人数的差值是累计去重人数净变化，不等于当天阅读人数。',
            'content_list': '内容管理列表的观测差值；比例变化以百分点显示，不能当作新增次数。',
            'detail': '详情显示最近一次取得的统计窗口及指标。',
            'overview': '内容分析按原始统计窗口展示。',
        }[source]
        if source == cumulative:
            source_note += '  圆点为真实记录，连线仅便于观察；间隔超过 48 小时用虚线。首次记录前不补造历史。'
        self.detail_note.set(source_note)
        self.draw_chart()

    def draw_chart(self):
        if not hasattr(self, 'canvas'):
            return
        c = self.canvas; c.delete('all')
        width, height = max(c.winfo_width(), 600), max(c.winfo_height(), 240)
        points = getattr(self, 'chart_points', [])
        if not points:
            text = '此来源暂无可绘制的数据'
            if self.selected_id and self.chart_source.get() in (SOURCES['detail'], SOURCES['overview']):
                text = '请在下方表格查看统计窗口和指标'
            c.create_text(width/2, height/2, text=text, fill=MUTED, font=('Microsoft YaHei UI', 12)); return
        left, top, right, bottom = 78, 28, width-28, height-45
        low, high = min(p[1] for p in points), max(p[1] for p in points)
        metric = next((key for key, label in metric_labels(self._platform()).items() if label == self.chart_metric.get()), '')
        span = max(high-low, high*.08, .001 if metric in RATIO_METRICS else 1)
        ymin, ymax = max(0, low-span*.2), high+span*.2
        if metric in RATIO_METRICS:
            ymax = min(1, ymax)
        tmin, tmax = min(p[0] for p in points), max(p[0] for p in points)
        def xy(p):
            return (left+(p[0]-tmin)/max(tmax-tmin, 1)*(right-left) if tmax>tmin else (left+right)/2,
                    bottom-(p[1]-ymin)/(ymax-ymin)*(bottom-top))
        for i in range(5):
            y = top + i*(bottom-top)/4
            c.create_line(left, y, right, y, fill='#e9eef0')
            value = ymax-i*(ymax-ymin)/4
            c.create_text(left-10, y, text=display_metric(metric, value) if metric in RATIO_METRICS else display(round(value, 1)), anchor='e', fill=MUTED)
        prev = None
        for p in points:
            x, y = xy(p)
            if prev:
                c.create_line(*xy(prev), x, y, fill=TEAL, width=2, dash=(5, 4) if p[0]-prev[0]>172800 else ())
            c.create_oval(x-3, y-3, x+3, y+3, fill=TEAL, outline='white')
            prev = p
        c.create_text(left, bottom+22, text=points[0][2], anchor='w', fill=MUTED)
        c.create_text(right, bottom+22, text=points[-1][2], anchor='e', fill=MUTED)
        c.create_text(right, 13, text=f'{self.chart_metric.get()} · {len(points)} 个观测点', anchor='e', fill=INK)

    def log(self, text):
        text = safe_error(text)
        line = f'[{datetime.now():%m-%d %H:%M:%S}] {text}\n'
        self.logbox.configure(state='normal'); self.logbox.insert('end', line)
        if int(self.logbox.index('end-1c').split('.')[0]) > 2000:
            self.logbox.delete('1.0', '501.0')
        self.logbox.see('end'); self.logbox.configure(state='disabled')
        try:
            logfile = self.data_dir / 'run.log'
            if logfile.exists() and logfile.stat().st_size > 2_000_000:
                logfile.replace(self.data_dir / 'run.previous.log')
            with logfile.open('a', encoding='utf-8') as handle:
                handle.write(line)
        except OSError:
            pass

    def login(self):
        try:
            if self._account() in ('我的公众号', '我的账号'):
                messagebox.showinfo('先填写账号名称', '请将左上角账号名称改为当前平台后台显示的实际名称，然后点击扫码登录。')
                return
            self._start('login', platform=self._platform())
        except Exception as exc:
            messagebox.showerror('无法登录', safe_error(exc))

    def collect(self, account=None, platform=None):
        try:
            return self._start('collect', account=account, platform=platform or self._platform())
        except Exception as exc:
            self.log(exc)
            return False

    def _configured_collection_account(self, platform):
        """Choose one explicitly configured account for this platform."""
        if platform == self._platform():
            name = self.account.get().strip()
        else:
            name = self.store.get_setting('active_account:' + platform, '')
            if not name and platform == 'wechat':
                name = self.store.get_setting('active_account', '')
            if not name or name in ('我的公众号', '我的账号'):
                names = self.store.accounts(platform=platform)
                name = names[0] if names else ''
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or '\x00' in name:
            return None
        return None if name.strip() in ('我的公众号', '我的账号') else name.strip()

    def collect_all(self):
        started, running, unconfigured = [], [], []
        for platform, label in PLATFORMS.items():
            if self._platform_busy(platform):
                running.append(label)
                continue
            account = self._configured_collection_account(platform)
            if account is None:
                unconfigured.append(label)
                continue
            if self.collect(account, platform=platform):
                started.append(label)
        messages = []
        if started:
            messages.append('已启动：' + '、'.join(started))
        if running:
            messages.append('正在运行，未重复启动：' + '、'.join(running))
        if unconfigured:
            messages.append('未配置账号，已跳过：' + '、'.join(unconfigured))
        if messages:
            self.status.set('；'.join(messages))
            self.log('一键全部采集 · ' + '；'.join(messages))
        if unconfigured and not started and not running:
            messagebox.showinfo('先配置采集账号', '请分别切换到需要采集的平台，填写后台实际账号名称并登录，再点击“一键全部采集”。')
        self._refresh_job_controls()

    def import_html(self):
        if self._platform_busy(self._platform()):
            return
        if self._platform() != 'wechat':
            messagebox.showinfo('当前平台不支持 HTML 导入', '小红书和抖音请通过登录后的内容管理列表采集。')
            return
        paths = filedialog.askopenfilenames(title='选择保存的后台 HTML，可多选', filetypes=[('网页文件', '*.html *.htm'), ('所有文件', '*.*')])
        if paths:
            self._start('html', list(paths), platform=self._platform())

    def _load_schedule(self):
        try:
            schedules = self.store.platform_schedules()
            for platform, entry in schedules.items():
                cfg = entry['config']
                if cfg['mode'] != 'off' and not entry.get('next'):
                    self.store.save_platform_schedule(platform, dict(entry, next=next_due(cfg).isoformat()))
            entry = schedules.get(self._platform(), {})
            cfg = validate_config(entry.get('config', {'mode': 'off', 'platform': self._platform()}))
        except (ValueError, TypeError):
            cfg = {'mode': 'off', 'platform': self._platform()}
            self.log('定时设置读取失败，请重新保存当前平台的设置。')
        self.schedule_mode.set({'off': '关闭', 'interval': '按间隔', 'daily': '每日固定时间'}[cfg['mode']])
        self.schedule_hours.set(str(cfg.get('hours', 6)))
        self.schedule_times.set(','.join(cfg.get('times', ['08:00', '20:00'])))
        self._schedule_status()

    def save_schedule(self):
        try:
            cfg = validate_config({'mode': {'关闭': 'off', '按间隔': 'interval', '每日固定时间': 'daily'}[self.schedule_mode.get()],
                                   'platform': self._platform(),
                                   'hours': self.schedule_hours.get(), 'times': self.schedule_times.get()})
            due = next_due(cfg)
            account = self._account()
            if cfg['mode'] != 'off' and account in ('我的公众号', '我的账号'):
                raise ValueError('开启定时前，请填写当前平台后台显示的实际账号名称。')
            self.store.save_platform_schedule(self._platform(), {'account': account, 'config': cfg,
                'next': due.isoformat() if due else None, 'blocked': False})
            self._schedule_status()
            self.log(f'{PLATFORMS[self._platform()]}定时设置已保存，其他平台计划保持原设置。')
        except Exception as exc:
            messagebox.showerror('设置未保存', safe_error(exc))

    def _schedule_status(self):
        self.schedule_form_title.set(f'当前设置：{PLATFORMS[self._platform()]} / {self.account.get().strip() or "未填写账号"}')
        schedules = self.store.platform_schedules()
        lines = []
        for platform, label in PLATFORMS.items():
            entry = schedules.get(platform, {})
            account = entry.get('account') or '未绑定账号'
            cfg = entry.get('config', {'mode': 'off'})
            if entry.get('blocked'):
                state = '已暂停，需检查登录或运行记录'
            elif cfg['mode'] == 'off':
                state = '定时关闭'
            else:
                setting = f'每 {cfg["hours"]:g} 小时' if cfg['mode'] == 'interval' else '每日 ' + '、'.join(cfg['times'])
                state = f'{setting} · 下次 {stamp(entry.get("next")) or "等待安排"}'
            job = getattr(self, 'active_jobs', {}).get(platform)
            if job:
                state = f'正在执行（{job.get("account", account)}） · ' + state
            lines.append(f'{label} / {account}：{state}')
        self.schedule_note.set('\n'.join(lines) + '\n以上时间均为电脑系统时间；同一平台不重叠执行。')

    def _tick(self):
        if self.closing:
            return
        self._monitor_tick()
        try:
            schedules = self.store.platform_schedules()
        except (ValueError, TypeError):
            schedules = {}
            self.log('定时配置无效，未执行采集；请重新保存设置。')
        changed = False
        for platform, entry in schedules.items():
            cfg = entry['config']
            if cfg['mode'] == 'off' or entry.get('blocked') or self._platform_busy(platform):
                continue
            if not entry.get('next'):
                self.store.save_platform_schedule(platform, dict(entry, next=next_due(cfg).isoformat()))
                changed = True
                continue
            if is_due(entry['next']):
                account = entry.get('account')
                if not account:
                    self.store.save_platform_schedule(platform, dict(entry, blocked=True))
                    self.log(f'{PLATFORMS[platform]}定时任务缺少绑定账号，请重新保存设置。')
                elif self.collect(account, platform=platform) is not False:
                    due = next_due(cfg)
                    self.store.save_platform_schedule(platform, dict(entry, next=due.isoformat() if due else None))
                    self.log(f'{PLATFORMS[platform]}定时任务已触发。')
                changed = True
        if changed:
            self._schedule_status()
        self.root.after(1000, self._tick)

    def backup(self, *, compressed):
        extension, label = ('.json.gz', 'gzip 备份') if compressed else ('.json', 'JSON 备份')
        filename = filedialog.asksaveasfilename(
            parent=self.root, title=f'保存全部平台{label}',
            initialfile=f'创作平台备份_{datetime.now():%Y%m%d_%H%M}{extension}',
            defaultextension=extension, filetypes=[(label, f'*{extension}')],
        )
        if not filename:
            return
        try:
            path = Path(filename)
            expected_suffix = '.gz' if compressed else '.json'
            if path.suffix.lower() != expected_suffix:
                append = '.gz' if compressed and path.suffix.lower() == '.json' else extension
                path = path.with_name(path.name + append)
                if path.exists() and not messagebox.askyesno(
                        '确认替换', f'“{path.name}”已存在。要替换此文件吗？', parent=self.root):
                    return
            self.store.export_backup(path)
            self.log(f'备份已导出：{path.name}')
            messagebox.showinfo('备份完成', '可将此备份文件带到另一台电脑导入。登录信息未包含在备份内。')
        except Exception as exc:
            messagebox.showerror('导出失败', safe_error(exc))

    def import_backup(self):
        if self.busy:
            self.status.set('请等待全部采集任务结束后再导入备份。')
            return
        path = filedialog.askopenfilename(
            parent=self.root, title='导入备份', filetypes=[('备份文件', ('*.json.gz', '*.json', '*.gz'))],
        )
        if path:
            try:
                result = self.store.import_backup(path)
                self.log(f'备份已合并：{result}')
                names = self.store.accounts(platform=self._platform())
                if names and self.account.get() not in names:
                    self.account.set(names[0])
                self.monitor_generation += 1
                self.monitor_cancel.set()
                self.refresh(); self._load_schedule(); self._load_monitor()
            except Exception as exc:
                messagebox.showerror('导入失败', safe_error(exc))

    def export_excel(self):
        path = filedialog.asksaveasfilename(title='导出当前平台账号 Excel', defaultextension='.xlsx', initialfile=f'{PLATFORMS[self._platform()]}数据_{datetime.now():%Y%m%d}.xlsx', filetypes=[('Excel 工作簿', '*.xlsx')])
        if path:
            try:
                from .export import export_excel
                export_excel(self.store, self._account(), path, platform=self._platform())
                self.log(f'Excel 已导出：{Path(path).name}')
            except Exception as exc:
                messagebox.showerror('导出失败', safe_error(exc))

    def help(self):
        messagebox.showinfo('使用说明',
            '1. 选择平台，填写后台显示的实际账号名称并登录；可采集当前平台，或一键全部采集已配置的平台。\n'
            '2. 微信可导入 HTML；时间采用文件修改时间并标为估计。小红书与抖音只采内容管理列表。\n'
            '3. 各平台可分别保存采集间隔；本程序需持续运行且电脑保持唤醒。\n'
            '4. JSON 用于换电脑恢复，Excel 用于查看统计。不要携带 browser_profiles 登录目录。\n'
            '5. 发表累计值保留全部采集时间，数据中心详情每篇保存最近一次。\n\n'
            '此版本已使用保存网页验证。在线后台适配需要首次登录试采，异常会写入运行记录。')

    def close(self):
        if self.closing:
            return
        self.stop_collection()
        self.closing = True
        self.status.set('正在停止任务并关闭采集浏览器，请稍候…')
        for button in self.actions:
            button.configure(state='disabled')
        self.collect_all_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.platforms.configure(state='disabled')
        self.accounts.configure(state='disabled')
        self.commands.put(None)
