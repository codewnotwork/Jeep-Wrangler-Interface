"""Record everything: poll every reading the truck supports and keep it.

A ``Monitor`` runs in a background thread. Each pass reads the fast-moving
readings; every few passes it also reads the slow ones and the
Chrysler-specific extras that answered. Samples go to ``log.csv`` (metric)
and to an in-memory buffer the dashboard draws from. Live troubleshooting
runs over the recent window every few seconds. On stop it writes the
report (Markdown + JSON) for a person or an LLM.

Codes, readiness monitors, Mode 06 results and the protocol are read
first, before recording starts, so a connection that drops later (key
off, cable pulled) cannot lose them. They are read again at the end when
the truck still answers, since Mode 06 results update during a drive.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import battery, drives, economy, extras, report, troubleshoot
from .elm327 import Elm327, ElmError
from .pids import PIDS, to_imperial
from .transport import TransportError

SLOW_EVERY = 10      #: slow readings are read on every Nth pass
EXTRAS_EVERY = 20    #: Chrysler extras on every Nth pass (they need a module switch)
BUFFER = 40000       #: samples kept in memory for the dashboard
ANALYSE_EVERY_S = 5.0
ANALYSE_WINDOW_S = 900.0
MIN_INTERVAL_S = 0.05


class Monitor:
    def __init__(self, elm: Elm327, out: Path, notes: str = "", speed_factor: float = 1.0,
                 tires: dict | None = None, sync_every_s: float | None = None, live_analysis: bool = True) -> None:
        self.elm, self.out, self.notes, self.speed_factor = elm, Path(out), notes, speed_factor
        #: When set, the log is forced onto the disk this often (the in-Jeep logger,
        #: where power can vanish), so a sudden power cut loses only the last few seconds.
        self.sync_every_s = sync_every_s
        #: Live troubleshooting for the dashboard. The in-Jeep logger turns it off: nobody is
        #: watching, and on a small Pi it would take time from recording. The saved report still has it.
        self.live_analysis = live_analysis
        #: Tire diameters behind ``speed_factor``, recorded in the report.
        self.tires = tires
        self.start_info: dict | None = None
        self.phase = "starting"
        self.samples: deque[tuple[float, dict[str, float]]] = deque(maxlen=BUFFER)
        self.markers: list[dict] = []
        self.analysis: dict = {"causes": [], "coverage": {}, "notes": []}
        self.catalog: list[dict] = []
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.count = 0
        self.saving = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._codes: tuple[list[str], list[str]] = ([], [])
        #: Battery check: watches the voltage dip if the engine is started during the recording.
        self.crank = battery.CrankWatch()

    # -- control -------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.finished is None

    def add_marker(self, text: str) -> dict:
        marker = {"t": round(time.time() - self.started, 1), "text": text.strip()[:200] or "Marker"}
        with self._lock:
            self.markers.append(marker)
        return marker

    # -- the loop --------------------------------------------------------------

    def _setup(self) -> tuple[list[str], list[str], list[extras.Extra]]:
        supported = self.elm.supported_pids()
        keys = [k for k in PIDS if PIDS[k].pid in supported]
        fast = [k for k in keys if k not in report.SLOW_PIDS]
        slow = [k for k in keys if k in report.SLOW_PIDS]
        self.phase = "reading"
        manufacturer = extras.probe(self.elm)
        # Save the one-shot data now, so a dropped connection later can't lose it.
        self.start_info = report.read_vehicle(self.elm, manufacturer)
        answered = {x["key"] for x in manufacturer}
        found = [e for e in extras.EXTRAS if e.key in answered]
        self.catalog = [{"key": k, "name": PIDS[k].name, "unit": PIDS[k].unit} for k in keys]
        self.catalog += [{"key": f"x_{e.key}", "name": f"{e.name} (unconfirmed)", "unit": e.unit} for e in found]
        dtcs = self.start_info.get("dtcs")
        self._codes = (dtcs.stored, dtcs.pending) if dtcs else ([], [])
        self.phase = "recording"
        return fast, slow, found

    def _read_extras(self, found: list[extras.Extra]) -> dict[str, float]:
        values: dict[str, float] = {}
        for module in sorted({e.module for e in found}):
            with self.elm.ecu(module):
                for e in found:
                    if e.module == module:
                        data = self.elm.read_data(e.service, e.ident)
                        value = e.decode(data) if data is not None else None
                        if value is not None:
                            values[f"x_{e.key}"] = value
        return values

    def _run(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        try:
            fast, slow, found = self._setup()
            columns = [c["key"] for c in self.catalog]
            with open(self.out / "log.csv", "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["timestamp", "elapsed_s"] + [f"{c['key']} ({c['unit']})" for c in self.catalog])
                last_analysis = last_sync = 0.0
                self.elm.fast = True
                while not self._stop.is_set():
                    tick = time.time()
                    full_pass = self.count % SLOW_EVERY == 0
                    wanted = fast + (slow if full_pass else [])
                    if self.crank.watching and not full_pass and "rpm" in fast:
                        # Still watching means the engine hasn't run yet (key on, engine off):
                        # read rpm and voltage as fast as possible
                        # so the dip while cranking isn't missed.
                        wanted = ["rpm"]
                    values = {k: r.value for k, r in self.elm.read_pids(wanted).items()}
                    if found and self.count % EXTRAS_EVERY == 0:
                        values.update(self._read_extras(found))
                    if self.crank.watching:
                        self._watch_crank(tick, values.get("rpm"))
                    # Rounded once here, so the time a browser sends back as "since" matches exactly.
                    t = round(tick - self.started, 3)
                    with self._lock:
                        self.samples.append((t, values))
                    stamp = datetime.fromtimestamp(tick, tz=timezone.utc).isoformat(timespec="milliseconds")
                    writer.writerow([stamp, f"{t:.3f}"] + [f"{values[k]:.3f}" if k in values else "" for k in columns])
                    self.count += 1
                    if self.count % 20 == 0:
                        fh.flush()
                    if self.sync_every_s and tick - last_sync >= self.sync_every_s:
                        last_sync = tick
                        fh.flush()
                        os.fsync(fh.fileno())
                    if self.live_analysis and tick - last_analysis >= ANALYSE_EVERY_S:
                        last_analysis = tick
                        self.analysis = troubleshoot.evaluate(self.window(ANALYSE_WINDOW_S), *self._codes)
                    spare = MIN_INTERVAL_S - (time.time() - tick)
                    if spare > 0:
                        self._stop.wait(spare)
        except (ElmError, TransportError) as exc:
            self.error = f"Lost contact with the truck: {exc}"
        except Exception as exc:  # surface anything unexpected on the page
            self.error = f"Monitoring stopped: {exc!r}"
        finally:
            self.elm.fast = False
            self.saving = True
            try:
                self._save()
            except Exception as exc:
                self.error = self.error or f"The log was saved but the report could not be written: {exc!r}"
            self.saving = False
            self.finished = time.time()

    def _watch_crank(self, tick: float, rpm: float | None) -> None:
        try:
            volts = self.elm.battery_voltage()
        except ElmError:
            volts = None
        self.crank.feed(tick - self.started, rpm, volts)

    # -- reading the buffer ---------------------------------------------------

    def window(self, seconds: float | None = None) -> list[tuple[float, dict[str, float]]]:
        with self._lock:
            data = list(self.samples)
        if seconds is None or not data:
            return data
        cutoff = data[-1][0] - seconds
        return [s for s in data if s[0] >= cutoff]

    def display(self, since: float = -1.0, max_points: int = 1500) -> dict:
        """Samples after ``since`` converted to US units, thinned to ``max_points``."""
        units = {c["key"]: c["unit"] for c in self.catalog}
        data = [s for s in self.window() if s[0] > since]
        step = max(1, len(data) // max_points)
        out = []
        for t, values in data[::step]:
            row = {k: round(to_imperial(v, units.get(k, ""))[0], 3) for k, v in values.items()}
            if "speed" in row:
                row["speed"] = round(row["speed"] * self.speed_factor, 3)  # corrected for tire size
            out.append([t, row])
        duration = (self.finished or time.time()) - self.started
        return {
            "running": self.running, "saving": self.saving, "error": self.error, "phase": self.phase,
            "elapsed": round(duration, 1), "count": self.count,
            "rate": round(self.count / duration, 1) if duration > 0 else 0,
            "catalog": [{**c, "unit": to_imperial(0, c["unit"])[1]} for c in self.catalog],
            "samples": out, "markers": list(self.markers), "analysis": self.analysis,
            "folder": self.out.name,
        }

    # -- saving ----------------------------------------------------------------

    def _save(self) -> None:
        data = self.window()
        if not data:
            return
        duration = data[-1][0] - data[0][0]
        summary = report.summarize_log(data, duration)
        mpg = economy.summarize(data, self.speed_factor)
        analysis = troubleshoot.evaluate(data, *self._codes)
        self.analysis = analysis
        info, when = None, "end of recording"
        try:
            info = report.read_vehicle(self.elm, (self.start_info or {}).get("manufacturer"))
        except Exception:
            info = None
        if info is None or info.get("status") is None:
            # The truck is gone or not answering (key off, cable out): use what was read at the start.
            info, when = self.start_info, "start of recording (the truck stopped answering before the end)"
        elif self.start_info:
            # Fill anything the end read missed from the start read.
            info = {k: (v if v not in (None, {}, []) else self.start_info.get(k)) for k, v in info.items()}
        if info is None:
            doc = report.offline_report(summary, "log.csv", notes=self.notes, fuel_economy=mpg)
        else:
            if not info.get("snapshot") and self.start_info and self.start_info.get("snapshot"):
                info = {**info, "snapshot": self.start_info["snapshot"]}
            doc = report.assemble_report(info, summary, "log.csv", notes=self.notes, fuel_economy=mpg)
            doc["vehicle_data_read_at"] = f"{info.get('read_at')} ({when})"
        if not doc["snapshot"]:
            # Nothing could be read at all: the last recorded values are the next best thing.
            last: dict[str, float] = {}
            for _, values in data:
                last.update(values)
            doc["snapshot"] = {k: {"value": round(v, 2), "unit": report._label(k)[1], "name": report._label(k)[0]}
                               for k, v in last.items()}
            doc["snapshot_source"] = "last recorded values"
        seen = {k for _, values in data for k in values}
        doc["not_reported"] = [{"key": c["key"], "name": c["name"]} for c in self.catalog if c["key"] not in seen]
        if self.tires:
            doc["tire_correction"] = {**self.tires, "speed_factor": round(self.speed_factor, 4)}
        doc["likely_causes"] = analysis
        doc["drive"] = drives.drive_summary(data, self.speed_factor)
        if self.crank.watching:
            self.crank.finish(data[-1][0])  # recording ended mid-crank or with the engine off
        doc["cranking"] = self.crank.result
        doc["drive"]["cranking_min_v"] = (self.crank.result or {}).get("cranking_min_v")
        doc["markers"] = list(self.markers)
        doc["segments_between_markers"] = report.segments_between(data, self.markers, self.speed_factor)
        (self.out / "report.json").write_text(report.to_json(doc), encoding="utf-8")
        (self.out / "report.md").write_text(report.to_markdown(doc), encoding="utf-8")
        (self.out / "markers.json").write_text(json.dumps(self.markers, indent=2), encoding="utf-8")
