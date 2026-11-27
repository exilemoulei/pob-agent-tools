"""PoB ModStore-faithful flag tables, tag checks, conversions, and minion/wrapper unpacking rules."""

from __future__ import annotations

from typing import Any

# Exact PoB Global.lua bitmask constants (PoE1/Data/Global.lua:103-173).
MOD_FLAG_BITS: dict[str, int] = {
    "Attack": 0x00000001,
    "Spell": 0x00000002,
    "Hit": 0x00000004,
    "Dot": 0x00000008,
    "Cast": 0x00000010,
    "Melee": 0x00000100,
    "Area": 0x00000200,
    "Projectile": 0x00000400,
    "Ailment": 0x00000800,
    "MeleeHit": 0x00001000,
    "Weapon": 0x00002000,
    "Axe": 0x00010000,
    "Bow": 0x00020000,
    "Claw": 0x00040000,
    "Dagger": 0x00080000,
    "Mace": 0x00100000,
    "Staff": 0x00200000,
    "Sword": 0x00400000,
    "Wand": 0x00800000,
    "Unarmed": 0x01000000,
    "Fishing": 0x02000000,
    "WeaponMelee": 0x04000000,
    "WeaponRanged": 0x08000000,
    "Weapon1H": 0x10000000,
    "Weapon2H": 0x20000000,
}

KEYWORD_FLAG_BITS: dict[str, int] = {
    "Aura": 0x00000001,
    "Curse": 0x00000002,
    "Warcry": 0x00000004,
    "Movement": 0x00000008,
    "Physical": 0x00000010,
    "Fire": 0x00000020,
    "Cold": 0x00000040,
    "Lightning": 0x00000080,
    "Chaos": 0x00000100,
    "Vaal": 0x00000200,
    "Bow": 0x00000400,
    "Arrow": 0x00000800,
    "Trap": 0x00001000,
    "Mine": 0x00002000,
    "Totem": 0x00004000,
    "Minion": 0x00008000,
    "Attack": 0x00010000,
    "Spell": 0x00020000,
    "Hit": 0x00040000,
    "Ailment": 0x00080000,
    "Brand": 0x00100000,
    "Poison": 0x00200000,
    "Bleed": 0x00400000,
    "Ignite": 0x00800000,
    "PhysicalDot": 0x01000000,
    "LightningDot": 0x02000000,
    "ColdDot": 0x04000000,
    "FireDot": 0x08000000,
    "ChaosDot": 0x10000000,
}

KEYWORD_FLAG_MATCH_ALL = 0x40000000
KEYWORD_MATCH_ALL_MASK = (~KEYWORD_FLAG_MATCH_ALL) & 0xFFFFFFFF

CONDITION_TAG_TYPES = frozenset(
    {
        "Condition",
        "ActorCondition",
        "Multiplier",
        "MultiplierThreshold",
        "PerStat",
        "StatThreshold",
        "SkillType",
        "SkillName",
        "SkillId",
        "SocketedIn",
        "ItemCondition",
        "DistanceRamp",
        "MeleeProximity",
        "PercentStat",
    }
)

_DEFENCE_NAME_TOKENS = (
    "Life",
    "Mana",
    "EnergyShield",
    "Armour",
    "Evasion",
    "Ward",
    "Resist",
    "Block",
    "Suppression",
    "Dodge",
    "DamageTaken",
    "Regen",
    "Reservation",
    "MovementSpeed",
    "LightRadius",
    "Flask",
    "StunAvoid",
    "Avoid",
    "AilmentAvoid",
    "ElementalAilment",
    "FreezeAvoid",
    "ShockAvoid",
    "ChillAvoid",
    "IgniteAvoid",
    "CurseEffectOnSelf",
)
_DEFENCE_EXACT_NAMES = frozenset({"Str", "Dex", "Int", "Omni", "AllAttributes", "AuraEffect", "BuffEffect"})

_OFFENSIVE_NAME_TOKENS = (
    "Damage",
    "Speed",
    "Crit",
    "Penetration",
    "Exposure",
    "Impale",
    "Bleed",
    "Ignite",
    "Poison",
    "Shock",
    "Chill",
    "Freeze",
    "Scorch",
    "Brittle",
    "Sap",
    "Ailment",
    "Dot",
    "Accuracy",
    "AreaOfEffect",
    "Projectile",
    "Pierce",
    "Chain",
    "Fork",
    "Split",
    "DoubleDamage",
    "TripleDamage",
    "GainAs",
    "ConvertTo",
    "Enemy",
    "Cull",
    "Overwhelm",
    "Minion",
    "Totem",
    "Trap",
    "Mine",
    "Brand",
)


def decode_bitmask(mask: int, table: dict[str, int]) -> list[str]:
    """Decode a PoB bitmask into sorted single-bit flag names."""
    if not mask:
        return []
    return sorted(name for name, bit in table.items() if (mask & bit) != 0)


def mod_flags_match(main_flags: int, mod_flags: int, offhand_flags: int | None = None) -> bool:
    """Exact PoB `ModDB.lua:144` ModFlag match-ALL semantics."""
    if mod_flags == 0:
        return True
    if (main_flags & mod_flags) == mod_flags:
        return True
    if offhand_flags is not None and (offhand_flags & mod_flags) == mod_flags:
        return True
    return False


def keyword_flags_match(main_kw: int, mod_kw: int) -> bool:
    """Exact PoB `Global.lua:190-215` `MatchKeywordFlags` semantics (match-ANY unless `MatchAll` bit is set)."""
    match_all = (mod_kw & KEYWORD_FLAG_MATCH_ALL) != 0
    mod_masked = mod_kw & KEYWORD_MATCH_ALL_MASK
    main_masked = main_kw & KEYWORD_MATCH_ALL_MASK
    if match_all:
        return (main_masked & mod_masked) == mod_masked
    return mod_masked == 0 or (main_masked & mod_masked) != 0


def format_tag_premise(tag: dict[str, Any]) -> str:
    """Format a PoB Mod tag table into a compact human-readable premise string."""
    ttype = str(tag.get("type") or "Tag")
    neg = "!" if tag.get("neg") else ""
    actor = f"{tag['actor']}:" if tag.get("actor") else ""
    var = (
        tag.get("var")
        or tag.get("stat")
        or tag.get("skillTypeName")
        or tag.get("skillName")
        or tag.get("skillId")
        or tag.get("slotName")
        or tag.get("keyword")
    )
    if not var:
        for list_key in ("varList", "skillTypeNames", "skillNameList"):
            vals = tag.get(list_key)
            if isinstance(vals, list) and vals:
                var = "|".join(str(x) for x in vals)
                break
    if ttype in ("Condition", "ActorCondition"):
        return f"Condition:{actor}{neg}{var or '?'}"
    if ttype == "Multiplier":
        extra = f"/{tag['div']}" if tag.get("div") else ""
        return f"Multiplier:{actor}{var or '?'}{extra}"
    if ttype in ("MultiplierThreshold", "StatThreshold"):
        op = "<=" if tag.get("upper") else ">="
        thr = tag.get("threshold") if tag.get("threshold") is not None else tag.get("thresholdVar", "?")
        return f"{ttype}:{actor}{var or '?'}{op}{thr}"
    if ttype == "PerStat":
        div = tag.get("div")
        return f"PerStat:{actor}{var or '?'}" + (f"/{div}" if div else "")
    if ttype == "SkillType":
        return f"SkillType:{neg}{var or tag.get('skillType', '?')}"
    if var:
        return f"{ttype}:{actor}{neg}{var}"
    return ttype


def unwrap_nested_mod(mod: dict[str, Any]) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
    """Unpack `MinionModifier` / `ExtraSkillMod` wrappers, returning `(effective_mod, is_minion_wrapper, outer_tags)`."""
    is_minion_wrapper = False
    outer_tags: list[dict[str, Any]] = []
    cur = mod
    for _ in range(3):
        nm = str(cur.get("name") or "")
        val = cur.get("value")
        if nm not in ("MinionModifier", "ExtraSkillMod") or not isinstance(val, dict):
            break
        inner = val.get("mod")
        if not isinstance(inner, dict):
            break
        if nm == "MinionModifier":
            is_minion_wrapper = True
        outer_tags.extend(t for t in (cur.get("tags") or []) if isinstance(t, dict))
        cur = inner
    if not outer_tags:
        return cur, is_minion_wrapper, []
    merged_tags = [*outer_tags, *(t for t in (cur.get("tags") or []) if isinstance(t, dict))]
    return {**cur, "tags": merged_tags}, is_minion_wrapper, outer_tags


def is_offensive_mod(mod: dict[str, Any], main_skill: dict[str, Any] | None = None) -> bool:
    """Return True when a parsed PoB mod scales offensive output (flags checked before defence short-circuit)."""
    name = str(mod.get("name") or "")
    mtype = str(mod.get("type") or "")
    flags = int(mod.get("flags") or 0)
    kw_flags = int(mod.get("keywordFlags") or 0) & KEYWORD_MATCH_ALL_MASK
    if flags != 0 or kw_flags != 0:
        return True
    if name in _DEFENCE_EXACT_NAMES:
        return False
    if any(tok in name for tok in _DEFENCE_NAME_TOKENS):
        return False
    if mtype in ("FLAG", "OVERRIDE"):
        return False
    if name == "Accuracy" or "Accuracy" in name:
        if not main_skill:
            return True
        ms_flags = int(main_skill.get("mainModFlags") or 0)
        ms_types = set(main_skill.get("skillTypes") or [])
        return (ms_flags & MOD_FLAG_BITS["Attack"]) != 0 or "Attack" in ms_types
    return any(tok in name for tok in _OFFENSIVE_NAME_TOKENS)


def _skill_name_matches(cand: str, target: str, *, include_transfigured: bool) -> bool:
    c_low = cand.strip().lower()
    t_low = target.strip().lower()
    if not c_low or not t_low:
        return False
    if c_low == t_low:
        return True
    if include_transfigured:
        return t_low.startswith(c_low + " of ") or c_low.startswith(t_low + " of ")
    return False


def _check_tag_mismatches(tags: list[Any], skill_ctx: dict[str, Any], root_skill: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    main_st = set(skill_ctx.get("skillTypes") or [])
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        ttype = tag.get("type")
        neg = bool(tag.get("neg"))
        if ttype == "SkillType" and main_st:
            st_names = [str(x) for x in (tag.get("skillTypeNames") or []) if x]
            if st_names:
                matched_list = [x for x in st_names if x in main_st]
                matched = bool(matched_list)
                if neg and matched:
                    reasons.append("NotSkillType:" + "|".join(matched_list))
                elif not neg and not matched:
                    reasons.append("SkillType:" + "|".join(st_names))
            else:
                st_name = tag.get("skillTypeName")
                if st_name:
                    if not neg and st_name not in main_st:
                        reasons.append(f"SkillType:{st_name}")
                    elif neg and st_name in main_st:
                        reasons.append(f"NotSkillType:{st_name}")
        elif ttype == "SkillName":
            target_nm = (
                str(root_skill.get("summonSkillName") or root_skill.get("skillName") or "")
                if tag.get("summonSkill")
                else str(root_skill.get("skillName") or "")
            )
            cands = [str(x) for x in (tag.get("skillNameList") or []) if x]
            if not cands and tag.get("skillName"):
                cands = [str(tag["skillName"])]
            if target_nm and cands:
                inc_tf = bool(tag.get("includeTransfigured"))
                matched = any(_skill_name_matches(c, target_nm, include_transfigured=inc_tf) for c in cands)
                if neg:
                    matched = not matched
                if not matched:
                    lbl = "NotSkillName:" if neg else "SkillName:"
                    reasons.append(lbl + "|".join(cands))
        elif ttype == "SkillId":
            want_id = str(tag.get("skillId") or "")
            cur_id = str(root_skill.get("skillId") or "")
            if want_id and cur_id:
                matched = want_id == cur_id
                if neg:
                    matched = not matched
                if not matched:
                    reasons.append(("NotSkillId:" if neg else "SkillId:") + want_id)
    return reasons


def _try_mod_flag_conversion(
    mod: dict[str, Any],
    mod_flags: int,
    main_flags: int,
    offhand_flags: int | None,
    conversions: dict[str, Any],
) -> str | None:
    name = str(mod.get("name") or "")
    mtype = str(mod.get("type") or "")
    if (
        conversions.get("spellDamageAppliesToAttacks")
        and name == "Damage"
        and mtype == "INC"
        and (mod_flags & MOD_FLAG_BITS["Spell"]) != 0
    ):
        conv = (mod_flags & ~MOD_FLAG_BITS["Spell"]) | MOD_FLAG_BITS["Attack"]
        if mod_flags_match(main_flags, conv, offhand_flags):
            return "ConvertedVia:SpellDamageAppliesToAttacks"
    if (
        conversions.get("castSpeedAppliesToAttacks")
        and name == "Speed"
        and mtype == "INC"
        and (mod_flags & MOD_FLAG_BITS["Cast"]) != 0
    ):
        conv = (mod_flags & ~MOD_FLAG_BITS["Cast"]) | MOD_FLAG_BITS["Attack"]
        if mod_flags_match(main_flags, conv, offhand_flags):
            return "ConvertedVia:CastSpeedAppliesToAttacks"
    if (
        conversions.get("dotIsSpell")
        and name == "Damage"
        and (mod_flags & MOD_FLAG_BITS["Spell"]) != 0
        and (main_flags & MOD_FLAG_BITS["Dot"]) != 0
    ):
        conv = (mod_flags & ~MOD_FLAG_BITS["Spell"]) | MOD_FLAG_BITS["Dot"]
        if mod_flags_match(main_flags, conv, offhand_flags):
            return "ConvertedVia:DotIsSpell"
    if (
        conversions.get("dotIsAttack")
        and name == "Damage"
        and (mod_flags & MOD_FLAG_BITS["Attack"]) != 0
        and (main_flags & MOD_FLAG_BITS["Dot"]) != 0
    ):
        conv = (mod_flags & ~MOD_FLAG_BITS["Attack"]) | MOD_FLAG_BITS["Dot"]
        if mod_flags_match(main_flags, conv, offhand_flags):
            return "ConvertedVia:DotIsAttack"
    if (
        conversions.get("clawDamageAppliesToUnarmed")
        and name == "Damage"
        and mtype == "INC"
        and (mod_flags & MOD_FLAG_BITS["Claw"]) != 0
    ):
        conv = (mod_flags & ~MOD_FLAG_BITS["Claw"]) | MOD_FLAG_BITS["Unarmed"]
        if mod_flags_match(main_flags, conv, offhand_flags):
            return "ConvertedVia:ClawDamageAppliesToUnarmed"
    return None


def evaluate_mod_against_skill(
    raw_mod: dict[str, Any],
    main_skill: dict[str, Any],
) -> tuple[list[str], list[str], bool]:
    """Evaluate a single serialized PoB mod against `main_skill`.

    Returns `(mismatches, conversion_notes, offensive)`.
    """
    mod, is_minion_mod, _outer_tags = unwrap_nested_mod(raw_mod)
    is_minion_build = bool(main_skill.get("isMinion"))
    conversions = main_skill.get("conversions") or {}
    conv_notes: list[str] = []

    if is_minion_mod and not is_minion_build:
        inner_name = str(mod.get("name") or "")
        inner_type = str(mod.get("type") or "")
        inner_flags = int(mod.get("flags") or 0)
        if conversions.get("minionDamageAppliesToPlayer") and inner_name == "Damage" and inner_type == "INC" and inner_flags == 0:
            conv_notes.append("ConvertedVia:SpiritualAid")
        elif (
            conversions.get("minionAttackSpeedAppliesToPlayer")
            and inner_name == "Speed"
            and inner_type == "INC"
            and (inner_flags == 0 or (inner_flags & MOD_FLAG_BITS["Attack"]) != 0)
        ):
            mod = {**mod, "flags": MOD_FLAG_BITS["Attack"]}
            conv_notes.append("ConvertedVia:SpiritualCommand")
        elif (
            conversions.get("minionCastSpeedAppliesToPlayer")
            and inner_name == "Speed"
            and inner_type == "INC"
            and (inner_flags == 0 or (inner_flags & MOD_FLAG_BITS["Cast"]) != 0)
        ):
            mod = {**mod, "flags": MOD_FLAG_BITS["Cast"]}
            conv_notes.append("ConvertedVia:MinionCastSpeedAppliesToPlayer")
        else:
            return ["ActorMismatch:MinionModifier does not apply to non-Minion main skill"], [], True

    skill_ctx = (
        main_skill.get("minionSkill")
        if (is_minion_build and is_minion_mod and isinstance(main_skill.get("minionSkill"), dict))
        else main_skill
    )
    offensive = is_offensive_mod(mod, skill_ctx)

    if is_minion_build and not is_minion_mod:
        nm = str(mod.get("name") or "")
        kw = int(mod.get("keywordFlags") or 0) & KEYWORD_MATCH_ALL_MASK
        targets_minion_or_enemy = (
            "Minion" in nm or nm.startswith("Enemy") or (kw & KEYWORD_FLAG_BITS["Minion"]) != 0
        )
        if offensive and not targets_minion_or_enemy:
            return ["ActorMismatch:Player offensive stat does not apply to Minions"], [], True

    reasons: list[str] = []
    main_flags = int(skill_ctx.get("mainModFlags") or 0)
    offhand_raw = skill_ctx.get("offhandModFlags")
    offhand_flags = int(offhand_raw) if offhand_raw is not None else None
    mod_flags = int(mod.get("flags") or 0)

    if not mod_flags_match(main_flags, mod_flags, offhand_flags):
        conv_note = _try_mod_flag_conversion(mod, mod_flags, main_flags, offhand_flags, conversions)
        if conv_note:
            conv_notes.append(conv_note)
        else:
            missing_bits = mod_flags & ~main_flags
            missing_names = decode_bitmask(missing_bits, MOD_FLAG_BITS) or [f"0x{missing_bits:x}"]
            reasons.append("ModFlag:" + "+".join(missing_names))

    main_kw = int(skill_ctx.get("mainKeywordFlags") or 0)
    mod_kw = int(mod.get("keywordFlags") or 0)
    if not keyword_flags_match(main_kw, mod_kw):
        match_all = (mod_kw & KEYWORD_FLAG_MATCH_ALL) != 0
        mod_masked = mod_kw & KEYWORD_MATCH_ALL_MASK
        main_masked = main_kw & KEYWORD_MATCH_ALL_MASK
        if match_all:
            missing_kw = decode_bitmask(mod_masked & ~main_masked, KEYWORD_FLAG_BITS)
            reasons.append("KeywordFlag(ALL):" + "+".join(missing_kw or [f"0x{mod_masked:x}"]))
        else:
            want_kw = decode_bitmask(mod_masked, KEYWORD_FLAG_BITS)
            reasons.append("KeywordFlag(ANY):" + "|".join(want_kw or [f"0x{mod_masked:x}"]))

    reasons.extend(_check_tag_mismatches(mod.get("tags") or [], skill_ctx, main_skill))
    return reasons, conv_notes, offensive
