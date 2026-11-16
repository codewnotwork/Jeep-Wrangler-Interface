"""The drive receiver: runs quietly on the PC and accepts drives from the in-Jeep Pi.

Start it with ``python -m wrangler_obd.receiver`` (the app starts it for you,
and can add it to Windows startup). It listens on the home network for the
Pi (see ``sync.py`` for how the two talk), and files each drive as a folder
in ``Documents/jeep-data`` named like ``2026-11-20 0815 jeep drive``, holding
the same report.md, report.json and log.csv as a recording made in the app.

Only one receiver runs at a time: a second one exits quietly. It needs the
pairing code in ``pi-pairing.json``, which the Pi was given when it was set up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import sync

PAIRING_FILE = "pi-pairing.json"
STATUS_FILE = "pi-status.json"
INDEX_FILE = "pi-received.json"
DRIVE_SUFFIX = "jeep drive"


def default_data_dir() -> Path:
    return Path.home() / "Documents" / "jeep-data"


def _write_json(path: Path, data) -> None:
    """Write via a temporary file, so a half-written file is never left behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def pairing_token(data_dir: Path) -> str:
    """The code shared with the Pi, created the first time it is needed."""
    path = Path(data_dir) / PAIRING_FILE
    token = _read_json(path, {}).get("token")
    if not token:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(24)
        _write_json(path, {"token": token})
    return token


class Receiver:
    """Where received drives go, separate from the web server so it can be tested directly."""

    def __init__(self, data_dir: Path, token: str) -> None:
        self.data_dir = Path(data_dir)
        self.token = token
        self.incoming = self.data_dir / ".incoming"
        self.lock = threading.Lock()

    def allowed(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), self.token.encode())

    def put_file(self, drive: str, name: str, data: bytes, digest: str | None) -> None:
        if not sync.DRIVE_ID.match(drive) or not sync.FILE_NAME.match(name):
            raise ValueError("bad drive or file name")
        if digest and hashlib.sha256(data).hexdigest() != digest.lower():
            raise ValueError(f"{name} arrived damaged")
        folder = self.incoming / drive
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, folder / name)

    def finish(self, drive: str, meta: dict) -> str:
        """File a drive once all its files are here and intact. Safe to call again for the same drive."""
        if not sync.DRIVE_ID.match(drive):
            raise ValueError("bad drive name")
        with self.lock:
            index = _read_json(self.data_dir / INDEX_FILE, {})
            if drive in index and (self.data_dir / index[drive]).is_dir():
                return index[drive]
            files = meta.get("files") or {}
            folder = self.incoming / drive
            bad = [n for n, d in files.items()
                   if not sync.FILE_NAME.match(n) or not (folder / n).is_file() or sync.sha256_file(folder / n) != d]
            if bad or not files:
                raise ValueError(f"missing or damaged: {', '.join(bad) or 'everything'}")
            name = self._folder_name(meta)
            target, n = self.data_dir / name, 2
            while target.exists():
                target = self.data_dir / f"{name} {n}"
                n += 1
            for leftover in folder.glob("*.part"):
                leftover.unlink()
            shutil.move(str(folder), str(target))
            info = {k: v for k, v in meta.items() if k != "files"}
            _write_json(target / "pi-drive.json", {**info, "received_at": datetime.now().isoformat(timespec="seconds")})
            index[drive] = target.name
            _write_json(self.data_dir / INDEX_FILE, index)
            return target.name

    @staticmethod
    def _folder_name(meta: dict) -> str:
        try:
            start = datetime.fromtimestamp(float(meta["start_epoch"]))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            start = datetime.now()
        return f"{start:%Y-%m-%d %H%M} {DRIVE_SUFFIX}"

    def save_status(self, status: dict) -> None:
        keep = {k: status[k] for k in list(status)[:30] if isinstance(k, str)}
        keep["heard_at"] = time.time()
        _write_json(self.data_dir / STATUS_FILE, keep)


class Handler(BaseHTTPRequestHandler):
    receiver: Receiver
    server_version = "WranglerReceiver"

    def log_message(self, *args) -> None:
        pass

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if self.receiver.allowed(self.headers.get(sync.TOKEN_HEADER)):
            return True
        self._json({"error": "wrong pairing code"}, 403)
        return False

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > sync.MAX_FILE_BYTES:
            raise ValueError("too large")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if not self._authorised():
            return
        if urlparse(self.path).path == "/ping":
            return self._json({"ok": True, "name": socket.gethostname()})
        self._json({"error": "unknown"}, 404)

    def do_PUT(self) -> None:
        if not self._authorised():
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            if len(parts) != 3 or parts[0] != "drives":
                return self._json({"error": "unknown"}, 404)
            self.receiver.put_file(parts[1], parts[2], self._body(), self.headers.get(sync.SHA_HEADER))
            self._json({"ok": True})
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        if not self._authorised():
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            body = json.loads(self._body() or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            if parts == ["status"]:
                self.receiver.save_status(body)
                return self._json({"ok": True})
            if len(parts) == 3 and parts[0] == "drives" and parts[2] == "done":
                return self._json({"ok": True, "folder": self.receiver.finish(parts[1], body)})
            self._json({"error": "unknown"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)


def _answer_discovery(sock: socket.socket, http_port: int) -> None:
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            return
        try:
            if json.loads(data).get("wrangler") != "discover":
                continue
        except (ValueError, AttributeError):
            continue
        reply = {"wrangler": "receiver", "port": http_port, "name": socket.gethostname()}
        try:
            sock.sendto(json.dumps(reply).encode(), addr)
        except OSError:
            pass


def serve(data_dir: Path | None = None, host: str = "0.0.0.0", port: int = sync.HTTP_PORT,
          discovery_port: int | None = sync.DISCOVERY_PORT) -> tuple[ThreadingHTTPServer, socket.socket | None]:
    """Start listening. Raises OSError when another receiver already has the port."""
    data_dir = Path(data_dir or default_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)
    receiver = Receiver(data_dir, pairing_token(data_dir))
    handler = type("BoundReceiverHandler", (Handler,), {"receiver": receiver})
    server = ThreadingHTTPServer((host, port), handler)
    udp = None
    if discovery_port is not None:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.bind((host, discovery_port))
        except OSError:
            server.server_close()
            udp.close()
            raise
        threading.Thread(target=_answer_discovery, args=(udp, server.server_address[1]), daemon=True).start()
    return server, udp


def is_running(data_dir: Path | None = None, port: int = sync.HTTP_PORT) -> bool:
    """Whether a receiver with this PC's pairing code answers on this computer."""
    token = _read_json(Path(data_dir or default_data_dir()) / PAIRING_FILE, {}).get("token")
    if not token:
        return False  # never set up, so it can't be running with our code
    return sync.Uploader(token, port=port)._ping(f"http://127.0.0.1:{port}")


def main(argv: list[str] | None = None) -> int:
    try:
        server, udp = serve()
    except OSError:
        return 0  # already running
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if udp:
            udp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


def received_drives(data_dir: Path | None = None, limit: int = 50) -> list[dict]:
    """Drives that came from the Pi, newest first, with a few numbers for a list."""
    data_dir = Path(data_dir or default_data_dir())
    out = []
    for folder in sorted(data_dir.glob(f"* {DRIVE_SUFFIX}*"), reverse=True)[:limit]:
        if not folder.is_dir():
            continue
        meta = _read_json(folder / "pi-drive.json", {})
        doc = _read_json(folder / "report.json", {})
        out.append({
            "folder": folder.name,
            "duration_s": meta.get("duration_s"),
            "end_reason": meta.get("end_reason"),
            "recovered": bool(meta.get("recovered")),
            "mpg": (doc.get("fuel_economy") or {}).get("mpg"),
            "findings": len(doc.get("findings") or []),
            "has_report": (folder / "report.md").is_file(),
        })
    return out


def status(data_dir: Path | None = None) -> dict:
    """What the Pi said the last time it checked in, or {}."""
    return _read_json(Path(data_dir or default_data_dir()) / STATUS_FILE, {})
