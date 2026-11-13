"""Fuel economy estimated from engine readings.

The 3.8L has no mass air flow sensor, so air flow is worked out from
manifold pressure, RPM and intake temperature (the "speed-density" method
the PCM itself uses), and fuel flow follows from the air/fuel ratio the PCM
holds in closed loop.

This is an estimate. Two things are assumed, not measured:

* volumetric efficiency (how completely the cylinders fill), fixed at
  ``VOLUMETRIC_EFFICIENCY``; the real figure varies with RPM and load;
* the engine is at the ideal mixture, which is not true at wide open
  throttle (richer) or when coasting with the fuel cut off (no fuel).

Correct it against the pump with ``fuel_factor``: fill the tank, log every
drive, fill again, and divide the gallons the pump shows by the gallons
this estimated. The trend between drives is trustworthy before that; the
absolute number is not.
"""

from __future__ import annotations

from typing import Mapping, Sequence

DISPLACEMENT_L = 3.778        #: Chrysler 3.8L EGH V6
VOLUMETRIC_EFFICIENCY = 0.80  #: assumed; see module docstring
AIR_GAS_CONSTANT = 287.05     #: J/(kg K), dry air
AIR_FUEL_RATIO = 14.1         #: ideal mixture for E10 pump gasoline (pure gasoline is 14.7)
FUEL_DENSITY_G_PER_L = 745.0  #: gasoline, approximate
KM_PER_MILE = 1.609344
L_PER_GALLON = 3.785411784

BANDS = [("stopped", 0, 1), ("under 30 mph", 1, 48.3), ("30 to 55 mph", 48.3, 88.5), ("over 55 mph", 88.5, 1e9)]


def air_flow_gps(values: Mapping[str, float]) -> float | None:
    """Air entering the engine in grams per second, or None if it cannot be worked out."""
    if "maf" in values:
        return values["maf"]
    if not all(k in values for k in ("rpm", "map", "intake_temp")):
        return None
    # A four-stroke fills each cylinder once every two revolutions.
    litres_per_s = values["rpm"] / 120.0 * DISPLACEMENT_L * VOLUMETRIC_EFFICIENCY
    density_g_per_l = values["map"] * 1000.0 / (AIR_GAS_CONSTANT * (values["intake_temp"] + 273.15))
    return litres_per_s * density_g_per_l


def fuel_flow_lph(values: Mapping[str, float], fuel_factor: float = 1.0) -> float | None:
    """Fuel use in litres per hour."""
    air = air_flow_gps(values)
    if air is None:
        return None
    return air / AIR_FUEL_RATIO / FUEL_DENSITY_G_PER_L * 3600.0 * fuel_factor


def tire_speed_factor(actual_in: float, calibrated_in: float) -> float:
    """Correction for tires of a different diameter than the PCM is set up for."""
    return actual_in / calibrated_in


def summarize(
    samples: Sequence[tuple[float, Mapping[str, float]]],
    speed_factor: float = 1.0,
    fuel_factor: float = 1.0,
) -> dict | None:
    """Integrate distance and fuel over timestamped samples: ``[(seconds, values), ...]``.

    ``speed_factor`` multiplies the speed the PCM reports (see
    ``tire_speed_factor``). Returns None if the readings needed are missing.
    """
    fuel_l = dist_km = seconds = 0.0
    bands = {name: {"seconds": 0.0, "km": 0.0, "litres": 0.0} for name, _, _ in BANDS}
    for (t0, v0), (t1, _) in zip(samples, samples[1:]):
        dt = t1 - t0
        flow = fuel_flow_lph(v0, fuel_factor)
        if flow is None or "speed" not in v0 or dt <= 0 or dt > 30:
            continue
        if v0.get("rpm", 0) < 100:
            flow = 0.0  # engine off
        speed = v0["speed"] * speed_factor
        litres, km = flow * dt / 3600.0, speed * dt / 3600.0
        fuel_l, dist_km, seconds = fuel_l + litres, dist_km + km, seconds + dt
        for name, lo, hi in BANDS:
            if lo <= speed < hi:
                b = bands[name]
                b["seconds"], b["km"], b["litres"] = b["seconds"] + dt, b["km"] + km, b["litres"] + litres
    if seconds == 0:
        return None

    def mpg(km: float, litres: float) -> float | None:
        return round((km / KM_PER_MILE) / (litres / L_PER_GALLON), 1) if litres > 0 and km > 0 else None

    out = {
        "method": "measured air flow" if any("maf" in v for _, v in samples) else "estimated from MAP, RPM and intake temperature",
        "is_estimate": True,
        "speed_factor": round(speed_factor, 4),
        "fuel_factor": round(fuel_factor, 4),
        "seconds": round(seconds, 1),
        "distance_miles": round(dist_km / KM_PER_MILE, 2),
        "fuel_gallons": round(fuel_l / L_PER_GALLON, 3),
        "mpg": mpg(dist_km, fuel_l),
        "l_per_100km": round(fuel_l / dist_km * 100, 1) if dist_km > 0 else None,
        "average_fuel_gallons_per_hour": round(fuel_l / L_PER_GALLON / seconds * 3600, 2),
        "by_speed": {},
    }
    for name, b in bands.items():
        if b["seconds"] > 0:
            out["by_speed"][name] = {
                "share_of_time_pct": round(b["seconds"] / seconds * 100, 1),
                "share_of_fuel_pct": round(b["litres"] / fuel_l * 100, 1) if fuel_l > 0 else None,
                "miles": round(b["km"] / KM_PER_MILE, 2),
                "mpg": mpg(b["km"], b["litres"]),
                "gallons_per_hour": round(b["litres"] / L_PER_GALLON / b["seconds"] * 3600, 2),
            }
    return out


def to_markdown(e: dict) -> list[str]:
    out = [
        "## Fuel economy (estimate)", "",
        f"{e['distance_miles']} miles on {e['fuel_gallons']} gallons: **{e['mpg']} mpg** "
        f"({e['l_per_100km']} L/100 km). Fuel use averaged {e['average_fuel_gallons_per_hour']} gallons per hour.",
        "",
        f"Method: {e['method']}. Speed correction {e['speed_factor']}, fuel correction {e['fuel_factor']}. "
        "Absolute numbers are approximate until the fuel correction is set from a fill-up; "
        "comparisons between drives are sound.",
        "",
        "| Speed | Time (%) | Fuel (%) | Miles | mpg | Gallons per hour |",
        "|---|---|---|---|---|---|",
    ]
    for name, b in e["by_speed"].items():
        out.append(
            f"| {name} | {b['share_of_time_pct']} | {b['share_of_fuel_pct']} | {b['miles']} | "
            f"{b['mpg'] if b['mpg'] is not None else ''} | {b['gallons_per_hour']} |"
        )
    return out
