"""Unit tests for L0 shared utility modules (`os_util`, `compat`, `json_io`) and their re-exports."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from pob_cli import bridge_protocol, compat, daemon, game_data, importers, json_io, os_util, trade_client
from pob_cli.cli_trade import common as cli_trade_common
from pob_cli.trade_client import utils as trade_utils


def test_read_json_dict_handles_valid_missing_non_dict_and_corrupt(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert json_io.read_json_dict(missing) is None

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"name": "大法师", "n": 1}, ensure_ascii=False), encoding="utf-8")
    assert json_io.read_json_dict(valid) == {"name": "大法师", "n": 1}

    as_list = tmp_path / "list.json"
    as_list.write_text("[1, 2, 3]", encoding="utf-8")
    assert json_io.read_json_dict(as_list) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    assert json_io.read_json_dict(corrupt) is None


def test_atomic_write_json_utf8_surrogate_fallback_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Default writes native UTF-8 and creates missing parent directories
    target = tmp_path / "nested" / "cache.json"
    json_io.atomic_write_json(target, {"gem": "大法师辅助", "v": 1})
    raw = target.read_text(encoding="utf-8")
    assert "大法师辅助" in raw
    assert json.loads(raw) == {"gem": "大法师辅助", "v": 1}

    # 2. Unpaired UTF-16 surrogate must NOT raise UnicodeEncodeError; falls back to ASCII escape
    surrogate_target = tmp_path / "surrogate.json"
    json_io.atomic_write_json(surrogate_target, {"text": "ok-\ud800-end"})
    assert surrogate_target.exists()
    loaded = json.loads(surrogate_target.read_text(encoding="utf-8"))
    assert loaded == {"text": "ok-\ud800-end"}

    # 3. Non-serializable payload fails safe without leaving temp files behind
    bad_target = tmp_path / "bad.json"
    json_io.atomic_write_json(bad_target, {"oops": object()})
    assert not bad_target.exists()
    assert not list(tmp_path.glob(".bad.json.*.tmp"))

    # 4. PermissionError retries os.replace up to 3 attempts
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src: Any, dst: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("busy")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    retry_target = tmp_path / "retry.json"
    json_io.atomic_write_json(retry_target, {"ok": True})
    assert attempts["n"] == 3
    assert json_io.read_json_dict(retry_target) == {"ok": True}


def test_compat_call_compat_and_ensure_utf8() -> None:
    def narrow(a: int, b: int = 2) -> tuple[int, int]:
        return (a, b)

    def wide(a: int, **kw: Any) -> dict[str, Any]:
        return {"a": a, **kw}

    assert compat.call_compat(narrow, 1, b=3, extra="ignored") == (1, 3)
    assert compat.call_compat(wide, 1, x=9) == {"a": 1, "x": 9}
    assert compat.call_compat(len, [1, 2, 3]) == 3

    calls: list[tuple[str, str]] = []

    class _GoodStream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            calls.append((encoding, errors))

    class _BadStream:
        def reconfigure(self, **_kw: Any) -> None:
            raise OSError("closed")

    compat.ensure_utf8(_GoodStream())
    assert calls == [("utf-8", "replace")]
    compat.ensure_utf8(_BadStream())
    compat.ensure_utf8(object())


def test_os_util_pid_liveness_and_win32_fail_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    assert os_util._is_pid_alive(os.getpid()) is True
    assert os_util._is_pid_alive(0) is False
    assert os_util._is_pid_alive(-10) is False
    assert os_util._is_pid_alive(True) is False
    assert os_util._is_pid_alive(False) is False
    assert os_util._is_pid_alive("123") is False  # type: ignore[arg-type]
    assert os_util._is_pid_alive(0x1_0000_0000) is False

    # Simulated win32 ctypes failure on another PID must fail safe to True (never break live locks)
    monkeypatch.setattr(sys, "platform", "win32")

    def boom() -> Any:
        raise OSError("ctypes unavailable")

    monkeypatch.setattr(os_util, "_get_kernel32", boom)
    other_pid = os.getpid() + 1 if os.getpid() < 0xFFFFFFFE else 2
    assert os_util._is_pid_alive(other_pid) is True


def test_os_util_lockfile_staleness_and_atomic_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "none.lock"
    assert os_util._is_lockfile_stale(missing, stale_sec=60.0) is False
    assert os_util._try_break_stale_lock(missing) is False

    # Live lock owned by current PID with fresh timestamp is not stale
    live_lock = tmp_path / "live.lock"
    live_lock.write_text(json.dumps({"pid": os.getpid(), "ts": 9_999_999_999.0}), encoding="utf-8")
    assert os_util._is_lockfile_stale(live_lock, stale_sec=60.0) is False

    # Dead-PID lockfile is detected as stale even with fresh mtime
    dead_lock = tmp_path / "dead.lock"
    dead_lock.write_text(json.dumps({"pid": 999_999_999, "ts": 9_999_999_999.0}), encoding="utf-8")
    monkeypatch.setattr(os_util, "_is_pid_alive", lambda _pid: False)
    assert os_util._is_lockfile_stale(dead_lock, stale_sec=60.0) is True

    # Atomic reclaim removes lock and leaves no .stale.* debris
    assert os_util._try_break_stale_lock(dead_lock) is True
    assert not dead_lock.exists()
    assert not list(tmp_path.glob(".dead.lock.stale.*"))

    # Old mtime triggers staleness even when payload is malformed JSON
    old_lock = tmp_path / "old.lock"
    old_lock.write_text("{corrupt", encoding="utf-8")
    os.utime(old_lock, (1.0, 1.0))
    assert os_util._is_lockfile_stale(old_lock, stale_sec=15.0) is True


def test_l0_reexport_contracts() -> None:
    assert importers._atomic_write_json is json_io.atomic_write_json
    assert trade_utils._atomic_write_json is json_io.atomic_write_json
    assert trade_client._atomic_write_json is json_io.atomic_write_json
    assert bridge_protocol.read_slot_state is json_io.read_json_dict
    assert game_data._is_pid_alive is os_util._is_pid_alive
    assert daemon._is_pid_alive is os_util._is_pid_alive
    assert callable(cli_trade_common._default_target_slot)
    assert cli_trade_common._TargetSlotType.name == "slot"
