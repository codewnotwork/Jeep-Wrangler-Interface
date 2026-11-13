"""Diagnostic report: everything a person or an LLM needs to assess the engine.

``build_report`` collects one-shot data (codes, freeze frame, readiness,
Mode 06 results, a snapshot of every PID) and, optionally, statistics from a
recorded log. ``to_markdown`` renders the same data for reading or pasting
into a chat. All numbers in the report are metric (SAE native units).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Iterable, Mapping

from . import __version__, alerts
from . import dtc as dtc_mod
from .elm327 import Elm327
from .pids import PIDS

SCHEMA = "wrangler-obd-report/1"
VEHICLE = "2010 Jeep Wrangler JK 2-door, 3.8L V6 (EGH)"

#: Readings that barely change during a drive. They are in the snapshot but
#: left out of the recorded log so the fast-moving ones are sampled more often.
SLOW_PIDS = {"dist_mil_on", "warmups_since_clear", "dist_since_clear", "baro", "ambient_temp", "fuel_level"}

Sample = Mapping[str, float]


def _round(value: float) -> float:
    return round(value, 2)


def _stats(values: list[float]) -> dict:
    return {"min": _round(min(values)), "mean": _round(statistics.fmean(values)), "max": _round(max(values))}


def summarize_log(samples: Iterable[Sample], duration_s: float | None = None) -> dict:
    """Reduce a recorded log to statistics, split by operating condition.

    Segments: ``idle`` (stopped, warm, under 1000 rpm) and ``cruise``
    (moving, steady throttle). Fuel trims mean different things in each: a
    lean trim only at idle points to a vacuum leak, lean everywhere points
    to fuel delivery or the MAP sensor.
    """
    samples = list(samples)
    summary: dict = {"samples": len(samples)}
    if duration_s is not None:
        summary["duration_s"] = _round(duration_s)
    if not samples:
        return summary

    def collect(rows: list[Sample]) -> dict:
        out = {}
        for key in PIDS:
            vals = [r[key] for r in rows if key in r]
            if vals:
                out[key] = _stats(vals)
        return out

    summary["overall"] = collect(samples)
    warm = [s for s in samples if s.get("coolant_temp", 0) >= 80]
    idle = [s for s in warm if s.get("speed", 0) < 1 and 0 < s.get("rpm", 0) < 1000]
    cruise = [s for s in warm if s.get("speed", 0) >= 40 and s.get("throttle", 0) < 40]
    trim_keys = ("stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "rpm", "map", "maf", "engine_load")
    segments = {}
    for name, rows in (("idle", idle), ("cruise", cruise)):
        if rows:
            stats = collect(rows)
            segments[name] = {"samples": len(rows), **{k: stats[k] for k in trim_keys if k in stats}}
    summary["segments"] = segments

    # Upstream O2 sensors should swing rich/lean constantly once warm.
    o2 = {}
    for key in ("o2_b1s1", "o2_b2s1"):
        vals = [s[key] for s in warm if key in s]
        if len(vals) >= 10:
            crossings = sum(1 for a, b in zip(vals, vals[1:]) if (a - 0.45) * (b - 0.45) < 0)
            o2[key] = {"min": _round(min(vals)), "max": _round(max(vals)), "crossings_of_0.45V": crossings}
    if o2:
        summary["o2_switching"] = o2

    findings: dict[tuple[str, str], alerts.Alert] = {}
    for s in samples:
        for alert in alerts.check(s):
            if alert.key.startswith("fuel_trim"):
                continue  # judged on segment averages below, not single samples
            findings.setdefault((alert.key, alert.severity), alert)
    for name, seg in segments.items():
        for bank in ("b1", "b2"):
            if f"ltft_{bank}" in seg:
                total = seg[f"ltft_{bank}"]["mean"] + seg.get(f"stft_{bank}", {"mean": 0})["mean"]
                alert = alerts.trim_alert(bank, total)
                if alert:
                    findings[(f"{alert.key}_{name}", alert.severity)] = alerts.Alert(
                        alert.severity, alert.key, f"At {name}: {alert.message}"
                    )
    summary["findings"] = [a.as_dict() for a in findings.values()]
    return summary


def _codes(codes: list[str]) -> list[dict]:
    return [{"code": c, "description": dtc_mod.describe(c)} for c in codes]


def build_report(
    elm: Elm327,
    log_summary: dict | None = None,
    log_file: str | None = None,
    notes: str = "",
    idle_check: dict | None = None,
    fuel_economy: dict | None = None,
) -> dict:
    """Query the vehicle and assemble the report dictionary."""
    status = elm.monitor_status() or {}
    dtcs = elm.read_dtcs()
    frame = elm.freeze_frame()
    snapshot = elm.read_pids(list(PIDS))
    tests = elm.test_results()

    findings = [a.as_dict() for a in alerts.check({k: r.value for k, r in snapshot.items()})]
    for t in tests:
        if not t.passed:
            findings.append({
                "severity": "warning", "key": f"mode06_{t.mid:02X}_{t.tid:02X}",
                "message": f"On-board test failing: {t.monitor}, {t.test}",
            })
        elif t.mid >= 0xA1 and t.unit == "counts" and t.value > 0:
            findings.append({
                "severity": "warning", "key": f"misfire_{t.mid:02X}",
                "message": f"{t.monitor}: {t.value:.0f} misfire(s) counted ({t.test.lower()})",
            })
    incomplete = [m["name"] for m in status.get("monitors", []) if not m["complete"]]
    if log_summary:
        findings += [f for f in log_summary.get("findings", []) if f not in findings]

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "units": "metric (°C, km/h, kPa, g/s, km)",
        "vehicle": {
            "description": VEHICLE,
            "vin": elm.vin(),
            "protocol": elm.protocol,
            "adapter": elm.version,
            "battery_voltage_at_port": elm.battery_voltage(),
        },
        "owner_notes": notes,
        "check_engine_light_on": dtcs.mil_on,
        "trouble_codes": {
            "stored": _codes(dtcs.stored),
            "pending": _codes(dtcs.pending),
            "permanent": _codes(dtcs.permanent),
        },
        "freeze_frame": None if frame is None else {
            "code": frame.dtc,
            "description": dtc_mod.describe(frame.dtc),
            "values": {k: {"value": _round(r.value), "unit": r.unit, "name": r.pid.name} for k, r in frame.readings.items()},
        },
        "readiness": {
            "monitors": status.get("monitors", []),
            "incomplete": incomplete,
            "ready_for_emissions_test": not incomplete if status else None,
        },
        "fuel_system_status": elm.fuel_system_status(),
        "snapshot": {k: {"value": _round(r.value), "unit": r.unit, "name": r.pid.name} for k, r in snapshot.items()},
        "onboard_tests": [t.as_dict() for t in tests],
        "findings": findings,
    }
    if fuel_economy is not None:
        report["fuel_economy"] = fuel_economy
    if idle_check is not None:
        report["idle_check"] = {"file": log_file, **idle_check}
    if log_summary is not None:
        report["log"] = {"file": log_file, **{k: v for k, v in log_summary.items() if k != "findings"}}
    return report


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


LLM_PROMPT = (
    "You are helping diagnose this vehicle. Using the data below, list the most likely "
    "problems in order of likelihood, the evidence for each, what to check or test next, "
    "and anything that looks healthy and can be ruled out. Say what extra data would help."
)


def to_markdown(report: dict) -> str:
    """Render the report as Markdown, readable by a person and compact enough for an LLM."""
    v = report["vehicle"]
    out = [
        "# Diagnostic report",
        "",
        f"> {LLM_PROMPT}",
        "",
        f"- Vehicle: {v['description']}",
        f"- VIN: {v['vin'] or 'not reported'}",
        f"- Generated: {report['generated_at']} (wrangler-obd {report['tool_version']})",
        f"- Protocol: {v['protocol']}",
        f"- Units: {report['units']}",
    ]
    if v["battery_voltage_at_port"] is not None:
        out.append(f"- Battery voltage at OBD port: {v['battery_voltage_at_port']:.1f} V")
    if report["fuel_system_status"]:
        out.append(f"- Fuel system: {report['fuel_system_status']}")
    if report["owner_notes"]:
        out += ["", "## Owner's notes", "", report["owner_notes"]]

    out += ["", "## Findings", ""]
    if report["findings"]:
        out += [f"- **{f['severity']}**: {f['message']}" for f in report["findings"]]
    else:
        out.append("Nothing flagged automatically.")

    if report.get("idle_check"):
        from . import idle

        out += [""] + idle.to_markdown(report["idle_check"])

    if report.get("fuel_economy"):
        from . import economy

        out += [""] + economy.to_markdown(report["fuel_economy"])

    mil = report["check_engine_light_on"]
    out += ["", "## Trouble codes", "", f"Check engine light: {'unknown' if mil is None else 'ON' if mil else 'off'}", ""]
    for label in ("stored", "pending", "permanent"):
        codes = report["trouble_codes"][label]
        out.append(f"- {label.capitalize()}: " + ("none" if not codes else ""))
        out += [f"  - {c['code']}: {c['description']}" for c in codes]

    out += ["", "## Freeze frame", ""]
    frame = report["freeze_frame"]
    if frame:
        out += [f"Conditions when {frame['code']} ({frame['description']}) set:", ""]
        out += _table(["Reading", "Value"], [[x["name"], f"{x['value']} {x['unit']}"] for x in frame["values"].values()])
    else:
        out.append("No freeze frame stored.")

    out += ["", "## Readiness monitors", ""]
    monitors = report["readiness"]["monitors"]
    if monitors:
        out += [f"- {m['name']}: {'complete' if m['complete'] else 'NOT complete'}" for m in monitors]
    else:
        out.append("Not reported.")

    out += ["", "## On-board test results (Mode 06)", ""]
    tests = report["onboard_tests"]
    if tests:
        rows = [
            [t["monitor"], t["test"], t["value"], t["min"], t["max"], t["unit"], "pass" if t["passed"] else "FAIL"]
            for t in tests
        ]
        out += _table(["Monitor", "Test", "Value", "Min", "Max", "Unit", "Result"], rows)
    else:
        out.append("None reported (they reset when codes are cleared).")

    out += ["", "## Snapshot of live data", ""]
    out += _table(["Reading", "Value"], [[x["name"], f"{x['value']} {x['unit']}"] for x in report["snapshot"].values()])

    log = report.get("log")
    if log and log.get("samples"):
        out += ["", "## Recorded log", "", f"{log['samples']} samples"
                + (f" over {log['duration_s']:.0f} s" if "duration_s" in log else "")
                + (f", raw data in `{log['file']}`." if log.get("file") else ".")]
        for name, title in (("idle", "Warm idle"), ("cruise", "Warm cruise")):
            seg = log.get("segments", {}).get(name)
            if seg:
                out += ["", f"### {title} ({seg['samples']} samples)", ""]
                out += _stat_table({k: s for k, s in seg.items() if k != "samples"})
        if log.get("o2_switching"):
            out += ["", "### Upstream O2 sensor switching (warm)", ""]
            rows = [[PIDS[k].name, s["min"], s["max"], s["crossings_of_0.45V"]] for k, s in log["o2_switching"].items()]
            out += _table(["Sensor", "Min V", "Max V", "Crossings of 0.45 V"], rows)
        out += ["", "### Whole log", ""]
        out += _stat_table(log.get("overall", {}))
    return "\n".join(out) + "\n"


def _stat_table(stats: Mapping[str, Mapping[str, float]]) -> list[str]:
    rows = [[f"{PIDS[k].name} ({PIDS[k].unit})", s["min"], s["mean"], s["max"]] for k, s in stats.items()]
    return _table(["Reading", "Min", "Mean", "Max"], rows)


def _table(head: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines
