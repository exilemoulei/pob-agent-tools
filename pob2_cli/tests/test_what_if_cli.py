# SPDX-License-Identifier: GPL-3.0-only
"""Domain tests for the orthogonal `pob2 what-if <equip|tree|skill|config|apply>` CLI group."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_mutate


def _mock_what_if(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        payload = dict(params or {})
        calls.append((action, payload))
        return {
            "ok": True,
            "game": "poe2",
            "applied": bool(payload.get("apply")),
            "before": {"primaryDps": 10000.0, "totalEhp": 20000.0, "life": 2000.0},
            "after": {"primaryDps": 12500.0, "totalEhp": 21000.0, "life": 2100.0},
            "delta": {"primaryDps": 2500.0, "primaryDpsPct": 25.0, "totalEhp": 1000.0, "totalEhpPct": 5.0},
            "detail": [],
        }

    monkeypatch.setattr(cli_mutate, "daemon_call_cli", _fake_daemon)
    return calls


def test_what_if_group_exposes_orthogonal_subcommands() -> None:
    runner = CliRunner()
    res = runner.invoke(cli.main, ["what-if", "--help"])
    assert res.exit_code == 0, res.output
    for sub in ("equip", "tree", "skill", "config", "apply"):
        assert sub in res.output


def test_what_if_equip_subcommand_and_slot_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    item_file = tmp_path / "ring.txt"
    item_file.write_text("Rarity: RARE\nTest Ring\nRuby Ring\n+30 to maximum Life\n", encoding="utf-8")

    r_ok = runner.invoke(
        cli.main,
        ["what-if", "equip", "--slot", "Ring 1", "--item-file", str(item_file), "--json"],
    )
    assert r_ok.exit_code == 0, r_ok.output
    assert r_ok.stderr == ""
    data = json.loads(r_ok.stdout)
    assert data["ok"] is True and data["applied"] is False
    assert calls[-1][0] == "what_if"
    assert calls[-1][1]["slot"] == "Ring 1"
    assert "Ruby Ring" in calls[-1][1]["item_text"]

    r_swap = runner.invoke(cli.main, ["what-if", "equip", "--weapon-swap", "true", "--json"])
    assert r_swap.exit_code == 0, r_swap.output
    assert calls[-1][1]["weapon_swap"] is True

    r_bad_slot = runner.invoke(cli.main, ["what-if", "equip", "--slot", "NotASlot", "--json"])
    assert r_bad_slot.exit_code == 1
    assert r_bad_slot.stderr == ""
    err = json.loads(r_bad_slot.stdout)
    assert err["ok"] is False

    r_empty = runner.invoke(cli.main, ["what-if", "equip", "--json"])
    assert r_empty.exit_code == 1
    assert r_empty.stderr == ""
    assert json.loads(r_empty.stdout)["ok"] is False


def test_what_if_tree_skill_config_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    r_tree = runner.invoke(
        cli.main,
        ["what-if", "tree", "--tree-node", "12345", "--isolated-node", "--alloc-mode", "1", "--json"],
    )
    assert r_tree.exit_code == 0, r_tree.output
    assert calls[-1][1]["tree_node"] == 12345
    assert calls[-1][1]["anoint_preview"] is True
    assert calls[-1][1]["alloc_mode"] == 1

    r_skill = runner.invoke(
        cli.main,
        ["what-if", "skill", "--gem-group", "1", "--gem-index", "2", "--gem-level", "20", "--set1", "true", "--json"],
    )
    assert r_skill.exit_code == 0, r_skill.output
    assert calls[-1][1]["gem_group"] == 1
    assert calls[-1][1]["gem_index"] == 2
    assert calls[-1][1]["gem_level"] == 20
    assert calls[-1][1]["set1"] is True

    r_cfg = runner.invoke(
        cli.main,
        ["what-if", "config", "--level", "95", "--config-key", "enemyIsBoss", "--config-value", "Pinnacle", "--json"],
    )
    assert r_cfg.exit_code == 0, r_cfg.output
    assert calls[-1][1]["level"] == 95
    assert calls[-1][1]["config_key"] == "enemyIsBoss"
    assert calls[-1][1]["config_value"] == "Pinnacle"


def test_what_if_apply_multi_domain_spec_and_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_what_if(monkeypatch)
    runner = CliRunner()

    spec = {
        "slot": "Amulet",
        "itemText": "Rarity: RARE\nSolar Amulet\n+40 to Spirit",
        "treeNode": 45678,
        "configKey": "conditionFullLife",
        "configValue": "true",
        "level": 92,
    }
    r_inline = runner.invoke(cli.main, ["what-if", "apply", "--spec", json.dumps(spec), "--json"])
    assert r_inline.exit_code == 0, r_inline.output
    assert r_inline.stderr == ""
    sent = calls[-1][1]
    assert sent["slot"] == "Amulet"
    assert sent["tree_node"] == 45678
    assert sent["config_key"] == "conditionFullLife"
    assert sent["config_value"] is True
    assert sent["level"] == 92
    assert sent["apply"] is False

    spec_file = tmp_path / "combo.json"
    spec_file.write_text(json.dumps({"level": 93, "weaponSwap": True}), encoding="utf-8")
    r_file = runner.invoke(cli.main, ["what-if", "apply", "--spec-file", str(spec_file), "--commit", "--json"])
    assert r_file.exit_code == 0, r_file.output
    assert calls[-1][1]["apply"] is True
    assert calls[-1][1]["weapon_swap"] is True

    r_bad_key = runner.invoke(cli.main, ["what-if", "apply", "--spec", '{"bogusKey": 1}', "--json"])
    assert r_bad_key.exit_code == 1
    assert r_bad_key.stderr == ""
    err = json.loads(r_bad_key.stdout)
    assert err["ok"] is False and err["errorKind"] == "invalid_spec"

    r_bad_json = runner.invoke(cli.main, ["what-if", "apply", "--spec", "{not-json", "--json"])
    assert r_bad_json.exit_code == 1
    assert r_bad_json.stderr == ""
    assert json.loads(r_bad_json.stdout)["ok"] is False
