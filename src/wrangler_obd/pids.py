"""Mode 01 PID definitions and decoders.

Values are decoded to metric units (the SAE J1979 native units). Use
``to_imperial`` to convert for display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class Pid:
    pid: int
    key: str
    name: str
    unit: str
    nbytes: int
    decode: Callable[[Sequence[int]], float]

    @property
    def command(self) -> str:
        return f"01{self.pid:02X}"


def _pct(d: Sequence[int]) -> float:
    return d[0] * 100.0 / 255.0


def _temp(d: Sequence[int]) -> float:
    return float(d[0] - 40)


def _trim(d: Sequence[int]) -> float:
    return (d[0] - 128) * 100.0 / 128.0


def _o2_volts(d: Sequence[int]) -> float:
    return d[0] / 200.0


def _word(d: Sequence[int]) -> int:
    return d[0] * 256 + d[1]


_DEFS = [
    Pid(0x04, "engine_load", "Calculated engine load", "%", 1, _pct),
    Pid(0x05, "coolant_temp", "Engine coolant temperature", "°C", 1, _temp),
    Pid(0x06, "stft_b1", "Short term fuel trim, bank 1", "%", 1, _trim),
    Pid(0x07, "ltft_b1", "Long term fuel trim, bank 1", "%", 1, _trim),
    Pid(0x08, "stft_b2", "Short term fuel trim, bank 2", "%", 1, _trim),
    Pid(0x09, "ltft_b2", "Long term fuel trim, bank 2", "%", 1, _trim),
    Pid(0x0B, "map", "Intake manifold pressure", "kPa", 1, lambda d: float(d[0])),
    Pid(0x0C, "rpm", "Engine RPM", "rpm", 2, lambda d: _word(d) / 4.0),
    Pid(0x0D, "speed", "Vehicle speed", "km/h", 1, lambda d: float(d[0])),
    Pid(0x0E, "timing_advance", "Ignition timing advance", "°", 1, lambda d: d[0] / 2.0 - 64.0),
    Pid(0x0F, "intake_temp", "Intake air temperature", "°C", 1, _temp),
    Pid(0x10, "maf", "Mass air flow", "g/s", 2, lambda d: _word(d) / 100.0),
    Pid(0x11, "throttle", "Throttle position", "%", 1, _pct),
    Pid(0x14, "o2_b1s1", "O2 sensor voltage, bank 1 sensor 1", "V", 2, _o2_volts),
    Pid(0x15, "o2_b1s2", "O2 sensor voltage, bank 1 sensor 2", "V", 2, _o2_volts),
    Pid(0x18, "o2_b2s1", "O2 sensor voltage, bank 2 sensor 1", "V", 2, _o2_volts),
    Pid(0x19, "o2_b2s2", "O2 sensor voltage, bank 2 sensor 2", "V", 2, _o2_volts),
    Pid(0x1F, "run_time", "Run time since engine start", "s", 2, lambda d: float(_word(d))),
    Pid(0x2F, "fuel_level", "Fuel tank level", "%", 1, _pct),
    Pid(0x33, "baro", "Barometric pressure", "kPa", 1, lambda d: float(d[0])),
    Pid(0x42, "module_voltage", "Control module voltage", "V", 2, lambda d: _word(d) / 1000.0),
    Pid(0x46, "ambient_temp", "Ambient air temperature", "°C", 1, _temp),
]

PIDS: dict[str, Pid] = {p.key: p for p in _DEFS}
PIDS_BY_NUMBER: dict[int, Pid] = {p.pid: p for p in _DEFS}

#: The default set shown by ``live`` and recorded by ``log``.
DEFAULT_LIVE = [
    "rpm", "speed", "coolant_temp", "intake_temp", "engine_load", "throttle",
    "maf", "timing_advance", "stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "module_voltage",
]


def decode_supported(base: int, data: Sequence[int]) -> set[int]:
    """Decode a 4-byte "PIDs supported" bitmap returned for PID ``base``."""
    supported = set()
    for i in range(32):
        if data[i // 8] & (0x80 >> (i % 8)):
            supported.add(base + i + 1)
    return supported


def decode_monitor_status(data: Sequence[int]) -> dict:
    """Decode PID 01 (MIL status and stored DTC count)."""
    return {"mil_on": bool(data[0] & 0x80), "dtc_count": data[0] & 0x7F}


_IMPERIAL = {
    "°C": ("°F", lambda v: v * 9 / 5 + 32),
    "km/h": ("mph", lambda v: v * 0.621371),
    "kPa": ("psi", lambda v: v * 0.145038),
    "g/s": ("lb/min", lambda v: v * 0.132277),
}


def to_imperial(value: float, unit: str) -> tuple[float, str]:
    """Convert a metric value to US units. Units without a conversion pass through."""
    if unit in _IMPERIAL:
        new_unit, fn = _IMPERIAL[unit]
        return fn(value), new_unit
    return value, unit
