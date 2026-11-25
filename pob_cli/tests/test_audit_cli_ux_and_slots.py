"""Regression tests for Layer 3 CLI UX & dynamic jewel slots:
- `pob tree change` repeatable `--allocate` / `--deallocate`, noop warnings, and unreachable allocate errors.
- `pob calc` `--group` and `--skill` selectors with read-only `calcsTab` restoration.
- `pob trade search --slot` dynamic jewel (`Jewel <id>`, `#<n>`) and Abyssal Socket resolution.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli.analyzer.calcs import search_calcs_breakdown
from pob_cli.cli import main as cli
from pob_cli.cli_trade.common import _is_known_slot, _read_current_item
from pob_cli.cli_trade.search_cmd import _resolve_current_and_category
from pob_cli.daemon.rpc_handlers import _h_calc
from pob_cli.formatters.mutation_md import format_mutation_report
from pob_cli.mutators.tree import _apply_tree_changes_in_place
from pob_cli.trade_query import category_for_slot, pob_slot_for_slot

# ---------------------------------------------------------------------------
# 1. `pob tree change` repeatable flags, noop warnings, unreachable errors
# ---------------------------------------------------------------------------


def test_cli_tree_change_repeatable_and_csv_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_invoke(action: str, params: dict[str, Any]) -> dict[str, Any]:
        captured["action"] = action
        captured["params"] = params
        return {
            "ok": True,
            "mode": "preview",
            "before": {"primaryDps": 1000.0, "totalEhp": 5000.0, "life": 3000, "points": {"used": 10, "usedMax": 123}},
            "after": {"primaryDps": 1000.0, "totalEhp": 5000.0, "life": 3000, "points": {"used": 10, "usedMax": 123}},
            "diff": {"deltas": []},
            "detail": {
                "noopWarnings": [
                    "Node 41415 is not currently allocated (skipped deallocate).",
                    "Node 41416 is not currently allocated (skipped deallocate).",
                ]
            },
        }

    monkeypatch.setattr("pob_cli.cli_mutate.invoke_daemon", _fake_invoke)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "tree",
            "change",
            "--allocate",
            "100, 101",
            "--allocate",
            "102:999",
            "--deallocate",
            "41415",
            "--deallocate",
            "41416, 41417",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["action"] == "tree_change"
    assert captured["params"]["allocate"] == ["100", "101", "102:999"]
    assert captured["params"]["deallocate"] == ["41415", "41416", "41417"]
    assert "> [WARN] Node 41415 is not currently allocated (skipped deallocate)." in res.output
    assert "> [WARN] Node 41416 is not currently allocated (skipped deallocate)." in res.output


def test_tree_noop_warnings_and_format_mutation_report() -> None:
    class _StubWorker:
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "get_tree_state":
                return {"allocatedNodes": [12345], "points": {"used": 1, "usedMax": 123}}
            raise AssertionError(f"Unexpected RPC {method}: {params}")

    noop_warnings: list[str] = []
    actions, _, orphaned, delta, cascade = _apply_tree_changes_in_place(
        _StubWorker(),  # type: ignore[arg-type]
        resolved_alloc=[(12345, None)],
        resolved_dealloc=[(41415, None)],
        noop_warnings_out=noop_warnings,
    )
    assert actions == [
        {"action": "skip_already_allocated", "id": 12345},
        {"action": "skip_already_unallocated", "id": 41415},
    ]
    assert orphaned == []
    assert delta == 0
    assert cascade is None
    assert noop_warnings == [
        "Node 12345 is already allocated (skipped allocate).",
        "Node 41415 is not currently allocated (skipped deallocate).",
    ]

    md = format_mutation_report(
        "Passive Tree Change",
        {
            "ok": True,
            "mode": "preview",
            "before": {"primaryDps": 500.0, "totalEhp": 2000.0, "life": 1500, "points": {"used": 1, "usedMax": 123}},
            "after": {"primaryDps": 500.0, "totalEhp": 2000.0, "life": 1500, "points": {"used": 1, "usedMax": 123}},
            "diff": {"deltas": []},
            "detail": {"noopWarnings": noop_warnings},
        },
    )
    assert "_(No stat changes detected)_" in md
    assert "> [WARN] Node 12345 is already allocated (skipped allocate)." in md
    assert "> [WARN] Node 41415 is not currently allocated (skipped deallocate)." in md


def test_tree_unreachable_allocate_raises_value_error() -> None:
    class _UnreachableWorker:
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method in ("get_tree_state", "tree_click"):
                # Simulate PoB silently ignoring tree_click on a disconnected/anoint-only node.
                return {"allocatedNodes": [54447], "points": {"used": 1, "usedMax": 123}}
            raise AssertionError(f"Unexpected RPC {method}: {params}")

    with pytest.raises(ValueError, match=r"Passive node 99999 could not be allocated"):
        _apply_tree_changes_in_place(
            _UnreachableWorker(),  # type: ignore[arg-type]
            resolved_alloc=[(99999, None)],
            resolved_dealloc=[],
        )


# ---------------------------------------------------------------------------
# 2. `pob calc --group` and `pob calc --skill` selectors + state restoration
# ---------------------------------------------------------------------------


class _FakeCalcsWorker:
    """In-memory BridgeWorker double modeling multi-group & multi-active-skill CalcsTab."""

    def __init__(self, *, initial_group: int = 1, initial_skill: int = 1) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.cur_group = initial_group
        self.cur_skill_by_group = {1: initial_skill, 2: 1}
        self.set_calls: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "label": "Main Fire",
                        "displayLabel": "Main Fire",
                        "mainActiveSkill": 1,
                        "skills": [
                            {"index": 1, "name": "Fireball", "nameZh": "火球"},
                            {"index": 2, "name": "Rolling Magma", "nameZh": "熔岩奔涌"},
                        ],
                        "gems": [
                            {"index": 1, "name": "Fireball", "nameZh": "火球", "support": False, "enabled": True},
                            {"index": 2, "name": "Rolling Magma", "nameZh": "熔岩奔涌", "support": False, "enabled": True},
                        ],
                    },
                    {
                        "index": 2,
                        "isMain": False,
                        "enabled": True,
                        "label": "Utility Movement",
                        "displayLabel": "Utility Movement",
                        "mainActiveSkill": 1,
                        "skills": [{"index": 1, "name": "Frostblink", "nameZh": "冰霜闪现"}],
                        "gems": [
                            {"index": 1, "name": "Frostblink", "nameZh": "冰霜闪现", "support": False, "enabled": True},
                            {"index": 2, "name": "Faster Casting", "support": True, "enabled": True},
                        ],
                    },
                ],
            }
        if method == "set_calcs_input":
            self.set_calls.append(dict(params))
            var = params.get("var")
            val = int(params["value"])
            if var == "skill_number":
                self.cur_group = val
            elif var == "mainActiveSkill":
                self.cur_skill_by_group[self.cur_group] = val
            else:
                raise AssertionError(f"Unknown calcs input var: {var}")
            return {"ok": True}
        if method == "get_build_header":
            return {
                "mainSocketGroup": {"index": 1, "list": [{"label": "Main Fire"}, {"label": "Utility Movement"}]},
                "mainSkill": {"index": 1, "list": [{"label": "Fireball"}, {"label": "Rolling Magma"}]},
            }
        if method == "get_calcs":
            g = self.cur_group
            s = self.cur_skill_by_group[g]
            dmg_map = {(1, 1): "12500", (1, 2): "8400", (2, 1): "1900"}
            skill_lists = {
                1: [{"label": "Fireball", "val": 1}, {"label": "Rolling Magma", "val": 2}],
                2: [{"label": "Frostblink", "val": 1}],
            }
            return {
                "selectors": {
                    "mainSocketGroup": {
                        "index": g,
                        "list": [{"label": "Main Fire", "val": 1}, {"label": "Utility Movement", "val": 2}],
                    },
                    "mainSkill": {"index": s, "list": skill_lists[g]},
                },
                "sections": [
                    {
                        "si": 1,
                        "id": "Offence",
                        "group": "Offence",
                        "subsections": [
                            {
                                "ui": 1,
                                "label": "Skill Hit Damage",
                                "rows": [
                                    {
                                        "ri": 1,
                                        "label": "Average Damage",
                                        "cells": [{"ci": 1, "raw": dmg_map[(g, s)], "text": dmg_map[(g, s)], "hasBreakdown": False}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        raise AssertionError(f"Unexpected RPC {method}: {params}")


def test_search_calcs_breakdown_group_and_skill_restores_state() -> None:
    # Start with CalcsTab pointing at group 2 (Frostblink), while main skill is group 1 (Fireball).
    worker = _FakeCalcsWorker(initial_group=2, initial_skill=1)

    # `--skill 1` without `--group` resolves socket group #1 (Fireball = 12500) and restores group 2!
    res_s1 = search_calcs_breakdown(worker, "Average Damage", skill="1", no_breakdown=True)  # type: ignore[arg-type]
    assert res_s1["matches"][0]["cells"][0]["value"] == 12500.0
    assert res_s1["calcsSkill"]["socketGroup"] == 1
    assert worker.cur_group == 2
    assert worker.cur_skill_by_group[2] == 1

    # `--skill "Rolling Magma"` selects group #1 AND active skill #2 (8400), then restores group 2 & group 1!
    res_rm = search_calcs_breakdown(worker, "Average Damage", skill="Rolling Magma", no_breakdown=True)  # type: ignore[arg-type]
    assert res_rm["matches"][0]["cells"][0]["value"] == 8400.0
    assert res_rm["calcsSkill"]["socketGroup"] == 1
    assert res_rm["calcsSkill"]["skillLabel"] == "Rolling Magma"
    assert worker.cur_group == 2
    assert worker.cur_skill_by_group == {1: 1, 2: 1}

    # `_h_calc` forwards `group` and `skill` cleanly.
    res_rpc = _h_calc(
        None,  # type: ignore[arg-type]
        {"keyword": "Average Damage", "group": "Main Fire", "skill": 2, "no_breakdown": True},
        worker,  # type: ignore[arg-type]
    )
    assert res_rpc["matches"][0]["cells"][0]["value"] == 8400.0
    assert worker.cur_group == 2

    with pytest.raises(ValueError, match="not found"):
        search_calcs_breakdown(worker, "Average Damage", skill="NonexistentSkill", no_breakdown=True)  # type: ignore[arg-type]


def test_cli_calc_group_skill_suppresses_non_main_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_calls: list[dict[str, Any]] = []

    def _fake_invoke(action: str, params: dict[str, Any]) -> dict[str, Any]:
        assert action == "calc"
        captured_calls.append(params)
        return {
            "keyword": "Average Damage",
            "totalMatches": 1,
            "calcsSkill": {
                "socketGroup": 2,
                "label": "Utility Movement",
                "skillLabel": "Frostblink",
                "mainSocketGroup": 1,
                "mainLabel": "Main Fire",
                "mainSkillLabel": "Fireball",
                "isMainSkill": False,
            },
            "matches": [
                {
                    "sectionGroup": "Offence",
                    "subsection": "Skill Hit Damage",
                    "rowLabel": "Average Damage",
                    "cells": [{"ci": 1, "text": "1900", "raw": "1900", "value": 1900.0}],
                }
            ],
        }

    monkeypatch.setattr("pob_cli.cli_inspect_parts.invoke_daemon", _fake_invoke)
    runner = CliRunner()

    # Without --group/--skill, the non-main CalcsTab warning IS shown.
    r_default = runner.invoke(cli, ["calc", "Average Damage"])
    assert r_default.exit_code == 0, r_default.output
    assert "[WARN] These rows are for #2 Utility Movement / Frostblink" in r_default.output

    # With explicit --skill or --group, params are forwarded and the non-main [WARN] is suppressed.
    r_explicit = runner.invoke(cli, ["calc", "--group", "2", "--skill", "Frostblink", "Average Damage"])
    assert r_explicit.exit_code == 0, r_explicit.output
    assert captured_calls[-1]["group"] == "2"
    assert captured_calls[-1]["skill"] == "Frostblink"
    assert "- Calcs tab skill: #2 Utility Movement / Frostblink" in r_explicit.output
    assert "[WARN] These rows are for" not in r_explicit.output


# ---------------------------------------------------------------------------
# 3. Dynamic jewel & Abyssal Socket names in `pob trade search --slot`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_slot", "exp_cat", "exp_pob_slot"),
    [
        ("Jewel 31683", "jewel", "Jewel 31683"),
        ("Jewel 1", "jewel", "Jewel 1"),
        ("Jewel 31683 (#8)", "jewel", "Jewel 31683"),
        ("#8", "jewel", "#8"),
        ("Abyssal Socket 1", "jewel.abyss", "Abyssal Socket 1"),
        ("Helmet Abyssal Socket 1", "jewel.abyss", "Helmet Abyssal Socket 1"),
        ("Body Armour Abyssal Socket 2", "jewel.abyss", "Body Armour Abyssal Socket 2"),
        ("helm_abyssal_socket_1", "jewel.abyss", "Helmet Abyssal Socket 1"),
    ],
)
def test_dynamic_jewel_slot_aliases(raw_slot: str, exp_cat: str, exp_pob_slot: str) -> None:
    assert _is_known_slot(raw_slot) is True
    assert category_for_slot(raw_slot) == exp_cat
    assert pob_slot_for_slot(raw_slot) == exp_pob_slot


def test_invalid_dynamic_abyss_slot_rejected() -> None:
    assert pob_slot_for_slot("Banana Abyssal Socket 1") is None
    assert _is_known_slot("Banana Abyssal Socket 1") is False


@pytest.mark.parametrize(
    ("slot_arg", "exp_category"),
    [
        ("Jewel 31683", "jewel"),
        ("#8", "jewel"),
        ("Helmet Abyssal Socket 1", "jewel.abyss"),
    ],
)
def test_cli_trade_search_dump_query_dynamic_jewel_slots(
    monkeypatch: pytest.MonkeyPatch,
    slot_arg: str,
    exp_category: str,
) -> None:
    fake_stats = [
        {
            "id": "explicit.stat_avoid_shock",
            "text": "#% chance to Avoid being Shocked",
            "type": "explicit",
        }
    ]
    monkeypatch.setattr("pob_cli.cli_trade.load_trade_stats", lambda **_kw: (fake_stats, "local_mock"))
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "trade",
            "search",
            "--slot",
            slot_arg,
            "--stat",
            "#% chance to Avoid being Shocked>=48",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    cat_opt = payload["query"]["query"]["filters"]["type_filters"]["filters"]["category"]["option"]
    assert cat_opt == exp_category
    stat_filters = payload["query"]["query"]["stats"][0]["filters"]
    assert any(f["id"] == "explicit.stat_avoid_shock" and f["value"]["min"] == 48 for f in stat_filters)


def test_resolve_current_and_category_cobalt_vs_cluster_jewel(monkeypatch: pytest.MonkeyPatch) -> None:
    cobalt_raw = (
        "Rarity: RARE\nDragon Eye\nCobalt Jewel\n--------\n"
        "7% increased maximum Life\n48% chance to Avoid being Shocked"
    )
    cluster_raw = (
        "Rarity: RARE\nArmageddon Bliss\nLarge Cluster Jewel\n--------\n"
        "Adds 8 Passive Skills\n1 Added Passive Skill is Prismatic Heart"
    )

    def _mock_daemon_cobalt(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert action == "items"
        return {
            "equippedSlots": [
                {"slotName": "Jewel 31683", "socketIndex": 8, "raw": cobalt_raw},
            ]
        }

    monkeypatch.setattr("pob_cli.cli_trade._daemon_call_safe", _mock_daemon_cobalt)
    cur_item, cur_err, _, _, cat = _resolve_current_and_category(
        dump_query=False,
        current_item_file=None,
        no_compare=False,
        slot="#8",
        pob_slot=pob_slot_for_slot("#8"),
        target="main",
        no_category=False,
        category=None,
    )
    assert cur_err is None
    assert cur_item is not None and cur_item.base_type == "Cobalt Jewel"
    assert cat == "jewel"

    def _mock_daemon_cluster(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert action == "items"
        return {"slot": {"name": "Jewel 31683"}, "raw": cluster_raw}

    monkeypatch.setattr("pob_cli.cli_trade._daemon_call_safe", _mock_daemon_cluster)
    cur_cl, err_cl, _, _, cat_cl = _resolve_current_and_category(
        dump_query=False,
        current_item_file=None,
        no_compare=False,
        slot="Jewel 31683",
        pob_slot=pob_slot_for_slot("Jewel 31683"),
        target="main",
        no_category=False,
        category=None,
    )
    assert err_cl is None
    assert cur_cl is not None and cur_cl.base_type == "Large Cluster Jewel"
    assert cat_cl == "jewel.cluster"

    # Verify `_read_current_item` also works directly with `Jewel 31683`.
    item_direct, err_direct, _, _ = _read_current_item("Jewel 31683", "main")
    assert err_direct is None
    assert item_direct is not None and item_direct.base_type == "Large Cluster Jewel"


# ---------------------------------------------------------------------------
# 4. Real Headless PoB Engine Integration (when runtime is available)
# ---------------------------------------------------------------------------


def test_real_engine_tree_change_and_calc_selectors() -> None:
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.mutators.tree import change_tree
    from pob_cli.snapshot import load_build
    from pob_cli.updater import resolve_engine_root

    try:
        pob_dir = resolve_engine_root()
    except Exception:
        pytest.skip("PoB engine runtime not configured")
    if not (pob_dir / "PoE1" / "Data").is_dir() and not (pob_dir / "Data").is_dir():
        pytest.skip("PoB engine data directory not present")

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" bandit="None" className="Witch" ascendClassName="Necromancer" mainSocketGroup="1"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_28" classId="3" ascendClassId="3" nodes="54447"/></Tree>
  <Skills activeSkillSet="1" defaultGemLevel="20" defaultGemQuality="0">
    <SkillSet id="1">
      <Skill enabled="true" label="Main Fire" mainActiveSkill="1" mainActiveSkillCalcs="1">
        <Gem nameSpec="Fireball" skillId="Fireball" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Rolling Magma" skillId="MagmaOrb" level="20" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" label="Utility Movement" mainActiveSkill="1" mainActiveSkillCalcs="1">
        <Gem nameSpec="Frostblink" skillId="Frostblink" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
</PathOfBuilding>"""

    w = BridgeWorker(pob_dir)
    w.start()
    try:
        load_build(w, xml_code=xml)

        # Idempotent deallocate of an unallocated node returns preview mode + noopWarnings.
        noop_res = change_tree(w, deallocate=["36634"], apply=False)
        assert "WHAT-IF" in noop_res["mode"]
        assert any("36634" in msg for msg in noop_res["detail"]["noopWarnings"])

        # Disconnected anoint-only (blighted) node raises ValueError.
        ts = w.call("get_tree_state", {})
        nodes_db = w.get_cached_tree_data(ts.get("treeVersion")).get("nodes", {})
        unreachable_nid = next(
            (int(nid) for nid, meta in nodes_db.items() if meta.get("blighted") and not meta.get("linked")),
            None,
        )
        assert unreachable_nid is not None
        with pytest.raises(ValueError, match="could not be allocated"):
            change_tree(w, allocate=[str(unreachable_nid)], apply=False)

        # CalcsTab selector switching & read-only state restoration.
        c_before = w.call("get_calcs", {})
        orig_grp = c_before["selectors"]["mainSocketGroup"]["index"]
        orig_sk = c_before["selectors"]["mainSkill"]["index"]

        res_g2 = search_calcs_breakdown(w, "Cast Rate", group=2, no_breakdown=True)
        assert res_g2["calcsSkill"]["socketGroup"] == 2

        res_rm = search_calcs_breakdown(w, "Average Damage", skill="Rolling Magma", no_breakdown=True)
        assert res_rm["calcsSkill"]["socketGroup"] == 1
        assert "Rolling Magma" in str(res_rm["calcsSkill"].get("skillLabel") or "") or res_rm["totalMatches"] >= 1

        c_after = w.call("get_calcs", {})
        assert c_after["selectors"]["mainSocketGroup"]["index"] == orig_grp
        assert c_after["selectors"]["mainSkill"]["index"] == orig_sk
    finally:
        w.stop()

