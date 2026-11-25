"""`pob matrix` takes `--no-auto-keep` like `audit`, `compare` and `diagnose`, so their Pinnacle numbers can agree."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli

MATRIX_RESULT = {"scenarios": [], "rows": [], "restoreCheck": {"verified": True}}


def test_matrix_passes_no_auto_keep_to_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent.append((action, dict(params)))
        return dict(MATRIX_RESULT)

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    plain = runner.invoke(cli.main, ["matrix", "Pinnacle", "--json"])
    assert plain.exit_code == 0, plain.output
    assert sent[-1] == ("matrix", {"target": "main", "scenarios": ["Pinnacle"], "full": False})

    bare = runner.invoke(cli.main, ["matrix", "Pinnacle", "--keep", "Flask 1", "--no-auto-keep", "--json"])
    assert bare.exit_code == 0, bare.output
    assert sent[-1] == (
        "matrix",
        {"target": "main", "scenarios": ["Pinnacle"], "full": False, "keep": ["Flask 1"], "no_auto_keep": True},
    )
