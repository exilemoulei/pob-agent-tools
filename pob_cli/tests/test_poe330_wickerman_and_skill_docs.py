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
    # --keep customMods cannot keep half a block.
    assert "keeps the block whole" in notes[0]
    assert "+200% to critical hit chance" in notes[0]
    assert rec.get("customMods330Notes") == notes


def test_custom_mods_pure_330_simulation_vs_kept() -> None:
    cfg_res: dict[str, Any] = {
        "sections": [],
        "customMods": [
            {
                "title": "WickermanSim",
                "text": (
                    "// Greater Scholar's Leg\n"
                    "45% reduced bonuses gained from Equipped Boots\n"
                    "25% increased bonuses gained from Equipped Gloves\n"
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
    notes = stripped.get("customMods330Notes") or []
    assert len(notes) == 1 and "counts twice" in notes[0]

    # Only gloves and boots bonuses come from a Viscera or Runegraft, and PoB does not parse the
    # Overseer's Whip aggression line at all: neither is a line that would count twice.
    # A Chinese line or a bare name is not something PoB's customMods parser reads, so it cannot count twice.
    for other in ("20% increased bonuses gained from equipped helmet", "30% increased bonuses gained from equipped quiver",
                  "Minions are Aggressive if you've used a Minion Skill Recently", "装备手套获得的加成提高 25%",
                  "Greater Overseer's Whip", "Runegraft of the Bound"):
        plain = _make_empty_stripped()
        recs: list[dict[str, Any]] = []
        _dewater_config_and_custom_mods(
            {"sections": [], "customMods": [{"title": "T", "text": other, "enabled": True}]}, set(), {}, plain, [], recs
        )
        assert "Viscera/Runegraft lines" not in recs[0]["label"], other
        assert "customMods330Notes" not in plain, other

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


def _gem(name: str, skill_id: str = "", *, support: bool = False) -> dict[str, Any]:
    return {"name": name, "skillId": skill_id, "enabled": True, "support": support}


def _convocation_case(
    main_gem: dict[str, Any],
    *,
    automated: bool,
    condition: bool = True,
    conv_support: dict[str, Any] | None = None,
    main_support: dict[str, Any] | None = None,
    condition_text: str = "Minions are Aggressive if you've used a Minion Skill Recently",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    conv = [_gem("Convocation", "Convocation")]
    if automated:
        conv.append(conv_support or _gem("Automation Support", "SupportAutomation", support=True))
    main = [main_gem] + ([main_support] if main_support else [])
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {"index": 1, "isMain": True, "enabled": True, "slot": "Body Armour", "gems": main},
            {"index": 2, "enabled": True, "slot": "Boots", "gems": conv},
        ],
    }
    text = condition_text if condition else "10% increased Minion Damage"
    return _detect_build_mechanics_and_pitfalls(
        stats={"MinionCombinedDPS": 3_000_000, "MinionLife": 50_000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75},
        skills_res=skills_res,
        items_res={"items": [], "slots": []},
        cfg_res={"sections": [], "customMods": [{"title": "Viscera", "text": text, "enabled": True}]},
    )


def _bricked(gaps: list[dict[str, str]]) -> bool:
    return bool([g for g in gaps if g["category"] == "TriggerMechanic"])


# Names and skill ids as the PoB 2.69 bridge reports them.
@pytest.mark.parametrize(
    "main_gem",
    [
        _gem("Blink Arrow", "BlinkArrow"),
        _gem("Mirror Arrow of Bombarding Clones", "MirrorArrowAltX"),
        _gem("Holy Strike", "HolyStrike"),
        _gem("Dark Bargain", "DarkPact"),
        _gem("Death Wish", "DeathWish"),
        _gem("Raise Spectre of Transience", "RaiseSpectreAltX"),
        _gem("Raise Zombie of Falling", "RaiseZombieAltY"),
        _gem("Vaal Domination", "VaalDomination"),
        _gem("召唤魔侍", "SummonSkeletons"),
    ],
)
def test_a_minion_skill_cast_by_hand_feeds_the_condition_whatever_convocation_does(main_gem: dict[str, Any]) -> None:
    gaps, mechanics = _convocation_case(main_gem, automated=True)
    assert not _bricked(gaps)
    # And hand-cast Convocation is then not what the condition hangs on.
    _, mechanics = _convocation_case(main_gem, automated=False)
    assert "minionSkillTrigger" not in mechanics


@pytest.mark.parametrize(
    ("main_gem", "main_support"),
    [
        # Permanent minions are summoned once, not used again and again.
        (_gem("Raise Spectre", "RaiseSpectre"), None),
        (_gem("Raise Zombie of Slamming", "RaiseZombieAltX"), None),
        # Shares a word with Dark Bargain in zh-TW but is no minion skill.
        (_gem("Dark Bargain of Trarthus", "DarkPactAltX"), None),
        # Manaforged Arrows fires the bow attack by itself.
        (_gem("Mirror Arrow", "MirrorArrow"), _gem("Manaforged Arrows", "SupportManaforgedArrows", support=True)),
        # A triggered minion skill is not used either.
        (_gem("Summon Raging Spirit", "SummonRagingSpirit"), _gem("Cast On Critical Strike", "SupportCastOnCriticalStrike", support=True)),
        (_gem("Summon Raging Spirit", "SummonRagingSpirit"), _gem("Cast on Death", "SupportCastOnDeath", support=True)),
    ],
)
def test_a_skill_that_does_not_feed_the_condition_leaves_the_warning_standing(
    main_gem: dict[str, Any], main_support: dict[str, Any] | None
) -> None:
    gaps, _ = _convocation_case(main_gem, automated=True, main_support=main_support)
    assert _bricked(gaps)


@pytest.mark.parametrize(
    ("attack", "support"),
    [
        (_gem("Holy Strike", "HolyStrike"), _gem("Cast on Melee Kill", "SupportCastOnMeleeKill", support=True)),
        (_gem("Dominating Blow", "DominatingBlow"), _gem("Cast On Critical Strike", "SupportCastOnCriticalStrike", support=True)),
        (_gem("Mirror Arrow", "MirrorArrow"), _gem("Awakened Cast On Critical Strike", "SupportAwakenedCastOnCriticalStrike", support=True)),
    ],
)
def test_the_attack_that_fires_a_cast_on_support_is_itself_used_by_hand(attack: dict[str, Any], support: dict[str, Any]) -> None:
    gaps, _ = _convocation_case(attack, automated=True, main_support=support)
    assert not _bricked(gaps)


@pytest.mark.parametrize(
    "support",
    [
        _gem("Cast on Melee Kill", "SupportCastOnMeleeKill", support=True),
        _gem("Cast on Death", "SupportCastOnDeath", support=True),
        _gem("Cast when Damage Taken", "SupportCastWhenDamageTaken", support=True),
        # Told by the id alone, and by the name alone.
        _gem("", "SupportCastOnWardBreak", support=True),
        _gem("Cast on Melee Kill", support=True),
    ],
)
def test_convocation_behind_any_trigger_support_is_not_called_manual(support: dict[str, Any]) -> None:
    spectre = _gem("Raise Spectre", "RaiseSpectre")
    gaps, mechanics = _convocation_case(spectre, automated=True, conv_support=support)
    assert _bricked(gaps)
    assert (mechanics.get("minionSkillTrigger") or {}).get("status") != "active_dependency"


def test_the_condition_is_read_in_the_wordings_the_chinese_dictionaries_use() -> None:
    spectre = _gem("Raise Spectre", "RaiseSpectre")
    for text in (
        "若你近期内使用了召唤生物技能，则召唤生物的伤害提高 20%",
        "若你近期有使用召喚物技能，召喚物增加 20% 傷害",
    ):
        gaps, _ = _convocation_case(spectre, automated=True, condition_text=text)
        assert _bricked(gaps), text


def test_manual_convocation_is_no_advisory_on_a_build_without_the_condition() -> None:
    spectre = _gem("Raise Spectre", "RaiseSpectre")
    gaps, mechanics = _convocation_case(spectre, automated=False, condition=False)
    assert "minionSkillTrigger" not in mechanics
    assert not [s for s in mechanics.get("coreSynergies") or [] if "Convocation" in s]
    assert not _bricked(gaps)
    # With the condition and nothing else feeding it, automating Convocation is the brick it warns of.
    gaps, _ = _convocation_case(spectre, automated=True)
    assert _bricked(gaps)


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

    for token in ("{vestigial}", "Wickerman", "Viscera", "Convocation", "pob data runegraft <query>", "pob data tattoo <query> [--kind"):
        assert token in skill_md, f"Missing {token!r} in SKILL.md"
    # What the engine does not bear out stays out: PoB has no `====` blocks, offers no Vestigial
    # implicit to a quiver, and models Viscera and Runegrafts itself.
    for doc_text in (skill_md, anti_md):
        assert "====" not in doc_text and "NEW 3.30 MECHANIC" not in doc_text
        assert "never on a quiver" in doc_text and "counts twice" in doc_text
        # PoB's rule lets Stasis Prison take one, and its example of a parsed Viscera is the gloves and
        # boots bonus, not the Overseer's Whip line it cannot parse.
        assert "Stasis Prison" in doc_text and "Greater Scholar's Leg" in doc_text
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


def test_the_hand_cast_minion_skills_are_the_ones_pob_types_so() -> None:
    """The id table against the engine's skill files, so a PoB update that adds a gem shows up here."""
    import re

    from engine_paths import find_engine_data

    from pob_cli.analyzer.pitfalls_minion_trigger import (
        _HAND_CAST_MINION_SKILL_IDS,
        _MINION_ATTACK_SKILL_IDS,
    )

    engine = find_engine_data()
    skills_dir = engine / "PoE1" / "Data" / "Skills" if engine else None
    if skills_dir is None or not skills_dir.is_dir():
        pytest.skip("no engine on this machine")

    def typed(path: Path) -> dict[str, tuple[str, bool]]:
        parts = re.split(r'\nskills\["([^"]+)"\] = \{', path.read_text(encoding="utf-8"))
        out: dict[str, tuple[str, bool]] = {}
        for sid, body in zip(parts[1::2], parts[2::2], strict=True):
            m = re.search(r"skillTypes = \{([^\n]*)\}", body)
            out[sid] = (m.group(1) if m else "", "permanentMinion = true" in body)
        return out

    gem_skills: dict[str, tuple[str, bool]] = {}
    for f in sorted(skills_dir.glob("act_*.lua")):
        gem_skills.update(typed(f))
    used = {
        sid.lower()
        for sid, (types, permanent) in gem_skills.items()
        if "SkillType.Minion]" in types and "SkillType.Triggered]" not in types and not permanent
    }
    item_granted = {"bloodoffering", "deathwish"}
    assert used - {"heraldofpurity"} == _HAND_CAST_MINION_SKILL_IDS - item_granted
    other = typed(skills_dir / "other.lua")
    assert all("SkillType.Minion]" in other[s][0] for s in ("BloodOffering", "DeathWish"))
    attacks = {sid.lower() for sid, (types, _) in gem_skills.items() if sid.lower() in used and "SkillType.Attack]" in types}
    assert attacks == _MINION_ATTACK_SKILL_IDS
