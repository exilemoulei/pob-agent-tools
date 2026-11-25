"""call_daemon must never resend an action or start a second daemon while the first one is alive."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from typing import Any

import pytest

from pob_cli import daemon
from pob_cli.daemon.client import call_daemon

_DEAD_PID = 2**22 + 12345


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pid: int, exc: BaseException) -> dict[str, Any]:
    state_file = tmp_path / "daemon.json"
    state_file.write_text(json.dumps({"pid": pid, "port": 40000, "token": "t"}), encoding="utf-8")
    seen: dict[str, Any] = {"posts": 0, "spawned": 0, "state_file": state_file}

    def fake_post(port: int, token: str, payload: dict[str, Any], **_: Any) -> tuple[int, dict[str, Any]]:
        seen["posts"] += 1
        if seen["spawned"]:
            return 200, {"ok": True, "result": {"from": "new daemon"}}
        raise exc

    def fake_ensure(rt_dir: Path, **_: Any) -> tuple[dict[str, Any], None]:
        seen["spawned"] += 1
        return {"pid": os.getpid(), "port": 40001, "token": "t2"}, None

    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "_http_post_rpc", fake_post)
    monkeypatch.setattr(daemon, "_ensure_daemon", fake_ensure)
    monkeypatch.setattr(daemon.client, "_is_pid_alive", lambda p: p == os.getpid())
    return seen


@pytest.mark.parametrize(
    "exc",
    [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        ConnectionResetError("connection reset by peer"),
        http.client.BadStatusLine("garbage"),
    ],
)
def test_live_daemon_that_drops_the_reply_is_not_replaced_or_sent_the_action_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException
) -> None:
    seen = _setup(monkeypatch, tmp_path, os.getpid(), exc)

    with pytest.raises(RuntimeError, match="may still be running"):
        call_daemon("apply_batch", {"ops": []}, runtime_dir=tmp_path, emit_cold_start_alert=False)

    assert seen["posts"] == 1
    assert seen["spawned"] == 0
    assert seen["state_file"].is_file()


def test_refused_connection_means_no_daemon_got_the_action_so_a_new_one_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _setup(monkeypatch, tmp_path, os.getpid(), ConnectionRefusedError("refused"))

    res = call_daemon("status", runtime_dir=tmp_path, emit_cold_start_alert=False)

    assert res == {"from": "new daemon"}
    assert seen["spawned"] == 1


def test_dead_daemon_is_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _setup(monkeypatch, tmp_path, _DEAD_PID, http.client.RemoteDisconnected("closed"))

    res = call_daemon("status", runtime_dir=tmp_path, emit_cold_start_alert=False)

    assert res == {"from": "new daemon"}
    assert seen["spawned"] == 1
    assert seen["posts"] == 1  # a dead pid is replaced before anything is sent to it
