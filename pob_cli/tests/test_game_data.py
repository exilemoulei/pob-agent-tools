"""Unit and benchmark tests for local PoB game data parser and SQLite index."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from engine_paths import find_engine_data
from support import perf_budget

from pob_cli.game_data import (
    _fold_search_text,
    ensure_gamedata_index,
    query_local_data,
    query_multi,
    resolve_pob_dir,
)
from pob_cli.updater import get_shared_cache_dir

REAL_POB_DIR = find_engine_data()
HAS_REAL_POB = REAL_POB_DIR is not None
MANAGED_POB_DIR = REAL_POB_DIR
HAS_MANAGED_POB = HAS_REAL_POB


@pytest.mark.skipif(not HAS_REAL_POB, reason="No real PoB engine found (set POB_ROOT or run pob bootstrap)")
def test_cold_build_and_warm_query_benchmarks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify cold SQLite index builds < 12s and warm queries execute < 15ms."""
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    t0 = time.perf_counter()
    db_path = ensure_gamedata_index(pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    cold_sec = time.perf_counter() - t0

    assert db_path.is_file()
    assert db_path.parent == get_shared_cache_dir(tmp_path, pob_root=REAL_POB_DIR)
    assert db_path.name.startswith("gamedata_") and db_path.name.endswith(".sqlite")
    assert cold_sec < perf_budget(12.0), f"Cold build took {cold_sec:.2f}s (expected < {perf_budget(12.0):.1f}s)"

    # Subsequent ensure_gamedata_index should hit cache immediately
    t_cache = time.perf_counter()
    db_path_2 = ensure_gamedata_index(pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    cache_ms = (time.perf_counter() - t_cache) * 1000.0
    assert db_path_2 == db_path
    assert cache_ms < perf_budget(15.0), f"Warm index check took {cache_ms:.2f}ms"

    warm_cases: list[tuple[str, str, dict[str, object]]] = [
        ("mod", "Life Converted to Energy Shield", {}),
        ("mod", "生命转", {}),
        ("mod", "maximum Life", {"slot": "body_armour", "ilvl": 81}),
        ("mod", "Prefixes Cannot Be Changed", {"slot": "ring"}),
        ("gem", "Dark Bargain", {}),
        ("unique", "Sap of the Seasons", {}),
        ("base", "Amber Amulet", {}),
        ("translate", "喋血之刃", {}),
        ("translate", "Dark Bargain", {}),
    ]
    for kind, query, kwargs in warm_cases:
        # The first call of a kind loads per-process tables (the zh dictionaries take ~90 ms), so time
        # a second one: otherwise the result depends on which tests ran earlier in the process.
        query_local_data(kind, query, pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        tq = time.perf_counter()
        res = query_local_data(kind, query, pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        elapsed_ms = (time.perf_counter() - tq) * 1000.0
        assert res["count"] >= 1, f"Expected >= 1 hit for {kind}({query!r})"
        limit_ms = perf_budget(15.0)
        assert elapsed_ms < limit_ms, f"Warm query {kind}({query!r}) took {elapsed_ms:.2f}ms (expected < {limit_ms:.0f}ms)"


@pytest.mark.skipif(not HAS_REAL_POB, reason="No real PoB engine found (set POB_ROOT or run pob bootstrap)")
def test_mod_bilingual_foulborn_bench_and_filters(tmp_path: Path) -> None:
    """Verify mod search in EN/ZH, Foulborn mods, ModMaster bench crafts, and slot/ilvl filters."""
    res_en = query_local_data("mod", "Life Converted to Energy Shield", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_en["zhAvailable"] is True
    ids_en = {m["id"] for m in res_en["results"]}
    assert "BreachBodyMaximumLifeConvertedToEnergyShield1___" in ids_en
    assert any(m["source"] == "Foulborn" for m in res_en["results"])

    res_zh = query_local_data("mod", "生命转", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    ids_zh = {m["id"] for m in res_zh["results"]}
    assert "BreachBodyMaximumLifeConvertedToEnergyShield1___" in ids_zh
    assert any("生命转" in (line or "") for m in res_zh["results"] for line in m["stats_zh"])

    # Slot + ilvl filter: Prime (ilvl 86) excluded at ilvl=81, Rapturous (ilvl 81) included
    res_body = query_local_data(
        "mod", "to maximum Life", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="Body Armour", ilvl=81, limit=0
    )
    body_ids = {m["id"] for m in res_body["results"]}
    assert "IncreasedLife11" in body_ids
    # Keyed by the base's defence tag (`str_armour`), not by `body_armour`.
    assert "LocalBaseArmourAndLife4" in body_ids
    assert "IncreasedLifeEnhancedBodyMod___" in body_ids
    assert "IncreasedLife12" not in body_ids
    for m in res_body["results"]:
        assert m["level"] <= 81
        if m["source"] == "Bench" or not m["weights"]:
            assert "Body Armour" in m["applicable_types"]
        else:
            assert any(
                (k in ("body_armour", "armour", "default") or k.startswith("body_armour_") or k.endswith("_armour"))
                and v > 0
                for k, v in m["weights"].items()
            )

    # Bench craft (ModMaster)
    res_bench = query_local_data(
        "mod", "Prefixes Cannot Be Changed", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="ring"
    )
    assert res_bench["count"] >= 1
    bench = res_bench["results"][0]
    assert bench["source"] == "Bench"
    assert bench["generation_type"] == "Bench Suffix"
    assert "Ring" in bench["applicable_types"]
    assert bench["bench_info"]["source"] == "Crafting Bench"

    # Tincture, Eldritch, JewelCluster coverage
    res_tinc = query_local_data(
        "mod", "Mana Burn", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="tincture"
    )
    assert any(m["source"] == "Tincture" and m["weights"].get("tincture", 0) > 0 for m in res_tinc["results"])

    res_eldritch = query_local_data(
        "mod", "Attack Speed", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path, slot="gloves", limit=50
    )
    assert any(m["source"] == "Eldritch" and m["generation_type"] in ("Exarch", "Eater") for m in res_eldritch["results"])

    res_cluster = query_local_data(
        "mod", "Prodigious Defence", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path
    )
    assert any(m["source"] == "JewelCluster" for m in res_cluster["results"])


@pytest.mark.skipif(not HAS_REAL_POB, reason="No real PoB engine found (set POB_ROOT or run pob bootstrap)")
def test_gem_stats_and_variants(tmp_path: Path) -> None:
    """Verify gem stats at levels 1/20/21, quality stats, ZH names, and transfigured/vaal/awakened variants."""
    res = query_local_data("gem", "Dark Bargain", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res["count"] >= 2
    dp = res["results"][0]
    assert dp["name"] == "Dark Bargain"
    assert dp["zh_name"] == "黑暗交易"
    assert dp["color"] == "Blue"
    assert dp["req_int"] == 100
    assert dp["cast_time"] == 0.5
    assert "1" in dp["levels"] and "20" in dp["levels"] and "21" in dp["levels"]
    assert dp["levels"]["20"]["stats"]["spell_minimum_base_chaos_damage"] > dp["levels"]["1"]["stats"]["spell_minimum_base_chaos_damage"]
    assert dp["quality_stats"][0]["at_20_quality"] == 2.0
    assert any(v["name"] == "Dark Bargain of Trarthus" and v["relation"] == "transfigured" for v in dp["variants"])

    # ZH gem query
    res_zh = query_local_data("gem", "黑暗交易", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_zh["results"][0]["name"] == "Dark Bargain"

    # Vaal variant discovery
    res_fb = query_local_data("gem", "Fireball", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    fb = res_fb["results"][0]
    assert fb["name"] == "Fireball"
    assert any(v["name"] == "Vaal Fireball" and v["relation"] == "vaal" for v in fb["variants"])

    # Support + Awakened variant discovery
    res_sup = query_local_data("gem", "Added Fire Damage Support", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    sup = res_sup["results"][0]
    assert sup["is_support"] is True
    assert any(v["name"] == "Awakened Added Fire Damage" and v["relation"] == "awakened" for v in sup["variants"])


@pytest.mark.skipif(not HAS_REAL_POB, reason="No real PoB engine found (set POB_ROOT or run pob bootstrap)")
def test_unique_and_base_and_translate(tmp_path: Path) -> None:
    """Verify unique (incl. Foulborn), base, and bidirectional translate queries."""
    res_sap = query_local_data("unique", "Sap of the Seasons", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_sap["count"] >= 1
    sap = res_sap["results"][0]
    assert sap["name"] == "Sap of the Seasons"
    assert sap["zh_name"] == "四季之侵"
    assert sap["base_type"] == "Prismatic Tincture"
    # Not every PobTools-zh data release translates this base (data-25 does not).
    if sap["base_type_zh"] is not None:
        assert sap["base_type_zh"] == "虹彩酊剂"
    assert sap["item_class"] == "Tincture"
    # PobTools-zh data-26 dropped Foulborn translations; older data carries them.
    if sap["zh_foulborn_name"] is not None:
        assert sap["zh_foulborn_name"] == "秽生 四季之侵"
    assert len(sap["explicits"]) >= 3

    res_ab = query_local_data("unique", "Foulborn Abyssus", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_ab["count"] >= 1
    ab = res_ab["results"][0]
    assert ab["name"] == "Abyssus"
    assert len(ab["foulborn_mods"]) >= 1

    res_base = query_local_data("base", "Amber Amulet", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_base["count"] == 1
    amber = res_base["results"][0]
    assert amber["zh_name"] == "琥珀护身符"
    assert amber["type"] == "Amulet"
    assert "+(20-30) to Strength" in amber["implicits"]

    res_flask = query_local_data("base", "Small Life Flask", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_flask["results"][0]["properties"]["flask"]["life"] == 70

    # Bidirectional translation exact-first ranking
    res_tr1 = query_local_data("translate", "喋血之刃", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_tr1["count"] >= 2
    assert res_tr1["results"][0]["match_type"] == "exact"
    assert {r["en"] for r in res_tr1["results"]} >= {"Bloodsoaked Blade", "Vampiric Blade"}

    res_tr2 = query_local_data("translate", "Dark Bargain", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_tr2["results"][0] == {
        "en": "Dark Bargain",
        "zh": "黑暗交易",
        "category": "gem",
        "match_type": "exact",
    }


def _create_synthetic_pob_dir(base_dir: Path, *, pob_ver: str = "2.99.0", extra_mod_level: int = 10) -> Path:
    poe1 = base_dir / "PoE1"
    data = poe1 / "Data"
    (data / "Skills").mkdir(parents=True, exist_ok=True)
    (data / "Bases").mkdir(parents=True, exist_ok=True)
    (data / "Uniques").mkdir(parents=True, exist_ok=True)

    (poe1 / "manifest.xml").write_text(
        f"<?xml version='1.0' encoding='utf-8'?><PoBVersion><Version number=\"{pob_ver}\" /></PoBVersion>",
        encoding="utf-8",
    )
    (data / "Gems.lua").write_text(
        """return {
    ["Metadata/Items/Gems/SkillGemTestBolt"] = {
        name = "Test Bolt",
        baseTypeName = "Test Bolt",
        gameId = "Metadata/Items/Gems/SkillGemTestBolt",
        variantId = "TestBolt",
        grantedEffectId = "TestBolt",
        tags = { intelligence = true, spell = true, },
        tagString = "Spell",
        reqStr = 0, reqDex = 0, reqInt = 50,
        naturalMaxLevel = 20,
    },
}""",
        encoding="utf-8",
    )
    (data / "Skills" / "act_int.lua").write_text(
        """local skills = ...
skills["TestBolt"] = {
    name = "Test Bolt",
    color = 3,
    description = "Fires a test bolt.",
    castTime = 0.7,
    qualityStats = { { "spell_damage_+%", 1 }, },
    stats = { "spell_minimum_base_fire_damage", },
    levels = {
        [1] = { 10, critChance = 5, levelRequirement = 1, cost = { Mana = 5 }, statInterpolation = { 1 }, },
        [20] = { 100, critChance = 6, levelRequirement = 70, cost = { Mana = 15 }, statInterpolation = { 1 }, },
    },
}""",
        encoding="utf-8",
    )
    (data / "Bases" / "amulet.lua").write_text(
        """local itemBases = ...
itemBases["Test Amulet"] = {
    type = "Amulet",
    tags = { amulet = true, default = true, },
    implicit = "+10 to Intelligence",
    req = { level = 12, },
}""",
        encoding="utf-8",
    )
    (data / "Uniques" / "amulet.lua").write_text(
        """return {
[[
Heart of Test
Test Amulet
Requires Level 12
Implicits: 1
+10 to Intelligence
+50 to maximum Life
]]
}""",
        encoding="utf-8",
    )
    (data / "ModExplicit.lua").write_text(
        f"""return {{
    ["TestLife1"] = {{ type = "Prefix", affix = "Testing", "+({extra_mod_level}-20) to maximum Life", level = {extra_mod_level}, group = "IncreasedLife", weightKey = {{ "amulet", "default" }}, weightVal = {{ 1000, 0 }}, modTags = {{ "life" }} }},
}}""",
        encoding="utf-8",
    )
    return base_dir


def test_pob_root_without_game_data_gives_way_and_a_poe2_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pob_cli.errors import CrossGameHallucinationError

    # A checkout may hold the legacy pack, which would answer before the last fallback below.
    monkeypatch.setattr("pob_cli.game_data.paths.detect_source_checkout_root", lambda: None)
    rt = tmp_path / "rt"
    engine_dir = _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.0", extra_mod_level=10)
    other = _create_synthetic_pob_dir(tmp_path / "other", pob_ver="2.98.0", extra_mod_level=10)

    monkeypatch.setenv("POB_ROOT", str(other))
    assert resolve_pob_dir(None, rt) == other

    # A typo: the daemon falls back to the managed engine for builds, so data queries must too.
    monkeypatch.setenv("POB_ROOT", str(tmp_path / "othre"))
    assert resolve_pob_dir(None, rt) == engine_dir
    assert resolve_pob_dir(None, tmp_path / "empty_rt") == tmp_path / "othre"

    # A data pack the daemon cannot run loses to a managed engine it can, and wins when there is none.
    monkeypatch.setenv("POB_ROOT", str(other))
    monkeypatch.setattr("pob_cli.updater.is_valid_engine_dir", lambda p: Path(p) == engine_dir)
    assert resolve_pob_dir(None, rt) == engine_dir
    monkeypatch.setattr("pob_cli.updater.is_valid_engine_dir", lambda p: False)
    assert resolve_pob_dir(None, rt) == other

    poe2_only = tmp_path / "poe2"
    (poe2_only / "PoE2" / "Data").mkdir(parents=True)
    (poe2_only / "Data").mkdir()
    monkeypatch.setenv("POB_ROOT", str(poe2_only))
    with pytest.raises(CrossGameHallucinationError):
        resolve_pob_dir(None, rt)


def test_no_zh_fallback_resolution_and_auto_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify --no-zh mode, <runtime_dir>/engine resolution, auto rebuild on update, and invalid kind error."""
    monkeypatch.delenv("POB_ROOT", raising=False)
    rt = tmp_path / "rt"
    engine_dir = _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.0", extra_mod_level=10)

    resolved = resolve_pob_dir(None, rt)
    assert resolved == engine_dir

    # --no-zh mode works 100% in English without Data/poe1/zh-rCN
    res_gem = query_local_data("gem", "Test Bolt", runtime_dir=rt)
    assert res_gem["zhAvailable"] is False
    assert res_gem["pobVersion"] == "2.99.0"
    assert res_gem["dataVersion"].startswith("nozh-")
    assert res_gem["results"][0]["zh_name"] is None
    assert res_gem["results"][0]["levels"]["20"]["stats"]["spell_minimum_base_fire_damage"] == 100

    res_mod = query_local_data("mod", "maximum Life", runtime_dir=rt, slot="amulet")
    assert res_mod["count"] == 1
    assert res_mod["results"][0]["level"] == 10
    assert res_mod["results"][0]["stats_zh"] == []

    res_tr = query_local_data("translate", "Test Bolt", runtime_dir=rt)
    assert res_tr["zhAvailable"] is False
    assert res_tr["results"][0]["en"] == "Test Bolt"
    assert res_tr["results"][0]["zh"] is None

    # Update engine data & version -> automatic index rebuild
    _create_synthetic_pob_dir(engine_dir, pob_ver="2.99.1", extra_mod_level=44)
    res_mod2 = query_local_data("mod", "maximum Life", runtime_dir=rt, slot="amulet")
    assert res_mod2["pobVersion"] == "2.99.1"
    assert res_mod2["results"][0]["level"] == 44

    with pytest.raises(ValueError, match="Unsupported kind"):
        query_local_data("invalid_kind", "foo", runtime_dir=rt)


def test_a_new_index_prunes_superseded_ones_but_keeps_the_newest_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each engine update names a new 50 MB index; the old ones used to pile up for ever."""
    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    rt = tmp_path / "rt"
    _create_synthetic_pob_dir(rt / "engine")
    cache_dir = rt / "cache"
    cache_dir.mkdir(parents=True)
    for age, name in enumerate(("gamedata_2.60.0_new.sqlite", "gamedata_2.50.0_old.sqlite", "gamedata_2.40.0_older.sqlite")):
        stale = cache_dir / name
        stale.write_bytes(b"old index")
        os.utime(stale, (1_700_000_000 - age * 86400, 1_700_000_000 - age * 86400))
    (cache_dir / "trade_stats.json").write_text("{}", encoding="utf-8")

    db_path = ensure_gamedata_index(runtime_dir=rt)
    assert db_path.parent == cache_dir
    assert sorted(p.name for p in cache_dir.iterdir() if p.suffix == ".sqlite") == sorted(
        [db_path.name, "gamedata_2.60.0_new.sqlite"]
    )
    assert (cache_dir / "trade_stats.json").is_file()


def test_tree_index_rebuilds_when_any_dictionary_file_it_reads_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pob_cli.game_data import ensure_treedata_index

    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    rt = tmp_path / "rt"
    engine_dir = _create_synthetic_pob_dir(rt / "engine")
    tree_dir = engine_dir / "PoE1" / "TreeData" / "3_30"
    tree_dir.mkdir(parents=True)
    (tree_dir / "tree.lua").write_text("return {}", encoding="utf-8")
    zh_dir = engine_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir.mkdir(parents=True)
    season = zh_dir / "Z-S29.json"
    season.write_text('{"entries": {}}', encoding="utf-8")

    first, _ = ensure_treedata_index(runtime_dir=rt)
    season.write_text('{"entries": {"Some Notable": "某个天赋"}}', encoding="utf-8")
    second, _ = ensure_treedata_index(runtime_dir=rt)
    assert second != first and second.is_file()


def test_fold_search_text_diacritics_and_punctuation() -> None:
    assert _fold_search_text("Doppelgänger Guise") == "doppelganger guise"
    assert _fold_search_text("Mjölner") == "mjolner"
    assert _fold_search_text("Watcher’s Eye") == "watcher's eye"
    assert _fold_search_text("Precursor‘s — Emblem") == "precursor's - emblem"


def test_traditional_queries_match_the_simplified_index_and_unknown_slots_match_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pob-zh shows Traditional names, the dictionary is Simplified: index and query fold alike."""
    import json

    from pob_cli import item_inspect

    assert _fold_search_text("圖騰") == _fold_search_text("图腾") == "图腾"
    # 覆 is a foldable character that Simplified text keeps, so only folding both sides is safe.
    assert _fold_search_text("覆盖") == _fold_search_text("覆蓋")

    monkeypatch.delenv("POB_ROOT", raising=False)
    rt = tmp_path / "rt"
    engine_dir = _create_synthetic_pob_dir(rt / "engine")
    zh_dir = engine_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir.mkdir(parents=True)
    (zh_dir / "meta.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    (zh_dir / "gems.json").write_text(
        json.dumps({"entries": {"Test Bolt": "覆盖图腾"}}, ensure_ascii=False), encoding="utf-8"
    )
    (engine_dir / "PoE1" / "Data" / "ModExplicit.lua").write_text(
        """return {
    ["TestLife1"] = { type = "Prefix", affix = "Testing", "+(10-20) to maximum Life", level = 10, group = "IncreasedLife", weightKey = { "amulet", "default" }, weightVal = { 1000, 0 }, modTags = { "life" } },
    ["TestLife2"] = { type = "Prefix", affix = "Generic", "+(30-40) to maximum Life", level = 20, group = "IncreasedLife", weightKey = { "default" }, weightVal = { 1000 }, modTags = { "life" } },
}""",
        encoding="utf-8",
    )
    (engine_dir / "PoE1" / "Data" / "Bases" / "staff.lua").write_text(
        """local itemBases = ...
itemBases["Maelstrom Staff"] = { type = "Staff", tags = { staff = true, default = true, }, req = { level = 64, }, }""",
        encoding="utf-8",
    )

    for query in ("覆盖图腾", "覆蓋圖騰", "圖騰"):
        gems = query_local_data("gem", query, runtime_dir=rt)
        assert [g["name"] for g in gems["results"]] == ["Test Bolt"], query
        assert query_local_data("translate", query, runtime_dir=rt)["results"][0]["en"] == "Test Bolt"

    def mods(slot: str) -> list[str]:
        res = query_local_data("mod", "maximum Life", runtime_dir=rt, slot=slot)
        return sorted(m["id"] for m in res["results"])

    assert mods("amulet") == mods("项链") == mods("項鍊") == ["TestLife1", "TestLife2"]
    # A typo used to inherit the default weight and list every generic mod as if it were a slot.
    assert mods("amulat") == []

    # Pasted item text carries the game's spelling; the index stores the folded base name.
    assert item_inspect._bench_slot("weapon1", "Maelström Staff", pob_dir=None, runtime_dir=rt) == "staff"


def test_synthetic_diacritics_special_uniques_item_skills_shared_cache_and_multi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline synthetic test covering diacritics, Special uniques, item skills, shared cache, and query_multi."""
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    shared_rt = tmp_path / ".runtime"
    engine_dir = _create_synthetic_pob_dir(shared_rt / "engine", pob_ver="2.99.5")
    # Mark synthetic engine dir valid for updater.is_valid_engine_dir on Windows & POSIX
    (engine_dir / "pob-zh.exe").write_bytes(b"")
    (engine_dir / "Data" / "bridge").mkdir(parents=True, exist_ok=True)
    (engine_dir / "Data" / "bridge" / "bridge.lua").write_text("-- stub", encoding="utf-8")
    data_dir = engine_dir / "PoE1" / "Data"

    # Add diacritic uniques
    (data_dir / "Uniques" / "body.lua").write_text(
        """return {
[[
Doppelgänger Guise
Test Amulet
Implicits: 0
(40-60)% more Critical Strike Chance while Insane
]],
[[
Mjölner
Test Amulet
Implicits: 0
Trigger a Socketed Lightning Spell on Hit
]]
}""",
        encoding="utf-8",
    )

    # Add Special uniques (WatchersEye.lua, BoundByDestiny.lua, Generated.lua)
    special_dir = data_dir / "Uniques" / "Special"
    special_dir.mkdir(parents=True, exist_ok=True)
    (special_dir / "WatchersEye.lua").write_text(
        """return {
    ["WrathLightningPenetration"] = { affix = "", "Damage Penetrates (10-15)% Lightning Resistance while affected by Wrath", level = 1, group = "WrathPen", weightKey = {}, weightVal = {}, modTags = {} },
}""",
        encoding="utf-8",
    )
    (special_dir / "BoundByDestiny.lua").write_text(
        """return {
    ["MaxLife2Elder"] = { type = "2Elder", affix = "", "(10-15)% increased maximum Life if 2 Elder Items are Equipped", level = 1, group = "MaxLife2Elder", weightKey = {}, weightVal = {}, modTags = {} },
}""",
        encoding="utf-8",
    )
    (special_dir / "Generated.lua").write_text(
        """data.uniques.generated = {}
local watchersEye = {
[[
Watcher's Eye
Prismatic Jewel
Limited to: 1
]]
}
local boundByDestiny = {
[[
Bound by Destiny
Prismatic Jewel
Limited to: 1
]]
}
local impossibleEscape = {
    "Impossible Escape",
    "Viridian Jewel",
    "Limited to: 1",
}
function buildForbidden(classNotables)
end
table.insert(data.uniques.generated, table.concat(watchersEye, "\\n"))
table.insert(data.uniques.generated, table.concat(boundByDestiny, "\\n"))
table.insert(data.uniques.generated, table.concat(impossibleEscape, "\\n"))
""",
        encoding="utf-8",
    )

    # Add item-granted skill in other.lua
    (data_dir / "Skills" / "other.lua").write_text(
        """return function(skills, mod, flag, skill)
skills["SupportUniqueSuspendInTimeCanUse"] = {
    name = "Temporal Mirage",
    hidden = true,
    color = 4,
    support = true,
    fromItem = true,
    constantStats = { { "base_cast_speed_+%", 100 } },
    stats = {},
    levels = {
        [1] = { levelRequirement = 70, manaMultiplier = -100 },
    },
}
end""",
        encoding="utf-8",
    )

    # Two separate swarm worker runtime dirs pointing at shared POB_ROOT must reuse ONE SQLite DB
    monkeypatch.setenv("POB_ROOT", str(engine_dir))
    worker_a = tmp_path / "swarm_a"
    worker_b = tmp_path / "swarm_b"
    db_a = ensure_gamedata_index(runtime_dir=worker_a)
    db_b = ensure_gamedata_index(runtime_dir=worker_b)
    assert db_a == db_b
    assert db_a.parent == shared_rt / "cache"

    # Diacritic folding queries
    res_dg = query_local_data("unique", "Doppelganger Guise", runtime_dir=worker_a)
    assert res_dg["count"] >= 1
    assert res_dg["results"][0]["name"] == "Doppelgänger Guise"

    res_mj = query_local_data("unique", "Mjolner", runtime_dir=worker_a)
    assert res_mj["count"] >= 1
    assert res_mj["results"][0]["name"] == "Mjölner"

    # Curly apostrophe & Special uniques
    for q_name, expected in (
        ("Watcher’s Eye", "Watcher's Eye"),
        ("Bound by Destiny", "Bound by Destiny"),
        ("Forbidden Flame", "Forbidden Flame"),
        ("Forbidden Flesh", "Forbidden Flesh"),
        ("Impossible Escape", "Impossible Escape"),
    ):
        r = query_local_data("unique", q_name, runtime_dir=worker_a)
        assert r["count"] >= 1, f"Expected hit for special unique {q_name!r}"
        assert r["results"][0]["name"] == expected

    # Item-granted skill lookup
    res_tm = query_local_data("gem", "Temporal Mirage", runtime_dir=worker_a)
    assert res_tm["count"] == 1
    assert res_tm["results"][0]["name"] == "Temporal Mirage"
    assert res_tm["results"][0]["is_item_skill"] is True
    assert res_tm["results"][0]["skill_source"] == "item"

    # query_multi using Path and open sqlite3.Connection (no redundant items/byQuery keys)
    multi_res = query_multi(db_a, "unique", [" Doppelganger Guise ", "Mjolner", "Forbidden Flame", "   "])
    assert multi_res["kind"] == "unique"
    assert multi_res["queries"] == ["Doppelganger Guise", "Mjolner", "Forbidden Flame"]
    assert "items" not in multi_res
    assert "byQuery" not in multi_res
    assert list(multi_res["results"].keys()) == ["Doppelganger Guise", "Mjolner", "Forbidden Flame"]
    assert multi_res["results"]["Mjolner"]["results"][0]["name"] == "Mjölner"

    conn = sqlite3.connect(f"{db_a.resolve().as_uri()}?mode=ro", uri=True)
    try:
        multi_conn = query_multi(conn, "gem", ["Test Bolt", "Temporal Mirage"])
        assert [multi_conn["results"][q]["results"][0]["name"] for q in multi_conn["queries"]] == [
            "Test Bolt",
            "Temporal Mirage",
        ]
    finally:
        conn.close()

    # query_multi with optional db=None (keyword dispatch matching daemon.py)
    multi_kw = query_multi(kind="gem", queries=["Test Bolt", "Temporal Mirage"], runtime_dir=worker_a)
    assert multi_kw["results"]["Temporal Mirage"]["results"][0]["name"] == "Temporal Mirage"

    # Explicit POB_CACHE_DIR override
    custom_cache = tmp_path / "custom_shared_cache"
    monkeypatch.setenv("POB_CACHE_DIR", str(custom_cache))
    db_c = ensure_gamedata_index(runtime_dir=worker_a)
    assert db_c.parent == custom_cache


def test_schema_v4_to_v5_auto_upgrade_and_explicit_pob_dir_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pre-existing v4 SQLite DB auto-rebuilds to v5 and explicit pob_dir ignores env POB_ROOT."""
    from pob_cli import game_data

    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    # Point env POB_ROOT at a shared .runtime/engine to simulate active swarm env
    env_rt = tmp_path / "env_shared" / ".runtime"
    env_eng = _create_synthetic_pob_dir(env_rt / "engine", pob_ver="2.99.9")
    (env_eng / "pob-zh.exe").write_bytes(b"")
    monkeypatch.setenv("POB_ROOT", str(env_eng))

    # Build against an explicit separate legacy-style pob_dir -> must write to tmp_path/rt/cache, NOT env_rt/cache
    legacy_pob = _create_synthetic_pob_dir(tmp_path / "LegacySeed", pob_ver="2.99.0")
    rt = tmp_path / "rt"
    db_path = ensure_gamedata_index(pob_dir=legacy_pob, runtime_dir=rt)
    assert db_path.parent == rt / "cache"

    # Downgrade DB schema_version to "4" and drop gems.is_item_skill to simulate a pre-existing v4 DB
    game_data._VALID_INDEX_CACHE.clear()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    # ensure_gamedata_index must detect v4 != SCHEMA_VERSION and auto-rebuild
    assert game_data.SCHEMA_VERSION >= 7
    rebuilt_path = ensure_gamedata_index(pob_dir=legacy_pob, runtime_dir=rt)
    assert rebuilt_path == db_path
    conn = sqlite3.connect(f"{rebuilt_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == str(game_data.SCHEMA_VERSION)
    finally:
        conn.close()


def test_concurrent_build_lockfile_prevents_thundering_herd_and_recovers_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify atomic build lockfile serializes concurrent cold builds, cleans .tmp, and recovers stale locks."""
    import concurrent.futures
    import json

    from pob_cli import game_data

    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    rt = tmp_path / "rt_lock"
    engine_dir = _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.7")

    # 1. Pre-create a stale lockfile from a dead PID + old mtime
    poe1_dir, zh_dir = game_data._resolve_poe1_data_dirs(engine_dir)
    pob_ver, data_ver = game_data.detect_versions(poe1_dir, zh_dir)
    cache_dir = rt / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    expected_db = cache_dir / f"gamedata_{pob_ver}_{data_ver}.sqlite"
    lock_file = Path(f"{expected_db}.build.lock")
    lock_file.write_text(json.dumps({"pid": 99999999, "ts": 1.0}), encoding="utf-8")
    os.utime(lock_file, (1.0, 1.0))

    # 2. Fire 4 concurrent threads calling ensure_gamedata_index simultaneously
    parse_calls = 0
    real_parse_gems = game_data._parse_gems_and_skills

    def counted_parse_gems(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        time.sleep(0.05)
        return real_parse_gems(*args, **kwargs)

    monkeypatch.setattr(game_data, "_parse_gems_and_skills", counted_parse_gems)
    game_data._VALID_INDEX_CACHE.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(ensure_gamedata_index, runtime_dir=rt) for _ in range(4)]
        paths = [f.result() for f in futs]

    assert all(p == expected_db for p in paths)
    assert parse_calls == 1
    assert not lock_file.exists()
    assert list(cache_dir.glob("*.tmp")) == []


@pytest.mark.skipif(not HAS_REAL_POB, reason="No real PoB engine found (set POB_ROOT or run pob bootstrap)")
def test_real_pob_diacritics_special_uniques_item_skills_and_partial_zh_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify real PoB index covers diacritics, Special uniques, Temporal Mirage, and partial CN translate."""
    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    assert query_local_data("unique", "Doppelganger Guise", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)["results"][0]["name"] == "Doppelgänger Guise"
    assert query_local_data("unique", "Mjolner", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)["results"][0]["name"] == "Mjölner"

    for special_name in (
        "Watcher's Eye",
        "Bound by Destiny",
        "Forbidden Flame",
        "Forbidden Flesh",
        "Impossible Escape",
        "The Balance of Terror",
        "Sublime Vision",
        "Megalomaniac",
        "Precursor's Emblem",
        "Skin of the Lords",
    ):
        res_sp = query_local_data("unique", special_name, pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
        assert res_sp["count"] >= 1, f"Missing Special unique {special_name}"
        assert res_sp["results"][0]["name"] == special_name

    # Item-granted skills from Data/Skills/other.lua & glove.lua
    for skill_name in ("Abberath's Fury", "Word of Blades"):
        res_sk = query_local_data("gem", skill_name, pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
        assert res_sk["count"] >= 1, f"Expected item skill {skill_name!r}"
        assert res_sk["results"][0]["name"] == skill_name
        assert res_sk["results"][0]["is_item_skill"] is True

    if HAS_MANAGED_POB:
        res_tm = query_local_data("gem", "Temporal Mirage", pob_dir=MANAGED_POB_DIR, runtime_dir=tmp_path / "managed")
        assert res_tm["count"] >= 1
        assert res_tm["results"][0]["name"] == "Temporal Mirage"
        assert res_tm["results"][0]["is_item_skill"] is True

        res_pearl = query_local_data("unique", "Pearl of Tsoatha", pob_dir=MANAGED_POB_DIR, runtime_dir=tmp_path / "managed")
        assert res_pearl["count"] >= 1
        pearl = res_pearl["results"][0]
        assert pearl["name"] == "Pearl of Tsoatha"
        assert pearl["implicits"] == ["+(8-10)% to all Elemental Resistances"]
        assert len(pearl["explicits"]) >= 1

    # That Which Was Taken parses cleanly despite leading `Item Class:` + `Rarity:` lines
    res_twwt = query_local_data("unique", "That Which Was Taken", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_twwt["count"] >= 1
    assert res_twwt["results"][0]["name"] == "That Which Was Taken"
    assert res_twwt["results"][0]["base_type"] == "Crimson Jewel"

    # Partial Chinese translate queries return core entity matches without lore proverb pollution
    res_ivory = query_local_data("translate", "象牙塔", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_ivory["count"] >= 1
    assert any(r["en"] == "The Ivory Tower" for r in res_ivory["results"])

    res_frost = query_local_data("translate", "寒冰法师", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_frost["count"] >= 1
    assert any("Frost" in r["en"] for r in res_frost["results"])

    # Gibberish CJK query must return 0 hits instead of lore proverbs
    res_gib = query_local_data("translate", "不存在的测试词缀", pob_dir=REAL_POB_DIR, runtime_dir=tmp_path)
    assert res_gib["count"] == 0


def test_build_lock_timeout_same_pid_stale_token_ownership_and_force_rebuild_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P0/P2 build lock hardening: no infinite spin, same-PID stale reclaim, token ownership, force_rebuild error."""
    import json

    from pob_cli import game_data

    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    # 1. _is_pid_alive bounds check
    assert game_data._is_pid_alive(0) is False
    assert game_data._is_pid_alive(-5) is False
    assert game_data._is_pid_alive(0x1_0000_0000) is False
    assert game_data._is_pid_alive(os.getpid()) is True

    # 2. Same-PID leaked lock older than stale_sec is reclaimed via atomic rename
    lock_path = tmp_path / "test.sqlite.build.lock"
    db_path = tmp_path / "test.sqlite"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "token": "old-tok", "ts": 1.0}), encoding="utf-8")
    os.utime(lock_path, (1.0, 1.0))
    assert game_data._is_build_lock_stale(lock_path, stale_sec=60.0) is True

    tok = game_data._acquire_build_lock(lock_path, db_path, "2.99.0", "nozh-1", timeout_sec=0.5, stale_sec=60.0)
    assert isinstance(tok, str) and tok
    stored = json.loads(lock_path.read_text(encoding="utf-8"))
    assert stored["token"] == tok

    # 3. _release_build_lock must NOT unlink a lock owned by a different token
    game_data._release_build_lock(lock_path, "wrong-token")
    assert lock_path.exists()
    game_data._release_build_lock(lock_path, tok)
    assert not lock_path.exists()

    # 4. Unbreakable stale lock (os.replace raises PermissionError) raises TimeoutError cleanly (no infinite spin)
    lock_path.write_text(json.dumps({"pid": 99999999, "token": "stuck", "ts": 1.0}), encoding="utf-8")
    os.utime(lock_path, (1.0, 1.0))
    real_replace = os.replace

    def deny_replace(src, dst):
        raise PermissionError("locked by AV")

    monkeypatch.setattr(os, "replace", deny_replace)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        game_data._acquire_build_lock(lock_path, db_path, "2.99.0", "nozh-1", timeout_sec=0.12, stale_sec=60.0)
    assert (time.monotonic() - t0) < 2.0
    monkeypatch.setattr(os, "replace", real_replace)
    lock_path.unlink(missing_ok=True)

    # 5. Write failure inside _acquire_build_lock cleans up 0-byte lockfile
    def fail_write(_fd, _data):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        game_data._acquire_build_lock(lock_path, db_path, "2.99.0", "nozh-1", timeout_sec=0.2)
    assert not lock_path.exists()
    monkeypatch.undo()

    # 6. force_rebuild=True must raise PermissionError if os.replace fails (never silently return old DB)
    rt = tmp_path / "rt_force"
    _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.8")
    built = ensure_gamedata_index(runtime_dir=rt)
    assert built.is_file()

    def deny_db_replace(src, dst):
        if Path(dst) == built:
            raise PermissionError("db in use")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", deny_db_replace)
    with pytest.raises(PermissionError, match="db in use"):
        ensure_gamedata_index(runtime_dir=rt, force_rebuild=True)


def test_synthetic_uniques_header_pearl_and_lore_proverb_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify That Which Was Taken header stripping, Pearl of Tsoatha implicit/explicit, and lore proverb exclusion."""
    import json

    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    rt = tmp_path / "rt_syn"
    engine_dir = _create_synthetic_pob_dir(rt / "engine", pob_ver="2.99.6")
    data_dir = engine_dir / "PoE1" / "Data"

    # Synthetic That Which Was Taken with Item Class + Rarity + Requirements + Level
    (data_dir / "Uniques" / "jewel.lua").write_text(
        """return {
[[
Item Class: Jewels
Rarity: Unique
That Which Was Taken
Crimson Jewel
Requirements:
Level: 48
Crafted: true
League: Affliction
Limited to: 1
Implicits: 0
+10 to all Attributes
]]
}""",
        encoding="utf-8",
    )

    # Synthetic Pearl of Tsoatha in Special/Generated.lua
    special_dir = data_dir / "Uniques" / "Special"
    special_dir.mkdir(parents=True, exist_ok=True)
    (special_dir / "Generated.lua").write_text(
        """data.uniques.generated = {}
local pearl = {
    "Pearl of Tsoatha",
    "Prismatic Ring",
    "LevelReq: 30",
    "Implicits: 1",
}
table.insert(data.uniques.generated, table.concat(pearl, "\\n"))
""",
        encoding="utf-8",
    )

    # Synthetic ZH dictionary with keystone/ascendancy proverbs + dotted entity name
    zh_dir = engine_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir.mkdir(parents=True, exist_ok=True)
    (zh_dir / "meta.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    (zh_dir / "Z-Keystone_Proverb.json").write_text(
        json.dumps({"entries": {"The First Ones exist,": "不存在的古老格言测试"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (zh_dir / "Z-Ascendancy_Proverb.json").write_text(
        json.dumps({"entries": {"The artist need not be present": "不存在的升华箴言词缀"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (zh_dir / "uniques.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Ralakesh's Impatience": "拉克斯・急躁",
                    "The Ivory Tower": "象牙之塔",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    res_tw = query_local_data("unique", "That Which Was Taken", runtime_dir=rt)
    assert res_tw["count"] == 1
    tw = res_tw["results"][0]
    assert tw["name"] == "That Which Was Taken"
    assert tw["base_type"] == "Crimson Jewel"
    assert tw["req_level"] == 48
    assert tw["explicits"] == ["+10 to all Attributes"]

    res_pearl = query_local_data("unique", "Pearl of Tsoatha", runtime_dir=rt)
    assert res_pearl["count"] == 1
    pearl = res_pearl["results"][0]
    assert pearl["implicits"] == ["+(8-10)% to all Elemental Resistances"]
    assert pearl["explicits"] == ["Socketed/Random Support Gem (Helmet / Gloves / Boots / Passive Tree)"]

    # Dotted entity name matches via Step 4a and skips 4b/4c
    res_dot = query_local_data("translate", "拉克斯急躁", runtime_dir=rt)
    assert res_dot["count"] == 1
    assert res_dot["results"][0]["en"] == "Ralakesh's Impatience"

    # Lore proverbs excluded and gibberish CJK returns 0 hits
    res_gib = query_local_data("translate", "不存在的测试词缀", runtime_dir=rt)
    assert res_gib["count"] == 0


def test_tree_zh_names_come_from_a_bootstrapped_engines_merged_passives_file(tmp_path: Path) -> None:
    from pob_cli.game_data import _load_tree_zh_maps

    merged = {"entries": {"Amplify": "增幅", "10% increased Area of Effect": "效果区域扩大 10%"}}
    managed = tmp_path / "zh-rCN"
    managed.mkdir()
    (managed / "passives.json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    names, stats = _load_tree_zh_maps(managed)
    assert names["Amplify"] == "增幅"
    assert stats["10% increased area of effect"] == "效果区域扩大 10%"

    # The pack keeps its own tree_dn.json; a passives.json next to it is the superseded one.
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "tree_dn.json").write_text(json.dumps({"entries": {"Amplify": "增幅"}}, ensure_ascii=False), encoding="utf-8")
    (pack / "passives.json").write_text(json.dumps({"entries": {"Amplify": "old", "Other": "x"}}), encoding="utf-8")
    names, _ = _load_tree_zh_maps(pack)
    assert names == {"Amplify": "增幅"}
