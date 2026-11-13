import json
import threading
import time
import urllib.request

import pytest

from wrangler_obd.app import App, serve
from wrangler_obd.elm327 import Cancelled, Elm327
from wrangler_obd.simulator import SimulatedTransport


def wait_for(app, timeout=20):
    end = time.time() + timeout
    while app.busy() and time.time() < end:
        time.sleep(0.02)
    assert not app.busy()


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "jeep-data")
    a.connect("sim")
    yield a
    a.disconnect()


def test_connect_reports_vehicle(app):
    assert app.vehicle["vin"] == "1J4AA2D10AL000000"
    assert app.vehicle["mil_on"] is True and app.vehicle["simulator"] is True
    assert {"rpm", "coolant_temp"} <= set(app.live())


def test_steps_run_and_save_progress(app, tmp_path, monkeypatch):
    # Shorten the timed steps so the test is quick.
    real_plan = app.plan

    def short(step, options):
        title, argv, seconds, out = real_plan(step, options)
        if "--duration" in argv:
            argv[argv.index("--duration") + 1] = "0.3"
        elif argv[0] in ("idle", "pedal"):
            argv += ["--duration", "0.3"]
        return title, argv, seconds, out

    monkeypatch.setattr(app, "plan", short)
    assert app.has_trans_temp() is None
    for step in ("pedal", "drive", "idle-1"):
        app.start(step, {"notes": "test", "minutes": 1})
        wait_for(app)
        assert app.job.ok, app.job.as_dict()["log"]
    assert set(app.progress) == {"pedal", "drive", "idle-1"}
    assert "finding(s)" in app.progress["drive"]["summary"]
    assert app.has_trans_temp() is True  # the simulator answers the 21 30 request
    saved = json.loads((tmp_path / "jeep-data" / "progress.json").read_text())
    assert saved["idle-1"]["folder"].endswith("idle-1")
    drive = app.latest("drive", "report.json")
    assert "Manufacturer readings" in (drive.parent / "report.md").read_text(encoding="utf-8")
    z = app.make_zip()
    assert z.exists() and z.suffix == ".zip" and z.parent == tmp_path


def test_scans_compare_hot_and_cold(app):
    app.start("scan-hot", {})
    wait_for(app)
    assert app.job.ok
    app.start("scan-cold", {})
    wait_for(app)
    assert app.job.ok and any("differ from" in ln for ln in app.job.lines)


def test_stop_ends_a_recording_early(app):
    app.start("drive", {"minutes": 30})
    time.sleep(0.3)
    app.stop()
    wait_for(app, timeout=30)
    log = "\n".join(app.job.lines)
    assert "Saved report.md" in log  # what was recorded is kept
    assert app.job.as_dict()["elapsed"] < 30


def test_one_step_at_a_time_and_needs_connection(tmp_path):
    a = App(tmp_path / "d")
    with pytest.raises(RuntimeError, match="Connect"):
        a.start("pedal", {})
    a.connect("sim")
    a.start("drive", {"minutes": 1})
    with pytest.raises(RuntimeError, match="still running"):
        a.start("pedal", {})
    a.stop()
    wait_for(a, timeout=30)
    with pytest.raises(ValueError):
        a.plan("format-the-pcm", {})
    a.disconnect()


def test_cancel_is_raised_once():
    elm = Elm327(SimulatedTransport())
    elm.connect()
    elm.stop()
    with pytest.raises(Cancelled):
        elm.read_pid("rpm")
    assert elm.read_pid("rpm") is not None


def test_http_api(tmp_path):
    app = App(tmp_path / "d")
    server = serve(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body, origin=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        with urllib.request.urlopen(base + "/") as r:
            assert b"Wrangler Diagnostics" in r.read()
        assert post("/api/run", {"step": "pedal"})[0] == 400  # not connected
        status, body = post("/api/connect", {"port": "sim"})
        assert status == 200 and body["vehicle"]["vin"]
        assert post("/api/connect", {"port": "sim"}, origin="http://evil.example")[0] == 403
        with urllib.request.urlopen(base + "/api/state") as r:
            assert json.loads(r.read())["connected"] is True
        assert post("/api/run", {"step": "codes"})[0] == 200
        wait_for(app)
        assert any("P0456" in ln for ln in app.job.lines)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/view?f=../../etc/passwd")
    finally:
        server.shutdown()
        app.disconnect()
