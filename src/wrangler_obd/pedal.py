"""Pedal map: how far the throttle opens for a given pedal position.

The JK is drive-by-wire. The pedal has two position sensors and the PCM
decides how far to open the throttle from them. A "throttle controller"
box changes the pedal signal before the PCM sees it, so the same foot
movement looks like a bigger one. This records the stock relationship, so
any change (a controller box, a tune) can be measured against it.
"""

from __future__ import annotations

import statistics
from typing import Mapping, Sequence

PEDAL_PIDS = ["pedal_d", "pedal_e", "throttle_commanded", "throttle", "throttle_relative", "rpm", "speed"]
BIN = 5.0  #: pedal travel per row of the table, in percent


def pedal_map(samples: Sequence[Mapping[str, float]]) -> dict:
    """Bin samples by pedal travel and average the throttle readings in each bin.

    Pedal travel is rescaled so the lowest value seen is 0% and the highest
    is 100%, because the raw sensor does not read 0 at rest.
    """
    rows = [s for s in samples if "pedal_d" in s]
    out: dict = {"samples": len(rows)}
    if not rows:
        out["note"] = "The PCM did not report accelerator pedal position."
        return out
    throttle_key = next((k for k in ("throttle_commanded", "throttle") if any(k in s for s in rows)), None)
    lo, hi = min(s["pedal_d"] for s in rows), max(s["pedal_d"] for s in rows)
    out["pedal_sensor_range"] = {"rest": round(lo, 1), "floor": round(hi, 1)}
    if hi - lo < 20 or throttle_key is None:
        out["note"] = "The pedal barely moved, so there is no curve to report. Press it slowly to the floor during the capture."
        return out
    out["throttle_reading"] = throttle_key
    bins: dict[int, list[float]] = {}
    for s in rows:
        if throttle_key in s:
            travel = (s["pedal_d"] - lo) / (hi - lo) * 100
            bins.setdefault(min(int(travel // BIN), int(100 // BIN) - 1), []).append(s[throttle_key])
    t_lo = min(min(v) for v in bins.values())
    t_hi = max(max(v) for v in bins.values())
    curve = []
    for b in sorted(bins):
        mean = statistics.fmean(bins[b])
        curve.append({
            "pedal_travel_pct": f"{b * BIN:.0f}-{(b + 1) * BIN:.0f}",
            "throttle_pct": round(mean, 1),
            "throttle_of_range_pct": round((mean - t_lo) / (t_hi - t_lo) * 100, 1) if t_hi > t_lo else None,
            "samples": len(bins[b]),
        })
    out["curve"] = curve
    return out


def to_markdown(m: dict, notes: str = "") -> str:
    out = ["# Pedal map", ""]
    if notes:
        out += [notes, ""]
    if "curve" not in m:
        return "\n".join(out + [m.get("note", "No data.")]) + "\n"
    r = m["pedal_sensor_range"]
    out += [
        f"{m['samples']} samples. Pedal sensor read {r['rest']}% at rest and {r['floor']}% at the floor. "
        f"Throttle column is `{m['throttle_reading']}`.",
        "",
        "| Pedal travel (%) | Throttle (%) | Throttle, share of its range (%) | Samples |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {c['pedal_travel_pct']} | {c['throttle_pct']} | {c['throttle_of_range_pct']} | {c['samples']} |"
        for c in m["curve"]
    ]
    return "\n".join(out) + "\n"
