"""Unit and local-engine integration tests for pob_cli.trade_query."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pob_cli import trade_query as tq
from pob_cli.updater import resolve_engine_root

# Representative real PoE1 trade stat entries (exact IDs and texts from GGG / TradeSiteStats.lua)
SAMPLE_ENTRIES: list[dict[str, str]] = [
    # Resistances
    {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
    {"id": "implicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "implicit"},
    {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
    {"id": "crafted.stat_4220027924", "text": "+#% to Cold Resistance", "type": "crafted"},
    {"id": "explicit.stat_1671376347", "text": "+#% to Lightning Resistance", "type": "explicit"},
    {"id": "explicit.stat_2923486259", "text": "+#% to Chaos Resistance", "type": "explicit"},
    {"id": "explicit.stat_2901986750", "text": "+#% to all Elemental Resistances", "type": "explicit"},
    {"id": "pseudo.pseudo_total_elemental_resistance", "text": "+#% total Elemental Resistance", "type": "pseudo"},
    # Life
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "pseudo.pseudo_total_life", "text": "+# total maximum Life", "type": "pseudo"},
    # Attack Speed (Global vs Local vs More vs Pseudo)
    {"id": "explicit.stat_681332047", "text": "#% increased Attack Speed", "type": "explicit"},
    {"id": "explicit.stat_210067635", "text": "#% increased Attack Speed (Local)", "type": "explicit"},
    {"id": "explicit.stat_2418322751", "text": "#% more Attack Speed", "type": "explicit"},
    {"id": "pseudo.pseudo_total_attack_speed", "text": "+#% total Attack Speed", "type": "pseudo"},
    # Added Flat Elemental Damage (Global vs Local vs To Attacks)
    {"id": "explicit.stat_321077055", "text": "Adds # to # Fire Damage", "type": "explicit"},
    {"id": "explicit.stat_709508406", "text": "Adds # to # Fire Damage (Local)", "type": "explicit"},
    {"id": "explicit.stat_1573130764", "text": "Adds # to # Fire Damage to Attacks", "type": "explicit"},
    {"id": "explicit.stat_2387423236", "text": "Adds # to # Cold Damage", "type": "explicit"},
    {"id": "explicit.stat_1037193709", "text": "Adds # to # Cold Damage (Local)", "type": "explicit"},
    {"id": "explicit.stat_1334060246", "text": "Adds # to # Lightning Damage", "type": "explicit"},
    {"id": "explicit.stat_3336890334", "text": "Adds # to # Lightning Damage (Local)", "type": "explicit"},
    # Armour / ES (Global vs Local)
    {"id": "explicit.stat_809229260", "text": "+# to Armour", "type": "explicit"},
    {"id": "explicit.stat_3484657501", "text": "+# to Armour (Local)", "type": "explicit"},
    {"id": "explicit.stat_3489782002", "text": "+# to maximum Energy Shield", "type": "explicit"},
    {"id": "explicit.stat_4052037485", "text": "+# to maximum Energy Shield (Local)", "type": "explicit"},
    # Shield Block
    {"id": "explicit.stat_2481353198", "text": "#% increased Chance to Block", "type": "explicit"},
    {"id": "explicit.stat_4253454700", "text": "+#% Chance to Block (Shields)", "type": "explicit"},
    # Suppression & Movement
    {"id": "explicit.stat_3680664274", "text": "+#% chance to Suppress Spell Damage", "type": "explicit"},
    {"id": "explicit.stat_2250533757", "text": "#% increased Movement Speed", "type": "explicit"},
    # Tiny/short entries that must NEVER hijack long unrelated queries (PR #1 bug regression)
    {"id": "explicit.stat_tiny_1", "text": "Damage", "type": "explicit"},
    {"id": "explicit.stat_tiny_2", "text": "Life", "type": "explicit"},
]


def _local_lua_exists() -> bool:
    return (resolve_engine_root() / "PoE1" / "Data" / "TradeSiteStats.lua").is_file()


# ---------------------------------------------------------------------------
# 1. Slot & Category mappings + weapon base inference
# ---------------------------------------------------------------------------


def test_slot_category_and_pob_slot_mappings() -> None:
    assert tq.category_for_slot("helmet") == "armour.helmet"
    assert tq.category_for_slot("Helm") == "armour.helmet"
    assert tq.category_for_slot("Body Armour") == "armour.chest"
    assert tq.category_for_slot("body-armour") == "armour.chest"
    assert tq.category_for_slot("Ring 1") == "accessory.ring"
    assert tq.category_for_slot("ring3") == "accessory.ring"
    assert tq.category_for_slot("weapon") == "weapon"
    assert tq.category_for_slot("weapon1") is None
    assert tq.category_for_slot("weapon2") is None
    assert tq.category_for_slot("bow") == "weapon.bow"
    assert tq.category_for_slot("Two-Handed Sword") == "weapon.twosword"
    assert tq.category_for_slot("one_handed_axe") == "weapon.oneaxe"
    assert tq.category_for_slot("flask3") == "flask"
    assert tq.category_for_slot("abyss_jewel") == "jewel.abyss"
    assert tq.category_for_slot("cluster_jewel") == "jewel.cluster"
    # Chinese slot names resolve in either script.
    assert tq.category_for_slot("頭盔") == tq.category_for_slot("头盔") == "armour.helmet"
    assert tq.category_for_slot("腰帶") == "accessory.belt"

    assert tq.pob_slot_for_slot("Body Armour") == "Body Armour"
    assert tq.pob_slot_for_slot("chest") == "Body Armour"
    assert tq.pob_slot_for_slot("shield") == "Weapon 2"
    assert tq.pob_slot_for_slot("quiver") == "Weapon 2"
    assert tq.pob_slot_for_slot("bow") == "Weapon 1"
    assert tq.pob_slot_for_slot("Ring 2") == "Ring 2"
    assert tq.pob_slot_for_slot("flask5") == "Flask 5"
    assert tq.pob_slot_for_slot("cluster_jewel") == "Jewel 1"


def test_infer_weapon_category_from_base_offline_fallback(tmp_path: Path) -> None:
    # Point pob_root at an empty temp dir to force keyword fallback without SQLite.
    assert tq.infer_weapon_category_from_base("Spine Bow", pob_root=tmp_path) == "weapon.bow"
    assert tq.infer_weapon_category_from_base("Profane Wand", pob_root=tmp_path) == "weapon.wand"
    assert tq.infer_weapon_category_from_base("Imperial Claw", pob_root=tmp_path) == "weapon.claw"
    assert tq.infer_weapon_category_from_base("Platinum Kris", pob_root=tmp_path) == "weapon.dagger"
    assert tq.infer_weapon_category_from_base("Void Sceptre", pob_root=tmp_path) == "weapon.sceptre"
    assert tq.infer_weapon_category_from_base("Eclipse Staff", pob_root=tmp_path) == "weapon.staff"
    assert tq.infer_weapon_category_from_base("Jewelled Foil", pob_root=tmp_path) == "weapon.onesword"
    assert tq.infer_weapon_category_from_base("Vaal Greatsword", pob_root=tmp_path) == "weapon.twosword"
    assert tq.infer_weapon_category_from_base("Vaal Axe", pob_root=tmp_path) == "weapon.twoaxe"
    assert tq.infer_weapon_category_from_base("Titanium Spirit Shield", pob_root=tmp_path) == "armour.shield"
    assert tq.infer_weapon_category_from_base("Broadhead Arrow Quiver", pob_root=tmp_path) == "armour.quiver"
    assert tq.infer_weapon_category_from_base("", pob_root=tmp_path) is None


# ---------------------------------------------------------------------------
# 2. Local TradeSiteStats.lua loader + caching + remote fallback
# ---------------------------------------------------------------------------


def test_load_local_trade_stats_parses_lua_and_ignores_nested_options(tmp_path: Path) -> None:
    lua_file = tmp_path / "PoE1" / "Data" / "TradeSiteStats.lua"
    lua_file.parent.mkdir(parents=True)
    lua_file.write_text(
        """-- synthetic test
return {
\t{
\t\t["entries"] = {
\t\t\t{
\t\t\t\t["id"] = "pseudo.pseudo_tangled_implicit_tier",
\t\t\t\t["option"] = {
\t\t\t\t\t["options"] = {
\t\t\t\t\t\t{
\t\t\t\t\t\t\t["id"] = 1,
\t\t\t\t\t\t\t["text"] = "Lesser",
\t\t\t\t\t\t},
\t\t\t\t\t},
\t\t\t\t},
\t\t\t\t["text"] = "Item has # Eater of Worlds Implicit Tier",
\t\t\t\t["type"] = "pseudo",
\t\t\t},
\t\t\t{
\t\t\t\t["id"] = "explicit.stat_210067635",
\t\t\t\t["text"] = "#% increased Attack Speed (Local)",
\t\t\t\t["type"] = "explicit",
\t\t\t},
\t\t},
\t\t["id"] = "explicit",
\t},
}
""",
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    entries = tq.load_local_trade_stats(pob_root=tmp_path, cache_dir=cache_dir)
    assert entries is not None
    assert len(entries) == 2
    assert entries[0] == {
        "id": "pseudo.pseudo_tangled_implicit_tier",
        "text": "Item has # Eater of Worlds Implicit Tier",
        "type": "pseudo",
    }
    assert entries[1]["id"] == "explicit.stat_210067635"

    # Cache JSON file should be created atomically.
    cache_files = list(cache_dir.glob("trade_stats_local_*.json"))
    assert len(cache_files) == 1
    cached_payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert len(cached_payload) == 2

    # Subsequent call with cleared in-memory memo should load from disk cache.
    tq._LOCAL_STATS_MEMO.clear()
    entries_again = tq.load_local_trade_stats(pob_root=tmp_path, cache_dir=cache_dir)
    assert entries_again == entries


def test_load_trade_stats_local_priority_and_remote_fallback(tmp_path: Path) -> None:
    empty_engine = tmp_path / "missing_engine"
    assert tq.load_local_trade_stats(pob_root=empty_engine, cache_dir=tmp_path / "c") is None

    # No local and no remote -> RuntimeError
    with pytest.raises(RuntimeError, match="No trade stats available"):
        tq.load_trade_stats(pob_root=empty_engine, cache_dir=tmp_path / "c")

    # Fallback to remote_fetcher when local absent
    remote_calls = 0

    def fake_remote() -> list[dict[str, object]]:
        nonlocal remote_calls
        remote_calls += 1
        return [{"id": "explicit", "entries": [{"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance"}]}]

    entries, source = tq.load_trade_stats(
        pob_root=empty_engine,
        cache_dir=tmp_path / "c",
        remote_fetcher=fake_remote,
    )
    assert source == "remote_api"
    assert remote_calls == 1
    assert entries[0]["id"] == "explicit.stat_3372524247"

    # When local exists, remote_fetcher must NOT be invoked (zero-network default)
    lua_file = tmp_path / "engine" / "PoE1" / "Data" / "TradeSiteStats.lua"
    lua_file.parent.mkdir(parents=True)
    lua_file.write_text(
        'return {\n\t{\n\t\t["entries"] = {\n\t\t\t{\n\t\t\t\t["id"] = "explicit.stat_4220027924",\n'
        '\t\t\t\t["text"] = "+#% to Cold Resistance",\n\t\t\t\t["type"] = "explicit",\n\t\t\t},\n\t\t},\n\t},\n}\n',
        encoding="utf-8",
    )
    entries2, source2 = tq.load_trade_stats(
        pob_root=tmp_path / "engine",
        cache_dir=tmp_path / "c",
        remote_fetcher=fake_remote,
    )
    assert source2 == "local_engine"
    assert remote_calls == 1  # untouched!
    assert entries2[0]["id"] == "explicit.stat_4220027924"

    # force_refresh=True prefers remote_fetcher when provided
    entries3, source3 = tq.load_trade_stats(
        pob_root=tmp_path / "engine",
        cache_dir=tmp_path / "c",
        remote_fetcher=fake_remote,
        force_refresh=True,
    )
    assert source3 == "remote_api"
    assert remote_calls == 2
    assert entries3[0]["id"] == "explicit.stat_3372524247"


# ---------------------------------------------------------------------------
# 3. Slot-aware (Local)/(Shields) resolution, bilingual ZH, heuristic bug fix
# ---------------------------------------------------------------------------


def test_real_poe1_resistance_and_slot_aware_local_routing() -> None:
    # Resistances -> explicit prioritized over implicit/crafted
    assert tq.resolve_stat("to Fire Resistance", SAMPLE_ENTRIES).id == "explicit.stat_3372524247"
    assert tq.resolve_stat("to Cold Resistance", SAMPLE_ENTRIES).id == "explicit.stat_4220027924"
    assert tq.resolve_stat("to Lightning Resistance", SAMPLE_ENTRIES).id == "explicit.stat_1671376347"

    # Attack Speed: weapon1/bow -> Local (210067635); gloves/ring1 -> Global (681332047)
    assert tq.resolve_stat("#% increased Attack Speed", SAMPLE_ENTRIES, slot="weapon1").id == "explicit.stat_210067635"
    assert tq.resolve_stat("Attack Speed", SAMPLE_ENTRIES, slot="bow").id == "explicit.stat_210067635"
    assert tq.resolve_stat("#% increased Attack Speed", SAMPLE_ENTRIES, slot="gloves").id == "explicit.stat_681332047"
    assert tq.resolve_stat("Attack Speed", SAMPLE_ENTRIES, slot="gloves").id == "explicit.stat_681332047"
    assert tq.resolve_stat("Attack Speed", SAMPLE_ENTRIES, slot="ring1").id == "explicit.stat_681332047"

    # Added Fire Damage: weapon1 -> Local (709508406); gloves -> Global (321077055)
    m_wpn = tq.resolve_stat("Adds # to # Fire Damage", SAMPLE_ENTRIES, slot="weapon1")
    assert m_wpn is not None and m_wpn.id == "explicit.stat_709508406"
    assert m_wpn.template == "adds # to # fire damage"  # scope stripped for Harvest compatibility

    m_glv = tq.resolve_stat("Adds # to # Fire Damage", SAMPLE_ENTRIES, slot="gloves")
    assert m_glv is not None and m_glv.id == "explicit.stat_321077055"

    # Armour & ES: body_armour -> Local; ring1/amulet -> Global
    assert tq.resolve_stat("+# to Armour", SAMPLE_ENTRIES, slot="body_armour").id == "explicit.stat_3484657501"
    assert tq.resolve_stat("+# to Armour", SAMPLE_ENTRIES, slot="ring1").id == "explicit.stat_809229260"
    assert tq.resolve_stat("+# to maximum Energy Shield", SAMPLE_ENTRIES, slot="body_armour").id == "explicit.stat_4052037485"
    assert tq.resolve_stat("+# to maximum Energy Shield", SAMPLE_ENTRIES, slot="amulet").id == "explicit.stat_3489782002"

    # Shield block -> (Shields)
    assert tq.resolve_stat("Chance to Block", SAMPLE_ENTRIES, slot="shield").id == "explicit.stat_4253454700"


def test_bilingual_chinese_stat_parsing_and_resolution() -> None:
    req_cold = tq.parse_stat_argument("冰霜抗性=40")
    assert req_cold.text == "冰霜抗性"
    assert req_cold.min_value == 40.0
    m_cold = tq.resolve_stat(req_cold.text, SAMPLE_ENTRIES, slot="ring1")
    assert m_cold is not None and m_cold.id == "explicit.stat_4220027924"

    req_as = tq.parse_stat_argument("攻击速度=15")
    assert req_as.min_value == 15.0
    assert tq.resolve_stat(req_as.text, SAMPLE_ENTRIES, slot="weapon1").id == "explicit.stat_210067635"
    assert tq.resolve_stat(req_as.text, SAMPLE_ENTRIES, slot="gloves").id == "explicit.stat_681332047"

    req_life = tq.parse_stat_argument("最大生命=70")
    assert req_life.min_value == 70.0
    assert tq.resolve_stat(req_life.text, SAMPLE_ENTRIES, slot="body_armour").id == "explicit.stat_3299347043"

    # Chinese default min without '='
    req_default = tq.parse_stat_argument("法压")
    assert req_default.min_value == 1.0
    assert tq.resolve_stat(req_default.text, SAMPLE_ENTRIES, slot="boots").id == "explicit.stat_3680664274"


def test_tiny_entry_does_not_hijack_long_unrelated_query() -> None:
    # Regression guard for PR #1's broken `entry_template in target -> 0.9` heuristic.
    assert tq.resolve_stat("Completely Fictional Quantum Damage Overdrive", SAMPLE_ENTRIES) is None
    assert tq.resolve_stat("Unicorn Rainbow Life Explosion", SAMPLE_ENTRIES) is None


@pytest.mark.parametrize(
    ("traditional", "simplified"),
    [("能量護盾", "能量护盾"), ("暴擊率", "暴击率"), ("火焰傷害", "火焰伤害"), ("閃避", "闪避"), ("護甲", "护甲")],
)
def test_translate_stat_query_folds_traditional_names(traditional: str, simplified: str) -> None:
    """Traditional names reach the Simplified alias table instead of a fuzzy guess at another stat."""
    assert tq.translate_stat_query(traditional) == tq.ZH_STAT_ALIASES[simplified]


def test_translate_stat_query_ignores_partial_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unaliased Chinese queries must never be rewritten using game_data `match_type == 'partial'` hits."""
    from pob_cli import game_data

    (tmp_path / "PoE1" / "Data").mkdir(parents=True)
    monkeypatch.setattr(tq, "resolve_engine_root", lambda: tmp_path)
    monkeypatch.setattr(
        game_data,
        "query_local_data",
        lambda kind, query, limit=12: {
            "results": [
                {"en": "The First Ones exist,", "zh": "先祖存在", "category": "passive", "match_type": "partial"},
            ]
        },
    )
    assert tq.translate_stat_query("不存在的测试词缀") == "不存在的测试词缀"

    # Exact/substring matches from SQLite still work normally
    monkeypatch.setattr(
        game_data,
        "query_local_data",
        lambda kind, query, limit=12: {
            "results": [
                {"en": "The First Ones exist,", "zh": "先祖存在", "category": "passive", "match_type": "partial"},
                {"en": "+{0} to maximum Mana", "zh": "最大魔力词缀测试", "category": "mod", "match_type": "exact"},
            ]
        },
    )
    assert tq.translate_stat_query("最大魔力词缀测试") == "+# to maximum Mana"


# ---------------------------------------------------------------------------
# 4. Harvest conversion planning & official query builder
# ---------------------------------------------------------------------------


def test_plan_stats_harvest_conversion_and_unique_guard() -> None:
    # Unique + --allow-conversion must raise ValueError
    with pytest.raises(ValueError, match="--allow-conversion cannot be combined with --rarity unique"):
        tq.plan_stats(
            [tq.parse_stat_argument("冰霜抗性=40")],
            SAMPLE_ENTRIES,
            slot="ring1",
            allow_conversion=True,
            rarity="unique",
        )

    # Resistance Harvest expansion
    plan = tq.plan_stats(
        [tq.parse_stat_argument("最大生命=70"), tq.parse_stat_argument("冰霜抗性=40")],
        SAMPLE_ENTRIES,
        slot="ring1",
        allow_conversion=True,
        rarity="rare",
    )
    assert len(plan.plain) == 1
    assert plan.plain[0].match is not None and plan.plain[0].match.id == "explicit.stat_3299347043"
    assert len(plan.conversions) == 1
    conv = plan.conversions[0]
    assert conv.group == "resistance"
    assert conv.target == "cold"
    assert conv.min_value == 40.0
    assert conv.stat_ids == {
        "fire": "explicit.stat_3372524247",
        "cold": "explicit.stat_4220027924",
        "lightning": "explicit.stat_1671376347",
    }

    # Weapon-slot Added Elemental Damage Harvest expansion uses LOCAL IDs for all 3 elements!
    wpn_plan = tq.plan_stats(
        [tq.parse_stat_argument("火焰点伤=50")],
        SAMPLE_ENTRIES,
        slot="weapon1",
        allow_conversion=True,
        convert_to="lightning",
        rarity="rare",
    )
    assert len(wpn_plan.conversions) == 1
    wconv = wpn_plan.conversions[0]
    assert wconv.target == "lightning"
    assert wconv.stat_ids == {
        "fire": "explicit.stat_709508406",
        "cold": "explicit.stat_1037193709",
        "lightning": "explicit.stat_3336890334",
    }


def test_build_query_status_currency_and_corrupted_mirrored_guards() -> None:
    plan_with_conv = tq.plan_stats(
        [tq.parse_stat_argument("最大生命=70"), tq.parse_stat_argument("冰霜抗性=40")],
        SAMPLE_ENTRIES,
        slot="ring1",
        allow_conversion=True,
    )

    # Default status="available", default currency="chaos_equiv" omits price.option,
    # and active conversions inject corrupted=false & mirrored=false.
    body = tq.build_query(
        plan=plan_with_conv,
        category="accessory.ring",
        rarity="rare",
        item_level_min=84,
        budget=150,
        pseudo_filters=[{"id": tq.OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False}],
    )
    q = body["query"]
    assert q["status"] == {"option": "available"}
    assert body["sort"] == {"price": "asc"}

    # Stat groups: plain AND, pseudo AND, conversion COUNT(min=1)
    assert [g["type"] for g in q["stats"]] == ["and", "and", "count"]
    assert q["stats"][1]["filters"][0]["id"] == tq.OPEN_PREFIX_ID
    assert q["stats"][2]["value"] == {"min": 1}
    assert {f["id"] for f in q["stats"][2]["filters"]} == {
        "explicit.stat_3372524247",
        "explicit.stat_4220027924",
        "explicit.stat_1671376347",
    }

    # Price filter omits "option" for chaos_equiv
    assert q["filters"]["trade_filters"] == {"filters": {"price": {"max": 150}}}
    assert q["filters"]["type_filters"] == {
        "filters": {
            "category": {"option": "accessory.ring"},
            "rarity": {"option": "rare"},
            "ilvl": {"min": 84},
        }
    }
    assert q["filters"]["misc_filters"] == {
        "filters": {
            "corrupted": {"option": "false"},
            "mirrored": {"option": "false"},
        }
    }

    # Exact divine currency sets price.option="divine"; allow_corrupted=True omits misc_filters
    body_div = tq.build_query(
        plan=plan_with_conv,
        category="accessory.ring",
        rarity="rare",
        currency="divine",
        budget=5,
        status="securable",
        allow_corrupted=True,
    )
    assert body_div["query"]["status"] == {"option": "securable"}
    assert body_div["query"]["filters"]["trade_filters"] == {
        "filters": {"price": {"option": "divine", "max": 5}}
    }
    assert "misc_filters" not in body_div["query"]["filters"]

    # Invalid status raises ValueError
    with pytest.raises(ValueError, match="Invalid trade status"):
        tq.build_query(plan=plan_with_conv, category=None, rarity="rare", status="bogus")


def test_weighted_sum_group_parsing_and_query_building() -> None:
    grp = tq.parse_sum_group_argument(
        "+#% to Fire Resistance*1.5|+#% to Cold Resistance|+#% to Lightning Resistance=20>=45"
    )
    assert grp.min_sum == 45.0
    assert [(e.text, e.weight, e.min_value) for e in grp.entries] == [
        ("+#% to Fire Resistance", 1.5, None),
        ("+#% to Cold Resistance", 1.0, None),
        ("+#% to Lightning Resistance", 1.0, 20.0),
    ]

    plan = tq.plan_stats(
        [tq.parse_stat_argument("+# to maximum Life=70")],
        SAMPLE_ENTRIES,
        slot="ring1",
        weighted_groups=[grp],
    )
    assert tq.parse_stat_argument("生命>=70") == tq.StatRequest(text="生命", min_value=70.0)
    assert tq.parse_stat_argument("+# to maximum Life >= 75") == tq.StatRequest(
        text="+# to maximum Life", min_value=75.0
    )
    assert len(plan.weighted_groups) == 1
    body = tq.build_query(
        plan=plan,
        category="accessory.ring",
        rarity="rare",
        search_sort="sum",
    )
    stats = body["query"]["stats"]
    # Weighted group placed first at index 0 for statgroup.0 sorting
    assert stats[0] == {
        "type": "weight",
        "value": {"min": 45.0},
        "filters": [
            {"id": "explicit.stat_3372524247", "value": {"weight": 1.5}, "disabled": False},
            {"id": "explicit.stat_4220027924", "value": {"weight": 1.0}, "disabled": False},
            {"id": "explicit.stat_1671376347", "value": {"weight": 1.0, "min": 20.0}, "disabled": False},
        ],
    }
    assert stats[1]["type"] == "and"
    assert body["sort"] == {"statgroup.0": "desc"}

    # --search-sort sum without any --sum group raises cleanly
    empty_plan = tq.plan_stats([], SAMPLE_ENTRIES)
    with pytest.raises(ValueError, match="--search-sort sum requires"):
        tq.build_query(plan=empty_plan, category=None, rarity="rare", search_sort="sum")


# ---------------------------------------------------------------------------
# 5. Real local TradeSiteStats.lua integration tests (gracefully skipped in CI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _local_lua_exists(), reason="Local PoB engine TradeSiteStats.lua not present")
def test_real_local_trade_site_stats_resolution_and_latency() -> None:
    entries, source = tq.load_trade_stats()
    assert source == "local_engine"
    assert len(entries) >= 15000

    # Warm the inverted index once
    assert tq.resolve_stat("最大生命", entries, slot="helmet").id == "explicit.stat_3299347043"

    # Key real-world bilingual + slot-aware assertions + latency check (<15ms warm)
    checks = [
        ("攻击速度", "weapon1", "explicit.stat_210067635"),
        ("攻击速度", "gloves", "explicit.stat_681332047"),
        ("Attack Speed", "weapon1", "explicit.stat_210067635"),
        ("Attack Speed", "gloves", "explicit.stat_681332047"),
        ("火焰点伤", "weapon1", "explicit.stat_709508406"),
        ("火焰点伤", "gloves", "explicit.stat_321077055"),
        ("护甲", "body_armour", "explicit.stat_3484657501"),
        ("护甲", "ring1", "explicit.stat_809229260"),
        ("能量护盾", "body_armour", "explicit.stat_4052037485"),
        ("能量护盾", "amulet", "explicit.stat_3489782002"),
        ("格挡", "shield", "explicit.stat_4253454700"),
        ("冰霜抗性", "ring1", "explicit.stat_4220027924"),
    ]
    for query_text, slot, expected_id in checks:
        t0 = time.perf_counter()
        match = tq.resolve_stat(query_text, entries, slot=slot)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert match is not None, f"Failed to resolve {query_text!r} for slot={slot!r}"
        assert match.id == expected_id, f"{query_text!r} ({slot}): got {match.id} ({match.text}), expected {expected_id}"
        assert elapsed_ms < 25.0, f"resolve_stat({query_text!r}) took {elapsed_ms:.1f}ms"

    # End-to-end weapon local Harvest expansion on the real 18k stat catalog
    plan = tq.plan_stats(
        [tq.parse_stat_argument("攻击速度=15"), tq.parse_stat_argument("火焰点伤=40")],
        entries,
        slot="weapon1",
        allow_conversion=True,
    )
    assert plan.plain[0].match is not None and plan.plain[0].match.id == "explicit.stat_210067635"
    assert plan.conversions[0].stat_ids == {
        "fire": "explicit.stat_709508406",
        "cold": "explicit.stat_1037193709",
        "lightning": "explicit.stat_3336890334",
    }
