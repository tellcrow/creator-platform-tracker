"""Portable application entry point."""
import argparse
import os
from pathlib import Path
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--self-test-report', type=Path)
    args = parser.parse_args()
    base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    data = (args.data_dir or base / 'data').resolve()
    report = args.self_test_report.resolve() if args.self_test_report else None
    if getattr(sys, 'frozen', False):
        os.chdir(base)
        # Relative Tcl paths also work when the Windows user profile is localized.
        bundle = os.path.relpath(sys._MEIPASS, base)
        os.environ['TCL_LIBRARY'] = os.path.join(bundle, '_tcl_data')
        os.environ['TK_LIBRARY'] = os.path.join(bundle, '_tk_data')
    if report:
        from tracker.diagnostics import run_checks
        return run_checks(report)
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / '.instance.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, '同一数据目录已有程序在运行，请使用已打开的窗口。', '创作平台长期数据记录器', 0)
        return 1
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    import tkinter as tk
    from tracker.app import App
    root = tk.Tk()
    app = App(root, data)
    if args.smoke_test:
        root.after(1000, app.close)
    root.mainloop()
    lock.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
