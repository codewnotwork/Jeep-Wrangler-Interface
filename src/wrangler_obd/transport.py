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


class SerialTransport:
    """USB or Bluetooth (rfcomm / COM port) ELM327 adapter."""

    def __init__(self, port: str, baudrate: int = 38400) -> None:
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TransportError("pyserial is required for serial adapters: pip install pyserial") from exc
        try:
            self._ser = serial.Serial(port, baudrate=baudrate, timeout=0.1)
        except serial.SerialException as exc:
            raise TransportError(f"cannot open {port}: {exc}") from exc

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
