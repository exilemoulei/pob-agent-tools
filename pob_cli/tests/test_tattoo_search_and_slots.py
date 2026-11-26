"""Offline unit tests for 3.30 Tattoo/Runegraft slang search, Did You Mean lock, slot aliases, and bilingual Replaces."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from pob_cli.formatters.gamedata_md import format_game_data
from pob_cli.game_data import _create_schema
from pob_cli.game_data.grounding import _build_zero_result_hint, _suggest_similar
from pob_cli.game_data.tattoos import (
    _insert_tattoos_table,
    _record_matches_mod,
    format_tattoo_replaces,
    query_tattoo_data,
)
from pob_cli.game_data.tw_norm import _fold_search_text

_SYNTH_ROWS: tuple[tuple[Any, ...], ...] = (
    ("RamakoShaman", "Tattoo of the Ramako Shaman", "雷默科萨满的文身", "Small Dexterity", ["+2% chance to Suppress Spell Damage"], ["法术伤害压制率 +2%"], {}),
    ("HinekoraStoryteller", "Tattoo of the Hinekora Storyteller", "辛格拉讲说书人的文身", "Small Intelligence", ["+3% to Chaos Resistance"], ["+3% 混沌抗性"], {}),
    ("ValakoStormrider", "Tattoo of the Valako Stormrider", "瓦拉克驭雷者的文身", "Small Intelligence", ["+6% to Lightning Resistance"], ["+6% 闪电抗性"], {}),
    ("NgamahuFirewalker", "Tattoo of the Ngamahu Firewalker", "努葛玛呼烈焰行者的文身", "Small Strength", ["+6% to Fire Resistance"], ["+6% 火焰抗性"], {}),
    ("ArohonguiMoonwarden", "Tattoo of the Arohongui Moonwarden", "亚欧红古月之守卫的文身", "Small Dexterity", ["+6% to Cold Resistance"], ["+6% 冰霜抗性"], {}),
    ("TawhoaHerbalist", "Tattoo of the Tawhoa Herbalist", "塔赫亚草药师的文身", "Small Dexterity", ["4% increased Flask Effect Duration"], ["药剂效果的持续时间延长 4%"], {}),
    ("TawhoaNaturalist", "Tattoo of the Tawhoa Naturalist", "塔赫亚自然术士的文身", "Small Dexterity", ["8% increased Life Recovery from Flasks"], ["药剂给予的生命恢复提高 8%"], {}),
    ("RongokuraiTurtle", "Tattoo of the Rongokurai Turtle", "隆戈库莱巨龟的文身", "Small Intelligence", ["You take 5% reduced Extra Damage from Critical Strikes"], ["受到的暴击伤害降低 5%"], {}),
    ("TukohamaShaman", "Tattoo of the Tukohama Shaman", "图克哈玛萨满的文身", "Small Strength", ["Regenerate 0.3% of Life per second"], ["每秒再生 0.3% 生命"], {}),
    ("HinekoraShaman", "Tattoo of the Hinekora Shaman", "辛格拉萨满的文身", "Small Intelligence", ["8% increased Mana Regeneration Rate"], ["魔力再生率提高 8%"], {}),
    ("KitavaBloodDrinker", "Tattoo of the Kitava Blood Drinker", "奇塔弗汲血者的文身", "Small Strength", ["0.5% of Attack Damage Leeched as Life"], ["攻击伤害的 0.5% 转化为生命偷取"], {}),
    ("RamakoFleetfoot", "Tattoo of the Ramako Fleetfoot", "雷默科捷足者的文身", "Small Dexterity", ["2% increased Movement Speed"], ["移动速度提高 2%"], {}),
    ("RamakoSniper", "Tattoo of the Ramako Sniper", "雷默科狙击手的文身", "Small Dexterity", ["5% increased Projectile Speed"], ["投射物速度提高 5%"], {}),
    ("TukohamaWarcaller", "Tattoo of the Tukohama Warcaller", "图克哈玛战争司号的文身", "Small Strength", ["8% increased Warcry Cooldown Recovery Rate"], ["战吼冷却回复速度加快 8%"], {}),
    ("ValakoShieldbearer", "Tattoo of the Valako Shieldbearer", "瓦拉克盾卫的文身", "Small Strength", ["+1% Chance to Block Attack Damage"], ["+1% 攻击伤害格挡几率"], {}),
    ("TasalioTideshifter", "Tattoo of the Tasalio Tideshifter", "塔萨里奥潮汐变换者的文身", "Small Dexterity", ["10% chance to Avoid being Stunned"], ["10% 几率避免被晕眩"], {}),
    ("TawhoaScout", "Tattoo of the Tawhoa Scout", "塔赫亚斥候的文身", "Small Dexterity", ["10% chance to Avoid being Poisoned"], ["10% 几率避免中毒"], {}),
    ("KitavaRebel", "Tattoo of the Kitava Rebel", "奇塔弗义军的文身", "Small Strength", ["10% chance to Avoid Bleeding"], ["10% 几率避免致流血"], {}),
    ("ArohonguiScout", "Tattoo of the Arohongui Scout", "亚欧红古斥候的文身", "Small Dexterity", ["10% chance to Avoid being Chilled", "10% chance to Avoid being Frozen"], ["10% 几率避免被冰缓", "10% 几率避免被冰冻"], {}),
    ("HinekoraWarmonger", "Tattoo of the Hinekora Warmonger", "辛格拉好战者的文身", "Small Intelligence", ["Minions have 5% increased maximum Life"], ["召唤生物的最大生命提高 5%"], {}),
    ("JourneyBody", "Journey Tattoo of the Body", None, "Notable", ["+25 to maximum Life per Allocated Journey Tattoo of the Body"], ["配置的每个身躯的旅途文身使生命上限 +25"], {"override_type": "HonouredHeartTattoo", "target_value": "+30 to Strength"}),
    ("JourneyMind", "Journey Tattoo of the Mind", None, "Notable", ["+30 to maximum Mana per Allocated Journey Tattoo of the Mind"], ["配置的每个意志的旅途文身使魔力上限 +30"], {"override_type": "HonouredMindTattoo", "target_value": "+30 to Dexterity"}),
    ("JourneySoul", "Journey Tattoo of the Soul", None, "Notable", ["+40 to maximum Energy Shield per Allocated Journey Tattoo of the Soul"], ["配置的每个灵魂的旅途文身使能量护盾上限 +40"], {"override_type": "HonouredSoulTattoo", "target_value": "+30 to Intelligence"}),
    ("RunegraftTreachery", "Runegraft of Treachery", None, "Mastery", ["15% increased Reservation Efficiency of Skills"], ["技能的保留效能提高 15%"], {"kind": "runegraft", "override_type": "AlternateMastery", "limit_text": "Limited to 1 Runegraft of Treachery"}),
    ("KeystoneAcro", "Acrobatics", "移灵幻影", "Keystone", ["Modifiers to Chance to Suppress Spell Damage instead apply to Chance to Dodge Spell Hits at 50% of their value"], ["法术伤害压制几率词缀改为以 50% 数值套用于法术躲避几率"], {"override_type": "KeystoneTattoo"}),
    ("LegacyMakanga", "Honoured Tattoo of the Makanga", None, "Small Attribute", ["+2% to all maximum Resistances"], ["+2% 全部抗性上限"], {"legacy": True, "min_connected": 7}),
)


def _build_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    recs = [
        {
            "id": rid, "name": nm, "zh_name": zh, "kind": ex.get("kind", "tattoo"),
            "override_type": ex.get("override_type", "Tattoo"), "target_type": tt,
            "target_value": ex.get("target_value", ""), "legacy": ex.get("legacy", False),
            "stats": st, "stats_zh": st_zh, "limit_text": ex.get("limit_text"),
            "limit_text_zh": None, "full_stats": st,
            "min_connected": ex.get("min_connected", 0), "max_connected": ex.get("max_connected", 100),
        }
        for rid, nm, zh, tt, st, st_zh, ex in _SYNTH_ROWS
    ]
    _insert_tattoos_table(conn, recs)
    conn.execute(
        "INSERT INTO gems (id, name, name_lower, display_name_lower, zh_name, zh_lower, is_support, is_vaal, is_transfigured, is_imbued, search_text_lower, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Spellslinger", "Spellslinger", "spellslinger", "spellslinger", "法术节魔", "法术节魔", 0, 0, 0, 0, "spellslinger 法术节魔", "{}"),
    )
    conn.execute(
        "INSERT INTO bases (name, name_lower, zh_name, zh_lower, item_type, req_level, hidden, slots_lower, search_text_lower, data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("Giant Life Flask", "giant life flask", "巨型生命药剂", "巨型生命药剂", "Flask", 1, 0, "flask", "giant life flask 巨型生命药剂", "{}"),
    )
    return conn


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("法术压制", "Tattoo of the Ramako Shaman"), ("法压", "Tattoo of the Ramako Shaman"),
        ("混抗", "Tattoo of the Hinekora Storyteller"), ("混沌抗", "Tattoo of the Hinekora Storyteller"),
        ("电抗", "Tattoo of the Valako Stormrider"), ("火抗", "Tattoo of the Ngamahu Firewalker"),
        ("冰抗", "Tattoo of the Arohongui Moonwarden"), ("三抗", "Tattoo of the Arohongui Moonwarden"),
        ("保留", "Runegraft of Treachery"), ("保留效能", "Runegraft of Treachery"),
        ("药剂持续", "Tattoo of the Tawhoa Herbalist"), ("药水持续", "Tattoo of the Tawhoa Herbalist"),
        ("药水效果", "Tattoo of the Tawhoa Herbalist"), ("药剂生命", "Tattoo of the Tawhoa Naturalist"),
        ("药水生命", "Tattoo of the Tawhoa Naturalist"), ("药水回复", "Tattoo of the Tawhoa Naturalist"),
        ("药剂回复", "Tattoo of the Tawhoa Naturalist"), ("血药", "Tattoo of the Tawhoa Naturalist"),
        ("血药回复", "Tattoo of the Tawhoa Naturalist"), ("暴击减伤", "Tattoo of the Rongokurai Turtle"),
        ("防暴", "Tattoo of the Rongokurai Turtle"), ("减暴伤", "Tattoo of the Rongokurai Turtle"),
        ("暴伤", "Tattoo of the Rongokurai Turtle"), ("秒回", "Tattoo of the Tukohama Shaman"),
        ("回蓝", "Tattoo of the Hinekora Shaman"), ("吸血", "Tattoo of the Kitava Blood Drinker"),
        ("移速", "Tattoo of the Ramako Fleetfoot"), ("投速", "Tattoo of the Ramako Sniper"),
        ("战吼冷却", "Tattoo of the Tukohama Warcaller"), ("攻击格挡", "Tattoo of the Valako Shieldbearer"),
        ("免晕", "Tattoo of the Tasalio Tideshifter"), ("免毒", "Tattoo of the Tawhoa Scout"),
        ("免流血", "Tattoo of the Kitava Rebel"), ("免冰", "Tattoo of the Arohongui Scout"),
        ("免冻", "Tattoo of the Arohongui Scout"),
    ],
)
def test_slang_and_colloquial_queries_hit_in_search_and_mod(query: str, expected: str) -> None:
    conn = _build_db()
    try:
        assert expected in [r["name"] for r in query_tattoo_data(conn, query, limit=25)]
        assert expected in [r["name"] for r in query_tattoo_data(conn, "", mod_filter=query, limit=25)]
    finally:
        conn.close()


def test_particle_less_names_and_cjk_bigram_fallback() -> None:
    conn = _build_db()
    try:
        for q, expected in (
            ("巨龟纹身", "Tattoo of the Rongokurai Turtle"),
            ("巨龟文身", "Tattoo of the Rongokurai Turtle"),
            ("草药师纹身", "Tattoo of the Tawhoa Herbalist"),
            ("自然术士纹身", "Tattoo of the Tawhoa Naturalist"),
            ("烈焰行者纹身", "Tattoo of the Ngamahu Firewalker"),
        ):
            hits = query_tattoo_data(conn, q)
            assert hits and hits[0]["name"] == expected
        for q, expected in (
            ("药剂持续时间", "Tattoo of the Tawhoa Herbalist"),
            ("药水持续时间", "Tattoo of the Tawhoa Herbalist"),
            ("药剂持续时", "Tattoo of the Tawhoa Herbalist"),
            ("召唤生命", "Tattoo of the Hinekora Warmonger"),
        ):
            hits = query_tattoo_data(conn, q)
            assert expected in [r["name"] for r in hits] and _record_matches_mod(hits[0], q)
        assert query_tattoo_data(conn, "不存在的假词缀xyz") == []
    finally:
        conn.close()


def test_zero_hit_suggestions_stay_inside_tattoos_and_cover_variants() -> None:
    conn = _build_db()
    try:
        for q in ("不存在的假词缀xyz", "法术节魔", "巨型生命药剂"):
            sugs = _suggest_similar(conn, "tattoo", _fold_search_text(q))
            assert all(s["name"] not in ("Spellslinger", "Giant Life Flask") for s in sugs)
        # Official Tatto.json uses '文身'; typos using '纹身' must still suggest the tattoo
        sugs_wen = _suggest_similar(conn, "tattoo", _fold_search_text("烈焰行者纹生"))
        assert sugs_wen and sugs_wen[0]["name"] == "Tattoo of the Ngamahu Firewalker"
        # Runegraft with zh_lower=NULL falls back to search_text_lower for CJK Did You Mean
        sugs_rg = _suggest_similar(conn, "runegraft", _fold_search_text("保留效能假词"))
        assert sugs_rg and sugs_rg[0]["name"] == "Runegraft of Treachery"
        hint = _build_zero_result_hint("tattoo", "不存在的假词缀xyz", [])
        assert "pob data tattoo" in hint and "--legacy" in hint and "Spellslinger" not in hint
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("alias", "expected_types", "forbidden_names"),
    [
        ("small_str", {"Small Strength"}, {"Journey Tattoo of the Body"}),
        ("str", {"Small Strength"}, {"Journey Tattoo of the Body"}),
        ("力量小点", {"Small Strength"}, {"Journey Tattoo of the Body"}),
        ("+10 str", {"Small Strength"}, {"Journey Tattoo of the Body"}),
        ("small_dex", {"Small Dexterity"}, {"Journey Tattoo of the Mind"}),
        ("dex", {"Small Dexterity"}, {"Journey Tattoo of the Mind"}),
        ("敏捷小点", {"Small Dexterity"}, {"Journey Tattoo of the Mind"}),
        ("small_int", {"Small Intelligence"}, {"Journey Tattoo of the Soul"}),
        ("int", {"Small Intelligence"}, {"Journey Tattoo of the Soul"}),
        ("智慧小点", {"Small Intelligence"}, {"Journey Tattoo of the Soul"}),
        ("small_attr", {"Small Strength", "Small Dexterity", "Small Intelligence", "Small Attribute"}, {"Journey Tattoo of the Mind"}),
        ("notable_attr", {"Notable"}, {"Tattoo of the Tawhoa Naturalist"}),
        ("+30", {"Notable"}, {"Tattoo of the Tawhoa Naturalist"}),
        ("属性大点", {"Notable"}, {"Tattoo of the Tawhoa Naturalist"}),
        ("mastery", {"Mastery"}, {"Tattoo of the Tawhoa Naturalist"}),
        ("keystone", {"Keystone"}, {"Tattoo of the Tawhoa Naturalist"}),
    ],
)
def test_slot_aliases_disambiguate_small_vs_notable(alias: str, expected_types: set[str], forbidden_names: set[str]) -> None:
    conn = _build_db()
    try:
        res = query_tattoo_data(conn, "", slot_filter=alias, include_legacy=(alias == "small_attr"), limit=0)
        assert res and {r["target_type"] for r in res} <= expected_types
        assert not (forbidden_names & {r["name"] for r in res})
        # Exact `small_attribute` targets only retired 3.22 Small Attribute tattoos (0 active, hits with --legacy).
        assert query_tattoo_data(conn, "", slot_filter="small_attribute", include_legacy=False) == []
        leg_sa = query_tattoo_data(conn, "", slot_filter="small attribute", include_legacy=True)
        assert leg_sa and {r["target_type"] for r in leg_sa} == {"Small Attribute"}
    finally:
        conn.close()


def test_bilingual_replaces_fields_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _build_db()
    try:
        rec = query_tattoo_data(conn, "Tattoo of the Tawhoa Naturalist")[0]
        assert rec["replaces_zh"] == "+10 敏捷小点"
        assert rec["replaces_display"] == "Small Dexterity (+10 敏捷小点)"
        assert format_tattoo_replaces("Notable", "+30 to Dexterity")[0] == "Notable: +30 to Dexterity (+30 敏捷大点)"
        assert format_tattoo_replaces("Mastery", "")[0] == "Mastery (专精节点)"
        assert format_tattoo_replaces("Keystone", "")[0] == "Keystone (基石天赋)"
        assert format_tattoo_replaces("Small Attribute", "")[0] == "Small Attribute (+10 任意属性小点)"
        monkeypatch.setenv("POB_LANG", "zh-CN")
        assert "Small Dexterity (+10 敏捷小点)" in format_game_data({"kind": "tattoo", "query": "q", "results": [rec]}, kind="tattoo")
        monkeypatch.setenv("POB_LANG", "en")
        md_en = format_game_data({"kind": "tattoo", "query": "q", "results": [rec]}, kind="tattoo")
        assert "Small Dexterity" in md_en and "+10 敏捷小点" not in md_en
    finally:
        conn.close()
