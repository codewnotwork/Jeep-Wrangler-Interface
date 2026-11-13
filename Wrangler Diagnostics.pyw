"""Double-click this file to open Wrangler Diagnostics in your web browser."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wrangler_obd.app import main  # noqa: E402

main()
