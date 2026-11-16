"""One line per drive, so the same commute can be compared over weeks.

``drive_summary`` reduces a recording to the numbers worth tracking from
drive to drive: estimated mpg, how long the engine took to warm up and how
much fuel went into that, idle quality and the fuel corrections at idle and
at cruise. ``history`` collects those lines from every saved recording.

Fuel figures are the speed-density estimate (see ``economy.py``): the
absolute mpg can be off, but drives compare fairly with each other.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Mapping, Sequence

from . import economy, troubleshoot

Sample = tuple[float, Mapping[str, float]]

WARM_C = 80.0          #: "warmed up": the PCM's fuel and idle settle from here
STARTED_WARM_C = 70.0  #: a recording that starts above this missed the warm-up
L_PER_GALLON = 3.785411784
KM_PER_MILE = 1.609344
MIN_DRIVE_S = 120.0


def _litres(samples: Sequence[Sample], fuel_factor: float = 1.0) -> float:
    total = 0.0
    for (t0, v0), (t1, _) in zip(samples, samples[1:]):
        dt = t1 - t0
        if not 0 < dt <= 30 or v0.get("rpm", 0) < 100:
            continue
        flow = economy.fuel_flow_lph(v0, fuel_factor)
        if flow is not None:
            total += flow * dt / 3600.0
    return total


def _mean_trim(rows: Sequence[Mapping[str, float]], kind: str) -> float | None:
    vals = [r[f"{kind}_{b}"] for r in rows for b in ("b1", "b2") if f"{kind}_{b}" in r]
    return round(statistics.fmean(vals), 1) if vals else None


def drive_summary(samples: Sequence[Sample], speed_factor: float = 1.0, fuel_factor: float = 1.0) -> dict:
    """The numbers to compare between drives. Fields are None when the data is missing."""
    samples = list(samples)
    out: dict = {"duration_min": None, "miles": None, "mpg_est": None, "gallons_est": None,
                 "start_coolant_f": None, "warmup_min": None, "warmup_gallons_est": None, "warmup_fuel_pct": None,
                 "idle_ltft": None, "cruise_ltft": None, "idle_rpm_stdev": None}
    if len(samples) < 2:
        return out
    out["duration_min"] = round((samples[-1][0] - samples[0][0]) / 60, 1)
    eco = economy.summarize(samples, speed_factor, fuel_factor)
    if eco:
        out["miles"], out["mpg_est"] = eco.get("distance_miles"), eco.get("mpg")
        out["gallons_est"] = eco.get("fuel_gallons")

    coolant = [(t, v["coolant_temp"]) for t, v in samples if "coolant_temp" in v]
    if coolant:
        start = coolant[0][1]
        out["start_coolant_f"] = round(start * 9 / 5 + 32)
        if start < STARTED_WARM_C:
            warm_at = next((t for t, c in coolant if c >= WARM_C), None)
            if warm_at is not None:
                t0 = samples[0][0]
                out["warmup_min"] = round((warm_at - t0) / 60, 1)
                warm_l = _litres([s for s in samples if s[0] <= warm_at], fuel_factor)
                total_l = _litres(samples, fuel_factor)
                out["warmup_gallons_est"] = round(warm_l / L_PER_GALLON, 3)
                out["warmup_fuel_pct"] = round(100 * warm_l / total_l) if total_l > 0 else None

    rest = troubleshoot.rest_levels([v for _, v in samples])
    idle = [v for _, v in troubleshoot.settled_idle(samples, rest)]
    cruise = [v for _, v in troubleshoot.steady_cruise(samples, rest)]
    out["idle_ltft"], out["cruise_ltft"] = _mean_trim(idle, "ltft"), _mean_trim(cruise, "ltft")
    rpm = [v["rpm"] for v in idle if "rpm" in v]
    if len(rpm) >= 10:
        out["idle_rpm_stdev"] = round(statistics.pstdev(rpm), 1)
    return out


def read_log(path: Path) -> list[Sample]:
    """Read a log.csv written by the monitor or the logger back into samples (metric)."""
    samples: list[Sample] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or header[:2] != ["timestamp", "elapsed_s"]:
            return []
        keys = [h.split(" (")[0] for h in header[2:]]
        for row in reader:
            try:
                t = float(row[1])
            except (IndexError, ValueError):
                continue
            values = {}
            for key, cell in zip(keys, row[2:]):
                if cell:
                    try:
                        values[key] = float(cell)
                    except ValueError:
                        pass
            samples.append((t, values))
    return samples


def history(data_dir: Path) -> list[dict]:
    """One summary per saved recording, oldest first.

    Uses the ``drive`` block saved in each report.json; older recordings
    without one are worked out from their log.csv once and cached.
    """
    data_dir = Path(data_dir)
    cache_file = data_dir / "history-cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    changed = False
    out = []
    for folder in sorted(p for p in data_dir.iterdir() if p.is_dir()) if data_dir.exists() else []:
        log = folder / "log.csv"
        if not log.exists() or not (folder.name.endswith(" monitor") or folder.name.endswith(" drive")):
            continue
        summary = None
        try:
            doc = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            summary = doc.get("drive")
            notes = doc.get("owner_notes") or ""
        except (OSError, ValueError):
            notes = ""
        if summary is None:
            key = f"{folder.name}|{log.stat().st_mtime_ns}"
            if key not in cache:
                cache[key] = drive_summary(read_log(log))
                changed = True
            summary = cache[key]
        if summary.get("duration_min") is None or summary["duration_min"] * 60 < MIN_DRIVE_S:
            continue
        out.append({"folder": folder.name, "when": folder.name[:15], "notes": notes[:120], **summary})
    if changed:
        try:
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return out
