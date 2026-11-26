"""Windows windowless subprocess launch tests and DETACHED_PROCESS regression guard."""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from pob_cli import bridge_client, daemon, os_util
from pob_cli.updater import self_update
from pob_cli.updater import state as updater_state

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "pob_cli"


class _FakeStartupInfo:
    dwFlags: int = 0
    wShowWindow: int = 0


def _ensure_win32_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        monkeypatch.setattr(sys, "platform", "win32")
        for attr, val in (
            ("CREATE_NEW_PROCESS_GROUP", 0x00000200), ("DETACHED_PROCESS", 0x00000008),
            ("CREATE_NO_WINDOW", 0x08000000), ("STARTF_USESHOWWINDOW", 0x00000001),
            ("SW_HIDE", 0), ("STARTUPINFO", _FakeStartupInfo),
        ):
            monkeypatch.setattr(subprocess, attr, val, raising=False)


def _winerror5() -> OSError:
    err = OSError("WinError 5")
    err.winerror = 5  # type: ignore[attr-defined]
    return err


def _assert_hidden_win_kwargs(
    kwargs: dict[str, Any], *, detached: bool, expect_breakaway: bool | None = None
) -> None:
    flags = int(kwargs.get("creationflags", 0))
    assert (flags & subprocess.CREATE_NO_WINDOW) and (flags & subprocess.DETACHED_PROCESS) == 0
    si = kwargs.get("startupinfo")
    assert si is not None and (si.dwFlags & subprocess.STARTF_USESHOWWINDOW)
    assert si.wShowWindow == getattr(subprocess, "SW_HIDE", 0)
    if detached:
        assert (flags & subprocess.CREATE_NEW_PROCESS_GROUP) and kwargs.get("close_fds") is True
        if expect_breakaway is not None:
            assert bool(flags & os_util._CREATE_BREAKAWAY_FROM_JOB) is expect_breakaway


@pytest.mark.parametrize("exc_factory", [lambda: PermissionError("denied"), _winerror5])
def test_daemon_spawn_win32_flags_and_breakaway_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_factory: Any
) -> None:
    _ensure_win32_env(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_popen(_cmd: Any, **kwargs: Any) -> object:
        calls.append(kwargs)
        if int(kwargs.get("creationflags", 0)) & os_util._CREATE_BREAKAWAY_FROM_JOB:
            raise exc_factory()
        return object()

    monkeypatch.setattr(daemon.subprocess, "Popen", _fake_popen)
    assert daemon._spawn_daemon_process(["python", "-m", "pob_cli.daemon"], tmp_path, io.StringIO()) is not None
    assert len(calls) == 2 and calls[0]["stdin"] == daemon.subprocess.DEVNULL
    _assert_hidden_win_kwargs(calls[0], detached=True, expect_breakaway=True)
    _assert_hidden_win_kwargs(calls[1], detached=True, expect_breakaway=False)


def test_spawn_detached_reraises_non_access_denied_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_win32_env(monkeypatch)
    attempts: list[int] = []

    def _missing(*_a: Any, **_kw: Any) -> object:
        attempts.append(1)
        raise FileNotFoundError("no such exe")

    monkeypatch.setattr(subprocess, "Popen", _missing)
    with pytest.raises(FileNotFoundError):
        os_util.spawn_detached_process(["nonexistent-exe"])
    assert len(attempts) == 1


@pytest.mark.parametrize("mode", ["release-check", "windows-swap"])
def test_self_update_detached_spawns_win32_flags_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _ensure_win32_env(monkeypatch)
    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    calls: list[dict[str, Any]] = []

    def _fake_popen(cmd: Any, **kwargs: Any) -> object:
        calls.append({"cmd": cmd, **kwargs})
        if int(kwargs.get("creationflags", 0)) & os_util._CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("denied") if mode == "release-check" else _winerror5()
        return object()

    monkeypatch.setattr(self_update.subprocess, "Popen", _fake_popen)
    if mode == "release-check":
        self_update.spawn_background_check(tmp_path)
        assert "_release-check" in calls[0]["cmd"]
    else:
        self_update.schedule_windows_swap(tmp_path / "bin", tmp_path / "bin.update-1" / "new", "99.0.0")
        assert calls[0]["cmd"][:2] == ["powershell", "-NoProfile"]
    assert len(calls) == 2 and calls[0]["stdin"] == calls[0]["stdout"] == calls[0]["stderr"] == subprocess.DEVNULL
    _assert_hidden_win_kwargs(calls[0], detached=True, expect_breakaway=True)
    _assert_hidden_win_kwargs(calls[1], detached=True, expect_breakaway=False)


def test_unpack_and_check_version_win32_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_win32_env(monkeypatch)
    monkeypatch.setattr(self_update, "is_windows", lambda: True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pob.exe", b"MZ-fake")
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        self_update.subprocess, "run",
        lambda cmd, **kw: seen.append({"cmd": cmd, **kw}) or subprocess.CompletedProcess(cmd, 0, "pob, version 99.0.0\n", ""),
    )
    out = self_update._unpack_and_check(
        tmp_path / "staging", "pob4agents-99.0.0-windows-x64.zip", buf.getvalue(), "v99.0.0", "99.0.0"
    )
    assert (out / "pob.exe").is_file() and len(seen) == 1 and seen[0]["cmd"][-1] == "--version"
    _assert_hidden_win_kwargs(seen[0], detached=False)


def test_probe_engine_gate_and_bridge_worker_win32_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_win32_env(monkeypatch)
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    monkeypatch.setattr(sys.modules["pob_cli.updater"], "build_engine_command", lambda _eng: (["fake-lua"], {}))
    monkeypatch.setattr(bridge_client, "build_engine_command", lambda _eng: (["fake-lua"], {}))
    gate_seen: list[dict[str, Any]] = []

    class _FakeGateProc:
        stdin, stderr = io.StringIO(), io.StringIO("")
        stdout = io.StringIO(
            json.dumps({"event": "gate_result", "data": {"ok": True}}) + "\n"
            + json.dumps({"event": "hello", "data": {"bridge": "test", "pobVersion": "2.68.0"}}) + "\n"
            + json.dumps({"id": 1, "result": {"pobVersion": "2.68.0", "caps": {}}}) + "\n"
        )
        kill = lambda self: None  # noqa: E731
        wait = lambda self, timeout=None: 0  # noqa: E731

    monkeypatch.setattr(updater_state.subprocess, "Popen", lambda _c, **kw: gate_seen.append(kw) or _FakeGateProc())
    assert updater_state.probe_engine_gate(eng, timeout_sec=5.0)["ok"] is True and len(gate_seen) == 1
    _assert_hidden_win_kwargs(gate_seen[0], detached=False)

    bridge_seen: list[dict[str, Any]] = []

    def _abort_popen(_cmd: Any, **kw: Any) -> Any:
        bridge_seen.append(kw)
        raise OSError("sentinel-stop-after-capture")

    monkeypatch.setattr(bridge_client.subprocess, "Popen", _abort_popen)
    worker = bridge_client.BridgeWorker(slot_name="win-flags", pob_dir=eng, runtime_dir=tmp_path / "rt")
    with pytest.raises(bridge_client.BridgeError, match="sentinel-stop-after-capture"):
        worker._launch_unlocked()
    assert len(bridge_seen) == 1
    _assert_hidden_win_kwargs(bridge_seen[0], detached=False)


def test_posix_helpers_omit_win32_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    assert os_util.win_hidden_startupinfo(windows=False) is None
    assert os_util.win_hidden_subprocess_kwargs(windows=False) == {}
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda _c, **kw: seen.append(kw) or object())
    os_util.spawn_detached_process(["echo", "hi"], windows=False)
    assert seen[0].get("start_new_session") is True and seen[0].get("close_fds") is True
    assert "creationflags" not in seen[0] and "startupinfo" not in seen[0]


def test_ast_guard_no_detached_process_and_all_subprocesses_hidden() -> None:
    detached_hits: list[str] = []
    unwrapped: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
            if (isinstance(node, ast.Attribute) and node.attr == "DETACHED_PROCESS") or (
                isinstance(node, ast.Name) and node.id == "DETACHED_PROCESS"
            ):
                detached_hits.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == "DETACHED_PROCESS"
                ):
                    detached_hits.append(f"{rel}:{node.lineno}")
                if (
                    rel != "os_util.py" and isinstance(fn, ast.Attribute)
                    and fn.attr in ("Popen", "run", "call", "check_call", "check_output")
                    and isinstance(fn.value, ast.Name) and fn.value.id in ("subprocess", "sub")
                    and not any(
                        kw.arg is None and isinstance(kw.value, ast.Call)
                        and isinstance(kw.value.func, ast.Name)
                        and kw.value.func.id == "win_hidden_subprocess_kwargs"
                        for kw in node.keywords
                    )
                ):
                    unwrapped.append(f"{rel}:{node.lineno}: {fn.value.id}.{fn.attr}")
    assert not detached_hits, "DETACHED_PROCESS forbidden in pob_cli src:\n" + "\n".join(detached_hits)
    assert not unwrapped, "Subprocess spawns must use os_util hidden/detached helpers:\n" + "\n".join(unwrapped)
