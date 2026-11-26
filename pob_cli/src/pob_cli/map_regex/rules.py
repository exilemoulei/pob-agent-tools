"""Pure deterministic PoB build signal -> BRICK / DANGER / SAFE map-mod classifier."""

from __future__ import annotations

from typing import Any

from .corpus import load_corpus
from .reflect_rules import classify_reflect_with_meta as _classify_reflect_with_meta

_TOGGLEABLE_T17_MODS: frozenset[str] = frozenset(
    {
        "marked_for_death",
        "volatile_cores",
        "drowning_orbs",
        "sawblades",
        "meteor_on_flask",
        "union_of_souls",
        "tentacle_fiends",
        "runes_exarch",
        "labyrinth_hazards",
        "marked_ground",
        "enemy_auras",
        "maven_interferes",
        "shaper_touched",
        "monsters_block",
        "terrain_chain",
    }
)


def _num(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = d.get(key)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _lower_set(items: Any) -> set[str]:
    return {str(x).strip().lower() for x in (items or ()) if str(x).strip()}


def _has_any(haystack: set[str], *needles: str) -> bool:
    for nd in needles:
        ndl = nd.lower()
        if ndl in haystack or any(ndl in h for h in haystack):
            return True
    return False


def _classify_sustain(
    mod_id: str,
    probe: dict[str, Any],
    keystones: set[str],
    skills: set[str],
) -> tuple[str, str]:
    ms = probe.get("mainSkill") or {}
    dfn = probe.get("defence") or {}
    flasks = probe.get("flasks") or {}
    has_rf = _has_any(skills, "righteous fire")
    life_regen = _num(dfn, "lifeRegen")
    net_life = _num(dfn, "netLifeRegen")
    life_degen = _num(dfn, "lifeDegen")
    es_regen = _num(dfn, "esRegen")
    net_es = _num(dfn, "netEsRegen")
    net_mana = _num(dfn, "netManaRegen")
    mana_spend = _num(ms, "manaCost") * max(_num(ms, "speed", 1.0), 1.0) if _num(ms, "manaCost") > 0 else 0.0
    life_leech = _num(dfn, "lifeLeechRate")
    mana_leech = _num(dfn, "manaLeechRate")
    es_leech = _num(dfn, "esLeechRate")
    mgoh = _num(dfn, "manaGainOnHit")
    enduring = bool(flasks.get("hasEnduringMana"))
    has_eb = _has_any(keystones, "eldritch battery")

    if mod_id == "no_regen":
        if has_rf or (life_degen > 0 and net_life - life_regen < 0):
            return "brick", f"RF/degen turns NetLifeRegen negative without regen (Net={net_life:,.0f}/s)"
        if mana_spend > 0 and mana_leech <= 0 and mgoh <= 0 and not enduring and not (has_eb and es_leech > 0):
            return (
                "brick",
                f"Skill spends {mana_spend:,.0f} mana/s without leech, MGoH, or Enduring mana flask",
            )
        if net_life >= 150 or net_es >= 150 or es_regen >= 150:
            return "danger", f"Relies on regen sustain (NetLifeRegen={net_life:,.0f}/s, ESRegen={es_regen:,.0f}/s)"
        return "safe", "SAFE: sustain does not depend on regeneration"

    if mod_id == "no_leech":
        if _has_any(keystones, "vaal pact"):
            return "brick", "Vaal Pact disables life regeneration; no-leech removes primary recovery"
        if mana_spend > 0 and mana_spend > max(net_mana, 0.0) and (mana_leech > 0 or (has_eb and es_leech > 0)) and not enduring:
            return (
                "brick",
                f"Mana spend {mana_spend:,.0f}/s exceeds NetManaRegen {net_mana:,.0f}/s and relies on leech",
            )
        if life_leech > 0 or mana_leech > 0 or es_leech > 0:
            return (
                "danger",
                f"Active leech recovery disabled (Life {life_leech:,.0f}/s, Mana {mana_leech:,.0f}/s, ES {es_leech:,.0f}/s)",
            )
        return "safe", "SAFE: build does not rely on leech"

    if mod_id == "less_recovery":
        post_life = net_life - 0.6 * max(life_regen, 0.0)
        if (has_rf or life_degen > 0) and post_life < 0:
            return "brick", f"60% less recovery drops NetLifeRegen to {post_life:,.0f}/s (degen exceeds regen)"
        if net_life >= 150 or net_es >= 150 or life_leech > 0 or es_leech > 0:
            return "danger", f"60% less recovery cuts regen/leech sustain (NetLifeRegen={net_life:,.0f}/s)"
        return "safe", "SAFE: minimal dependence on Life/ES recovery rate"

    if mod_id == "less_leech_cap":
        if _has_any(keystones, "vaal pact") or life_leech >= 500 or es_leech >= 500:
            return "danger", f"Reduced max leech rate impairs leech recovery (Life {life_leech:,.0f}/s, ES {es_leech:,.0f}/s)"
        return "safe", "SAFE: build does not rely heavily on leech cap"

    return "safe", "SAFE"


def _classify_defence_and_curses(
    mod_id: str,
    probe: dict[str, Any],
    keystones: set[str],
    asc_nodes: set[str],
    uniques: set[str],
    skills: set[str],
) -> tuple[str, str]:
    dfn = probe.get("defence") or {}
    auras = probe.get("auras") or {}
    aura_count = int(_num(auras, "count", 0))
    aura_names = _lower_set(auras.get("names"))
    max_ele = max(_num(dfn, "fireMaxRes", 75), _num(dfn, "coldMaxRes", 75), _num(dfn, "lightMaxRes", 75))
    min_overcap = min(_num(dfn, "fireOvercap", 0), _num(dfn, "coldOvercap", 0), _num(dfn, "lightOvercap", 0))
    curse_eff = _num(dfn, "curseEffectOnSelf", 100.0)
    is_ci = _has_any(keystones, "chaos inoculation") or (_num(dfn, "life", 1000) <= 1.0 and _num(dfn, "es") > 0)
    is_ll = (not is_ci) and _num(dfn, "lifeUnreservedPct", 100.0) <= 50.0

    if mod_id == "max_res":
        if max_ele >= 83 or _has_any(skills, "righteous fire") or _has_any(uniques, "melding of the flesh"):
            return "brick", f"High max-res architecture ({max_ele:.0f}% max ele res / RF / Melding) severely penalized"
        return "danger", f"-max resistances cuts elemental mitigation from {max_ele:.0f}% cap"

    if mod_id == "less_aura":
        if aura_count <= 0:
            return "safe", "SAFE: 0 active auras"
        has_purity = _has_any(aura_names, "purity of")
        if (has_purity and min_overcap < 25) or is_ll or (is_ci and _has_any(aura_names, "discipline")):
            return "brick", f"{aura_count} aura(s) carry resist cap or CI/LL survival"
        return "danger", f"{aura_count} active non-curse aura(s) lose up to 60% effect"

    if mod_id == "ele_weakness":
        if curse_eff <= 0:
            return "safe", f"SAFE: CurseEffectOnSelf={curse_eff:.0f}% (curse immune)"
        if min_overcap >= 35:
            return "safe", f"SAFE: elemental overcap +{min_overcap:.0f}% absorbs Elemental Weakness"
        return "danger", f"Lowest elemental overcap is +{min_overcap:.0f}% (<35% needed for Elemental Weakness)"

    if mod_id == "temp_chains":
        if curse_eff <= 0:
            return "safe", f"SAFE: CurseEffectOnSelf={curse_eff:.0f}% (curse immune)"
        if _has_any(asc_nodes, "one step ahead", "unstoppable") or _num(dfn, "actionSpeedMin") >= 100:
            return "safe", "SAFE: Action Speed floor prevents Temporal Chains slow"
        return "danger", f"Temporal Chains slows Action Speed (CurseEffectOnSelf={curse_eff:.0f}%)"

    if mod_id == "vulnerability":
        if curse_eff <= 0:
            return "safe", f"SAFE: CurseEffectOnSelf={curse_eff:.0f}% (curse immune)"
        if _num(dfn, "physMaxHit", 25000) < 20000 and _num(dfn, "armour") < 15000:
            return "danger", f"Vulnerability amplifies physical hits (PhysMaxHit={_num(dfn, 'physMaxHit'):,.0f})"
        return "safe", "SAFE: physical mitigation absorbs Vulnerability"

    if mod_id == "less_armour_block":
        if _has_any(uniques, "aegis aurora", "svalinn"):
            return "brick", "Aegis Aurora / Svalinn recovery and block core crippled by less armour & block"
        blk = max(_num(dfn, "blockChance"), _num(dfn, "spellBlockChance"))
        if blk >= 60 or _num(dfn, "armour") >= 30000:
            return "danger", f"Block {blk:.0f}% / Armour {_num(dfn, 'armour'):,.0f} heavily reduced"
        return "safe", "SAFE: build does not rely on high block or armour"

    if mod_id == "less_suppression":
        supp = _num(dfn, "spellSuppressionChance")
        over = _num(dfn, "spellSuppressionOvercap")
        if supp >= 95 and over < 20:
            return "danger", f"Spell Suppression {supp:.0f}% (overcap +{over:.0f}%) loses prevented damage"
        return "safe", "SAFE: suppression not tightly reliant or overcapped"

    if mod_id == "monster_crit":
        cred = _num(dfn, "critExtraDamageReduction")
        if cred >= 100:
            return "safe", f"SAFE: CritExtraDamageReduction={cred:.0f}% (crit immune)"
        if cred < 50:
            return "danger", f"Low crit damage reduction ({cred:.0f}% < 50%) vs monster crit mods"
        return "safe", f"SAFE: CritExtraDamageReduction={cred:.0f}% mitigates monster crits"

    return "safe", "SAFE"


def _classify_offence_and_mechanics(
    mod_id: str,
    probe: dict[str, Any],
    keystones: set[str],
    asc_nodes: set[str],
    uniques: set[str],
    skills: set[str],
) -> tuple[str, str]:
    ms = probe.get("mainSkill") or {}
    charges = probe.get("charges") or {}
    cliffs = probe.get("triggerCliffs") or []
    curses = probe.get("curses") or {}
    comb = max(_num(ms, "combinedDps"), _num(ms, "totalDps"), 0.0)

    if mod_id == "less_cdr":
        has_trigger = bool(cliffs) or _has_any(
            skills | uniques, "cast on critical", "cast when damage taken", "mjolner", "poet's pen", "cospri's", "wardloop", "sword of ages"
        )
        if has_trigger:
            return "brick", f"Trigger/CDR breakpoint build ({len(cliffs)} trigger cliff(s)) desynced by less CDR"
        if _num(ms, "cooldown") > 0:
            return "danger", f"Main skill has {_num(ms, 'cooldown'):.2f}s cooldown slowed by less CDR"
        return "safe", "SAFE: no cooldown-bound trigger loop"

    if mod_id in ("steal_charges", "no_charges"):
        has_ralakesh = _has_any(uniques, "ralakesh's impatience")
        if has_ralakesh:
            return "safe", "SAFE: Ralakesh's Impatience grants permanent minimum-count charge effects"
        if _has_any(skills, "flicker strike"):
            return "brick", "Flicker Strike requires Frenzy Charge generation (no Ralakesh's Impatience)"
        active_max = [
            (k, int(_num(charges, f"max{k}")), int(_num(charges, f"min{k}")))
            for k in ("Power", "Frenzy", "Endurance")
            if charges.get(f"use{k}")
        ]
        if active_max and all(mn >= mx for _, mx, mn in active_max):
            return "safe", "SAFE: minimum charges equal maximum charges"
        vulnerable = [f"{k}({mn}/{mx})" for k, mx, mn in active_max if mx >= 3 and mn < mx]
        if vulnerable:
            return "danger", f"Relies on active charges ({', '.join(vulnerable)}) without Ralakesh"
        return "safe", "SAFE: build does not depend on charge stacking"

    if mod_id == "crit_res":
        cc = _num(ms, "critChance")
        if cc >= 40 and _num(ms, "totalDps") > 0:
            return "danger", f"Crit hit build ({cc:.0f}% crit chance) loses significant DPS"
        return "safe", f"SAFE: CritChance={cc:.0f}% (non-crit or DoT)"

    if mod_id == "avoid_ailments":
        if comb > 0 and _num(ms, "igniteDps") / comb >= 0.5:
            return "brick", f"Ignite is {_num(ms, 'igniteDps') / comb * 100:.0f}% of CombinedDPS"
        return "safe", "SAFE: build DPS does not primarily rely on elemental ailments"

    if mod_id == "avoid_poison_bleed":
        pb = _num(ms, "poisonDps") + _num(ms, "bleedDps")
        if comb > 0 and pb / comb >= 0.5:
            return "brick", f"Poison+Bleed is {pb / comb * 100:.0f}% of CombinedDPS"
        return "safe", "SAFE: build DPS does not primarily rely on poison/bleed"

    if mod_id == "less_accuracy":
        if ms.get("isAttack") and not _has_any(keystones, "resolute technique") and not ms.get("hitsCannotBeEvaded"):
            hc = _num(ms, "hitChance", 100.0)
            if hc < 100.0:
                return "danger", f"Attack build without RT/HitsCannotBeEvaded (HitChance={hc:.1f}%)"
        return "safe", "SAFE: spell/minion/RT/HitsCannotBeEvaded unaffected by less Accuracy"

    if mod_id == "monsters_hexproof":
        if _has_any(asc_nodes | uniques, "profane bloom", "cospri's will"):
            return "safe", "SAFE: Profane Bloom / Cospri's Will bypasses Hexproof"
        if _has_any(skills, "hexblast", "impending doom"):
            return "brick", "Hexblast / Impending Doom cannot function on Hexproof monsters"
        if int(_num(curses, "count", 0)) >= 2:
            return "danger", f"Multi-curse build ({int(_num(curses, 'count'))} curses) blocked by Hexproof"
        return "safe", "SAFE: build is not hex-dependent"

    if mod_id == "less_flask_charges":
        if _has_any(uniques, "mageblood"):
            return "safe", "SAFE: Mageblood utility flasks do not consume charges"
        return "safe", "SAFE: manageable flask charge penalty"

    if mod_id == "less_flask_effect":
        if _has_any(uniques, "mageblood"):
            return "danger", "Mageblood scaling heavily penalized by less flask effect"
        return "safe", "SAFE: not heavily flask-effect scaled"

    return "safe", "SAFE"


def _classify_t17_and_ground(
    mod_id: str,
    probe: dict[str, Any],
    keystones: set[str],
    asc_nodes: set[str],
    uniques: set[str],
    skills: set[str],
) -> tuple[str, str]:
    ms = probe.get("mainSkill") or {}
    dfn = probe.get("defence") or {}
    panth = probe.get("pantheon") or {}
    panth_set = _lower_set([panth.get("major"), panth.get("minor")])
    is_ci = _has_any(keystones, "chaos inoculation") or (_num(dfn, "life", 1000) <= 1.0 and _num(dfn, "es") > 0)
    is_ll = (not is_ci) and _num(dfn, "lifeUnreservedPct", 100.0) <= 50.0

    if mod_id == "action_speed":
        if _has_any(asc_nodes, "one step ahead"):
            return "safe", "SAFE: Trickster One Step Ahead prevents Action Speed below 108%"
        if _has_any(uniques, "garb of the ephemeral") or _has_any(asc_nodes, "unstoppable") or _num(dfn, "actionSpeedMin") >= 100:
            return "safe", "SAFE: Action Speed cannot be reduced below base value"
        spd = _num(ms, "speed")
        if spd >= 6.0 or bool(probe.get("triggerCliffs")):
            return "brick", f"High skill usage rate ({spd:.1f}/s) rapidly stacks Action Speed slow"
        if spd >= 3.0:
            return "danger", f"Moderate skill rate ({spd:.1f}/s) incurs noticeable Action Speed slow"
        return "safe", f"SAFE: low skill usage rate ({spd:.1f}/s)"

    if mod_id == "desolation":
        if _has_any(uniques, "dissolution of the flesh"):
            return "brick", "Dissolution of the Flesh breaks under continuous Awakeners' Desolation DoT"
        if _num(dfn, "netLifeRegen") < 100 and _num(dfn, "netEsRegen") < 100:
            return "danger", "Awakeners' Desolation DoT dangerous with low regen"
        return "safe", "SAFE: sufficient regen to navigate Awakeners' Desolation"

    if mod_id == "burning_ground":
        if _has_any(uniques, "dissolution of the flesh"):
            return "brick", "Dissolution of the Flesh prevents life reservation reset on Burning Ground"
        if _has_any(panth_set, "abberath") or dfn.get("unaffectedByBurningGround"):
            return "safe", "SAFE: Soul of Abberath / Burning Ground immunity"
        return "safe", "SAFE: Burning Ground manageable"

    if mod_id == "shocked_ground":
        if _num(dfn, "shockAvoid") >= 100 or dfn.get("unaffectedByShock") or _has_any(skills, "tempest shield"):
            return "safe", "SAFE: shock immune (100% Shock Avoid / Tempest Shield)"
        return "danger", f"Shocked Ground increases damage taken (ShockAvoid={_num(dfn, 'shockAvoid'):.0f}%)"

    if mod_id == "remove_life_es_on_hit":
        if is_ci or is_ll:
            return "brick", "CI / Low-Life pool severely punished by % Life/ES removal on hit"
        return "danger", "Rare/Unique hits strip 5% Life, Mana and ES directly"

    if mod_id == "less_defences":
        if is_ci or _num(dfn, "es") >= 4000 or (_num(dfn, "armour") + _num(dfn, "evasion")) >= 40000:
            return (
                "danger",
                f"Less Defences heavily cuts ES/Armour/Evasion (ES={_num(dfn, 'es'):,.0f}, "
                f"Arm={_num(dfn, 'armour'):,.0f}, Eva={_num(dfn, 'evasion'):,.0f})",
            )
        return "safe", "SAFE: build does not rely heavily on global defence scaling"

    if mod_id in _TOGGLEABLE_T17_MODS:
        return "safe", f"SAFE: toggleable T17 preset (add via --avoid {mod_id} if desired)"

    return "safe", "SAFE: no critical build conflict"


def classify_map_mods(
    probe: dict[str, Any],
    *,
    tier: str = "both",
    mode: str = "safe",
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify all in-tier map mods into `brick`, `danger`, or `safe` with reasons."""
    tier_norm = (tier or "both").strip().lower()
    if tier_norm not in ("t16", "t17", "both"):
        raise ValueError(f"Invalid tier {tier!r}; expected t16, t17, or both.")
    mode_norm = (mode or "safe").strip().lower()
    if mode_norm not in ("brick", "safe"):
        raise ValueError(f"Invalid mode {mode!r}; expected brick or safe.")

    corp = corpus or load_corpus("en", use_cn_overlay=False)
    keystones = _lower_set(probe.get("keystones"))
    asc_nodes = _lower_set(probe.get("ascendancyNodes"))
    uniques = _lower_set(probe.get("uniques"))
    skills = _lower_set(probe.get("activeSkillNames"))
    if probe.get("mainSkill", {}).get("name"):
        skills.add(str(probe["mainSkill"]["name"]).strip().lower())

    results: list[dict[str, Any]] = []
    for mod in corp["mods"]:
        mid = mod["id"]
        tiers = mod.get("tiers") or []
        if tier_norm != "both" and tier_norm not in tiers:
            continue

        exempt_meta: dict[str, Any] | None = None
        if mid == "reflect_ele":
            sev, reason, exempt_meta = _classify_reflect_with_meta(probe, "ele")
        elif mid == "reflect_phys":
            sev, reason, exempt_meta = _classify_reflect_with_meta(probe, "phys")
        elif mid in ("no_regen", "no_leech", "less_recovery", "less_leech_cap"):
            sev, reason = _classify_sustain(mid, probe, keystones, skills)
        elif mid in ("max_res", "less_aura", "ele_weakness", "temp_chains", "vulnerability", "less_armour_block", "less_suppression", "monster_crit"):
            sev, reason = _classify_defence_and_curses(mid, probe, keystones, asc_nodes, uniques, skills)
        elif mid in ("less_cdr", "steal_charges", "no_charges", "crit_res", "avoid_ailments", "avoid_poison_bleed", "less_accuracy", "monsters_hexproof", "less_flask_charges", "less_flask_effect"):
            sev, reason = _classify_offence_and_mechanics(mid, probe, keystones, asc_nodes, uniques, skills)
        else:
            sev, reason = _classify_t17_and_ground(mid, probe, keystones, asc_nodes, uniques, skills)

        excluded = (sev == "brick") or (mode_norm == "safe" and sev == "danger")
        row: dict[str, Any] = {
            "id": mid,
            "tiers": tiers,
            "severity": sev,
            "excluded": excluded,
            "reason": reason,
        }
        if exempt_meta is not None:
            row["exemptMeta"] = exempt_meta
        results.append(row)

    return {
        "tier": tier_norm,
        "mode": mode_norm,
        "classifications": results,
    }
