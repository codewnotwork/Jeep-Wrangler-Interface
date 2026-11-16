import io
import json
import urllib.error
import zipfile

from wrangler_obd import updater

SHA = "a" * 40


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


GOOD = {
    "Jeep-Wrangler-Interface-aaaa/Wrangler Diagnostics.pyw": "launcher v2",
    "Jeep-Wrangler-Interface-aaaa/src/wrangler_obd/app.py": "app v2",
    "Jeep-Wrangler-Interface-aaaa/src/wrangler_obd/updater.py": "updater v2",
    "Jeep-Wrangler-Interface-aaaa/.github/workflows/tests.yml": "ci",
}


def fake_github(monkeypatch, zip_files=GOOD, error=None):
    def get(url, accept="application/vnd.github+json"):
        if error:
            raise error
        if "api.github.com" in url:
            return json.dumps({"sha": SHA, "commit": {"committer": {"date": "2026-11-17T08:00:00Z"},
                                                      "message": "Add auto-update\n\nDetails"}}).encode()
        assert url.endswith(SHA)
        return make_zip(zip_files)

    monkeypatch.setattr(updater, "_get", get)


def test_installs_newer_version(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "src" / "wrangler_obd").mkdir(parents=True)
    (app / "src" / "wrangler_obd" / "app.py").write_text("app v1")
    (app / "notes-of-mine.txt").write_text("keep me")
    fake_github(monkeypatch)
    r = updater.check_and_update(app, tmp_path / "data" / "update.json")
    assert r["status"] == "updated" and r["installed"]["message"] == "Add auto-update"
    assert (app / "src" / "wrangler_obd" / "app.py").read_text() == "app v2"
    assert (app / "notes-of-mine.txt").read_text() == "keep me"
    assert not (app / ".github").exists()
    assert updater.installed(app)["commit"] == SHA
    assert json.loads((tmp_path / "data" / "update.json").read_text())["status"] == "updated"
    # Second start: nothing new.
    assert updater.check_and_update(app)["status"] == "current"


def test_refuses_bad_downloads(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    fake_github(monkeypatch, {**GOOD, "Jeep-Wrangler-Interface-aaaa/../../evil.txt": "x"})
    assert updater.check_and_update(app)["status"] == "error"
    fake_github(monkeypatch, {"other/readme.txt": "not this app"})
    r = updater.check_and_update(app)
    assert r["status"] == "error" and "missing" in r["error"]
    assert updater.installed(app) == {} and not (tmp_path / "evil.txt").exists()


def test_offline_private_and_developer(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    fake_github(monkeypatch, error=urllib.error.URLError("no network"))
    assert updater.check_and_update(app)["status"] == "offline"
    fake_github(monkeypatch, error=urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    assert updater.check_and_update(app)["status"] == "private"
    (app / ".git").mkdir()
    assert updater.check_and_update(app)["status"] == "developer"
