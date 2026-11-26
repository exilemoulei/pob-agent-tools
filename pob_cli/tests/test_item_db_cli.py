"""CLI, Markdown formatter, lossless JSON, and real-engine tests for `pob item db`."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli import cli as cli_mod
from pob_cli.bridge_client import BridgeWorker
from pob_cli.formatters.item_db_md import format_item_db_markdown
from pob_cli.mutators import ITEM_DB_TEMPLATE_NOTE, list_item_db_sorts, search_and_try_item_db

_SAMPLE_STAT_SORT_RES: dict[str, Any] = {
    "query": "",
    "dbType": "UNIQUE",
    "total": 114,
    "page": 1,
    "sortMode": "FullDPS",
    "sortLabel": "Sort by Full DPS",
    "statSort": True,
    "baselineValue": 57_390_000.0,
    "filters": {
        "slot": "Helmet",
        "itemType": "Any type",
        "league": "Any league",
        "requirement": "Any requirements",
        "obtainable": "Obtainable",
        "searchMode": "Anywhere",
    },
    "templateNote": ITEM_DB_TEMPLATE_NOTE,
    "matches": [
        {
            "index": 1,
            "title": "Crown of Eyes",
            "titleZh": "邪眼之冠",
            "baseName": "Hubris Circlet",
            "baseNameZh": "灵主之环",
            "primarySlot": "Helmet",
            "measuredPower": 70_820_000.0,
            "delta": 13_430_000.0,
            "deltaPct": 23.401289,
        },
        {
            "index": 2,
            "title": "Goldrim",
            "titleZh": "金缕帽",
            "baseName": "Leather Cap",
            "baseNameZh": "皮帽",
            "primarySlot": "Helmet",
            "measuredPower": 57_390_000.0,
            "delta": 0.0,
            "deltaPct": 0.0,
        },
    ],
    "equipped": False,
    "applied": False,
    "selectedItem": None,
    "tooltip": None,
    "mutation": None,
}


def test_cli_item_db_optional_query_and_all_new_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, Any], float | None]] = []

    def _fake_call(action: str, params: dict[str, Any], *, timeout: float | None = None, **_kw: Any) -> dict[str, Any]:
        captured.append((action, dict(params), timeout))
        if params.get("list_sorts"):
            return {
                "dbType": "UNIQUE",
                "listSorts": True,
                "sorts": [
                    {"sortMode": "Name", "stat": None, "label": "Sort by Name", "labelZh": "按名称排序"},
                    {"sortMode": "FullDPS", "stat": "FullDPS", "label": "Sort by Full DPS", "labelZh": "按完整 DPS"},
                ],
            }
        return dict(_SAMPLE_STAT_SORT_RES)

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    runner = CliRunner()

    r1 = runner.invoke(
        cli_mod.main,
        [
            "item", "db", "--slot", "Helmet", "--sort", "dps", "--item-type", "Armour",
            "--requirement", "useable", "--obtainable", "obtainable", "--search-mode", "names",
            "--league", "Delirium", "--limit", "10", "--page", "2", "--json",
        ],
    )
    assert r1.exit_code == 0, r1.output
    action, params, timeout = captured[-1]
    assert (action, timeout, params["query"], params["slot"], params["sort"], params["page"]) == (
        "item_db", 120.0, "", "Helmet", "dps", 2
    )
    assert (params["item_type"], params["requirement"], params["obtainable"], params["search_mode"], params["league"], params["limit"]) == (
        "Armour", "useable", "obtainable", "names", "Delirium", 10
    )

    payload = json.loads(r1.output)
    assert payload["sortMode"] == "FullDPS" and payload["baselineValue"] == pytest.approx(57_390_000.0)
    assert payload["templateNote"] == ITEM_DB_TEMPLATE_NOTE
    assert payload["matches"][0]["delta"] == pytest.approx(13_430_000.0)

    r2 = runner.invoke(cli_mod.main, ["item", "db", "--list-sorts"])
    assert r2.exit_code == 0 and captured[-1][1]["list_sorts"] is True and "FullDPS" in r2.output

    r3 = runner.invoke(cli_mod.main, ["item", "db", "法师之血", "--json"])
    assert r3.exit_code == 0 and captured[-1][1]["query"] == "法师之血" and captured[-1][1]["sort"] is None

    r4 = runner.invoke(cli_mod.main, ["item", "db"])
    assert r4.exit_code != 0 and "Provide QUERY" in r4.output


def test_cli_item_db_poe2_guardrail_blocks_poe2_slot() -> None:
    res = CliRunner().invoke(cli_mod.main, ["item", "db", "--slot", "charm1", "--sort", "dps"])
    assert res.exit_code == 1


def test_format_item_db_markdown_stat_sort_taken_hit_slot_note_and_build_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_LANG", "zh-CN")
    md = format_item_db_markdown({**_SAMPLE_STAT_SORT_RES, "slotNote": "注：珠宝默认按首个孔位试算"})
    assert "FullDPS" in md and "57.39M" in md and "obtainable=`Obtainable`" in md
    assert "邪眼之冠 (Crown of Eyes)" in md and "70.82M (+13.43M, +23.4%)" in md
    assert "注：珠宝默认按首个孔位试算" in md and ITEM_DB_TEMPLATE_NOTE in md

    # TakenHit column header annotates negated sign semantics; 0-hit hint renders in statSort mode too.
    md_taken = format_item_db_markdown({**_SAMPLE_STAT_SORT_RES, "sortMode": "PhysicalTakenHit"})
    assert "New (-PhysicalTakenHit, higher=less taken) / Delta" in md_taken
    md_empty_stat = format_item_db_markdown(
        {**_SAMPLE_STAT_SORT_RES, "total": 0, "matches": [], "hint": "Retry with `--obtainable any`"}
    )
    assert "--obtainable any" in md_empty_stat

    # Non-finite inf TotalEHP renders cleanly with zero NaN.
    md_inf = format_item_db_markdown(
        {
            **_SAMPLE_STAT_SORT_RES,
            "sortMode": "TotalEHP",
            "baselineValue": "inf",
            "matches": [
                {"index": 1, "title": "Aegis Aurora", "baseName": "Champion Kite Shield", "measuredPower": "inf", "delta": 0.0, "deltaPct": 0.0},
                {"index": 2, "title": "Prism Guardian", "baseName": "Archon Kite Shield", "measuredPower": 85_000.0, "delta": None, "deltaPct": None},
            ],
        }
    )
    assert "inf (0)" in md_inf and "85.00K" in md_inf and "nan" not in md_inf.lower()

    # Equipped mutation propagates buildSlot into recency stamp (`Slot: alt-slot`, not `Slot: main`).
    md_eq = format_item_db_markdown(
        {
            "query": "Nimis",
            "equipped": True,
            "slotName": "Ring 2",
            "buildSlot": "alt-slot",
            "selectedItem": {"title": "Nimis"},
            "tooltip": {"lines": []},
            "mutation": {"mode": "WHAT-IF", "before": {"primaryDps": 100.0}, "after": {"primaryDps": 150.0}},
        }
    )
    assert "Slot: alt-slot" in md_eq


@pytest.mark.skipif(find_runnable_engine() is None, reason="Needs runnable PoB engine (POB_ROOT or pob bootstrap)")
def test_real_engine_item_db_slot_sort_guard_and_stateless_reset() -> None:
    engine_root = find_runnable_engine()
    assert engine_root is not None
    w = BridgeWorker(engine_root)
    try:
        w.start()
        lc = w.call("list_classes", {})
        witch = next(c for c in lc["classes"] if c["name"] == "Witch")
        w.call("set_class", {"classId": witch["id"]})
        w.call("set_build_field", {"field": "level", "value": 90})
        w.call("add_group", {"label": "Main", "includeInFullDPS": True})
        w.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
        w.call("set_build_field", {"field": "mainSocketGroup", "value": 1})

        # 1. list_sorts as very first DB call (proves ensure_item_dbs on item_db_options)
        sorts = list_item_db_sorts(w)
        assert sorts["listSorts"] is True and any(s["sortMode"] == "FullDPS" for s in sorts["sorts"])

        # 2. Helmet --sort dps page=1 vs page=2 (disjoint & monotonic descending)
        p1 = search_and_try_item_db(w, slot_name="Helmet", sort_mode="dps", limit=5, page=1)
        p2 = search_and_try_item_db(w, slot_name="Helmet", sort_mode="dps", limit=5, page=2)
        assert p1["statSort"] is True and len(p1["matches"]) == 5 and len(p2["matches"]) == 5
        t1 = [m["title"] for m in p1["matches"]]
        t2 = [m["title"] for m in p2["matches"]]
        assert set(t1).isdisjoint(set(t2))
        pow1 = [float(m["measuredPower"]) for m in p1["matches"]]
        pow2 = [float(m["measuredPower"]) for m in p2["matches"]]
        assert pow1 == sorted(pow1, reverse=True) and max(pow2) <= min(pow1) + 1e-6

        # 3. Real bridge.lua DB_STAT_SORT_MAX=300 tooMany guard on Weapon 1 + obtainable=any
        with pytest.raises(ValueError, match="> 300"):
            search_and_try_item_db(w, slot_name="Weapon 1", obtainable="any", sort_mode="dps")

        # 4. Exact targetSlot on Ring 2 when Ring 1 has high Spell/Fire damage and Ring 2 is empty
        ring1_raw = "\n".join(
            [
                "Rarity: RARE", "Strong Ring", "Opal Ring", "Item Level: 85", "Implicits: 1",
                "25% increased Elemental Damage",
                "30% increased Fire Damage",
                "16% increased Cast Speed",
                "+35% to Global Critical Strike Multiplier",
            ]
        )
        w.call("add_item", {"raw": ring1_raw, "slotName": "Ring 1", "equip": True})
        r_ring2 = search_and_try_item_db(w, slot_name="Ring 2", sort_mode="dps", equip=True, item_index=1, limit=5)
        assert r_ring2["filters"].get("targetSlot") == "Ring 2"
        post_dps = float(r_ring2["mutation"]["after"]["primaryDps"])
        assert float(r_ring2["matches"][0]["measuredPower"]) == pytest.approx(post_dps, rel=1e-3)

        # 5. Oversized >64KB Pearl of Tsoatha equips cleanly after variant compaction
        r_pearl = search_and_try_item_db(w, query="Pearl of Tsoatha", slot_name="Ring 2", equip=True, limit=5)
        assert r_pearl["equipped"] is True and r_pearl["slotName"] == "Ring 2"

        # 6. Allocate nearest Witch jewel socket and verify generic Jewel sort completes fast with slotNote
        slots = w.call("list_items", {}).get("slots", [])
        best_sock = None
        best_cost = 9999
        for s in slots:
            if not s.get("nodeId"):
                continue
            try:
                cost = w.call("node_hover", {"id": int(s["nodeId"])}).get("cost")
            except Exception:
                continue
            if cost is not None and int(cost) < best_cost:
                best_cost, best_sock = int(cost), s
        assert best_sock is not None
        w.call("tree_click", {"id": int(best_sock["nodeId"])})
        t0 = time.perf_counter()
        r_jwl = search_and_try_item_db(w, slot_name="Jewel", sort_mode="dps", limit=5)
        slack = max(1.0, float(os.environ.get("POB_TEST_PERF_SLACK", "1.0")))
        assert (time.perf_counter() - t0) < 30.0 * slack
        assert len(r_jwl["matches"]) == 5 and r_jwl.get("slotNote") and r_jwl["filters"].get("targetSlot")

        # 7. Stateless reset: plain EN & ZH Mageblood search succeeds right after Jewel+FullDPS
        r_mb = search_and_try_item_db(w, query="Mageblood", limit=5)
        assert r_mb["statSort"] is False and r_mb["sortMode"] == "Name"
        assert any("Mageblood" in str(m.get("title") or "") for m in r_mb["matches"])
        r_zh = search_and_try_item_db(w, query="法师之血", limit=5)
        assert any("Mageblood" in str(m.get("title") or "") for m in r_zh["matches"])
    finally:
        w.stop()
