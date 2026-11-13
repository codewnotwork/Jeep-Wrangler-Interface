"""Find manufacturer data items by asking a module for every identifier.

Chrysler does not publish where readings such as transmission fluid
temperature live. This asks one module (engine or transmission) for each
identifier in a range, using read-only services 0x21 and 0x22, and saves
whatever answers. Scan once cold and once hot, then ``compare`` the two
files: the items that changed, and by how much, show which one is the
temperature.

Only run a scan parked, key on, engine off.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

from .elm327 import ENGINE, TRANSMISSION, Elm327

MODULES = {"engine": ENGINE, "trans": TRANSMISSION}
CONTEXT_PIDS = ["coolant_temp", "intake_temp", "ambient_temp", "rpm"]


def scan(
    elm: Elm327,
    module: str,
    service: int,
    start: int,
    end: int,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, str]:
    """Ask ``module`` for identifiers ``start``..``end``. Returns {"22:1234": "0A1B", ...}."""
    found: dict[str, str] = {}
    width = 2 if service == 0x21 else 4
    elm.fast = True
    try:
        with elm.ecu(MODULES[module]):
            for n, ident in enumerate(range(start, end + 1), 1):
                data = elm.read_data(service, ident)
                if data is not None:
                    found[f"{service:02X}:{ident:0{width}X}"] = bytes(data).hex().upper()
                if progress and (n % 64 == 0 or ident == end):
                    progress(n, end - start + 1, len(found))
    finally:
        elm.fast = False
    return found


def run(
    elm: Elm327,
    module: str,
    ranges: list[tuple[int, int, int]],
    label: str = "",
    progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    """Scan several (service, start, end) ranges and wrap the result with context."""
    context = {k: round(r.value, 1) for k, r in elm.read_pids(CONTEXT_PIDS).items()}
    started = time.monotonic()
    items: dict[str, str] = {}
    complete = True
    try:
        for service, start, end in ranges:
            items.update(scan(elm, module, service, start, end, progress))
    except KeyboardInterrupt:
        complete = False
    return {
        "schema": "wrangler-obd-scan/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": module,
        "label": label,
        "ranges": [f"{s:02X}:{a:X}-{b:X}" for s, a, b in ranges],
        "complete": complete,
        "seconds": round(time.monotonic() - started, 1),
        "engine_readings_at_start": context,
        "items": items,
    }


def compare(old: dict, new: dict) -> list[dict]:
    """Items present in both scans whose bytes differ, with simple numeric readings of each."""
    changed = []
    for key in sorted(set(old["items"]) & set(new["items"])):
        a, b = old["items"][key], new["items"][key]
        if a != b:
            row = {"item": key, "before": a, "after": b}
            if len(a) == len(b) and len(a) <= 8:
                row["before_number"], row["after_number"] = int(a, 16), int(b, 16)
            changed.append(row)
    return changed


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
