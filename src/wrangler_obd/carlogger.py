"""The in-Jeep logger: runs on a Raspberry Pi and records every drive by itself.

It starts when the Pi boots (the Pi powers up with the ignition), waits for
the OBDLink adapter and a running engine, and records the drive with the
same ``Monitor`` the app's dashboard uses, so each drive gets the same
log.csv, report.md and report.json. When the engine has been off for a
little while, or the truck stops answering, the drive is finished.

Whenever home WiFi is in range, finished drives are sent to the drive
receiver on the PC (see ``sync.py``), and kept on the Pi until they are.

Power and the SD card:

* With a power board (LiFePO4wered/Pi+ by default), the Pi keeps running on
  the board's battery after the key is turned off. The logger notices the
  outside power is gone, finishes the drive, gives the upload a couple of
  minutes, then shuts the Pi down properly. If power comes back first (a
  quick stop, or the dip while cranking), it carries on.
* Without one, a power cut is still survivable: the log is forced onto the
  card every few seconds, each drive lives in its own folder, and a drive
  that was cut off is finished from its log at the next start.

Run it with ``python3 -m wrangler_obd.carlogger --config /etc/wrangler-logger.json``.
``--sim`` uses the simulator instead of the truck, for trying it at a desk.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Callable

from . import __version__, economy, report, sync
from .elm327 import Elm327, ElmError
from .monitor import Monitor
from .transport import SerialTransport, TransportError

log = logging.getLogger("wrangler-logger")

DEFAULTS: dict = {
    "data_dir": "/var/lib/wrangler",
    "port": "auto",              #: serial device of the OBDLink, or "auto"
    "power": "none",             #: "lifepo4wered", "gpio:<pin>" (low = power lost) or "none"
    "token": "",                 #: pairing code from the PC
    "pc_hosts": [],              #: PC addresses to try before broadcasting
    "receiver_port": sync.HTTP_PORT,
    "discovery_port": sync.DISCOVERY_PORT,
    "speed_factor": 1.0,         #: tire size correction, from the app's settings
    "tires": None,
    "engine_on_rpm": 300,
    "engine_off_s": 20,          #: engine off this long ends the drive
    "min_drive_s": 60,           #: shorter drives (moving the truck) are not kept
    "sync_every_s": 5,           #: how often the log is forced onto the card
    "upload_every_s": 30,
    "power_off_confirm_s": 3,    #: outside power gone this long counts as key off
    "grace_s": 150,              #: time on battery for saving and uploading before shutting down
    "auto_shutdown_min": 5,      #: power board backstop: it shuts down by itself after this long
    "keep_sent": 300,            #: drives kept on the Pi after they reached the PC
    "min_free_mb": 500,
    "retry_s": 10,
    "led": True,
}


def load_config(path: str | Path | None) -> dict:
    cfg = dict(DEFAULTS)
    if path:
        try:
            cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            log.warning("no config at %s, using defaults", path)
    return cfg


# -- power ------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


class NoPowerBoard:
    """Nothing to ask: the Pi just loses power with the key."""

    name = "none"

    def setup(self, cfg: dict) -> None:
        pass

    def external(self) -> bool | None:
        return None

    def details(self) -> dict:
        return {}


class LiFePO4wered:
    """The LiFePO4wered/Pi+ board, through its ``lifepo4wered-cli`` tool."""

    name = "lifepo4wered"
    #: AUTO_BOOT_VIN: the Pi starts whenever outside power appears.
    AUTO_BOOT_VIN = 3

    def __init__(self, cli: str = "lifepo4wered-cli", vin_present_mv: int = 4500) -> None:
        self.cli, self.vin_present_mv = cli, vin_present_mv

    def _get(self, var: str) -> int | None:
        out = _run([self.cli, "get", var])
        m = re.search(r"-?\d+", out or "")
        value = int(m.group()) if m else None
        return value if value is not None and value >= 0 else None

    def setup(self, cfg: dict) -> None:
        # Kept in the board's memory (not its flash) on purpose: removing the
        # battery for a few minutes undoes them if anything ever goes wrong.
        _run([self.cli, "set", "auto_boot", str(self.AUTO_BOOT_VIN)])
        minutes = int(cfg.get("auto_shutdown_min") or 0)
        if minutes > 0:
            _run([self.cli, "set", "auto_shdn_time", str(minutes)])

    def external(self) -> bool | None:
        vin = self._get("vin")
        return None if vin is None else vin >= self.vin_present_mv

    def details(self) -> dict:
        return {"board_battery_mv": self._get("vbat"), "board_input_mv": self._get("vin")}


class GpioPowerSignal:
    """A board that pulls a GPIO pin low when outside power is lost (many supercapacitor boards)."""

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.name = f"gpio:{pin}"

    def setup(self, cfg: dict) -> None:
        _run(["pinctrl", "set", str(self.pin), "ip", "pu"])

    def external(self) -> bool | None:
        out = _run(["pinctrl", "get", str(self.pin)]) or ""
        if "| hi" in out:
            return True
        if "| lo" in out:
            return False
        return None

    def details(self) -> dict:
        return {}


def power_board(spec: str):
    spec = (spec or "none").strip().lower()
    if spec == "lifepo4wered":
        return LiFePO4wered()
    if spec.startswith("gpio:"):
        return GpioPowerSignal(int(spec.split(":", 1)[1]))
    return NoPowerBoard()


# -- the drives on the card -------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_durable(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_log(path: Path) -> list[tuple[float, dict[str, float]]]:
    """Samples back from a log.csv, skipping a last line cut off by a power loss."""
    samples = []
    try:
        fh = open(path, newline="", encoding="utf-8", errors="replace")
    except OSError:
        return samples
    with fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or len(header) < 2:
            return samples
        keys = [h.rsplit(" (", 1)[0] for h in header[2:]]
        for row in reader:
            if len(row) != len(header):
                continue
            try:
                t = float(row[1])
                values = {k: float(v) for k, v in zip(keys, row[2:]) if v != ""}
            except ValueError:
                continue
            samples.append((t, values))
    return samples


class DriveStore:
    """One folder per drive under ``root``: ``<id>.partial`` while recording, then ``<id>``.

    A file named ``.sent`` in a finished drive records that the PC has it.
    """

    SENT = ".sent"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def new(self, now: float | None = None) -> tuple[str, Path]:
        stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
        host = re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname())[:30] or "pi"
        drive_id = f"{host}-{stamp}"
        path = self.root / f"{drive_id}.partial"
        path.mkdir(parents=True, exist_ok=True)
        return drive_id, path

    def finish(self, partial: Path, meta: dict) -> Path:
        _write_durable(partial / "meta.json", json.dumps(meta, indent=2))
        final = partial.with_name(partial.name[: -len(".partial")])
        os.replace(partial, final)
        _fsync_dir(self.root)
        return final

    def discard(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def finished(self) -> list[Path]:
        return sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.endswith(".partial")
                      and (p / "meta.json").is_file())

    def pending(self) -> list[Path]:
        return [p for p in self.finished() if not (p / self.SENT).exists()]

    def meta(self, path: Path) -> dict:
        try:
            return json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"drive_id": path.name}

    def mark_sent(self, path: Path, pc_folder: str) -> None:
        _write_durable(path / self.SENT, json.dumps({"pc_folder": pc_folder, "at": time.time()}))

    def recover(self, notes: str = "", speed_factor: float = 1.0) -> list[Path]:
        """Finish drives that were cut off by a power loss, from what reached the card."""
        recovered = []
        for partial in sorted(self.root.glob("*.partial")):
            samples = read_log(partial / "log.csv")
            if len(samples) < 2:
                self.discard(partial)
                continue
            duration = samples[-1][0] - samples[0][0]
            try:
                if not (partial / "report.json").exists():
                    summary = report.summarize_log(samples, duration)
                    doc = report.offline_report(summary, "log.csv", notes=notes,
                                                fuel_economy=economy.summarize(samples, speed_factor))
                    doc["recovered_after_power_loss"] = True
                    (partial / "report.json").write_text(report.to_json(doc), encoding="utf-8")
                    (partial / "report.md").write_text(report.to_markdown(doc), encoding="utf-8")
            except Exception as exc:  # the log itself is still worth keeping
                log.warning("could not build a report for %s: %r", partial.name, exc)
            drive_id = partial.name[: -len(".partial")]
            start = _start_epoch(partial / "log.csv") or partial.stat().st_mtime - duration
            meta = {"drive_id": drive_id, "start_epoch": start, "end_epoch": start + duration,
                    "duration_s": round(duration, 1), "samples": len(samples),
                    "end_reason": "power was cut before the drive was saved", "recovered": True,
                    "logger_version": __version__, "pi": socket.gethostname()}
            recovered.append(self.finish(partial, meta))
        return recovered

    def prune(self, keep_sent: int, min_free_mb: int) -> None:
        sent = [p for p in self.finished() if (p / self.SENT).exists()]
        while sent and (len(sent) > keep_sent or _free_mb(self.root) < min_free_mb):
            self.discard(sent.pop(0))


def _free_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 1e6
    except OSError:
        return float("inf")


def _start_epoch(path: Path) -> float | None:
    """The wall clock time of the first sample in a log.csv."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            first = next(reader, None)
        return datetime.fromisoformat(first[0]).timestamp() if first else None
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def clock_synced() -> bool | None:
    out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return None if out is None else out.strip() == "yes"


def find_adapter() -> str | None:
    """The OBDLink's serial device. The EX is an FTDI USB serial adapter."""
    by_id = sorted(glob("/dev/serial/by-id/*"))
    preferred = [p for p in by_id if re.search(r"obd|ftdi|stn|elm", p, re.I)]
    for path in preferred + by_id + sorted(glob("/dev/ttyUSB*")) + sorted(glob("/dev/ttyACM*")):
        if os.path.exists(path):
            return path
    return None


# -- the status light -------------------------------------------------------------


class Led:
    """Blinks the Pi's green activity light like a heartbeat while a drive is recorded."""

    def __init__(self, enabled: bool) -> None:
        paths = [p for p in ("/sys/class/leds/ACT", "/sys/class/leds/led0") if os.path.isdir(p)]
        self.path = Path(paths[0]) / "trigger" if enabled and paths else None
        self.default = None
        if self.path:
            try:
                m = re.search(r"\[([^\]]+)\]", self.path.read_text())
                self.default = m.group(1) if m else None
            except OSError:
                self.path = None

    def recording(self, on: bool) -> None:
        if not self.path:
            return
        try:
            self.path.write_text("heartbeat" if on else (self.default or "mmc0"))
        except OSError:
            pass


# -- the logger -------------------------------------------------------------------


class CarLogger:
    def __init__(self, cfg: dict, open_elm: Callable[[], Elm327] | None = None, power=None,
                 uploader: sync.Uploader | None = None, poweroff: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.store = DriveStore(Path(cfg["data_dir"]) / "drives")
        self.open_elm = open_elm or self._open_serial
        self.power = power or power_board(cfg.get("power", "none"))
        self.uploader = uploader or sync.Uploader(cfg.get("token", ""), cfg.get("pc_hosts") or [],
                                                  int(cfg["receiver_port"]), int(cfg["discovery_port"]))
        self.poweroff = poweroff or (lambda: subprocess.run(["systemctl", "poweroff"], check=False))
        self.led = Led(bool(cfg.get("led")))
        self.quit = threading.Event()
        self.power_lost = threading.Event()
        self.idle = threading.Event()     #: set while no drive is being recorded or saved
        self.idle.set()
        self.kick = threading.Event()     #: wakes the uploader early
        self.upload_lock = threading.Lock()
        self.monitor: Monitor | None = None
        self.state = "starting"
        self.last_upload: dict = {}       #: {"at", "found", "sent", "error"}
        self.power_lost_at: float | None = None
        self.powered_off = False

    # -- connecting ---------------------------------------------------------

    def _open_serial(self) -> Elm327:
        port = self.cfg.get("port") or "auto"
        if port == "auto":
            port = find_adapter()
            if not port:
                raise TransportError("the OBDLink is not plugged in")
        return Elm327(SerialTransport(port))

    def _connect(self) -> Elm327 | None:
        try:
            elm = self.open_elm()
        except (TransportError, ElmError, OSError) as exc:
            self.state = f"waiting for the adapter: {exc}"
            return None
        try:
            elm.connect()
            return elm
        except (ElmError, TransportError, OSError) as exc:
            self.state = f"waiting for the truck: {exc}"
            try:
                elm.close()
            except Exception:
                pass
            return None

    # -- the main loop ------------------------------------------------------

    def run(self) -> None:
        for path in self.store.recover(speed_factor=float(self.cfg.get("speed_factor") or 1.0)):
            log.info("finished %s from its log after a power cut", path.name)
        try:
            self.power.setup(self.cfg)
        except Exception as exc:
            log.warning("power board setup failed: %r", exc)
        threads = [threading.Thread(target=self._upload_loop, daemon=True),
                   threading.Thread(target=self._power_loop, daemon=True)]
        for t in threads:
            t.start()
        self._drive_loop()

    def _drive_loop(self) -> None:
        while not self.quit.is_set():
            if self.power_lost.is_set():
                self.state = "key off: saving and sending before shutting down"
                self.quit.wait(1)
                continue
            elm = self._connect()
            if elm is None:
                self.quit.wait(float(self.cfg["retry_s"]))
                continue
            try:
                self._wait_and_record(elm)
            except (ElmError, TransportError, OSError) as exc:
                self.state = f"lost the adapter: {exc}"
                log.info("%s", self.state)
            finally:
                try:
                    elm.close()
                except Exception:
                    pass
            self.quit.wait(2)

    def _engine_running(self, elm: Elm327) -> bool:
        r = elm.read_pids(["rpm"]).get("rpm")
        return r is not None and r.value >= float(self.cfg["engine_on_rpm"])

    def _wait_and_record(self, elm: Elm327) -> None:
        self.state = "waiting for the engine to start"
        quiet_since = time.monotonic()
        while not self.quit.is_set() and not self.power_lost.is_set():
            if self._engine_running(elm):
                self.record(elm)
                quiet_since = time.monotonic()
                self.state = "waiting for the engine to start"
            elif time.monotonic() - quiet_since > 120:
                return  # reconnect now and then: a long wait may mean the adapter went to sleep
            self.quit.wait(2)

    def record(self, elm: Elm327) -> Path | None:
        """Record one drive, until the engine stops or the power goes. Returns the finished folder."""
        cfg = self.cfg
        drive_id, folder = self.store.new()
        start_epoch, synced = time.time(), clock_synced()
        monitor = Monitor(elm, folder, notes=cfg.get("notes", ""), speed_factor=float(cfg.get("speed_factor") or 1.0),
                          tires=cfg.get("tires"), sync_every_s=float(cfg["sync_every_s"]), live_analysis=False)
        self.idle.clear()
        self.monitor = monitor
        self.state = "recording a drive"
        self.led.recording(True)
        log.info("recording %s", drive_id)
        monitor.start()
        last_running, reason = time.time(), None
        try:
            while reason is None:
                self.quit.wait(1)
                now = time.time()
                recent = [v for t, v in monitor.window(5) if monitor.started + t >= now - 3]
                if any(v.get("rpm", 0) >= float(cfg["engine_on_rpm"]) for v in recent):
                    last_running = now
                if not monitor.running:
                    reason = monitor.error or "recording stopped"
                elif self.quit.is_set():
                    reason = "logger stopped"
                elif self.power_lost.is_set():
                    reason = "key off (power went off)"
                elif now - last_running >= float(cfg["engine_off_s"]):
                    reason = "engine off"
            self.state = "saving the drive"
            monitor.stop()
            monitor.join(180)
            samples = monitor.window()
            duration = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0
            if duration < float(cfg["min_drive_s"]):
                log.info("drive %s too short (%.0f s), not kept", drive_id, duration)
                self.store.discard(folder)
                return None
            meta = {
                "drive_id": drive_id, "start_epoch": start_epoch, "end_epoch": time.time(),
                "duration_s": round(duration, 1), "samples": len(samples), "end_reason": reason,
                "clock_synced_at_start": synced, "error": monitor.error, "recovered": False,
                "logger_version": __version__, "pi": socket.gethostname(),
            }
            final = self.store.finish(folder, meta)
            log.info("saved %s (%s)", final.name, reason)
            return final
        finally:
            self.monitor = None
            self.led.recording(False)
            self.idle.set()
            self.kick.set()

    # -- uploading ----------------------------------------------------------

    def _upload_loop(self) -> None:
        while not self.quit.is_set():
            self.kick.wait(float(self.cfg["upload_every_s"]))
            self.kick.clear()
            if self.quit.is_set():
                return
            try:
                self.upload_round()
            except Exception as exc:  # keep trying next round
                log.warning("upload round failed: %r", exc)

    def status(self) -> dict:
        return {
            "pi": socket.gethostname(), "logger_version": __version__, "state": self.state,
            "drives_waiting": len(self.store.pending()), "drives_on_pi": len(self.store.finished()),
            "free_mb": round(_free_mb(self.store.root)), "power_board": self.power.name,
            "clock_synced": clock_synced(), "time": time.time(), **self.power.details(),
        }

    def upload_round(self) -> dict:
        """Find the PC and send whatever it doesn't have yet."""
        with self.upload_lock:
            result: dict = {"at": time.time(), "found": False, "sent": 0, "error": None}
            pending = self.store.pending()
            base = self.uploader.find()
            if base:
                result["found"] = True
                for path in pending:
                    try:
                        pc_folder = self.uploader.upload_drive(path, path.name, self.store.meta(path))
                        self.store.mark_sent(path, pc_folder)
                        result["sent"] += 1
                        log.info("sent %s to the PC as %r", path.name, pc_folder)
                    except sync.ReceiverError as exc:
                        result["error"] = str(exc)
                        log.warning("could not send %s: %s", path.name, exc)
                        break
                try:
                    self.uploader.send_status(self.status())
                except sync.ReceiverError:
                    pass
            self.store.prune(int(self.cfg["keep_sent"]), int(self.cfg["min_free_mb"]))
            self.last_upload = result
            return result

    # -- power ------------------------------------------------------------------

    def _power_loop(self) -> None:
        while not self.quit.is_set() and not self.powered_off:
            self.check_power(time.monotonic(), self.power.external())
            if self.quit.wait(1):
                return

    _off_since: float | None = None

    def check_power(self, now: float, external: bool | None) -> None:
        """One look at the outside power. Separate from the loop so it can be tested step by step."""
        cfg = self.cfg
        if external is False:
            self._off_since = self._off_since if self._off_since is not None else now
        else:
            self._off_since = None
            if self.power_lost.is_set() and external is True:
                log.info("power is back, carrying on")
                self.power_lost.clear()
                self.power_lost_at = None
            return
        if not self.power_lost.is_set() and now - self._off_since >= float(cfg["power_off_confirm_s"]):
            log.info("outside power is gone (key off)")
            self.power_lost_at = now
            self.power_lost.set()
            self.last_upload = {}
            self.kick.set()
        if self.power_lost.is_set() and self.ready_to_power_off(now):
            log.info("shutting down")
            self.powered_off = True
            self.quit.set()
            self.kick.set()
            self.poweroff()

    def ready_to_power_off(self, now: float) -> bool:
        lost = self.power_lost_at if self.power_lost_at is not None else now
        grace = float(self.cfg["grace_s"])
        if not self.idle.is_set():
            # Still saving the drive (a long one on a slow Pi can take a minute or two): give it
            # longer, but stop before the power board's own backstop would cut the power.
            backstop = float(self.cfg.get("auto_shutdown_min") or 0) * 60 - 30
            return now - lost >= max(grace + 60, backstop)
        if now - lost >= grace:
            return True
        tried = self.last_upload.get("at") is not None
        if tried and not self.store.pending():
            return True  # everything is on the PC
        return tried and not self.last_upload.get("found") and now - lost >= 30  # no PC here


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wrangler-logger", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="/etc/wrangler-logger.json")
    parser.add_argument("--sim", action="store_true", help="record the simulator instead of the truck")
    parser.add_argument("--data-dir", help="override where drives are kept")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config(args.config)
    if args.data_dir:
        cfg["data_dir"] = args.data_dir
    open_elm = None
    if args.sim:
        from .simulator import SimulatedTransport

        open_elm = lambda: Elm327(SimulatedTransport())  # noqa: E731
    logger = CarLogger(cfg, open_elm=open_elm)
    # Stopping (shutdown, Ctrl+C) finishes and saves a drive in progress instead of abandoning it.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: logger.quit.set())
    log.info("Wrangler in-Jeep logger %s starting, drives in %s", __version__, logger.store.root)
    logger.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
