"""Unit and real headless PoB engine tests for Wave 1 build-ops slice."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from engine_paths import find_runnable_engine

from pob_cli.analyzer import inspect_config, inspect_items, search_calcs_breakdown
from pob_cli.bridge_client import BridgeWorker
from pob_cli.comparator import _parse_matrix_scenario, run_matrix
from pob_cli.formatters import (
    RAW_PRESERVE_KEYS,
    fmt_delta,
    fmt_num,
    render_update_warning,
    sanitize_for_output,
    to_json_str,
)
from pob_cli.mutators import (
    _edit_raw_item_mods,
    _extract_compact_calcs,
    _safe_ele_max_hit,
    add_skill_group,
    change_tree,
    create_new_build,
    edit_item_mods,
    modify_config,
    query_item_db,
    remove_skill_gem,
    replace_skill_group,
    run_batch,
    run_sweep,
    search_and_try_item_db,
    set_class_ascendancy,
    switch_main,
    unequip_item,
)
from pob_cli.snapshot import diff_digest, history_of, undo_build

POB_DIR = find_runnable_engine()


# ---------------------------------------------------------------------------
# Pure unit tests (formatters, raw item mod editor, matrix parser)
# ---------------------------------------------------------------------------


def test_formatters_inf_nan_and_sentinels() -> None:
    assert fmt_num(None) == "-"
    assert fmt_num("") == "-"
    assert fmt_num(True) == "Yes"
    assert fmt_num(False) == "No"
    assert fmt_num("inf") == "inf"
    assert fmt_num("-inf") == "-inf"
    assert fmt_num("Infinity") == "inf"
    assert fmt_num("nan") == "nan"
    assert fmt_num("N/A") == "N/A"
    assert fmt_num(math.inf) == "inf"
    assert fmt_num(-math.inf) == "-inf"
    assert fmt_num(math.nan) == "nan"
    assert fmt_num(12345.6) == "12,346"
    assert fmt_num(12.34, decimals=2) == "12.34"

    # fmt_delta with CI "inf" sentinels and NaN/strings
    assert fmt_delta("inf", 1000) == "inf -> 1,000"
    assert fmt_delta(1000, "inf") == "1,000 -> inf"
    assert fmt_delta("inf", "inf") == "0"
    assert fmt_delta(math.inf, math.inf) == "0"
    assert fmt_delta(-math.inf, math.inf) == "-inf -> inf"
    assert fmt_delta(math.nan, 100) == "nan -> 100"
    assert fmt_delta(math.nan, math.nan) == "0"
    assert fmt_delta("N/A", "N/A") == "0"
    assert fmt_delta("N/A", "100") == "N/A -> 100"
    assert fmt_delta(100, 150, "%") == "+50% (+50.0%)"


def test_diff_digest_ci_inf_nan_rfc8259_json() -> None:
    """Verify diff_digest + to_json_str handles PoB 'inf'/'-inf'/'nan' stats with valid RFC-8259 JSON."""
    before = {
        "stats": {
            "CombinedDPS": 100000.0,
            "TotalEHP": "inf",
            "ChaosMaximumHitTaken": "inf",
            "PhysicalMaximumHitTaken": 15000.0,
            "Ward": "nan",
            "Life": 1.0,
        },
        "warnings": [],
        "buildInfo": {"points": {"used": 100}},
    }
    after = {
        "stats": {
            "CombinedDPS": 120000.0,
            "TotalEHP": "inf",           # unchanged 'inf' must be skipped!
            "ChaosMaximumHitTaken": 12000.0,  # 'inf' -> finite must emit None delta/pct
            "PhysicalMaximumHitTaken": "inf",  # finite -> 'inf' must emit None delta/pct
            "Ward": math.nan,            # both NaN must be skipped!
            "Life": 1.0,
        },
        "warnings": [],
        "buildInfo": {"points": {"used": 100}},
    }
    diff = diff_digest(before, after)
    by_stat = {d["stat"]: d for d in diff["deltas"]}

    assert "TotalEHP" not in by_stat
    assert "Ward" not in by_stat
    assert "Life" not in by_stat

    assert by_stat["CombinedDPS"]["delta"] == pytest.approx(20000.0)
    assert by_stat["CombinedDPS"]["pct"] == pytest.approx(20.0)

    assert by_stat["ChaosMaximumHitTaken"]["before"] == "inf"
    assert by_stat["ChaosMaximumHitTaken"]["after"] == 12000.0
    assert by_stat["ChaosMaximumHitTaken"]["delta"] is None
    assert by_stat["ChaosMaximumHitTaken"]["pct"] is None
    assert by_stat["ChaosMaximumHitTaken"]["deltaStr"] == "inf -> 12,000"

    assert by_stat["PhysicalMaximumHitTaken"]["before"] == 15000.0
    assert by_stat["PhysicalMaximumHitTaken"]["after"] == "inf"
    assert by_stat["PhysicalMaximumHitTaken"]["delta"] is None
    assert by_stat["PhysicalMaximumHitTaken"]["pct"] is None
    assert by_stat["PhysicalMaximumHitTaken"]["deltaStr"] == "15,000 -> inf"

    json_text = to_json_str(diff)
    assert "NaN" not in json_text
    assert "Infinity" not in json_text

    def _reject_nonfinite(tok: str) -> None:
        raise ValueError(f"Non-RFC-8259 JSON token: {tok}")

    parsed = json.loads(json_text, parse_constant=_reject_nonfinite)
    assert len(parsed["deltas"]) == 3


def test_render_update_warning_and_raw_preserve_keys() -> None:
    assert render_update_warning(None) == ""
    assert render_update_warning("   ") == ""
    assert render_update_warning("GitHub timeout") == "> [WARN] GitHub timeout\n\n"

    assert "updateWarning" in RAW_PRESERVE_KEYS
    assert "customModsText" in RAW_PRESERVE_KEYS
    payload = {
        "display": "^xFF0000Red Text^7",
        "updateWarning": "^7Keep exact warning",
        "customModsText": "^7+100 to maximum Life",
        "beforeRaw": "^7Rarity: RARE",
        "afterRaw": "^7Rarity: RARE",
    }
    sanitized = sanitize_for_output(payload)
    assert sanitized["display"] == "Red Text"
    assert sanitized["updateWarning"] == "^7Keep exact warning"
    assert sanitized["customModsText"] == "^7+100 to maximum Life"
    assert sanitized["beforeRaw"] == "^7Rarity: RARE"
    assert sanitized["afterRaw"] == "^7Rarity: RARE"


def test_edit_raw_item_mods_preserves_headers_and_trailers() -> None:
    sample = "\n".join(
        [
            "Rarity: RARE",
            "Viper Band",
            "Ruby Ring",
            "Crafted: true",
            "Prefix: {range:0.5}IncreasedLife8",
            "Item Level: 84",
            "Implicits: 1",
            "{tags:elemental,fire,resistance}+30% to Fire Resistance",
            "+75 to maximum Life",
            "{tags:elemental,cold,resistance}+42% to Cold Resistance",
            "{crafted}+15% to Fire and Chaos Resistances",
            "Corrupted",
        ]
    )

    after, changes = _edit_raw_item_mods(
        sample,
        replace_mods=[("Cold Resistance", "+45% to Lightning Resistance")],
        remove_mods=["Fire and Chaos"],
        add_mods=["+30 to Intelligence"],
    )

    lines = after.splitlines()
    # Headers untouched
    assert lines[:7] == sample.splitlines()[:7]
    assert lines[7] == "{tags:elemental,fire,resistance}+30% to Fire Resistance"
    assert "+75 to maximum Life" in lines
    assert "+45% to Lightning Resistance" in lines
    assert "+30 to Intelligence" in lines
    assert all("Cold Resistance" not in ln for ln in lines)
    assert all("Fire and Chaos" not in ln for ln in lines)
    # Added mod placed BEFORE Corrupted trailer
    assert lines[-1] == "Corrupted"
    assert lines[-2] == "+30 to Intelligence"
    assert len(changes) == 3

    # Removing an implicit decrements Implicits count
    after_no_imp, _ = _edit_raw_item_mods(sample, remove_mods=["+30% to Fire Resistance"])
    assert "Implicits: 0" in after_no_imp.splitlines()

    # Non-matching pattern raises clear ValueError
    with pytest.raises(ValueError, match="did not match"):
        _edit_raw_item_mods(sample, replace_mods=[("NonExistentModXYZ", "+10 to Life")])
    with pytest.raises(ValueError, match="did not match"):
        _edit_raw_item_mods(sample, remove_mods=["NonExistentModXYZ"])


def test_parse_matrix_scenario_formats() -> None:
    p_orig = _parse_matrix_scenario("original")
    assert p_orig["name"] == "Original" and p_orig["base"] == "Original"

    p_uber = _parse_matrix_scenario("Uber")
    assert p_uber["name"] == "Uber" and p_uber["base"] == "Uber"

    p_wither = _parse_matrix_scenario("Wither15:MultiplierWitheredStackCount=15")
    assert p_wither["name"] == "Wither15"
    assert p_wither["config"] == [("MultiplierWitheredStackCount", "15")]

    p_noguard = _parse_matrix_scenario("NoGuard:FlasksOff")
    assert p_noguard["name"] == "NoGuard"
    assert p_noguard["flasks"] is False

    p_dict = _parse_matrix_scenario(
        {"name": "CustomPinnacle", "base": "Pinnacle", "config": {"usePowerCharges": True}, "flasks": False}
    )
    assert p_dict["name"] == "CustomPinnacle"
    assert p_dict["base"] == "Pinnacle"
    assert p_dict["config"] == [("usePowerCharges", True)]
    assert p_dict["flasks"] is False

    # Commas inside customMods / appendCustomMods must stay intact (P2-2)
    p_res = _parse_matrix_scenario(
        "ResTest:Pinnacle,customMods=+10% to Fire, Cold and Lightning Resistances"
    )
    assert p_res["name"] == "ResTest"
    assert p_res["base"] == "Pinnacle"
    assert p_res["customMods"] == "+10% to Fire, Cold and Lightning Resistances"

    p_res_trail = _parse_matrix_scenario(
        "ResTrail:Pinnacle,customMods=+10% to Fire, Cold and Lightning Resistances,FlasksOff,usePowerCharges=true"
    )
    assert p_res_trail["base"] == "Pinnacle"
    assert p_res_trail["customMods"] == "+10% to Fire, Cold and Lightning Resistances"
    assert p_res_trail["flasks"] is False
    assert p_res_trail["config"] == [("usePowerCharges", "true")]

    p_append = _parse_matrix_scenario(
        "AppendRes:Original,appendCustomMods=+15% to Fire, Cold and Lightning Resistances,no_flasks"
    )
    assert p_append["base"] == "Original"
    assert p_append["appendCustomMods"] == "+15% to Fire, Cold and Lightning Resistances"
    assert p_append["flasks"] is False

    with pytest.raises(ValueError, match="Unrecognized matrix token"):
        _parse_matrix_scenario("Bad:Original,bogusToken")


# ---------------------------------------------------------------------------
# Real headless BridgeWorker integration tests
# ---------------------------------------------------------------------------


def _has_zh_ui(worker: BridgeWorker) -> bool:
    """Only the Windows pob-zh engine translates UI labels; the POSIX LuaJIT runtime is English only."""
    return bool(worker.caps.get("translateToggle"))


@pytest.fixture(scope="module")
def pob_worker():
    """Boot one isolated headless pob-zh.exe worker for build-ops integration tests (never touches .runtime/)."""
    if POB_DIR is None:
        pytest.skip("No runnable PoB engine found (set POB_ROOT or run pob bootstrap)")
    worker = BridgeWorker(slot_name="test", pob_dir=POB_DIR)
    worker.start()

    # Build a clean, deterministic test character in memory (zero network dependency)
    # Witch -> Elementalist with Fireball, Ring 1, Belt, Flask 1, and an allocated jewel socket
    lc = worker.call("list_classes", {})
    witch = next(c for c in lc["classes"] if c["name"] == "Witch")
    worker.call("set_class", {"classId": witch["id"]})
    elem = next(a for a in witch["ascendancies"] if a["name"] == "Elementalist")
    worker.call("set_ascendancy", {"ascendClassId": elem["id"]})

    # Add Fireball main skill
    worker.call("add_group", {"label": "Main Fireball"})
    worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
    worker.call("set_build_field", {"field": "mainSocketGroup", "value": 1})

    # Equip Ring 1, Belt, and an active Diamond Flask
    ring_raw = "\n".join(
        [
            "Rarity: RARE",
            "Test Storm Loop",
            "Ruby Ring",
            "Item Level: 84",
            "Implicits: 1",
            "+30% to Fire Resistance",
            "+70 to maximum Life",
            "+40% to Cold Resistance",
            "+30 to maximum Mana",
        ]
    )
    belt_raw = "\n".join(
        [
            "Rarity: RARE",
            "Test Heavy Belt",
            "Leather Belt",
            "Item Level: 84",
            "Implicits: 1",
            "+40 to maximum Life",
            "+80 to maximum Life",
            "+35% to Fire Resistance",
        ]
    )
    flask_raw = "\n".join(
        [
            "Rarity: MAGIC",
            "Diamond Flask",
            "Quality: 20",
            "Implicits: 0",
        ]
    )
    worker.call("add_item", {"raw": ring_raw, "slotName": "Ring 1", "equip": True})
    worker.call("add_item", {"raw": belt_raw, "slotName": "Belt", "equip": True})
    worker.call("add_item", {"raw": flask_raw, "slotName": "Flask 1", "equip": True})
    worker.call("set_slot_active", {"slotName": "Flask 1", "active": True})

    # Allocate the nearest base-tree jewel socket to Witch start and socket a Cobalt Jewel
    items_res = worker.call("list_items", {})
    socket_slots = [s for s in items_res.get("slots", []) if s.get("nodeId")]
    best_sock = None
    best_cost = 9999
    for sock in socket_slots:
        try:
            hover = worker.call("node_hover", {"id": int(sock["nodeId"])})
        except Exception:
            continue
        cost = hover.get("cost")
        if cost is not None and int(cost) < best_cost:
            best_cost = int(cost)
            best_sock = sock
    if best_sock is not None:
        worker.call("tree_click", {"id": int(best_sock["nodeId"])})
        jewel_raw = "\n".join(
            [
                "Rarity: RARE",
                "Test Cobalt Jewel",
                "Cobalt Jewel",
                "Implicits: 0",
                "+15 to Intelligence",
                "7% increased maximum Life",
            ]
        )
        worker.call("add_item", {"raw": jewel_raw, "slotName": best_sock["name"], "equip": True})

    # Set a config var & custom mod for inspection tests
    worker.call("set_config", {"var": "usePowerCharges", "value": True})
    worker.call(
        "set_custom_mods",
        {"list": [{"title": "Default", "text": "+25 to maximum Mana", "enabled": True}]},
    )
    history_of(worker).load_build(worker.call("export_code", {})["code"], None)

    try:
        yield worker
    finally:
        worker.stop()


def test_inspect_config(pob_worker: BridgeWorker) -> None:
    cfg = inspect_config(pob_worker)
    assert cfg["activeConfigSetId"] is not None
    assert cfg["activeConfigSet"] is not None
    vars_by_name = {v["var"]: v for v in cfg["variables"]}
    assert "usePowerCharges" in vars_by_name
    assert vars_by_name["usePowerCharges"]["value"] is True
    assert vars_by_name["usePowerCharges"]["defaultValue"] is False
    assert "enemyIsBoss" in vars_by_name
    assert "+25 to maximum Mana" in cfg["customModsText"]
    assert "+25 to maximum Mana" in cfg["activeCustomModLines"]

    cfg_all = inspect_config(pob_worker, include_defaults=True)
    assert len(cfg_all["variables"]) > len(cfg["variables"])
    assert cfg_all["totalCount"] == len(cfg_all["variables"])


def test_inspect_items_all_raw_and_backward_compat(pob_worker: BridgeWorker) -> None:
    res = inspect_items(pob_worker, all_raw=True)
    assert res["allRaw"] is True
    slots_by_name = {s["slotName"]: s for s in res["equippedSlots"]}
    assert "Ring 1" in slots_by_name
    assert "Belt" in slots_by_name
    assert "Flask 1" in slots_by_name
    assert "+40% to Cold Resistance" in (slots_by_name["Ring 1"].get("raw") or "")
    assert "+80 to maximum Life" in (slots_by_name["Belt"].get("raw") or "")

    # Allocated jewel socket must be included with raw text
    jewel_entries = [s for s in res["equippedSlots"] if s.get("nodeId") and s.get("itemId")]
    assert len(jewel_entries) >= 1
    assert "Cobalt Jewel" in (jewel_entries[0].get("raw") or "")

    # Backward-compatible single-slot lookup
    single = inspect_items(pob_worker, slot_name="Ring 1", raw=True)
    assert single["slot"]["name"] == "Ring 1"
    assert "+40% to Cold Resistance" in (single["raw"] or "")


def test_search_calcs_breakdown_bilingual(pob_worker: BridgeWorker) -> None:
    # 1. English spaced query
    res_avg = search_calcs_breakdown(pob_worker, "Average Hit")
    assert res_avg["totalMatches"] >= 1
    top_avg = res_avg["matches"][0]
    assert "average hit" in top_avg["rowLabel"].lower()
    assert any(c.get("breakdown") for c in top_avg["cells"])

    # 2. Compact/camelCase query ('Average Hit' == 'averagehit')
    res_compact = search_calcs_breakdown(pob_worker, "averagehit")
    assert res_compact["totalMatches"] >= 1
    assert "average hit" in res_compact["matches"][0]["rowLabel"].lower()

    # 3. English semantic synonym query ('Mana Regeneration' -> Mana / Recovery)
    res_mana_regen = search_calcs_breakdown(pob_worker, "Mana Regeneration")
    assert res_mana_regen["totalMatches"] >= 1
    top_mr = res_mana_regen["matches"][0]
    assert top_mr["sectionId"] == "Mana"
    assert "recovery" in top_mr["rowLabel"].lower()
    assert any(c.get("breakdown") for c in top_mr["cells"])

    # 4. Chinese queries
    if not _has_zh_ui(pob_worker):
        pytest.skip("Chinese calc labels need the pob-zh engine (caps.translateToggle)")
    res_ehp_zh = search_calcs_breakdown(pob_worker, "有效生命")
    assert res_ehp_zh["totalMatches"] >= 1

    res_mr_zh = search_calcs_breakdown(pob_worker, "魔力回复")
    assert res_mr_zh["totalMatches"] >= 1
    assert res_mr_zh["matches"][0]["sectionId"] == "Mana"

    # 5. Even when translate is temporarily turned OFF, ZH queries still work AND translate state is restored!
    pob_worker.call("set_translate", {"enabled": False})
    try:
        res_zh_off = search_calcs_breakdown(pob_worker, "有效生命")
        assert res_zh_off["totalMatches"] >= 1
        assert pob_worker.call("version", {})["translate"] is False
    finally:
        pob_worker.call("set_translate", {"enabled": True})


def _capture_semantic_state(worker: BridgeWorker) -> dict:
    """Capture full semantic build state for strict 0-residue verification (independent of Lua XML attribute order)."""
    stats = worker.call("get_stats", {})["stats"]
    cls = worker.call("list_classes", {})["current"]
    cfg = inspect_config(worker)
    items = inspect_items(worker, all_raw=True)
    return {
        "stats": stats,
        "class": cls,
        "configVars": {v["var"]: v["value"] for v in cfg["variables"]},
        "customModsText": cfg["customModsText"],
        "equippedRaw": {s["slotName"]: s.get("raw") for s in items["equippedSlots"]},
    }


def test_modify_config_append_custom_mods_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    before_life = float(before_state["stats"]["Life"])

    # 1. What-if append (0-residue rollback)
    res_whatif = modify_config(
        pob_worker,
        append_custom_mods="+200 to maximum Life; +10% to all Elemental Resistances",
        apply=False,
    )
    assert res_whatif["applied"] is False
    assert res_whatif["after"]["life"] > before_life + 150
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply append -> preserves existing custom mod AND adds new one, then test clear + append strips '# disabled'
    res_apply = modify_config(
        pob_worker,
        assignments={"MultiplierWitheredStackCount": 10},
        append_custom_mods="+200 to maximum Life",
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        cfg_applied = inspect_config(pob_worker)
        assert "+25 to maximum Mana" in cfg_applied["customModsText"]
        assert "+200 to maximum Life" in cfg_applied["customModsText"]
        vars_by_name = {v["var"]: v for v in cfg_applied["variables"]}
        assert vars_by_name["multiplierWitheredStackCount"]["value"] == 10

        # Clear customMods ('# disabled' placeholder), then append must strip '# disabled'
        modify_config(pob_worker, custom_mods="clear", apply=True)
        try:
            res_after_clear = modify_config(
                pob_worker,
                append_custom_mods="+100 to maximum Energy Shield",
                apply=False,
            )
            appended_entry = next(a for a in res_after_clear["detail"]["assignments"] if a["var"] == "customMods")
            assert "# disabled" not in appended_entry["customModsText"]
            assert appended_entry["customModsText"] == "+100 to maximum Energy Shield"
        finally:
            undo_build(pob_worker)
    finally:
        undo_build(pob_worker)
        history_of(pob_worker).redo.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_edit_item_mods_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    cold_before = float(before_state["stats"]["ColdResist"])
    light_before = float(before_state["stats"]["LightningResist"])

    # 1. What-if replace Cold Res -> Lightning Res (0-residue)
    res_whatif = edit_item_mods(
        pob_worker,
        slot_name="Ring 1",
        replace_mods=[("Cold Resistance", "+45% to Lightning Resistance")],
        apply=False,
    )
    assert res_whatif["applied"] is False
    assert res_whatif["slotName"] == "Ring 1"
    assert "+45% to Lightning Resistance" in res_whatif["afterRaw"]
    assert "Test Storm Loop" in res_whatif["afterRaw"]
    deltas = {d["stat"]: d for d in res_whatif["mutation"]["diff"]["deltas"]}
    assert deltas["ColdResist"]["after"] < cold_before
    assert deltas["LightningResist"]["after"] > light_before
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply add_mods -> persists, then undo restores
    res_apply = edit_item_mods(
        pob_worker,
        slot_name="Ring 1",
        add_mods=["+150 to maximum Life"],
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        ring_after = inspect_items(pob_worker, slot_name="Ring 1", raw=True)
        assert "+150 to maximum Life" in (ring_after["raw"] or "")
    finally:
        undo_build(pob_worker)
        history_of(pob_worker).redo.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_search_and_try_item_db(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)

    # Pure search EN (fast defaults: omits raw and tooltipLines)
    search_en = search_and_try_item_db(pob_worker, query="Mageblood", db_type="UNIQUE", limit=5)
    assert search_en["equipped"] is False
    assert search_en["total"] >= 1
    assert search_en["matches"][0]["title"] == "Mageblood"
    assert "raw" not in search_en["matches"][0]
    assert "tooltipLines" not in search_en["matches"][0]

    # Opt-in tooltips & raw
    search_en_full = query_item_db(
        pob_worker,
        query="Mageblood",
        db_type="UNIQUE",
        limit=3,
        with_tooltips=True,
        with_raw=True,
    )
    assert search_en_full["matches"][0].get("raw")
    assert search_en_full["matches"][0].get("tooltipLines")

    # NFKD diacritic folding fallback (Mjolner -> Mjölner)
    search_diacritic = query_item_db(pob_worker, query="Mjolner", db_type="UNIQUE", limit=5)
    assert search_diacritic["total"] >= 1
    assert any("mj" in (m.get("title") or "").lower() for m in search_diacritic["matches"])

    # Pure search ZH (Chinese item names come from the pob-zh engine only)
    if _has_zh_ui(pob_worker):
        search_zh = search_and_try_item_db(pob_worker, query="法师之血", db_type="UNIQUE", limit=5)
        assert search_zh["total"] >= 1
        assert search_zh["matches"][0]["title"] == "Mageblood"

    # Try equip (what-if, 0-residue)
    try_res = search_and_try_item_db(
        pob_worker,
        query="Mageblood",
        db_type="UNIQUE",
        slot_name="Belt",
        equip=True,
        apply=False,
    )
    assert try_res["equipped"] is True
    assert try_res["applied"] is False
    assert try_res["slotName"] == "Belt"
    assert try_res["tooltip"] is not None
    assert try_res["mutation"] is not None
    assert _capture_semantic_state(pob_worker) == before_state


def test_set_class_ascendancy_what_if_and_apply(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    zh = _has_zh_ui(pob_worker)

    # 1. What-if switch to Templar / Hierophant (ZH) + Aul Bloodline (0-residue)
    res_whatif = set_class_ascendancy(
        pob_worker,
        class_name="Templar",
        ascendancy_name="圣宗" if zh else "Hierophant",
        secondary_ascendancy_name="Aul Bloodline",
        apply=False,
    )
    assert res_whatif["applied"] is False
    after_detail = res_whatif["detail"]["after"]
    assert after_detail["className"] == "Templar"
    assert after_detail["ascendClassName"] == "Hierophant"
    assert after_detail["secondaryAscendClassId"] > 0
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. Apply switch to Reliquarian (inferred Scion) + Abyssal Bloodline, then undo
    res_apply = set_class_ascendancy(
        pob_worker,
        ascendancy_name="Reliquarian",
        secondary_ascendancy_name="深渊血脉" if zh else "Abyssal Bloodline",
        apply=True,
    )
    assert res_apply["applied"] is True
    try:
        cur = pob_worker.call("list_classes", {})["current"]
        assert cur["className"] == "Scion"
        assert cur["ascendClassName"] == "Reliquarian"
        assert cur["secondaryAscendClassId"] > 0
    finally:
        undo_build(pob_worker)
        history_of(pob_worker).redo.clear()

    assert _capture_semantic_state(pob_worker) == before_state


def test_run_matrix_zero_residue(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    before_undo = list(history_of(pob_worker).undo)
    before_redo = list(history_of(pob_worker).redo)
    before_last_good = pob_worker.last_good_code

    # Default 4 scenarios
    res_default = run_matrix(pob_worker)
    assert res_default["baseline"] == "Original"
    names = [s["name"] for s in res_default["scenarios"]]
    assert names == ["Original", "Pinnacle", "Uber", "FlasksOff"]
    # Uber Boss DPS should be strictly lower than Original (Original has Power Charges + Diamond Flask + Pinnacle)
    def _sc_val(sc: dict, key: str) -> float:
        src = sc.get("digest") or sc.get("summary") or {}
        return float(src[key])

    orig_dps = _sc_val(res_default["scenarios"][0], "primaryDps")
    pinnacle_dps = _sc_val(res_default["scenarios"][1], "primaryDps")
    uber_dps = _sc_val(res_default["scenarios"][2], "primaryDps")
    flasks_off_dps = _sc_val(res_default["scenarios"][3], "primaryDps")
    assert orig_dps > pinnacle_dps > uber_dps > 0
    assert orig_dps > flasks_off_dps > 0

    # Custom scenarios (string + dict)
    res_custom = run_matrix(
        pob_worker,
        scenarios=[
            "Original",
            "Wither15:MultiplierWitheredStackCount=15",
            "NoGuard:FlasksOff",
            {"name": "PinnaclePlusLife", "base": "Pinnacle", "customMods": "+500 to maximum Life"},
        ],
    )
    assert [s["name"] for s in res_custom["scenarios"]] == [
        "Original",
        "Wither15",
        "NoGuard",
        "PinnaclePlusLife",
    ]
    orig_life = _sc_val(res_custom["scenarios"][0], "life")
    plus_life = _sc_val(res_custom["scenarios"][3], "life")
    assert plus_life >= orig_life + 500

    # Strict 0-residue & stack preservation check
    assert _capture_semantic_state(pob_worker) == before_state
    assert history_of(pob_worker).undo == before_undo
    assert history_of(pob_worker).redo == before_redo
    assert pob_worker.last_good_code == before_last_good


# ---------------------------------------------------------------------------
# Pure unit tests for Mutators overhaul (mock BridgeWorker, RPC-count checks)
# ---------------------------------------------------------------------------


class _FakeBridgeWorker:
    """In-memory mock BridgeWorker for verifying RPC reduction, batching, cascade guard, and sweep isolation."""

    def __init__(self) -> None:
        import threading

        self.lock = threading.RLock()
        self.last_good_code: str | None = None
        self.caps: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.batch_calls: list[list[tuple[str, dict]]] = []

        # Mutable simulated build state
        self.allocated: set[int] = {1, 2, 3, 4, 5, 6, 7}
        self.cascade_children: dict[int, set[int]] = {2: {3, 4, 5, 6, 7}}
        self.nodes_db: dict[str, dict] = {
            str(i): {"id": i, "name": f"Node {i}", "type": "Normal"} for i in range(1, 20)
        }
        self.config_vars: dict[str, object] = {"enemyIsBoss": "Pinnacle", "usePowerCharges": False}
        self.dps: float = 100000.0
        self.life: float = 4000.0
        self.groups: list[dict] = [
            {
                "index": 1,
                "label": "Main",
                "enabled": True,
                "includeInFullDPS": False,
                "isMain": True,
                "gems": [
                    {"index": 1, "name": "Fireball", "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True},
                    {"index": 2, "name": "Combustion Support", "nameSpec": "Combustion Support", "level": 20, "quality": 20, "enabled": True},
                ],
            }
        ]
        self.item_db_entries: list[dict] = [
            {
                "title": "Doppelgänger Guise",
                "name": "Doppelgänger Guise",
                "baseName": "Sadist Garb",
                "type": "Body Armour",
                "primarySlot": "Body Armour",
                "raw": "Rarity: UNIQUE\nDoppelgänger Guise\nSadist Garb\nImplicits: 0\n+40% more Damage",
            },
            {
                "title": "Kaom's Heart",
                "name": "Kaom's Heart",
                "baseName": "Glorious Plate",
                "type": "Body Armour",
                "primarySlot": "Body Armour",
                "raw": "Rarity: UNIQUE\nKaom's Heart\nGlorious Plate\nImplicits: 0\n+500 to maximum Life",
            },
        ]

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict:
        return {"nodes": self.nodes_db}

    def _snapshot(self) -> str:
        return json.dumps(
            {
                "allocated": sorted(self.allocated),
                "config_vars": dict(self.config_vars),
                "dps": self.dps,
                "life": self.life,
                "groups": json.loads(json.dumps(self.groups)),
            }
        )

    def _restore(self, code: str) -> None:
        data = json.loads(code)
        self.allocated = set(data["allocated"])
        self.config_vars = dict(data["config_vars"])
        self.dps = float(data["dps"])
        self.life = float(data["life"])
        self.groups = data["groups"]

    def _tree_state(self) -> dict:
        return {
            "treeVersion": "3_26",
            "allocatedNodes": sorted(self.allocated),
            "dynamicNodes": [],
            "points": {"used": len(self.allocated), "usedMax": 123, "ascUsed": 0, "ascMax": 8},
        }

    def call_batch(
        self,
        calls: list[tuple[str, dict | None]],
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict]:
        self.batch_calls.append([(m, dict(p or {})) for m, p in calls])
        out: list[dict] = []
        for method, params in calls:
            if raise_on_error:
                out.append(self.call(method, params or {}))
            else:
                try:
                    out.append(self.call(method, params or {}))
                except Exception as exc:
                    out.append({"error": str(exc)})
        return out

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        p = dict(params or {})
        self.calls.append((method, p))

        if method == "export_code":
            return {"code": self._snapshot()}
        if method == "import_code":
            self._restore(p["code"])
            return {"ok": True}
        if method == "get_stats":
            return {
                "stats": {
                    "CombinedDPS": self.dps,
                    "TotalDPS": self.dps,
                    "TotalEHP": self.life * 10.0,
                    "Life": self.life,
                    "Mana": 500.0,
                    "PhysicalMaximumHitTaken": 12000.0,
                    "FireMaximumHitTaken": 32000.0,
                    "ColdMaximumHitTaken": 28000.0,
                    "LightningMaximumHitTaken": 35000.0,
                    "ChaosMaximumHitTaken": 15000.0,
                    "FireResist": 75.0,
                    "ColdResist": 75.0,
                    "LightningResist": 75.0,
                    "ChaosResist": 0.0,
                }
            }
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return {"className": "Witch", "ascendClassName": "Elementalist", "level": 90, "points": self._tree_state()["points"]}
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1}}
        if method == "get_tree_state":
            return self._tree_state()
        if method == "tree_click":
            nid = int(p["id"])
            if nid in self.allocated:
                self.allocated.discard(nid)
                self.allocated -= self.cascade_children.get(nid, set())
            else:
                self.allocated.add(nid)
            return self._tree_state()
        if method == "node_info":
            return {"id": p["id"], "diff": []}
        if method == "node_hover":
            return {"id": p["id"], "cost": 1, "allocated": int(p["id"]) in self.allocated}
        if method == "item_db":
            q = (p.get("query") or "").strip().lower()
            if not q:
                items = list(self.item_db_entries) if int(p.get("page", 1)) == 1 else []
            else:
                # Simulate Lua filter that misses diacritic-stripped queries
                items = [it for it in self.item_db_entries if q in it["title"].lower()]
            return {"items": items, "total": len(items)}
        if method == "item_tooltip":
            return {"lines": [f"Tooltip for {p.get('raw', '')[:20]}"], "summary": {"primarySlot": "Body Armour"}}
        if method == "list_config":
            return {
                "activeConfigSetId": 1,
                "configSets": [{"id": 1, "title": "Default"}],
                "sections": [
                    {
                        "name": "General",
                        "items": [
                            {"var": k, "value": v, "type": "check" if isinstance(v, bool) else "list"}
                            for k, v in self.config_vars.items()
                        ],
                    }
                ],
                "customMods": [],
            }
        if method == "set_config":
            self.config_vars[p["var"]] = p["value"]
            if p["var"] == "usePowerCharges" and p["value"]:
                self.dps += 25000.0
            return {"ok": True}
        if method == "reset_config":
            self.config_vars[p["var"]] = False
            return {"ok": True}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": json.loads(json.dumps(self.groups)), "skillSets": []}
        if method == "gem_search":
            q = str(p.get("query") or "")
            if "invalid_gem_boom" in q.lower():
                raise RuntimeError("Simulated bridge failure on bad gem")
            return {"gems": [{"name": q, "gemId": f"Gem/{q}"}]}
        if method == "delete_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            idx = int(p["index"])
            if idx < 1 or idx > len(grp["gems"]):
                raise RuntimeError(f"Bad gem index {idx}")
            grp["gems"].pop(idx - 1)
            for j, gm in enumerate(grp["gems"], start=1):
                gm["index"] = j
            return {"ok": True}
        if method == "add_group":
            new_idx = max((int(g["index"]) for g in self.groups), default=0) + 1
            grp = {
                "index": new_idx,
                "label": p.get("label", ""),
                "slot": p.get("slot"),
                "enabled": True,
                "includeInFullDPS": False,
                "isMain": False,
                "gems": [],
            }
            self.groups.append(grp)
            return {"index": new_idx, "group": grp}
        if method == "add_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            new_gem = {
                "index": len(grp["gems"]) + 1,
                "name": p.get("nameSpec") or p.get("gemId"),
                "nameSpec": p.get("nameSpec"),
                "level": p.get("level", 20),
                "quality": p.get("quality", 20),
                "enabled": p.get("enabled", True),
                "count": p.get("count", 1),
                "enableGlobal1": True,
                "enableGlobal2": True,
            }
            grp["gems"].append(new_gem)
            if new_gem["name"] == "Arc":
                self.dps = 180000.0
            return {"gem": dict(new_gem)}
        if method == "set_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            idx = int(p["index"])
            gm = next(g for g in grp["gems"] if int(g["index"]) == idx)
            for k in ("nameSpec", "gemId", "level", "quality", "enabled", "count", "enableGlobal1", "enableGlobal2"):
                if k in p:
                    gm[k] = p[k]
            return {"gem": dict(gm)}
        if method == "set_group":
            grp = next(g for g in self.groups if g["index"] == int(p["index"]))
            for k in ("label", "enabled", "includeInFullDPS", "slot"):
                if k in p:
                    grp[k] = p[k]
            return {"group": grp}
        if method == "list_items":
            return {
                "slots": [
                    {"name": "Body Armour", "label": "Body Armour", "selItemId": 0},
                    {"name": "Ring 1", "label": "Ring 1", "selItemId": 0},
                ],
                "items": [],
                "itemSets": [],
            }
        if method == "get_calcs":
            return {
                "sections": [
                    {
                        "id": "SkillHitDamage",
                        "subsections": [
                            {
                                "label": "Hit Damage",
                                "labelZh": "击中伤害",
                                "rows": [
                                    {
                                        "label": "Average Hit",
                                        "labelZh": "平均击中",
                                        "cells": [{"raw": f"{int(self.dps / 2)}", "text": f"{int(self.dps / 2)}"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        raise AssertionError(f"Unexpected mock RPC '{method}' with params {p}")


def test_change_tree_reuses_tree_click_state_and_cascade_guard() -> None:
    w = _FakeBridgeWorker()

    # 1. Allocating 3 nodes must call get_tree_state ONLY ONCE (reuses tree_click return payload!)
    res_alloc = change_tree(w, allocate=["10", "11", "12"], apply=True)
    assert res_alloc["applied"] is True
    assert res_alloc["pointsDelta"] == 3
    assert res_alloc["orphanedNodes"] == []
    assert "cascadeWarning" not in res_alloc
    get_tree_calls = [m for m, _ in w.calls if m == "get_tree_state"]
    assert len(get_tree_calls) == 1
    assert {10, 11, 12}.issubset(w.allocated)

    # 2. Cascade deallocation of >3 orphaned nodes blocked when allow_cascade=False (and rolled back!)
    alloc_before = set(w.allocated)
    with pytest.raises(ValueError, match="Cascade deallocation blocked: 5 orphaned"):
        change_tree(w, deallocate=["2"], apply=True, allow_cascade=False)
    assert w.allocated == alloc_before

    # 3. Cascade deallocation allowed when allow_cascade=True, emits orphanedNodes + cascadeWarning + pointsDelta
    res_casc = change_tree(w, deallocate=["2"], apply=False, allow_cascade=True)
    assert res_casc["orphanedNodes"] == [3, 4, 5, 6, 7]
    assert res_casc["pointsDelta"] == -6
    assert "5 additional orphaned" in res_casc["cascadeWarning"]
    assert res_casc["detail"]["cascadeWarning"] == res_casc["cascadeWarning"]
    assert w.allocated == alloc_before  # what-if rolled back


def test_query_item_db_fast_defaults_diacritics_and_opt_in_tooltips() -> None:
    w = _FakeBridgeWorker()

    # Default search: NFKD diacritic folding ("Doppelganger" -> "Doppelgänger Guise"), NO item_tooltip, NO raw!
    res_fast = query_item_db(w, query="Doppelganger", db_type="UNIQUE", limit=5)
    assert res_fast["total"] == 1
    assert res_fast["matches"][0]["title"] == "Doppelgänger Guise"
    assert "raw" not in res_fast["matches"][0]
    assert "tooltipLines" not in res_fast["matches"][0]
    assert all(m != "item_tooltip" for m, _ in w.calls)

    # Apostrophe folding ("Kaoms Heart" -> "Kaom's Heart")
    res_apos = query_item_db(w, query="Kaoms Heart", db_type="UNIQUE", limit=5)
    assert res_apos["total"] == 1
    assert res_apos["matches"][0]["title"] == "Kaom's Heart"

    # Opt-in tooltips + raw uses call_batch!
    w.calls.clear()
    w.batch_calls.clear()
    res_full = query_item_db(w, query="Doppelganger", db_type="UNIQUE", limit=5, with_tooltips=True, with_raw=True)
    assert res_full["matches"][0]["raw"].startswith("Rarity: UNIQUE")
    assert res_full["matches"][0]["tooltipLines"]
    assert any(
        all(m == "item_tooltip" for m, _ in batch)
        for batch in w.batch_calls
        if batch
    )


def test_batch_tab_caching_replace_group_and_no_undo_fast_mode() -> None:
    w = _FakeBridgeWorker()
    initial_snap = w._snapshot()
    w.last_good_code = initial_snap

    # Execute 2 consecutive config ops + replace_group + tree + skill with apply=True, no_undo=True
    res = run_batch(
        w,
        [
            {"op": "config", "set": {"usePowerCharges": True}},
            {"op": "config", "set": {"enemyIsBoss": "Uber"}},
            {
                "op": "replace_group",
                "group": "main",
                "label": "Arc Setup",
                "gems": ["Arc:21:20", {"name": "Spell Echo Support", "level": 20, "quality": 20}],
            },
            {"op": "tree", "allocate": ["10"]},
            {"op": "set_group", "group": "main", "includeInFullDPS": True},
        ],
        apply=True,
        no_undo=True,
    )
    assert res["applied"] is True
    assert res["noUndo"] is True
    assert res["undoDepth"] == 0
    assert res["after"]["primaryDps"] == pytest.approx(180000.0)

    # Verify only 1 post-commit export_code (no pre-mutation export_code) AND last_good_code + slot state persisted!
    export_calls = [m for m, _ in w.calls if m == "export_code"]
    assert len(export_calls) == 1
    assert w.last_good_code != initial_snap
    assert w.last_good_code == w._snapshot()
    assert history_of(w).rev == 1  # one commit, one slot save

    # Verify tree invalidated skills cache (list_skills called again for set_group after tree)
    list_skill_calls = [m for m, _ in w.calls if m == "list_skills"]
    assert len(list_skill_calls) == 2

    # Verify list_config was called ONLY ONCE across both config ops (schema cached; boss re-check adds 1)
    list_cfg_calls = [m for m, _ in w.calls if m == "list_config"]
    assert len(list_cfg_calls) <= 2

    # Verify replace_group deleted old gems in reverse order (2 then 1) in ONE call_batch!
    del_batches = [b for b in w.batch_calls if b and all(m == "delete_gem" for m, _ in b)]
    assert len(del_batches) == 1
    assert [p["index"] for _, p in del_batches[0]] == [2, 1]
    assert [g["name"] for g in w.groups[0]["gems"]] == ["Arc", "Spell Echo Support"]

    # Verify a subsequent failed no_undo batch rolls back to Batch 1's committed state (NOT stale initial_snap!)
    committed_snap = w._snapshot()
    with pytest.raises(ValueError, match="Unsupported batch op"):
        run_batch(
            w,
            [
                {"op": "config", "set": {"enemyIsBoss": "Standard"}},
                {"op": "nonexistent_boom_op"},
            ],
            apply=True,
            no_undo=True,
        )
    assert w._snapshot() == committed_snap


def test_run_sweep_fault_isolation_and_rollback_mock() -> None:
    w = _FakeBridgeWorker()
    orig_snap = w._snapshot()

    res = run_sweep(
        w,
        [
            {
                "name": "Arc Swap",
                "ops": [{"op": "replace_group", "group": "main", "gems": ["Arc:20:20"]}],
            },
            {
                "name": "Broken Variant",
                "ops": [
                    {"op": "config", "set": {"usePowerCharges": True}},  # mutates before failing!
                    {"op": "unsupported_bad_op_xyz"},
                ],
            },
            {
                "name": "Power Charges",
                "ops": [{"op": "config", "set": {"usePowerCharges": True}}],
            },
        ],
        base_ops=[{"op": "config", "set": {"enemyIsBoss": "Uber"}}],
        calc_keywords=["Average Hit"],
    )

    assert res["succeeded"] == 2
    assert res["failed"] == 1
    assert res["baseOpsCount"] == 1
    assert res["variants"][0]["ok"] is True
    assert res["variants"][0]["summary"]["primaryDps"] == pytest.approx(180000.0)
    assert res["variants"][0]["summary"]["eleMaxHit"] == pytest.approx(28000.0)
    assert res["baseline"]["summary"]["eleMaxHit"] == pytest.approx(28000.0)
    assert res["variants"][0]["calcs"]["Average Hit"][0]["values"][0]["raw"] == "90000"

    assert res["variants"][1]["ok"] is False
    assert "Unsupported batch op" in res["variants"][1]["error"]

    # Variant 3 must run on a clean sweep_base_code (NOT polluted by Variant 2's partial mutation!)
    assert res["variants"][2]["ok"] is True
    assert res["variants"][2]["summary"]["primaryDps"] == pytest.approx(125000.0)

    # 0-residue guarantee
    assert w._snapshot() == orig_snap


def test_switch_main_loadouts_entries_kind_sentinels_and_zh() -> None:
    w = _FakeBridgeWorker()
    selected_loadouts: list[int] = []
    loadout_payload: dict = {
        "entries": [
            {"index": 1, "label": "Loadouts:", "kind": "header"},
            {"index": 2, "label": "Bossing", "labelZh": "打王", "kind": "loadout"},
            {"index": 3, "label": "Mapping", "labelZh": "刷图", "kind": "loadout"},
            {"index": 4, "label": "Manage Loadouts", "kind": "action"},
            {"index": 5, "label": "No Loadouts", "kind": "loadout"},
        ],
        "selIndex": 2,
    }

    orig_call = w.call

    def _patched_call(method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if method == "list_loadouts":
            w.calls.append((method, dict(params or {})))
            return loadout_payload
        if method == "select_loadout":
            w.calls.append((method, dict(params or {})))
            selected_loadouts.append(int((params or {})["index"]))
            return {"ok": True}
        return orig_call(method, params, timeout=timeout)

    w.call = _patched_call  # type: ignore[method-assign]

    # 1-based ordinal resolves first real loadout (raw index 2, skipping header)
    switch_main(w, loadout_index=1)
    assert selected_loadouts[-1] == 2

    # Chinese labelZh matches Mapping (raw index 3)
    switch_main(w, loadout_index="刷图")
    assert selected_loadouts[-1] == 3

    # Batch switch_main resolves English label
    run_batch(w, [{"op": "switch_main", "loadout": "Bossing"}], apply=False)
    assert selected_loadouts[-1] == 2

    # Only header/action/sentinel entries -> raises clear ValueError
    loadout_payload = {
        "entries": [
            {"index": 1, "label": "Loadouts:", "kind": "header"},
            {"index": 2, "label": "No Loadouts", "kind": "loadout"},
            {"index": 3, "label": "Manage Loadouts", "kind": "action"},
        ]
    }
    with pytest.raises(ValueError, match="no synced loadouts"):
        switch_main(w, loadout_index=1)


def test_source_skill_group_guards_replace_and_remove_gem_1() -> None:
    w = _FakeBridgeWorker()
    w.groups = [
        {
            "index": 1,
            "label": "Death Aura",
            "slot": "Body Armour",
            "source": "Item:1:Body Armour",
            "enabled": True,
            "includeInFullDPS": False,
            "isMain": True,
            "gems": [
                {"index": 1, "name": "Death Aura", "nameSpec": "Death Aura", "level": 20, "quality": 0, "enabled": True},
                {"index": 2, "name": "Swift Affliction Support", "nameSpec": "Swift Affliction Support", "level": 20, "quality": 20, "enabled": True},
            ],
        },
        {
            "index": 2,
            "label": "Auras",
            "enabled": True,
            "includeInFullDPS": False,
            "isMain": False,
            "gems": [
                {"index": 1, "name": "Determination", "nameSpec": "Determination", "level": 20, "quality": 20, "enabled": True},
            ],
        },
    ]

    # replace_skill_group & batch replace_group refuse source groups without calling delete_gem
    with pytest.raises(ValueError, match="slot='Body Armour'"):
        replace_skill_group(w, "main", ["Arc:20:20"], apply=False)
    with pytest.raises(ValueError, match="slot='Body Armour'"):
        run_batch(w, [{"op": "replace_group", "group": "main", "gems": ["Arc:20:20"]}], apply=False)
    assert all(m != "delete_gem" for m, _ in w.calls)

    # remove_skill_gem & batch skill removeGem refuse gem #1 of a source group
    with pytest.raises(ValueError, match="remove gem #1"):
        remove_skill_gem(w, group_selector="main", gem_selector=1, apply=False)
    with pytest.raises(ValueError, match="remove gem #1"):
        run_batch(w, [{"op": "skill", "group": "main", "removeGem": "Death Aura"}], apply=False)
    assert all(m != "delete_gem" for m, _ in w.calls)

    # Removing gem #2 (a regular support gem) from the same source group is still allowed
    rm_res = remove_skill_gem(w, group_selector="main", gem_selector=2, apply=False)
    assert rm_res["detail"]["removedGem"]["name"] == "Swift Affliction Support"
    batch_rm = run_batch(w, [{"op": "skill", "group": "main", "removeGem": 2}], apply=False)
    assert batch_rm["detail"]["steps"][0]["op"] == "skill"


def test_run_batch_no_undo_fresh_slot_rollback_and_redo_clear() -> None:
    w = _FakeBridgeWorker()
    assert w.last_good_code is None
    orig_snap = w._snapshot()

    # 1. Fresh slot (last_good_code=None): mid-batch failure MUST roll back step 1!
    with pytest.raises(ValueError, match="Unsupported batch op"):
        run_batch(
            w,
            [
                {"op": "config", "set": {"usePowerCharges": True}},
                {"op": "nonexistent_boom_op"},
            ],
            apply=True,
            no_undo=True,
        )
    assert w._snapshot() == orig_snap
    assert w.config_vars["usePowerCharges"] is False

    # 2. Successful no_undo commit clears the redo stack BEFORE the slot is saved
    hist = history_of(w)
    hist.redo = ["STALE_REDO_CODE"]
    redo_len_at_save: list[int] = []
    hist.save = lambda: redo_len_at_save.append(len(hist.redo))  # type: ignore[method-assign]
    res = run_batch(
        w,
        [{"op": "config", "set": {"usePowerCharges": True}}],
        apply=True,
        no_undo=True,
    )
    assert res["redoDepth"] == 0
    assert hist.redo == []
    assert redo_len_at_save == [0]


def test_batch_cache_invalidations_and_flask_bounds() -> None:
    w = _FakeBridgeWorker()
    orig_call = w.call

    def _ext_call(method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if method in ("set_build_field", "set_custom_mods", "set_slot_active"):
            w.calls.append((method, dict(params or {})))
            return {"ok": True}
        if method == "item_edit_begin":
            w.calls.append((method, dict(params or {})))
            return {"name": "Test Amulet", "actions": {"anoint": True}}
        if method == "item_edit_popup":
            w.calls.append((method, dict(params or {})))
            return {
                "controls": [
                    {"name": "notableDB", "options": [{"id": 1, "label": "Whispers of Doom", "labelZh": "灭世之语"}]}
                ]
            }
        if method == "item_edit_commit":
            w.calls.append((method, dict(params or {})))
            return {"item": {"id": 99, "name": "Test Amulet"}}
        if method == "list_items":
            w.calls.append((method, dict(params or {})))
            return {
                "slots": [
                    {"name": "Amulet", "label": "Amulet", "selItemId": 50},
                    {"name": "Ring 1", "label": "Ring 1", "selItemId": 0},
                ],
                "items": [{"id": 50, "name": "Test Amulet"}],
                "itemSets": [],
            }
        return orig_call(method, params, timeout=timeout)

    w.call = _ext_call  # type: ignore[method-assign]

    # 1. set_level invalidates tree, config, items, skills; customMods & anoint invalidate skills
    run_batch(
        w,
        [
            {"op": "tree", "allocate": ["10"]},
            {"op": "config", "set": {"usePowerCharges": True}},
            {"op": "set_group", "group": "main", "includeInFullDPS": True},
            {"op": "set_level", "level": 95},
            {"op": "tree", "allocate": ["11"]},
            {"op": "config", "customMods": "+50 to maximum Life"},
            {"op": "set_group", "group": "main", "enabled": True},
            {"op": "anoint", "slot": "Amulet", "notable": "Whispers of Doom"},
            {"op": "set_group", "group": "main", "label": "Main After Anoint"},
            {"op": "flask", "index": "flask 1", "active": False},
        ],
        apply=False,
    )
    # get_tree_state: before set_level (step 1) AND re-fetched after set_level (step 5)
    assert sum(1 for m, _ in w.calls if m == "get_tree_state") == 2
    # list_skills: step 3, re-fetched after set_level+customMods (step 7), re-fetched after anoint (step 9)
    assert sum(1 for m, _ in w.calls if m == "list_skills") == 3
    # flask "flask 1" normalized to "Flask 1"
    flask_calls = [p for m, p in w.calls if m == "set_slot_active"]
    assert flask_calls == [{"slotName": "Flask 1", "active": False}]

    # 2. Batch flask out-of-bounds validation
    for bad_sel in (0, 6, "flask 0", "flask 6", "bogus"):
        with pytest.raises(ValueError, match="Flask selector"):
            run_batch(w, [{"op": "flask", "index": bad_sel, "active": True}], apply=False)


def test_create_new_build_buildinfo_safe_ele_max_hit_and_compact_calcs_cjk() -> None:
    # 1. _safe_ele_max_hit handles None without crashing or skewing min
    assert _safe_ele_max_hit({"FireMaximumHitTaken": None, "ColdMaximumHitTaken": 25000.0, "LightningMaximumHitTaken": None}) == 25000.0
    assert _safe_ele_max_hit({"FireMaximumHitTaken": None, "ColdMaximumHitTaken": None, "LightningMaximumHitTaken": None}) is None
    assert _safe_ele_max_hit({"FireMaximumHitTaken": "inf", "ColdMaximumHitTaken": "inf", "LightningMaximumHitTaken": "inf"}) == "inf"

    # 2. create_new_build promotes buildInfo to top-level
    w = _FakeBridgeWorker()
    orig_call = w.call

    def _nb_call(method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if method in ("new_build", "set_build_field"):
            w.calls.append((method, dict(params or {})))
            return {"ok": True}
        return orig_call(method, params, timeout=timeout)

    w.call = _nb_call  # type: ignore[method-assign]
    nb_res = create_new_build(w, level=92, apply=False)
    assert nb_res["buildInfo"] is not None
    assert nb_res["buildInfo"] == nb_res["detail"]["buildInfo"]

    # 3. _extract_compact_calcs toggles set_translate for CJK keywords when translate_enabled=False and restores in finally
    w_en = _FakeBridgeWorker()
    w_en.caps = {"translateToggle": True}
    w_en.translate_enabled = False
    orig_en_call = w_en.call

    def _tr_call(method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if method == "set_translate":
            w_en.calls.append((method, dict(params or {})))
            w_en.translate_enabled = bool((params or {}).get("enabled"))
            return {"ok": True}
        if method == "get_calcs":
            w_en.calls.append((method, dict(params or {})))
            row = {
                "label": "Average Hit",
                "cells": [{"raw": "50000", "text": "50000"}],
            }
            if w_en.translate_enabled:
                row["labelZh"] = "平均击中"
            return {"sections": [{"id": "SkillHitDamage", "subsections": [{"label": "Hit Damage", "rows": [row]}]}]}
        return orig_en_call(method, params, timeout=timeout)

    w_en.call = _tr_call  # type: ignore[method-assign]

    # ASCII keyword does NOT toggle translate
    ascii_res = _extract_compact_calcs(w_en, ["Average Hit"])
    assert len(ascii_res["Average Hit"]) == 1
    assert [m for m, _ in w_en.calls if m == "set_translate"] == []
    assert w_en.translate_enabled is False

    # CJK keyword toggles set_translate(True) -> get_calcs -> set_translate(False)
    zh_res = _extract_compact_calcs(w_en, ["平均击中"])
    assert len(zh_res["平均击中"]) == 1
    assert zh_res["平均击中"][0]["rowLabelZh"] == "平均击中"
    tr_calls = [p for m, p in w_en.calls if m == "set_translate"]
    assert tr_calls == [{"enabled": True}, {"enabled": False}]
    assert w_en.translate_enabled is False


def test_calc_names_the_calcs_tab_skill_and_warns_when_it_is_not_the_main_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PoB's Calcs tab keeps its own skill selection, so `pob calc` says whose numbers it shows."""
    from click.testing import CliRunner

    from pob_cli import cli_inspect_parts
    from pob_cli.cli import main

    groups = [{"val": 1, "label": "Arc", "labelZh": "电弧"}, {"val": 2, "label": "Hexblast"}]
    skills = [{"val": 1, "label": "Vaal Arc"}, {"val": 2, "label": "Arc"}]
    main_skill = 1
    row = {"label": "Average Hit", "cells": [{"raw": "327610", "text": "327,610"}]}
    w = _FakeBridgeWorker()

    def _call(method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if method == "get_calcs":
            return {
                "sections": [{"id": "SkillHitDamage", "subsections": [{"label": "Hit Damage", "rows": [row]}]}],
                "selectors": {
                    "mainSocketGroup": {"index": 1, "list": groups},
                    "mainSkill": {"index": 1, "list": skills},
                },
            }
        if method == "get_build_header":
            return {
                "mainSocketGroup": {"index": main_group, "list": groups},
                "mainSkill": {"index": main_skill, "list": skills},
            }
        raise AssertionError(method)

    w.call = _call  # type: ignore[method-assign]

    main_group = 2
    res = search_calcs_breakdown(w, "Average Hit", no_breakdown=True)
    skill = res["calcsSkill"]
    assert (skill["socketGroup"], skill["label"], skill["mainSocketGroup"]) == (1, "Arc", 2)
    assert skill["isMainSkill"] is False

    monkeypatch.setattr(cli_inspect_parts, "invoke_daemon", lambda *_a, **_kw: res)
    out = CliRunner().invoke(main, ["calc", "Average Hit"]).output
    assert "- Calcs tab skill: #1 电弧 / Vaal Arc" in out
    assert "[WARN]" in out and "not the main skill (#2 Hexblast" in out

    # Same socket group, another active skill in it: the two must not read as the same thing.
    main_group, main_skill = 1, 2
    other = search_calcs_breakdown(w, "Average Hit", no_breakdown=True)
    assert other["calcsSkill"]["isMainSkill"] is False
    monkeypatch.setattr(cli_inspect_parts, "invoke_daemon", lambda *_a, **_kw: other)
    assert "not the main skill (#1 电弧 / Arc)" in CliRunner().invoke(main, ["calc", "Average Hit"]).output

    main_skill = 1
    same = search_calcs_breakdown(w, "Average Hit", no_breakdown=True)
    assert same["calcsSkill"]["isMainSkill"] is True
    monkeypatch.setattr(cli_inspect_parts, "invoke_daemon", lambda *_a, **_kw: same)
    assert "[WARN]" not in CliRunner().invoke(main, ["calc", "Average Hit"]).output


# ---------------------------------------------------------------------------
# Real headless PoB integration tests for standalone helpers, expanded batch & sweep
# ---------------------------------------------------------------------------


def test_standalone_helpers_new_build_groups_unequip(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)

    # 1. add_skill_group (what-if, 0-residue)
    add_grp = add_skill_group(
        pob_worker,
        gems=["Arc:21:20", {"name": "Controlled Destruction Support", "level": 20, "quality": 20}],
        label="Arc Test Group",
        include_in_full_dps=True,
        apply=False,
    )
    assert add_grp["applied"] is False
    assert len(add_grp["detail"]["gemsAdded"]) == 2
    assert _capture_semantic_state(pob_worker) == before_state

    # 2. replace_skill_group (apply + undo)
    rep_grp = replace_skill_group(
        pob_worker,
        "main",
        ["Arc:20:20", "Controlled Destruction Support:20:20"],
        label="Replaced Arc",
        apply=True,
    )
    assert rep_grp["applied"] is True
    assert rep_grp["detail"]["removedCount"] >= 1
    try:
        skills_after = pob_worker.call("list_skills", {})
        main_grp = next(g for g in skills_after["groups"] if g["index"] == skills_after["mainSocketGroup"])
        gem_names = [g.get("name") or g.get("nameSpec") for g in main_grp["gems"]]
        assert gem_names == ["Arc", "Controlled Destruction"]
        assert main_grp["label"] == "Replaced Arc"
    finally:
        undo_build(pob_worker)
        history_of(pob_worker).redo.clear()
    assert _capture_semantic_state(pob_worker) == before_state

    # 3. unequip_item (what-if, 0-residue)
    uneq = unequip_item(pob_worker, "Ring 1", apply=False)
    assert uneq["applied"] is False
    assert uneq["detail"]["slotName"] == "Ring 1"
    assert uneq["after"]["life"] < uneq["before"]["life"]
    assert _capture_semantic_state(pob_worker) == before_state

    # 4. create_new_build (what-if, 0-residue)
    nb = create_new_build(
        pob_worker,
        class_name="Templar",
        ascendancy="Hierophant",
        level=92,
        apply=False,
    )
    assert nb["applied"] is False
    assert nb["buildInfo"] is not None
    assert nb["buildInfo"] == nb["detail"]["buildInfo"]
    assert nb["detail"]["level"] == 92
    assert nb["detail"]["class"]["className"] == "Templar"
    assert nb["detail"]["class"]["ascendClassName"] == "Hierophant"
    assert _capture_semantic_state(pob_worker) == before_state


def test_expanded_run_batch_ops_and_return_sections(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)

    res = run_batch(
        pob_worker,
        [
            {"op": "set_level", "level": 95},
            {"op": "add_group", "label": "Temp Arc", "gems": ["Arc:20:20", "Spell Echo Support:20:20"]},
            {"op": "switch_main", "skillGroup": "Temp Arc"},
            {"op": "set_group", "group": "Temp Arc", "includeInFullDPS": True},
            {"op": "replace_group", "group": "Temp Arc", "gems": ["Arc:21:20", "Controlled Destruction Support:20:20"]},
            {"op": "item_edit", "slot": "Ring 1", "addMods": ["+50 to maximum Life"]},
            {"op": "item_db", "query": "Mageblood", "slot": "Belt"},
            {"op": "unequip", "slot": "Flask 1"},
            {"op": "config", "set": {"useFrenzyCharges": True}},
            {"op": "tree_reset"},
        ],
        apply=False,
        return_sections=["summary", "calc:Average Hit", "export"],
    )
    assert res["applied"] is False
    assert len(res["detail"]["steps"]) == 10
    assert "returns" in res
    assert res["returns"]["summary"]["buildInfo"]["level"] == 95
    assert res["returns"]["calc:Average Hit"]["totalMatches"] >= 1
    assert isinstance(res["returns"]["export"], str) and len(res["returns"]["export"]) > 20

    # Strict 0-residue after what-if batch with return_sections
    assert _capture_semantic_state(pob_worker) == before_state


def test_run_sweep_real_engine_with_scenario_calcs_and_fault_isolation(pob_worker: BridgeWorker) -> None:
    before_state = _capture_semantic_state(pob_worker)
    before_undo = list(history_of(pob_worker).undo)
    before_redo = list(history_of(pob_worker).redo)
    before_last_good = pob_worker.last_good_code

    sweep_res = run_sweep(
        pob_worker,
        [
            {
                "name": "Arc 2L",
                "ops": [
                    {
                        "op": "replace_group",
                        "group": "main",
                        "gems": ["Arc:20:20", "Controlled Destruction Support:20:20"],
                    }
                ],
            },
            {
                "name": "Bad Empty Amulet Edit",
                "ops": [
                    {"op": "item_edit", "slot": "Amulet", "addMods": ["+50 to maximum Life"]},
                ],
            },
            {
                "name": "Fireball + Controlled Destruction",
                "ops": [
                    {"op": "skill", "group": "main", "addGem": "Controlled Destruction Support", "level": 20, "quality": 20},
                ],
            },
        ],
        scenario="Pinnacle",
        calc_keywords=["Average Hit", "Mana"],
    )

    assert sweep_res["succeeded"] == 2
    assert sweep_res["failed"] == 1
    assert sweep_res["scenario"] == "Pinnacle"

    base_dps = float(sweep_res["baseline"]["summary"]["primaryDps"])
    v_arc, v_bad, v_fb_cd = sweep_res["variants"]

    assert v_arc["ok"] is True
    assert float(v_arc["summary"]["primaryDps"]) > 0
    assert v_arc["summary"].get("eleMaxHit") is not None
    assert "Average Hit" in v_arc["calcs"] and len(v_arc["calcs"]["Average Hit"]) >= 1

    assert v_bad["ok"] is False
    assert "Amulet" in v_bad["error"]

    assert v_fb_cd["ok"] is True
    assert float(v_fb_cd["summary"]["primaryDps"]) > base_dps

    # Strict 0-residue & stack preservation check
    assert _capture_semantic_state(pob_worker) == before_state
    assert history_of(pob_worker).undo == before_undo
    assert history_of(pob_worker).redo == before_redo
    assert pob_worker.last_good_code == before_last_good


# ---------------------------------------------------------------------------
# Pure unit tests for Vaal gem defaults, Config list/alias matching,
# DaemonRequestError classification, batch/sweep keep & guards
# ---------------------------------------------------------------------------


def test_vaal_vs_non_vaal_add_gem_default_skill_flags() -> None:
    """Vaal gems default to enableGlobal1=False (Vaal burst OFF) & enableGlobal2=True (base ON); non-Vaal gets 0 set_gem RPCs."""
    from pob_cli.mutators import _add_gem_with_default_skill_flags, _is_vaal_gem_entry, add_skill_gem

    assert _is_vaal_gem_entry({"skillId": "VaalHaste"}) is True
    assert _is_vaal_gem_entry({"skillId": "Vaal"}) is False
    assert _is_vaal_gem_entry({"name": "Vaal Discipline"}) is True
    assert _is_vaal_gem_entry({"nameZh": "瓦尔·迅捷"}) is True
    assert _is_vaal_gem_entry({"name": "Anomalous Vaal Grace"}) is True
    assert _is_vaal_gem_entry({"name": "Determination"}, fallback_name="Determination") is False

    # 1. _add_gem_with_default_skill_flags on Vaal gem calls set_gem(enableGlobal1=False, enableGlobal2=True)
    w = _FakeBridgeWorker()
    res_vaal = _add_gem_with_default_skill_flags(
        w, {"group": 1, "nameSpec": "Vaal Discipline", "level": 20, "quality": 20, "enabled": True}
    )
    assert res_vaal["gem"]["enableGlobal1"] is False
    assert res_vaal["gem"]["enableGlobal2"] is True
    set_gem_calls = [p for m, p in w.calls if m == "set_gem"]
    assert set_gem_calls == [
        {"group": 1, "index": 3, "enableGlobal1": False, "enableGlobal2": True},
    ]

    # 2. _add_gem_with_default_skill_flags on non-Vaal gem performs 0 set_gem RPCs
    w2 = _FakeBridgeWorker()
    res_base = _add_gem_with_default_skill_flags(
        w2, {"group": 1, "nameSpec": "Determination", "level": 20, "quality": 20, "enabled": True}
    )
    assert res_base["gem"]["nameSpec"] == "Determination"
    assert [m for m, _ in w2.calls if m == "set_gem"] == []

    # 3. Missing gem index on Vaal gem raises RuntimeError (daemon/engine contract fault)
    class _BrokenAddGemWorker:
        def call(self, method: str, params: dict) -> dict:
            assert method == "add_gem"
            return {"gem": {"nameSpec": "Vaal Haste"}}

    with pytest.raises(RuntimeError, match="add_gem did not return the added gem index"):
        _add_gem_with_default_skill_flags(_BrokenAddGemWorker(), {"group": 1, "nameSpec": "Vaal Haste"})  # type: ignore[arg-type]

    # 4. add_skill_gem, add_skill_group, replace_skill_group, and batch skill.addGem apply Vaal defaults ONLY to Vaal gems
    w3 = _FakeBridgeWorker()
    add_skill_gem(w3, group_selector="main", gem_name="Vaal Haste", apply=True)
    add_skill_gem(w3, group_selector="main", gem_name="Grace", apply=True)
    set_calls_3 = [p for m, p in w3.calls if m == "set_gem"]
    assert len(set_calls_3) == 1
    assert set_calls_3[0]["enableGlobal1"] is False and set_calls_3[0]["enableGlobal2"] is True

    w4 = _FakeBridgeWorker()
    add_skill_group(w4, gems=["Vaal Grace:20:20", "Determination:20:20", "瓦尔迅捷:20:20"], apply=True)
    set_calls_4 = [p for m, p in w4.calls if m == "set_gem"]
    assert len(set_calls_4) == 2
    assert all(c["enableGlobal1"] is False and c["enableGlobal2"] is True for c in set_calls_4)
    assert [c["index"] for c in set_calls_4] == [1, 3]

    w5 = _FakeBridgeWorker()
    replace_skill_group(w5, "main", ["Arc:20:20", "Vaal Righteous Fire:20:20"], apply=True)
    set_calls_5 = [p for m, p in w5.calls if m == "set_gem"]
    assert set_calls_5 == [{"group": 1, "index": 2, "enableGlobal1": False, "enableGlobal2": True}]

    w6 = _FakeBridgeWorker()
    run_batch(
        w6,
        [
            {"op": "skill", "group": "main", "addGem": "Vaal Molten Shell"},
            {"op": "skill", "group": "main", "addGem": "Enduring Cry"},
        ],
        apply=True,
    )
    set_calls_6 = [p for m, p in w6.calls if m == "set_gem"]
    assert len(set_calls_6) == 1
    assert set_calls_6[0]["enableGlobal1"] is False and set_calls_6[0]["enableGlobal2"] is True


def test_config_list_matching_boss_aliases_resistance_penalty_and_r12_guard() -> None:
    """Verify enemyIsBoss=No/Shaper/labelZh, resistancePenalty=None->0, reset/default/nil, and R12 presetBossSkills guard."""
    from pob_cli.mutators import _apply_config_in_place

    class _CfgWorker:
        def __init__(self, preset_boss_skill_val: object = "None") -> None:
            self.calls: list[tuple[str, dict]] = []
            self.preset_boss_skill_val = preset_boss_skill_val

        def call(self, method: str, params: dict) -> dict:
            self.calls.append((method, dict(params)))
            if method == "list_config":
                return {
                    "sections": [
                        {
                            "items": [
                                {
                                    "var": "enemyIsBoss",
                                    "type": "list",
                                    "list": [
                                        {"val": "None", "label": "No", "labelZh": "否"},
                                        {"val": "Boss", "label": "Standard Boss", "labelZh": "普通首领"},
                                        {"val": "Pinnacle", "label": "Guardian/Pinnacle Boss", "labelZh": "^xFFFFFF终局首领"},
                                        {"val": "Uber", "label": "Uber Pinnacle Boss", "labelZh": "超级终局首领"},
                                    ],
                                },
                                {
                                    "var": "resistancePenalty",
                                    "type": "list",
                                    "list": [
                                        {"val": 0, "label": "None"},
                                        {"val": -30, "label": "Act 5 (-30%)"},
                                        {"val": -60, "label": "Act 10 (-60%)"},
                                    ],
                                },
                                {
                                    "var": "deliriousPercentage",
                                    "type": "list",
                                    "list": [
                                        {"val": 0, "label": "None"},
                                        {"val": 100, "label": "100%"},
                                    ],
                                },
                                {
                                    "var": "bandit",
                                    "type": "list",
                                    "list": [{"val": "None", "label": "Kill all"}],
                                },
                                {
                                    "var": "presetBossSkills",
                                    "type": "list",
                                    "value": self.preset_boss_skill_val,
                                    "list": [
                                        {"val": "None", "label": "None"},
                                        {"val": "Shaper Slam", "label": "Shaper Slam"},
                                    ],
                                },
                                {"var": "usePowerCharges", "type": "check"},
                            ]
                        }
                    ]
                }
            if method in ("set_config", "reset_config"):
                return {"ok": True}
            raise AssertionError(f"Unexpected call {method}")

    # 1. presetBossSkills="None" must NOT trigger R12 re-apply
    w = _CfgWorker(preset_boss_skill_val="None")
    _apply_config_in_place(
        w,
        [
            ("enemyIsBoss", "No"),
            ("enemyIsBoss", "None"),
            ("enemyIsBoss", "Shaper"),
            ("enemyIsBoss", "Uber Atziri"),
            ("enemyIsBoss", "终局首领"),
            ("resistancePenalty", "None"),
            ("deliriousPercentage", "None"),
            ("resistancePenalty", "reset"),
            ("resistancePenalty", "default"),
            ("resistancePenalty", "nil"),
            ("bandit", "Kill all"),
            ("usePowerCharges", "none"),
        ],
    )
    edits = [(m, p) for m, p in w.calls if m != "list_config"]
    assert edits == [
        ("set_config", {"var": "enemyIsBoss", "value": "None"}),
        ("set_config", {"var": "enemyIsBoss", "value": "None"}),
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
        ("set_config", {"var": "enemyIsBoss", "value": "Boss"}),
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
        ("set_config", {"var": "resistancePenalty", "value": 0}),
        ("set_config", {"var": "deliriousPercentage", "value": 0}),
        ("reset_config", {"var": "resistancePenalty"}),
        ("reset_config", {"var": "resistancePenalty"}),
        ("reset_config", {"var": "resistancePenalty"}),
        ("set_config", {"var": "bandit", "value": "None"}),
        ("reset_config", {"var": "usePowerCharges"}),
    ]

    # 2. Active presetBossSkills ("Shaper Slam") MUST still trigger R12 re-apply
    w2 = _CfgWorker(preset_boss_skill_val="Shaper Slam")
    _apply_config_in_place(w2, [("enemyIsBoss", "Pinnacle")])
    edits2 = [(m, p) for m, p in w2.calls if m != "list_config"]
    assert edits2 == [
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
        ("set_config", {"var": "presetBossSkills", "value": "Shaper Slam"}),
    ]


def test_daemon_request_error_guards_and_rpc_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify DaemonRequestError classification across mutators, snapshot, importers, batch/sweep, and daemon RPC."""
    import http.client
    import os
    import sys
    import threading
    import types
    from http.server import ThreadingHTTPServer

    import pob_cli.analyzer as analyzer_mod
    import pob_cli.comparator as comp_mod
    import pob_cli.daemon as daemon_mod
    import pob_cli.importers as importers_mod
    from pob_cli.bridge_client import BridgeError
    from pob_cli.errors import DaemonRequestError
    from pob_cli.mutators import _apply_anoint_in_place, _apply_edit_item_mods_in_place
    from pob_cli.snapshot import redo_build

    assert issubclass(DaemonRequestError, RuntimeError)
    assert daemon_mod.DaemonRequestError is DaemonRequestError

    w = _FakeBridgeWorker()

    # 1. Empty slot in _apply_edit_item_mods_in_place, edit_item_mods, and _apply_anoint_in_place -> DaemonRequestError
    with pytest.raises(DaemonRequestError, match="has no equipped item to edit"):
        _apply_edit_item_mods_in_place(w, slot_name="Ring 1", add_mods=["+10 to maximum Life"])
    with pytest.raises(DaemonRequestError, match="has no equipped item to edit"):
        edit_item_mods(w, slot_name="Ring 1", add_mods=["+10 to maximum Life"])
    with pytest.raises(DaemonRequestError, match="has no equipped item to anoint"):
        _apply_anoint_in_place(w, slot_name="Ring 1", notable_query="Beef")

    # 2. Empty undo/redo stacks -> DaemonRequestError
    with pytest.raises(DaemonRequestError, match="Undo stack is empty"):
        undo_build(w)
    with pytest.raises(DaemonRequestError, match="Redo stack is empty"):
        redo_build(w)

    # 3. Missing raw/notable in batch ops -> ValueError (NOT KeyError!)
    with pytest.raises(ValueError, match="requires non-empty 'raw'"):
        run_batch(w, [{"op": "item"}], apply=False)
    with pytest.raises(ValueError, match="requires non-empty 'notable'"):
        run_batch(w, [{"op": "anoint", "slot": "Amulet"}], apply=False)

    # 4. batch audit/audit:full/audit:attribution & keep propagation + sweep keep merging
    seen_calls: dict[str, dict] = {}

    def _fake_audit(_w, keep=None, attribution=True):
        seen_calls["audit"] = {"keep": keep, "attribution": attribution}
        return {"ok": True}

    def _fake_matrix(_w, scenarios=None, *, full=False, keep=None):
        seen_calls["matrix"] = {"scenarios": scenarios, "keep": keep}
        return {"ok": True}

    def _fake_apply_scenario(_w, plan, keep=None, **_kw):
        seen_calls["sweep_scenario"] = {"plan": plan, "keep": keep}
        return {"base": plan["base"]}

    monkeypatch.setattr(analyzer_mod, "audit_build", _fake_audit)
    monkeypatch.setattr(comp_mod, "run_matrix", _fake_matrix)
    monkeypatch.setattr(comp_mod, "_apply_matrix_scenario_in_place", _fake_apply_scenario)

    r_b1 = run_batch(
        w,
        [{"op": "config", "set": {"usePowerCharges": True}}],
        return_sections=["audit", "matrix:Original;Pinnacle"],
        keep=["conditionStationary"],
        apply=False,
    )
    assert "audit" in r_b1["returns"]
    assert seen_calls["audit"] == {"keep": ["conditionStationary"], "attribution": False}
    assert seen_calls["matrix"] == {"scenarios": ["Original", "Pinnacle"], "keep": ["conditionStationary"]}

    r_b2 = run_batch(
        w,
        [{"op": "config", "set": {"usePowerCharges": True}}],
        return_sections=["audit:full"],
        keep="Flask 1,conditionStationary",
        apply=False,
    )
    assert "audit" in r_b2["returns"]
    assert seen_calls["audit"] == {"keep": ["Flask 1", "conditionStationary"], "attribution": True}

    with pytest.raises(ValueError, match="Unsupported audit return modifier"):
        run_batch(
            w,
            [{"op": "config", "set": {"usePowerCharges": True}}],
            return_sections=["audit:bogus"],
            apply=False,
        )

    run_sweep(
        w,
        [{"name": "V1", "ops": []}],
        scenario={"name": "PinKeep", "base": "Pinnacle", "keep": ["Flask 1", "conditionStationary"]},
        keep=["conditionStationary", "Molten Shell"],
    )
    assert seen_calls["sweep_scenario"]["keep"] == ["conditionStationary", "Molten Shell", "Flask 1"]

    # 5. importers.py invalid league slug: DaemonRequestError when no fetch_errs, RuntimeError when fetch_errs non-empty
    monkeypatch.setattr(importers_mod, "_fetch_poe_ninja_build_index_state", lambda **_kw: {"leagueBuilds": []})
    monkeypatch.setattr(importers_mod, "_fetch_poe_ninja_index_state", lambda **_kw: {"buildLeagues": []})
    with pytest.raises(DaemonRequestError, match="Could not find league slug"):
        importers_mod.fetch_poe_ninja_character_code("https://poe.ninja/poe1/builds/nosuchleague/character/Acc-1234/Char")

    monkeypatch.setattr(
        importers_mod,
        "_fetch_poe_ninja_build_index_state",
        lambda **_kw: (_ for _ in ()).throw(OSError("upstream down")),
    )
    with pytest.raises(RuntimeError) as exc_info:
        importers_mod.fetch_poe_ninja_character_code("https://poe.ninja/poe1/builds/nosuchleague/character/Acc-1234/Char")
    assert type(exc_info.value) is RuntimeError

    # 6. DaemonManager + RpcHandler errorKind + export pobDir + timeless_list + call_daemon _rpc_result
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"

    class _StubWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.last_good_code: str | None = None
            self.hello_info: dict[str, object] = {}
            self.lock = threading.RLock()

        def is_alive(self) -> bool:
            return True

        def ensure_alive(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def call(self, method: str, params: dict | None = None) -> dict:
            if method == "export_code":
                return {"code": "EXPORTED_CODE"}
            raise BridgeError("simulated engine crash")

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok-secret", runtime_dir=rt, offline=True, skip_update_check=True)

    # export includes pobDir
    exp = mgr.dispatch("export", {})
    assert exp["code"] == "EXPORTED_CODE"
    assert exp["pobDir"] == str(eng)

    # timeless_list delegates lazily to pob_cli.timeless.describe_worker
    fake_timeless = types.ModuleType("pob_cli.timeless")
    fake_timeless.describe_worker = lambda worker, pob_dir=None, runtime_dir=None: {  # type: ignore[attr-defined]
        "slot": worker.slot_name,
        "pobDir": str(pob_dir),
        "runtimeDir": str(runtime_dir),
    }
    monkeypatch.setitem(sys.modules, "pob_cli.timeless", fake_timeless)
    tl_res = mgr.dispatch("timeless_list", {})
    assert tl_res == {"slot": "main", "pobDir": str(eng), "runtimeDir": str(rt)}

    # Missing flask_index / active / gem raise ValueError (not KeyError)
    with pytest.raises(ValueError, match="flask_index"):
        mgr.dispatch("item_flask", {})
    with pytest.raises(ValueError, match="active"):
        mgr.dispatch("item_flask", {"flask_index": 1})
    with pytest.raises(ValueError, match="gem"):
        mgr.dispatch("skill_rm", {})

    # borrow empty ref raises DaemonRequestError with dynamic slot name
    with pytest.raises(DaemonRequestError, match=r"No reference build loaded in 'boss_ref' slot"):
        mgr.dispatch("borrow", {"ref_target": "boss_ref"})

    # HTTP RpcHandler tags errorKind="request" vs "daemon", and call_daemon preserves classification
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon_mod.make_handler(mgr))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "daemon.json").write_text(
            json.dumps({"pid": os.getpid(), "port": port, "token": "tok-secret"}),
            encoding="utf-8",
        )

        # Request error -> errorKind="request" -> call_daemon raises DaemonRequestError
        with pytest.raises(DaemonRequestError, match="No reference build loaded"):
            daemon_mod.call_daemon("borrow", {"ref_target": "boss_ref"}, auto_start=False, runtime_dir=rt)

        # BridgeError -> errorKind="daemon" -> call_daemon raises plain RuntimeError (NOT DaemonRequestError)
        with pytest.raises(RuntimeError, match="simulated engine crash") as bridge_exc:
            daemon_mod.call_daemon(
                "load",
                {"source": importers_mod.xml_to_share_code("<PathOfBuilding/>")},
                auto_start=False,
                runtime_dir=rt,
            )
        assert type(bridge_exc.value) is RuntimeError

        # Negative Content-Length -> HTTP 400 with errorKind="request"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        conn.request("POST", "/rpc", body=b"", headers={"X-PoB-Token": "tok-secret", "Content-Length": "-5"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 400
        assert payload["errorKind"] == "request"
    finally:
        server.shutdown()
        server.server_close()
