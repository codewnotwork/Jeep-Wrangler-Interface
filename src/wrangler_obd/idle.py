"""Idle check: catch a stumble that is too small to see on the tach.

Samples a small set of fast-moving readings as quickly as the adapter
allows while the engine idles, finds brief RPM dips, and records what the
fuel trims, O2 sensors, manifold pressure, spark timing and system voltage
were doing at each dip. It also reads the PCM's own per-cylinder misfire
counters (Mode 06) before and after, which is the most direct evidence of
which cylinder is responsible.
"""

from __future__ import annotations

import statistics
import time
from typing import Callable, Sequence

from .elm327 import Elm327
from .logger import CsvLogger
from .pids import PIDS

#: Read on every pass, so a short dip is not missed.
FAST_PIDS = ["rpm", "stft_b1", "stft_b2", "o2_b1s1", "o2_b2s1", "map", "timing_advance", "module_voltage"]
#: Slow-moving context, read on every SLOW_EVERY-th pass to keep the sample rate up.
SLOW_PIDS = ["engine_load", "ltft_b1", "ltft_b2", "coolant_temp", "throttle", "speed"]
SLOW_EVERY = 10
IDLE_PIDS = FAST_PIDS + SLOW_PIDS

DIP_RPM = 25.0  #: a sample this far below the local median counts as a dip
WINDOW_S = 10.0  #: width of the window used for the local median
CONTEXT = ("stft_b1", "stft_b2", "o2_b1s1", "o2_b2s1", "map", "timing_advance", "module_voltage")

Sample = tuple[float, dict[str, float]]


def misfire_counts(elm: Elm327) -> dict[str, float]:
    """Current-drive-cycle misfire count per cylinder from Mode 06 (TID 0C)."""
    return {
        t.monitor: t.value
        for t in elm.test_results()
        if 0xA1 <= t.mid <= 0xAF and t.tid == 0x0C
    }


def capture(
    elm: Elm327,
    duration: float,
    logger: CsvLogger | None = None,
    keys: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    min_interval: float = 0.02,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Sample]:
    """Sample ``keys`` as fast as the adapter allows for ``duration`` seconds.

    ``min_interval`` caps the rate (50 per second by default), which only
    matters for the simulator; a real adapter is slower than that.
    """
    supported = elm.supported_pids()
    keys = [k for k in (keys or IDLE_PIDS) if PIDS[k].pid in supported]
    samples: list[Sample] = []
    elm.fast = True
    try:
        start = clock()
        while clock() - start < duration:
            t = clock() - start
            wanted = keys if len(samples) % SLOW_EVERY == 0 else [k for k in keys if k in FAST_PIDS]
            values = {k: r.value for k, r in elm.read_pids(wanted).items()}
            samples.append((t, values))
            if logger:
                logger.write(values)
            spare = min_interval - (clock() - start - t)
            if spare > 0:
                sleep(spare)
    finally:
        elm.fast = False
    return samples


def _median_near(samples: Sequence[Sample], i: int, key: str) -> float | None:
    """Median of ``key`` over the samples within WINDOW_S / 2 of sample ``i``."""
    t = samples[i][0]
    vals = []
    for j in range(i, -1, -1):
        if t - samples[j][0] > WINDOW_S / 2:
            break
        if key in samples[j][1]:
            vals.append(samples[j][1][key])
    for j in range(i + 1, len(samples)):
        if samples[j][0] - t > WINDOW_S / 2:
            break
        if key in samples[j][1]:
            vals.append(samples[j][1][key])
    return statistics.median(vals) if vals else None


def analyze(
    samples: Sequence[Sample],
    misfires_before: dict[str, float] | None = None,
    misfires_after: dict[str, float] | None = None,
    baro_kpa: float | None = None,
) -> dict:
    """Summarise an idle capture. See the module docstring."""
    rows = [(t, v) for t, v in samples if "rpm" in v]
    out: dict = {"samples": len(rows)}
    if len(rows) < 5:
        out["verdict"] = ["Too few samples to analyse."]
        return out
    duration = rows[-1][0] - rows[0][0]
    rpm = [v["rpm"] for _, v in rows]
    out["duration_s"] = round(duration, 1)
    out["sample_rate_hz"] = round(len(rows) / duration, 1) if duration > 0 else None
    out["rpm"] = {
        "mean": round(statistics.fmean(rpm), 1), "stdev": round(statistics.pstdev(rpm), 1),
        "min": round(min(rpm), 1), "max": round(max(rpm), 1),
    }
    conditions = {}
    for key in ("coolant_temp", "speed", "throttle", "map", "module_voltage", "ltft_b1", "ltft_b2", "stft_b1", "stft_b2"):
        vals = [v[key] for _, v in rows if key in v]
        if vals:
            conditions[key] = {
                "mean": round(statistics.fmean(vals), 2), "min": round(min(vals), 2), "max": round(max(vals), 2),
            }
    out["conditions"] = conditions

    # Dips: runs of samples well below the local median RPM.
    events: list[dict] = []
    current: dict | None = None
    for i, (t, v) in enumerate(rows):
        med = _median_near(rows, i, "rpm")
        drop = med - v["rpm"]
        if drop >= DIP_RPM:
            if current is None:
                current = {"t_s": round(t, 2), "drop_rpm": 0.0, "end": t, "context": {}}
                events.append(current)
            current["end"] = t
            if drop > current["drop_rpm"]:
                current["drop_rpm"] = round(drop, 1)
                # Deviation of each reading from its own local median at the deepest point.
                current["context"] = {
                    k: round(v[k] - m, 3) for k in CONTEXT if k in v and (m := _median_near(rows, i, k)) is not None
                }
        else:
            current = None
    for e in events:
        e["length_s"] = round(e.pop("end") - e["t_s"], 2)
    out["dips"] = {
        "threshold_rpm": DIP_RPM,
        "count": len(events),
        "per_minute": round(len(events) * 60 / duration, 1) if duration > 0 else None,
        "events": events[:40],
    }
    if len(events) >= 3:
        gaps = [b["t_s"] - a["t_s"] for a, b in zip(events, events[1:])]
        out["dips"]["spacing_s"] = {"mean": round(statistics.fmean(gaps), 1), "stdev": round(statistics.pstdev(gaps), 1)}
        out["dips"]["mean_change_at_dip"] = {
            k: round(statistics.fmean(e["context"][k] for e in events if k in e["context"]), 3)
            for k in CONTEXT if any(k in e["context"] for e in events)
        }

    if misfires_before is not None and misfires_after is not None:
        out["misfire_counters"] = {
            name: {"before": misfires_before.get(name), "after": after,
                   "new": None if misfires_before.get(name) is None else after - misfires_before[name]}
            for name, after in misfires_after.items()
        }

    out["verdict"] = _verdict(out, baro_kpa)
    return out


def _verdict(a: dict, baro_kpa: float | None) -> list[str]:
    """Plain-language observations. These are leads, not a diagnosis."""
    notes: list[str] = []
    cond = a["conditions"]
    if cond.get("coolant_temp", {}).get("mean", 100) < 80:
        notes.append("Engine was not fully warm (coolant under 80 °C); repeat warm for a fair comparison.")
    if cond.get("speed", {}).get("max", 0) > 0:
        notes.append("The truck was moving for part of the capture; idle results are less reliable.")

    dips = a["dips"]
    if dips["count"] == 0:
        notes.append(
            f"No RPM dips of {DIP_RPM:.0f} rpm or more were seen. RPM varied by ±{a['rpm']['stdev']} rpm (standard deviation)."
        )
    else:
        notes.append(f"{dips['count']} RPM dip(s) seen, about {dips['per_minute']} per minute.")
        spacing = dips.get("spacing_s")
        if spacing and spacing["mean"] > 0 and spacing["stdev"] / spacing["mean"] < 0.25:
            notes.append(
                f"Dips are evenly spaced, about every {spacing['mean']} s. A regular rhythm suggests something "
                "cycling (EVAP purge, A/C compressor, cooling fan, alternator load) more than a random misfire."
            )
        change = dips.get("mean_change_at_dip", {})
        volts = change.get("module_voltage")
        if volts is not None and volts <= -0.3:
            notes.append(f"System voltage sags {abs(volts):.2f} V at the dips: look at electrical load and charging.")
        b1, b2 = change.get("o2_b1s1"), change.get("o2_b2s1")
        if b1 is not None and b2 is not None and abs(b1 - b2) >= 0.15:
            bank = "1 (cylinders 1, 3, 5)" if b1 < b2 else "2 (cylinders 2, 4, 6)"
            notes.append(f"The upstream O2 sensor on bank {bank} reads leaner at the dips, pointing at that bank.")

    for name, c in a.get("misfire_counters", {}).items():
        if c["new"]:
            notes.append(f"{name}: the PCM counted {c['new']:.0f} new misfire(s) during the capture.")
    if a.get("misfire_counters") and not any(c["new"] for c in a["misfire_counters"].values()):
        notes.append("The PCM's misfire counters did not move during the capture.")

    for bank in ("b1", "b2"):
        if f"ltft_{bank}" in cond:
            total = cond[f"ltft_{bank}"]["mean"] + cond.get(f"stft_{bank}", {"mean": 0})["mean"]
            if abs(total) >= 8:
                side = "lean (possible vacuum leak if cruise trims are normal)" if total > 0 else "rich"
                notes.append(f"Bank {bank[1]} idle fuel trim totals {total:+.1f}%: running {side}.")
    if "ltft_b1" in cond and "ltft_b2" in cond and abs(cond["ltft_b1"]["mean"] - cond["ltft_b2"]["mean"]) >= 6:
        notes.append("Long term fuel trims differ between banks by 6% or more, so the cause is likely on one bank.")
    if baro_kpa and "map" in cond and cond["map"]["mean"] / baro_kpa > 0.45:
        notes.append(
            f"Idle manifold pressure is {cond['map']['mean']:.0f} kPa against {baro_kpa:.0f} kPa barometric, "
            "which is weak vacuum for a warm idle in Park."
        )
    return notes


def to_markdown(a: dict) -> list[str]:
    """Markdown lines for the idle section of the report."""
    out = ["## Idle check", ""]
    if "rpm" not in a:
        return out + a["verdict"]
    out.append(
        f"{a['samples']} samples over {a['duration_s']} s ({a['sample_rate_hz']} per second). "
        f"RPM mean {a['rpm']['mean']}, standard deviation {a['rpm']['stdev']}, range {a['rpm']['min']} to {a['rpm']['max']}."
    )
    out += ["", "### Observations", ""] + [f"- {n}" for n in a["verdict"]]
    if a.get("misfire_counters"):
        out += ["", "### PCM misfire counters (Mode 06)", "", "| Monitor | Before | After | New |", "|---|---|---|---|"]
        out += [f"| {n} | {c['before']} | {c['after']} | {c['new']} |" for n, c in a["misfire_counters"].items()]
    events = a["dips"]["events"]
    if events:
        keys = [k for k in CONTEXT if any(k in e["context"] for e in events)]
        out += ["", "### RPM dips", "", "Other columns are the change from that reading's local median at the dip.", ""]
        out.append("| Time (s) | Drop (rpm) | Length (s) | " + " | ".join(keys) + " |")
        out.append("|" + "---|" * (3 + len(keys)))
        for e in events:
            ctx = " | ".join(f"{e['context'].get(k, ''):+.2f}" if k in e["context"] else "" for k in keys)
            out.append(f"| {e['t_s']} | {e['drop_rpm']} | {e['length_s']} | {ctx} |")
    out += ["", "### Conditions during the capture", "", "| Reading | Mean | Min | Max |", "|---|---|---|---|"]
    out += [f"| {PIDS[k].name} ({PIDS[k].unit}) | {s['mean']} | {s['min']} | {s['max']} |" for k, s in a["conditions"].items()]
    return out
