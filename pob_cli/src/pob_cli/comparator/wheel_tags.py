"""Static main-skill tag profiling, wheel pre-filtering, and mastery effect selection for `rank-wheels`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..formatters import strip_pob_escapes
from ..game_data.tree_graph import PassiveGraph, WheelInfo
from .ranking import _classify_item_weapon_type

_WEAPON_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bow": ("with bows", "bow damage", "bow skills", "bow attack"),
    "wand": ("with wands", "wand damage", "wand attack"),
    "claw": ("with claws", "claw damage", "claw attack"),
    "dagger": ("with daggers", "dagger damage", "dagger attack"),
    "axe": ("with axes", "axe damage", "axe attack"),
    "sword": ("with swords", "sword damage", "sword attack"),
    "mace": ("with maces", "with sceptres", "mace or sceptre", "mace damage"),
    "staff": ("with staves", "with warstaves", "staff damage", "while wielding a staff"),
    "shield": (
        "holding a shield",
        "equipped shield",
        "with shields",
        "shield block",
        "from shields",
        "defences from equipped shield",
    ),
    "dual_wield": ("while dual wielding", "dual wielding"),
    "two_handed": ("with two handed", "two handed melee"),
}

_ARCHETYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "minion": ("minion", "minions", "zombies", "skeletons", "spectres", "golems"),
    "totem": ("totem", "totems", "ballista"),
    "trap": ("trap ", "traps", "throwing speed"),
    "mine": ("mine ", "mines", "detonation"),
    "brand": ("brand ", "brands", "attached to an enemy"),
    "warcry": ("warcry", "warcries", "exerted"),
    "bleed": ("bleeding", "bleed"),
    "poison": ("poison", "poisoned"),
    "ignite": ("ignite", "ignites", "ignited", "burning damage"),
    "melee": ("melee ",),
    "projectile": ("projectile", "projectiles"),
    "spell": ("spell damage", "cast speed", "with spells", "spell critical", "spell skills"),
    "attack": ("attack damage", "attack speed", "with attacks", "attack skills", "weapon elemental"),
}

_DAMAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fire": ("fire damage", "fire skills", "fire resistance penetration", "fire damage over time"),
    "cold": ("cold damage", "cold skills", "cold resistance penetration", "cold damage over time"),
    "lightning": ("lightning damage", "lightning skills", "lightning resistance penetration", "shock"),
    "chaos": ("chaos damage", "chaos skills", "chaos damage over time", "withered"),
    "physical": ("physical damage", "impale", "overwhelm physical"),
}

_CORE_UNIVERSAL_KEYWORDS: tuple[str, ...] = (
    "reservation efficiency",
    "aura",
    "maximum life",
    "maximum energy shield",
    "spell suppression",
    "maximum fire resistance",
    "maximum cold resistance",
    "maximum lightning resistance",
    "maximum chaos resistance",
    "all elemental resistances",
    "chaos resistance",
    "mana regeneration",
    "maximum mana",
    "reduced mana cost",
    "power charge",
    "frenzy charge",
    "endurance charge",
    "curse",
    "flask",
)


@dataclass
class BuildProfile:
    """Inferred static build profile for wheel pre-filtering and proxy jewel templates."""

    is_spell: bool = False
    is_attack: bool = False
    is_melee: bool = False
    is_projectile: bool = False
    is_minion: bool = False
    is_totem: bool = False
    is_trap: bool = False
    is_mine: bool = False
    is_brand: bool = False
    is_dot: bool = False
    is_crit: bool = False
    is_trigger: bool = False
    is_ci: bool = False
    is_es_primary: bool = False
    is_mom: bool = False
    is_agnostic: bool = False
    damage_types: set[str] = field(default_factory=set)
    dot_types: set[str] = field(default_factory=set)
    weapon_types: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)


def _fnum(val: Any) -> float:
    if isinstance(val, bool) or val is None:
        return 0.0
    try:
        v = float(val)
        return v if math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def extract_build_profile(
    stats: dict[str, Any],
    *,
    skills_res: dict[str, Any] | None = None,
    items_res: dict[str, Any] | None = None,
    allocated_keystone_names: set[str] | None = None,
) -> BuildProfile:
    """Extract main-skill archetypes, damage types, weapon classes, and defense paradigm from PoB snapshot."""
    prof = BuildProfile()
    st = stats if isinstance(stats, dict) else {}
    ks_low = {k.strip().lower() for k in (allocated_keystone_names or set())}

    # Defense paradigm
    life = _fnum(st.get("Life"))
    es = _fnum(st.get("EnergyShield"))
    prof.is_ci = (life <= 1.5 and es > 50.0) or ("chaos inoculation" in ks_low)
    prof.is_es_primary = prof.is_ci or (es > life * 0.9 and es > 500.0)
    prof.is_mom = "mind over matter" in ks_low
    prof.is_agnostic = "the agnostic" in ks_low

    # Skills analysis
    main_idx = (skills_res or {}).get("mainSocketGroup") or 1
    groups = (skills_res or {}).get("groups") or []
    main_grp = next((g for g in groups if isinstance(g, dict) and (g.get("isMain") or g.get("index") == main_idx)), None)
    if main_grp is None and groups:
        main_grp = groups[0]

    gem_blob_parts: list[str] = []
    if isinstance(main_grp, dict):
        for gm in main_grp.get("gems") or []:
            if isinstance(gm, dict) and gm.get("enabled", True):
                gem_blob_parts.append(strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower())
    gem_blob = " | ".join(gem_blob_parts)

    if any(k in gem_blob for k in ("minion", "zombie", "skeleton", "spectre", "golem", "raging spirit", "animate", "phantasm", "holy relic", "absolution")):
        prof.is_minion = True
    if any(k in gem_blob for k in ("totem", "ballista")):
        prof.is_totem = True
    if "trap" in gem_blob and "trapped" not in gem_blob:
        prof.is_trap = True
    if any(
        k in gem_blob
        for k in ("minefield", "high-impact mine", "blastchain", "pyroclast", "hexblast", "locust mine", "mine support")
    ):
        prof.is_mine = True
    if "brand" in gem_blob:
        prof.is_brand = True
    if any(k in gem_blob for k in ("cast on critical", "cast while channelling", "cospri", "mjolner", "spellslinger", "poet's pen")):
        prof.is_trigger = True

    # Attack vs Spell detection from gem names first, falling back to PoB stats only when ambiguous.
    hit_chance = st.get("HitChance")
    if any(k in gem_blob for k in ("spell", "arc", "fireball", "spark", "ice nova", "ball lightning", "blade vortex", "righteous fire", "essence drain", "forbidden rite", "hexblast", "detonate dead", "cremation", "vortex", "cold snap", "blazing salvo", "eye of winter", "freezing pulse", "crackling lance", "storm brand", "penance brand", "armageddon brand", "ethereal knives", "reap", "exsanguinate")):
        prof.is_spell = True
    if any(k in gem_blob for k in ("cyclone", "lightning strike", "tornado shot", "lightning arrow", "rain of arrows", "flicker strike", "boneshatter", "molten strike", "cobra lash", "venom gyre", "frost blades", "dual strike", "heavy strike", "cleave", "lacerate", "split arrow", "kinetic blast", "power siphon", "elemental hit", "la ", "ts ")):
        prof.is_attack = True
    if not prof.is_attack and not prof.is_spell and not prof.is_minion:
        # Fallback when gem name isn't recognized: PoB only populates HitChance for attack skills.
        if hit_chance is None:
            prof.is_spell = True
        else:
            prof.is_attack = True
    if prof.is_trigger:
        prof.is_spell = True

    if any(k in gem_blob for k in ("cyclone", "strike", "slam", "shatter", "cleave", "lacerate", "flicker", "melee", "sweep", "reave")):
        prof.is_melee = True
    if any(k in gem_blob for k in ("arrow", "shot", "projectile", "spark", "fireball", "kinetic", "siphon", "cobra", "venom", "spear", "eye of winter", "freezing pulse", "ball lightning", "lightning strike")):
        prof.is_projectile = True

    # Crit & DoT
    crit_chance = _fnum(st.get("CritChance"))
    total_dot = _fnum(st.get("TotalDotDPS") or st.get("TotalDot") or st.get("FullDotDPS"))
    comb_dps = _fnum(st.get("CombinedDPS") or st.get("FullDPS") or st.get("TotalDPS"))
    prof.is_crit = (crit_chance >= 20.0) or ("critical" in gem_blob)
    prof.is_dot = (total_dot > 0 and (comb_dps <= 0 or total_dot >= comb_dps * 0.25)) or any(
        k in gem_blob for k in ("ignite", "poison", "bleed", "righteous fire", "essence drain", "bane", "caustic", "toxic rain", "death's oath", "rupture")
    )
    if _fnum(st.get("IgniteDPS")) > 0 or "ignite" in gem_blob or "righteous fire" in gem_blob:
        prof.dot_types.add("ignite")
    if _fnum(st.get("BleedDPS")) > 0 or "bleed" in gem_blob:
        prof.dot_types.add("bleed")
    if _fnum(st.get("PoisonDPS")) > 0 or "poison" in gem_blob:
        prof.dot_types.add("poison")

    # Damage types from stats or gem hints
    dmg_vals = {
        "physical": _fnum(st.get("PhysicalDPS")) + _fnum(st.get("PhysicalAverage")),
        "fire": _fnum(st.get("FireDPS")) + _fnum(st.get("FireAverage")) + _fnum(st.get("IgniteDPS")),
        "cold": _fnum(st.get("ColdDPS")) + _fnum(st.get("ColdAverage")),
        "lightning": _fnum(st.get("LightningDPS")) + _fnum(st.get("LightningAverage")),
        "chaos": _fnum(st.get("ChaosDPS")) + _fnum(st.get("ChaosAverage")) + _fnum(st.get("PoisonDPS")),
    }
    max_dmg = max(dmg_vals.values(), default=0.0)
    if max_dmg > 0:
        for dt, dv in dmg_vals.items():
            if dv >= max_dmg * 0.15:
                prof.damage_types.add(dt)
    for dt, hints in (
        ("fire", ("fire", "flame", "ignite", "burn", "incinerate", "molten", "pyro", "armageddon", "cremation", "detonate dead", "righteous fire", "blazing")),
        ("cold", ("cold", "ice", "frost", "freez", "vortex", "winter", "glacial", "arctic", "creeping frost")),
        ("lightning", ("lightning", "arc", "spark", "storm", "shock", "crackling", "ball lightning", "voltaxic", "wrath")),
        ("chaos", ("chaos", "poison", "toxic", "caustic", "essence drain", "blight", "hexblast", "forbidden rite", "bane", "soulrend", "viper")),
        ("physical", ("phys", "bleed", "impale", "boneshatter", "reap", "exsanguinate", "blade vortex", "ethereal knives", "lacerate", "cyclone")),
    ):
        if any(h in gem_blob for h in hints):
            prof.damage_types.add(dt)

    # Weapons from equipped items
    if isinstance(items_res, dict):
        items_by_id = {it.get("id"): it for it in (items_res.get("items") or []) if isinstance(it, dict)}
        slots_by_name = {str(s.get("name") or ""): s for s in (items_res.get("slots") or []) if isinstance(s, dict)}
        w1 = items_by_id.get((slots_by_name.get("Weapon 1") or {}).get("selItemId"))
        w2 = items_by_id.get((slots_by_name.get("Weapon 2") or {}).get("selItemId"))
        w1_t = _classify_item_weapon_type(w1) if isinstance(w1, dict) else None
        w2_t = _classify_item_weapon_type(w2) if isinstance(w2, dict) else None
        if w1_t and w1_t != "non_weapon":
            prof.weapon_types.add(w1_t)
        if w2_t == "non_weapon":
            if isinstance(w2, dict) and "shield" in str(w2.get("type") or w2.get("baseName") or "").lower():
                prof.weapon_types.add("shield")
        elif w2_t:
            prof.weapon_types.add(w2_t)
            if w1_t and w1_t != "non_weapon":
                prof.weapon_types.add("dual_wield")
        if isinstance(w1, dict) and any(
            k in str(w1.get("type") or "").lower() for k in ("two handed", "bow", "staff", "warstaff")
        ):
            prof.weapon_types.add("two_handed")

    # Flatten tags summary for JSON reporting
    for flag, lbl in (
        (prof.is_spell, "spell"),
        (prof.is_attack, "attack"),
        (prof.is_melee, "melee"),
        (prof.is_projectile, "projectile"),
        (prof.is_minion, "minion"),
        (prof.is_totem, "totem"),
        (prof.is_trap, "trap"),
        (prof.is_mine, "mine"),
        (prof.is_brand, "brand"),
        (prof.is_dot, "dot"),
        (prof.is_crit, "crit"),
        (prof.is_trigger, "trigger"),
        (prof.is_ci, "ci"),
        (prof.is_es_primary, "es"),
        (prof.is_mom, "mom"),
        (prof.is_agnostic, "agnostic"),
    ):
        if flag:
            prof.tags.add(lbl)
    prof.tags |= prof.damage_types | prof.dot_types | prof.weapon_types
    return prof


def _wheel_text_blob(graph: PassiveGraph, wheel: WheelInfo, *, exclude_minion_lines: bool = False) -> str:
    parts = [wheel.name.lower()]
    for nid in wheel.node_ids:
        nd = graph.nodes.get(nid) or {}
        if nd.get("type") == "Mastery":
            continue
        parts.append(str(nd.get("name") or "").lower())
        for ln in nd.get("stats") or []:
            ln_low = str(ln).lower()
            if exclude_minion_lines and "minion" in ln_low:
                continue
            parts.append(ln_low)
    return " | ".join(parts)


def is_wheel_relevant_for_build(graph: PassiveGraph, wheel: WheelInfo, profile: BuildProfile) -> bool:
    """Return True if wheel matches build tags or is a universal utility/defense wheel."""
    blob = _wheel_text_blob(graph, wheel)

    # 1. Hard defense paradigm incompatibilities (CI vs Life-only; Agnostic vs ES-only).
    if profile.is_ci and "maximum life" in blob and "energy shield" not in blob and "reservation" not in blob:
        return False
    if profile.is_agnostic and "energy shield" in blob and "maximum life" not in blob and "mana" not in blob:
        return False

    # 2. Archetype-specific wheels: matching active archetype keeps the wheel immediately;
    # non-matching archetype rejects it BEFORE player weapon/attack/spell checks.
    for arch_key, prof_flag in (
        ("minion", profile.is_minion),
        ("totem", profile.is_totem),
        ("trap", profile.is_trap),
        ("mine", profile.is_mine),
        ("brand", profile.is_brand),
    ):
        if any(k in blob for k in _ARCHETYPE_KEYWORDS[arch_key]):
            return bool(prof_flag)

    # Strip minion-scoped stat lines before evaluating player weapon/attack/spell filters.
    p_blob = _wheel_text_blob(graph, wheel, exclude_minion_lines=True)

    # 3. Weapon-restricted wheels: reject mismatched weapon classes BEFORE universal keyword checks.
    weapon_hits = {wt for wt, kws in _WEAPON_KEYWORDS.items() if any(k in p_blob for k in kws)}
    if weapon_hits:
        if "shield" in weapon_hits and "shield" in profile.weapon_types:
            return True
        if profile.weapon_types and not (weapon_hits & profile.weapon_types):
            return False
        if not profile.is_attack and "shield" not in weapon_hits and "staff" not in weapon_hits and "wand" not in weapon_hits:
            return False

    # 4. Secondary archetype filters (warcry / bleed / poison / ignite / spell-vs-attack).
    if any(k in p_blob for k in _ARCHETYPE_KEYWORDS["warcry"]) and not profile.is_attack:
        return False
    if any(k in p_blob for k in _ARCHETYPE_KEYWORDS["bleed"]) and "bleed" not in profile.dot_types:
        return False
    if any(k in p_blob for k in _ARCHETYPE_KEYWORDS["poison"]) and "poison" not in profile.dot_types:
        return False
    if any(k in p_blob for k in _ARCHETYPE_KEYWORDS["ignite"]) and "ignite" not in profile.dot_types and "fire" not in profile.damage_types:
        return False

    is_spell_w = any(k in p_blob for k in _ARCHETYPE_KEYWORDS["spell"])
    is_atk_w = any(k in p_blob for k in _ARCHETYPE_KEYWORDS["attack"]) or any(k in p_blob for k in _ARCHETYPE_KEYWORDS["melee"])
    if is_spell_w and not is_atk_w and not profile.is_spell:
        return False
    if is_atk_w and not is_spell_w and not profile.is_attack:
        return False

    # 5. Single-element / damage-type wheels: filter unrelated elements unless carrying core universal defense/aura.
    is_core_univ = any(k in blob for k in _CORE_UNIVERSAL_KEYWORDS)
    dmg_hits = {dt for dt, kws in _DAMAGE_KEYWORDS.items() if any(k in p_blob for k in kws)}
    if dmg_hits and profile.damage_types and not (dmg_hits & profile.damage_types) and not is_core_univ:
        return False

    if is_core_univ:
        return True
    if dmg_hits or is_spell_w or is_atk_w:
        return True
    # Keep general crit / elemental / accuracy / armour / evasion / block / attribute wheels.
    return any(
        k in blob
        for k in (
            "elemental damage",
            "critical strike",
            "accuracy",
            "armour",
            "evasion",
            "chance to block",
            "strength",
            "dexterity",
            "intelligence",
            "movement speed",
            "skill effect duration",
            "area of effect",
            "projectile",
        )
    )


def pick_best_mastery_effects(
    mastery_node: dict[str, Any],
    used_effect_ids: set[int],
    profile: BuildProfile,
    *,
    max_effects: int = 2,
) -> list[dict[str, Any]]:
    """Select top unused mastery effects on `mastery_node` honoring the Mastery Uniqueness Invariant."""
    effects = mastery_node.get("masteryEffects") or []
    scored: list[tuple[int, int, dict[str, Any]]] = []

    for eff in effects:
        if not isinstance(eff, dict):
            continue
        eid_raw = eff.get("effect") if eff.get("effect") is not None else eff.get("effectId")
        if eid_raw is None:
            continue
        eid = int(eid_raw)
        if eid in used_effect_ids:
            continue
        st_en = [str(s) for s in (eff.get("stats") or [])]
        st_zh = [str(s) for s in (eff.get("statsZh") or [])]
        txt = " ".join(st_en).lower()
        score = 1
        if any(
            k in txt
            for k in (
                "reservation efficiency",
                "increased maximum life",
                "to maximum life",
                "increased maximum energy shield",
                "to maximum energy shield",
                "spell suppression",
                "maximum fire resistance",
                "maximum cold resistance",
                "maximum lightning resistance",
                "maximum chaos resistance",
                "all maximum elemental resistances",
            )
        ):
            score += 5
        if any(
            k in txt
            for k in (
                "is lucky",
                "as inverted",
                "more ",
                "penetrates",
                "+1 to level",
                "multiplier",
                "aura",
                "suppressed",
                "damage per",
            )
        ):
            score += 4
        if any(k in txt for k in ("of enemies hitting you", "if you have at least 6", "you count as")):
            score -= 3
        if profile.is_spell and any(k in txt for k in ("spell", "cast", "exposure", "mana", "arcane surge")):
            score += 4
        if profile.is_attack and any(k in txt for k in ("attack", "accuracy", "impale", "melee", "projectile")):
            score += 4
        if profile.is_crit and ("critical" in txt or "crit" in txt):
            score += 4
        if profile.is_dot and ("damage over time" in txt or "ailment" in txt or "ignite" in txt or "poison" in txt or "bleed" in txt):
            score += 4
        for dt in profile.damage_types:
            if dt in txt:
                score += 4
        if any(k in txt for k in ("chests", "light radius", "rarity of items")):
            score -= 2
        scored.append(
            (
                -score,
                eid,
                {
                    "effectId": eid,
                    "stats": st_en,
                    "statsZh": st_zh or None,
                },
            )
        )

    scored.sort(key=lambda t: (t[0], t[1]))
    return [item[2] for item in scored[:max_effects]]
