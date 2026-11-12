"""Byte transports for talking to an ELM327 adapter.

Every transport exposes the same two calls: ``write`` sends a command and
``read_until_prompt`` returns everything up to the ELM327 ``>`` prompt.
"""

from __future__ import annotations

import socket
import time
from typing import Protocol

PROMPT = b">"


class TransportError(IOError):
    """Raised when the adapter cannot be reached or stops answering."""


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read_until_prompt(self, timeout: float) -> bytes: ...

    def close(self) -> None: ...


#: Rates tried by auto-detection, most common first. OBDLink adapters default to
#: 115200, most ELM327 clones to 38400.
BAUD_RATES = (115200, 38400, 9600, 230400, 57600, 500000)


def detect_baud(ser, rates=BAUD_RATES, probe_timeout: float = 0.5) -> int | None:
    """Find the rate at which the adapter answers with a prompt. Leaves ``ser`` set to it."""
    for rate in rates:
        ser.baudrate = rate
        ser.reset_input_buffer()
        # Two DEL bytes abort any half-sent command; the ELM327 answers CR with a prompt.
        ser.write(b"\x7f\x7f\r")
        ser.flush()
        buf = bytearray()
        deadline = time.monotonic() + probe_timeout
        while time.monotonic() < deadline:
            buf.extend(ser.read(ser.in_waiting or 1))
            if PROMPT in buf:
                return rate
    return None


class SerialTransport:
    """USB or Bluetooth (rfcomm / COM port) ELM327 adapter.

    ``baudrate=None`` auto-detects the adapter's rate.
    """

    def __init__(self, port: str, baudrate: int | None = None) -> None:
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TransportError("pyserial is required for serial adapters: pip install pyserial") from exc
        try:
            self._ser = serial.Serial(port, baudrate=baudrate or BAUD_RATES[0], timeout=0.1)
        except serial.SerialException as exc:
            raise TransportError(f"cannot open {port}: {exc}") from exc
        if baudrate is None:
            found = detect_baud(self._ser)
            if found is None:
                self._ser.close()
                raise TransportError(
                    f"no ELM327 answered on {port} at {', '.join(map(str, BAUD_RATES))} baud; "
                    "check the adapter is plugged in and the key is on"
                )
        self.baudrate = self._ser.baudrate

    def write(self, data: bytes) -> None:
        self._ser.reset_input_buffer()
        self._ser.write(data)
        self._ser.flush()

    def read_until_prompt(self, timeout: float) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf.extend(chunk)
                if PROMPT in chunk:
                    return bytes(buf)
        raise TransportError(f"timed out waiting for adapter prompt (got {bytes(buf)!r})")

    def close(self) -> None:
        self._ser.close()


class TcpTransport:
    """Wi-Fi ELM327 adapter (usually 192.168.0.10:35000)."""

    def __init__(self, host: str, port: int = 35000, connect_timeout: float = 5.0) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as exc:
            raise TransportError(f"cannot connect to {host}:{port}: {exc}") from exc

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read_until_prompt(self, timeout: float) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(f"timed out waiting for adapter prompt (got {bytes(buf)!r})")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout:
                continue
            if not chunk:
                raise TransportError("adapter closed the connection")
            buf.extend(chunk)
            if PROMPT in chunk:
                return bytes(buf)

    def close(self) -> None:
        self._sock.close()
