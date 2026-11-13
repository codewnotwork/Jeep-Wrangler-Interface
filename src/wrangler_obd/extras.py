"""Manufacturer readings to try, beyond the standard OBD-II set.

The identifiers and decodings come from the community-maintained OBDb
signal set for the Wrangler JK (https://github.com/OBDb/Jeep-Wrangler-JK,
CC BY-SA 4.0). That set covers every JK engine and year, and its own test
data shows a 2010 truck rejecting some of them, so each one is only a
candidate: it is asked for once, and reported only if the truck answers.

Mark an entry ``confirmed=True`` once it has been checked on the truck
against something independent (an infrared thermometer, a gauge, a scan tool).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .elm327 import ENGINE, TRANSMISSION, Elm327, ElmError


@dataclass(frozen=True)
class Extra:
    key: str
    name: str
    module: int
    service: int
    ident: int
    byte: int          #: index of the first data byte
    nbytes: int
    scale: float
    offset: float
    unit: str
    low: float         #: answers outside low..high are treated as "not this reading"
    high: float
    confirmed: bool = False

    def decode(self, data: Sequence[int]) -> float | None:
        if len(data) < self.byte + self.nbytes:
            return None
        raw = int.from_bytes(bytes(data[self.byte : self.byte + self.nbytes]), "big")
        value = raw * self.scale + self.offset
        return value if self.low <= value <= self.high else None


_F = 5.0 / 9.0  # the set gives some temperatures in °F; they are stored here in °C

EXTRAS = [
    Extra("trans_temp", "Transmission fluid temperature", TRANSMISSION, 0x21, 0x30, 11, 1, 1, -50, "°C", -40, 160),
    Extra("trans_temp_5c", "Transmission fluid temperature (variant 21 5C)", TRANSMISSION, 0x21, 0x5C, 4, 1, 1, -50, "°C", -40, 160),
    Extra("trans_temp_04fe", "Transmission fluid temperature (variant 22 04FE)", TRANSMISSION, 0x22, 0x04FE, 0, 1, 1, -40, "°C", -40, 160),
    Extra("oil_temp", "Engine oil temperature", ENGINE, 0x21, 0x2D, 7, 1, 1.8 * _F, (-83 - 32) * _F, "°C", -40, 160),
    Extra("oil_pressure", "Engine oil pressure", ENGINE, 0x22, 0x022A, 0, 1, 0.58 * 6.894757, 0, "kPa", 0, 900),
    Extra("knock_sensor_1", "Knock sensor 1", ENGINE, 0x22, 0x01A9, 0, 2, 1 / 13107, 0, "V", 0, 5),
    Extra("knock_sensor_2", "Knock sensor 2", ENGINE, 0x22, 0x01AA, 0, 2, 1 / 13107, 0, "V", 0, 5),
    Extra("knock_retard", "Short term knock retard", ENGINE, 0x22, 0x01AE, 0, 2, 0.5, 0, "°", 0, 30),
    Extra("ac_pressure", "A/C high side pressure", ENGINE, 0x22, 0x0329, 0, 1, 14, 0, "kPa", 0, 3570),
    Extra("injector_ms_cyl1", "Injector pulse width, cylinder 1", ENGINE, 0x22, 0x0234, 0, 4, 0.001, 0, "ms", 0, 50),
    Extra("injector_ms_cyl2", "Injector pulse width, cylinder 2", ENGINE, 0x22, 0x0235, 0, 4, 0.001, 0, "ms", 0, 50),
    Extra("injector_ms_cyl3", "Injector pulse width, cylinder 3", ENGINE, 0x22, 0x0236, 0, 4, 0.001, 0, "ms", 0, 50),
    Extra("injector_ms_cyl4", "Injector pulse width, cylinder 4", ENGINE, 0x22, 0x0237, 0, 4, 0.001, 0, "ms", 0, 50),
]


def probe(elm: Elm327) -> list[dict]:
    """Ask for every candidate once. Returns the ones the truck answered sensibly."""
    found = []
    for module in (ENGINE, TRANSMISSION):
        wanted = [e for e in EXTRAS if e.module == module]
        try:
            with elm.ecu(module):
                for e in wanted:
                    data = elm.read_data(e.service, e.ident)
                    value = e.decode(data) if data is not None else None
                    if value is not None:
                        found.append({
                            "key": e.key, "name": e.name, "value": round(value, 2), "unit": e.unit,
                            "request": f"{module:03X} {e.service:02X}{e.ident:0{2 if e.service == 0x21 else 4}X}",
                            "raw": bytes(data).hex().upper(), "confirmed": e.confirmed,
                        })
        except ElmError:
            continue  # this module does not take direct requests; the standard data is unaffected
    return found
