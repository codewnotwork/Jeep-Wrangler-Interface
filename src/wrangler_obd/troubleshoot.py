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

from . import alerts

WARM_C = 80.0
TRIM_LIMIT = 8.0       #: total (short + long) fuel trim, percent
BANK_SPLIT = 6.0       #: difference between banks, percent
SMALL_LEAK_IDLE = 3.0  #: long-term trim at idle, percent, for the small-leak check
SMALL_LEAK_GAP = 2.5   #: ...and how much higher than at cruise
MIN_SECONDS = 20.0     #: time in a condition before its rules are trusted
IDLE_SETTLE_S = 3.0    #: idle must hold this long before it counts (skips the dip back from a blip)
CRUISE_SETTLE_S = 5.0  #: cruise must hold this long (skips the seconds after coasting or a fuel cut)
CAT_WARM_RUN_S = 300.0 #: the converter is not judged in the first minutes after a start
VOLTS_WINDOW_S = 30.0  #: charging voltage is judged on a rolling median over this long


def _mean(rows: Sequence[Mapping[str, float]], key: str) -> float | None:
    vals = [r[key] for r in rows if key in r]
    return statistics.fmean(vals) if vals else None


def _seconds(samples: Sequence[Sample], chosen: Sequence[Sample]) -> float:
    """Time covered by the chosen samples (gaps over 30 s are not counted)."""
    times = {t for t, _ in chosen}
    total = 0.0
    for (t0, _), (t1, _) in zip(samples, samples[1:]):
        if t0 in times and 0 < t1 - t0 < 30:
            total += t1 - t0
    return total


def rest_levels(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Lowest pedal and relative throttle seen: the foot-off positions on this truck."""
    out = {}
    for key in ("pedal_d", "throttle_relative"):
        vals = [r[key] for r in rows if key in r]
        if vals:
            out[key] = min(vals)
    return out


def foot_off(v: Mapping[str, float], rest: Mapping[str, float]) -> bool | None:
    """True when the accelerator is released, None when the truck does not say."""
    for key in ("pedal_d", "throttle_relative"):
        if key in v and key in rest:
            return v[key] <= rest[key] + 2.0
    return None


def held(samples: Sequence[Sample], pred, hold_s: float) -> list[Sample]:
    """Samples where ``pred`` has been true without a break for at least ``hold_s``."""
    out: list[Sample] = []
    since: float | None = None
    for t, v in samples:
        if pred(v):
            since = t if since is None else since
            if t - since >= hold_s:
                out.append((t, v))
        else:
            since = None
    return out


def settled_idle(samples: Sequence[Sample], rest: Mapping[str, float] | None = None) -> list[Sample]:
    """Warm idle, stopped, foot off the pedal, settled for a few seconds."""
    rest = rest_levels([v for _, v in samples]) if rest is None else rest

    def pick(v: Mapping[str, float]) -> bool:
        return (v.get("coolant_temp", 0) >= WARM_C and v.get("speed", 1) < 1 and 400 < v.get("rpm", 0) < 1000
                and foot_off(v, rest) is not False)

    return held(samples, pick, IDLE_SETTLE_S)


def steady_cruise(samples: Sequence[Sample], rest: Mapping[str, float] | None = None) -> list[Sample]:
    """Warm, 40 km/h (25 mph) or more, foot on the pedal, held for a few seconds.

    Coasting is left out: the PCM cuts fuel then, which sends every O2 sensor
    to 0 V and says nothing about the mixture or the converter.
    """
    rest = rest_levels([v for _, v in samples]) if rest is None else rest

    def pick(v: Mapping[str, float]) -> bool:
        if v.get("coolant_temp", 0) < WARM_C or v.get("speed", 0) < 40 or v.get("throttle", 0) >= 40:
            return False
        off = foot_off(v, rest)
        return not off if off is not None else v.get("engine_load", 100) >= 20

    return held(samples, pick, CRUISE_SETTLE_S)


def sustained_volts(samples: Sequence[Sample]) -> tuple[float, float | None] | None:
    """Highest rolling-median charging voltage with the engine running, and the air temperature then.

    A median over 30 seconds ignores the brief peaks a big stereo or a
    cooling fan switching on can cause, so only a voltage that stays high
    counts.
    """
    running = [(t, v) for t, v in samples if v.get("rpm", 0) > 400 and "module_voltage" in v]
    best: tuple[float, float | None] | None = None
    start = 0
    for i, (t, v) in enumerate(running):
        while running[start][0] < t - VOLTS_WINDOW_S:
            start += 1
        part = running[start : i + 1]
        if t - part[0][0] < VOLTS_WINDOW_S * 0.8:
            continue
        med = statistics.median(x["module_voltage"] for _, x in part)
        if best is None or med > best[0]:
            temps = [alerts.air_temp(x) for _, x in part]
            temps = [x for x in temps if x is not None]
            best = (med, statistics.median(temps) if temps else None)
    return best


def catalyst_temps_estimated(rows: Sequence[Mapping[str, float]]) -> bool:
    """Both banks' converter temperatures match nearly all the time: a PCM estimate, not sensors.

    The two are read one after the other, so while the value is changing
    fast a few pairs differ slightly; 95% matching within 1 °C counts.
    """
    pairs = [(r["cat_temp_b1"], r["cat_temp_b2"]) for r in rows if "cat_temp_b1" in r and "cat_temp_b2" in r]
    return len(pairs) >= 10 and sum(abs(a - b) <= 1.0 for a, b in pairs) >= 0.95 * len(pairs)


def _crossings(vals: Sequence[float], level: float = 0.45) -> int:
    return sum(1 for a, b in zip(vals, vals[1:]) if (a - level) * (b - level) < 0)


def _spread(vals: Sequence[float]) -> float:
    """Middle 80% range: ignores the odd outlier at either end."""
    s = sorted(vals)
    return s[int(0.9 * (len(s) - 1))] - s[int(0.1 * (len(s) - 1))]


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
    rest = rest_levels(rows)
    idle_samples, cruise_samples = settled_idle(samples, rest), steady_cruise(samples, rest)
    idle, cruise = [v for _, v in idle_samples], [v for _, v in cruise_samples]
    running = [v for v in rows if v.get("rpm", 0) > 400]
    idle_s, cruise_s = _seconds(samples, idle_samples), _seconds(samples, cruise_samples)
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
    elif not lean_idle and have_idle and have_cruise:
        # A small leak shows first in the long-term correction the PCM has
        # learned for idle, before the total trim gets anywhere near a code.
        li, lc = {}, {}
        for b in ("b1", "b2"):
            i, c = _mean(idle, f"ltft_{b}"), _mean(cruise, f"ltft_{b}")
            if i is not None and c is not None:
                li[b], lc[b] = i, c
        if li and all(li[b] >= SMALL_LEAK_IDLE and li[b] - lc[b] >= SMALL_LEAK_GAP for b in li):
            parts = ", ".join(f"bank {b[1]} {li[b]:+.1f}% at idle vs {lc[b]:+.1f}% at cruise" for b in li)
            causes.append(_cause(
                "small_vacuum_leak", "Possible small vacuum leak (mild, and can be normal)", 35,
                [f"Long-term fuel correction is higher at idle than at cruise: {parts}.",
                 "Air leaking in past the throttle matters most at idle, when little air is coming through it. "
                 "A few percent is mild and within normal for many engines."],
                "With the engine warm and idling, listen around the intake for a hiss. Check the PCV valve and its "
                "hoses, the brake booster hose, the small vacuum lines and the throttle body gasket. To confirm, "
                "watch short-term fuel correction while spraying carburetor cleaner briefly near a suspect spot "
                "(engine off of anything hot, away from the exhaust): if it drops, that spot leaks. A shop smoke "
                "test finds it for certain.",
            ))
    if lean_idle and "cruise" not in trims:
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
    high = sustained_volts(samples)
    if high is not None:
        peak, temp = high
        limit = alerts.volts_high_limit(temp)
        if peak >= limit:
            at = f" at about {temp:.0f} °C" if temp is not None else ""
            causes.append(_cause(
                "charging_high", "Overcharging (voltage regulation)", 80,
                [f"System voltage held at {peak:.1f} V for 30 seconds or more{at}. The normal limit then is "
                 f"{limit:.1f} V (the PCM charges a cold battery a little higher)."],
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
    # The converter is judged only in steady cruise once it has had time to
    # light off. A good converter stores oxygen, so its downstream sensor
    # switches far less often than the upstream one; a worn one copies it.
    cat_rows = [v for v in cruise if v.get("run_time", CAT_WARM_RUN_S) >= CAT_WARM_RUN_S]
    for up, down, name in (("o2_b1s1", "o2_b1s2", "bank 1"), ("o2_b2s1", "o2_b2s2", "bank 2")):
        u = [v[up] for v in cat_rows if up in v]
        d = [v[down] for v in cat_rows if down in v]
        if len(d) < 40 or len(u) < 40 or _crossings(u) < 20:
            continue
        ratio = _crossings(d) / _crossings(u)
        # The switch ratio is what decides: a healthy converter's downstream
        # sensor can drift slowly across 0.5 V or more (oxygen storage filling
        # and emptying) while still switching rarely.
        if ratio >= 0.5:
            code = {"bank 1": "P0420", "bank 2": "P0430"}[name]
            causes.append(_cause(
                f"catalyst_{down}", f"Catalytic converter on {name} may be worn", 45 + (35 if code in codes else 0),
                [f"In steady warm cruise the downstream O2 sensor switched {_crossings(d)} times for the upstream "
                 f"sensor's {_crossings(u)} (ratio {ratio:.2f}) and spanned {_spread(d):.2f} V. Behind a working "
                 "converter it switches rarely and holds fairly steady (ratio well under 0.5)."]
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
    if catalyst_temps_estimated(rows):
        notes.append("Catalyst temperatures are identical on both banks, so they are the PCM's estimate, not "
                     "measured by a sensor.")
    return {
        "causes": causes,
        "coverage": {"warm_idle_s": round(idle_s), "cruise_s": round(cruise_s), "samples": len(rows),
                     "catalyst_temps_estimated": catalyst_temps_estimated(rows)},
        "notes": notes,
    }
