"""Sortable article table and lazily expanded source snapshots."""
from .schema import SOURCES, platform_metrics, default_source
from .article_table import sort_article_rows


class ArticleViewMixin:
    def _history_sources(self):
        return ('publish', 'detail', 'overview') if self._platform() == 'wechat' else ('content_list',)

    def _sort_articles(self, key):
        self.sort_desc = not self.sort_desc if key == self.sort_key else False
        self.sort_key = key
        self.render_articles()

    def _article_values(self, row, title=None, number=None, state=None, delta=False):
        from .app import display_metric, stamp
        return [row.get('number', '') if number is None else number,
                row.get('title', '') if title is None else title,
                stamp(row.get('publish_time') or row.get('publish_date')),
                *[display_metric(k, row.get('metrics', {}).get(k), delta=delta) for k in self._main_metrics()],
                stamp(row.get('observed_at')), SOURCES.get(row.get('source'), row.get('source', '')),
                row.get('state', '') if state is None else state, '历史曲线']

    def render_articles(self):
        tree = self.article_tree
        expanded = {aid for aid in tree.get_children() if tree.item(aid, 'open')}
        view = tree.yview()
        tree.delete(*tree.get_children())
        needle = self.search.get().strip().casefold()
        rows = [row for row in self.rows.values() if not needle or needle in row.get('title', '').casefold()]
        for row in sort_article_rows(rows, self.sort_key, self.sort_desc, platform=self._platform()):
            aid = str(row['article_id'])
            tree.insert('', 'end', iid=aid, values=self._article_values(row))
            tree.insert(aid, 'end', iid=aid + ':placeholder', values=['', '展开查看数据来源及最近变化'])
            if aid in expanded:
                self._expand_article(aid)
                tree.item(aid, open=True)
        for key, label in self.article_heads.items():
            tree.heading(key, text=label + (' ▼' if self.sort_desc else ' ▲') if key == self.sort_key else label)
        if self.selected_id and tree.exists(self.selected_id):
            tree.selection_set(self.selected_id)
        if view:
            tree.yview_moveto(view[0])

    def _expand_article(self, aid=None):
        aid = aid or self.article_tree.focus()
        if aid not in self.rows:
            return
        tree = self.article_tree
        tree.delete(*tree.get_children(aid))
        platform = self._platform()
        history = self.store.history(self._account(), int(aid), None, platform=platform)
        grouped = {}
        for row in history:
            grouped.setdefault(row['source'], []).append(row)
        for source in self._history_sources():
            records = grouped.get(source, [])
            if not records:
                continue
            latest = records[-1]
            period = ' 至 '.join(filter(None, [latest.get('period_start'), latest.get('period_end')]))
            title = SOURCES[source] + (' · ' + period if period else '')
            values = self._article_values(latest, title=title, number='', state='最近快照')
            tree.insert(aid, 'end', iid=aid + ':' + source, values=values, tags=('detail',))
            if source == default_source(platform) and len(records) > 1:
                previous = records[-2]
                delta = dict(latest, metrics={k: latest['metrics'].get(k) - previous['metrics'].get(k)
                    if latest['metrics'].get(k) is not None and previous['metrics'].get(k) is not None else None for k in platform_metrics(platform)})
                tree.insert(aid, 'end', iid=aid + ':change', values=self._article_values(delta,
                    title='较上次累计净变化（不等于每日人数）' if platform == 'wechat' else '较上次观测变化（比例以百分点表示）',
                    number='', state='两次观测差值', delta=True), tags=('detail',))
        if not tree.get_children(aid):
            tree.insert(aid, 'end', iid=aid + ':empty', values=['', '暂无观测'])

    def _article_click(self, event):
        tree = self.article_tree
        aid = tree.identify_row(event.y)
        if aid and tree.identify_column(event.x) == '#' + str(len(tree['columns'])):
            self._open_article_history(aid)
            return 'break'

    def _article_double_click(self, event):
        if self.article_tree.identify_column(event.x) == '#0':
            return
        aid = self.article_tree.identify_row(event.y)
        if aid:
            self._open_article_history(aid)
            return 'break'

    def _open_article_history(self, aid):
        tree = self.article_tree
        parent = tree.parent(aid)
        self.selected_id = parent or aid
        if self.selected_id not in self.rows:
            return
        source = aid.split(':', 1)[1] if parent else self.rows[aid]['source']
        if source == 'change':
            source = default_source(self._platform())
        if source in self._history_sources():
            self.chart_source.set(SOURCES[source])
        tree.selection_set(aid)
        self.select_article()
        self.tabs.select(self._trend_tab)
