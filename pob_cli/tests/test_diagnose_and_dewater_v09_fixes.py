"""Offline unit tests for v0.9.x diagnose output fixes, smart dewatering auto-keep, and support filtering."""

from __future__ import annotations

from pob_cli.analyzer.dewater_auto import infer_auto_keep_and_flask_notes
from pob_cli.analyzer.dewater_steps import _dewater_config_and_custom_mods
from pob_cli.comparator.gem_caveats import (
    _check_support_hard_conflict,
    _gem_caveat,
    compute_sadism_per_ailment_pct,
)
from pob_cli.formatters.diagnose_compare_md import (
    _filter_duplicate_md_advisories,
    _fmt_ask_grounding_line,
)
from pob_cli.formatters.diagnose_md import format_diagnose_markdown
from pob_cli.game_data.fact_cards import (
    _dedup_and_prioritize_unique_lines,
    _format_gem_fact_card,
    _format_unique_fact_card,
    normalize_stat_lines,
)


def test_unique_fact_card_prefers_equipped_item_and_dedups_variants() -> None:
    db_cloak = {
        "name": "Cloak of Flame",
        "zh_name": "烈炎之袍",
        "base_type": "Scholar's Robe",
        "variants": ["Pre 3.25.0", "Current"],
        "explicits": [
            "{variant:1}+(30-50)% to Fire Resistance",
            "{variant:2}+(50-75)% to Fire Resistance",
            "{variant:1}(30-50)% increased Ignite Duration on Enemies",
            "{variant:2}(40-75)% increased Ignite Duration on Enemies",
            "40% of Physical Damage from Hits taken as Fire Damage",
        ],
    }
    card_db = _format_unique_fact_card(db_cloak)
    assert card_db["explicits"] == [
        "(40-75)% increased Ignite Duration on Enemies",
        "40% of Physical Damage from Hits taken as Fire Damage",
        "+(50-75)% to Fire Resistance",
    ]

    equipped_override = {
        "name": "Valyrium",
        "baseType": "Moonstone Ring",
        "explicits": [
            "+38 to maximum Energy Shield",
            "+35% to Fire Resistance",
            "Stun Threshold is based on Energy Shield instead of Life",
        ],
    }
    card_eq = _format_unique_fact_card({"name": "Valyrium", "explicits": ["junk"]}, equipped_override=equipped_override)
    assert card_eq["fromBuild"] is True
    assert "Stun Threshold is based on Energy Shield instead of Life" in card_eq["explicits"]

    valyrium_multi = [
        "{variant:1}+10 to maximum Energy Shield",
        "{variant:2}+(30-40) to maximum Energy Shield",
        "{variant:1}+20% to Fire Resistance",
        "{variant:2}+(30-40)% to Fire Resistance",
        "-40% to Cold Resistance",
        "Stun Threshold is based on Energy Shield instead of Life",
    ]
    prioritized = _dedup_and_prioritize_unique_lines(valyrium_multi)
    assert "Stun Threshold is based on Energy Shield instead of Life" in prioritized[:4]


def test_stat_newline_stitching_and_gem_internal_stat_filtering() -> None:
    ramako_lines = [
        "Nearby Enemies' Fire Resistance against\nDamage over Time is -20% while you are Stationary",
        "Nearby Enemies' Fire Resistance against",
        "Damage over Time is -20%",
    ]
    norm = normalize_stat_lines(ramako_lines)
    assert norm == [
        "Nearby Enemies' Fire Resistance against Damage over Time is -20% while you are Stationary",
        "Nearby Enemies' Fire Resistance against Damage over Time is -20%",
    ]

    gem_card = _format_gem_fact_card(
        {
            "name": "Detonate Dead",
            "levels": {
                "20": {
                    "stats": {
                        "base_skill_show_average_damage_instead_of_dps": 1,
                        "quality_display_active_skill_ignite_damage_is_gem": 1,
                        "spell_minimum_base_fire_damage": 850,
                        "spell_maximum_base_fire_damage": 1275,
                    }
                }
            },
            "quality_stats": [
                {"stat": "quality_display_active_skill_is_gem", "per_quality": 1, "at_20_quality": 20},
                {"stat": "burn_damage_+%", "per_quality": 0.5, "at_20_quality": 10},
            ],
        }
    )
    assert "base_skill_show_average_damage_instead_of_dps" not in gem_card["lv20Stats"]
    assert "quality_display_active_skill_ignite_damage_is_gem" not in gem_card["lv20Stats"]
    assert gem_card["lv20Stats"]["spell_minimum_base_fire_damage"] == 850
    assert len(gem_card["quality20"]) == 1


def test_support_hard_conflicts_trigger_caveat_and_dynamic_sadism() -> None:
    ignite_stats = {"IgniteDPS": 2_500_000.0, "HitDPS": 120_000.0, "FireDamage": 40_000.0, "ColdDamage": 0.0}
    assert _check_support_hard_conflict("Elemental Focus Support", ignite_stats) is not None
    assert _check_support_hard_conflict("Hypothermia Support", ignite_stats) is not None
    assert _check_support_hard_conflict("Brutality Support", ignite_stats) is not None
    assert _check_support_hard_conflict("Swift Affliction Support", ignite_stats) is None

    # Chill from Skitterbots makes Hypothermia legal
    skitter_skills = {"groups": [{"enabled": True, "slot": "Helm", "gems": [{"name": "Summon Skitterbots", "enabled": True}]}]}
    assert _check_support_hard_conflict("Hypothermia Support", ignite_stats, skills_res=skitter_skills) is None

    pct = compute_sadism_per_ailment_pct()
    assert -45 <= pct <= -38
    sadism_note = _gem_caveat("Sadism Support") or ""
    assert "-74%" not in sadism_note
    assert f"{pct:+d}%" in sadism_note

    cdr_note = _gem_caveat("Cooldown Recovery Support", group_has_trigger=True)
    assert cdr_note is not None and "trigger" in cdr_note.lower()


def test_smart_dewater_auto_keep_and_question_grounding_dedup() -> None:
    auto_keep, flask_notes = infer_auto_keep_and_flask_notes(
        stats={"IgniteChance": 100.0, "MinimumEnduranceCharges": 0.0},
        skills_res={
            "groups": [
                {
                    "enabled": True,
                    "gems": [{"name": "Cast when Stunned Support", "skillId": "SupportCastWhenStunned", "enabled": True}],
                }
            ]
        },
        items_res={
            "items": [{"id": 1, "title": "Flagellant's Ruby Flask", "raw": "Gain 3 Charges when you are Hit by an Enemy"}],
            "slots": [{"name": "Flask 1", "isFlask": True, "active": True, "selItemId": 1}],
        },
        tree_summary={"ascendancyNodes": [{"id": 61355, "name": "Ramako, Sun's Light"}], "notables": [{"name": "Inexorable"}]},
    )
    assert "conditionenemyignited" in auto_keep
    assert "conditionstationary" in auto_keep
    assert "useendurancecharges" in auto_keep
    assert "Flask 1" in flask_notes

    cfg_res = {
        "sections": [
            {
                "items": [
                    {"var": "conditionEnemyIgnited", "value": True},
                    {"var": "conditionStationary", "value": 1},
                    {"var": "usePowerCharges", "value": True},
                ]
            }
        ]
    }
    stripped: dict[str, list[str]] = {"config": [], "flasks": [], "skills": [], "kept": [], "autoKept": []}
    mutations: list[tuple[str, dict[str, object]]] = []
    records: list[dict[str, object]] = []
    _dewater_config_and_custom_mods(
        cfg_res, set(), {}, stripped, mutations, records, auto_keep_reasons=auto_keep
    )
    assert any("conditionEnemyIgnited" in s for s in stripped["autoKept"])
    assert any("conditionStationary" in s for s in stripped["autoKept"])
    assert any("usePowerCharges" in s for s in stripped["config"])

    # Empty Question Grounding omitted; duplicate Ramako advisory suppressed
    assert _fmt_ask_grounding_line({"askGrounding": {"query": "怎么提升点燃伤害和生存", "resolvedSlang": [], "addedEntities": []}}) is None
    deduped_adv = _filter_duplicate_md_advisories(
        {"core_synergies": ["[RAMAKO FIXED -20% DOT RES] Stationary Fire Res -20%"]},
        [{"ruleId": "PATCH_330_RAMAKO_FIXED_DOT_RES", "tag": "[POE1-PATCH-WARN]", "trigger": "Ramako", "message": "dup"}],
    )
    assert deduped_adv == []

    md = format_diagnose_markdown(
        {
            "env": {"slot": "main", "patch": "3.30", "pobVersion": "2.69.0", "dataVersion": "3.30", "rev": 1},
            "buildInfo": {"level": 97, "className": "Marauder", "ascendClassName": "Chieftain", "points": {"used": 120, "usedMax": 120}},
            "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 2_000_000, "totalEhp": 80_000},
            "dewateredPinnacle": {"primaryDps": 1_800_000, "totalEhp": 75_000},
            "inflationStripped": stripped,
            "topNodes": {
                "stat": "CombinedDPS",
                "baselineLabel": "Dewatered Pinnacle",
                "maxDist": 5,
                "byPerPoint": [{"id": 48660, "effectId": 17942, "allocSpec": "48660:17942", "name": "Fire Mastery: +20% Fire DoT Multiplier", "pathDist": 1, "pathPower": 5.2, "power": 5.2}],
            },
            "topGems": {
                "dpsField": "CombinedDPS",
                "baselineLabel": "Dewatered Pinnacle",
                "mode": "add_link (slot #6)",
                "candidates": [{"name": "Swift Affliction Support", "nameZh": "极速腐化(辅)", "delta": 200_000, "pct": 11.1}],
                "excludedSupports": [{"name": "Elemental Focus Support", "reason": "Cannot inflict Elemental Ailments (bricks Ignite)"}],
            },
        }
    )
    assert "Auto-Kept" in md
    assert "48660:17942" in md
    assert "@ Dewatered Pinnacle" in md
    assert "Excluded conflicting supports: Elemental Focus Support" in md
