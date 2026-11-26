"""Unit and regression tests for v0.10.1 Layer 2 wild-build diagnose accuracy & CLI hook removal."""

from __future__ import annotations

from typing import Any

import click
import pytest
from click.testing import CliRunner

from pob_cli import analyzer, cli
from pob_cli.guardrails import GuardrailScope, check_input, warning_context
from pob_cli.guardrails.rules_false_friends import SAME_NAME_FALSE_FRIENDS_POE1

_ZERO_STATS: dict[str, Any] = {
    "CombinedDPS": 0.0,
    "FullDPS": 0.0,
    "TotalDPS": 0.0,
    "TotalDot": 0.0,
    "TotalDotDPS": 0.0,
    "MinionCombinedDPS": 0.0,
    "MinionTotalDPS": 0.0,
    "ManaCost": 25.0,
}


# ---------------------------------------------------------------------------
# 1. Ramako Tattoo False-Positive Fix vs Real Chieftain Preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tattoo_label",
    [
        "Tattoo of the Ramako Sniper",
        "Tattoo of the Ramako Fleetfoot",
        "Tattoo of the Ramako Shaman",
        "Runegraft of the River",
        "雷默科狙击手纹身",
        "雷默科飛毛腿的刺青",
    ],
)
def test_ramako_tattoos_never_emit_chieftain_ramako_synergy(tattoo_label: str) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Lightning Arrow", "enabled": True, "support": False}],
            }
        ],
    }
    alloc_nodes = [(tattoo_label, ["5% increased Projectile Speed"])]

    # Case A: populated non-Chieftain tree_summary (e.g. Deadeye)
    _, mech_deadeye = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"CombinedDPS": 2_500_000.0, "ManaCost": 28.0},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary={
            "keystones": [{"id": 1001, "name": "Point Blank"}],
            "ascendancyNodes": [{"id": 2001, "name": "Gathering Winds", "asc": "Deadeye"}],
        },
        alloc_node_stats=alloc_nodes,
        build_info={"ascendClassName": "Deadeye"},
    )
    assert not any(
        "[RAMAKO FIXED -20% DOT RES]" in s for s in (mech_deadeye.get("coreSynergies") or [])
    )

    # Case B: tree_summary=None fallback
    _, mech_fallback = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"CombinedDPS": 2_500_000.0, "ManaCost": 28.0},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary=None,
        alloc_node_stats=alloc_nodes,
    )
    assert not any(
        "[RAMAKO FIXED -20% DOT RES]" in s for s in (mech_fallback.get("coreSynergies") or [])
    )


@pytest.mark.parametrize(
    ("tree_summary", "alloc_nodes"),
    [
        (
            {"ascendancyNodes": [{"id": 61355, "name": "Ramako, Sun's Light", "asc": "Chieftain"}]},
            [("Tattoo of the Ramako Fleetfoot", ["2% increased Movement Speed"])],
        ),
        (
            {"ascendancyNodes": [{"name": "Ramako, Sun's Light", "asc": "Chieftain"}]},
            [],
        ),
        (
            {"ascendancyNodes": [{"name": "雷默科，太阳之光", "asc": "Chieftain"}]},
            [],
        ),
        (
            {"keystones": [], "ascendancyNodes": []},
            [("Ramako, Sun's Light", ["Nearby Enemies' Fire Resistance against Damage over Time is -20%"])],
        ),
    ],
)
def test_real_chieftain_ramako_suns_light_still_emits_synergy(
    tree_summary: dict[str, Any],
    alloc_nodes: list[tuple[str, list[str]]],
) -> None:
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"CombinedDPS": 1_800_000.0, "ManaCost": 0.0},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary=tree_summary,
        alloc_node_stats=alloc_nodes,
    )
    syns = mech.get("coreSynergies") or []
    assert any("[RAMAKO FIXED -20% DOT RES]" in s for s in syns)


# ---------------------------------------------------------------------------
# 2. FF_POE1_MAGEBLOOD: Standard Leftmost + 3.30 Foulborn Rightmost
# ---------------------------------------------------------------------------


def test_ff_poe1_mageblood_covers_leftmost_slot_skipping_and_foulborn_rightmost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mb_rule = next(r for r in SAME_NAME_FALSE_FRIENDS_POE1 if r.rule_id == "FF_POE1_MAGEBLOOD")
    for text in (mb_rule.summary, mb_rule.poe1_behavior):
        assert "Leftmost" in text
        assert "Rightmost" in text
        assert "Foulborn Mageblood" in text
        assert "秽生 法师之血" in text
        assert "Unique" in text

    for query, scope in (
        ("Mageblood", GuardrailScope.DATA_QUERY),
        ("Foulborn Mageblood", GuardrailScope.ITEM_RAW),
        ("秽生 法师之血", GuardrailScope.DATA_QUERY),
    ):
        with warning_context() as warns:
            returned = check_input(query, scope=scope, expected_game="poe1")
        mb_warns = [w for w in returned if w.rule_id == "FF_POE1_MAGEBLOOD"]
        assert mb_warns, f"Expected FF_POE1_MAGEBLOOD for {query!r}"
        assert any(w.rule_id == "FF_POE1_MAGEBLOOD" for w in warns)
        assert "Leftmost" in mb_warns[0].summary
        assert "Rightmost" in mb_warns[0].summary
        assert "Foulborn" in mb_warns[0].summary
        assert "Leftmost" in mb_warns[0].poe1_behavior
        assert "Rightmost" in mb_warns[0].poe1_behavior
    _ = capsys.readouterr()


# ---------------------------------------------------------------------------
# 3. Structured 0-DPS Diagnostic Hints: Empty <Spectres> & Link / Luminary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gem_entry",
    [
        {"name": "Raise Spectre", "enabled": True, "support": False},
        {"name": "Raise Spectre of Transience", "nameZh": "瞬息之召唤灵体", "enabled": True, "support": False},
        {"name": "召唤灵体", "enabled": True, "support": False},
        {"name": "喚醒幽魂", "enabled": True, "support": False},
    ],
)
def test_zero_dps_raise_spectre_emits_high_minion_config_gap(gem_entry: dict[str, Any]) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    gem_entry,
                    {"name": "Minion Damage", "level": 20, "enabled": True, "support": True},
                ],
            }
        ],
    }
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats=dict(_ZERO_STATS),
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    minion_gaps = [g for g in gaps if g["category"] == "MinionConfig" and g["severity"] == "HIGH"]
    assert len(minion_gaps) == 1
    msg = minion_gaps[0]["message"]
    assert "<Spectres>" in msg and "Manage Spectres" in msg
    assert "NOT because support links are broken" in msg


def test_zero_dps_raise_spectre_in_full_dps_secondary_group_and_nonzero_suppression() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Boots",
                "enabled": True,
                "gems": [{"name": "Convocation", "enabled": True, "support": False}],
            },
            {
                "index": 2,
                "isMain": False,
                "slot": "Helmet",
                "enabled": True,
                "includeInFullDPS": True,
                "gems": [{"name": "Raise Spectre", "enabled": True, "support": False}],
            },
        ],
    }
    gaps_zero, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats=dict(_ZERO_STATS),
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert any(g["category"] == "MinionConfig" and g["severity"] == "HIGH" for g in gaps_zero)

    # When Spectres ARE configured and MinionCombinedDPS > 0, no empty-<Spectres> gap is emitted
    gaps_populated, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={**_ZERO_STATS, "MinionCombinedDPS": 4_200_000.0},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert not any(g["category"] == "MinionConfig" for g in gaps_populated)


@pytest.mark.parametrize(
    ("main_gem", "asc_nodes"),
    [
        ({"name": "Destructive Link", "enabled": True, "support": False}, []),
        ({"name": "Flame Link", "enabled": True, "support": False}, []),
        ({"name": "Protective Link", "enabled": True, "support": False}, []),
        ({"name": "毁灭羁绊", "enabled": True, "support": False}, []),
        ({"name": "連結：直覺", "enabled": True, "support": False}, []),
        (
            {"name": "Determination", "enabled": True, "support": False},
            [{"id": 90001, "name": "Luminary"}],
        ),
        (
            {"name": "纯净之捷", "enabled": True, "support": False},
            [{"id": 90002, "name": "光明使者"}],
        ),
    ],
)
def test_zero_dps_link_and_luminary_support_archetype_hint(
    main_gem: dict[str, Any],
    asc_nodes: list[dict[str, Any]],
) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [main_gem],
            }
        ],
    }
    gaps, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats=dict(_ZERO_STATS),
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary={"keystones": [], "ascendancyNodes": asc_nodes},
    )
    sup_gaps = [g for g in gaps if g["category"] == "SupportArchetype" and g["severity"] == "INFO"]
    assert len(sup_gaps) == 1
    msg = sup_gaps[0]["message"]
    assert "CombinedDPS = 0" in msg
    assert "[PoB未模拟]" in msg
    assert "2.69.0" in msg
    assert any("[SUPPORT ARCHETYPE]" in s and "[PoB未模拟]" in s for s in (mech.get("coreSynergies") or []))


def test_support_archetype_no_false_positive_on_searing_bond_or_offensive_luminary() -> None:
    # Searing Bond (`灼熱連結`) is a totem DoT skill, NOT a Link support skill
    gaps_sb, mech_sb = analyzer._detect_build_mechanics_and_pitfalls(
        stats=dict(_ZERO_STATS),
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 1,
                    "isMain": True,
                    "enabled": True,
                    "gems": [{"name": "Searing Bond", "nameZh": "灼熱連結", "enabled": True, "support": False}],
                }
            ],
        },
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert not any(g["category"] == "SupportArchetype" for g in gaps_sb)
    assert not any("[SUPPORT ARCHETYPE]" in s for s in (mech_sb.get("coreSynergies") or []))

    # Luminary with direct offensive main skills (Fireball or Charged Dash) must NOT be masked as support
    for off_gem in ("Fireball", "Charged Dash"):
        gaps_off, _ = analyzer._detect_build_mechanics_and_pitfalls(
            stats=dict(_ZERO_STATS),
            skills_res={
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "gems": [{"name": off_gem, "enabled": True, "support": False}],
                    }
                ],
            },
            items_res={"slots": [], "items": []},
            cfg_res={},
            tree_summary={"keystones": [], "ascendancyNodes": [{"name": "Luminary"}]},
        )
        assert not any(g["category"] == "SupportArchetype" for g in gaps_off), off_gem

    # Bare `Dash` + tree-extracted `asc="Luminary"` (without build_info) IS recognized as SupportArchetype
    gaps_dash_lum, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats=dict(_ZERO_STATS),
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 1,
                    "isMain": True,
                    "enabled": True,
                    "gems": [{"name": "Dash", "enabled": True, "support": False}],
                }
            ],
        },
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary={"keystones": [], "ascendancyNodes": [{"id": 99, "name": "Radiant Faith", "asc": "Luminary"}]},
    )
    assert any(g["category"] == "SupportArchetype" for g in gaps_dash_lum)


# ---------------------------------------------------------------------------
# 4. L1 CLI `hook` Removal Regression Guard
# ---------------------------------------------------------------------------


def test_cli_hook_subcommand_removed() -> None:
    assert "hook" not in cli.main.commands
    assert "hook" not in cli._LAZY_SUBCOMMANDS
    ctx = click.Context(cli.main)
    assert cli.main.get_command(ctx, "hook") is None

    res = CliRunner().invoke(cli.main, ["hook"])
    assert res.exit_code == 2
    assert "No such command" in res.output
