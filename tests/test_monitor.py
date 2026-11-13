import json
import time

import pytest

from wrangler_obd import troubleshoot
from wrangler_obd.app import App
from wrangler_obd.elm327 import Elm327
from wrangler_obd.monitor import Monitor
from wrangler_obd.simulator import SimulatedTransport, SimulatedVehicle


def series(seconds, **values):
    """One sample per second with fixed values."""
    return [(float(t), dict(values)) for t in range(seconds)]


IDLE = dict(rpm=700.0, speed=0.0, coolant_temp=92.0, throttle=14.0, module_voltage=14.1)
CRUISE = dict(rpm=2000.0, speed=80.0, coolant_temp=93.0, throttle=22.0, module_voltage=14.1)


def shift(samples, by):
    return [(t + by, v) for t, v in samples]


def ids(result):
    return [c["id"] for c in result["causes"]]


def test_healthy_engine_has_no_causes():
    data = series(60, **IDLE, ltft_b1=2.0, stft_b1=1.0, ltft_b2=1.0, stft_b2=-1.0)
    data += shift(series(60, **CRUISE, ltft_b1=1.0, stft_b1=0.0, ltft_b2=1.0, stft_b2=0.0), 60)
    r = troubleshoot.evaluate(data)
    assert r["causes"] == [] and r["notes"] == []
    assert r["coverage"]["warm_idle_s"] >= 55 and r["coverage"]["cruise_s"] >= 55


def test_vacuum_leak_versus_fuel_delivery():
    lean_idle = series(60, **IDLE, ltft_b1=12.0, stft_b1=3.0, ltft_b2=11.0, stft_b2=2.0)
    ok_cruise = shift(series(60, **CRUISE, ltft_b1=2.0, stft_b1=0.0, ltft_b2=2.0, stft_b2=0.0), 60)
    r = troubleshoot.evaluate(lean_idle + ok_cruise, stored_codes=["P0171"])
    assert ids(r)[0] == "vacuum_leak" and r["causes"][0]["score"] == 90
    assert "P0171" in " ".join(r["causes"][0]["evidence"])

    lean_cruise = shift(series(60, **CRUISE, ltft_b1=12.0, stft_b1=4.0, ltft_b2=13.0, stft_b2=3.0), 60)
    r = troubleshoot.evaluate(lean_idle + lean_cruise)
    assert ids(r) == ["fuel_delivery"]

    r = troubleshoot.evaluate(lean_idle)  # no cruise yet: it says what it needs
    assert ids(r) == ["lean_idle"] and any("Cruise checks need" in n for n in r["notes"])


def test_bank_split_and_rich():
    r = troubleshoot.evaluate(series(60, **IDLE, ltft_b1=11.0, stft_b1=2.0, ltft_b2=1.0, stft_b2=0.0))
    assert ids(r) == ["bank_split"] and "bank 1" in r["causes"][0]["title"]
    r = troubleshoot.evaluate(series(60, **IDLE, ltft_b1=-12.0, stft_b1=-2.0, ltft_b2=-11.0, stft_b2=-1.0))
    assert ids(r) == ["rich"]


def test_cooling_charging_and_transmission():
    cold = dict(CRUISE, coolant_temp=70.0)
    r = troubleshoot.evaluate(series(120, **cold, run_time=1200.0))
    assert ids(r) == ["thermostat"]
    r = troubleshoot.evaluate(series(60, **dict(IDLE, coolant_temp=119.0)))
    assert ids(r)[0] == "overheat" and r["causes"][0]["score"] == 95
    r = troubleshoot.evaluate(series(60, **dict(IDLE, module_voltage=12.5)))
    assert ids(r) == ["charging_low"]
    r = troubleshoot.evaluate(series(60, **IDLE, x_trans_temp=125.0))
    assert ids(r) == ["trans_hot"]


def test_o2_catalyst_egr_misfire():
    stuck = series(60, **IDLE, o2_b1s1=0.1, o2_b2s1=0.1)
    for i, (_, v) in enumerate(stuck):
        v["o2_b2s1"] = 0.1 + 0.8 * (i % 2)  # bank 2 switches, bank 1 does not
    assert ids(troubleshoot.evaluate(stuck)) == ["o2_lazy_o2_b1s1"]

    worn = series(60, **CRUISE)
    for i, (_, v) in enumerate(worn):
        v["o2_b1s1"] = 0.1 + 0.8 * (i % 2)
        v["o2_b1s2"] = 0.15 + 0.7 * (i % 2)
    r = troubleshoot.evaluate(worn, stored_codes=["P0420"])
    assert ids(r) == ["catalyst_o2_b1s2"] and r["causes"][0]["score"] == 80

    assert ids(troubleshoot.evaluate(series(60, **IDLE, egr_cmd=10.0))) == ["egr_idle"]
    r = troubleshoot.evaluate(series(5, **IDLE), pending_codes=["P0302"])
    assert ids(r) == ["misfire_codes"] and "cylinder 2" in r["causes"][0]["title"]


@pytest.fixture
def elm():
    e = Elm327(SimulatedTransport(SimulatedVehicle(seed=2)))
    e.connect()
    return e


def test_monitor_records_everything_and_saves(elm, tmp_path):
    m = Monitor(elm, tmp_path / "run", notes="test run")
    m.start()
    time.sleep(0.6)
    m.add_marker("changed plugs")
    time.sleep(0.5)
    view = m.display()
    assert view["running"] and view["count"] > 5
    keys = {c["key"] for c in view["catalog"]}
    assert {"rpm", "coolant_temp", "evap_purge", "fuel_level", "x_trans_temp", "x_oil_temp"} <= keys
    units = {c["key"]: c["unit"] for c in view["catalog"]}
    assert units["coolant_temp"] == "°F" and units["speed"] == "mph" and units["x_trans_temp"] == "°F"
    t, first = view["samples"][0]
    assert 60 < first["coolant_temp"] < 80 and "x_trans_temp" in first and "fuel_level" in first  # 20 °C shown as 68 °F
    later = m.display(since=view["samples"][-1][0])
    assert all(s[0] > view["samples"][-1][0] for s in later["samples"])
    m.stop()
    m.join(20)
    assert not m.running and m.error is None
    header = (tmp_path / "run" / "log.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "rpm (rpm)" in header and "x_trans_temp (°C)" in header
    doc = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert doc["markers"][0]["text"] == "changed plugs" and "likely_causes" in doc
    assert [s["after_marker"] for s in doc["segments_between_markers"]] == ["Start", "changed plugs"]
    assert "x_trans_temp" in doc["log"]["overall"]
    md = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
    for text in ("## Likely causes", "## Before and after each marker", "changed plugs", "Transmission fluid temperature"):
        assert text in md


def test_monitor_saves_when_the_truck_goes_away(elm, tmp_path):
    m = Monitor(elm, tmp_path / "run")
    m.start()
    time.sleep(0.5)
    elm.transport.ignition_on = False  # key turned off mid-recording
    m.join(20)
    assert not m.running and "Lost contact" in m.error
    doc = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert doc["vehicle"]["vin"] is None and doc["log"]["samples"] > 3
    assert "# Diagnostic report" in (tmp_path / "run" / "report.md").read_text(encoding="utf-8")


def test_app_monitor_and_layout(tmp_path):
    app = App(tmp_path / "d")
    with pytest.raises(RuntimeError):
        app.start_monitor()
    app.connect("sim")
    app.start_monitor(notes="hello")
    assert app.busy() and app.live() == {}
    with pytest.raises(RuntimeError, match="still running"):
        app.start("pedal", {})
    time.sleep(0.4)
    app.stop_monitor()
    app.monitor.join(20)
    assert not app.busy() and (app.monitor.out / "report.md").exists()
    assert app.layout() == {}
    app.save_layout({"tiles": ["rpm", "x_trans_temp"], "window": 60})
    assert app.layout() == {"tiles": ["rpm", "x_trans_temp"], "window": 60}
    app.disconnect()
