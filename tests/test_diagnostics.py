import json

import pytest

from wrangler_obd import Elm327, SimulatedTransport, SimulatedVehicle, alerts, report
from wrangler_obd.cli import main
from wrangler_obd.mode06 import decode_test_results


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def elm(clock):
    e = Elm327(SimulatedTransport(SimulatedVehicle(clock=clock, seed=1)))
    e.connect()
    return e


def test_freeze_frame(elm):
    frame = elm.freeze_frame()
    assert frame.dtc == "P0456"
    assert frame.readings["rpm"].value == 1850
    assert frame.readings["coolant_temp"].value == 91
    assert frame.readings["speed"].value == 72
    assert "module_voltage" not in frame.readings  # not part of the stored frame


def test_clear_resets_freeze_frame_readiness_and_tests(elm):
    assert all(m["complete"] for m in elm.monitor_status()["monitors"])
    elm.clear_dtcs()
    assert elm.freeze_frame() is None
    assert elm.test_results() == []
    monitors = {m["name"]: m["complete"] for m in elm.monitor_status()["monitors"]}
    assert monitors["Misfire"] is True  # continuous monitors stay complete
    assert monitors["Catalyst"] is False and monitors["Evaporative system"] is False


def test_fuel_system_status(elm, clock):
    assert elm.fuel_system_status().startswith("Open loop")
    clock.t = 200
    assert elm.fuel_system_status().startswith("Closed loop")


def test_mode06_results(elm):
    results = {(r.mid, r.tid): r for r in elm.test_results()}
    assert len(results) == 12
    o2 = results[(0x01, 0x01)]
    assert o2.value == pytest.approx(0.61) and o2.unit == "V" and o2.passed
    assert o2.margin == pytest.approx(0.19 / 0.8)
    evap = results[(0x3C, 0x80)]
    assert evap.value == -5 and evap.unit == "Pa" and not evap.passed
    miss = results[(0xA3, 0x0C)]
    assert miss.value == 3 and miss.monitor == "Misfire, cylinder 2"
    assert miss.open_ended and miss.margin is None


def test_mode06_unknown_unit_stays_raw():
    (r,) = decode_test_results([0x21, 0x81, 0x7A, 0x00, 0x10, 0x00, 0x00, 0x00, 0x20])
    assert not r.scaled and (r.value, r.minimum, r.maximum) == (16, 0, 32) and r.passed
    assert r.as_dict()["unit"] == "raw (unit id 7A)"


def test_alerts():
    assert alerts.check({"rpm": 700, "coolant_temp": 95, "module_voltage": 14.1, "ltft_b1": 3, "stft_b1": 1}) == []
    found = alerts.check({"rpm": 2000, "coolant_temp": 119, "module_voltage": 12.4, "ltft_b1": 18, "stft_b1": 5})
    assert {(a.key, a.severity) for a in found} == {
        ("coolant_temp", "critical"), ("module_voltage", "warning"), ("fuel_trim_b1", "critical"),
    }
    assert alerts.check({"rpm": 0, "module_voltage": 12.4}) == []  # engine off, healthy battery
    assert alerts.check({"ltft_b2": -12})[0].message.endswith("(running rich)")


def test_summarize_log_segments():
    idle = [{"rpm": 700, "speed": 0, "coolant_temp": 92, "throttle": 14, "ltft_b1": 14, "stft_b1": 2,
             "o2_b1s1": 0.2 + 0.5 * (i % 2)} for i in range(20)]
    cruise = [{"rpm": 2100, "speed": 80, "coolant_temp": 93, "throttle": 25, "ltft_b1": 3, "stft_b1": 0}] * 20
    cold = [{"rpm": 900, "speed": 0, "coolant_temp": 30, "throttle": 14, "ltft_b1": 0, "stft_b1": 0}] * 5
    s = report.summarize_log(cold + idle + cruise, duration_s=45)
    assert s["samples"] == 45
    assert s["segments"]["idle"]["samples"] == 20 and s["segments"]["cruise"]["samples"] == 20
    assert s["segments"]["idle"]["ltft_b1"]["mean"] == 14
    assert s["o2_switching"]["o2_b1s1"]["crossings_of_0.45V"] == 19
    # Lean at idle only: one finding, and it names the segment.
    assert [f["message"][:8] for f in s["findings"]] == ["At idle:"]
    assert report.summarize_log([]) == {"samples": 0}


def test_build_report(elm):
    data = report.build_report(elm, notes="rough idle")
    assert data["schema"] == report.SCHEMA
    assert data["trouble_codes"]["stored"] == [{"code": "P0456", "description": "EVAP very small leak detected"}]
    assert data["freeze_frame"]["code"] == "P0456"
    assert data["readiness"]["ready_for_emissions_test"] is True
    assert data["snapshot"]["rpm"]["unit"] == "rpm"
    keys = {f["key"] for f in data["findings"]}
    assert {"mode06_3C_80", "misfire_A3"} <= keys
    json.loads(report.to_json(data))  # serialisable
    md = report.to_markdown(data)
    for text in ("rough idle", "P0171", "Conditions when P0456", "Misfire, cylinder 2", "FAIL"):
        assert text in md


def test_cli_diagnostics(tmp_path, capsys):
    for cmd in (["freeze"], ["readiness"], ["tests"]):
        assert main(["--sim", *cmd]) == 0
    out = capsys.readouterr().out
    assert "Conditions when P0456 set" in out and "Ready for an emissions test" in out and "FAIL" in out
    assert main(["--sim", "report", "--out", str(tmp_path / "r"), "--duration", "0.05", "--notes", "hi"]) == 0
    data = json.loads((tmp_path / "r" / "report.json").read_text(encoding="utf-8"))
    assert data["log"]["file"] == "log.csv" and data["log"]["samples"] >= 1
    assert "fuel_level" not in data["log"]["overall"] and "rpm" in data["log"]["overall"]
    assert (tmp_path / "r" / "report.md").exists() and (tmp_path / "r" / "log.csv").exists()
