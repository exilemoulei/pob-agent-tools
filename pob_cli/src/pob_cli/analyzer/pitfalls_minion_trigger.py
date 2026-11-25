"""The Convocation guard: a triggered Convocation does not feed `used a Minion Skill Recently`."""

from __future__ import annotations

import re
from typing import Any

from ..formatters import strip_pob_escapes

_USED_MINION_RECENTLY_RE = re.compile(
    r"used\s+a\s+minion\s+skill\s+recently|近期(?:[内內]|有)?使用[了过過]?(?:召唤|召喚)(?:生物|物)技能|\boverseer['’]s\s+whip\b",
    re.IGNORECASE,
)
# What a build uses again and again: the gem skills PoB 2.69 types as Minion that are neither
# `permanentMinion` nor Triggered (Data/Skills/act_*.lua), less Convocation itself and Herald of Purity
# (reserved, not cast), plus Blood Offering and Death Wish, which items grant. A test checks it on the engine.
_HAND_CAST_MINION_SKILL_IDS: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "AnimateWeapon", "AnimateWeaponAltX", "AnimateWeaponAltY", "VaalAnimateWeapon",
        "BlinkArrow", "BlinkArrowAltX", "BlinkArrowAltY", "MirrorArrow", "MirrorArrowAltX", "MirrorArrowAltY",
        "BodyswapAltX", "BoneOffering", "FleshOffering", "SpiritOffering", "BloodOffering", "DarkPact", "DeathWish",
        "RaiseSpectreAltX", "RaiseZombieAltY", "SummonRagingSpirit", "SummonRagingSpiritAltX",
        "SummonReaper", "SummonReaperAltX", "SummonReaperAltY",
        "SummonSkeletons", "SummonSkeletonsAltX", "SummonSkeletonsAltY", "VaalSummonSkeletons",
        "Absolution", "AbsolutionAltX", "VaalAbsolution", "DominatingBlow", "DominatingBlowAltX", "VaalDomination",
        "HolyStrike",
    )
)
# A trigger fires only what it can support. Of the skills above, the Cast on/when/while supports,
# Spellslinger, Arcanist Brand and an item that triggers socketed spells take the ones PoB types
# Spell and Triggerable; Manaforged Arrows and Maloney's Mechanism take the bow attacks. The rest
# (the melee attacks, the Vaal skills, Death Wish) are used by hand whatever they are linked to.
# Automation takes instant spells only, so Convocation and none of these; Cast on Death takes no
# minion skill at all.
_TRIGGERABLE_MINION_SPELL_IDS: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "AnimateWeapon", "AnimateWeaponAltX", "AnimateWeaponAltY", "BodyswapAltX", "BoneOffering", "FleshOffering",
        "SpiritOffering", "BloodOffering", "DarkPact", "RaiseSpectreAltX", "RaiseZombieAltY",
        "SummonRagingSpirit", "SummonRagingSpiritAltX", "SummonReaper", "SummonReaperAltX",
        "SummonSkeletons", "SummonSkeletonsAltX", "SummonSkeletonsAltY", "Absolution", "AbsolutionAltX",
    )
)
_TRIGGERABLE_MINION_BOW_ATTACK_IDS: frozenset[str] = frozenset(
    s.lower() for s in ("BlinkArrow", "BlinkArrowAltX", "BlinkArrowAltY", "MirrorArrow", "MirrorArrowAltX", "MirrorArrowAltY")
)
_SPELL_TRIGGER_SUPPORT_IDS: frozenset[str] = frozenset(
    {
        "SupportCastOnCriticalStrike", "SupportCastOnCritTriggered", "SupportAwakenedCastOnCriticalStrike",
        "SupportCastOnCritTriggeredPlus", "SupportCastOnMeleeKill", "SupportCastOnMeleeKillTriggered",
        "SupportCastWhenStunned", "SupportCastWhileChannelling", "SupportCastWhileChannellingTriggered",
        "SupportAwakenedCastWhileChannelling", "SupportCastWhileChannellingTriggeredPlus",
        "SupportCastOnWardBreak", "SupportCastWhenDamageTaken",
    }
)
_SPELL_TRIGGER_SUPPORT_NAMES = (
    "cast when stunned", "cast on critical strike", "cast while channelling", "cast on melee kill",
    "cast on ward break", "cast when damage taken", "受伤时施放", "受傷時施放", "arcanist brand", "spellslinger",
)
# "Trigger a Socketed Spell ..." and "Trigger Socketed (Minion) Spells ..."; an item that names an
# element, curses or warcries fires none of these skills.
_ITEM_SPELL_TRIGGER_RE = re.compile(r"trigger (?:a )?socketed (?:minion )?spells?\b|触发插槽内")


def _find_recent_minion_sources(ctx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    c_blocks = ((ctx.get("cfg_res") or {}).get("customMods")) or []
    c_lines = [
        s for b in c_blocks if isinstance(b, dict) and b.get("enabled", True) is not False
        for ln in str(b.get("text") or "").splitlines()
        if (s := strip_pob_escapes(ln).strip()) and not s.startswith(("#", "//"))
    ]
    if _USED_MINION_RECENTLY_RE.search("\n".join(c_lines) if c_blocks else str(ctx.get("custom_mods_joined") or "")):
        out.append("customMods")

    act_it, raw_map = ctx.get("active_items_by_slot") or {}, ctx.get("raw_by_slot") or {}
    for sn in dict.fromkeys([*act_it.keys(), *raw_map.keys()]):
        if not sn or "Swap" in str(sn):
            continue
        it = act_it.get(sn) if isinstance(act_it.get(sn), dict) else {}
        blobs = [str(raw_map.get(sn) or "")]
        blobs.extend(strip_pob_escapes(str(it[k])) for k in ("raw", "title", "titleZh", "name", "nameZh") if it.get(k))
        for lk in ("implicits", "explicits", "mods", "craftedMods", "enchantMods", "fracturedMods"):
            if isinstance(it.get(lk), list):
                blobs.extend(strip_pob_escapes(str(v)) for v in it[lk] if v)
        if _USED_MINION_RECENTLY_RE.search("\n".join(blobs)):
            out.append(f"slot:{sn}")

    for n_lbl, st_lines in ctx.get("alloc_node_stats") or []:
        if _USED_MINION_RECENTLY_RE.search("\n".join([str(n_lbl or ""), *(str(x) for x in (st_lines or []))])):
            out.append(f"tree:{strip_pob_escapes(str(n_lbl or '')).strip() or 'passive'}")
    return list(dict.fromkeys(out))


def _check_minion_convocation_trigger(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    recent = _find_recent_minion_sources(ctx)
    raw_map = ctx.get("raw_by_slot") or {}
    trig_conv: list[tuple[Any, str, str]] = []
    man_conv: list[tuple[Any, str]] = []
    has_manual_combat = False

    for g in ctx.get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        g_idx, g_slot = g.get("index"), str(g.get("slot") or "-")
        parsed = [
            (
                [t for k in ("name", "nameSpec", "nameZh", "skillId") if (t := strip_pob_escapes(str(gm.get(k) or "")).strip().lower())],
                str(gm.get("skillId") or ""),
                bool(gm.get("support")),
            )
            for gm in (g.get("gems") or [])
            if isinstance(gm, dict) and gm.get("enabled", True)
        ]
        slot_low = str(raw_map.get(g_slot) or "").lower()
        has_auto = any(gsid in ("Automation", "SupportAutomation") or any(t in ("automation", "automation support", "自动化", "自動化") for t in toks) for toks, gsid, _ in parsed)
        has_cwdt = any(gsid == "SupportCastWhenDamageTaken" or any("cast when damage taken" in t or "受伤时施放" in t or "受傷時施放" in t for t in toks) for toks, gsid, _ in parsed)
        spell_trig = bool(_ITEM_SPELL_TRIGGER_RE.search(slot_low)) or any(
            gsid in _SPELL_TRIGGER_SUPPORT_IDS or any(p in " ".join(toks) for p in _SPELL_TRIGGER_SUPPORT_NAMES)
            for toks, gsid, _ in parsed
        )
        bow_trig = "trigger a socketed bow skill" in slot_low or any(
            gsid == "SupportManaforgedArrows" or "manaforged arrows" in " ".join(toks) for toks, gsid, _ in parsed
        )
        t_lbl = "Automation Support" if has_auto else ("Cast when Damage Taken Support" if has_cwdt else "triggered socket")
        for toks, gsid, is_sup in parsed:
            if gsid == "Convocation" or any(t in ("convocation", "号召", "號召") for t in toks):
                # Convocation is an instant, triggerable spell: Automation and every spell trigger take it.
                (trig_conv.append((g_idx, g_slot, t_lbl)) if (has_auto or spell_trig) else man_conv.append((g_idx, g_slot)))
            elif not is_sup:
                sid = gsid.lower()
                fired = (spell_trig and sid in _TRIGGERABLE_MINION_SPELL_IDS) or (bow_trig and sid in _TRIGGERABLE_MINION_BOW_ATTACK_IDS)
                if sid in _HAND_CAST_MINION_SKILL_IDS and not fired:
                    has_manual_combat = True

    if recent and trig_conv and not man_conv and not has_manual_combat:
        f_idx, f_slot, f_trig = trig_conv[0]
        loc = f"Group #{f_idx}" if f_idx is not None else f_slot
        gaps.append({
            "category": "TriggerMechanic", "severity": "HIGH",
            "message": (
                f"Convocation + {f_trig} ({loc}): `{f_trig}` triggers `Convocation` rather than 'using' it, "
                f"failing `if you've used a Minion Skill Recently` ({', '.join(recent[:3])}, e.g. Wickerman "
                "`Greater Overseer's Whip` minion aggression) with no manual combat minion skill. "
                "Unlink `Automation Support` / trigger support and cast `Convocation` manually."
            ),
        })
        mechanics["minionSkillTrigger"] = {
            "status": "bricked_by_trigger",
            "automatedConvocationGroups": [i for i, _, _ in trig_conv],
            "hasUsedMinionSkillRecentlyMod": True,
            "recentMinionModSources": recent,
        }
    elif man_conv and recent and not has_manual_combat:
        # Only a build that carries the condition, and feeds it with nothing else, depends on this.
        f_idx, f_slot = man_conv[0]
        loc = f"Group #{f_idx}" if f_idx is not None else f_slot
        note = (
            f"Manual Convocation (unautomated, {loc}) is what sustains "
            f"`if you've used a Minion Skill Recently` ({', '.join(recent[:3])}, e.g. Wickerman "
            "`Greater Overseer's Whip` minion aggression). Do NOT link `Automation Support` / `CwDT` or remove `Convocation`!"
        )
        mechanics["minionSkillTrigger"] = {
            "status": "active_dependency",
            "manualConvocationGroups": [i for i, _ in man_conv],
            "hasUsedMinionSkillRecentlyMod": True,
            "recentMinionModSources": recent,
            "note": note,
        }
        mechanics.setdefault("coreSynergies", []).append(note)
