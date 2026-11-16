"""Keep the app up to date from GitHub.

The launcher calls ``check_and_update`` before it loads the rest of the app,
so a new version takes effect on the same start. It asks GitHub for the
newest commit on ``main``, and when that differs from the one installed
(recorded in ``version.json`` next to the launcher), downloads that commit's
ZIP and copies its files over the app folder. Recordings and settings live
in ``Documents/jeep-data`` and are never touched.

It needs the repository to be public (no password or token is stored on the
computer) and an internet connection. Offline, in the truck, the check gives
up after a few seconds and the installed version starts as usual.

This module uses only the standard library and imports nothing else from the
package, so the launcher can load it on its own before the update.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = "codewnotwork/Jeep-Wrangler-Interface"
BRANCH = "main"
TIMEOUT_S = 4.0
MAX_ZIP_BYTES = 20 * 1024 * 1024
#: Files that must be in a download for it to count as this app.
REQUIRED = ("Wrangler Diagnostics.pyw", "src/wrangler_obd/app.py", "src/wrangler_obd/updater.py")
VERSION_FILE = "version.json"
USER_AGENT = "wrangler-diagnostics-updater"


class UpdateError(Exception):
    pass


def installed(app_dir: Path) -> dict:
    """The installed version: ``{"commit", "date", "message"}``, or ``{}`` if unknown."""
    try:
        return json.loads((Path(app_dir) / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise UpdateError("the download is larger than expected")
    return data


def latest() -> dict:
    """The newest commit on ``main``: ``{"commit", "date", "message"}``."""
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("private") from exc
        raise
    return {
        "commit": data["sha"],
        "date": data["commit"]["committer"]["date"],
        "message": data["commit"]["message"].splitlines()[0][:200],
    }


def install(app_dir: Path, version: dict) -> None:
    """Download ``version``'s files and copy them over ``app_dir``."""
    app_dir = Path(app_dir)
    blob = _get(f"https://codeload.github.com/{REPO}/zip/{version['commit']}", accept="application/zip")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf, tempfile.TemporaryDirectory() as tmp:
        names = zf.namelist()
        top = names[0].split("/")[0] + "/" if names else ""
        for name in names:
            # Refuse anything that would land outside the folder.
            if not name.startswith(top) or ".." in Path(name).parts or name.startswith("/"):
                raise UpdateError(f"unexpected file in the download: {name}")
        for need in REQUIRED:
            if top + need not in names:
                raise UpdateError(f"the download is missing {need}")
        zf.extractall(tmp)
        src = Path(tmp) / top.rstrip("/")
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if rel.parts[0] in (".git", ".github"):
                continue
            dest = app_dir / rel
            if path.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
    (app_dir / VERSION_FILE).write_text(json.dumps(version, indent=2), encoding="utf-8")


def check_and_update(app_dir: Path, status_file: Path | None = None) -> dict:
    """Update if a newer version is out. Never raises: returns what happened.

    ``status`` is one of ``updated``, ``current``, ``offline``, ``private``,
    ``developer`` (a git checkout, left alone) or ``error``.
    """
    app_dir = Path(app_dir)
    have = installed(app_dir)
    result: dict = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "installed": have}
    try:
        if (app_dir / ".git").exists():
            result["status"] = "developer"
        else:
            new = latest()
            result["latest"] = new
            if new["commit"] == have.get("commit"):
                result["status"] = "current"
            else:
                install(app_dir, new)
                result.update(status="updated", installed=new, previous=have)
    except UpdateError as exc:
        result.update(status="private" if str(exc) == "private" else "error", error=str(exc))
    except urllib.error.HTTPError as exc:
        result.update(status="error", error=f"GitHub answered {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result.update(status="offline", error=str(exc))
    except Exception as exc:  # an update problem must never stop the app from starting
        result.update(status="error", error=repr(exc))
    if status_file is not None:
        try:
            Path(status_file).parent.mkdir(parents=True, exist_ok=True)
            Path(status_file).write_text(json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
    return result
