# SPDX-License-Identifier: GPL-3.0-only
"""Regression tests for live055 3 P0 + 5 P1 + 4 P2 bugfixes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_mutate, cli_rune
from pob2_cli.analyzer.summary import inspect_tree
from pob2_cli.cli_errors import _emit_error_and_resolve_code
from pob2_cli.daemon.client import _rpc_result
from pob2_cli.errors import DaemonRequestError, InternalBugError
from pob2_cli.game_data import ground_query
from pob2_cli.importers.share_urls import resolve_poe2_build_source
from pob2_cli.mutators.what_if import _apply_mutation_ops
from pob2_cli.snapshot.commands import undo_build
from pob2_cli.snapshot.helpers import clean_points_dict
from pob2_cli.snapshot.history import history_of


class _FakeWorker:
    """Minimal in-memory BridgeWorker stub for unit testing mutators & analyzers."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}
        self.pob_version = "0.23.1"
        import threading

        self.lock = threading.RLock()

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        payload = dict(params or {})
        self.calls.append((method, payload))
        val = self.responses.get(method, {"ok": True})
        return val(payload) if callable(val) else dict(val)


# --- P0-1: what-if config --config-key uses GuardrailScope.CONFIG_VAR ---


def test_p0_1_what_if_config_key_uses_config_var() -> None:
    worker = _FakeWorker()
    ops = _apply_mutation_ops(worker, {"config_key": "enemyLevel", "config_value": 82})
    assert ops == [{"op": "set_config", "var": "enemyLevel", "value": 82}]
    assert worker.calls == [("set_config", {"var": "enemyLevel", "value": 82})]


# --- P0-2: what-if skill --gem-index sends `index` (not `gem`) and validates 1-based ---


def test_p0_2_what_if_skill_gem_index_sends_index_key() -> None:
    worker = _FakeWorker()
    ops = _apply_mutation_ops(
        worker,
        {"gem_group": 12, "gem_index": 1, "gem_level": 19, "gem_quality": 20, "gem_enabled": False},
    )
    assert worker.calls == [
        ("set_gem", {"group": 12, "index": 1, "level": 19, "quality": 20, "enabled": False})
    ]
    assert ops[0]["op"] == "set_gem" and ops[0]["index"] == 1
    assert "gem" not in ops[0]

    with pytest.raises(DaemonRequestError, match="1-based"):
        _apply_mutation_ops(worker, {"gem_group": 12, "gem_index": 0, "gem_level": 19})


# --- P0-3: what-if skill --set1/--set2 rejects silent noOp and propagates real changes ---


def test_p0_3_what_if_skill_weapon_set_noop_rejected_and_real_change_ok() -> None:
    noop_worker = _FakeWorker(
        {
            "set_skill_weapon_set": {
                "ok": True,
                "index": 12,
                "set1": False,
                "set2": True,
                "gem1EnableGlobal1": False,
                "gem1EnableGlobal2": True,
                "changed": False,
                "noOp": True,
            }
        }
    )
    with pytest.raises(DaemonRequestError, match="POE2-NO-OP"):
        _apply_mutation_ops(noop_worker, {"gem_group": 12, "set1": False})

    ok_worker = _FakeWorker(
        {
            "set_skill_weapon_set": {
                "ok": True,
                "index": 12,
                "set1": False,
                "set2": True,
                "gem1EnableGlobal1": False,
                "gem1EnableGlobal2": True,
                "changed": True,
                "noOp": False,
            }
        }
    )
    ops = _apply_mutation_ops(ok_worker, {"gem_group": 12, "gem_index": 2, "set1": False})
    assert ok_worker.calls == [("set_skill_weapon_set", {"index": 12, "gemIndex": 2, "set1": False})]
    assert ops[0]["changed"] is True
    assert ops[0]["gem1EnableGlobal1"] is False


# --- P1-4: rune rank --type auto-maps unique slots, rejects ambiguous without --slot, echoes slot/itemType ---


def test_p1_4_rune_rank_slot_mapping_and_empty_frontier_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(tmp_path / "no_daemon"))
    runner = CliRunner()

    # Unique armour type auto-maps to Body Armour and echoes slot & itemType in JSON
    r_body = runner.invoke(
        cli_rune.rune_group,
        ["rank", "--type", "Body Armour", "--metric", "TotalEHP", "--json"],
    )
    assert r_body.exit_code == 0, r_body.output
    data = json.loads(r_body.output)
    assert data["slot"] == "Body Armour"
    assert data["itemType"] == "Body Armour"

    # Ambiguous type without --slot fails fast with structured usage_error (rc=1)
    r_ring = runner.invoke(cli_rune.rune_group, ["rank", "--type", "Ring", "--json"])
    assert r_ring.exit_code == 1
    err = json.loads(r_ring.output)
    assert err["errorKind"] == "usage_error"
    assert err["code"] == "POE2-USAGE-ERROR"

    # Empty Pareto frontier prints explicit summary line in Markdown
    md = cli_rune._format_pareto_human(
        {
            "patch": "0.5.5",
            "kind": "rune",
            "evalMode": "live_cow",
            "slot": "Body Armour",
            "itemType": "Body Armour",
            "metric": "TotalEHP",
            "evaluatedCount": 5,
            "bisAnchor": None,
            "budgetChampion": None,
            "paretoFrontier": [],
            "topCandidates": [{"name": "Iron Rune", "gain": -10.0, "costEx": 0.5}],
            "warnings": [],
        }
    )
    assert "slot=Body Armour" in md
    assert "[Summary]" in md
    assert "Iron Rune" in md


# --- P1-5: pob2 ground Chinese recall + fuzzy noise rejection ---


def test_p1_5_ground_zh_recall_and_noise_rejection() -> None:
    res_ws = ground_query("单武器双天赋")
    assert any(c.slug == "spirit_reservation_and_weapon_sets" for c in res_ws.cards)

    res_armour = ground_query("护甲")
    assert any(c.slug == "armour_formula_and_before_resistances" for c in res_armour.cards)

    # Unrelated / fuzzy noise queries must return 0 hits
    for noisy in ("Kaom's", "War Cry", "Break Frost", "战吼"):
        res = ground_query(noisy)
        assert len(res.cards) == 0, f"Expected 0 hits for {noisy!r}, got {[c.slug for c in res.cards]}"


# --- P1-6: what-if equip --unequip flag & backwards-compatible slot arg without swallowing --json ---


def test_p1_6_what_if_equip_unequip_does_not_swallow_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def _fake_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
        captured.append({"action": action, **params})
        return {"ok": True, "game": "poe2", "applied": False, "detail": [], "diff": {"deltas": []}}

    monkeypatch.setattr(cli_mutate, "daemon_call_cli", _fake_call)
    runner = CliRunner()

    # Flag form: --slot "Body Armour" --unequip --json
    r1 = runner.invoke(cli.main, ["what-if", "equip", "--slot", "Body Armour", "--unequip", "--json"])
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.output)["ok"] is True
    assert captured[-1]["unequip_slot"] == "Body Armour"
    assert "slot" not in captured[-1]

    # Backwards-compatible value form: --unequip "Body Armour" --json
    r2 = runner.invoke(cli.main, ["what-if", "equip", "--unequip", "Body Armour", "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["ok"] is True
    assert captured[-1]["unequip_slot"] == "Body Armour"

    # Bare --unequip without --slot fails cleanly as JSON usage_error (not "Invalid slot '--json'")
    r3 = runner.invoke(cli.main, ["what-if", "equip", "--unequip", "--json"])
    assert r3.exit_code == 1
    err = json.loads(r3.output)
    assert err["errorKind"] == "usage_error"
    assert "--json" not in err["error"]


# --- P1-7: what-if apply --spec supports natural nested schema + flat schema ---


def test_p1_7_what_if_apply_nested_and_flat_spec() -> None:
    nested = cli_mutate._load_apply_spec(
        '{"config":{"level":95,"enemyLevel":82,"conditionEnemyFrozen":"true"},'
        '"tree":{"tree_node":331,"alloc_mode":1},'
        '"skill":{"group":12,"index":1,"level":19,"set1":false},'
        '"equip":{"slot":"Body Armour","unequip":true}}',
        None,
    )
    assert nested["level"] == 95
    assert nested["config_vars"] == {"enemyLevel": 82, "conditionEnemyFrozen": True}
    assert nested["tree_node"] == 331 and nested["alloc_mode"] == 1
    assert nested["gem_group"] == 12 and nested["gem_index"] == 1 and nested["gem_level"] == 19
    assert nested["set1"] is False
    assert nested["unequip_slot"] == "Body Armour"
    assert "slot" not in nested

    # Multiple config_vars execute cleanly in _apply_mutation_ops
    worker = _FakeWorker()
    ops = _apply_mutation_ops(worker, {"level": 95, "config_vars": {"enemyLevel": 82}})
    assert any(op["op"] == "set_config" and op["var"] == "enemyLevel" and op["value"] == 82 for op in ops)

    # Flat spec still works
    flat = cli_mutate._load_apply_spec('{"level":95}', None)
    assert flat == {"level": 95}


# --- P1-8: craft inspect --base ... --ilvl standalone ---


def test_p1_8_craft_inspect_base_only_standalone() -> None:
    runner = CliRunner()
    r = runner.invoke(cli.main, ["craft", "inspect", "--base", "Desolate Crossbow", "--ilvl", "77", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True
    assert data["mode"] == "base_pool"
    assert data["baseType"] == "Desolate Crossbow"
    assert data["ilvl"] == 77
    assert data["totalPoolWeight"] > 0
    assert data["familyCount"] > 0
    assert isinstance(data["outcomes"], list) and len(data["outcomes"]) > 0


# --- P2-9 & P2-10: error envelope, exit codes, and zero stderr on --json ---


def test_p2_9_and_10_error_envelopes_and_zero_stderr() -> None:
    # Internal Python bugs must map to rc=1 internal_error, NOT rc=5 daemon_error
    assert _emit_error_and_resolve_code(None, AttributeError("CONFIG_KEY")) == 1
    assert _emit_error_and_resolve_code(None, KeyError("missing")) == 1
    with pytest.raises(InternalBugError):
        _rpc_result({"ok": False, "errorKind": "internal_error", "error": "CONFIG_KEY"})

    # Empty undo history raises [POE2-EMPTY-HISTORY]
    worker = _FakeWorker()
    history_of(worker).load_build("dummy", "test")
    with pytest.raises(DaemonRequestError, match="POE2-EMPTY-HISTORY"):
        undo_build(worker)

    # craft --target migration error under --json must include non-empty code AND empty stderr
    runner = CliRunner()
    r = runner.invoke(cli.main, ["craft", "odds", "--base", "Desolate Crossbow", "--target", "main", "--json"])
    assert r.exit_code == 1
    assert r.stderr == ""
    err = json.loads(r.stdout)
    assert err["errorKind"] == "cli_migration"
    assert err["code"] == "POE2-CLI-MIGRATION"


# --- P2-14: poe.ninja 403 / Cloudflare HTML returns POE2-FETCH-BLOCKED ---


def test_p2_14_poe_ninja_403_and_html_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pob2_cli.importers.share_urls.check_public_https_url", lambda _u: None)
    monkeypatch.setattr("pob2_cli.importers.url_guard.check_public_https_url", lambda _u: None)

    transport_403 = httpx.MockTransport(
        lambda req: httpx.Response(403, text="<html><title>Just a moment...</title></html>", request=req)
    )
    with pytest.raises(DaemonRequestError, match="POE2-FETCH-BLOCKED"):
        resolve_poe2_build_source("https://poe.ninja/poe2/pob/abc123", transport=transport_403)

    transport_html200 = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<!DOCTYPE html><html><title>Just a moment...</title></html>", request=req)
    )
    with pytest.raises(DaemonRequestError, match="POE2-FETCH-BLOCKED"):
        resolve_poe2_build_source("https://poe.ninja/poe2/pob/abc123", transport=transport_html200)


# --- P2-15: JSON strips PoB color codes from points.display ---


def test_p2_15_strip_pob_color_codes_from_points_display() -> None:
    cleaned = clean_points_dict({"used": 117, "usedMax": 123, "display": "^7117 / 123 ^xDD002224 / 24"})
    assert cleaned["display"] == "117 / 123 24 / 24"
    assert "^" not in cleaned["display"]

    worker = _FakeWorker(
        {
            "get_tree_state": {"allocatedNodes": [1, 2], "overrides": {}},
            "get_weapon_set_state": {"ok": True},
            "get_build_info": {
                "classId": 9,
                "className": "Mercenary",
                "ascendClassName": "Gemling Legionnaire",
                "treeVersion": "0_5",
                "points": {"used": 117, "usedMax": 123, "display": "^7117 / 123 ^xDD002224 / 24"},
            },
        }
    )
    tree_out = inspect_tree(worker)
    assert tree_out["points"]["display"] == "117 / 123 24 / 24"


# --- SKILL.md budget check ---


def test_skill_md_budget() -> None:
    skill_path = Path(__file__).resolve().parents[2] / "skills" / "pob2" / "SKILL.md"
    raw = skill_path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert len(lines) <= 90, f"SKILL.md has {len(lines)} lines (>90)"
    assert len(raw) <= 10240, f"SKILL.md is {len(raw)} bytes (>10240)"
    assert all(len(ln) <= 500 for ln in lines), "SKILL.md has a line > 500 chars"
