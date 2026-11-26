"""Hardening tests for analyzer.py (8 build-architecture & mana/res rules) and formatters.py (3 rendering rules)."""

from __future__ import annotations

import copy
import re
import threading
from typing import Any

import pytest

from pob_cli import analyzer
from pob_cli.formatters import (
    format_cautions,
    format_diagnose_markdown,
    format_game_data,
    format_mutation_report,
)


class _AuditFakeWorker:
    """Minimal stateful fake BridgeWorker for offline `audit_build` tests."""

    def __init__(
        self,
        *,
        stats: dict[str, Any],
        items_res: dict[str, Any] | None = None,
        skills_res: dict[str, Any] | None = None,
        build_info: dict[str, Any] | None = None,
        warnings: list[dict[str, str]] | None = None,
        flasks: dict[str, bool] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.last_good_code: str | None = "INIT"
        self._stats = dict(stats)
        self._items_res = copy.deepcopy(items_res or {"slots": [], "items": []})
        self._skills_res = copy.deepcopy(skills_res or {"mainSocketGroup": 1, "groups": []})
        self._build_info = dict(build_info or {})
        self._warnings = list(warnings or [])
        self.flasks: dict[str, bool] = dict(flasks or {})
        self._snaps: dict[str, dict[str, bool]] = {}
        self._seq = 0

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "get_stats":
            return {"stats": dict(self._stats)}
        if method == "get_sidebar":
            return {"warnings": list(self._warnings), "rows": []}
        if method == "get_build_info":
            return dict(self._build_info)
        if method == "get_build_header":
            return {}
        if method == "list_items":
            slots = list(self._items_res.get("slots") or [])
            items = list(self._items_res.get("items") or [])
            next_id = max((int(it.get("id") or 0) for it in items), default=0) + 1
            for fname, active in self.flasks.items():
                if not any(s.get("name") == fname for s in slots):
                    slots.append({"name": fname, "isFlask": True, "active": active, "selItemId": next_id})
                    items.append({"id": next_id, "name": fname, "type": "Flask"})
                    next_id += 1
                else:
                    for s in slots:
                        if s.get("name") == fname:
                            s["active"] = active
            return {"slots": slots, "items": items}
        if method == "set_slot_active":
            self.flasks[params["slotName"]] = bool(params["active"])
            return {"ok": True}
        if method == "list_skills":
            return copy.deepcopy(self._skills_res)
        if method == "list_config":
            return {"sections": [], "customMods": []}
        if method in ("set_config", "reset_config", "set_custom_mods", "set_gem"):
            return {"ok": True}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "export_code":
            self._seq += 1
            code = f"SNAP_{self._seq}"
            self._snaps[code] = dict(self.flasks)
            return {"code": code}
        if method == "import_code":
            self.flasks = dict(self._snaps.get(params["code"], self.flasks))
            return {"ok": True}
        return {}


# ---------------------------------------------------------------------------
# Analyzer Rule 1: Doryani's Prototype negative Lightning Res exemption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body_name", ["Doryani's Prototype", "多里亚尼之型"])
def test_rule1_doryanis_prototype_suppresses_lightning_gap_and_emits_info(body_name: str) -> None:
    items_res = {
        "slots": [{"name": "Body Armour", "selItemId": 1}],
        "items": [{"id": 1, "name": body_name, "title": body_name, "baseName": "Saint's Hauberk"}],
    }
    w = _AuditFakeWorker(
        stats={
            "CombinedDPS": 5_000_000,
            "TotalEHP": 120_000,
            "Life": 4500,
            "LifeUnreserved": 4500,
            "FireResist": 75,
            "FireResistOverCap": 30,
            "ColdResist": 60,  # Still flagged!
            "ColdResistOverCap": 0,
            "LightningResist": -200,
            "LightningResistOverCap": 0,
            "ChaosResist": 75,
        },
        items_res=items_res,
    )
    res = analyzer.audit_build(w, attribution=False)  # type: ignore[arg-type]
    issues = res["healthIssues"]
    assert not any(
        g["category"] == "Resistance" and "Lightning Resist" in g["message"] for g in issues
    )
    assert any(
        g["category"] == "Resistance" and g["severity"] == "HIGH" and "Cold Resist is uncapped" in g["message"]
        for g in issues
    )
    info_gaps = [g for g in issues if g["severity"] == "INFO" and "Doryani's Prototype" in g["message"]]
    assert info_gaps, f"Expected INFO Doryani gap, got: {issues}"
    msg = info_gaps[0]["message"]
    assert "-200%" in msg and "6m" in msg and "Armour" in msg and "Lightning DoT" in msg
    assert any("Doryani's Prototype" in s for s in res["core_synergies"])


def test_rule1_non_doryani_body_still_flags_uncapped_lightning_res() -> None:
    w = _AuditFakeWorker(
        stats={
            "CombinedDPS": 1_000_000,
            "TotalEHP": 50_000,
            "Life": 4000,
            "LifeUnreserved": 4000,
            "FireResist": 75,
            "FireResistOverCap": 25,
            "ColdResist": 75,
            "ColdResistOverCap": 25,
            "LightningResist": -60,
            "LightningResistOverCap": 0,
            "ChaosResist": 0,
        },
        items_res={
            "slots": [{"name": "Body Armour", "selItemId": 1}],
            "items": [{"id": 1, "name": "Cloak of Flame", "baseName": "Scholar's Robe"}],
        },
    )
    res = analyzer.audit_build(w, attribution=False)  # type: ignore[arg-type]
    assert any(
        g["category"] == "Resistance" and g["severity"] == "HIGH" and "Lightning Resist is uncapped" in g["message"]
        for g in res["healthIssues"]
    )
    assert not any("Doryani's Prototype" in s for s in res["core_synergies"])


# ---------------------------------------------------------------------------
# Analyzer Rule 2: CWS Zero-ES Core-Synergy Guard & Defiance of Destiny
# ---------------------------------------------------------------------------


def test_rule2_cws_the_agnostic_and_defiance_of_destiny_guards() -> None:
    items_res = {
        "slots": [
            {"name": "Ring 1", "selItemId": 1},
            {"name": "Amulet", "selItemId": 2},
            {"name": "Jewel 1", "nodeId": 26725, "selItemId": 3},
        ],
        "items": [
            {"id": 1, "name": "Valyrium", "title": "Valyrium"},
            {"id": 2, "name": "Defiance of Destiny", "title": "Defiance of Destiny"},
            {"id": 3, "name": "Bloodnotch", "title": "Bloodnotch"},
        ],
    }
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Detonate Dead of Scavenging", "enabled": True, "support": False},
                    {"name": "Cast when Stunned", "skillId": "SupportCastWhenStunned", "enabled": True, "support": True},
                ],
            }
        ],
    }
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res=skills_res,
        items_res=items_res,
        cfg_res={},
        tree_summary={"keystones": [{"id": 19732, "name": "The Agnostic"}]},
    )
    syns = mech.get("coreSynergies") or []
    assert any(
        "[DO NOT UNSPEC]" in s and "The Agnostic" in s and "50%" in s and "Blood Magic" in s
        for s in syns
    ), f"Missing [DO NOT UNSPEC] guard: {syns}"
    assert any(
        "[CORE DEFENSE]" in s and "Defiance of Destiny" in s and "damage amulet" in s
        for s in syns
    ), f"Missing [CORE DEFENSE] guard: {syns}"


def test_rule2_agnostic_without_cws_items_no_false_positive() -> None:
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 50},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary={"keystones": [{"id": 19732, "name": "The Agnostic"}]},
    )
    assert not any("[DO NOT UNSPEC]" in s for s in (mech.get("coreSynergies") or []))


# ---------------------------------------------------------------------------
# Analyzer Rule 3: Repeat & Cooldown-Aware Mana Spend Rate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("support_name", "skill_id", "expected_mult"),
    [
        ("Greater Spell Echo", "SupportGreaterSpellEcho", 3),
        ("Spell Echo", "SupportSpellEcho", 2),
        ("Multistrike", "SupportMultistrike", 3),
        ("Awakened Multistrike", "SupportAwakenedMultistrike", 4),
        ("Controlled Destruction", "SupportControlledDestruction", 1),
    ],
)
def test_rule3_repeat_multiplier_detection(support_name: str, skill_id: str, expected_mult: int) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"name": "Arc", "enabled": True, "support": False},
                    {"name": support_name, "skillId": skill_id, "enabled": True, "support": True},
                ],
            }
        ],
    }
    mult, _ = analyzer._detect_repeat_multiplier(skills_res)
    assert mult == expected_mult


def test_rule3_hexblast_of_contradiction_gse_cooldown_caps_mana_spend() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Hexblast of Contradiction", "enabled": True, "support": False},
                    {"name": "Greater Spell Echo", "skillId": "SupportGreaterSpellEcho", "enabled": True, "support": True},
                ],
            }
        ],
    }
    # Speed=5.4 / 3 repeats = 1.8 casts/s -> capped by 1.0s cooldown to 1.0 casts/s -> 500 Mana/s (not 2700!)
    spend = analyzer._compute_effective_mana_spend(
        {"ManaCost": 500, "Speed": 5.4, "Cooldown": 1.0},
        skills_res,
    )
    assert spend["repeats"] == 3
    assert spend["cooldownCapped"] is True
    assert spend["effectiveCastsPerSec"] == pytest.approx(1.0)
    assert spend["spendRate"] == pytest.approx(500.0)

    # In audit_build with 520/s regen, there must be NO false mana spend deficit warning
    w_ok = _AuditFakeWorker(
        stats={
            "CombinedDPS": 10_000_000,
            "TotalEHP": 100_000,
            "Life": 4000,
            "LifeUnreserved": 4000,
            "Mana": 4000,
            "ManaUnreserved": 2000,
            "ManaCost": 500,
            "Speed": 5.4,
            "Cooldown": 1.0,
            "ManaRegenRecovery": 520.0,
            "FireResist": 75,
            "FireResistOverCap": 30,
            "ColdResist": 75,
            "ColdResistOverCap": 30,
            "LightningResist": 75,
            "LightningResistOverCap": 30,
            "ChaosResist": 75,
        },
        skills_res=skills_res,
    )
    res_ok = analyzer.audit_build(w_ok, attribution=False)  # type: ignore[arg-type]
    assert not any("Mana spend rate" in g["message"] for g in res_ok["healthIssues"])

    # With only 200/s regen, a real deficit IS flagged with repeat + cooldown details
    w_low = _AuditFakeWorker(
        stats={
            **w_ok._stats,
            "ManaRegenRecovery": 200.0,
        },
        skills_res=skills_res,
    )
    res_low = analyzer.audit_build(w_low, attribution=False)  # type: ignore[arg-type]
    spend_gaps = [g for g in res_low["healthIssues"] if "Mana spend rate" in g["message"]]
    assert len(spend_gaps) == 1
    assert "3x repeats" in spend_gaps[0]["message"]
    assert "capped by 1.00s CD" in spend_gaps[0]["message"]
    assert "500.0/s" in spend_gaps[0]["message"]


# ---------------------------------------------------------------------------
# Analyzer Rule 4: Inspiration High-Mana Reset Gap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mana_cost", "expected_severity"),
    [
        (750, "HIGH"),
        (700, "HIGH"),
        (550, "MEDIUM"),
        (400, "MEDIUM"),
        (250, None),
    ],
)
def test_rule4_inspiration_high_mana_reset_thresholds(
    mana_cost: int,
    expected_severity: str | None,
) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Hexblast of Contradiction", "enabled": True, "support": False},
                    {"name": "Inspiration", "skillId": "SupportInspiration", "enabled": True, "support": True},
                ],
            }
        ],
    }
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": mana_cost},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    insp_gaps = [g for g in gaps if "Inspiration Support mana-reset trap" in g["message"]]
    if expected_severity is None:
        assert insp_gaps == []
    else:
        assert len(insp_gaps) == 1
        assert insp_gaps[0]["severity"] == expected_severity
        assert "800" in insp_gaps[0]["message"] and "922" in insp_gaps[0]["message"]


# ---------------------------------------------------------------------------
# Analyzer Rule 5: Unequipped (slot: -) Simulated Aura Split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty_slot", ["", None, "-"])
def test_rule5_unequipped_simulated_aura_split(empty_slot: Any) -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Lightning Arrow", "enabled": True, "support": False}],
            },
            {
                "index": 4,
                "slot": empty_slot,
                "enabled": True,
                "gems": [
                    {"name": "Haste", "enabled": True, "support": False},
                    {"name": "Zealotry", "enabled": True, "support": False},
                ],
            },
        ],
    }
    gaps, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaUnreserved": -180, "ManaCost": 20},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    sim_gaps = [g for g in gaps if "[SIMULATED AURA RESERVATION]" in g["message"]]
    assert len(sim_gaps) == 1
    msg = sim_gaps[0]["message"]
    assert "Group #4" in msg and "Haste" in msg and "Zealotry" in msg and "Petrified Blood" in msg
    assert any("[SIMULATED AURA RESERVATION]" in s for s in (mech.get("coreSynergies") or []))

    # Also triggers via reservation warning when ManaUnreserved is not negative
    gaps_w, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaUnreserved": 10, "ManaCost": 20},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        warnings=["You have reserved more Mana than you have available"],
    )
    assert any("[SIMULATED AURA RESERVATION]" in g["message"] for g in gaps_w)

    # No false positive when ManaUnreserved >= 0 and no reservation warning
    gaps_clean, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaUnreserved": 250, "ManaCost": 20},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        warnings=[],
    )
    assert not any("[SIMULATED AURA RESERVATION]" in g["message"] for g in gaps_clean)


# ---------------------------------------------------------------------------
# Analyzer Rule 6: Uul-Netol's Vow 7th Link + Returning Projectiles + Pierce
# ---------------------------------------------------------------------------


def test_rule6_uul_netols_vow_7th_link_and_returning_proj_pierce_synergy() -> None:
    items_res = {
        "slots": [{"name": "Amulet", "selItemId": 10}],
        "items": [{"id": 10, "name": "Uul-Netol's Vow", "title": "Uul-Netol's Vow", "baseName": "Unset Amulet"}],
    }
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Splitting Steel", "enabled": True, "support": False},
                    {"name": "Pierce", "skillId": "SupportPierce", "level": 20, "quality": 20, "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "slot": "Amulet",
                "enabled": True,
                "gems": [
                    {
                        "name": "Returning Projectiles",
                        "nameZh": "投射物返回(辅)",
                        "skillId": "SupportReturningProjectiles",
                        "level": 20,
                        "quality": 20,
                        "enabled": True,
                        "support": True,
                    }
                ],
            },
        ],
    }
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 18},
        skills_res=skills_res,
        items_res=items_res,
        cfg_res={},
    )
    amulet_7l = mech.get("amulet7thLink") or []
    assert len(amulet_7l) == 1
    assert "Returning Projectiles" in amulet_7l[0]
    assert any(
        "[7L SYNERGY]" in s and "Returning Projectiles" in s and "Pierce" in s
        for s in (mech.get("coreSynergies") or [])
    )

    # Without Uul-Netol's Vow, Amulet gem is NOT exposed as amulet7thLink
    _, mech_no_vow = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 18},
        skills_res=skills_res,
        items_res={"slots": [{"name": "Amulet", "selItemId": 11}], "items": [{"id": 11, "name": "Ashes of the Stars"}]},
        cfg_res={},
    )
    assert not mech_no_vow.get("amulet7thLink")


# ---------------------------------------------------------------------------
# Analyzer Rule 7: Multi-Active Trigger / CWS / Hinekora+DD+Penance Mark
# ---------------------------------------------------------------------------


def test_rule7_multi_active_trigger_and_hinekora_advisory() -> None:
    # Case A: >=2 enabled active skills in main group with trigger support (CWS)
    skills_multi = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Detonate Dead of Scavenging", "enabled": True, "support": False},
                    {"name": "Cremation of the Volcano", "enabled": True, "support": False},
                    {"name": "Cast when Stunned", "skillId": "SupportCastWhenStunned", "enabled": True, "support": True},
                ],
            }
        ],
    }
    _, mech_a = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res=skills_multi,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert any(
        "[MULTI-ACTIVE / TRIGGER ARCHITECTURE]" in s and "CombinedDPS" in s and "NOT dead links" in s
        for s in (mech_a.get("coreSynergies") or [])
    )

    # Case B: Hinekora, Death's Fury (1731) + The Hateful Accuser
    _, mech_b = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res={
            "slots": [{"name": "Ring 2", "selItemId": 20}],
            "items": [{"id": 20, "name": "The Hateful Accuser", "title": "The Hateful Accuser"}],
        },
        cfg_res={},
        tree_summary={"ascendancyNodes": [{"id": 1731, "name": "Hinekora, Death's Fury", "asc": "Chieftain"}]},
    )
    assert any("[MULTI-ACTIVE / TRIGGER ARCHITECTURE]" in s for s in (mech_b.get("coreSynergies") or []))

    # Negative case: single active skill, no Hinekora
    _, mech_none = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 1,
                    "isMain": True,
                    "enabled": True,
                    "gems": [{"name": "Fireball", "enabled": True, "support": False}],
                }
            ],
        },
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert not any("[MULTI-ACTIVE / TRIGGER ARCHITECTURE]" in s for s in (mech_none.get("coreSynergies") or []))


# ---------------------------------------------------------------------------
# Analyzer Rule 8: Pathfinder Permanent Flasks Dewater Note
# ---------------------------------------------------------------------------


def test_rule8_pathfinder_permanent_flasks_dewater_note() -> None:
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 25},
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 2,
                    "slot": "Gloves",
                    "enabled": True,
                    "gems": [{"name": "Petrified Blood", "enabled": True, "support": False}],
                }
            ],
        },
        items_res={
            "slots": [
                {"name": "Belt", "selItemId": 1},
                {"name": "Flask 5", "selItemId": 2, "isFlask": True, "active": True},
            ],
            "items": [
                {"id": 1, "name": "The Tides of Time"},
                {"id": 2, "name": "Progenesis"},
            ],
        },
        cfg_res={},
        tree_summary={
            "ascendancyNodes": [
                {"id": 100, "name": "Nature's Adrenaline", "asc": "Pathfinder"},
                {"id": 101, "name": "Master Surgeon", "asc": "Pathfinder"},
            ]
        },
        build_info={"ascendClassName": "Pathfinder"},
        stripped_flasks=["Flask 1: Quicksilver Flask", "Flask 5: Progenesis"],
    )
    syns = mech.get("coreSynergies") or []
    pf_notes = [s for s in syns if "[PATHFINDER PERMANENT FLASKS]" in s]
    assert len(pf_notes) == 1
    note = pf_notes[0]
    assert "--keep flasks" in note
    assert "Master Surgeon + Petrified Blood + Progenesis" in note
    assert "Nature's Adrenaline" in note and "The Tides of Time" in note

    # Non-Pathfinder does NOT emit Pathfinder note
    _, mech_other = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 25},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res={"slots": [], "items": []},
        cfg_res={},
        build_info={"ascendClassName": "Hierophant"},
        stripped_flasks=["Flask 1: Diamond Flask"],
    )
    assert not any("PATHFINDER" in s for s in (mech_other.get("coreSynergies") or []))


# ---------------------------------------------------------------------------
# Formatter Rule 1: format_game_data (gem cooldown, Exceptional, Legacy 3.28+)
# ---------------------------------------------------------------------------


def test_formatter1_format_game_data_gem_badges_and_cooldown() -> None:
    payload = {
        "kind": "gem",
        "query": "test gems",
        "count": 4,
        "pobVersion": "2.68.1",
        "dataVersion": "3.30",
        "results": [
            {
                "name": "Hexblast of Contradiction",
                "zh_name": "咒术枯萎·矛盾",
                "color": "Blue",
                "is_support": False,
                "cast_time": 1.0,
                "cooldown": 1.0,
                "stored_uses": 3,
                "tag_string": "Spell, Chaos, AoE, Hex",
            },
            {
                "name": "Frostblink",
                "color": "Blue",
                "is_support": False,
                "cooldown": 2.6,
                "stored_uses": 1,
            },
            {
                "name": "Greater Spell Echo",
                "zh_name": "高阶施法回响",
                "color": "Blue",
                "is_support": True,
                "is_exceptional": True,
                "natural_max_level": 3,
                "corrupted_max_level": 4,
                "mana_multiplier": 150,
            },
            {
                "name": "Awakened Spell Echo",
                "color": "Blue",
                "is_support": True,
                "legacy_removed_328": True,
                "natural_max_level": 5,
                "corrupted_max_level": 6,
            },
        ],
    }
    md = format_game_data(payload, "gem", "test gems")
    assert "CD: 1s (3 uses)" in md
    assert "CD: 2.6s" in md
    assert "CD: 2.6s (1 uses)" not in md
    assert "[Exceptional | MaxLv 3/4]" in md
    assert "[LEGACY REMOVED 3.28+]" in md
    assert "> [POE1-PATCH-WARN]" in md and "3.28" in md


# ---------------------------------------------------------------------------
# Formatter Rule 2: format_diagnose_markdown (Amulet 7L + 2b Core Synergies)
# ---------------------------------------------------------------------------


def test_formatter2_format_diagnose_amulet_7l_and_core_synergies() -> None:
    diag = {
        "env": {"game": "poe1", "patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "treeVersion": "3_30", "slot": "main"},
        "buildInfo": {"level": 100, "className": "Duelist", "ascendClassName": "Champion", "points": {"used": 123, "usedMax": 123, "ascUsed": 8, "ascMax": 8}},
        "mainGroup": {
            "index": 1,
            "slot": "Body Armour",
            "displayLabel": "Splitting Steel",
            "gems": [
                {"name": "Splitting Steel", "level": 21, "quality": 20, "enabled": True},
                {"name": "Pierce", "level": 20, "quality": 20, "enabled": True},
            ],
        },
        "amulet_7th_link": ["Returning Projectiles (20/20%)"],
        "core_synergies": [
            "[7L SYNERGY] Returning Projectiles + Pierce: do NOT drop Pierce!",
            "[DO NOT UNSPEC] The Agnostic + Valyrium/Bloodnotch/CWS: ES=0 lock.",
        ],
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 20_000_000, "totalEhp": 250_000, "physMaxHit": 30_000, "eleMaxHit": 60_000, "chaosMaxHit": 40_000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 15_000_000, "totalEhp": 180_000, "physMaxHit": 25_000, "eleMaxHit": 50_000, "chaosMaxHit": 35_000},
        "healthGaps": [],
        "factCards": {
            "gems": [
                {
                    "name": "Hexblast of Contradiction",
                    "isSupport": False,
                    "castTime": 1.0,
                    "cooldown": 1.0,
                    "stored_uses": 3,
                },
                {
                    "name": "Greater Spell Echo",
                    "isSupport": True,
                    "is_exceptional": True,
                    "natural_max_level": 3,
                    "corrupted_max_level": 4,
                },
                {
                    "name": "Awakened Elemental Focus",
                    "isSupport": True,
                    "legacy_removed_328": True,
                },
            ],
            "uniques": [],
        },
    }
    md = format_diagnose_markdown(diag)
    linked_line = next(ln for ln in md.splitlines() if ln.startswith("- **Linked Gems**: "))
    assert re.search(r"\bSplitting Steel\)?\s+\(21/20%\)", linked_line)
    assert re.search(r"\bPierce\)?\s+\(20/20%\)", linked_line)
    assert "+ [Amulet 7L: Returning Projectiles (20/20%)]" in linked_line
    assert "#### 2b. Core Build Synergies & Do-Not-Brick Advisories" in md
    assert "- [7L SYNERGY] Returning Projectiles + Pierce: do NOT drop Pierce!" in md
    assert "- [DO NOT UNSPEC] The Agnostic + Valyrium/Bloodnotch/CWS: ES=0 lock." in md
    assert "CD=1s (3 uses)" in md
    assert "[Exceptional | MaxLv 3/4]" in md
    assert "[LEGACY REMOVED 3.28+]" in md


# ---------------------------------------------------------------------------
# Formatter Rule 3: Mutation formatter (`tree change`, `skill set`, `skill add`, `batch`) cautions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "payload"),
    [
        (
            "Passive Tree Change",
            {
                "mode": "WHAT-IF",
                "before": {"primaryDps": 1000, "totalEhp": 5000, "life": 4000, "points": {"used": 100, "usedMax": 120}},
                "after": {"primaryDps": 1000, "totalEhp": 5000, "life": 4000, "points": {"used": 99, "usedMax": 120}},
                "diff": {"deltas": []},
                "cautions": ["[BUILD-BRICK RISK] Deallocating The Agnostic breaks CWS!"],
            },
        ),
        (
            "Skill Set (Group: main, Gem: 2)",
            {
                "mode": "APPLY",
                "before": {"primaryDps": 1000, "totalEhp": 5000, "life": 4000, "points": {}},
                "after": {"primaryDps": 1200, "totalEhp": 5000, "life": 4000, "points": {}},
                "diff": {"deltas": []},
                "cautions": ["[LEGACY 3.28+ REMOVED] 'Awakened Spell Echo' was drop-disabled in 3.28."],
            },
        ),
        (
            "Add Gem 'Awakened Elemental Focus' (5/20%) to Group 'main'",
            {
                "mode": "WHAT-IF",
                "before": {},
                "after": {"primaryDps": 1200, "totalEhp": 5000, "life": 4000, "points": {}},
                "diff": {"deltas": []},
                "detail": {"cautions": ["[LEGACY 3.28+ REMOVED] unavailable in current leagues."]},
            },
        ),
        (
            "Batch Execution (2 steps)",
            {
                "mode": "WHAT-IF",
                "before": {"primaryDps": 1000, "totalEhp": 5000, "life": 4000, "points": {}},
                "after": {"primaryDps": 1200, "totalEhp": 5000, "life": 4000, "points": {}},
                "diff": {"deltas": []},
                "steps": [
                    {"op": "tree", "cautions": ["[BUILD-BRICK RISK] Step 1 caution"]},
                    {"op": "skill_add", "cautions": ["[LEGACY 3.28+ REMOVED] Step 2 caution"]},
                ],
            },
        ),
    ],
)
def test_formatter3_mutation_report_renders_cautions(title: str, payload: dict[str, Any]) -> None:
    md = format_mutation_report(title, payload)
    expected_lines = format_cautions(payload)
    assert expected_lines, "Expected at least one caution line"
    for line in expected_lines:
        assert line.startswith("> [CAUTION] ")
        assert line in md


# ---------------------------------------------------------------------------
# Polish Pass: Ramako Fixed -20% DoT Res, Frost Bomb, Spectre Lifetap, Pantheon, Return Split
# ---------------------------------------------------------------------------


def test_polish_ramako_frost_bomb_and_guardrail() -> None:
    from pob_cli import guardrails

    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Flame Surge of Combusting", "enabled": True, "support": False}],
            },
            {
                "index": 2,
                "slot": "Weapon 1",
                "enabled": True,
                "gems": [{"name": "Frost Bomb", "nameZh": "寒霜爆", "enabled": True, "support": False}],
            },
        ],
    }
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
        tree_summary={"ascendancyNodes": [{"id": 61355, "name": "Ramako, Sun's Light", "asc": "Chieftain"}]},
    )
    syns = mech.get("coreSynergies") or []
    assert any("[RAMAKO FIXED -20% DOT RES]" in s and "Punishment" in s for s in syns)
    assert any("[UTILITY SKILL: FROST BOMB]" in s and "-75% Enemy Life Regeneration" in s for s in syns)

    scan = guardrails.scan_text_traps("Ramako, Sun's Light 雷默科 易燃")
    assert any(w.get("ruleId") == "PATCH_330_RAMAKO_FIXED_DOT_RES" for w in (scan.get("warnings") or []))


def test_polish_spectre_hideout_cast_and_pantheon_and_return_split() -> None:
    from pob_cli import formatters, mutators

    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Raise Spectre", "enabled": True, "support": False}],
            },
            {
                "index": 2,
                "slot": "Gloves",
                "enabled": True,
                "gems": [{"name": "Lifetap", "enabled": True, "support": True}],
            },
            {
                "index": 3,
                "slot": "Boots",
                "enabled": True,
                "gems": [{"name": "Lifetap", "enabled": True, "support": True}],
            },
        ],
    }
    _, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 218, "ManaUnreserved": -74},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    assert any("[SPECTRE HIDEOUT CAST]" in s for s in (mech.get("coreSynergies") or []))

    md = formatters.format_diagnose_markdown(
        {
            "env": {"game": "poe1", "patch": "3.30", "pobVersion": "2.68.1", "slot": "main"},
            "buildInfo": {"level": 96, "className": "Ranger", "ascendClassName": "Pathfinder"},
            "mainGroup": {"index": 1, "slot": "Body Armour", "gems": []},
            "keystones": ["Iron Reflexes"],
            "pantheon": {
                "major": "TheBrineKing",
                "majorHint": "Freeze Immune + Stun Lock Protection",
                "minor": "Gruthkul",
                "minorHint": "Phys DR on Hit + Maim Attackers",
            },
            "original": {},
            "dewateredPinnacle": {},
        }
    )
    assert "- **Pantheon**: Major `TheBrineKing` (Freeze Immune + Stun Lock Protection) | Minor `Gruthkul`" in md

    assert mutators._normalize_return_sections("skills summary") == ["skills", "summary"]
    assert mutators._normalize_return_sections("skills,summary,calc:DPS;Mana") == ["skills", "summary", "calc:DPS;Mana"]
