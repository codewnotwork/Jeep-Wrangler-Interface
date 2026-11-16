"""Double-click this file to open Wrangler Diagnostics in your web browser."""

import importlib.util
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path.home() / "Documents" / "jeep-data"
sys.path.insert(0, str(HERE / "src"))


def _show_error(text: str) -> None:
    """A .pyw has no window, so report a failed start in a dialog and a file."""
    log = DATA / "last-error.txt"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
    except OSError:
        log = None
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        where = f"\n\nA copy is saved in {log}" if log else ""
        messagebox.showerror("Wrangler Diagnostics could not start", text[-1500:] + where)
        root.destroy()
    except Exception:
        pass


def _auto_update() -> None:
    """Fetch the newest version before loading the app, unless turned off in the app's settings."""
    try:
        settings = json.loads((DATA / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if settings.get("auto_update") is False:
        return
    try:
        # Loaded by path so no other part of the app is in memory while its files are replaced.
        spec = importlib.util.spec_from_file_location("wrangler_updater", HERE / "src" / "wrangler_obd" / "updater.py")
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        updater.check_and_update(HERE, DATA / "update.json")
    except Exception:
        pass  # never let an update problem stop the app from starting


try:
    _auto_update()
    from wrangler_obd.app import main

    main()
except SystemExit:
    raise
except BaseException:
    _show_error(traceback.format_exc())
