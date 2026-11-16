"""Battery check from the engine start: resting voltage and the dip while cranking.

When a recording starts with the key on and the engine off, the monitor
reads the adapter's voltage (ATRV) as fast as it can until the engine
runs. A healthy battery and starter hold the voltage above about 10 V
while cranking; a dip below about 9.6 V means a weak battery, a poor
connection or a starter drawing too much. Resting voltage before the
start says how charged the battery was (12.6 V full, 12.2 V about half).
"""

from __future__ import annotations

import statistics

CRANK_RPM = 100.0       #: below this the engine is off
RUNNING_RPM = 400.0     #: above this it has started
DIP_V = 0.8             #: a drop this far below rest, engine still off, counts as cranking
WEAK_V = 9.6
FINE_V = 10.0
MAX_CRANK_S = 20.0


class CrankWatch:
    """Feed it (time, rpm, volts) from key-on until ``done``."""

    def __init__(self) -> None:
        self.rest: list[float] = []
        self.crank_start: float | None = None
        self.min_v: float | None = None
        self.done = False
        self.result: dict | None = None

    @property
    def watching(self) -> bool:
        return not self.done

    def feed(self, t: float, rpm: float | None, volts: float | None) -> None:
        if self.done:
            return
        if rpm is not None and rpm > RUNNING_RPM:
            if self.crank_start is None and not self.rest:
                self.done = True  # already running when the recording started
                return
            self.finish(t)
            return
        if volts is None:
            # The PCM often goes quiet mid-crank; a missing voltage just isn't a sample.
            if rpm is not None and rpm >= CRANK_RPM and self.crank_start is None:
                self.crank_start = t
            return
        cranking = (rpm is not None and rpm >= CRANK_RPM) or (
            self.rest and volts < statistics.median(self.rest[-20:]) - DIP_V)
        if cranking:
            if self.crank_start is None:
                self.crank_start = t
            self.min_v = volts if self.min_v is None else min(self.min_v, volts)
        elif self.crank_start is None:
            self.rest.append(volts)
        if self.crank_start is not None and t - self.crank_start > MAX_CRANK_S:
            self.finish(t)

    def finish(self, t: float) -> None:
        self.done = True
        if self.crank_start is None or self.min_v is None:
            return
        rest = round(statistics.median(self.rest[-20:]), 2) if self.rest else None
        self.result = {
            "resting_v": rest,
            "cranking_min_v": round(self.min_v, 2),
            "crank_s": round(t - self.crank_start, 1),
            "verdict": verdict(rest, self.min_v),
        }


def verdict(rest: float | None, min_v: float) -> str:
    if min_v < WEAK_V:
        return (f"Weak: the voltage fell to {min_v:.1f} V while cranking (under {WEAK_V} V). "
                "Have the battery load-tested and check the terminals and ground strap.")
    if min_v < FINE_V:
        return (f"Borderline: {min_v:.1f} V while cranking. Fine for now; worth a load test "
                "before cold weather.")
    text = f"Good: the voltage held at {min_v:.1f} V or above while cranking."
    if rest is not None and rest < 12.3:
        text += f" Resting voltage was low ({rest:.1f} V), so the battery wasn't fully charged."
    return text
