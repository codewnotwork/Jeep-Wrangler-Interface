"""Mode 06: on-board monitoring test results (CAN format).

The PCM runs its own pass/fail tests on the catalysts, O2 sensors, EVAP
system and misfire detection. Mode 06 exposes each test's measured value
and its limits, so a test that is close to failing shows up before a
trouble code sets.

Each result is 9 bytes: monitor id (MID), test id (TID), unit/scaling id,
then value, minimum and maximum as 16-bit numbers. MIDs and scaling ids are
standardised by SAE J1979; TIDs 0x80 and up are manufacturer-defined, so
those tests are reported without a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

MONITORS: dict[int, str] = {
    0x01: "O2 sensor, bank 1 sensor 1",
    0x02: "O2 sensor, bank 1 sensor 2",
    0x05: "O2 sensor, bank 2 sensor 1",
    0x06: "O2 sensor, bank 2 sensor 2",
    0x21: "Catalyst, bank 1",
    0x22: "Catalyst, bank 2",
    0x31: "EGR, bank 1",
    0x39: "EVAP (cap off)",
    0x3A: 'EVAP (0.090" leak)',
    0x3B: 'EVAP (0.040" leak)',
    0x3C: 'EVAP (0.020" leak)',
    0x3D: "EVAP purge flow",
    0x41: "O2 sensor heater, bank 1 sensor 1",
    0x42: "O2 sensor heater, bank 1 sensor 2",
    0x45: "O2 sensor heater, bank 2 sensor 1",
    0x46: "O2 sensor heater, bank 2 sensor 2",
    0xA1: "Misfire, all cylinders",
    0xA2: "Misfire, cylinder 1",
    0xA3: "Misfire, cylinder 2",
    0xA4: "Misfire, cylinder 3",
    0xA5: "Misfire, cylinder 4",
    0xA6: "Misfire, cylinder 5",
    0xA7: "Misfire, cylinder 6",
}

#: What each monitor checks, in plain English.
PURPOSE: dict[str, str] = {
    "o2_up": "Checks that the upstream oxygen sensor switches rich/lean quickly enough to control the mixture.",
    "o2_down": "Checks that the downstream oxygen sensor (behind the converter) reads sensibly; it is also used to judge the converter.",
    "catalyst": "Checks how well the catalytic converter stores oxygen and cleans the exhaust. Higher numbers mean a worn converter.",
    "egr": "Checks that the EGR valve lets the right amount of exhaust back into the intake (it lowers combustion temperature).",
    "evap": "Checks the fuel vapour system (tank, lines, charcoal canister) for leaks.",
    "purge": "Checks that the purge valve draws fuel vapour from the canister into the engine when told to.",
    "heater": "Checks that the oxygen sensor's heater warms it up in time after a cold start.",
    "misfire": "Counts misfires the PCM detected. Zero is ideal; a few spread over many cylinders is normal.",
}


def _purpose_key(mid: int) -> str | None:
    if mid in (0x01, 0x05):
        return "o2_up"
    if mid in (0x02, 0x06):
        return "o2_down"
    if 0x21 <= mid <= 0x24:
        return "catalyst"
    if 0x31 <= mid <= 0x38:
        return "egr"
    if 0x39 <= mid <= 0x3C:
        return "evap"
    if mid == 0x3D:
        return "purge"
    if 0x41 <= mid <= 0x48:
        return "heater"
    if 0xA1 <= mid <= 0xAD:
        return "misfire"
    return None


# Standard test ids (below 0x80).
TESTS: dict[int, str] = {
    0x01: "Rich to lean sensor threshold voltage",
    0x02: "Lean to rich sensor threshold voltage",
    0x03: "Low sensor voltage for switch time",
    0x04: "High sensor voltage for switch time",
    0x05: "Rich to lean switch time",
    0x06: "Lean to rich switch time",
    0x07: "Minimum sensor voltage",
    0x08: "Maximum sensor voltage",
    0x09: "Time between sensor transitions",
    0x0A: "Sensor period",
    0x0B: "Misfire count, average of last 10 drive cycles",
    0x0C: "Misfire count, this or last drive cycle",
}

# Unit/scaling ids: (scale, offset, unit, signed). Ids not listed here are
# reported as raw numbers, which still compare correctly against the limits.
_SCALING: dict[int, tuple[float, float, str]] = {
    0x01: (1, 0, ""), 0x02: (0.1, 0, ""), 0x03: (0.01, 0, ""), 0x04: (0.001, 0, ""),
    0x05: (0.0000305, 0, ""), 0x06: (0.000305, 0, ""),
    0x07: (0.25, 0, "rpm"), 0x08: (0.01, 0, "km/h"), 0x09: (1, 0, "km/h"),
    0x0A: (0.000122, 0, "V"), 0x0B: (0.001, 0, "V"), 0x0C: (0.01, 0, "V"),
    0x0D: (0.00390625, 0, "mA"), 0x0E: (0.001, 0, "A"), 0x0F: (0.01, 0, "A"),
    0x10: (0.001, 0, "s"), 0x11: (0.1, 0, "s"), 0x12: (1, 0, "s"),
    0x13: (0.001, 0, "ohm"), 0x14: (1, 0, "ohm"), 0x15: (1, 0, "kohm"),
    0x16: (0.1, -40, "°C"), 0x17: (0.01, 0, "kPa"), 0x1A: (1, 0, "kPa"),
    0x1C: (0.01, 0, "°"), 0x1D: (0.5, 0, "°"),
    0x20: (0.0039062, 0, "ratio"), 0x24: (1, 0, "counts"), 0x25: (1, 0, "km"),
    0x27: (0.01, 0, "g/s"), 0x28: (1, 0, "g/s"), 0x2B: (1, 0, "switches"),
    0x2F: (0.01, 0, "%"), 0x30: (0.001526, 0, "%"), 0x34: (1, 0, "min"), 0x35: (0.01, 0, "s"),
    # 0x80 and up are signed.
    0x81: (1, 0, ""), 0x82: (0.1, 0, ""), 0x83: (0.01, 0, ""), 0x84: (0.001, 0, ""),
    0x8A: (0.000122, 0, "V"), 0x8B: (0.001, 0, "V"), 0x8C: (0.01, 0, "V"),
    0x90: (0.001, 0, "s"), 0x96: (0.1, 0, "°C"), 0x9C: (0.01, 0, "°"), 0x9D: (0.5, 0, "°"),
    0xAF: (0.01, 0, "%"), 0xB0: (0.001526, 0, "%"),
    0xFC: (0.01, 0, "kPa"), 0xFD: (0.001, 0, "kPa"), 0xFE: (0.25, 0, "Pa"),
}


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest class

    mid: int
    tid: int
    unit_id: int
    value: float
    minimum: float
    maximum: float
    unit: str
    scaled: bool  #: False if the unit id is unknown and the numbers are raw

    @property
    def monitor(self) -> str:
        return MONITORS.get(self.mid, f"Monitor 0x{self.mid:02X}")

    @property
    def test(self) -> str:
        return TESTS.get(self.tid, f"Chrysler test 0x{self.tid:02X} (definition not published)")

    @property
    def passed(self) -> bool:
        return self.minimum <= self.value <= self.maximum

    @property
    def margin(self) -> float | None:
        """How far inside the limits the value sits: 0.0 at a limit, 0.5 dead centre.

        None when the limits are the full 16-bit range (the PCM is just
        reporting a number, such as a misfire count) or the test failed.
        """
        span = self.maximum - self.minimum
        if span <= 0 or not self.passed or self.open_ended:
            return None
        return min(self.value - self.minimum, self.maximum - self.value) / span

    @property
    def open_ended(self) -> bool:
        """True when min and max span the whole range, i.e. there is no real limit."""
        return (self.minimum, self.maximum) == _scaled_range(self.unit_id)

    @property
    def purpose(self) -> str:
        key = _purpose_key(self.mid)
        return PURPOSE[key] if key else ""

    def limit_use(self) -> tuple[float, str] | None:
        """How close the value is to the limit that matters: ``(fraction, "max"|"min")``.

        0.0 is far from the limit and 1.0 is at it. Only one side of most
        tests is a real limit: the other is zero or the end of the range.
        """
        lo, hi = _scaled_range(self.unit_id)
        has_max = self.maximum < hi and self.maximum > 0
        has_min = self.minimum > max(lo, 0)
        if self.minimum == self.maximum or not self.passed:
            return None
        if has_max and not has_min and self.value >= 0:
            return self.value / self.maximum, "max"
        if has_min and not has_max and self.value > 0:
            return self.minimum / self.value, "min"
        if has_min and has_max:
            m = self.margin
            return (1 - 2 * m, "both") if m is not None else None
        return None

    def verdict(self) -> str:
        """One plain-English line on the result and its room to spare."""
        if not self.passed:
            return f"FAILED: {_num(self.value)} is outside {_num(self.minimum)} to {_num(self.maximum)}."
        if _purpose_key(self.mid) == "misfire" and self.unit == "counts":
            n = round(self.value)
            return "No misfires counted." if n == 0 else f"{n} misfire{'s' if n != 1 else ''} counted."
        if self.minimum == self.maximum and self.value == self.minimum:
            return f"Passed: {_num(self.value)}, the expected value."
        if self.minimum == self.maximum or self.open_ended:
            return f"Passed. Reports {_num(self.value)}; no limit applies."
        use = self.limit_use() if self.scaled else None
        if use is None:
            return f"Passed ({_num(self.value)}, allowed {_num(self.minimum)} to {_num(self.maximum)})."
        frac, side = use
        if frac >= 0.999:
            return f"Passed: reached the {_num(self.value)} the PCM looks for."
        limit = {"max": f"limit {_num(self.maximum)}", "min": f"must stay above {_num(self.minimum)}",
                 "both": f"allowed {_num(self.minimum)} to {_num(self.maximum)}"}[side]
        how = "with plenty of room" if frac < 0.5 else "with some room" if frac < 0.8 else "but near the limit"
        return f"Passed {how}: {_num(self.value)} ({limit}, {frac * 100:.0f}% of the way to it)."

    def as_dict(self) -> dict:
        return {
            "monitor": self.monitor, "test": self.test, "checks": self.purpose, "verdict": self.verdict(),
            "mid": f"{self.mid:02X}", "tid": f"{self.tid:02X}",
            "value": round(self.value, 4), "min": round(self.minimum, 4), "max": round(self.maximum, 4),
            "unit": self.unit if self.scaled else f"raw (unit id {self.unit_id:02X})",
            "passed": self.passed,
        }


def _scale(unit_id: int, raw: int) -> tuple[float, str, bool]:
    signed = unit_id >= 0x80
    if signed and raw >= 0x8000:
        raw -= 0x10000
    if unit_id in _SCALING:
        scale, offset, unit = _SCALING[unit_id]
        return raw * scale + offset, unit, True
    return float(raw), "", False


def _scaled_range(unit_id: int) -> tuple[float, float]:
    lo, hi = (0x8000, 0x7FFF) if unit_id >= 0x80 else (0x0000, 0xFFFF)
    return _scale(unit_id, lo)[0], _scale(unit_id, hi)[0]


def decode_test_results(payload: Sequence[int]) -> list[TestResult]:
    """Decode the bytes after the 0x46 mode byte of a Mode 06 response."""
    results = []
    data = list(payload)
    for i in range(0, len(data) - 8, 9):
        mid, tid, unit_id = data[i : i + 3]
        raw = [data[i + j] * 256 + data[i + j + 1] for j in (3, 5, 7)]
        value, unit, scaled = _scale(unit_id, raw[0])
        results.append(TestResult(
            mid, tid, unit_id, value, _scale(unit_id, raw[1])[0], _scale(unit_id, raw[2])[0], unit, scaled,
        ))
    return results


def _num(x: float) -> str:
    return f"{x:.0f}" if abs(x) >= 1000 else f"{x:.4g}"
