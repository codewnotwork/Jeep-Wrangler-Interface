"""Double-click this file to open Wrangler Diagnostics in your web browser."""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def _show_error(text: str) -> None:
    """A .pyw has no window, so report a failed start in a dialog and a file."""
    log = Path.home() / "Documents" / "jeep-data" / "last-error.txt"
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


try:
    from wrangler_obd.app import main

    main()
except SystemExit:
    raise
except BaseException:
    _show_error(traceback.format_exc())
