"""pob's own updates (updater/self_update.py, cli_self_update.py): notice, signed download, swap."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ed25519_sign import TEST_PUBLIC_KEY, sign_sums_text

from pob_cli import __version__, cli_self_update
from pob_cli.cli import main
from pob_cli.updater import UpdateError, releases, self_update, signature

BASE = "https://github.com/exilemoulei/pob4agents/releases"
NEW = "99.0.0"


class _Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self.url = ""


class _Client:
    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> _Resp:
        return self.routes.get(url, _Resp(404))


def _build(version: str, platform: str) -> bytes:
    exe = "pob.exe" if platform.startswith("windows") else "pob"
    script = f'#!/bin/sh\necho "pob, version {version}"\n'.encode()
    buf = io.BytesIO()
    if platform.startswith("windows"):
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(exe, script)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(exe)
            info.size, info.mode = len(script), 0o755
            tf.addfile(info, io.BytesIO(script))
    return buf.getvalue()


@pytest.fixture
def fake_release(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Resp]:
    platform = "linux-x64"
    name = self_update.build_asset_name(NEW, platform)
    data = _build(NEW, platform)
    sums = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    routes = {
        f"{BASE}/latest": _Resp(302, headers={"location": f"{BASE}/tag/v{NEW}"}),
        f"{BASE}/download/v{NEW}/SHA256SUMS.txt": _Resp(200, sums),
        f"{BASE}/download/v{NEW}/SHA256SUMS.txt.sig": _Resp(200, sign_sums_text(sums).encode()),
        f"{BASE}/download/v{NEW}/{name}": _Resp(200, data),
    }
    monkeypatch.setattr(signature, "RELEASE_PUBLIC_KEYS", (TEST_PUBLIC_KEY,))
    monkeypatch.setattr(sys.modules["pob_cli.updater"], "_make_http_client", lambda **_kw: _Client(routes))
    monkeypatch.setattr(releases, "is_windows", lambda: False)
    monkeypatch.setattr(releases, "get_posix_platform_tag", lambda: platform)
    monkeypatch.setattr(self_update, "is_windows", lambda: False)
    return routes


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell script as the new build")
def test_download_verifies_and_unpacks_the_new_build(fake_release: Any, tmp_path: Path) -> None:
    result = self_update.download_release(tmp_path / "staging")
    assert result["version"] == NEW
    assert (result["dir"] / "pob").is_file()


def test_nothing_newer_means_up_to_date(fake_release: Any, tmp_path: Path) -> None:
    fake_release[f"{BASE}/latest"] = _Resp(302, headers={"location": f"{BASE}/tag/v{__version__}"})
    assert self_update.download_release(tmp_path / "s") == {"tag": f"v{__version__}", "upToDate": True}


@pytest.mark.parametrize("damage", ["unsigned", "tampered", "no-build"])
def test_refuses_unverified_builds(fake_release: Any, tmp_path: Path, damage: str) -> None:
    name = self_update.build_asset_name(NEW, "linux-x64")
    if damage == "unsigned":
        del fake_release[f"{BASE}/download/v{NEW}/SHA256SUMS.txt.sig"]
    elif damage == "tampered":
        fake_release[f"{BASE}/download/v{NEW}/{name}"] = _Resp(200, b"swapped")
    else:
        fake_release[f"{BASE}/download/v{NEW}/SHA256SUMS.txt"] = _Resp(200, b"")
        fake_release[f"{BASE}/download/v{NEW}/SHA256SUMS.txt.sig"] = _Resp(200, sign_sums_text(b"").encode())
    with pytest.raises(UpdateError):
        self_update.download_release(tmp_path / "s")
    assert not (tmp_path / "s" / "new" / "pob").exists()


def test_swap_posix_replaces_the_directory(tmp_path: Path) -> None:
    current, new = tmp_path / "bin", tmp_path / "bin.update" / "new"
    current.mkdir()
    (current / "pob").write_text("old", encoding="utf-8")
    new.mkdir(parents=True)
    (new / "pob").write_text("new", encoding="utf-8")
    self_update.swap_posix(current, new)
    assert (current / "pob").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "bin.old").exists()


def test_windows_swap_script_quotes_paths() -> None:
    script = self_update.windows_swap_script(42, Path("C:/Users/O'Brien/pob4agents/bin"), Path("C:/x/new"))
    assert "Wait-Process -Id 42" in script
    assert "O''Brien" in script and "O'Brien" not in script.replace("O''Brien", "")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell swap")
def test_windows_swap_script_runs(tmp_path: Path) -> None:
    current, new = tmp_path / "bin", tmp_path / "staging" / "new"
    current.mkdir()
    (current / "pob.exe").write_text("old", encoding="utf-8")
    new.mkdir(parents=True)
    (new / "pob.exe").write_text("new", encoding="utf-8")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    script = self_update.windows_swap_script(gone.pid, current, new)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=60)
    assert (current / "pob.exe").read_text(encoding="utf-8") == "new"
    assert not new.exists() and not (tmp_path / "bin.old").exists()


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [("0.9.0", "0.8.0", True), ("v0.10.0", "0.9.9", True), ("0.9.0", "0.9.0-rc1", True),
     ("0.9.0-rc1", "0.9.0", False), ("0.8.0", "0.8.0", False), ("junk", "0.8.0", False)],
)
def test_version_order(candidate: str, current: str, newer: bool) -> None:
    assert releases.is_newer(candidate, current) is newer


def test_notice_reads_the_daily_cache(tmp_path: Path) -> None:
    assert self_update.pending_notice(tmp_path) == (None, True)
    (tmp_path / self_update.CHECK_FILENAME).write_text(
        json.dumps({"checkedAt": time.time(), "latest": f"v{NEW}"}), encoding="utf-8"
    )
    assert self_update.pending_notice(tmp_path) == (f"v{NEW}", False)
    later = time.time() + self_update.CHECK_INTERVAL_SECONDS + 1
    assert self_update.pending_notice(tmp_path, now=later) == (f"v{NEW}", True)


def test_refresh_writes_the_cache(fake_release: Any, tmp_path: Path) -> None:
    assert self_update.refresh_check(tmp_path) == f"v{NEW}"
    assert self_update.read_check(tmp_path)["latest"] == f"v{NEW}"


def test_cli_notice_only_on_a_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / self_update.CHECK_FILENAME).write_text(
        json.dumps({"checkedAt": 0, "latest": f"v{NEW}"}), encoding="utf-8"
    )
    spawned: list[Path] = []
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_OFFLINE", raising=False)
    monkeypatch.delenv("POB_NO_UPDATE_NOTICE", raising=False)
    monkeypatch.setattr(self_update, "spawn_background_check", spawned.append)
    monkeypatch.setattr(cli_self_update, "_stderr_is_terminal", lambda: True)
    shown: list[str] = []
    monkeypatch.setattr(cli_self_update, "_echo_stderr_utf8", shown.append)
    result = CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert result.exit_code == 0, result.output
    assert spawned and shown and f"pob {NEW} is available" in shown[0]
    monkeypatch.setenv("POB_NO_UPDATE_NOTICE", "1")
    shown.clear()
    CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert shown == []


def test_cli_points_other_installs_at_their_updater(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "install_method", lambda: "uv")
    result = CliRunner().invoke(main, ["self-update"])
    assert result.exit_code == 1
    assert "installed via uv" in result.output and "install." in result.output


@pytest.mark.parametrize(
    ("frozen", "exe", "prefix", "expected"),
    [
        (True, "C:/Users/a/scoop/apps/pob/current/pob.exe", "", "scoop"),
        (True, "/home/a/.local/share/pob4agents/bin/pob", "", "standalone"),
        (False, "", "/home/a/.local/share/uv/tools/pob4agents", "uv"),
        (False, "", "/home/a/.local/pipx/venvs/pob-cli", "pipx"),
        (False, "", "/usr", "pip"),
    ],
)
def test_install_method(
    monkeypatch: pytest.MonkeyPatch, frozen: bool, exe: str, prefix: str, expected: str
) -> None:
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if exe:
        monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(sys, "prefix", prefix or sys.prefix)
    monkeypatch.setattr(self_update, "detect_source_checkout_root", lambda: None)
    assert self_update.install_method() == expected
    assert self_update.update_command(expected)


def test_standalone_build_names() -> None:
    assert self_update.build_asset_name("1.2.3", "windows-x64") == "pob4agents-1.2.3-windows-x64.zip"
    assert self_update.build_asset_name("1.2.3", "macos-arm64") == "pob4agents-1.2.3-macos-arm64.tar.gz"
    assert os.path.basename(self_update.REPO_RAW).endswith("main")


def test_no_notice_without_a_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / self_update.CHECK_FILENAME).write_text(json.dumps({"checkedAt": 0, "latest": f"v{NEW}"}), encoding="utf-8")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_OFFLINE", raising=False)
    spawned: list[Path] = []
    monkeypatch.setattr(self_update, "spawn_background_check", spawned.append)
    result = CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert result.exit_code == 0
    assert spawned == [] and "is available" not in result.output
