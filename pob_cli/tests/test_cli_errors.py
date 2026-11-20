"""Daemon errors must surface as clean `Error: ...` messages, never Python tracebacks.

Two kinds are kept apart:
- request rejected by a healthy daemon (DaemonRequestError) -> exit 1 (arg/state error)
- the daemon itself failing (not running, boot failure, timeout, transport error) -> exit 5
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli, daemon
from pob_cli.cli import DAEMON_FAILURE_EXIT_CODE, main
from pob_cli.daemon import BootstrapMissingError, DaemonRequestError


def _raising(exc: BaseException):
    def _fake(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        raise exc

    return _fake


# --- request rejected by the daemon -> exit 1 ---------------------------------------------


def test_switch_main_loadout_without_synced_loadouts_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raising(DaemonRequestError("Build has no synced loadouts available.")))

    res = CliRunner().invoke(main, ["switch-main", "--target", "ref", "--loadout", "Late Upgrades"])

    assert res.exit_code == 1
    assert "Error: Build has no synced loadouts available." in res.output
    assert "Traceback" not in res.output
    assert "daemon failure" not in res.output


@pytest.mark.parametrize(
    "argv",
    [
        ["summary"],
        ["skills"],
        ["calc", "Ignite"],
        ["switch-main", "--spec", "missing"],
    ],
)
def test_rejected_requests_are_clean_for_any_command(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raising(DaemonRequestError("No build loaded in slot 'main'")))

    res = CliRunner().invoke(main, argv)

    assert res.exit_code == 1, res.output
    assert "Error: No build loaded in slot 'main'" in res.output
    assert "Traceback" not in res.output


def test_empty_rejection_message_names_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raising(DaemonRequestError("")))

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error: Daemon rejected 'summary'" in res.output


# --- the daemon itself failed -> exit 5 ----------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Timed out waiting for PoB daemon to start. Check daemon.log."),
        RuntimeError("PoB daemon exited during startup (code 1)."),
        RuntimeError("PoB daemon state file disappeared after startup."),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
    ],
)
def test_daemon_failures_are_distinct_from_rejections(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raising(exc))

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res.output
    assert "Error: PoB daemon failure during 'summary':" in res.output
    assert str(exc) in res.output
    assert "pob stop" in res.output
    assert "Traceback" not in res.output


def test_bootstrap_missing_still_prints_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "call_daemon",
        _raising(BootstrapMissingError("engine missing", update_status={"state": "bootstrap_missing"})),
    )

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error:" not in res.output
    assert "daemon failure" not in res.output


# --- call_daemon classifies `ok: false` as a rejection --------------------------------------


def test_call_daemon_raises_request_error_on_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "Build has no synced loadouts available."})

    real_client = httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(daemon, "read_daemon_state", lambda *a, **k: {"port": 1, "token": "t"})
    monkeypatch.setattr(daemon.httpx, "Client", fake_client)

    with pytest.raises(DaemonRequestError, match="no synced loadouts"):
        daemon.call_daemon("switch_main", {"loadout": "x"}, auto_start=False, runtime_dir=tmp_path)

    assert issubclass(DaemonRequestError, RuntimeError)  # `pob update` / `pob stop` still catch it
