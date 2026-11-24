"""Tests for PoE1 3.28/3.30 hardening in game_data.py and guardrails.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from engine_paths import find_engine_data

from pob_cli import game_data
from pob_cli.game_data import (
    SCHEMA_VERSION,
    ensure_gamedata_index,
    get_gem_level_bounds,
    ground_text_entities,
    is_legacy_awakened_gem,
    normalize_tw_query,
    query_local_data,
    query_multi,
)
from pob_cli.guardrails import (
    LEGACY_PATCH_TRAPS_POE1,
    SAME_NAME_FALSE_FRIENDS_POE1,
    GuardrailScope,
    check_input,
    scan_text_traps,
    warning_context,
)

REAL_POB_DIR = find_engine_data()
HAS_REAL_POB = REAL_POB_DIR is not None


# ---------------------------------------------------------------------------
# 1. is_legacy_awakened_gem & get_gem_level_bounds (offline + synthetic + real)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Awakened Spell Echo", True),
        ("Awakened Spell Echo Support", True),
        ("^x8888FFAwakened Elemental Focus Support", True),
        ("^7Awakened Cast On Critical Strike", True),
        ("  awakened   multistrike   support  ", True),
        ("Awakened Added Fire Damage", True),
        ("Awakened Empower", False),
        ("Awakened Empower Support", False),
        ("^7Awakened Enhance", False),
        ("Awakened Enhance Support", False),
        ("Awakened Enlighten", False),
        ("^xFFFFFFAwakened Enlighten Support", False),
        ("Greater Spell Echo", False),
        ("Empower Support", False),
        ("Spell Echo Support", False),
        ("Awakened", False),
        ("", False),
    ],
)
def test_is_legacy_awakened_gem(name: str, expected: bool) -> None:
    assert is_legacy_awakened_gem(name) is expected


@pytest.mark.parametrize(
    "exc_name",
    [
        "Greater Spell Echo",
        "Cooldown Recovery",
        "Eldritch Blasphemy",
        "Hexpass",
        "Hextoad",
        "Congregation",
        "Empower",
        "Enhance",
        "Enlighten",
        "Eclipse",
        "Pyre",
        "Fissure",
        "Gluttony",
        "Hiveborn",
        "Overheat",
        "Pacifism",
        "Bonespire",
        "Communion",
    ],
)
def test_get_gem_level_bounds_offline_exceptional_lv3(exc_name: str, tmp_path: Path) -> None:
    missing_db = tmp_path / "nonexistent.sqlite"
    for variant in (exc_name, f"{exc_name} Support", f"^7{exc_name}"):
        b = get_gem_level_bounds(variant, db_path=missing_db)
        assert b is not None, f"Missing offline fallback for {variant!r}"
        assert b["name"] == exc_name
        assert b["natural_max_level"] == 3
        assert b["corrupted_max_level"] == 4
        assert b["is_exceptional"] is True
        assert b["legacy_removed_328"] is False


@pytest.mark.parametrize("awak_exc", ["Awakened Empower", "Awakened Enhance", "Awakened Enlighten"])
def test_get_gem_level_bounds_offline_awakened_exceptional_lv4(awak_exc: str, tmp_path: Path) -> None:
    missing_db = tmp_path / "nonexistent.sqlite"
    b = get_gem_level_bounds(f"{awak_exc} Support", db_path=missing_db)
    assert b == {
        "name": awak_exc,
        "natural_max_level": 4,
        "corrupted_max_level": 5,
        "is_exceptional": True,
        "legacy_removed_328": False,
    }


def test_get_gem_level_bounds_offline_legacy_awakened_tw_and_unknown(tmp_path: Path) -> None:
    missing_db = tmp_path / "nonexistent.sqlite"

    b_leg = get_gem_level_bounds("^x8888FFAwakened Spell Echo Support", db_path=missing_db)
    assert b_leg == {
        "name": "Awakened Spell Echo",
        "natural_max_level": 5,
        "corrupted_max_level": 6,
        "is_exceptional": False,
        "legacy_removed_328": True,
    }

    # TW normalization before fallback lookup
    assert get_gem_level_bounds("高階施放迴響輔助", db_path=missing_db) == {
        "name": "Greater Spell Echo",
        "natural_max_level": 3,
        "corrupted_max_level": 4,
        "is_exceptional": True,
        "legacy_removed_328": False,
    }
    assert get_gem_level_bounds("覺醒．賦予輔助", db_path=missing_db) == {
        "name": "Awakened Empower",
        "natural_max_level": 4,
        "corrupted_max_level": 5,
        "is_exceptional": True,
        "legacy_removed_328": False,
    }
    b_tw_leg = get_gem_level_bounds("覺醒．施放迴響輔助", db_path=missing_db)
    assert b_tw_leg is not None
    assert b_tw_leg["name"] == "Awakened Spell Echo"
    assert b_tw_leg["natural_max_level"] == 5
    assert b_tw_leg["corrupted_max_level"] == 6
    assert b_tw_leg["legacy_removed_328"] is True

    # Unknown gem returns None when no SQLite is available
    assert get_gem_level_bounds("Nonexistent Skill Gem XYZ", db_path=missing_db) is None
    assert get_gem_level_bounds("", db_path=missing_db) is None


def test_synthetic_gem_indexing_cooldown_stored_uses_exceptional_and_schema_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    rt = tmp_path / "rt"
    poe1 = rt / "engine" / "PoE1"
    data = poe1 / "Data"
    (data / "Skills").mkdir(parents=True, exist_ok=True)
    (data / "Bases").mkdir(parents=True, exist_ok=True)
    (data / "Uniques").mkdir(parents=True, exist_ok=True)

    (poe1 / "manifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><PoBVersion><Version number="2.99.8" /></PoBVersion>',
        encoding="utf-8",
    )
    (data / "Gems.lua").write_text(
        """return {
    ["Metadata/Items/Gems/HexblastAltX"] = {
        name = "Hexblast of Contradiction",
        gameId = "Metadata/Items/Gems/SkillGemHexblast",
        variantId = "HexblastAltX",
        grantedEffectId = "HexblastAltX",
        tags = { intelligence = true, spell = true, },
        tagString = "Spell, Chaos, Hex",
        reqInt = 100,
        naturalMaxLevel = 20,
    },
    ["Metadata/Items/Gems/SupportGreaterSpellEcho"] = {
        name = "Greater Spell Echo",
        gameId = "Metadata/Items/Gems/SupportGemGreaterSpellEcho",
        variantId = "SupportGreaterSpellEcho",
        grantedEffectId = "SupportGreaterSpellEcho",
        tags = { exceptional = true, intelligence = true, support = true, },
        tagString = "Exceptional, Spell, Support",
        reqInt = 100,
        naturalMaxLevel = 3,
    },
    ["Metadata/Items/Gems/SupportAwakenedEmpower"] = {
        name = "Awakened Empower",
        gameId = "Metadata/Items/Gems/SupportGemEmpowerPlus",
        variantId = "SupportAwakenedEmpower",
        grantedEffectId = "SupportAwakenedEmpower",
        tags = { exceptional = true, awakened = true, strength = true, support = true, },
        tagString = "Exceptional, Support",
        reqStr = 100,
        naturalMaxLevel = 4,
    },
    ["Metadata/Items/Gems/SupportAwakenedSpellEcho"] = {
        name = "Awakened Spell Echo",
        gameId = "Metadata/Items/Gems/SupportGemSpellEchoPlus",
        variantId = "SupportAwakenedSpellEcho",
        grantedEffectId = "SupportAwakenedSpellEcho",
        tags = { awakened = true, intelligence = true, support = true, },
        tagString = "Spell, Support",
        reqInt = 100,
        naturalMaxLevel = 5,
    },
}""",
        encoding="utf-8",
    )
    (data / "Skills" / "act_int.lua").write_text(
        """local skills = ...
skills["HexblastAltX"] = {
    name = "Hexblast of Contradiction",
    color = 3,
    castTime = 0.75,
    stats = {},
    levels = {
        [1] = { cooldown = 1, storedUses = 3, critChance = 7.5, levelRequirement = 28, cost = { Mana = 14 }, },
        [20] = { cooldown = 1, storedUses = 3, critChance = 7.5, levelRequirement = 70, cost = { Mana = 26 }, },
    },
}""",
        encoding="utf-8",
    )
    (data / "Skills" / "sup_int.lua").write_text(
        """local skills = ...
skills["SupportGreaterSpellEcho"] = {
    name = "Greater Spell Echo",
    color = 3,
    support = true,
    stats = {},
    levels = {
        [1] = { levelRequirement = 72, manaMultiplier = 50, },
        [3] = { levelRequirement = 72, manaMultiplier = 50, },
        [4] = { levelRequirement = 74, manaMultiplier = 50, },
    },
}
skills["SupportAwakenedSpellEcho"] = {
    name = "Awakened Spell Echo",
    color = 3,
    support = true,
    stats = {},
    levels = {
        [5] = { levelRequirement = 80, manaMultiplier = 50, },
    },
}""",
        encoding="utf-8",
    )
    (data / "Skills" / "sup_str.lua").write_text(
        """local skills = ...
skills["SupportAwakenedEmpower"] = {
    name = "Awakened Empower",
    color = 1,
    support = true,
    stats = {},
    levels = {
        [4] = { levelRequirement = 72, manaMultiplier = 30, },
        [5] = { levelRequirement = 74, manaMultiplier = 30, },
    },
}""",
        encoding="utf-8",
    )

    assert SCHEMA_VERSION >= 8
    db_path = ensure_gamedata_index(runtime_dir=rt)

    # Verify cooldown & stored_uses + bounds returned in query_local_data("gem", ...)
    res_hb = query_local_data("gem", "咒術枯萎．矛盾", runtime_dir=rt)
    assert res_hb["count"] == 1
    hb = res_hb["results"][0]
    assert hb["name"] == "Hexblast of Contradiction"
    assert hb["cooldown"] == 1.0
    assert hb["stored_uses"] == 3
    assert hb["is_exceptional"] is False
    assert hb["legacy_removed_328"] is False
    assert hb["natural_max_level"] == 20
    assert hb["corrupted_max_level"] == 21

    res_gse = query_local_data("gem", "高階施放迴響輔助", runtime_dir=rt)
    assert res_gse["count"] == 1
    gse = res_gse["results"][0]
    assert gse["name"] == "Greater Spell Echo"
    assert gse["is_exceptional"] is True
    assert gse["legacy_removed_328"] is False
    assert gse["natural_max_level"] == 3
    assert gse["corrupted_max_level"] == 4

    res_ae = query_local_data("gem", "覺醒．賦予輔助", runtime_dir=rt)
    assert res_ae["count"] == 1
    ae = res_ae["results"][0]
    assert ae["is_exceptional"] is True
    assert ae["legacy_removed_328"] is False
    assert ae["natural_max_level"] == 4
    assert ae["corrupted_max_level"] == 5

    res_ase = query_local_data("gem", "Awakened Spell Echo", runtime_dir=rt)
    assert res_ase["count"] == 1
    ase = res_ase["results"][0]
    assert ase["is_exceptional"] is False
    assert ase["legacy_removed_328"] is True
    assert ase["natural_max_level"] == 5
    assert ase["corrupted_max_level"] == 6

    # SQLite-backed get_gem_level_bounds works for standard gems too
    assert get_gem_level_bounds("咒術枯萎．矛盾", db_path=db_path) == {
        "name": "Hexblast of Contradiction",
        "natural_max_level": 20,
        "corrupted_max_level": 21,
        "is_exceptional": False,
        "legacy_removed_328": False,
    }

    # Downgrade schema_version to 7 -> ensure_gamedata_index must auto-rebuild to >= 8
    game_data._VALID_INDEX_CACHE.clear()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    rebuilt = ensure_gamedata_index(runtime_dir=rt)
    conn = sqlite3.connect(f"{rebuilt.resolve().as_uri()}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        assert int(meta["schema_version"]) == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. normalize_tw_query (all TW aliases + Trad->Simp char map + pass-through)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tw_input", "expected"),
    [
        ("高階施放迴響輔助", "Greater Spell Echo"),
        ("高階施放迴響", "Greater Spell Echo"),
        ("冷卻恢復輔助", "Cooldown Recovery"),
        ("冷卻恢復", "Cooldown Recovery"),
        ("啟發輔助", "Inspiration"),
        ("啟發", "Inspiration"),
        ("咒閃輔助", "Hexpass"),
        ("咒閃", "Hexpass"),
        ("異界詛咒光環輔助", "Eldritch Blasphemy"),
        ("異界詛咒光環", "Eldritch Blasphemy"),
        ("殘酷輔助", "Cruelty"),
        ("殘酷", "Cruelty"),
        ("虐待輔助", "Sadism"),
        ("虐待", "Sadism"),
        ("犧牲輔助", "Immolate"),
        ("犧牲", "Immolate"),
        ("魔幻輔助", "Archmage"),
        ("魔幻", "Archmage"),
        ("聚眾輔助", "Congregation"),
        ("聚眾", "Congregation"),
        ("活栓輔助", "Lifetap"),
        ("活栓", "Lifetap"),
        ("穿透輔助", "Pierce"),
        ("施放迴響輔助", "Spell Echo"),
        ("施放迴響", "Spell Echo"),
        ("元素集中輔助", "Elemental Focus"),
        ("急凍輔助", "Hypothermia"),
        ("極速苦痛輔助", "Swift Affliction"),
        ("燃燒傷害輔助", "Burning Damage"),
        ("致命異常輔助", "Deadly Ailments"),
        ("暈眩時施放輔助", "Cast when Stunned"),
        ("暈眩時施放", "Cast when Stunned"),
        ("永恆祝福輔助", "Eternal Blessing"),
        ("永恆祝福", "Eternal Blessing"),
        ("持續時間延長輔助", "Increased Duration"),
        ("增加暴擊傷害輔助", "Increased Critical Damage"),
        ("秘能波動輔助", "Arcane Surge"),
        ("投射物返回輔助", "Returning Projectiles"),
        ("喚醒幽魂", "Raise Spectre"),
        ("咒術枯萎．矛盾", "Hexblast of Contradiction"),
        ("怒焰奔騰．燃燒", "Flame Surge of Combusting"),
        ("熔炎送葬．火山", "Cremation of the Volcano"),
        ("烈炎風暴．猛擲", "Firestorm of Pelting"),
        ("屍體爆破．清洗", "Detonate Dead of Scavenging"),
        ("石化之血", "Petrified Blood"),
        ("覺醒．賦予輔助", "Awakened Empower"),
        ("覺醒·賦予輔助", "Awakened Empower"),
        ("覺醒．元素集中輔助", "Awakened Elemental Focus"),
        ("覺醒·元素集中", "Awakened Elemental Focus"),
        ("覺醒．施放迴響輔助", "Awakened Spell Echo"),
        ("覺醒·施放迴響", "Awakened Spell Echo"),
        ("多里亞尼之型", "Doryani's Prototype"),
        ("拒絕命運", "Defiance of Destiny"),
        ("缺血症", "Bloodnotch"),
        ("不變之力", "Immutable Force"),
        ("戰日", "Valyrium"),
        ("憎惡控告者", "The Hateful Accuser"),
        ("烏爾尼多之誓", "Uul-Netol's Vow"),
        ("希比爾之嘆", "Sibyl's Lament"),
        ("無盡飢餓", "Unending Hunger"),
        ("無限飢餓", "Unending Hunger"),
        ("時間之潮", "The Tides of Time"),
        ("時光浪潮", "The Tides of Time"),
        ("史瓦林", "Svalinn"),
        ("銀河星雲", "Nebulis"),
        ("烈炎之袍", "Cloak of Flame"),
        ("不眨之眼", "The Unblinking Eye"),
        ("龍族印記", "Wyrmsign"),
        ("三步突擊", "Three-step Assault"),
        ("阿茲里聖徽", "Atziri's Foible"),
    ],
)
def test_normalize_tw_query_aliases(tw_input: str, expected: str) -> None:
    assert normalize_tw_query(tw_input) == expected


def test_normalize_tw_query_char_fallback_and_sentences() -> None:
    # Trad -> Simp char map fallback (not in alias table)
    assert normalize_tw_query("元素傷害輔助") == "元素伤害辅助"
    assert normalize_tw_query("覺醒．殘暴輔助") == "觉醒·残暴辅助"
    assert normalize_tw_query("藥劑與項鍊裝備") == "药剂与项链装备"

    # Mixed sentence replacement preserves surrounding text & spacing
    sent = normalize_tw_query("我用高階施放迴響輔助配咒術枯萎．矛盾穿多里亞尼之型")
    assert "Greater Spell Echo" in sent
    assert "Hexblast of Contradiction" in sent
    assert "Doryani's Prototype" in sent

    # English & Simplified pass-through untouched
    assert normalize_tw_query("Dark Bargain") == "Dark Bargain"
    assert normalize_tw_query("黑暗交易") == "黑暗交易"
    assert normalize_tw_query("") == ""


# ---------------------------------------------------------------------------
# 3. Real local PoE1 engine DB checks (Hexblast of Contradiction + TW queries)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
def test_real_db_hexblast_cooldown_exceptional_bounds_and_tw_queries() -> None:
    # Hexblast of Contradiction: cooldown=1.0, stored_uses=3
    res_hb = query_local_data("gem", "Hexblast of Contradiction", pob_dir=REAL_POB_DIR, limit=5)
    assert res_hb["count"] >= 1
    hb = res_hb["results"][0]
    assert hb["name"] == "Hexblast of Contradiction"
    assert hb["cooldown"] == 1.0
    assert hb["stored_uses"] == 3
    assert hb["is_exceptional"] is False
    assert hb["legacy_removed_328"] is False
    assert hb["natural_max_level"] == 20
    assert hb["corrupted_max_level"] == 21

    # Exceptional Lv3/4, Awakened Exceptional Lv4/5, Legacy Awakened Lv5/6
    gse = query_local_data("gem", "Greater Spell Echo", pob_dir=REAL_POB_DIR, limit=3)["results"][0]
    assert gse["is_exceptional"] is True
    assert gse["legacy_removed_328"] is False
    assert gse["natural_max_level"] == 3
    assert gse["corrupted_max_level"] == 4

    ae = query_local_data("gem", "Awakened Empower", pob_dir=REAL_POB_DIR, limit=3)["results"][0]
    assert ae["is_exceptional"] is True
    assert ae["legacy_removed_328"] is False
    assert ae["natural_max_level"] == 4
    assert ae["corrupted_max_level"] == 5

    ase = query_local_data("gem", "Awakened Spell Echo", pob_dir=REAL_POB_DIR, limit=3)["results"][0]
    assert ase["is_exceptional"] is False
    assert ase["legacy_removed_328"] is True
    assert ase["natural_max_level"] == 5
    assert ase["corrupted_max_level"] == 6

    # TW queries across gem / unique / translate / multi / ground
    assert query_local_data("gem", "咒術枯萎．矛盾", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Hexblast of Contradiction"
    assert query_local_data("gem", "高階施放迴響輔助", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Greater Spell Echo"
    assert query_local_data("unique", "多里亞尼之型", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Doryani's Prototype"
    assert query_local_data("unique", "拒絕命運", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Defiance of Destiny"
    assert query_local_data("unique", "缺血症", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Bloodnotch"
    assert query_local_data("unique", "戰日", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Valyrium"
    assert query_local_data("unique", "烏爾尼多之誓", pob_dir=REAL_POB_DIR, limit=3)["results"][0]["name"] == "Uul-Netol's Vow"

    tr = query_local_data("translate", "多里亞尼之型", pob_dir=REAL_POB_DIR, limit=5)
    assert tr["count"] >= 1
    assert any(r["en"] == "Doryani's Prototype" for r in tr["results"])

    multi = query_multi(kind="unique", queries=["多里亞尼之型", "戰日"], pob_dir=REAL_POB_DIR, limit=3)
    assert multi["results"]["多里亞尼之型"]["results"][0]["name"] == "Doryani's Prototype"
    assert multi["results"]["戰日"]["results"][0]["name"] == "Valyrium"

    g = ground_text_entities(
        "我用高階施放迴響輔助配咒術枯萎．矛盾，穿多里亞尼之型和拒絕命運，能不能換覺醒．施放迴響輔助？",
        pob_dir=REAL_POB_DIR,
    )
    ent_names = {e["name"] for e in g["entities"]}
    assert "Doryani's Prototype" in ent_names
    assert "Defiance of Destiny" in ent_names
    assert any("Hexblast of Contradiction" in n for n in ent_names)
    patch_ids = {p["ruleId"] for p in g["patchTraps"]}
    assert "PATCH_328_AWAKENED_REMOVED" in patch_ids
    assert "PATCH_330_DORYANI_PROTOTYPE_NEG_RES" in patch_ids


# ---------------------------------------------------------------------------
# 4. guardrails.py updates & 5 new LegacyPatchTrapRule entries
# ---------------------------------------------------------------------------


def test_updated_false_friend_and_cwc_rule_wording() -> None:
    ff_map = {r.rule_id: r for r in SAME_NAME_FALSE_FRIENDS_POE1}
    ff_sock = ff_map["FF_POE1_SOCKETS_DUPLICATE_SUPPORTS"]
    assert "(Awakened 1-5)" not in ff_sock.summary
    assert "Exceptional Supports Lv1-3 / corrupt 4" in ff_sock.summary
    assert "only Awakened Empower/Enhance/Enlighten remain at Lv1-4 / corrupt 5 since 3.28" in ff_sock.summary

    patch_map = {r.rule_id: r for r in LEGACY_PATCH_TRAPS_POE1}
    cwc = patch_map["PATCH_330_CWC_NOT_COOLDOWN"]
    assert "use Awakened CwC" not in cwc.remediation
    assert "Lv5 = 300ms" not in cwc.remediation


@pytest.mark.parametrize(
    ("text", "scope", "expected_rule"),
    [
        # 1. PATCH_328_AWAKENED_REMOVED
        ("Awakened Spell Echo", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("Awakened Spell Echo Support", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("Awakened Elemental Focus", GuardrailScope.DATA_QUERY, "PATCH_328_AWAKENED_REMOVED"),
        ("Awakened Cast On Critical Strike", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("Awakened Multistrike", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("觉醒·施放回响", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("覺醒．施放迴響輔助", GuardrailScope.GEM_NAME, "PATCH_328_AWAKENED_REMOVED"),
        ("觉醒元素集中", GuardrailScope.DATA_QUERY, "PATCH_328_AWAKENED_REMOVED"),
        # 2. PATCH_330_INSPIRATION_MANA_RESET
        ("Inspiration", GuardrailScope.GEM_NAME, "PATCH_330_INSPIRATION_MANA_RESET"),
        ("Inspiration Support", GuardrailScope.DATA_QUERY, "PATCH_330_INSPIRATION_MANA_RESET"),
        ("启迪", GuardrailScope.GEM_NAME, "PATCH_330_INSPIRATION_MANA_RESET"),
        ("啟發輔助", GuardrailScope.DATA_QUERY, "PATCH_330_INSPIRATION_MANA_RESET"),
        # 3. PATCH_330_SADISM_DURATION_TRAP
        ("Sadism", GuardrailScope.GEM_NAME, "PATCH_330_SADISM_DURATION_TRAP"),
        ("Sadism Support", GuardrailScope.DATA_QUERY, "PATCH_330_SADISM_DURATION_TRAP"),
        ("凌虐", GuardrailScope.GEM_NAME, "PATCH_330_SADISM_DURATION_TRAP"),
        ("虐待輔助", GuardrailScope.DATA_QUERY, "PATCH_330_SADISM_DURATION_TRAP"),
        # 4. PATCH_330_AGNOSTIC_CWS_SYNERGY
        ("The Agnostic", GuardrailScope.DATA_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("Valyrium", GuardrailScope.DATA_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("Bloodnotch", GuardrailScope.ITEM_RAW, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("Cast when Stunned", GuardrailScope.GEM_NAME, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("惘信者", GuardrailScope.DATA_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("不可知论", GuardrailScope.CALC_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("缺血症", GuardrailScope.DATA_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        ("戰日", GuardrailScope.DATA_QUERY, "PATCH_330_AGNOSTIC_CWS_SYNERGY"),
        # 5. PATCH_330_DORYANI_PROTOTYPE_NEG_RES
        ("Doryani's Prototype", GuardrailScope.DATA_QUERY, "PATCH_330_DORYANI_PROTOTYPE_NEG_RES"),
        ("Doryani's Prototype", GuardrailScope.ITEM_RAW, "PATCH_330_DORYANI_PROTOTYPE_NEG_RES"),
        ("多里亚尼的试作甲", GuardrailScope.DATA_QUERY, "PATCH_330_DORYANI_PROTOTYPE_NEG_RES"),
        ("多里亞尼之型", GuardrailScope.DATA_QUERY, "PATCH_330_DORYANI_PROTOTYPE_NEG_RES"),
    ],
)
def test_five_new_patch_traps_trigger_correctly(
    text: str, scope: GuardrailScope, expected_rule: str
) -> None:
    with warning_context() as warns:
        returned = check_input(text, scope=scope, expected_game="poe1", emit_stderr=False)
    assert any(w.rule_id == expected_rule for w in returned)
    assert any(w.rule_id == expected_rule for w in warns)

    scanned = scan_text_traps(text)
    assert any(p["ruleId"] == expected_rule for p in scanned["patchTraps"])


@pytest.mark.parametrize(
    ("legal_text", "scope"),
    [
        ("Awakened Empower", GuardrailScope.GEM_NAME),
        ("Awakened Empower Support", GuardrailScope.GEM_NAME),
        ("Awakened Enhance", GuardrailScope.GEM_NAME),
        ("Awakened Enlighten", GuardrailScope.GEM_NAME),
        ("Awakened Enlighten Support", GuardrailScope.DATA_QUERY),
        ("觉醒·赋予", GuardrailScope.GEM_NAME),
        ("覺醒．賦予輔助", GuardrailScope.GEM_NAME),
        ("觉醒·增幅", GuardrailScope.GEM_NAME),
        ("觉醒·启蒙", GuardrailScope.GEM_NAME),
        ("Greater Spell Echo", GuardrailScope.GEM_NAME),
        ("Spell Echo", GuardrailScope.GEM_NAME),
        ("Elemental Focus", GuardrailScope.GEM_NAME),
        ("Inspired Learning", GuardrailScope.DATA_QUERY),
        ("Doryani's Fist", GuardrailScope.DATA_QUERY),
        ("Doryani's Catalyst", GuardrailScope.DATA_QUERY),
        ("Doryani's Invitation", GuardrailScope.ITEM_RAW),
        ("diagnostic report", GuardrailScope.CALC_QUERY),
    ],
)
def test_zero_false_positives_on_new_guardrail_rules(legal_text: str, scope: GuardrailScope) -> None:
    new_rule_ids = {
        "PATCH_328_AWAKENED_REMOVED",
        "PATCH_330_INSPIRATION_MANA_RESET",
        "PATCH_330_SADISM_DURATION_TRAP",
        "PATCH_330_AGNOSTIC_CWS_SYNERGY",
        "PATCH_330_DORYANI_PROTOTYPE_NEG_RES",
    }
    with warning_context() as warns:
        returned = check_input(legal_text, scope=scope, expected_game="poe1", emit_stderr=False)
    assert not ({w.rule_id for w in returned} & new_rule_ids)
    assert not ({w.rule_id for w in warns} & new_rule_ids)

    scanned = scan_text_traps(legal_text)
    assert not ({p["ruleId"] for p in scanned["patchTraps"]} & new_rule_ids)


def test_awakened_sextant_does_not_trigger_awakened_gem_removed() -> None:
    with warning_context():
        warns = check_input("Awakened Sextant", scope=GuardrailScope.DATA_QUERY, expected_game="poe1", emit_stderr=False)
    rids = {w.rule_id for w in warns}
    assert "PATCH_324_SEXTANTS_REMOVED" in rids
    assert "PATCH_328_AWAKENED_REMOVED" not in rids


def test_mixed_prompt_awakened_empower_and_removed_awakened_still_catches_removed() -> None:
    scanned = scan_text_traps("Should I replace Awakened Spell Echo with Awakened Empower?")
    rids = {p["ruleId"] for p in scanned["patchTraps"]}
    assert "PATCH_328_AWAKENED_REMOVED" in rids
