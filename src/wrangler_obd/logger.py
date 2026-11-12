"""CSV logging of live PID readings."""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from typing import Callable, TextIO

from .elm327 import Elm327
from .pids import PIDS


class CsvLogger:
    """Writes one row per sample: timestamp, elapsed seconds, then one column per PID."""

    def __init__(self, fh: TextIO, keys: list[str]) -> None:
        self.keys = keys
        self._writer = csv.writer(fh)
        self._fh = fh
        self._writer.writerow(["timestamp", "elapsed_s"] + [f"{k} ({PIDS[k].unit})" for k in keys])
        self._start: float | None = None

    def write(self, values: dict[str, float], now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self._start is None:
            self._start = now
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="milliseconds")
        row = [stamp, f"{now - self._start:.3f}"]
        row += [f"{values[k]:.3f}" if k in values else "" for k in self.keys]
        self._writer.writerow(row)
        self._fh.flush()


def record(
    elm: Elm327,
    logger: CsvLogger,
    interval: float = 0.5,
    duration: float | None = None,
    on_sample: Callable[[dict[str, float]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll the logger's PIDs until ``duration`` elapses (or forever). Returns sample count."""
    start = time.monotonic()
    samples = 0
    while duration is None or time.monotonic() - start < duration:
        tick = time.monotonic()
        values = {k: r.value for k, r in elm.read_pids(logger.keys).items()}
        logger.write(values)
        samples += 1
        if on_sample:
            on_sample(values)
        sleep(max(0.0, interval - (time.monotonic() - tick)))
    return samples
