"""Install the in-Jeep logger on a Raspberry Pi from the PC, with one button in the app.

The app's "In-Jeep logger" tab calls ``install``. It signs in to the Pi
over the home network (SSH, with the user name and password chosen in
Raspberry Pi Imager), copies this app's own ``wrangler_obd`` files across,
writes the logger's settings (including the pairing code, so the Pi's
drives are accepted by this PC) and runs ``pi/install.sh`` there, which
sets up the background service and the SD card protections, then restarts
the Pi.

SSH is done with the ``paramiko`` package, installed on first use.
"""

from __future__ import annotations

import importlib
import json
import posixpath
import shlex
import site
import socket
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Callable, Protocol

from . import receiver, sync

PACKAGE_DIR = Path(__file__).resolve().parent
REMOTE_TMP = "/tmp/wrangler-install"
POWER_CHOICES = ("lifepo4wered", "none")


class SetupError(RuntimeError):
    pass


class Remote(Protocol):
    def put(self, data: bytes, path: str) -> None: ...

    def run(self, command: str, stdin: str = "", echo: Callable[[str], None] = print) -> int: ...

    def close(self) -> None: ...


def ensure_paramiko(echo: Callable[[str], None]):
    try:
        return importlib.import_module("paramiko")
    except ImportError:
        pass
    echo("Installing the SSH helper (paramiko), once. This takes a minute…")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for extra in ([], ["--user"]):
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *extra, "paramiko"],
                              capture_output=True, text=True, creationflags=flags)
        if proc.returncode == 0:
            break
    else:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise SetupError("could not install paramiko: " + " / ".join(tail))
    site.addsitedir(site.getusersitepackages())
    importlib.invalidate_caches()
    return importlib.import_module("paramiko")


class SshRemote:
    def __init__(self, host: str, user: str, password: str, echo: Callable[[str], None]) -> None:
        paramiko = ensure_paramiko(echo)
        self.client = paramiko.SSHClient()
        # A Pi on the home network, set up minutes ago: there is no known key to check against yet.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(host, username=user, password=password, timeout=15,
                                allow_agent=False, look_for_keys=False)
        except paramiko.AuthenticationException as exc:
            raise SetupError("the Pi did not accept that user name and password") from exc
        except (OSError, paramiko.SSHException) as exc:
            raise SetupError(
                f"could not reach the Pi at {host} ({exc}). Check it has been on for 3 minutes, "
                "is near the WiFi, and that the name matches the one set in Raspberry Pi Imager"
            ) from exc
        self.sftp = self.client.open_sftp()

    def put(self, data: bytes, path: str) -> None:
        with self.sftp.open(path, "wb") as fh:
            fh.write(data)

    def run(self, command: str, stdin: str = "", echo: Callable[[str], None] = print) -> int:
        chan = self.client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        if stdin:
            chan.sendall(stdin.encode())
        chan.shutdown_write()
        buf = b""
        while True:
            data = chan.recv(4096)
            if not data:
                break
            buf += data
            *lines, buf = buf.split(b"\n")
            for line in lines:
                echo(line.decode("utf-8", "replace").rstrip("\r"))
        if buf:
            echo(buf.decode("utf-8", "replace"))
        return chan.recv_exit_status()

    def close(self) -> None:
        try:
            self.sftp.close()
        finally:
            self.client.close()


def local_addresses() -> list[str]:
    """This PC's addresses on the home network, given to the Pi as a first guess."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 9))  # picks the interface used for the LAN; nothing is sent
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    return [a for a in dict.fromkeys(found) if not a.startswith("127.")]


def package_files() -> dict[str, bytes]:
    """The files the Pi needs: this app's Python modules (the same version as the PC's)."""
    files = {f"wrangler_obd/{p.name}": p.read_bytes() for p in sorted(PACKAGE_DIR.glob("*.py"))}
    for name in ("install.sh", "wrangler-logger.service"):
        data = resources.files("wrangler_obd").joinpath("pi", name).read_bytes()
        files[f"pi/{name}"] = data.replace(b"\r\n", b"\n")  # a Windows copy may have CRLF line ends; bash can't run those
    return files


def logger_config(data_dir: Path, power: str, settings: dict, speed_factor: float) -> dict:
    return {
        "token": receiver.pairing_token(data_dir),
        "pc_hosts": local_addresses(),
        "receiver_port": sync.HTTP_PORT,
        "discovery_port": sync.DISCOVERY_PORT,
        "power": power,
        "speed_factor": round(speed_factor, 4),
        "tires": {"tire_in": settings.get("tire_in"), "pcm_tire_in": settings.get("pcm_tire_in")},
    }


def install(host: str, user: str, password: str, power: str, data_dir: Path, settings: dict,
            speed_factor: float, echo: Callable[[str], None] = print,
            connect: Callable[..., Remote] | None = None) -> None:
    host, user = host.strip(), user.strip()
    if not host or not user or not password:
        raise SetupError("fill in the Pi's name, user name and password")
    if power not in POWER_CHOICES:
        raise SetupError(f"unknown power board {power!r}")
    echo(f"Connecting to {host} as {user}…")
    remote = (connect or SshRemote)(host, user, password, echo)
    try:
        echo("Connected. Copying the logger to the Pi…")
        remote.run(f"rm -rf {REMOTE_TMP} && mkdir -p {REMOTE_TMP}/wrangler_obd {REMOTE_TMP}/pi", echo=lambda _: None)
        for rel, data in package_files().items():
            remote.put(data, posixpath.join(REMOTE_TMP, rel))
        config = logger_config(data_dir, power, settings, speed_factor)
        remote.put(json.dumps(config, indent=2).encode(), f"{REMOTE_TMP}/wrangler-logger.json")
        echo(f"Running the setup on the Pi (power board: {power}). This can take 5 to 15 minutes…")
        args = " ".join(shlex.quote(a) for a in ["--power", power])
        # sudo reads the password from the first line of input, so it never appears in a command line.
        status = remote.run(f"sudo -S -p '' bash {REMOTE_TMP}/pi/install.sh {args}", stdin=password + "\n", echo=echo)
        if status != 0:
            raise SetupError(f"the setup on the Pi stopped with an error (code {status}); see the lines above")
        echo("Done. The Pi is restarting. From now on it records every drive by itself.")
    finally:
        remote.close()
