"""Point-and-click app: a local web page with one button per step.

Run ``python -m wrangler_obd.app`` (or double-click ``Wrangler Diagnostics.pyw``).
It starts a small web server on this computer only and opens it in the
browser. Nothing is sent anywhere. Results are saved under
``Documents/jeep-data``. The server stops by itself a couple of minutes
after the page is closed, unless a recording is still running.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cli, economy
from .elm327 import Elm327, ElmError
from .monitor import Monitor
from .simulator import SimulatedTransport
from .transport import SerialTransport, TransportError

IDLE_PRESETS = {
    "idle-1": "Park, A/C off, stereo off",
    "idle-2": "In Drive, foot on the brake",
    "idle-3": "Park, A/C on",
    "idle-4": "Park, stereo on loud",
}
STEP_SECONDS = {"pedal": 45, "idle": 180}
QUIET_SHUTDOWN_S = 120

#: Tire diameters in inches. The 2010 Sahara left the factory on 255/70R18
#: tires (about 32.1 in), and the PCM computes speed for that size unless a
#: dealer or tuner reprogrammed it.
DEFAULT_SETTINGS = {"tire_in": 33.0, "pcm_tire_in": 32.1}


def default_data_dir() -> Path:
    return Path.home() / "Documents" / "jeep-data"


def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    ports = []
    for p in sorted(lp.comports()):
        desc = p.description or ""
        likely = any(w in desc.lower() for w in ("obdlink", "usb serial", "ftdi", "stn", "elm"))
        ports.append({"device": p.device, "description": desc, "likely": likely})
    ports.sort(key=lambda p: not p["likely"])
    return ports


class _LogWriter(io.TextIOBase):
    """Collects printed output for the page. A carriage return rewrites the last line."""

    def __init__(self, job: "Job") -> None:
        self.job = job

    def write(self, text: str) -> int:
        with self.job.lock:
            for i, part in enumerate(text.split("\n")):
                if i:
                    self.job.lines.append("")
                if "\r" in part:
                    self.job.lines[-1] = part.split("\r")[-1]
                else:
                    self.job.lines[-1] += part
        return len(text)


class Job:
    def __init__(self, step: str, title: str, seconds: float | None, out: Path | None) -> None:
        self.step, self.title, self.seconds, self.out = step, title, seconds, out
        self.started = time.time()
        self.finished: float | None = None
        self.ok: bool | None = None
        self.lines: list[str] = [""]
        self.lock = threading.Lock()

    def as_dict(self) -> dict:
        with self.lock:
            lines = [ln for ln in self.lines if ln.strip()]
        return {
            "step": self.step, "title": self.title, "seconds": self.seconds,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "running": self.finished is None, "ok": self.ok, "log": lines[-200:],
            "folder": self.out.name if self.out else None,
        }


class App:
    """State shared by the web handlers: the connection, the running job, saved progress."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.elm: Elm327 | None = None
        self.port: str | None = None
        self.vehicle: dict = {}
        self.job: Job | None = None
        self.lock = threading.Lock()
        self.last_seen = time.time()
        self.progress = self._load_progress()
        self.monitor: Monitor | None = None

    # -- saved progress ----------------------------------------------------

    def _progress_file(self) -> Path:
        return self.data_dir / "progress.json"

    def _load_progress(self) -> dict:
        try:
            return json.loads(self._progress_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _mark_done(self, step: str, folder: str | None, summary: str = "") -> None:
        self.progress[step] = {"when": datetime.now().strftime("%a %b %d, %I:%M %p"), "folder": folder, "summary": summary}
        self._progress_file().write_text(json.dumps(self.progress, indent=2), encoding="utf-8")

    # -- connection ----------------------------------------------------------

    def connect(self, port: str) -> dict:
        if self.busy():
            raise RuntimeError("Wait for the current step to finish first.")
        self.disconnect()
        transport = SimulatedTransport() if port == "sim" else SerialTransport(port)
        elm = Elm327(transport)
        try:
            elm.connect()
        except Exception:
            elm.close()
            raise
        status = elm.monitor_status() or {}
        volts = elm.battery_voltage()
        self.elm, self.port = elm, port
        self.vehicle = {
            "vin": elm.vin(), "protocol": elm.protocol, "adapter": elm.version,
            "battery": round(volts, 1) if volts else None,
            "mil_on": status.get("mil_on"), "dtc_count": status.get("dtc_count"),
            "simulator": port == "sim",
        }
        return self.vehicle

    def disconnect(self) -> None:
        if self.monitor and self.monitor.running:
            self.monitor.stop()
            self.monitor.join(30)
        if self.elm:
            try:
                self.elm.close()
            except Exception:
                pass
        self.elm, self.port, self.vehicle = None, None, {}

    def busy(self) -> bool:
        return (self.job is not None and self.job.finished is None) or self.monitoring()

    def monitoring(self) -> bool:
        return self.monitor is not None and self.monitor.running

    # -- monitor ---------------------------------------------------------------

    def start_monitor(self, notes: str = "") -> None:
        if not self.elm:
            raise RuntimeError("Connect to the adapter first.")
        if self.busy():
            raise RuntimeError("Something is already running. Stop it first.")
        tires = self.settings()
        self.monitor = Monitor(self.elm, self._folder("monitor"), notes=notes[:2000],
                               speed_factor=self.speed_factor(), tires=tires)
        self.monitor.start()

    def stop_monitor(self, notes: str | None = None) -> None:
        if self.monitor:
            if notes is not None:
                self.monitor.notes = notes[:2000]  # saved with the report
            self.monitor.stop()

    # -- settings ---------------------------------------------------------------

    def _settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def settings(self) -> dict:
        """Tire diameters in inches: what is on the truck, and what the PCM is set up for."""
        out = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(self._settings_file().read_text(encoding="utf-8"))
            for key in out:
                if isinstance(saved.get(key), (int, float)) and 20 <= saved[key] <= 50:
                    out[key] = float(saved[key])
        except (OSError, ValueError):
            pass
        return out

    def save_settings(self, body: dict) -> dict:
        new = {}
        for key in DEFAULT_SETTINGS:
            value = float(body.get(key) or 0)
            if not 20 <= value <= 50:
                raise ValueError("Tire diameters should be in inches, between 20 and 50.")
            new[key] = round(value, 2)
        self._settings_file().write_text(json.dumps(new), encoding="utf-8")
        return new

    def speed_factor(self) -> float:
        s = self.settings()
        return economy.tire_speed_factor(s["tire_in"], s["pcm_tire_in"])

    def _layout_file(self) -> Path:
        return self.data_dir / "layout.json"

    def layout(self) -> dict:
        try:
            return json.loads(self._layout_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_layout(self, layout: dict) -> None:
        tiles = [str(k)[:60] for k in layout.get("tiles", [])][:60]
        window = int(layout.get("window", 300))
        self._layout_file().write_text(json.dumps({"tiles": tiles, "window": window}), encoding="utf-8")

    def live(self) -> dict:
        """A few readings for the header, only when no step is running."""
        if not self.elm or self.busy() or not self.lock.acquire(blocking=False):  # busy() covers monitoring
            return {}
        try:
            r = self.elm.read_pids(["rpm", "coolant_temp", "speed", "module_voltage"])
            out = {k: round(v.value, 1) for k, v in r.items()}
            if "speed" in out:
                out["speed"] = round(out["speed"] * self.speed_factor(), 1)  # corrected for tire size
            return out
        except (ElmError, TransportError):
            return {}
        finally:
            self.lock.release()

    # -- steps ---------------------------------------------------------------

    def _folder(self, name: str) -> Path:
        return self.data_dir / f"{datetime.now():%Y-%m-%d %H%M} {name}"

    def plan(self, step: str, options: dict) -> tuple[str, list[str], float | None, Path | None]:
        """Turn a step from the page into (title, CLI arguments, expected seconds, output folder)."""
        notes = (options.get("notes") or "").strip()
        if step == "pedal":
            out = self._folder("pedal")
            return "Pedal map", ["pedal", "--out", str(out), "--notes", notes or "stock"], 45, out
        if step == "drive":
            minutes = max(1, min(180, int(options.get("minutes") or 30)))
            out = self._folder("drive")
            argv = ["report", "--out", str(out), "--duration", str(minutes * 60), "--notes", notes or "drive"]
            argv += ["--speed-factor", f"{self.speed_factor():.4f}"]
            return f"Drive recording ({minutes} min)", argv, minutes * 60, out
        if step in IDLE_PRESETS:
            out = self._folder(step)
            return f"Idle check: {IDLE_PRESETS[step]}", ["idle", "--out", str(out), "--notes", notes or IDLE_PRESETS[step]], 180, out
        if step == "health":
            out = self._folder("health")
            return "Quick health check", ["report", "--out", str(out), "--notes", notes or "quick check"], None, out
        if step in ("scan-hot", "scan-cold"):
            label = "hot, after a drive" if step == "scan-hot" else "cold, sat overnight"
            out = self._folder(step)
            out.mkdir(parents=True, exist_ok=True)
            argv = ["scan", "--out", str(out / "scan.json"), "--label", label]
            other = self.latest(("scan-cold" if step == "scan-hot" else "scan-hot"), "scan.json")
            if other:
                argv += ["--compare", str(other)]
            return f"Transmission scan ({label})", argv, None, out
        if step == "codes":
            return "Read trouble codes", ["dtc"], None, None
        raise ValueError(f"unknown step {step!r}")

    def latest(self, name: str, filename: str) -> Path | None:
        found = sorted(self.data_dir.glob(f"* {name}/{filename}"))
        return found[-1] if found else None

    def start(self, step: str, options: dict) -> None:
        if not self.elm:
            raise RuntimeError("Connect to the adapter first.")
        if self.busy():
            raise RuntimeError("Another step is still running.")
        title, argv, seconds, out = self.plan(step, options)
        job = Job(step, title, seconds, out)
        self.job = job
        threading.Thread(target=self._run, args=(job, argv), daemon=True).start()

    def _run(self, job: Job, argv: list[str]) -> None:
        args = cli.build_parser().parse_args(argv)
        if self.port == "sim":
            args.sim = True
        writer = _LogWriter(job)
        with self.lock:
            try:
                with redirect_stdout(writer):
                    cli.COMMANDS[args.command](self.elm, args)
                job.ok = True
            except KeyboardInterrupt:
                writer.write("\nStopped.\n")
                job.ok = bool(job.out and any(job.out.iterdir())) if job.out and job.out.exists() else False
            except SystemExit as exc:
                writer.write(f"\n{exc}\n")
                job.ok = False
            except (ElmError, TransportError) as exc:
                writer.write(f"\nLost contact with the truck: {exc}\nCheck the cable and the key, then press Connect again.\n")
                job.ok = False
            except Exception as exc:  # show anything unexpected instead of failing silently
                writer.write(f"\nSomething went wrong: {exc!r}\n")
                job.ok = False
            finally:
                # Save progress before marking the job finished, so anyone who sees
                # it finished also sees the saved progress file.
                try:
                    if job.ok and job.step != "codes":
                        self._mark_done(job.step, job.out.name if job.out else None, self._summary(job))
                finally:
                    job.finished = time.time()

    def _summary(self, job: Job) -> str:
        """One line for the step card after it finishes."""
        if job.out is None:
            return ""
        if job.step in ("drive", "health"):
            try:
                data = json.loads((job.out / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ""
            trans = [x for x in data.get("manufacturer_readings", []) if x["key"].startswith("trans_temp")]
            bits = []
            mpg = (data.get("fuel_economy") or {}).get("mpg")
            if mpg:
                bits.append(f"about {mpg} mpg")
            bits.append(f"{len(data.get('findings', []))} finding(s)")
            bits.append(
                f"transmission {trans[0]['value'] * 9 / 5 + 32:.0f} °F" if trans else "no transmission temperature yet"
            )
            return ", ".join(bits)
        return ""

    def stop(self) -> None:
        if self.busy() and self.elm:
            self.elm.stop()

    def has_trans_temp(self) -> bool | None:
        report = self.latest("drive", "report.json")
        if report is None:
            return None
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return any(x["key"].startswith("trans_temp") for x in data.get("manufacturer_readings", []))

    # -- files ---------------------------------------------------------------

    def make_zip(self) -> Path:
        stamp = f"{datetime.now():%Y-%m-%d %H%M}"
        target = self.data_dir.parent / f"jeep-data {stamp}"
        return Path(shutil.make_archive(str(target), "zip", root_dir=self.data_dir))

    def open_folder(self, path: Path | None = None) -> None:
        path = Path(path or self.data_dir)
        system = platform.system()
        if system == "Windows":
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])

    def state(self) -> dict:
        return {
            "connected": self.elm is not None, "port": self.port, "vehicle": self.vehicle,
            "job": self.job.as_dict() if self.job else None,
            "progress": self.progress, "data_dir": str(self.data_dir),
            "trans_temp_found": self.has_trans_temp(),
            "monitoring": self.monitoring(),
        }


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "WranglerOBD"

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        self.app.last_seen = time.time()
        url = urlparse(self.path)
        if url.path == "/":
            html = resources.files("wrangler_obd").joinpath("app.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self._json(self.app.state())
        if url.path == "/api/ports":
            return self._json(list_ports())
        if url.path == "/api/monitor":
            m = self.app.monitor
            since = float(parse_qs(url.query).get("since", ["-1"])[0])
            return self._json({"exists": False} if m is None else {"exists": True, **m.display(since)})
        if url.path == "/api/layout":
            return self._json(self.app.layout())
        if url.path == "/api/settings":
            return self._json(self.app.settings())
        if url.path == "/api/live":
            return self._json(self.app.live())
        if url.path == "/view":
            # Show a saved report as text. Only files inside the data folder.
            rel = parse_qs(url.query).get("f", [""])[0]
            path = (self.app.data_dir / rel).resolve()
            if self.app.data_dir.resolve() in path.parents and path.is_file() and path.suffix in (".md", ".json", ".csv"):
                return self._send(200, path.read_bytes(), "text/plain; charset=utf-8")
            return self._send(404, b"Not found", "text/plain")
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        self.app.last_seen = time.time()
        # Only accept requests from this page, not from other websites.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self._json({"error": "forbidden"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        app = self.app
        try:
            path = urlparse(self.path).path
            if path == "/api/connect":
                return self._json({"vehicle": app.connect(body.get("port") or "")})
            if path == "/api/disconnect":
                app.disconnect()
            elif path == "/api/run":
                app.start(body.get("step", ""), body)
            elif path == "/api/monitor/start":
                app.start_monitor(str(body.get("notes") or ""))
            elif path == "/api/monitor/stop":
                app.stop_monitor(str(body["notes"]) if "notes" in body else None)
            elif path == "/api/settings":
                return self._json(app.save_settings(body))
            elif path == "/api/monitor/marker":
                if not app.monitoring():
                    raise RuntimeError("Start monitoring first.")
                return self._json(app.monitor.add_marker(str(body.get("text") or "")))
            elif path == "/api/layout":
                app.save_layout(body)
            elif path == "/api/stop":
                app.stop()
            elif path == "/api/zip":
                z = app.make_zip()
                try:
                    app.open_folder(z)
                except Exception:
                    pass
                return self._json({"zip": str(z)})
            elif path == "/api/open":
                rel = body.get("folder")
                app.open_folder(app.data_dir / rel if rel else None)
            elif path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self._json({"error": "unknown"}, 404)
            self._json({"ok": True})
        except (ElmError, TransportError) as exc:
            self._json({"error": f"Could not talk to the adapter: {exc}"}, 400)
        except (RuntimeError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)


def serve(app: App, port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    for candidate in (port, port + 1, port + 2, 0):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError:
            continue
    raise OSError("no free port")


def _watchdog(app: App, server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(10)
        if not app.busy() and time.time() - app.last_seen > QUIET_SHUTDOWN_S:
            server.shutdown()
            return


def main(argv: list[str] | None = None) -> int:
    no_browser = "--no-browser" in (argv or sys.argv[1:])
    app = App()
    server = serve(app)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=_watchdog, args=(app, server), daemon=True).start()
    if not no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    if sys.stdout:
        print(f"Wrangler Diagnostics is running at {url}  (close this window to quit)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.disconnect()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
