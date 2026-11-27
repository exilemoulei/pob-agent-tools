"""Offline & real-engine tests for Layer 1: tree override base visibility, Tattoo Slots summary, customMods bypass, and XML `<Override>` audit."""

from __future__ import annotations

import base64
import threading
import zlib
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli.analyzer.audit import audit_build
from pob_cli.analyzer.diagnose import diagnose_build
from pob_cli.analyzer.pitfalls_tattoos import (
    _index_tattoo_records,
    audit_illegal_tattoo_overrides,
    audit_tattoo_pitfalls,
    check_custom_mods_tattoo_bypass,
)
from pob_cli.analyzer.tree_helpers import apply_node_override, classify_base_target_type
from pob_cli.analyzer.tree_inspect import inspect_tree
from pob_cli.formatters.diagnose_md import format_diagnose_markdown
from pob_cli.formatters.tree_md import format_tree_markdown

REAL_ENGINE = find_runnable_engine()
HAS_REAL_POB = REAL_ENGINE is not None
EXAMPLE_CODE_PATH = Path(__file__).resolve().parents[2] / "examples" / "builds" / "dark_bargain_ci_skeleton_330_clean.code.txt"


def _sample_catalog() -> dict[str, dict[str, Any]]:
    return _index_tattoo_records(
        [
            {
                "id": "dex_flask_life",
                "name": "Tattoo of the Tawhoa Naturalist",
                "zh_name": "塔赫亚自然术士的文身",
                "kind": "tattoo",
                "target_type": "Small Dexterity",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "full_stats": ["8% increased Life Recovery from Flasks"],
            },
            {
                "id": "str_fire_res",
                "name": "Tattoo of the Ngamahu Firewalker",
                "zh_name": "努葛玛呼烈焰行者的文身",
                "kind": "tattoo",
                "target_type": "Small Strength",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "full_stats": ["+6% to Fire Resistance"],
            },
            {
                "id": "makanga_7",
                "name": "Tattoo of the Tawhoa Makanga",
                "zh_name": "塔赫亚玛坎加的文身",
                "kind": "tattoo",
                "target_type": "Small Dexterity",
                "target_value": "",
                "min_connected": 7,
                "max_connected": 100,
                "limit_text": None,
                "full_stats": ["+1% to maximum Chaos Resistance"],
            },
            {
                "id": "makanga_max1",
                "name": "Tattoo of the Ngamahu Makanga",
                "zh_name": "努葛玛呼玛坎加的文身",
                "kind": "tattoo",
                "target_type": "Small Strength",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 1,
                "limit_text": None,
                "full_stats": ["+1% to maximum Fire Resistance"],
            },
            {
                "id": "ks_tattoo_1",
                "name": "Journey Tattoo of the Soul",
                "zh_name": "灵魂之旅的文身",
                "kind": "tattoo",
                "target_type": "Keystone",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": "Limited to 1 Keystone Tattoo",
                "full_stats": ["Limited to 1 Keystone Tattoo"],
            },
            {
                "id": "ks_tattoo_2",
                "name": "Journey Tattoo of the Mind",
                "zh_name": "心智之旅的文身",
                "kind": "tattoo",
                "target_type": "Keystone",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": "Limited to 1 Keystone Tattoo",
                "full_stats": ["Limited to 1 Keystone Tattoo"],
            },
            {
                "id": "rg_treachery",
                "name": "Runegraft of Treachery",
                "zh_name": "背叛之符文嵌板",
                "kind": "runegraft",
                "target_type": "Mastery",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": "Limited to 1 Runegraft of Treachery",
                "full_stats": ["15% increased Reservation Efficiency of Skills", "Limited to 1 Runegraft of Treachery"],
            },
        ]
    )


class _FakeTreeInspectWorker:
    def __init__(self, tree_state: dict[str, Any], nodes_db: dict[str, Any]) -> None:
        self.lock = threading.RLock()
        self._ts = tree_state
        self._ndb = nodes_db

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "get_tree_state":
            return self._ts
        if method == "list_items":
            return {"items": [], "slots": []}
        if method == "node_hover":
            return {"id": (params or {}).get("id"), "cost": 0, "allocated": True}
        return {}

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_30", "nodes": self._ndb}


def test_apply_node_override_preserves_base_visibility_and_tree_md_slots() -> None:
    raw_int = {"id": 3656, "name": "Intelligence", "type": "Normal", "stats": ["+10 to Intelligence"], "linked": [1, 2]}
    raw_notable = {"id": 700, "name": "Agility", "type": "Notable", "stats": ["+30 to Dexterity"], "linked": [1, 2]}
    raw_hybrid = {"id": 701, "name": "Versatility", "type": "Normal", "stats": ["+10 to Strength and Intelligence"], "linked": [1]}
    assert classify_base_target_type(raw_int) == "Small Intelligence"
    assert classify_base_target_type(raw_notable) == "Notable: +30 to Dexterity"
    assert classify_base_target_type(raw_hybrid) == "Small Attribute"

    node = dict(raw_int)
    apply_node_override(
        node,
        {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker", "stats": ["+6% to Fire Resistance"]},
        raw_int,
    )
    assert node["name"] == "Tattoo of the Ngamahu Firewalker"
    assert node["baseNodeName"] == "Intelligence"
    assert node["baseTargetType"] == "Small Intelligence"

    nodes_db = {
        "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [10]},
        "10": {"id": 10, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 11]},
        "11": {"id": 11, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [10, 700]},
        "700": raw_notable,
        "3656": raw_int,
    }
    ts = {
        "treeVersion": "3_30",
        "className": "Ranger",
        "ascendClassName": "Pathfinder",
        "points": {"used": 4, "usedMax": 123, "ascUsed": 0, "ascMax": 8},
        "allocatedNodes": [1, 10, 11, 700, 3656],
        "overrides": {
            "10": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist", "stats": ["8% increased Life Recovery from Flasks"]},
        },
        "dynamicNodes": [],
        "sockets": [],
    }
    w = _FakeTreeInspectWorker(ts, nodes_db)
    summary = inspect_tree(w)  # type: ignore[arg-type]
    assert summary["tattooSlots"]["buildLoaded"] is True
    assert summary["tattooSlots"]["tattoosUsed"] == 1
    md = format_tree_markdown(summary)
    assert "Tattoo Slots" in md and "Small Str 1 free" in md and "Small Dex 0 free (1 tattooed)" in md
    assert "Small Int 1 free" in md and "+30 Notable 1 free" in md and "Used: 1/50" in md

    filtered = inspect_tree(w, search="Tawhoa")  # type: ignore[arg-type]
    assert filtered["matches"][0]["baseNodeName"] == "Dexterity"
    assert filtered["matches"][0]["baseTargetType"] == "Small Dexterity"
    f_md = format_tree_markdown(filtered, search_label="Tawhoa")
    assert "[base: Small Dexterity]" in f_md


def test_custom_mods_tattoo_bypass_detection() -> None:
    cfg = {
        "customMods": [
            {"title": "Normal", "text": "+10 to maximum Life", "enabled": True},
            {"title": "Disabled", "text": "# Tattoo of the Ngamahu Firewalker\n+6% to Fire Resistance", "enabled": False},
        ]
    }
    assert check_custom_mods_tattoo_bypass(cfg) == []

    for text in (
        "# Tattoo of the Tawhoa Naturalist\n8% increased Life Recovery from Flasks",
        "文身：+6% 火焰抗性",
        "纹身模拟 +3% 混沌抗性",
        "符文之結：15% 保留效能",
        "Runegraft of Treachery",
    ):
        gaps = check_custom_mods_tattoo_bypass({"customMods": [{"title": "Custom", "text": text, "enabled": True}]})
        assert len(gaps) == 1
        assert gaps[0]["severity"] == "HIGH"
        assert "[CUSTOM-MOD-TATTOO-BYPASS]" in gaps[0]["message"]
        assert "pob tree tattoo" in gaps[0]["message"]


def test_illegal_tattoo_static_xml_audit_rules() -> None:
    cat = _sample_catalog()
    nodes_db: dict[str, Any] = {
        "3656": {"id": 3656, "name": "Intelligence", "type": "Normal", "stats": ["+10 to Intelligence"], "linked": [1, 2]},
        "100": {"id": 100, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 2]},
        "101": {"id": 101, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1, 2]},
        "200": {"id": 200, "name": "Acrobatics", "type": "Keystone", "linked": [1]},
        "201": {"id": 201, "name": "Mind Over Matter", "type": "Keystone", "linked": [1]},
        "300": {"id": 300, "name": "Life Mastery", "type": "Mastery", "linked": [1]},
        "301": {"id": 301, "name": "Flask Mastery", "type": "Mastery", "linked": [1]},
        "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [100, 101]},
        "2": {"id": 2, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [100, 101]},
    }
    # 1) Cross-attribute, cluster expansionSkill, conquered conflict, min_connected, max_connected, duplicate Keystone limit
    ts = {
        "allocatedNodes": [1, 2, 3656, 100, 101, 200, 201, 300, 301, 9001],
        "dynamicNodes": [{"id": 9001, "name": "Dexterity", "type": "Normal", "expansionSkill": True}],
        "overrides": {
            "3656": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
            "9001": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
            "100": {"why": "tattoo", "name": "Tattoo of the Tawhoa Makanga"},
            "101": {"why": "tattoo", "name": "Tattoo of the Ngamahu Makanga"},
            "200": {"why": "tattoo", "name": "Journey Tattoo of the Soul"},
            "201": {"why": "tattoo", "name": "Journey Tattoo of the Mind"},
            "300": {"why": "mastery", "name": "Runegraft of Treachery"},
            "301": {"why": "mastery", "name": "Life Mastery"},
        },
        "hashOverrides": {
            "2": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
        },
    }
    ts["overrides"]["2"] = {"why": "conquered", "name": "Price of Glory", "stats": []}

    findings, viols = audit_illegal_tattoo_overrides(ts, nodes_db, tattoo_catalog=cat)
    msgs = "\n".join(f["message"] for f in findings)
    assert all(f["severity"] == "HIGH" and "[ILLEGAL-TATTOO-NODE]" in f["message"] for f in findings)
    assert "Node 3656" in msgs and "Small Intelligence" in msgs and "Small Dexterity" in msgs
    assert "Node 9001" in msgs and "expansionSkill" in msgs
    assert "Node 2" in msgs and "Timeless Jewel" in msgs
    assert "Node 100" in msgs and "MinimumConnected >= 7" in msgs
    assert "Node 101" in msgs and "MaximumConnected <= 1" in msgs
    assert "Limited to 1 keystone tattoo" in msgs
    # Valid Runegraft on Mastery 300 and plain Mastery 301 must NOT be flagged
    assert "Node 300" not in msgs and "Node 301" not in msgs
    assert len(viols) >= 6

    # 2) >50 total tattoos violation (51 Strength tattoos + 1 valid Runegraft -> only 51 counted, triggers >50)
    many_db = {str(i): {"id": i, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1]} for i in range(1, 52)}
    many_db["900"] = {"id": 900, "name": "Life Mastery", "type": "Mastery", "linked": [1]}
    many_ts = {
        "allocatedNodes": [*range(1, 52), 900],
        "overrides": {
            **{str(i): {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker"} for i in range(1, 52)},
            "900": {"why": "mastery", "name": "Runegraft of Treachery"},
        },
    }
    f_over, _ = audit_illegal_tattoo_overrides(many_ts, many_db, tattoo_catalog=cat)
    assert any("exceeds PoE1 cap of 50" in f["message"] for f in f_over)

    # Exactly 50 tattoos + 1 Runegraft must NOT trigger >50 cap
    many_ts_ok = {
        "allocatedNodes": [*range(1, 51), 900],
        "overrides": {
            **{str(i): {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker"} for i in range(1, 51)},
            "900": {"why": "mastery", "name": "Runegraft of Treachery"},
        },
    }
    f_ok, _ = audit_illegal_tattoo_overrides(many_ts_ok, many_db, tattoo_catalog=cat)
    assert f_ok == []

    # audit_tattoo_pitfalls tolerates minimal fake worker without get_cached_tree_data
    class _MinimalFake:
        def call(self, _m: str, _p: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("no tree rpc")

    g_min, info_min = audit_tattoo_pitfalls(cfg_res={"customMods": []}, worker=_MinimalFake())
    assert g_min == [] and info_min is None


def test_adjacency_uses_allocated_count_and_skips_incomplete_db() -> None:
    cat = _sample_catalog()
    # Node 50 has 8 static links (1..8) all present in nodes_db, but only 2 allocated -> violates min_connected=7
    nodes_db: dict[str, Any] = {
        "50": {"id": 50, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 2, 3, 4, 5, 6, 7, 8]},
        "60": {"id": 60, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1, 2, 3]},
        **{str(i): {"id": i, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [50]} for i in range(1, 9)},
    }
    ts_min_fail = {
        "allocatedNodes": [50, 1, 2],
        "overrides": {"50": {"why": "tattoo", "name": "Tattoo of the Tawhoa Makanga"}},
    }
    f_min_fail, _ = audit_illegal_tattoo_overrides(ts_min_fail, nodes_db, tattoo_catalog=cat)
    assert len(f_min_fail) == 1 and "MinimumConnected >= 7" in f_min_fail[0]["message"] and "only 2" in f_min_fail[0]["message"]

    # When 7 of the 8 adjacent nodes are allocated -> min_connected=7 passes
    ts_min_ok = {
        "allocatedNodes": [50, 1, 2, 3, 4, 5, 6, 7],
        "overrides": {"50": {"why": "tattoo", "name": "Tattoo of the Tawhoa Makanga"}},
    }
    f_min_ok, _ = audit_illegal_tattoo_overrides(ts_min_ok, nodes_db, tattoo_catalog=cat)
    assert f_min_ok == []

    # Node 60 has 3 static links: 1 allocated -> max_connected=1 passes; 2 allocated -> rejected
    ts_max_ok = {
        "allocatedNodes": [60, 1],
        "overrides": {"60": {"why": "tattoo", "name": "Tattoo of the Ngamahu Makanga"}},
    }
    assert audit_illegal_tattoo_overrides(ts_max_ok, nodes_db, tattoo_catalog=cat)[0] == []
    ts_max_fail = {
        "allocatedNodes": [60, 1, 2],
        "overrides": {"60": {"why": "tattoo", "name": "Tattoo of the Ngamahu Makanga"}},
    }
    f_max_fail, _ = audit_illegal_tattoo_overrides(ts_max_fail, nodes_db, tattoo_catalog=cat)
    assert len(f_max_fail) == 1 and "MaximumConnected <= 1" in f_max_fail[0]["message"] and "2 allocated" in f_max_fail[0]["message"]

    # Incomplete nodes_db (linked ids 2 and 3 missing from nodes_db) -> skip adjacency check, do NOT falsely flag!
    partial_db = {"60": nodes_db["60"], "1": nodes_db["1"]}
    assert audit_illegal_tattoo_overrides(ts_max_fail, partial_db, tattoo_catalog=cat)[0] == []
    partial_db_min = {"50": nodes_db["50"], "1": nodes_db["1"]}
    assert audit_illegal_tattoo_overrides(ts_min_fail, partial_db_min, tattoo_catalog=cat)[0] == []


def test_diagnose_markdown_renders_tattoo_slots_summary() -> None:
    sample_diag = {
        "buildInfo": {"level": 95, "className": "Ranger", "ascendClassName": "Pathfinder", "points": {"used": 110, "usedMax": 123}},
        "mainGroup": {"index": 1, "displayLabel": "Toxic Rain", "slot": "Weapon 1", "gems": []},
        "tattooSlots": {
            "buildLoaded": True,
            "tattoosUsed": 2,
            "tattooLimit": 50,
            "runegraftsUsed": 1,
            "buckets": {
                "Small Strength": {"free": 1, "tattooed": 0},
                "Small Dexterity": {"free": 4, "tattooed": 2},
                "Small Intelligence": {"free": 2, "tattooed": 0},
                "Notable: +30 to Strength": {"free": 1, "tattooed": 0},
            },
        },
        "original": {},
        "dewateredPinnacle": {},
        "healthGaps": [],
    }
    for compact in (False, True):
        md = format_diagnose_markdown(sample_diag, compact=compact)
        assert "- **Tattoo Slots**: Small Str 1 free | Small Dex 4 free (2 tattooed) | Small Int 2 free | +30 Notable 1 free | Used: 2/50 (+1 Runegrafts)" in md


def test_legal_tattoo_override_produces_zero_audit_warnings() -> None:
    cat = _sample_catalog()
    nodes_db: dict[str, Any] = {
        "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [10, 11]},
        "10": {"id": 10, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 11]},
        "11": {"id": 11, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1, 10]},
        "300": {"id": 300, "name": "Life Mastery", "type": "Mastery", "linked": [10]},
    }
    ts = {
        "allocatedNodes": [1, 10, 11, 300],
        "overrides": {
            "10": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
            "11": {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker"},
            "300": {"why": "mastery", "name": "Runegraft of Treachery"},
        },
    }
    findings, viols = audit_illegal_tattoo_overrides(ts, nodes_db, tattoo_catalog=cat)
    assert findings == []
    assert viols == []
    gaps, info = audit_tattoo_pitfalls(cfg_res={"customMods": []}, tree_state=ts, nodes_db=nodes_db, tattoo_catalog=cat)
    assert gaps == []
    assert info is None


def test_catalog_cache_invalidates_and_retained_hash_overrides_counted(tmp_path: Path) -> None:
    from pob_cli.analyzer.pitfalls_tattoos import _CATALOG_CACHE, load_tattoo_catalog
    from pob_cli.analyzer.tattoo_slots import analyze_tattoo_slots

    # 1) Retained hashOverrides on unallocated nodes count toward global tattoosUsed, not per-bucket allocated totals
    nodes_db: dict[str, Any] = {
        "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [10]},
        "10": {"id": 10, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1]},
        "99": {"id": 99, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1]},
        "300": {"id": 300, "name": "Life Mastery", "type": "Mastery", "linked": [1]},
    }
    ts = {
        "allocatedNodes": [1, 10],
        "overrides": {
            "10": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
            "1": {"why": "conquered", "name": "Price of Glory"},
        },
        "hashOverrides": {
            "99": {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker"},
            "300": {"why": "mastery", "name": "Runegraft of Treachery"},
        },
    }
    slots = analyze_tattoo_slots(ts, nodes_db)
    assert slots["buckets"]["Small Dexterity"]["tattooed"] == 1
    assert slots["buckets"]["Small Strength"]["tattooed"] == 0
    assert slots["tattoosUsed"] == 2
    assert slots["runegraftsUsed"] == 1

    # 2) Catalog cache invalidates when TattooPassives.lua changes on disk
    lua_file = tmp_path / "PoE1" / "Data" / "TattooPassives.lua"
    lua_file.parent.mkdir(parents=True, exist_ok=True)
    lua_file.write_text(
        'return { nodes = { ["t1"] = { dn = "Tattoo of the First", sd = { "+6% to Fire Resistance" }, '
        'targetType = "Small Strength" } } }\n',
        encoding="utf-8",
    )

    class _W:
        def __init__(self, d: Path) -> None:
            self.pob_dir = d

    _CATALOG_CACHE.pop(str(tmp_path), None)
    cat1 = load_tattoo_catalog(_W(tmp_path))
    assert "tattoo of the first" in cat1
    assert "tattoo of the second" not in cat1

    lua_file.write_text(
        'return { nodes = { ["t2"] = { dn = "Tattoo of the Second", sd = { "+6% to Cold Resistance" }, '
        'targetType = "Small Dexterity", MinimumConnected = 7 } } }\n',
        encoding="utf-8",
    )
    cat2 = load_tattoo_catalog(_W(tmp_path))
    assert "tattoo of the second" in cat2
    assert "tattoo of the first" not in cat2


@pytest.mark.skipif(not HAS_REAL_POB, reason="No runnable real PoB engine found")
def test_real_pob_forged_override_xml_and_custom_mods_bypass(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker

    assert REAL_ENGINE is not None
    code = EXAMPLE_CODE_PATH.read_text(encoding="utf-8").strip()
    padded = code + "=" * (-len(code) % 4)
    xml = zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8")

    w = BridgeWorker("tattoo-audit-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        w.call("import_code", {"code": code, "mode": "replace"})
        # Clean build should have 0 illegal tattoo findings and populate tattooSlots in inspect_tree
        clean_tree = inspect_tree(w)
        assert clean_tree["tattooSlots"]["buildLoaded"] is True
        assert clean_tree["tattooSlots"]["tattoosUsed"] >= 15
        clean_audit = audit_build(w, attribution=False, include_envelope=False)
        assert not any("[ILLEGAL-TATTOO-NODE]" in h["message"] for h in clean_audit["healthIssues"])

        # Find an allocated Small Intelligence node in this build and forge a Small Dexterity tattoo onto it
        ts = w.call("get_tree_state", {})
        ndb = w.get_cached_tree_data(ts.get("treeVersion")).get("nodes", {})
        int_nid = next(
            int(nid)
            for nid in ts.get("allocatedNodes", [])
            if isinstance(ndb.get(str(int(nid))), dict)
            and ndb[str(int(nid))].get("type") == "Normal"
            and ndb[str(int(nid))].get("name") == "Intelligence"
        )
        # Replace or inject `<Override nodeId="int_nid" dn="Tattoo of the Tawhoa Naturalist"/>` and customMods bypass
        forged_xml = xml
        import re as _re

        forged_xml = _re.sub(rf'<Override\b[^>]*\bnodeId="{int_nid}"[^>]*/>', "", forged_xml)
        forged_xml = _re.sub(rf'<Override\b[^>]*\bnodeId="{int_nid}"[^>]*>.*?</Override>', "", forged_xml, flags=_re.DOTALL)
        forged_override = f'<Override nodeId="{int_nid}" dn="Tattoo of the Tawhoa Naturalist"/>'
        if "<Overrides>" in forged_xml:
            forged_xml = forged_xml.replace("<Overrides>", f"<Overrides>\n\t\t\t\t{forged_override}", 1)
        else:
            forged_xml = forged_xml.replace("</Spec>", f"\t\t\t<Overrides>\n\t\t\t\t{forged_override}\n\t\t\t</Overrides>\n\t\t</Spec>", 1)

        from pob_cli.mutators.config import modify_config

        forged_code = base64.urlsafe_b64encode(zlib.compress(forged_xml.encode("utf-8"))).decode("ascii")
        w.call("import_code", {"code": forged_code, "mode": "replace"})
        modify_config(
            w,
            assignments=[],
            append_custom_mods="# Tattoo of the Tawhoa Naturalist\n8% increased Life Recovery from Flasks",
            apply=True,
        )

        forged_audit = audit_build(w, attribution=False, include_envelope=False)
        audit_msgs = "\n".join(h["message"] for h in forged_audit["healthIssues"])
        assert "[ILLEGAL-TATTOO-NODE]" in audit_msgs
        assert f"Node {int_nid}" in audit_msgs
        assert "[CUSTOM-MOD-TATTOO-BYPASS]" in audit_msgs

        forged_diag = diagnose_build(w, no_rank=True)
        diag_msgs = "\n".join(h["message"] for h in forged_diag["healthGaps"])
        assert "[ILLEGAL-TATTOO-NODE]" in diag_msgs
        assert "[CUSTOM-MOD-TATTOO-BYPASS]" in diag_msgs
        assert isinstance(forged_diag.get("tattooSlots"), dict)
    finally:
        w.stop()
