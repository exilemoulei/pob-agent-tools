"""Daemon-side errors must surface as clean `Error: ...` messages, never Python tracebacks."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli
from pob_cli.cli import main
from pob_cli.daemon import BootstrapMissingError


def _raise_daemon_error(message: str):
    def _fake(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        raise RuntimeError(message)

    return _fake


def test_switch_main_loadout_without_synced_loadouts_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raise_daemon_error("Build has no synced loadouts available."))

    res = CliRunner().invoke(main, ["switch-main", "--target", "ref", "--loadout", "Late Upgrades"])

    assert res.exit_code == 1
    assert "Error: Build has no synced loadouts available." in res.output
    assert "Traceback" not in res.output
    assert not isinstance(res.exception, RuntimeError)


@pytest.mark.parametrize(
    "argv",
    [
        ["summary"],
        ["skills"],
        ["calc", "Ignite"],
        ["switch-main", "--spec", "missing"],
    ],
)
def test_daemon_errors_are_clean_for_any_command(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raise_daemon_error("No build loaded in slot 'main'"))

    res = CliRunner().invoke(main, argv)

    assert res.exit_code == 1, res.output
    assert "Error: No build loaded in slot 'main'" in res.output
    assert "Traceback" not in res.output


def test_empty_daemon_error_message_names_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "call_daemon", _raise_daemon_error(""))

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error: Daemon error during 'summary'" in res.output


def test_bootstrap_missing_still_prints_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        raise BootstrapMissingError("engine missing", update_status={"state": "bootstrap_missing"})

    monkeypatch.setattr(cli, "call_daemon", _fake)

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error:" not in res.output
