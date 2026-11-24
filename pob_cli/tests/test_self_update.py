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
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> _Resp:
        resp = self.routes.get(url, _Resp(404))
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _build(version: str, platform: str, exe: str | None = None) -> bytes:
    exe = exe or ("pob.exe" if platform.startswith("windows") else "pob")
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
    current = Path("C:/Users/O'Brien/pob4agents/bin")
    script = self_update.windows_swap_script(42, current, Path("C:/x/bin.update-1/new"), NEW)
    assert "$proc.Id -eq 42" in script
    assert "O''Brien" in script and "O'Brien" not in script.replace("O''Brien", "")


def test_windows_swap_script_waits_retries_and_reports() -> None:
    current = Path("C:/pob4agents/bin")
    script = self_update.windows_swap_script(42, current, Path("C:/pob4agents/bin.update-1/new"), NEW)
    # Waits for every process started from the install folder (the daemon too), not only this one.
    assert "$path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)" in script
    assert "AddSeconds(120)" in script
    # Retries the move with backoff, then removes its own staging folder.
    assert "if ($i -ge 7) { throw }" in script and "$delay *= 2" in script
    assert f"$staging = '{Path('C:/pob4agents/bin.update-1')}'" in script
    # Records the outcome where the next pob run looks for it.
    assert f"$result = '{self_update.swap_result_path(current)}'" in script
    assert "Write-Result $false $_.Exception.Message" in script and f"version = '{NEW}'" in script
    # A failed swap drops its download unless the rollback failed too.
    assert "if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $staging" in script


def test_schedule_windows_swap_starts_outside_the_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(self_update.subprocess, "Popen", lambda cmd, **kw: started.append({"cmd": cmd, **kw}))
    current = tmp_path / "bin"
    self_update.schedule_windows_swap(current, tmp_path / "bin.update-1" / "new", NEW)
    assert started[0]["cwd"] == str(tmp_path)
    assert started[0]["cmd"][:2] == ["powershell", "-NoProfile"]
    assert started[0]["stderr"] == subprocess.DEVNULL


def test_schedule_windows_swap_without_powershell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError("powershell")

    monkeypatch.setattr(self_update.subprocess, "Popen", missing)
    with pytest.raises(UpdateError, match="PowerShell"):
        self_update.schedule_windows_swap(tmp_path / "bin", tmp_path / "bin.update-1" / "new", NEW)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell swap")
def test_windows_swap_script_runs(tmp_path: Path) -> None:
    current, new = tmp_path / "bin", tmp_path / "staging" / "new"
    current.mkdir()
    (current / "pob.exe").write_text("old", encoding="utf-8")
    new.mkdir(parents=True)
    (new / "pob.exe").write_text("new", encoding="utf-8")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    script = self_update.windows_swap_script(gone.pid, current, new, NEW)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=60, cwd=tmp_path
    )
    assert (current / "pob.exe").read_text(encoding="utf-8") == "new"
    assert not new.exists() and not (tmp_path / "bin.old").exists() and not (tmp_path / "staging").exists()
    assert self_update.take_swap_result(current) == {"ok": True, "version": NEW, "error": ""}


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


# --- Faults: a damaged cache, the network, the disk, a second update, a failed Windows swap ------


@pytest.mark.parametrize("checked_at", ["x", [1], {"a": 1}, None, True, float("nan"), float("inf"), 10**400])
def test_damaged_cache_counts_as_never_checked(tmp_path: Path, checked_at: object) -> None:
    (tmp_path / self_update.CHECK_FILENAME).write_text(
        json.dumps({"checkedAt": checked_at, "latest": f"v{NEW}"}), encoding="utf-8"
    )
    assert self_update.pending_notice(tmp_path) == (f"v{NEW}", True)


def _terminal_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_OFFLINE", raising=False)
    monkeypatch.delenv("POB_NO_UPDATE_NOTICE", raising=False)
    monkeypatch.setattr(self_update, "spawn_background_check", lambda _dir: None)
    monkeypatch.setattr(cli_self_update, "_stderr_is_terminal", lambda: True)
    shown: list[str] = []
    monkeypatch.setattr(cli_self_update, "_echo_stderr_utf8", shown.append)
    return shown


def test_damaged_cache_never_breaks_a_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shown = _terminal_notice(monkeypatch, tmp_path)
    (tmp_path / self_update.CHECK_FILENAME).write_text('{"checkedAt": "x", "latest": ["v9"]}', encoding="utf-8")
    result = CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert result.exit_code == 0, result.output
    assert shown == []


def test_notice_failure_never_breaks_a_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _terminal_notice(monkeypatch, tmp_path)

    def broken(*_a: object, **_kw: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(self_update, "pending_notice", broken)
    result = CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("error", ["connect", "timeout"])
def test_network_errors_are_update_errors(fake_release: Any, tmp_path: Path, error: str) -> None:
    import httpx

    url = f"{BASE}/download/v{NEW}/{self_update.build_asset_name(NEW, 'linux-x64')}"
    exc = httpx.ConnectError("refused") if error == "connect" else httpx.ReadTimeout("timed out")
    fake_release[url] = exc
    with pytest.raises(UpdateError, match=f"Downloading v{NEW} failed"):
        self_update.download_release(tmp_path / "s")


def test_unwritable_staging_is_an_update_error(fake_release: Any, tmp_path: Path) -> None:
    (tmp_path / "file").write_text("not a directory", encoding="utf-8")
    with pytest.raises(UpdateError, match="Could not unpack"):
        self_update.download_release(tmp_path / "file" / "staging")


def _replace_build(routes: dict[str, Any], data: bytes) -> None:
    name = self_update.build_asset_name(NEW, "linux-x64")
    sums = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    routes[f"{BASE}/download/v{NEW}/{name}"] = _Resp(200, data)
    routes[f"{BASE}/download/v{NEW}/SHA256SUMS.txt"] = _Resp(200, sums)
    routes[f"{BASE}/download/v{NEW}/SHA256SUMS.txt.sig"] = _Resp(200, sign_sums_text(sums).encode())


def test_archive_without_pob_is_an_update_error(fake_release: Any, tmp_path: Path) -> None:
    _replace_build(fake_release, _build(NEW, "linux-x64", exe="other"))
    with pytest.raises(UpdateError, match="has no pob at its root"):
        self_update.download_release(tmp_path / "s")


def test_hung_new_build_is_an_update_error(
    fake_release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(cmd: list[str], **_kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(self_update.subprocess, "run", hang)
    with pytest.raises(UpdateError, match="does not run here"):
        self_update.download_release(tmp_path / "s")


@pytest.mark.parametrize(("printed", "ok"), [(f"pob, version {NEW}\n", True), (f"pob, version {NEW}1\n", False)])
def test_new_build_must_print_exactly_the_version(
    fake_release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, printed: str, ok: bool
) -> None:
    # Substring matching would accept 99.0.01 for 99.0.0.
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, printed, "")
    )
    if ok:
        assert self_update.download_release(tmp_path / "s")["version"] == NEW
    else:
        with pytest.raises(UpdateError, match="does not run here"):
            self_update.download_release(tmp_path / "s")


def test_update_lock_is_exclusive(tmp_path: Path) -> None:
    current = tmp_path / "bin"
    current.mkdir()
    with self_update.update_lock(current):
        with pytest.raises(UpdateError, match="Another pob self-update"):
            with self_update.update_lock(current):
                pass
    with self_update.update_lock(current):
        pass  # released on exit


def test_staging_is_unique_per_run(tmp_path: Path) -> None:
    current = tmp_path / "bin"
    current.mkdir()
    a, b = self_update.new_staging(current), self_update.new_staging(current)
    assert a != b and a.parent == b.parent == tmp_path and a.name.startswith("bin.update-")


def _standalone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    current = tmp_path / "bin"
    current.mkdir()
    (current / "pob").write_text("old", encoding="utf-8")
    monkeypatch.setattr(self_update, "install_method", lambda: "standalone")
    monkeypatch.setattr(self_update, "app_dir", lambda: current)
    monkeypatch.setattr(cli_self_update, "_stop_daemon_quietly", lambda: None)
    return current


def _leftovers(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("bin.update-"))


def test_cli_network_failure_exits_5_cleanly(
    fake_release: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    _standalone(monkeypatch, tmp_path)
    fake_release[f"{BASE}/download/v{NEW}/SHA256SUMS.txt"] = httpx.ConnectError("refused")
    result = CliRunner().invoke(main, ["self-update"])
    assert result.exit_code == 5, result.output
    assert f"Error: Downloading v{NEW} failed: refused" in result.output
    assert "Traceback" not in result.output and _leftovers(tmp_path) == []


def test_cli_swap_failure_exits_5_and_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _standalone(monkeypatch, tmp_path)
    monkeypatch.setattr(self_update, "is_windows", lambda: False)

    def downloaded(staging: Path, **_kw: object) -> dict[str, Any]:
        (staging / "new").mkdir()
        return {"tag": f"v{NEW}", "version": NEW, "dir": staging / "new"}

    def denied(*_a: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(self_update, "download_release", downloaded)
    monkeypatch.setattr(self_update, "swap_posix", denied)
    result = CliRunner().invoke(main, ["self-update"])
    assert result.exit_code == 5, result.output
    assert f"Error: Could not replace {current}: denied" in result.output
    assert _leftovers(tmp_path) == []


def test_cli_refuses_a_second_concurrent_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _standalone(monkeypatch, tmp_path)
    with self_update.update_lock(current):
        result = CliRunner().invoke(main, ["self-update"])
    assert result.exit_code == 5, result.output
    assert "Another pob self-update is running" in result.output


def test_cli_windows_refuses_a_cwd_inside_the_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _standalone(monkeypatch, tmp_path)
    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    monkeypatch.chdir(current)
    result = CliRunner().invoke(main, ["self-update"])
    assert result.exit_code == 1, result.output
    assert "cd elsewhere" in result.output


def test_cli_windows_leaves_the_swap_to_the_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = _standalone(monkeypatch, tmp_path)
    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    monkeypatch.chdir(tmp_path)
    scheduled: list[tuple[Any, ...]] = []

    def downloaded(staging: Path, **_kw: object) -> dict[str, Any]:
        (staging / "new").mkdir()
        return {"tag": f"v{NEW}", "version": NEW, "dir": staging / "new"}

    monkeypatch.setattr(self_update, "download_release", downloaded)
    monkeypatch.setattr(self_update, "schedule_windows_swap", lambda *a: scheduled.append(a))
    result = CliRunner().invoke(main, ["self-update", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["updated"] == "pending"
    assert scheduled[0][0] == current and scheduled[0][2] == NEW
    assert scheduled[0][1].is_dir()  # the helper still needs staging/new


@pytest.mark.parametrize("ok", [True, False])
def test_next_run_reports_a_failed_windows_swap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ok: bool) -> None:
    current = tmp_path / "bin"
    current.mkdir()
    result_file = self_update.swap_result_path(current)
    payload = {"ok": ok, "version": NEW, "error": "" if ok else "Access to the path is denied."}
    # Windows PowerShell 5.1 writes UTF-8 with a BOM.
    result_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_update, "app_dir", lambda: current)
    monkeypatch.setenv("POB_NO_UPDATE_NOTICE", "1")
    shown: list[str] = []
    monkeypatch.setattr(cli_self_update, "_echo_stderr_utf8", shown.append)
    result = CliRunner().invoke(main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert result.exit_code == 0, result.output
    assert not result_file.exists()
    if ok:
        assert shown == []
    else:
        assert len(shown) == 1 and f"self-update to {NEW} did not finish: Access to the path is denied." in shown[0]
