import json
import socket
import threading
import time

import pytest

from wrangler_obd import carlogger, pisetup, receiver, sync
from wrangler_obd.elm327 import Elm327
from wrangler_obd.simulator import SimulatedTransport, SimulatedVehicle

TOKEN = "test-token"


class StoppableVehicle(SimulatedVehicle):
    """The simulator, with an ignition: after ``engine_off`` is set the engine reads 0 rpm."""

    def __init__(self):
        super().__init__(seed=1)
        self.engine_off = False

    def values(self):
        v = super().values()
        if self.engine_off:
            v.update(rpm=0.0, speed=0.0)
        return v


@pytest.fixture
def pc(tmp_path):
    """A receiver on this machine, as the PC would run it."""
    data = tmp_path / "pc"
    data.mkdir()
    (data / receiver.PAIRING_FILE).write_text(json.dumps({"token": TOKEN}))
    server, _ = receiver.serve(data, host="127.0.0.1", port=0, discovery_port=None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield data, server.server_address[1]
    server.shutdown()
    server.server_close()


def make_drive(root, name="pi-20261120-081500", text="hello"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "log.csv").write_text("timestamp,elapsed_s,rpm (rpm)\n")
    (folder / "report.md").write_text(text)
    (folder / ".sent-not-a-real-file").write_text("skipped: hidden")
    return folder


def test_upload_files_a_drive_once(tmp_path, pc):
    data, port = pc
    up = sync.Uploader(TOKEN, ["127.0.0.1"], port=port)
    assert up.find() == f"http://127.0.0.1:{port}"
    folder = make_drive(tmp_path / "pi")
    start = time.mktime((2026, 11, 20, 8, 15, 0, 0, 0, -1))
    name = up.upload_drive(folder, folder.name, {"start_epoch": start, "end_reason": "engine off"})
    assert name == "2026-11-20 0815 jeep drive"
    got = data / name
    assert (got / "report.md").read_text() == "hello" and (got / "log.csv").exists()
    assert not (got / ".sent-not-a-real-file").exists()
    assert json.loads((got / "pi-drive.json").read_text())["end_reason"] == "engine off"
    # Sending the same drive again (the Pi never heard the answer) does not make a copy.
    assert up.upload_drive(folder, folder.name, {"start_epoch": start}) == name
    assert len(list(data.glob("* jeep drive*"))) == 1
    up.send_status({"drives_waiting": 0})
    assert json.loads((data / receiver.STATUS_FILE).read_text())["drives_waiting"] == 0


def test_receiver_refuses_wrong_code_and_damaged_files(tmp_path, pc):
    data, port = pc
    assert sync.Uploader("wrong", ["127.0.0.1"], port=port).find() is None
    rec = receiver.Receiver(data, TOKEN)
    with pytest.raises(ValueError):
        rec.put_file("drive-1", "log.csv", b"abc", "0" * 64)
    with pytest.raises(ValueError):
        rec.put_file("../escape", "log.csv", b"abc", None)
    with pytest.raises(ValueError):
        rec.finish("drive-1", {"files": {"log.csv": "0" * 64}})


def test_receiver_answers_discovery(tmp_path):
    data = tmp_path / "pc"
    udp_port = _free_udp_port()
    server, udp = receiver.serve(data, host="127.0.0.1", port=0, discovery_port=udp_port)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(json.dumps(sync.DISCOVER).encode(), ("127.0.0.1", udp_port))
        reply = json.loads(s.recvfrom(2048)[0])
        assert reply["wrangler"] == "receiver" and reply["port"] == server.server_address[1]
        s.close()
    finally:
        server.server_close()
        udp.close()


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def logger_for(tmp_path, port=1, **cfg):
    vehicle = StoppableVehicle()
    config = {**carlogger.DEFAULTS, "data_dir": str(tmp_path / "pi"), "token": TOKEN, "pc_hosts": ["127.0.0.1"],
              "receiver_port": port, "led": False, "engine_off_s": 2, "min_drive_s": 1, **cfg}
    lg = carlogger.CarLogger(config, open_elm=lambda: Elm327(SimulatedTransport(vehicle)),
                             power=carlogger.NoPowerBoard(), poweroff=lambda: None)
    lg.uploader.discover = lambda wait_s=2.0: []  # no broadcasting from tests
    return lg, vehicle


def test_records_a_drive_until_the_engine_stops_then_sends_it(tmp_path, pc):
    data, port = pc
    lg, vehicle = logger_for(tmp_path, port)
    elm = lg._connect()
    assert elm is not None and lg._engine_running(elm)
    threading.Timer(4, lambda: setattr(vehicle, "engine_off", True)).start()
    folder = lg.record(elm)
    assert folder is not None and not folder.name.endswith(".partial")
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["end_reason"] == "engine off" and meta["samples"] > 5 and not meta["recovered"]
    assert {"log.csv", "report.md", "report.json"} <= {p.name for p in folder.iterdir()}
    assert lg.store.pending() == [folder]
    result = lg.upload_round()
    assert result["found"] and result["sent"] == 1 and lg.store.pending() == []
    received = list(data.glob("* jeep drive"))
    assert len(received) == 1 and (received[0] / "report.json").exists()
    assert json.loads((data / receiver.STATUS_FILE).read_text())["drives_waiting"] == 0


def test_short_drive_is_not_kept(tmp_path):
    lg, vehicle = logger_for(tmp_path, min_drive_s=600)
    elm = lg._connect()
    threading.Timer(1, lambda: setattr(vehicle, "engine_off", True)).start()
    assert lg.record(elm) is None
    assert list(lg.store.root.iterdir()) == []


def test_drive_cut_off_by_power_loss_is_recovered(tmp_path):
    store = carlogger.DriveStore(tmp_path / "drives")
    _, partial = store.new()
    rows = ["timestamp,elapsed_s,rpm (rpm),coolant_temp (°C)"]
    for i in range(30):
        rows.append(f"2026-11-20T13:15:{i:02d}.000+00:00,{i}.000,{700 + i},90.0")
    rows.append("2026-11-20T13:15:30.000+00:00,30.0")  # the line being written when power went
    (partial / "log.csv").write_text("\n".join(rows), encoding="utf-8")
    _, empty = store.new(time.time() + 5)
    (done,) = store.recover()
    assert not empty.exists()
    meta = store.meta(done)
    assert meta["recovered"] and meta["samples"] == 30 and meta["duration_s"] == 29
    doc = json.loads((done / "report.json").read_text())
    assert doc["recovered_after_power_loss"] and doc["log"]["overall"]["rpm"]
    assert (done / "report.md").exists()


def test_key_off_shutdown_waits_for_upload_then_powers_off(tmp_path):
    off = []
    lg, _ = logger_for(tmp_path, grace_s=100)
    lg.poweroff = lambda: off.append(True)
    make_drive(lg.store.root, "pi-20261120-081500")
    (lg.store.root / "pi-20261120-081500" / "meta.json").write_text("{}")
    lg.check_power(0, False)
    lg.check_power(2, False)
    assert not lg.power_lost.is_set()
    lg.check_power(3.5, False)
    assert lg.power_lost.is_set() and not off
    lg.last_upload = {"at": 1, "found": True, "sent": 0}
    lg.check_power(20, False)
    assert not off  # PC found, drive still going across
    lg.check_power(30, True)  # key back on before the end: carry on
    assert not lg.power_lost.is_set()
    lg.check_power(40, False)
    lg.check_power(44, False)
    lg.last_upload = {"at": 45, "found": False}
    lg.check_power(60, False)
    assert not off  # no PC yet: keep trying for 30 s
    lg.check_power(75, False)
    assert off == [True] and lg.quit.is_set()


def test_shutdown_after_everything_sent_or_grace(tmp_path):
    lg, _ = logger_for(tmp_path, grace_s=100)
    lg.check_power(0, False)
    lg.check_power(5, False)
    assert not lg.ready_to_power_off(6)  # not tried yet
    lg.last_upload = {"at": 6, "found": True}
    assert lg.ready_to_power_off(7)  # nothing waiting
    lg.idle.clear()
    assert not lg.ready_to_power_off(110) and lg.ready_to_power_off(170)


def test_lifepo4wered_reads_input_voltage(monkeypatch):
    calls = []

    def fake(cmd, timeout=5.0):
        calls.append(cmd)
        return {"vin": "5012\n", "vbat": "3301\n"}.get(cmd[-1], "")

    monkeypatch.setattr(carlogger, "_run", fake)
    board = carlogger.LiFePO4wered()
    assert board.external() is True
    board.setup({"auto_shutdown_min": 5})
    assert ["lifepo4wered-cli", "set", "auto_boot", "3"] in calls
    monkeypatch.setattr(carlogger, "_run", lambda cmd, timeout=5.0: "120\n")
    assert board.external() is False
    monkeypatch.setattr(carlogger, "_run", lambda cmd, timeout=5.0: None)
    assert board.external() is None


class FakeRemote:
    def __init__(self, host, user, password, echo):
        self.files, self.commands = {}, []
        FakeRemote.last = self

    def put(self, data, path):
        self.files[path] = data

    def run(self, command, stdin="", echo=print):
        self.commands.append((command, stdin))
        return 0

    def close(self):
        pass


def test_installer_copies_the_logger_and_pairs_it(tmp_path):
    lines = []
    pisetup.install("wrangler-pi.local", "bryan", "s3cret pass", "lifepo4wered", tmp_path,
                    {"tire_in": 33.0, "pcm_tire_in": 32.1}, 1.028, echo=lines.append, connect=FakeRemote)
    r = FakeRemote.last
    assert f"{pisetup.REMOTE_TMP}/wrangler_obd/carlogger.py" in r.files
    assert f"{pisetup.REMOTE_TMP}/pi/install.sh" in r.files
    cfg = json.loads(r.files[f"{pisetup.REMOTE_TMP}/wrangler-logger.json"])
    assert cfg["token"] == receiver.pairing_token(tmp_path) and cfg["power"] == "lifepo4wered"
    assert cfg["speed_factor"] == 1.028
    command, stdin = r.commands[-1]
    assert "install.sh --power lifepo4wered" in command and "s3cret" not in command
    assert stdin == "s3cret pass\n"
    with pytest.raises(pisetup.SetupError):
        pisetup.install("pi", "bryan", "", "none", tmp_path, {}, 1.0, connect=FakeRemote)
