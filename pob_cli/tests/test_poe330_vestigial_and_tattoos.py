"""Tests for PoE1 3.30 Vestigial unique mods, Tattoo/Runegraft data queries, parser fixes, and numeric tree ID lookup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli.cli_data import cmd_data
from pob_cli.cli_data_render import _render_single_data_markdown
from pob_cli.game_data import (
    SCHEMA_VERSION,
    _create_schema,
    _insert_tattoos_table,
    _parse_tattoos,
    _ZhDictionary,
    ensure_gamedata_index,
    ensure_treedata_index,
    query_local_data,
    query_tattoo_data,
    query_tree_data,
)
from pob_cli.item_inspect import inspect_item
from pob_cli.trade_item import (
    NON_CONVERTIBLE_KINDS,
    build_item_text,
    parse_item_text,
)
from pob_cli.updater import get_runtime_dir

# PoB-crafted rare gloves modeled after build YlpaJJQx2xtD (2 prefixes + 3 suffixes + Crafted: true header)
_YLPA_CRAFTED_GLOVES = """Rarity: RARE
Soul Grip
Fingerless Silk Gloves
Crafted: true
Prefix: {range:0.5}IncreasedLife7
Prefix: {range:0.5}LocalIncreasedEnergyShieldPercent5
Suffix: {range:0.5}FireResistance6
Suffix: {range:0.5}ColdResistance6
Suffix: {range:0.5}LightningResistance6
Quality: 20
Sockets: B-B-B-B
LevelReq: 70
Implicits: 1
{tags:caster,damage}{range:0.5}16% increased Spell Damage
+85 to maximum Life
92% increased Energy Shield
+45% to Fire Resistance
+44% to Cold Resistance
+43% to Lightning Resistance"""

_CLOAK_OF_FLAME_WITH_DIVIDER_AND_VESTIGIAL = """Rarity: UNIQUE
Cloak of Flame
Scholar's Robe
Energy Shield: 35
Sockets: B-B-B-R-R-G
LevelReq: 18
Implicits: 1
{vestigial}+200 to maximum Life
+45% to Fire Resistance
48% increased Ignite Duration on Enemies
40% of Physical Damage taken as Fire Damage
============================
Vestigial Item"""


@pytest.fixture(scope="module")
def isolated_engine_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build SQLite game & tree indexes once in an isolated tmp directory without touching shared runtime cache."""
    cache_dir = tmp_path_factory.mktemp("pob_l4_isolated_cache")
    engine_dir = get_runtime_dir() / "engine"
    if not (engine_dir / "PoE1" / "Data" / "TattooPassives.lua").is_file():
        pytest.skip("PoE1 3.30 runtime engine data not present in current environment")

    import os

    prev_cache = os.environ.get("POB_CACHE_DIR")
    os.environ["POB_CACHE_DIR"] = str(cache_dir)
    try:
        db_path = ensure_gamedata_index(pob_dir=engine_dir, runtime_dir=cache_dir)
        tree_db_path, _ = ensure_treedata_index(pob_dir=engine_dir, runtime_dir=cache_dir)
    finally:
        if prev_cache is None:
            os.environ.pop("POB_CACHE_DIR", None)
        else:
            os.environ["POB_CACHE_DIR"] = prev_cache

    assert db_path.parent == cache_dir
    assert tree_db_path.parent == cache_dir
    return {"engine_dir": engine_dir, "cache_dir": cache_dir, "db_path": db_path}


# ---------------------------------------------------------------------------
# 1. Item Parser Fixes: Crafted: true, ==== divider, {vestigial} round-trip
# ---------------------------------------------------------------------------


def test_parse_crafted_header_and_author_divider_and_vestigial_roundtrip() -> None:
    assert SCHEMA_VERSION >= 13
    assert "vestigial" in NON_CONVERTIBLE_KINDS

    gloves = parse_item_text(_YLPA_CRAFTED_GLOVES)
    assert gloves.rarity == "RARE"
    assert gloves.base_type == "Fingerless Silk Gloves"
    assert all("crafted:" not in m.raw.lower() for m in gloves.mods)
    assert len(gloves.mods) == 6  # 1 implicit + 2 prefixes + 3 suffixes

    cloak = parse_item_text(_CLOAK_OF_FLAME_WITH_DIVIDER_AND_VESTIGIAL)
    assert cloak.rarity == "UNIQUE"
    assert cloak.name == "Cloak of Flame"
    assert "Vestigial" in cloak.influences
    assert all("===" not in m.raw for m in cloak.mods)
    vest_mods = [m for m in cloak.mods if m.kind == "vestigial"]
    assert len(vest_mods) == 1
    assert vest_mods[0].raw == "+200 to maximum Life"
    assert vest_mods[0].tags == ("vestigial",)

    # Round-trip via build_item_text -> parse_item_text
    raw_trade_payload: dict[str, Any] = {
        "rarity": "UNIQUE",
        "name": "Cloak of Flame",
        "baseType": "Scholar's Robe",
        "vestigial": True,
        "vestigialMods": ["+200 to maximum Life"],
        "explicitMods": [
            "+45% to Fire Resistance",
            "40% of Physical Damage taken as Fire Damage",
        ],
    }
    built_txt = build_item_text(raw_trade_payload)
    assert "{vestigial}+200 to maximum Life" in built_txt
    assert "Vestigial Item" in built_txt

    reparsed = parse_item_text(built_txt)
    assert "Vestigial" in reparsed.influences
    reparsed_vest = [m for m in reparsed.mods if m.kind == "vestigial"]
    assert len(reparsed_vest) == 1
    assert reparsed_vest[0].raw == "+200 to maximum Life"


def test_inspect_item_skips_pob_crafted_true_header(
    isolated_engine_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = isolated_engine_env["cache_dir"]
    engine_dir = isolated_engine_env["engine_dir"]
    monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))

    res = inspect_item(
        _YLPA_CRAFTED_GLOVES,
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert res["unmatched"] == []
    assert res["certain"] is True
    assert res["open"] == {"prefix": 1, "suffix": 0}


# ---------------------------------------------------------------------------
# 2. 3.30 Vestigial Unique Mod Indexing & pob data unique / mod Rendering
# ---------------------------------------------------------------------------


def test_vestigial_mod_indexing_and_donor_unique_notes(
    isolated_engine_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = isolated_engine_env["cache_dir"]
    engine_dir = isolated_engine_env["engine_dir"]
    monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))

    # +200 to maximum Life -> DivergentIncreasedLifeUniqueBodyStr1, donor Kaom's Heart, Body Armour only
    mod_res = query_local_data(
        "mod",
        "+200 to maximum Life",
        slot="Body Armour",
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    vest_hits = [r for r in mod_res["results"] if r["id"] == "DivergentIncreasedLifeUniqueBodyStr1"]
    assert len(vest_hits) == 1
    kaom_mod = vest_hits[0]
    assert kaom_mod["source"] == "Vestigial"
    assert kaom_mod["vestigialDonors"] == ["Kaom's Heart"]
    assert kaom_mod["applicable_types"] == ["body_armour"]

    # Body-restricted Vestigial mod must not match Helmet slot
    helmet_res = query_local_data(
        "mod",
        "+200 to maximum Life",
        slot="Helmet",
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert all(r["id"] != "DivergentIncreasedLifeUniqueBodyStr1" for r in helmet_res["results"])

    # Markdown rendering for Vestigial mod includes donor attribution
    mod_md = _render_single_data_markdown(mod_res, "mod", "+200 to maximum Life")
    assert "Kaom's Heart" in mod_md

    # Donor uniques carry vestigial_donor_mods + Vestigial slot note in Markdown
    cloak_res = query_local_data("unique", "Cloak of Flame", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert cloak_res["count"] >= 1
    cloak_u = next(r for r in cloak_res["results"] if r["name"] == "Cloak of Flame")
    assert any("Physical Damage taken as Fire Damage" in s for s in cloak_u["vestigial_donor_mods"])

    cloak_md = _render_single_data_markdown(cloak_res, "unique", "Cloak of Flame")
    assert "Vestigial Donor Mods" in cloak_md
    assert "3.30 Vestigial Slot Note" in cloak_md
    assert "{vestigial}" in cloak_md

    kaom_res = query_local_data("unique", "Kaom's Heart", pob_dir=engine_dir, runtime_dir=cache_dir)
    kaom_u = next(r for r in kaom_res["results"] if r["name"] == "Kaom's Heart")
    assert "+200 to maximum Life" in kaom_u["vestigial_donor_mods"]


# ---------------------------------------------------------------------------
# 3. 3.30 Tattoos & Runegrafts Offline Query (Bilingual + Legacy Filtering)
# ---------------------------------------------------------------------------


def test_tattoo_and_runegraft_queries_bilingual_and_legacy(
    isolated_engine_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = isolated_engine_env["cache_dir"]
    engine_dir = isolated_engine_env["engine_dir"]
    monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("POB_HOME", str(engine_dir))

    # Simplified Chinese 3.30 Tattoo name from Tatto.json (works with both 文身 and 纹身)
    zh_tat = query_local_data("tattoo", "努葛玛呼烈焰行者的纹身", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert zh_tat["count"] >= 1
    assert zh_tat["results"][0]["name"] == "Tattoo of the Ngamahu Firewalker"
    assert zh_tat["results"][0]["kind"] == "tattoo"
    assert zh_tat["results"][0]["zh_name"] == "努葛玛呼烈焰行者的文身"

    # Runegraft of the Bound exact lookup & --kind runegraft listing
    rg_bound = query_local_data("tattoo", "Runegraft of the Bound", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert rg_bound["count"] >= 1
    top_rg = rg_bound["results"][0]
    assert top_rg["name"] == "Runegraft of the Bound"
    assert top_rg["kind"] == "runegraft"
    assert top_rg["override_type"] == "AlternateMastery"
    assert top_rg["target_type"] == "Mastery"
    assert top_rg["zh_name"]
    assert all("limited to" not in s.lower() for s in top_rg["stats"])
    assert top_rg["limit_text"] == "Limited to 1 Runegraft of the Bound"

    rg_all = query_local_data(
        "tattoo",
        "",
        tattoo_kind="runegraft",
        limit=50,
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert rg_all["count"] == 32
    assert all(r["kind"] == "runegraft" for r in rg_all["results"])

    # Retired 3.22 tattoos (legacy=True) hidden by default, shown with include_legacy=True
    legacy_hidden = query_local_data(
        "tattoo",
        "Honoured Tattoo of the Dove",
        include_legacy=False,
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert legacy_hidden["count"] == 0

    legacy_shown = query_local_data(
        "tattoo",
        "Honoured Tattoo of the Dove",
        include_legacy=True,
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert legacy_shown["count"] == 1
    assert legacy_shown["results"][0]["legacy"] is True

    # CLI end-to-end check with --kind runegraft and Markdown rendering
    runner = CliRunner()
    cli_res = runner.invoke(cmd_data, ["tattoo", "Runegraft of the Bound", "--kind", "runegraft"])
    assert cli_res.exit_code == 0, cli_res.output
    assert "Runegraft of the Bound" in cli_res.output
    assert "Mastery" in cli_res.output


# ---------------------------------------------------------------------------
# 4. Passive Tree Exact Numeric NodeId Lookup (pob data tree 61308)
# ---------------------------------------------------------------------------


def test_tree_exact_numeric_node_id_lookup(
    isolated_engine_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = isolated_engine_env["cache_dir"]
    engine_dir = isolated_engine_env["engine_dir"]
    monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))

    res = query_tree_data("61308", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert res["count"] >= 1
    first = res["results"][0]
    assert first["id"] == 61308
    assert first["name"] == "Amplify"
    assert first["zhName"] == "增幅"


# ---------------------------------------------------------------------------
# 5. Self-contained synthetic unit test (0 external runtime dependency)
# ---------------------------------------------------------------------------


def test_synthetic_tattoo_and_zh_tatto_json_offline(tmp_path: Path) -> None:
    poe1_dir = tmp_path / "PoE1"
    data_dir = poe1_dir / "Data"
    data_dir.mkdir(parents=True)
    zh_dir = tmp_path / "zh-rCN"
    zh_dir.mkdir(parents=True)

    (zh_dir / "Tatto.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Tattoo of the Ngamahu Firewalker": "努葛玛呼烈焰行者的文身",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (zh_dir / "Z-S29.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Runegraft of the Bound": "束缚之符文嵌板",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "TattooPassives.lua").write_text(
        """return {
\t["nodes"] = {
\t\t["Runegraft of the Bound"] = {
\t\t\t["dn"] = "Runegraft of the Bound",
\t\t\t["id"] = "runegraft_5",
\t\t\t["overrideType"] = "AlternateMastery",
\t\t\t["targetType"] = "Mastery",
\t\t\t["targetValue"] = "",
\t\t\t["MinimumConnected"] = 0,
\t\t\t["MaximumConnected"] = 100,
\t\t\t["sd"] = {
\t\t\t\t[1] = "20% increased bonuses gained from Equipped Gloves",
\t\t\t\t[2] = "Limited to 1 Runegraft of the Bound",
\t\t\t},
\t\t},
\t\t["Tattoo of the Ngamahu Firewalker"] = {
\t\t\t["dn"] = "Tattoo of the Ngamahu Firewalker",
\t\t\t["id"] = "Ngamahu1",
\t\t\t["overrideType"] = "NgamahuTattoo",
\t\t\t["targetType"] = "Small Strength",
\t\t\t["targetValue"] = "",
\t\t\t["sd"] = { [1] = "+6% to Fire Resistance" },
\t\t},
\t\t["Honoured Tattoo of the Dove"] = {
\t\t\t["dn"] = "Honoured Tattoo of the Dove",
\t\t\t["id"] = "SpecialSmallNode1",
\t\t\t["legacy"] = true,
\t\t\t["overrideType"] = "UnknownTattoo",
\t\t\t["targetType"] = "Small Attribute",
\t\t\t["targetValue"] = "",
\t\t\t["sd"] = { [1] = "+4% to Chaos Resistance" },
\t\t},
\t},
}""",
        encoding="utf-8",
    )

    zh = _ZhDictionary.load(zh_dir)
    tattoos = _parse_tattoos(poe1_dir, zh)
    assert len(tattoos) == 3

    conn = sqlite3.connect(":memory:")
    try:
        _create_schema(conn)
        _insert_tattoos_table(conn, tattoos)

        active = query_tattoo_data(conn, "")
        assert [r["name"] for r in active] == [
            "Runegraft of the Bound",
            "Tattoo of the Ngamahu Firewalker",
        ]
        assert len(query_tattoo_data(conn, "", include_legacy=True)) == 3
        assert len(query_tattoo_data(conn, "", kind="runegraft")) == 1
        assert len(query_tattoo_data(conn, "烈焰行者的纹身", slot_filter="str")) == 1
        assert len(query_tattoo_data(conn, "", mod_filter="Fire Resistance")) == 1
    finally:
        conn.close()
