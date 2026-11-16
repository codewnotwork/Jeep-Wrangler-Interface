"""Getting finished drives from the in-Jeep Pi to the PC over home WiFi.

The PC runs the drive receiver (``receiver.py``): a small web server on
port ``HTTP_PORT`` that answers "who's there?" broadcasts on
``DISCOVERY_PORT``. The Pi (``carlogger.py``) uses ``Uploader`` here to find
it and send each drive's files, then asks the receiver to check them.
Nothing leaves the home network.

Every request carries the pairing code the installer gave the Pi, so the
PC accepts drives only from that Pi. Each file goes with its SHA-256, and
the receiver files a drive only once every file has arrived intact, so a
WiFi drop halfway through just means the drive is sent again next time.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

HTTP_PORT = 47800
DISCOVERY_PORT = 47801
TOKEN_HEADER = "X-Wrangler-Token"
SHA_HEADER = "X-Sha256"
DISCOVER = {"wrangler": "discover"}
#: Drive ids and file names allowed on the wire: no paths, no surprises.
DRIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
MAX_FILE_BYTES = 200 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class ReceiverError(IOError):
    pass


class Uploader:
    """The Pi's side: find the PC's receiver and send drives to it."""

    def __init__(self, token: str, hosts: list[str] | tuple[str, ...] = (), port: int = HTTP_PORT,
                 discovery_port: int = DISCOVERY_PORT, timeout: float = 15.0) -> None:
        self.token, self.hosts, self.port = token, list(hosts), port
        self.discovery_port, self.timeout = discovery_port, timeout
        self.base: str | None = None  #: the receiver found last time, tried first

    # -- finding the PC ---------------------------------------------------

    def _request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None,
                 timeout: float | None = None) -> dict:
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={TOKEN_HEADER: self.token, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error", "")
            except ValueError:
                detail = ""
            raise ReceiverError(f"the PC refused {method} {url}: {exc.code} {detail}".strip()) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ReceiverError(f"could not reach {url}: {exc}") from exc

    def _ping(self, base: str) -> bool:
        try:
            return bool(self._request("GET", f"{base}/ping", timeout=3).get("ok"))
        except ReceiverError:
            return False

    def discover(self, wait_s: float = 2.0) -> list[str]:
        """Broadcast on the local network and collect the receivers that answer."""
        found: list[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return found
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.3)
            payload = json.dumps(DISCOVER).encode()
            for target in ("255.255.255.255", "<broadcast>"):
                try:
                    sock.sendto(payload, (target, self.discovery_port))
                except OSError:
                    pass
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                try:
                    data, (ip, _) = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    reply = json.loads(data)
                except ValueError:
                    continue
                if reply.get("wrangler") == "receiver":
                    base = f"http://{ip}:{int(reply.get('port') or self.port)}"
                    if base not in found:
                        found.append(base)
        finally:
            sock.close()
        return found

    def find(self) -> str | None:
        """The receiver's address, or None when no PC is answering (away from home, PC off)."""
        candidates = ([self.base] if self.base else []) + [f"http://{h}:{self.port}" for h in self.hosts]
        for base in dict.fromkeys(candidates):
            if self._ping(base):
                self.base = base
                return base
        for base in self.discover():
            if self._ping(base):
                self.base = base
                return base
        self.base = None
        return None

    # -- sending ---------------------------------------------------------------

    def upload_drive(self, folder: Path, drive_id: str, meta: dict) -> str:
        """Send every file in ``folder`` and have the PC file the drive. Returns the PC's folder name."""
        if not self.base:
            raise ReceiverError("no receiver")
        files = {}
        for path in sorted(Path(folder).iterdir()):
            if not path.is_file() or not FILE_NAME.match(path.name):
                continue
            digest = sha256_file(path)
            files[path.name] = digest
            self._request("PUT", f"{self.base}/drives/{drive_id}/{path.name}", data=path.read_bytes(),
                          headers={SHA_HEADER: digest, "Content-Type": "application/octet-stream"},
                          timeout=max(self.timeout, 60))
        body = json.dumps({**meta, "files": files}).encode()
        reply = self._request("POST", f"{self.base}/drives/{drive_id}/done", data=body,
                              headers={"Content-Type": "application/json"})
        return str(reply.get("folder") or "")

    def send_status(self, status: dict) -> None:
        if self.base:
            self._request("POST", f"{self.base}/status", data=json.dumps(status).encode(),
                          headers={"Content-Type": "application/json"}, timeout=5)
