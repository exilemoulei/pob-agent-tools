"""Tests for PoE1 3.30 Wickerman/Runegraft customMods dewater classification, Convocation trigger guard, and skill docs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pob_cli.analyzer.dewater_steps import _dewater_config_and_custom_mods
from pob_cli.analyzer.pitfalls_core import _detect_build_mechanics_and_pitfalls
from pob_cli.formatters.build_md import (
    format_compare_markdown,
    format_matrix_markdown,
    format_sweep_markdown,
)
from pob_cli.formatters.diagnose_md import format_diagnose_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_empty_stripped() -> dict[str, list[str]]:
    return {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
        "kept": [],
        "autoKept": [],
        "keepRejected": [],
    }


def test_custom_mods_skips_double_slash_and_hash_comments_only_block() -> None:
    cfg_res: dict[str, Any] = {
        "sections": [],
        "customMods": [
            {
                "title": "CommentsOnly",
                "text": "# PoB section header\n  // 3.30 note only\n   \n# trailing",
                "enabled": True,
            }
        ],
    }
    stripped = _make_empty_stripped()
    known: dict[str, str] = {}
    mutations: list[tuple[str, dict[str, Any]]] = []
    records: list[dict[str, Any]] = []

    _dewater_config_and_custom_mods(cfg_res, set(), known, stripped, mutations, records)

    assert not records
    assert "customMods330Notes" not in stripped
    assert not any("customMods[" in ln for ln in stripped["config"])


def test_custom_mods_mixed_330_viscera_runegraft_and_unverified_padding() -> None:
    cfg_res: dict[str, Any] = {
        "sections": [],
        "customMods": [
            {
                "title": "Default",
                "text": (
                    "// 3.30 Runegraft of the Bound + PoB padding\n"
                    "# comment ignored\n"
                    "30% increased bonuses gained from equipped gloves\n"
                    "50% reduced bonuses gained from equipped boots\n"
                    "+200% to critical hit chance\n"
                ),
                "enabled": True,
            }
        ],
    }
    stripped = _make_empty_stripped()
    known: dict[str, str] = {}
    mutations: list[tuple[str, dict[str, Any]]] = []
    records: list[dict[str, Any]] = []

    _dewater_config_and_custom_mods(cfg_res, set(), known, stripped, mutations, records)

    assert len(records) == 1
    rec = records[0]
    label = rec["label"]
    assert "//" not in label and "# comment" not in label
    assert "MIXED: Viscera/Runegraft lines" in label
    assert "30% increased bonuses gained from equipped gloves" in label
    assert "50% reduced bonuses gained from equipped boots" in label
    assert "other custom lines [+200% to critical hit chance]" in label
    # PoB 2.69 models the grafts and Runegrafts themselves; the advice must not be to keep a copy.
    assert "PoB models an equipped Viscera or allocated Runegraft itself" in label

    notes = stripped.get("customMods330Notes") or []
    assert len(notes) == 1
    assert "mixes Viscera/Runegraft lines" in notes[0] and "counts twice" in notes[0]
    assert "+200% to critical hit chance" in notes[0]
    assert rec.get("customMods330Notes") == notes


def test_custom_mods_pure_330_simulation_vs_kept() -> None:
    cfg_res: dict[str, Any] = {
        "sections": [],
        "customMods": [
            {
                "title": "WickermanSim",
                "text": (
                    "// Upper Limb Greater Overseer's Whip\n"
                    "Minions are Aggressive if you've used a Minion Skill Recently\n"
                    "20% increased bonuses gained from equipped helmet\n"
                ),
                "enabled": True,
            }
        ],
    }
    stripped = _make_empty_stripped()
    mutations: list[tuple[str, dict[str, Any]]] = []
    records: list[dict[str, Any]] = []
    _dewater_config_and_custom_mods(cfg_res, set(), {}, stripped, mutations, records)

    assert len(records) == 1
    lbl = records[0]["label"]
    assert "MIXED:" not in lbl
    assert "Viscera/Runegraft lines" in lbl and "PoB models" in lbl
    assert "if active in-game" not in lbl
    assert len(stripped.get("customMods330Notes") or []) == 1

    stripped_kept = _make_empty_stripped()
    records_kept: list[dict[str, Any]] = []
    _dewater_config_and_custom_mods(cfg_res, {"custommods"}, {}, stripped_kept, [], records_kept)
    assert not records_kept
    assert stripped_kept["kept"]
    assert "customMods330Notes" not in stripped_kept


def test_diagnose_and_build_md_render_330_custom_mods_callout() -> None:
    mixed_label = (
        "customMods[Default] (MIXED: Viscera/Runegraft lines "
        "[30% increased bonuses gained from equipped gloves; 50% reduced bonuses gained from equipped boots] "
        "+ other custom lines [+200% to critical hit chance]; PoB models an equipped Viscera or allocated Runegraft itself)"
    )
    note_txt = (
        "`customMods[Default]` mixes Viscera/Runegraft lines "
        "[30% increased bonuses gained from equipped gloves] with other custom lines [+200% to critical hit chance]."
    )
    diag_payload: dict[str, Any] = {
        "buildInfo": {"level": 96, "className": "Witch", "ascendClassName": "Necromancer"},
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 12_000_000, "totalEhp": 90_000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 5_000_000, "totalEhp": 80_000},
        "topWaterSources": [
            {"dimension": "config", "label": mixed_label, "dpsDelta": 7_000_000, "dpsPct": 140.0, "totalEhpDelta": 0, "totalEhpPct": 0.0}
        ],
        "inflationStripped": {"customMods330Notes": [note_txt]},
    }
    diag_md = format_diagnose_markdown(diag_payload)
    top_idx = diag_md.index("**Top Water Sources Stripped**:")
    note_idx = diag_md.index("> [3.30 CustomMods Note]")
    assert note_idx > top_idx
    assert mixed_label in diag_md
    assert note_txt in diag_md

    # Fallback when only topWaterSources carries the 3.30 label without inflationStripped
    diag_fallback_md = format_diagnose_markdown(
        {
            "buildInfo": {"level": 95, "className": "Witch", "ascendClassName": "Necromancer"},
            "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 8_000_000, "totalEhp": 70_000},
            "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 5_000_000, "totalEhp": 70_000},
            "topWaterSources": [{"dimension": "config", "label": mixed_label, "dpsDelta": 3_000_000, "dpsPct": 60.0}],
        }
    )
    assert "> [3.30 CustomMods Note]" in diag_fallback_md

    matrix_md = format_matrix_markdown(
        {
            "baseline": "Original",
            "primaryDpsKey": "CombinedDPS",
            "scenarios": [
                {"name": "Original", "primaryDps": 10_000_000, "totalEhp": 80_000},
                {
                    "name": "Pinnacle",
                    "primaryDps": 6_000_000,
                    "totalEhp": 75_000,
                    "applied": {"stripped": {"customMods330Notes": [note_txt]}},
                },
            ],
        }
    )
    assert "> [3.30 CustomMods Note]" in matrix_md

    sweep_md = format_sweep_markdown(
        {
            "scenario": "Pinnacle",
            "scenarioApplied": {"stripped": {"customMods330Notes": [note_txt]}},
            "variants": [{"name": "VarA", "ok": True, "primaryDps": 6_500_000, "totalEhp": 76_000}],
        }
    )
    assert "> [3.30 CustomMods Note]" in sweep_md

    compare_md = format_compare_markdown(
        {
            "mainBuildInfo": {"level": 96, "ascendClassName": "Necromancer"},
            "refBuildInfo": {"level": 95, "ascendClassName": "Necromancer"},
            "baselines": {
                "original": [],
                "pinnacleDewatered": {"rows": [], "mainStripped": {"customMods330Notes": [note_txt]}},
            },
        }
    )
    assert "> [3.30 CustomMods Note]" in compare_md


def test_convocation_automation_bricks_used_minion_skill_recently() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "slot": "Body Armour",
                "gems": [
                    {"name": "Raise Spectre", "skillId": "RaiseSpectre", "enabled": True, "support": False},
                    {"name": "Minion Damage Support", "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "enabled": True,
                "slot": "Boots",
                "gems": [
                    {"name": "Convocation", "skillId": "Convocation", "enabled": True, "support": False},
                    {"name": "Automation", "skillId": "Automation", "enabled": True, "support": False},
                ],
            },
        ],
    }
    items_res = {
        "items": [
            {
                "id": 1,
                "name": "Greater Overseer's Whip",
                "raw": "Rarity: UNIQUE\nGreater Overseer's Whip\nUpper Limb Viscera\nMinions are Aggressive if you've used a Minion Skill Recently",
            }
        ],
        "slots": [{"name": "Arm", "selItemId": 1}],
    }
    gaps, mechanics = _detect_build_mechanics_and_pitfalls(
        stats={"MinionCombinedDPS": 4_500_000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75},
        skills_res=skills_res,
        items_res=items_res,
        cfg_res={"sections": [], "customMods": []},
    )
    trig_gaps = [g for g in gaps if g["category"] == "TriggerMechanic" and g["severity"] == "HIGH"]
    assert len(trig_gaps) == 1
    msg = trig_gaps[0]["message"]
    assert "Automation Support" in msg
    assert "used a Minion Skill Recently" in msg
    assert "Greater Overseer's Whip" in msg
    assert mechanics["minionSkillTrigger"]["status"] == "bricked_by_trigger"


def test_convocation_automation_not_flagged_when_manual_combat_minion_skill_exists() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "slot": "Body Armour",
                "gems": [
                    {"name": "Bodyswap of Sacrifice", "skillId": "BodyswapAltX", "enabled": True, "support": False},
                ],
            },
            {
                "index": 2,
                "enabled": True,
                "slot": "Boots",
                "gems": [
                    {"name": "Convocation", "skillId": "Convocation", "enabled": True, "support": False},
                    {"name": "Automation Support", "enabled": True, "support": True},
                ],
            },
        ],
    }
    cfg_res = {
        "sections": [],
        "customMods": [{"title": "Viscera", "text": "Minions are Aggressive if you've used a Minion Skill Recently", "enabled": True}],
    }
    gaps, _mechanics = _detect_build_mechanics_and_pitfalls(
        stats={"CombinedDPS": 3_000_000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75},
        skills_res=skills_res,
        items_res={"items": [], "slots": []},
        cfg_res=cfg_res,
    )
    assert not [g for g in gaps if g["category"] == "TriggerMechanic"]


def _convocation_case(main_gem: dict[str, Any], *, automated: bool, condition: bool) -> tuple[list[dict[str, str]], dict[str, Any]]:
    conv: list[dict[str, Any]] = [{"name": "Convocation", "skillId": "Convocation", "enabled": True, "support": False}]
    if automated:
        conv.append({"name": "Automation Support", "enabled": True, "support": True})
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {"index": 1, "isMain": True, "enabled": True, "slot": "Body Armour", "gems": [main_gem]},
            {"index": 2, "enabled": True, "slot": "Boots", "gems": conv},
        ],
    }
    text = "Minions are Aggressive if you've used a Minion Skill Recently" if condition else "10% increased Minion Damage"
    return _detect_build_mechanics_and_pitfalls(
        stats={"MinionCombinedDPS": 3_000_000, "MinionLife": 50_000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75},
        skills_res=skills_res,
        items_res={"items": [], "slots": []},
        cfg_res={"sections": [], "customMods": [{"title": "Viscera", "text": text, "enabled": True}]},
    )


@pytest.mark.parametrize(
    "main_gem",
    [
        {"name": "Blink Arrow", "skillId": "BlinkArrow", "enabled": True, "support": False},
        {"name": "Mirror Arrow", "skillId": "MirrorArrow", "enabled": True, "support": False},
        {"name": "Holy Strike", "skillId": "HolyStrike", "enabled": True, "support": False},
        {"name": "Dark Pact", "skillId": "DarkPact", "enabled": True, "support": False},
    ],
)
def test_a_minion_skill_cast_by_hand_feeds_the_condition_whatever_convocation_does(main_gem: dict[str, Any]) -> None:
    gaps, mechanics = _convocation_case(main_gem, automated=True, condition=True)
    assert not [g for g in gaps if g["category"] == "TriggerMechanic"]
    # And hand-cast Convocation is then not what the condition hangs on.
    _, mechanics = _convocation_case(main_gem, automated=False, condition=True)
    assert "minionSkillTrigger" not in mechanics


def test_manual_convocation_is_no_advisory_on_a_build_without_the_condition() -> None:
    spectre = {"name": "Raise Spectre", "skillId": "RaiseSpectre", "enabled": True, "support": False}
    gaps, mechanics = _convocation_case(spectre, automated=False, condition=False)
    assert "minionSkillTrigger" not in mechanics
    assert not [s for s in mechanics.get("coreSynergies") or [] if "Convocation" in s]
    assert not [g for g in gaps if g["category"] == "TriggerMechanic"]
    # With the condition and nothing else feeding it, automating Convocation is the brick it warns of.
    gaps, _ = _convocation_case(spectre, automated=True, condition=True)
    assert [g for g in gaps if g["category"] == "TriggerMechanic"]


def test_manual_convocation_records_load_bearing_mechanic_and_renders_in_diagnose() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "slot": "Body Armour",
                "gems": [
                    {"name": "Raise Spectre", "skillId": "RaiseSpectre", "enabled": True, "support": False},
                    {"name": "Animate Guardian", "skillId": "AnimateGuardian", "enabled": True, "support": False},
                ],
            },
            {
                "index": 2,
                "enabled": True,
                "slot": "Helmet",
                "gems": [
                    {"name": "Convocation", "nameZh": "号召", "skillId": "Convocation", "enabled": True, "support": False},
                ],
            },
        ],
    }
    items_res = {
        "items": [
            {
                "id": 10,
                "name": "Greater Overseer's Whip",
                "implicits": ["Minions are Aggressive if you've used a Minion Skill Recently"],
            }
        ],
        "slots": [{"name": "Arm", "selItemId": 10}],
    }
    gaps, mechanics = _detect_build_mechanics_and_pitfalls(
        stats={"MinionCombinedDPS": 8_000_000, "MinionLife": 95_000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75},
        skills_res=skills_res,
        items_res=items_res,
        cfg_res={"sections": [], "customMods": []},
    )
    assert not [g for g in gaps if g["category"] == "TriggerMechanic"]
    mst = mechanics.get("minionSkillTrigger")
    assert isinstance(mst, dict)
    assert mst["status"] == "active_dependency"
    assert mst["hasUsedMinionSkillRecentlyMod"] is True
    assert "slot:Arm" in mst["recentMinionModSources"]
    assert "Automation Support" in mst["note"] and "Greater Overseer's Whip" in mst["note"]

    md = format_diagnose_markdown(
        {
            "buildInfo": {"level": 97, "className": "Witch", "ascendClassName": "Necromancer"},
            "original": {"primaryDpsKey": "MinionCombinedDPS", "primaryDps": 8_000_000, "totalEhp": 95_000},
            "dewateredPinnacle": {"primaryDpsKey": "MinionCombinedDPS", "primaryDps": 8_000_000, "totalEhp": 95_000},
            "core_synergies": ["[OTHER SYNERGY] Existing synergy stays intact."],
            "buildMechanics": mechanics,
        }
    )
    assert "#### 2b. Core Build Synergies & Do-Not-Brick Advisories" in md
    assert "Manual Convocation" in md and "Greater Overseer's Whip" in md


def test_skill_and_reference_docs_330_wickerman_vestigial_and_cli_contracts() -> None:
    skill_dir = REPO_ROOT / "skills" / "pob"
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    anti_md = (skill_dir / "references" / "poe1-330-anti-hallucination.md").read_text(encoding="utf-8")
    trig_md = (skill_dir / "references" / "triggers-and-mechanics.md").read_text(encoding="utf-8")
    batch_md = (skill_dir / "references" / "whatif-and-batch.md").read_text(encoding="utf-8")
    timeless_md = (skill_dir / "references" / "timeless.md").read_text(encoding="utf-8")

    assert len(skill_md.splitlines()) <= 85
    assert len(trig_md.splitlines()) <= 45

    for token in ("{vestigial}", "Wickerman", "Viscera", "Convocation", "pob data tattoo|runegraft"):
        assert token in skill_md, f"Missing {token!r} in SKILL.md"
    # What the engine does not bear out stays out: PoB has no `====` blocks, offers no Vestigial
    # implicit to a quiver, and models Viscera and Runegrafts itself.
    for doc_text in (skill_md, anti_md):
        assert "====" not in doc_text and "NEW 3.30 MECHANIC" not in doc_text
        assert "never on a quiver" in doc_text and "counts twice" in doc_text
        assert "simulated in `customMods`" not in doc_text

    for token in (
        "Vestigial",
        "+200 to maximum Life",
        "Cloak of Flame",
        "Wickerman",
        "Runegraft of the Bound",
        "pob data tattoo",
        "pob data runegraft",
    ):
        assert token in anti_md, f"Missing {token!r} in poe1-330-anti-hallucination.md"

    for token in ("Greater Overseer's Whip", "used a Minion Skill Recently", "Convocation", "Automation Support"):
        assert token in trig_md, f"Missing {token!r} in triggers-and-mechanics.md"

    for token in ('"set"', '"skill_part"', "--group", "--skill", "--allocate", "--out"):
        assert token in batch_md, f"Missing {token!r} in whatif-and-batch.md"
    # The batch validator refuses a bare var/value pair on a config step; the doc must not offer it.
    from pob_cli.mutators.batch_validate import validate_batch_op

    with pytest.raises(ValueError, match='"set"'):
        validate_batch_op(1, "config", {"op": "config", "var": "enemyIsBoss", "value": "Pinnacle"})
    assert "is refused" in batch_md

    for token in ("Elegant Hubris", "Price of Glory", "TreeWaste", "--socket-radius"):
        assert token in timeless_md, f"Missing {token!r} in timeless.md"

    for doc_name, doc_text in (
        ("SKILL.md", skill_md),
        ("poe1-330-anti-hallucination.md", anti_md),
        ("triggers-and-mechanics.md", trig_md),
        ("whatif-and-batch.md", batch_md),
        ("timeless.md", timeless_md),
    ):
        low = doc_text.lower()
        assert "stop exploring" not in low, f"Nagging prompt leaked into {doc_name}"
