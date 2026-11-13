"""Rules that flag readings worth a second look.

Thresholds are for the 2010 JK 3.8L: a 195 °F (90 °C) thermostat, so normal
coolant is roughly 90-105 °C, and a charging system that should hold
13.5-14.8 V with the engine running. All inputs are metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

COOLANT_WARN_C = 110.0
COOLANT_CRIT_C = 118.0
VOLTS_LOW_RUNNING = 13.0
VOLTS_HIGH = 15.1
VOLTS_LOW_ENGINE_OFF = 11.8
TRIM_WARN_PCT = 10.0
TRIM_CRIT_PCT = 20.0


@dataclass(frozen=True)
class Alert:
    severity: str  #: "warning" or "critical"
    key: str
    message: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "key": self.key, "message": self.message}


def check(values: Mapping[str, float]) -> list[Alert]:
    """Check one set of live readings (keys from ``pids.PIDS``)."""
    alerts: list[Alert] = []
    running = values.get("rpm", 0) > 400

    coolant = values.get("coolant_temp")
    if coolant is not None and coolant >= COOLANT_WARN_C:
        sev = "critical" if coolant >= COOLANT_CRIT_C else "warning"
        alerts.append(Alert(sev, "coolant_temp", f"Coolant is hot: {coolant:.0f} °C ({coolant * 9 / 5 + 32:.0f} °F)"))

    volts = values.get("module_voltage")
    if volts is not None:
        if volts >= VOLTS_HIGH:
            alerts.append(Alert("warning", "module_voltage", f"System voltage is high: {volts:.1f} V (overcharging)"))
        elif running and volts < VOLTS_LOW_RUNNING:
            alerts.append(Alert(
                "warning", "module_voltage", f"System voltage is low with the engine running: {volts:.1f} V (not charging)",
            ))
        elif not running and volts < VOLTS_LOW_ENGINE_OFF:
            alerts.append(Alert("warning", "module_voltage", f"Battery is low: {volts:.1f} V"))

    for bank in ("b1", "b2"):
        ltft = values.get(f"ltft_{bank}")
        if ltft is None:
            continue
        alert = trim_alert(bank, ltft + values.get(f"stft_{bank}", 0.0))
        if alert:
            alerts.append(alert)
    return alerts


def trim_alert(bank: str, total: float) -> Alert | None:
    """Flag a total fuel trim (short + long term) that is far from zero."""
    if abs(total) < TRIM_WARN_PCT:
        return None
    sev = "critical" if abs(total) >= TRIM_CRIT_PCT else "warning"
    direction = "adding fuel (running lean)" if total > 0 else "pulling fuel (running rich)"
    return Alert(sev, f"fuel_trim_{bank}", f"Bank {bank[1]} total fuel trim is {total:+.1f}%: PCM is {direction}")
