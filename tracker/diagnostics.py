"""Offline release checks; never opens a browser or visits an account."""
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import tempfile

def run_checks(report_path):
    results = {'platform': platform.platform(), 'checks': {}, 'online_account_access': False}
    def check(name, operation):
        try:
            value = operation()
            results['checks'][name] = {'ok': True, 'detail': value}
        except Exception as exc:
            results['checks'][name] = {'ok': False, 'detail': type(exc).__name__ + ': ' + str(exc)[:500]}
    with tempfile.TemporaryDirectory(prefix='wechat-release-check-') as temp:
        folder = Path(temp)
        def data_check():
            from .store import Store
            from .export import export_excel
            from openpyxl import load_workbook
            source = Store(folder / 'one.sqlite')
            target = Store(folder / 'two.sqlite')
            try:
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                rows = [{'article_key': '2247483000_1', 'title': '打包自检文章',
                    'observed_at': (start+timedelta(days=i)).isoformat(), 'source': 'publish',
                    'time_basis': 'live', 'metrics': {'read': 100+i*10, 'donation': None}}
                    for i in range(10)]
                source.add_observations('离线自检', rows)
                assert source.db.execute("SELECT name FROM sqlite_master WHERE name='forecasts'").fetchone() is None
                assert 'forecasts' not in source.backup_data()['accounts'][0]
                plain = folder / 'backup.json'
                compressed = folder / 'backup.json.gz'
                source.export_backup(plain)
                source.export_backup(compressed)
                assert json.loads(plain.read_bytes())['version'] == 4
                with compressed.open('rb') as backup_file:
                    assert backup_file.read(2) == b'\x1f\x8b'
                assert target.import_backup(plain)['inserted'] == 10
                assert target.import_backup(compressed)['inserted'] == 0
                export_excel(target,'离线自检',folder/'export.xlsx')
                workbook = load_workbook(folder/'export.xlsx')
                assert workbook['历史采集明细'].max_row == 11
                assert workbook.sheetnames == ['文章最新数据', '历史采集明细', '口径说明']
                workbook.close()
                return 'SQLite / JSON v4 / gzip v4 / Excel passed; prediction module absent'
            finally:
                source.close(); target.close()
        def tkinter_check():
            import tkinter as tk
            root = tk.Tk(); root.withdraw()
            try:
                return root.tk.call('info', 'patchlevel')
            finally:
                root.destroy()
        def social_check():
            from .store import Store
            from .schema import primary_metric
            from .social_parser import parse_snapshot
            from .social_collector import SocialCollector
            from .export import export_excel
            from openpyxl import load_workbook
            source = Store(folder / 'social.sqlite')
            target = Store(folder / 'social-restored.sqlite')
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            try:
                for name, key in [('xiaohongshu', 'a' * 24), ('douyin', '1234567890123')]:
                    metric = primary_metric(name)
                    for i in range(10):
                        raw = {'cards': [{'title': '离线模拟作品', 'publish_time': '2026-01-01 08:00',
                            'stable_id': key, 'metrics': {metric: str(100 + i * 10), 'likes': '0'}}],
                            'account_names': ['同名离线账号'], 'total': 1, 'filters_default': True}
                        parsed = parse_snapshot(raw, name, observed_at=(start + timedelta(days=i)).isoformat())
                        assert len(parsed['observations']) == 1
                        source.add_observations('同名离线账号', parsed['observations'], platform=name)
                    export_excel(source, '同名离线账号', folder / (name + '.xlsx'), platform=name)
                    book = load_workbook(folder / (name + '.xlsx'))
                    assert book['历史采集明细'].max_row == 11
                    book.close()
                source.export_backup(folder / 'social.json')
                assert target.import_backup(folder / 'social.json')['inserted'] == 20
                assert len(target.articles('同名离线账号', platform='douyin')) == 1
                assert len(target.articles('同名离线账号', platform='xiaohongshu')) == 1
                assert target.accounts() == []
                return 'Two social platforms: parser / account isolation / JSON / Excel passed'
            finally:
                source.close(); target.close()
        def driver_check():
            from playwright.sync_api import sync_playwright
            with sync_playwright() as driver:
                assert driver.chromium.name == 'chromium'
            return 'Playwright local driver starts/stops; no browser launched'
        def schedule_check():
            from .store import Store
            from .schema import PLATFORMS
            source = Store(folder / 'schedules.sqlite')
            target = Store(folder / 'schedules-restored.sqlite')
            try:
                for hours, name in enumerate(PLATFORMS, start=1):
                    source.save_platform_schedule(name, {
                        'account': '离线计划账号',
                        'config': {'mode': 'interval', 'platform': name, 'hours': hours},
                        'next': datetime.now(timezone.utc).isoformat(), 'blocked': False,
                    })
                plans = source.platform_schedules()
                assert len(plans) == 3
                assert [plans[name]['config']['hours'] for name in PLATFORMS] == [1, 2, 3]
                source.set_platform_schedule_blocked('xiaohongshu', '离线计划账号', True)
                assert source.platform_schedules()['xiaohongshu']['blocked']
                assert not source.platform_schedules()['douyin']['blocked']
                source.export_backup(folder / 'schedules.json')
                target.import_backup(folder / 'schedules.json')
                restored = target.platform_schedules()
                assert len(restored) == 3
                for name in PLATFORMS:
                    assert restored[name]['config']['mode'] == 'off'
                    assert restored[name]['config']['hours'] == plans[name]['config']['hours']
                    assert restored[name]['next'] is None and not restored[name]['blocked']
                return 'Three independent plans / failure isolation / backup restores disabled passed'
            finally:
                source.close(); target.close()
        check('data_roundtrip', data_check)
        check('social_roundtrip', social_check)
        check('platform_schedules', schedule_check)
        check('tkinter', tkinter_check)
        check('playwright_driver', driver_check)
    results['ok'] = all(item['ok'] for item in results['checks'].values())
    Path(report_path).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if results['ok'] else 1
