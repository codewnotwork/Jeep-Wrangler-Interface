"""Live troubleshooting: turn combinations of readings into likely causes.

``evaluate`` looks at a window of recent samples and returns a ranked list
of suspected causes, each with the evidence that triggered it and the next
thing to check. The rules look at patterns across readings and operating
conditions (warm idle versus steady cruise), not at single values, because
that is what separates, say, a vacuum leak from a weak fuel pump.

These are leads for a person to confirm, not a verdict. The truck has no
fuel pressure or air flow sensor, so several causes can only be narrowed
down from here; the ``next_check`` says how to confirm each one.

All inputs are metric (see ``pids.py``).
"""

from __future__ import annotations

import statistics
from typing import Mapping, Sequence

Sample = tuple[float, Mapping[str, float]]

WARM_C = 80.0
TRIM_LIMIT = 8.0       #: total (short + long) fuel trim, percent
BANK_SPLIT = 6.0       #: difference between banks, percent
MIN_SECONDS = 20.0     #: time in a condition before its rules are trusted


def _mean(rows: Sequence[Mapping[str, float]], key: str) -> float | None:
    vals = [r[key] for r in rows if key in r]
    return statistics.fmean(vals) if vals else None


def _seconds(samples: Sequence[Sample], pick) -> float:
    total = 0.0
    for (t0, v0), (t1, _) in zip(samples, samples[1:]):
        if pick(v0) and 0 < t1 - t0 < 30:
            total += t1 - t0
    return total


def _is_idle(v: Mapping[str, float]) -> bool:
    return v.get("coolant_temp", 0) >= WARM_C and v.get("speed", 1) < 1 and 400 < v.get("rpm", 0) < 1000


def _is_cruise(v: Mapping[str, float]) -> bool:
    return v.get("coolant_temp", 0) >= WARM_C and v.get("speed", 0) >= 40 and v.get("throttle", 0) < 40


def _total_trim(rows: Sequence[Mapping[str, float]], bank: str) -> float | None:
    ltft = _mean(rows, f"ltft_{bank}")
    if ltft is None:
        return None
    return ltft + (_mean(rows, f"stft_{bank}") or 0.0)


def _cause(ident: str, title: str, score: int, evidence: list[str], next_check: str) -> dict:
    return {"id": ident, "title": title, "score": score, "evidence": evidence, "next_check": next_check}


def evaluate(samples: Sequence[Sample], stored_codes: Sequence[str] = (), pending_codes: Sequence[str] = ()) -> dict:
    """Return ``{"causes": [...], "coverage": {...}, "notes": [...]}`` for a window of samples."""
    rows = [v for _, v in samples]
    idle = [v for v in rows if _is_idle(v)]
    cruise = [v for v in rows if _is_cruise(v)]
    running = [v for v in rows if v.get("rpm", 0) > 400]
    idle_s, cruise_s = _seconds(samples, _is_idle), _seconds(samples, _is_cruise)
    have_idle, have_cruise = idle_s >= MIN_SECONDS, cruise_s >= MIN_SECONDS
    codes = set(stored_codes) | set(pending_codes)
    causes: list[dict] = []

    # -- fuel trims ---------------------------------------------------------
    trims = {
        seg: {b: _total_trim(seg_rows, b) for b in ("b1", "b2")}
        for seg, seg_rows, ok in (("idle", idle, have_idle), ("cruise", cruise, have_cruise)) if ok
    }

    def both(seg: str, test) -> bool:
        vals = [v for v in trims.get(seg, {}).values() if v is not None]
        return bool(vals) and all(test(v) for v in vals)

    def describe(seg: str) -> str:
        t = trims[seg]
        parts = [f"bank {b[1]} {t[b]:+.1f}%" for b in ("b1", "b2") if t[b] is not None]
        return f"Total fuel trim at {seg}: " + ", ".join(parts)

    lean_codes = sorted(codes & {"P0171", "P0174"})
    lean_idle, lean_cruise = both("idle", lambda v: v >= TRIM_LIMIT), both("cruise", lambda v: v >= TRIM_LIMIT)
    if lean_idle and "cruise" in trims and both("cruise", lambda v: v < TRIM_LIMIT - 3):
        ev = [describe("idle"), describe("cruise"), "Lean at idle but close to normal at cruise."]
        if lean_codes:
            ev.append(f"Lean code(s) present: {', '.join(lean_codes)}")
        causes.append(_cause(
            "vacuum_leak", "Vacuum leak", 80 + (10 if lean_codes else 0), ev,
            "Listen for hissing with the engine idling. Check the PCV hose, brake booster hose, intake manifold "
            "gaskets and the EVAP purge line. A smoke test finds it for certain.",
        ))
    elif lean_idle and "cruise" not in trims:
        causes.append(_cause(
            "lean_idle", "Running lean at idle (vacuum leak or fuel delivery)", 50, [describe("idle"),
            "No steady cruise data yet to tell the two apart."],
            "Drive at a steady 25 mph or more for a minute with the monitor running, then look again.",
        ))
    if lean_cruise:
        ev = [describe("cruise")] + ([describe("idle")] if "idle" in trims else [])
        ev.append("Lean under load, where a vacuum leak has little effect.")
        if lean_codes:
            ev.append(f"Lean code(s) present: {', '.join(lean_codes)}")
        causes.append(_cause(
            "fuel_delivery", "Low fuel delivery (pump, filter, regulator) or MAP sensor reading low",
            75 + (10 if lean_codes else 0), ev,
            "Measure fuel pressure at the rail with a gauge (about 58 psi on the 3.8L). Compare the MAP reading "
            "with the key on and engine off against barometric pressure: they should match.",
        ))
    for seg in trims:
        b1, b2 = trims[seg]["b1"], trims[seg]["b2"]
        if b1 is not None and b2 is not None and abs(b1 - b2) >= BANK_SPLIT:
            lean = "1 (cylinders 1, 3, 5)" if b1 > b2 else "2 (cylinders 2, 4, 6)"
            causes.append(_cause(
                "bank_split", f"Problem on one side of the engine: bank {lean} is leaner", 70,
                [describe(seg), f"The banks differ by {abs(b1 - b2):.1f}%. Shared causes affect both banks equally."],
                "On that bank, check for an intake gasket leak, a restricted injector, an exhaust leak ahead of the "
                "upstream O2 sensor, or a biased upstream O2 sensor.",
            ))
            break
    rich_segs = [seg for seg in trims if both(seg, lambda v: v <= -TRIM_LIMIT)]
    if rich_segs:
        causes.append(_cause(
            "rich", "Running rich (leaking injector, purge valve stuck open, high fuel pressure)", 65,
            [describe(seg) for seg in rich_segs] + ["The PCM is pulling fuel out to compensate."],
            "Pinch off or unplug the EVAP purge line and see if trims recover. Check for a fuel smell in the oil "
            "and for a dripping injector.",
        ))

    # -- cooling --------------------------------------------------------------
    coolant = [v["coolant_temp"] for v in rows if "coolant_temp" in v]
    run_time = max((v["run_time"] for v in rows if "run_time" in v), default=0)
    if coolant:
        if max(coolant) >= 110:
            causes.append(_cause(
                "overheat", "Engine running hot", 95 if max(coolant) >= 118 else 80,
                [f"Coolant reached {max(coolant):.0f} °C ({max(coolant) * 9 / 5 + 32:.0f} °F). Normal is about 90-105 °C."],
                "Check coolant level (engine cold), the fan clutch or electric fan, and for a radiator blocked with mud.",
            ))
        recent = coolant[-max(1, len(coolant) // 5):]
        if run_time >= 900 and max(recent) < 78:
            causes.append(_cause(
                "thermostat", "Thermostat stuck open", 75,
                [f"After {run_time / 60:.0f} minutes running, coolant is only {max(recent):.0f} °C "
                 f"({max(recent) * 9 / 5 + 32:.0f} °F). It should settle near 90 °C (195 °F)."],
                "Replace the thermostat. A cold-running engine stays in warm-up fueling and uses more fuel.",
            ))

    # -- charging -------------------------------------------------------------
    volts = _mean(running, "module_voltage")
    if volts is not None and len(running) >= 10:
        if volts < 13.2:
            causes.append(_cause(
                "charging_low", "Charging system weak (alternator, belt, or connections)", 85 if volts < 12.8 else 65,
                [f"System voltage averages {volts:.1f} V with the engine running. Expected 13.5-14.8 V."],
                "Check the belt and battery terminals, then test alternator output with a meter at the battery.",
            ))
        elif volts > 15.1:
            causes.append(_cause(
                "charging_high", "Overcharging (voltage regulation)", 80,
                [f"System voltage averages {volts:.1f} V with the engine running."],
                "On this truck the PCM regulates the alternator: check the battery temperature sensor and grounds.",
            ))

    # -- oxygen sensors and catalyst -----------------------------------------
    closed = idle + cruise
    for key, name in (("o2_b1s1", "bank 1"), ("o2_b2s1", "bank 2")):
        vals = [v[key] for v in closed if key in v]
        if len(vals) >= 40 and (have_idle or have_cruise) and max(vals) - min(vals) < 0.3:
            stuck = "lean" if statistics.fmean(vals) < 0.45 else "rich"
            causes.append(_cause(
                f"o2_lazy_{key}", f"Upstream O2 sensor on {name} is not switching", 60,
                [f"It stayed between {min(vals):.2f} and {max(vals):.2f} V over {len(vals)} warm samples, stuck {stuck}. "
                 "A healthy sensor swings between about 0.1 and 0.9 V."],
                "If fuel trims on that bank are also far off, fix the mixture first. Otherwise suspect the sensor "
                "or its wiring.",
            ))
    if have_cruise:
        for up, down, name in (("o2_b1s1", "o2_b1s2", "bank 1"), ("o2_b2s1", "o2_b2s2", "bank 2")):
            u = [v[up] for v in cruise if up in v]
            d = [v[down] for v in cruise if down in v]
            if len(d) >= 40 and len(u) >= 40 and max(u) - min(u) >= 0.4 and max(d) - min(d) >= 0.5:
                code = {"bank 1": "P0420", "bank 2": "P0430"}[name]
                causes.append(_cause(
                    f"catalyst_{down}", f"Catalytic converter on {name} may be worn", 45 + (35 if code in codes else 0),
                    [f"The downstream O2 sensor swings {max(d) - min(d):.2f} V at cruise, nearly as much as the upstream "
                     "one. Behind a working converter it should hold fairly steady."]
                    + ([f"Code {code} is present."] if code in codes else []),
                    "Check the Mode 06 catalyst test result and rule out exhaust leaks and misfires first, since "
                    "both mimic this.",
                ))

    # -- idle quality, EGR, misfires -----------------------------------------
    idle_rpm = [v["rpm"] for v in idle if "rpm" in v]
    if have_idle and len(idle_rpm) >= 20 and statistics.pstdev(idle_rpm) >= 25:
        causes.append(_cause(
            "rough_idle", "Unsteady idle", 55,
            [f"Idle speed varied by ±{statistics.pstdev(idle_rpm):.0f} rpm (standard deviation) around "
             f"{statistics.fmean(idle_rpm):.0f} rpm."],
            "Run the idle check on the Guided steps tab: it times each dip against purge, EGR, voltage and the "
            "misfire counters.",
        ))
    egr_idle = [v["egr_cmd"] for v in idle if "egr_cmd" in v]
    if have_idle and egr_idle and max(egr_idle) > 2:
        causes.append(_cause(
            "egr_idle", "EGR open at idle", 60,
            [f"The PCM commanded EGR up to {max(egr_idle):.0f}% at idle. It should be closed."],
            "Inspect the EGR valve for carbon holding it open.",
        ))
    misfire = sorted(c for c in codes if c.startswith("P030"))
    if misfire:
        cyl = [c[-1] for c in misfire if c != "P0300"]
        causes.append(_cause(
            "misfire_codes", "Misfire" + (f" on cylinder {', '.join(cyl)}" if cyl else " (random or multiple cylinders)"), 85,
            [f"Code(s) present: {', '.join(misfire)}"],
            "For one cylinder: swap its spark plug and coil lead with a neighbour and see if the misfire follows. "
            "For random misfires: look at fuel trims and vacuum leaks first.",
        ))

    # -- transmission ---------------------------------------------------------
    trans = [v["x_trans_temp"] for v in rows if "x_trans_temp" in v]
    if trans and max(trans) >= 120:
        causes.append(_cause(
            "trans_hot", "Transmission fluid running hot", 85 if max(trans) >= 130 else 70,
            [f"Fluid reached {max(trans):.0f} °C ({max(trans) * 9 / 5 + 32:.0f} °F). Sustained temperatures above "
             "about 105 °C (220 °F) shorten fluid life."],
            "Check fluid level and condition. Towing, big tires and slow off-road work raise it; an auxiliary "
            "cooler helps.",
        ))

    causes.sort(key=lambda c: -c["score"])
    notes = []
    if not have_idle:
        notes.append("Idle checks need about 20 seconds of warm idle (stopped, engine at temperature).")
    if not have_cruise:
        notes.append("Cruise checks need about 20 seconds at a steady 25 mph or more with the engine warm.")
    return {
        "causes": causes,
        "coverage": {"warm_idle_s": round(idle_s), "cruise_s": round(cruise_s), "samples": len(rows)},
        "notes": notes,
    }
