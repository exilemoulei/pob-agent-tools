"""Tests for PoE1 3.30 Vestigial unique mods, Tattoo/Runegraft data queries, parser fixes, and numeric tree ID lookup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data, has_zh_data

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
    engine_dir = find_engine_data()
    if engine_dir is None or not has_zh_data(engine_dir) or not (engine_dir / "PoE1" / "Data" / "Vestigial.lua").is_file():
        pytest.skip("no engine with Vestigial.lua and Chinese data on this machine")

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
    assert SCHEMA_VERSION >= 15  # 15: Vestigial slots by donor, takes_vestigial, tattoo names
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

    # A listing as GGG's API sends it since 3.29 (and as PoB's ImportTab reads it): the flag sits on
    # the implicit, and the type line carries the "Vestigial" prefix.
    listing: dict[str, Any] = {
        "rarity": "UNIQUE",
        "name": "Cloak of Flame",
        "typeLine": "Vestigial Scholar's Robe",
        "baseType": "Scholar's Robe",
        "vestigial": True,
        "implicitMods": [{"description": "+200 to maximum Life", "flags": {"vestigial": True}}],
        "explicitMods": [
            "+45% to Fire Resistance",
            "40% of Physical Damage taken as Fire Damage",
        ],
    }
    built_txt = build_item_text(listing)
    assert "Implicits: 1\n{vestigial}+200 to maximum Life" in built_txt
    # PoB has no "Vestigial Item" state line; it would keep one as an unsupported modifier.
    assert "Vestigial Item" not in built_txt

    assert parse_item_text(built_txt).base_type == "Scholar's Robe"
    reparsed_vest = [m for m in parse_item_text(built_txt).mods if m.kind == "vestigial"]
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
    assert kaom_mod["applicable_types"] == ["Body Armour"]

    # Body-restricted Vestigial mod must not match Helmet slot
    helmet_res = query_local_data(
        "mod",
        "+200 to maximum Life",
        slot="Helmet",
        pob_dir=engine_dir,
        runtime_dir=cache_dir,
    )
    assert all(r["id"] != "DivergentIncreasedLifeUniqueBodyStr1" for r in helmet_res["results"])

    # PoB puts a Vestigial implicit only on a unique of the donor's item type: the slot comes from
    # the donor, never from the mod id, and no quiver takes one.
    for slot, donor_type in (("helmet", "Helmet"), ("gloves", "Gloves"), ("boots", "Boots"), ("shield", "Shield")):
        hits = query_local_data("mod", "Divergent", slot=slot, pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)
        vest = [r for r in hits["results"] if r["source"] == "Vestigial"]
        assert len(vest) > 50, slot
        assert all(r["applicable_types"] == [donor_type] and r["vestigialDonors"] for r in vest), slot
    quiver = query_local_data("mod", "Divergent", slot="quiver", pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)
    assert not [r for r in quiver["results"] if r["source"] == "Vestigial"]

    mod_md = _render_single_data_markdown(mod_res, "mod", "+200 to maximum Life")
    assert "unique Body Armour, from Kaom's Heart" in mod_md

    # Donor uniques carry vestigial_donor_mods + Vestigial slot note in Markdown
    cloak_res = query_local_data("unique", "Cloak of Flame", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert cloak_res["count"] >= 1
    cloak_u = next(r for r in cloak_res["results"] if r["name"] == "Cloak of Flame")
    assert any("Physical Damage taken as Fire Damage" in s for s in cloak_u["vestigial_donor_mods"])

    cloak_md = _render_single_data_markdown(cloak_res, "unique", "Cloak of Flame")
    assert "Gives as Vestigial implicit" in cloak_md
    assert "Takes a Vestigial implicit" in cloak_md and cloak_u["takes_vestigial"] is True

    # PoB offers the Vestigial source to core-pool uniques of five item types only.
    for name in ("Replica Kaom's Heart", "Drillneck", "Mageblood"):
        res = query_local_data("unique", name, pob_dir=engine_dir, runtime_dir=cache_dir)
        unique = next(r for r in res["results"] if r["name"] == name)
        assert unique["takes_vestigial"] is False, name
        assert "Takes a Vestigial implicit" not in _render_single_data_markdown(res, "unique", name)

    # A unique whose data puts a header before its base type still has its base, class and source
    # read, so one with a source is not offered a Vestigial implicit.
    for name, base in (("Gorgon's Gaze", "Regicide Mask"), ("Echoes of Creation", "Royal Burgonet")):
        res = query_local_data("unique", name, pob_dir=engine_dir, runtime_dir=cache_dir)
        unique = next(r for r in res["results"] if r["name"] == name)
        assert (unique["base_type"], unique["item_class"], bool(unique["source"])) == (base, "Helmet", True), name
        assert unique["takes_vestigial"] is False, name

    # PoB compares titles after writing a-umlaut plain; Vestigial.lua names the donor that way.
    guise_res = query_local_data("unique", "Doppelg\u00e4nger Guise", pob_dir=engine_dir, runtime_dir=cache_dir)
    guise = next(r for r in guise_res["results"] if r["name"].endswith("nger Guise"))
    assert guise["vestigial_donor_mods"]
    guise_mod = query_local_data(
        "mod", "DivergentRegenerateLifeNotUnhingedUnique__1", pob_dir=engine_dir, runtime_dir=cache_dir
    )
    assert guise_mod["results"][0]["applicable_types"] == ["Body Armour"]
    # The word itself finds them.
    by_word = query_local_data("mod", "vestigial", pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)
    assert len(by_word["results"]) > 400 and all(r["source"] == "Vestigial" for r in by_word["results"])

    kaom_res = query_local_data("unique", "Kaom's Heart", pob_dir=engine_dir, runtime_dir=cache_dir)
    kaom_u = next(r for r in kaom_res["results"] if r["name"] == "Kaom's Heart")
    assert "+200 to maximum Life" in kaom_u["vestigial_donor_mods"]

    # A Vestigial mod is found by the unique that gives it.
    donor_res = query_local_data("mod", "Kaom's Heart", pob_dir=engine_dir, runtime_dir=cache_dir)
    assert "DivergentIncreasedLifeUniqueBodyStr1" in [m["id"] for m in donor_res["results"]]


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
    # The Simplified dictionary of a bootstrapped engine names no runegraft; they are still found
    # by the words players use for the kind.
    for kind_word in ("符文嫁接", "符文之结"):
        by_word = query_local_data("tattoo", kind_word, pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)
        assert by_word["count"] == 32 and {r["kind"] for r in by_word["results"]} == {"runegraft"}, kind_word
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

    # `pob data runegraft` means runegrafts: it never answers with tattoos.
    only_rg = query_local_data("runegraft", "", tattoo_kind="tattoo", pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)
    assert {r["kind"] for r in only_rg["results"]} == {"runegraft"}
    clash = runner.invoke(cmd_data, ["runegraft", "--kind", "tattoo"])
    assert clash.exit_code == 2 and "runegrafts only" in clash.output


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

    # Small nodes that share a name and stats fold into one row; a node asked for by id must be the
    # one that stays (476 and 487 are two such "Strength" nodes).
    for wanted, twin in ((487, 476), (476, 487)):
        ids = [n["id"] for n in query_tree_data([str(wanted), "Strength"], pob_dir=engine_dir, runtime_dir=cache_dir, limit=0)["results"]]
        assert ids[0] == wanted and twin not in ids

    # A slang word that names a keystone must not bring its tattoo into a runegraft-only query.
    slang_res = query_local_data("tattoo", "杂耍", pob_dir=engine_dir, runtime_dir=cache_dir, tattoo_kind="runegraft")
    assert not [r for r in slang_res["results"] if r.get("kind") != "runegraft"]
    assert query_local_data("tattoo", "杂耍", pob_dir=engine_dir, runtime_dir=cache_dir)["results"][0]["name"] == "Acrobatics"


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
