import io

import pytest

from wrangler_obd import Elm327, SimulatedTransport, SimulatedVehicle
from wrangler_obd.cli import main
from wrangler_obd.elm327 import ElmError
from wrangler_obd.logger import CsvLogger, record


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


def test_connect_detects_can(elm):
    assert elm.version.startswith("ELM327")
    assert "CAN" in elm.protocol
    assert elm.transport.echo is False and elm.transport.spaces is False


def test_supported_pids_cover_core_set(elm):
    s = elm.supported_pids()
    assert {0x05, 0x0C, 0x0D, 0x42, 0x46} <= s


def test_live_values_track_simulated_engine(elm, clock):
    idle = elm.read_pids(["rpm", "coolant_temp", "speed"])
    assert 600 < idle["rpm"].value < 760
    assert idle["speed"].value == 0
    clock.t = 200
    warm = elm.read_pid("coolant_temp")
    assert warm.value == 96


def test_dtc_read_and_clear(elm):
    report = elm.read_dtcs()
    assert report.stored == ["P0456"]
    assert report.pending == ["P0171"]
    assert report.permanent == ["P0456"]
    assert report.mil_on is True
    elm.clear_dtcs()
    after = elm.read_dtcs()
    assert after.stored == [] and after.pending == []
    assert after.permanent == ["P0456"]
    assert after.mil_on is False


def test_vin_multiframe(elm):
    assert elm.vin() == "1J4AA2D10AL000000"


def test_ignition_off_fails_cleanly():
    t = SimulatedTransport()
    t.ignition_on = False
    with pytest.raises(ElmError):
        Elm327(t).connect()


def test_battery_voltage(elm):
    assert 13 < elm.battery_voltage() < 15


def test_csv_logging(elm):
    buf = io.StringIO()
    logger = CsvLogger(buf, ["rpm", "coolant_temp"])
    n = record(elm, logger, interval=0, duration=0.05, sleep=lambda s: None)
    lines = buf.getvalue().strip().splitlines()
    assert lines[0] == "timestamp,elapsed_s,rpm (rpm),coolant_temp (°C)"
    assert len(lines) == n + 1 and n >= 1


def test_cli_smoke(capsys):
    for cmd in (["info"], ["pids"], ["live", "--once"], ["dtc"], ["clear", "--yes"]):
        assert main(["--sim", *cmd]) == 0
    out = capsys.readouterr().out
    assert "P0456" in out and "Engine RPM" in out


def test_cli_rejects_unknown_pid():
    with pytest.raises(SystemExit):
        main(["--sim", "live", "--pids", "boost"])
