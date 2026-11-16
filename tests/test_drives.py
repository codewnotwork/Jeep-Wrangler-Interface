import json
import time

from wrangler_obd import battery, drives, troubleshoot
from wrangler_obd.elm327 import Elm327
from wrangler_obd.mode06 import decode_test_results
from wrangler_obd.monitor import Monitor
from wrangler_obd.simulator import SimulatedTransport, SimulatedVehicle


def drive(cold=True):
    """15 min: 2 min cold idle, then cruise warming 20 to 90 °C, then a warm idle."""
    out = []
    for t in range(900):
        coolant = min(90.0, 20 + t * 0.15) if cold else 90.0
        if t < 120 or t >= 780:
            v = dict(rpm=700.0, speed=0.0, throttle=14.0, map=32.0, intake_temp=20.0,
                     ltft_b1=4.5, ltft_b2=4.0, pedal_d=15.0)
        else:
            v = dict(rpm=2000.0, speed=80.0, throttle=22.0, map=55.0, intake_temp=20.0,
                     ltft_b1=1.5, ltft_b2=1.0, pedal_d=30.0)
        v["coolant_temp"] = coolant
        out.append((float(t), v))
    return out


def test_drive_summary_measures_warmup_and_trims():
    d = drives.drive_summary(drive())
    assert d["duration_min"] == 15.0 and d["start_coolant_f"] == 68
    assert d["warmup_min"] == 6.7  # 80 °C reached at 400 s
    assert 0 < d["warmup_gallons_est"] and 0 < d["warmup_fuel_pct"] < 100
    assert d["idle_ltft"] == 4.2 and d["cruise_ltft"] == 1.2
    assert d["mpg_est"] and d["miles"] > 5

    warm = drives.drive_summary(drive(cold=False))
    assert warm["warmup_min"] is None and warm["start_coolant_f"] == 194


def test_history_reads_reports_and_old_logs(tmp_path):
    new = tmp_path / "2026-11-16 0800 monitor"
    new.mkdir()
    (new / "log.csv").write_text("timestamp,elapsed_s,rpm (rpm)\n", encoding="utf-8")
    (new / "report.json").write_text(json.dumps({"drive": {"duration_min": 12.0, "mpg_est": 11.0},
                                                 "owner_notes": "commute"}), encoding="utf-8")
    old = tmp_path / "2026-11-15 1700 monitor"
    old.mkdir()
    rows = ["timestamp,elapsed_s,rpm (rpm),speed (km/h),coolant_temp (°C)"]
    rows += [f"x,{t},700,0,{20 + t * 0.1}" for t in range(0, 300)]
    (old / "log.csv").write_text("\n".join(rows), encoding="utf-8")
    short = tmp_path / "2026-11-15 1800 monitor"
    short.mkdir()
    (short / "log.csv").write_text("timestamp,elapsed_s,rpm (rpm)\nx,0,700\nx,30,700\n", encoding="utf-8")
    (tmp_path / "not a recording").mkdir()

    rows = drives.history(tmp_path)
    assert [r["folder"] for r in rows] == [old.name, new.name]  # oldest first, the 30 s one left out
    assert rows[1]["mpg_est"] == 11.0 and rows[1]["notes"] == "commute"
    assert rows[0]["start_coolant_f"] == 68 and (tmp_path / "history-cache.json").exists()
    assert drives.history(tmp_path) == rows  # second time from the cache


def test_small_vacuum_leak_is_mild_and_separate_from_lean_idle():
    data = drive(cold=False)
    r = troubleshoot.evaluate(data)
    ids = [c["id"] for c in r["causes"]]
    assert "small_vacuum_leak" in ids and "vacuum_leak" not in ids
    cause = next(c for c in r["causes"] if c["id"] == "small_vacuum_leak")
    assert cause["score"] < 50 and "can be normal" in cause["title"]

    even = [(t, {**v, "ltft_b1": 2.0, "ltft_b2": 2.0}) for t, v in data]
    assert "small_vacuum_leak" not in [c["id"] for c in troubleshoot.evaluate(even)["causes"]]


def test_mode06_plain_english():
    # Catalyst B1: 0.02 against a 0.29 limit (unit 0x20 ratio, 1/128 per count).
    (cat,) = decode_test_results([0x21, 0x80, 0x20, 0x00, 0x02, 0x00, 0x00, 0x00, 0x25])
    assert "catalytic converter" in cat.purpose
    assert cat.verdict().startswith("Passed with plenty of room")
    (miss,) = decode_test_results([0xA2, 0x0C, 0x24, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF])
    assert miss.verdict() == "No misfires counted."
    (unknown,) = decode_test_results([0x21, 0x99, 0x20, 0x00, 0x02, 0x00, 0x00, 0x00, 0x25])
    assert "definition not published" in unknown.test


def test_crank_watch():
    w = battery.CrankWatch()
    for t in range(10):
        w.feed(t * 0.2, 0.0, 12.6)
    for t, v in ((2.0, 10.6), (2.2, 9.9), (2.4, 10.3)):
        w.feed(t, None, v)  # PCM quiet mid-crank
    w.feed(2.8, 800.0, 14.0)
    assert w.done and w.result["resting_v"] == 12.6 and w.result["cranking_min_v"] == 9.9
    assert w.result["verdict"].startswith("Borderline")

    running = battery.CrankWatch()
    running.feed(0.0, 700.0, 14.2)
    assert running.done and running.result is None
    assert battery.verdict(12.6, 9.2).startswith("Weak") and battery.verdict(12.1, 10.5).startswith("Good")


def test_monitor_records_the_cranking_dip(tmp_path):
    vehicle = SimulatedVehicle(seed=3)
    vehicle.start_engine_at = 0.8
    elm = Elm327(SimulatedTransport(vehicle))
    elm.connect()
    m = Monitor(elm, tmp_path / "run")
    m.start()
    deadline = time.time() + 10
    while m.crank.watching and time.time() < deadline:
        time.sleep(0.1)
    m.stop()
    m.join(20)
    doc = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    crank = doc["cranking"]
    assert crank and crank["resting_v"] > 12.3 and 10.0 <= crank["cranking_min_v"] < 10.8
    assert crank["verdict"].startswith("Good") and doc["drive"]["cranking_min_v"] == crank["cranking_min_v"]
    md = (tmp_path / "run" / "report.md").read_text(encoding="utf-8")
    assert "## Warm-up and battery" in md and "while cranking" in md
