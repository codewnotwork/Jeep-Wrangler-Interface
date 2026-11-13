"""ELM327 adapter driver: setup, request/response parsing and OBD services."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import dtc as dtc_mod
from . import mode06
from .pids import PIDS, PIDS_BY_NUMBER, Pid, decode_fuel_system, decode_monitor_status, decode_supported
from .transport import Transport, TransportError

_HEX_LINE = re.compile(r"^[0-9A-F]+$")
_FRAME_INDEX = re.compile(r"^([0-9A-F]):([0-9A-F]+)$")
_BYTE_COUNT = re.compile(r"^[0-9A-F]{3}$")

# Lines the ELM327 prints instead of data.
_NO_DATA = {"NODATA", "SEARCHING...", "STOPPED", "?"}
_ERRORS = ("UNABLETOCONNECT", "BUSERROR", "CANERROR", "BUSINIT:...ERROR", "FBERROR", "DATAERROR", "BUFFERFULL")

PROTOCOLS = {
    "0": "Automatic",
    "1": "SAE J1850 PWM",
    "2": "SAE J1850 VPW",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN",
}


class ElmError(RuntimeError):
    """The adapter or vehicle reported an error."""


@dataclass
class Reading:
    pid: Pid
    value: float

    @property
    def unit(self) -> str:
        return self.pid.unit


@dataclass
class FreezeFrame:
    """Sensor values the PCM saved at the moment a trouble code set."""

    dtc: str
    readings: dict[str, Reading] = field(default_factory=dict)


@dataclass
class DtcReport:
    stored: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    permanent: list[str] = field(default_factory=list)
    mil_on: bool | None = None


def parse_response(raw: str) -> list[list[int]]:
    """Split raw ELM327 output into one byte list per ECU message.

    Handles single-frame replies (one line per ECU) and ISO-TP multi-frame
    replies (a 3-digit byte count, then ``0:``, ``1:`` ... lines).
    Assumes headers off. Spaces are ignored.
    """
    lines = []
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.replace(" ", "").replace(">", "").strip().upper()
        if line:
            lines.append(line)

    for line in lines:
        if any(line.startswith(e) for e in _ERRORS):
            raise ElmError(line)

    messages: list[list[int]] = []
    multi: list[int] | None = None
    multi_len = 0
    for line in lines:
        if line in _NO_DATA:
            continue
        if _BYTE_COUNT.match(line):
            if multi:
                messages.append(multi[:multi_len])
            multi, multi_len = [], int(line, 16)
            continue
        m = _FRAME_INDEX.match(line)
        if m and multi is not None:
            multi.extend(bytes.fromhex(m.group(2)))
            continue
        if _HEX_LINE.match(line) and len(line) % 2 == 0:
            messages.append(list(bytes.fromhex(line)))
    if multi:
        messages.append(multi[:multi_len])
    return messages


class Elm327:
    """High-level connection to the vehicle through an ELM327 adapter."""

    def __init__(self, transport: Transport, timeout: float = 5.0) -> None:
        self.transport = transport
        self.timeout = timeout
        self.version = ""
        self.protocol = ""
        self._supported: set[int] | None = None

    # -- low level -------------------------------------------------------

    def command(self, cmd: str, timeout: float | None = None) -> str:
        """Send one command and return the raw text before the prompt."""
        self.transport.write((cmd + "\r").encode("ascii"))
        raw = self.transport.read_until_prompt(timeout or self.timeout)
        text = raw.decode("ascii", errors="replace")
        # Drop an echoed copy of the command if echo is still on.
        lines = [ln for ln in text.replace("\r", "\n").split("\n") if ln.strip() and ln.strip() != cmd]
        return "\n".join(lines).replace(">", "").strip()

    def query(self, cmd: str, timeout: float | None = None) -> list[list[int]]:
        """Send an OBD request (hex string like ``010C``) and return parsed messages."""
        return parse_response(self.command(cmd, timeout))

    def _service_response(self, cmd: str, timeout: float | None = None) -> list[list[int]]:
        """Return messages whose first byte is the positive reply to ``cmd``'s mode."""
        mode = int(cmd[:2], 16)
        return [m for m in self.query(cmd, timeout) if m and m[0] == mode + 0x40]

    # -- setup -----------------------------------------------------------

    def connect(self) -> None:
        """Reset the adapter and configure it for parsing, then find the protocol."""
        self.version = self.command("ATZ", timeout=max(self.timeout, 5.0)).splitlines()[-1].strip()
        for cmd in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
            reply = self.command(cmd)
            if "OK" not in reply:
                raise ElmError(f"adapter rejected {cmd}: {reply!r}")
        # 0100 forces protocol detection; the first one can take several seconds.
        if not self._service_response("0100", timeout=max(self.timeout, 10.0)):
            raise ElmError("vehicle did not answer. Is the ignition on?")
        code = self.command("ATDPN").lstrip("A")
        self.protocol = PROTOCOLS.get(code, f"unknown ({code})")

    def close(self) -> None:
        self.transport.close()

    def battery_voltage(self) -> float | None:
        """Voltage at the OBD port as measured by the adapter (ATRV)."""
        m = re.search(r"(\d+(?:\.\d+)?)", self.command("ATRV"))
        return float(m.group(1)) if m else None

    # -- Mode 01 ---------------------------------------------------------

    def supported_pids(self) -> set[int]:
        if self._supported is None:
            supported: set[int] = set()
            base = 0x00
            while base <= 0xC0:
                msgs = self._service_response(f"01{base:02X}")
                if not msgs:
                    break
                for m in msgs:
                    if len(m) >= 6 and m[1] == base:
                        supported |= decode_supported(base, m[2:6])
                if base + 0x20 not in supported:
                    break
                base += 0x20
            self._supported = supported
        return self._supported

    def read_pid(self, key: str) -> Reading | None:
        """Read one PID by key (see ``pids.PIDS``). Returns None if unsupported."""
        pid = PIDS[key]
        for m in self._service_response(pid.command):
            if len(m) >= 2 + pid.nbytes and m[1] == pid.pid:
                return Reading(pid, pid.decode(m[2 : 2 + pid.nbytes]))
        return None

    def read_pids(self, keys: list[str]) -> dict[str, Reading]:
        supported = self.supported_pids()
        out = {}
        for key in keys:
            if PIDS[key].pid in supported:
                reading = self.read_pid(key)
                if reading is not None:
                    out[key] = reading
        return out

    def monitor_status(self) -> dict | None:
        for m in self._service_response("0101"):
            if len(m) >= 6 and m[1] == 0x01:
                return decode_monitor_status(m[2:6])
        return None

    def fuel_system_status(self) -> str | None:
        """Whether the PCM is in open or closed loop fuel control (PID 03)."""
        if 0x03 not in self.supported_pids():
            return None
        for m in self._service_response("0103"):
            if len(m) >= 3 and m[1] == 0x03:
                return decode_fuel_system(m[2:])
        return None

    # -- Mode 02: freeze frame -------------------------------------------

    def _supported_ids(self, prefix: str, suffix: str, skip: int) -> set[int]:
        """Walk the "supported" bitmaps of a service (ids 00, 20, 40 ...)."""
        mode = int(prefix, 16)
        supported: set[int] = set()
        base = 0x00
        while base <= 0xE0:
            found = False
            for m in self._service_response(f"{prefix}{base:02X}{suffix}"):
                if len(m) >= skip + 4 and m[0] == mode + 0x40 and m[1] == base:
                    supported |= decode_supported(base, m[skip : skip + 4])
                    found = True
            if not found or base + 0x20 not in supported:
                break
            base += 0x20
        return supported

    def freeze_frame(self) -> FreezeFrame | None:
        """Read freeze frame 0. Returns None when no frame is stored."""
        dtc = None
        for m in self._service_response("020200"):
            if len(m) >= 5 and m[1] == 0x02:
                dtc = dtc_mod.decode_dtc(m[3], m[4])
        if dtc is None:
            return None
        frame = FreezeFrame(dtc)
        for number in sorted(self._supported_ids("02", "00", skip=3)):
            pid = PIDS_BY_NUMBER.get(number)
            if pid is None:
                continue
            for m in self._service_response(f"02{number:02X}00"):
                if len(m) >= 3 + pid.nbytes and m[1] == number:
                    frame.readings[pid.key] = Reading(pid, pid.decode(m[3 : 3 + pid.nbytes]))
        return frame

    # -- Mode 06: on-board test results ----------------------------------

    def test_results(self) -> list[mode06.TestResult]:
        """Read every on-board monitoring test result the PCM reports."""
        results: list[mode06.TestResult] = []
        for mid in sorted(self._supported_ids("06", "", skip=2)):
            if mid % 0x20 == 0:
                continue
            for m in self._service_response(f"06{mid:02X}"):
                results.extend(r for r in mode06.decode_test_results(m[1:]) if r.mid == mid)
        return results

    # -- DTCs ------------------------------------------------------------

    def _read_dtcs(self, mode: str) -> list[str]:
        codes: list[str] = []
        for m in self._service_response(mode):
            for code in dtc_mod.decode_dtc_payload(m[1:]):
                if code not in codes:
                    codes.append(code)
        return codes

    def read_dtcs(self) -> DtcReport:
        status = self.monitor_status()
        return DtcReport(
            stored=self._read_dtcs("03"),
            pending=self._read_dtcs("07"),
            permanent=self._read_dtcs("0A"),
            mil_on=status["mil_on"] if status else None,
        )

    def clear_dtcs(self) -> None:
        """Mode 04: clear codes, freeze frame and readiness monitors. Engine off, key on."""
        if not self._service_response("04", timeout=max(self.timeout, 10.0)):
            raise ElmError("vehicle did not confirm the clear")

    # -- Mode 09 ---------------------------------------------------------

    def vin(self) -> str | None:
        try:
            msgs = self._service_response("0902")
        except (ElmError, TransportError):
            return None
        for m in msgs:
            if len(m) >= 3 and m[1] == 0x02:
                body = bytes(b for b in m[3:] if 0x20 < b < 0x7F)
                return body.decode("ascii")[-17:] or None
        return None
