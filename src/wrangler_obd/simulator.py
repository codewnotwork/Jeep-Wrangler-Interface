"""A fake ELM327 + 2010 JK 3.8L PCM, so everything runs without the Jeep.

It speaks the same text protocol as a real adapter, so the driver code
under test is exactly the code that talks to the truck.
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable

from .pids import PIDS

DEFAULT_VIN = "1J4AA2D10AL000000"  # placeholder JK-style VIN, not a real vehicle


class SimulatedVehicle:
    """Generates plausible engine values that change over time."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, seed: int | None = None) -> None:
        self._clock = clock
        self._t0 = clock()
        self._rng = random.Random(seed)
        self.stored_dtcs = ["P0456"]
        self.pending_dtcs = ["P0171"]
        self.permanent_dtcs = ["P0456"]
        self.mil_on = True
        # Monitors this truck has (PID 01 byte C) and which are still incomplete (byte D).
        self.monitors_available = 0x65  # catalyst, evap, O2 sensor, O2 heater
        self.monitors_incomplete = 0x00
        # Freeze frame captured when P0456 set: warm engine at a steady cruise.
        self.freeze_dtc: str | None = "P0456"
        self.freeze_values: dict[str, float] = {
            "engine_load": 38, "coolant_temp": 91, "stft_b1": 1.6, "ltft_b1": 5.5,
            "stft_b2": -0.8, "ltft_b2": 2.3, "map": 48, "rpm": 1850, "speed": 72,
            "intake_temp": 27, "throttle": 22,
        }
        # Mode 06 on-board test results: (monitor id, test id, unit/scaling id, value, min, max).
        self.test_results: list[tuple[int, int, int, int, int, int]] = [
            (0x01, 0x01, 0x0B, 610, 0, 800),      # O2 B1S1 rich-to-lean threshold voltage
            (0x21, 0x80, 0x20, 115, 0, 192),      # catalyst B1 ratio
            (0x22, 0x80, 0x20, 98, 0, 192),       # catalyst B2 ratio
            (0x3C, 0x80, 0xFE, 0xFFEC, 0x8000, 0xFFD8),  # EVAP 0.020" leak: failing
            (0xA1, 0x0C, 0x24, 3, 0, 65535),      # misfire, all cylinders, this drive cycle
            (0xA2, 0x0B, 0x24, 0, 0, 65535),
            (0xA2, 0x0C, 0x24, 0, 0, 65535),
            (0xA3, 0x0C, 0x24, 3, 0, 65535),      # cylinder 2 has a few misfires
            (0xA4, 0x0C, 0x24, 0, 0, 65535),
            (0xA5, 0x0C, 0x24, 0, 0, 65535),
            (0xA6, 0x0C, 0x24, 0, 0, 65535),
            (0xA7, 0x0C, 0x24, 0, 0, 65535),
        ]

    def elapsed(self) -> float:
        return self._clock() - self._t0

    #: An idle stumble: every STUMBLE_EVERY seconds cylinder 2 misfires briefly.
    STUMBLE_EVERY = 7.0
    STUMBLE_LENGTH = 0.4
    stumble = True

    def stumbling(self) -> bool:
        return self.stumble and 3.0 <= self.elapsed() % self.STUMBLE_EVERY < 3.0 + self.STUMBLE_LENGTH

    def new_misfires(self) -> int:
        """Misfires counted since start: one per stumble."""
        if not self.stumble or self.elapsed() < 3.0:
            return 0
        return int((self.elapsed() - 3.0) // self.STUMBLE_EVERY) + 1

    def values(self) -> dict[str, float]:
        t = self.elapsed()
        jitter = self._rng.uniform
        miss = self.stumbling()
        # Idle for 20 s, then a gentle repeating drive cycle.
        driving = max(0.0, math.sin((t - 20) / 15)) if t > 20 else 0.0
        rpm = 680 + driving * 1800 + jitter(-8, 8) - (45 if miss else 0)
        speed = driving * 80
        throttle = 14 + driving * 35
        coolant = min(96.0, 20 + t * 0.6)
        return {
            "engine_load": 22 + driving * 45,
            "coolant_temp": coolant,
            "stft_b1": jitter(-3, 3),
            "ltft_b1": 6.3,  # slightly lean, matches the pending P0171
            "stft_b2": jitter(-3, 3),
            "ltft_b2": 2.3,
            "map": 32 + driving * 50,
            "rpm": rpm,
            "speed": speed,
            "timing_advance": 12 + driving * 18,
            "intake_temp": 24,
            "maf": 3.2 + driving * 30,
            "throttle": throttle,
            "o2_b1s1": 0.45 + 0.4 * math.sin(t * 3),
            "o2_b1s2": 0.65,
            "o2_b2s1": 0.12 if miss else 0.45 + 0.4 * math.sin(t * 3 + 1),  # unburned oxygen reads lean
            "o2_b2s2": 0.66,
            "run_time": t,
            "fuel_level": 61,
            "baro": 99,
            "module_voltage": 14.2 + jitter(-0.05, 0.05),
            "ambient_temp": 18,
            "dist_mil_on": 42,
            "warmups_since_clear": 12,
            "dist_since_clear": 310,
        }


def _encode(key: str, value: float) -> list[int]:
    """Inverse of the decoders in pids.py."""
    clamp = lambda v, hi=255: max(0, min(hi, int(round(v))))  # noqa: E731
    word = lambda v: [clamp(v, 0xFFFF) >> 8, clamp(v, 0xFFFF) & 0xFF]  # noqa: E731
    if key in ("engine_load", "throttle", "fuel_level"):
        return [clamp(value * 255 / 100)]
    if key in ("coolant_temp", "intake_temp", "ambient_temp"):
        return [clamp(value + 40)]
    if key.startswith(("stft", "ltft")):
        return [clamp(value * 128 / 100 + 128)]
    if key in ("map", "speed", "baro"):
        return [clamp(value)]
    if key == "rpm":
        return word(value * 4)
    if key == "timing_advance":
        return [clamp((value + 64) * 2)]
    if key == "maf":
        return word(value * 100)
    if key.startswith("o2_"):
        return [clamp(value * 200), 0x80]
    if key in ("run_time", "dist_mil_on", "dist_since_clear"):
        return word(value)
    if key == "warmups_since_clear":
        return [clamp(value)]
    if key == "module_voltage":
        return word(value * 1000)
    raise KeyError(key)


def _encode_dtc(code: str) -> list[int]:
    system = "PCBU".index(code[0])
    a = (system << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return [a, int(code[3:5], 16)]


class SimulatedTransport:
    """Implements the Transport protocol on top of a SimulatedVehicle."""

    def __init__(self, vehicle: SimulatedVehicle | None = None) -> None:
        self.vehicle = vehicle or SimulatedVehicle()
        self.echo = True
        self.spaces = True
        self.ignition_on = True
        self._pending = b""
        self._supported = {p.pid for p in PIDS.values()} | {0x01, 0x03}

    # Transport protocol --------------------------------------------------

    def write(self, data: bytes) -> None:
        cmd = data.decode("ascii").strip().upper().replace(" ", "")
        reply = self._handle(cmd)
        out = (cmd + "\r" if self.echo else "") + reply + "\r\r>"
        self._pending = out.encode("ascii")

    def read_until_prompt(self, timeout: float) -> bytes:
        data, self._pending = self._pending, b""
        return data

    def close(self) -> None:
        pass

    # Command handling ----------------------------------------------------

    def _hex(self, data: list[int]) -> str:
        sep = " " if self.spaces else ""
        return sep.join(f"{b:02X}" for b in data)

    def _multiframe(self, data: list[int]) -> str:
        lines = [f"{len(data):03X}"]
        first, rest = data[:6], data[6:]
        lines.append("0:" + self._hex(first))
        idx = 1
        while rest:
            lines.append(f"{idx % 16:X}:" + self._hex(rest[:7]))
            rest = rest[7:]
            idx += 1
        return "\r".join(lines)

    def _reply(self, data: list[int]) -> str:
        return self._hex(data) if len(data) <= 7 else self._multiframe(data)

    def _handle(self, cmd: str) -> str:
        if cmd.startswith("AT"):
            return self._at(cmd[2:])
        if not self.ignition_on:
            return "UNABLE TO CONNECT"
        try:
            mode = int(cmd[:2], 16)
        except ValueError:
            return "?"
        v = self.vehicle
        if mode == 0x01 and len(cmd) == 5:
            cmd = cmd[:4]  # trailing digit: number of replies to wait for
        if mode == 0x01 and len(cmd) == 4:
            pid = int(cmd[2:4], 16)
            if pid % 0x20 == 0:
                bitmap = 0
                for p in self._supported:
                    if pid < p <= pid + 0x20:
                        bitmap |= 1 << (32 - (p - pid))
                if pid + 0x20 in self._supported or any(p > pid + 0x20 for p in self._supported):
                    bitmap |= 1
                if pid and not any(pid < p <= pid + 0x20 for p in self._supported):
                    return "NO DATA"
                return self._reply([0x41, pid, *bitmap.to_bytes(4, "big")])
            if pid == 0x01:
                a = (0x80 if v.mil_on else 0) | len(v.stored_dtcs)
                return self._reply([0x41, 0x01, a, 0x07, v.monitors_available, v.monitors_incomplete])
            if pid == 0x03:
                return self._reply([0x41, 0x03, 0x02 if v.values()["coolant_temp"] > 40 else 0x01, 0x00])
            for key, p in PIDS.items():
                if p.pid == pid:
                    return self._reply([0x41, pid, *_encode(key, v.values()[key])])
            return "NO DATA"
        if mode in (0x03, 0x07, 0x0A):
            codes = {0x03: v.stored_dtcs, 0x07: v.pending_dtcs, 0x0A: v.permanent_dtcs}[mode]
            payload = [mode + 0x40, len(codes)]
            for c in codes:
                payload += _encode_dtc(c)
            return self._reply(payload)
        if mode == 0x04:
            v.stored_dtcs, v.pending_dtcs = [], []
            v.mil_on = False
            v.freeze_dtc, v.freeze_values = None, {}
            v.monitors_incomplete = v.monitors_available
            v.test_results = []
            return "44"
        if mode == 0x02 and len(cmd) == 6:
            return self._freeze_frame(int(cmd[2:4], 16))
        if mode == 0x06 and len(cmd) == 4:
            return self._test_results(int(cmd[2:4], 16))
        if cmd == "0902":
            return self._reply([0x49, 0x02, 0x01, *DEFAULT_VIN.encode("ascii")])
        return "NO DATA"

    @staticmethod
    def _bitmap(base: int, ids: set[int]) -> list[int] | None:
        """4-byte "supported" bitmap for ``base``, or None if nothing is at or above it."""
        bitmap = 0
        for i in ids:
            if base < i <= base + 0x20:
                bitmap |= 1 << (32 - (i - base))
        if any(i > base + 0x20 for i in ids):
            bitmap |= 1
        if base and not bitmap:
            return None
        return list(bitmap.to_bytes(4, "big"))

    def _freeze_frame(self, pid: int) -> str:
        v = self.vehicle
        ids = {PIDS[k].pid for k in v.freeze_values} | ({0x02} if v.freeze_dtc else set())
        if pid % 0x20 == 0:
            bitmap = self._bitmap(pid, ids)
            return self._reply([0x42, pid, 0x00, *bitmap]) if bitmap else "NO DATA"
        if pid == 0x02:
            return self._reply([0x42, 0x02, 0x00, *(_encode_dtc(v.freeze_dtc) if v.freeze_dtc else [0, 0])])
        for key, value in v.freeze_values.items():
            if PIDS[key].pid == pid:
                return self._reply([0x42, pid, 0x00, *_encode(key, value)])
        return "NO DATA"

    def _test_results(self, mid: int) -> str:
        results = self.vehicle.test_results
        if mid % 0x20 == 0:
            bitmap = self._bitmap(mid, {r[0] for r in results})
            return self._reply([0x46, mid, *bitmap]) if bitmap else "NO DATA"
        payload = [0x46]
        for m, tid, uas, value, lo, hi in results:
            if m == mid:
                if m in (0xA1, 0xA3) and tid == 0x0C:
                    value += self.vehicle.new_misfires()
                payload += [m, tid, uas, value >> 8, value & 0xFF, lo >> 8, lo & 0xFF, hi >> 8, hi & 0xFF]
        return self._reply(payload) if len(payload) > 1 else "NO DATA"

    def _at(self, cmd: str) -> str:
        if cmd in ("Z", "WS"):
            self.echo, self.spaces = True, True
            return "\r\rELM327 v1.5"
        if cmd == "I":
            return "ELM327 v1.5"
        if cmd in ("E0", "E1"):
            self.echo = cmd == "E1"
            return "OK"
        if cmd in ("S0", "S1"):
            self.spaces = cmd == "S1"
            return "OK"
        if cmd in ("L0", "L1", "H0", "H1") or cmd.startswith("SP") or cmd.startswith("ST"):
            return "OK"
        if cmd == "DPN":
            return "A6"
        if cmd == "RV":
            return f"{self.vehicle.values()['module_voltage'] - 0.1:.1f}V"
        return "?"
