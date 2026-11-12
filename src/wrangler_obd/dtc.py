"""Diagnostic trouble code decoding and descriptions."""

from __future__ import annotations

from typing import Sequence

_SYSTEMS = "PCBU"

# Generic SAE codes plus ones that come up often on the 2007-2011 JK 3.8L.
DESCRIPTIONS: dict[str, str] = {
    "P0016": "Crankshaft/camshaft position correlation, bank 1",
    "P0106": "MAP sensor range/performance",
    "P0107": "MAP sensor circuit low",
    "P0108": "MAP sensor circuit high",
    "P0112": "Intake air temperature sensor circuit low",
    "P0113": "Intake air temperature sensor circuit high",
    "P0117": "Engine coolant temperature sensor circuit low",
    "P0118": "Engine coolant temperature sensor circuit high",
    "P0121": "Throttle position sensor range/performance",
    "P0128": "Coolant temperature below thermostat regulating temperature",
    "P0131": "O2 sensor circuit low voltage, bank 1 sensor 1",
    "P0133": "O2 sensor slow response, bank 1 sensor 1",
    "P0135": "O2 sensor heater circuit, bank 1 sensor 1",
    "P0141": "O2 sensor heater circuit, bank 1 sensor 2",
    "P0151": "O2 sensor circuit low voltage, bank 2 sensor 1",
    "P0155": "O2 sensor heater circuit, bank 2 sensor 1",
    "P0161": "O2 sensor heater circuit, bank 2 sensor 2",
    "P0171": "System too lean, bank 1",
    "P0172": "System too rich, bank 1",
    "P0174": "System too lean, bank 2",
    "P0175": "System too rich, bank 2",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0305": "Cylinder 5 misfire detected",
    "P0306": "Cylinder 6 misfire detected",
    "P0335": "Crankshaft position sensor A circuit",
    "P0340": "Camshaft position sensor A circuit, bank 1",
    "P0401": "EGR flow insufficient",
    "P0420": "Catalyst efficiency below threshold, bank 1",
    "P0430": "Catalyst efficiency below threshold, bank 2",
    "P0440": "EVAP system malfunction",
    "P0441": "EVAP incorrect purge flow",
    "P0442": "EVAP small leak detected",
    "P0455": "EVAP large leak detected",
    "P0456": "EVAP very small leak detected",
    "P0457": "EVAP leak detected (fuel cap loose/off)",
    "P0500": "Vehicle speed sensor",
    "P0505": "Idle air control system",
    "P0506": "Idle RPM lower than expected",
    "P0507": "Idle RPM higher than expected",
    "P0513": "Invalid SKIM key",
    "P0522": "Oil pressure sensor circuit low",
    "P0523": "Oil pressure sensor circuit high",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P0600": "Serial communication link",
    "P0700": "Transmission control system (check TCM codes)",
    "P1281": "Engine is cold too long (Chrysler)",
    "U0100": "Lost communication with ECM/PCM",
    "U0101": "Lost communication with TCM",
    "U0121": "Lost communication with ABS module",
    "U0155": "Lost communication with instrument cluster",
}


def decode_dtc(a: int, b: int) -> str | None:
    """Turn two raw bytes into a code like ``P0456``. Returns None for padding (0000)."""
    if a == 0 and b == 0:
        return None
    system = _SYSTEMS[(a >> 6) & 0x03]
    return f"{system}{(a >> 4) & 0x03}{a & 0x0F:X}{b:02X}"


def decode_dtc_payload(payload: Sequence[int]) -> list[str]:
    """Decode the bytes after the mode byte of a Mode 03/07/0A response.

    CAN (ISO 15765) responses start with a count byte, so the payload
    length is odd. Older protocols send three pairs per frame with no count.
    """
    data = list(payload)
    if len(data) % 2 == 1:
        data = data[1:]
    codes = []
    for i in range(0, len(data) - 1, 2):
        code = decode_dtc(data[i], data[i + 1])
        if code:
            codes.append(code)
    return codes


def describe(code: str) -> str:
    if code in DESCRIPTIONS:
        return DESCRIPTIONS[code]
    if code[1] in "13":
        return "Manufacturer-specific code (look up in a Chrysler/Jeep reference)"
    return "No description on file"
