"""Eldritch Battery detection, Replica Sorrow of the Divine flask awareness, and Mana/ES sustain audit."""

from __future__ import annotations

from typing import Any

from ..formatters import strip_pob_escapes
from ..game_data import fold_han
from ..snapshot import _finite_float
from .summary import _active_item_lines, _compute_effective_mana_spend


def is_eldritch_battery_active(stats: dict[str, Any], tree_summary: dict[str, Any] | None) -> bool:
    """Return True when Eldritch Battery protects Mana (via engine stat or allocated keystone)."""
    raw_eb = stats.get("EnergyShieldProtectsMana")
    if raw_eb is True or (raw_eb is not False and _finite_float(raw_eb) > 0):
        return True
    for ks in (tree_summary or {}).get("keystones") or []:
        blob = (
            f"{ks.get('name') or ''} {ks.get('nameZh') or ''}" if isinstance(ks, dict) else str(ks or "")
        ).lower()
        if any(t in blob for t in ("eldritch battery", "异能魔力", "異能魔力")):
            return True
    return False


def is_replica_sorrow_eb_item(item: dict[str, Any] | None, extra_raw: str = "") -> bool:
    """Return True when `item` (or its raw text) is `Replica Sorrow of the Divine` / grants `Eldritch Battery during Effect`."""
    if not isinstance(item, dict) and not extra_raw:
        return False
    it = item if isinstance(item, dict) else {}
    name_parts = [
        strip_pob_escapes(str(it.get(k) or ""))
        for k in ("title", "name", "titleZh", "nameZh")
    ]
    raw_lines: list[str] = []
    for rtxt in (str(it.get("raw") or ""), str(extra_raw or "")):
        if rtxt:
            raw_lines.extend(strip_pob_escapes(ln) for ln in _active_item_lines(rtxt))
    joined = "\n".join([*name_parts, *raw_lines])
    low = joined.lower()
    if "replica sorrow of the divine" in low or "eldritch battery during effect" in low:
        return True
    folded = fold_han(low)
    if "神圣哀悼" in folded and ("仿品" in folded or "赝品" in folded):
        return True
    # zh-rCN writes the line with 生效期间, zh-rTW (folded) with 持续时间内.
    return "异能魔力" in folded and any(t in folded for t in ("生效期间", "效果期间", "持续时间内"))


def find_equipped_eb_flask_slots(
    items_res: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, bool]]:
    """Return `[(slot_name, item_title, is_active), ...]` for equipped `Replica Sorrow of the Divine` flasks."""
    if not isinstance(items_res, dict):
        return []
    items_by_id = {
        it["id"]: it for it in (items_res.get("items") or []) if isinstance(it, dict) and "id" in it
    }
    raw_by_slot: dict[str, str] = {}
    for entry in equipped_slots_raw or []:
        if isinstance(entry, dict) and entry.get("slotName") and entry.get("raw"):
            raw_by_slot[str(entry["slotName"])] = str(entry["raw"])

    found: list[tuple[str, str, bool]] = []
    for slot in items_res.get("slots") or []:
        if not isinstance(slot, dict) or not slot.get("isFlask") or slot.get("inactive"):
            continue
        iid = slot.get("selItemId")
        sname = str(slot.get("name") or "")
        if not iid or not sname:
            continue
        it = items_by_id.get(iid) or {}
        if is_replica_sorrow_eb_item(it, raw_by_slot.get(sname, "")):
            title = strip_pob_escapes(
                str(it.get("titleZh") or it.get("title") or it.get("name") or sname)
            ).strip()
            found.append((sname, title or sname, bool(slot.get("active"))))
    return found


def _fmt_eb_pool_summary(es_val: float, es_for_mana: float, mana_unres: float, es_cost: float) -> str:
    tot = es_for_mana + mana_unres
    if es_cost > 0:
        return f"{es_for_mana:.0f} remaining ES + {mana_unres:.0f} Unreserved Mana = {tot:.0f} after {es_cost:.0f} ES Cost"
    return f"{es_val:.0f} ES + {mana_unres:.0f} Unreserved Mana = {tot:.0f}"


_UNMODELLED_FLAGS = ("totem", "minion", "trap", "mine", "triggered")
_TOTEM_TOKENS = ("totem", "ballista", "图腾", "圖騰")
_MINION_ACTIVE_PREFIXES = ("summon ", "raise ", "animate ")
_TRIGGER_SUPPORT_TOKENS = ("cast when", "cast on", "cast while", "trigger", "spellslinger", "arcanist brand", "automation")


def _unmodelled_spend_reason(
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    skill_flags: dict[str, Any] | None,
) -> str | None:
    """Why ManaCost x Speed does not describe the player's mana spend, or None when it may.

    Totems, traps and mines cast on their own, minion DPS is not the summon's cast rate, and a
    triggered skill's rate is not paid from the player's casting (often 0-cost or trigger-gated).
    """
    flags = skill_flags if isinstance(skill_flags, dict) else {}
    for f in _UNMODELLED_FLAGS:
        if flags.get(f):
            return f
    if _finite_float(stats.get("SkillTriggerRate")) > 0:
        return "triggered"
    groups = (skills_res or {}).get("groups") or []
    main_idx = (skills_res or {}).get("mainSocketGroup")
    main_group = next(
        (g for g in groups if isinstance(g, dict) and (g.get("isMain") or (main_idx is not None and g.get("index") == main_idx))),
        None,
    )
    gems = [gm for gm in (main_group or {}).get("gems") or [] if isinstance(gm, dict) and gm.get("enabled", True)]
    for gm in gems:
        if gm.get("support"):
            blob = " ".join(_name_parts(gm))
            if any(t in blob for t in _TOTEM_TOKENS):
                return "totem"
            if any(t in blob for t in _TRIGGER_SUPPORT_TOKENS):
                return "triggered"
    selected = _selected_active_parts(main_group or {}, gems)
    if any(t in part for part in selected for t in _TOTEM_TOKENS):
        return "totem"
    if any(part.startswith(_MINION_ACTIVE_PREFIXES) for part in selected):
        return "minion"
    return None


def _name_parts(entry: dict[str, Any]) -> list[str]:
    parts = (strip_pob_escapes(str(entry.get(k) or "")).strip().lower() for k in ("name", "nameSpec", "nameZh", "skillId"))
    return [part for part in parts if part]


def _selected_active_parts(group: dict[str, Any], gems: list[dict[str, Any]]) -> list[str]:
    """Names of the group's selected active skill; empty when the listing does not say which it is.

    Another active gem in the same links (a Decoy Totem next to Fireball) says nothing about the
    selected skill, so only that skill's own name may rule the estimate out.
    """
    skills = group.get("skills") or []
    try:
        idx = int(group.get("mainActiveSkill") or 1)
    except (TypeError, ValueError):
        idx = 1
    if 1 <= idx <= len(skills) and isinstance(skills[idx - 1], dict):
        return _name_parts(skills[idx - 1])
    actives = [gm for gm in gems if not gm.get("support")]
    return _name_parts(actives[0]) if len(actives) == 1 else []


def collect_mana_sustain_issues(
    stats: dict[str, Any],
    skills_res_audit: dict[str, Any] | None,
    *,
    tree_summary: dict[str, Any] | None = None,
    items_res: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    skill_flags: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Evaluate static Mana/ES cost & recovery issues with Eldritch Battery + `Replica Sorrow of the Divine` awareness."""
    issues: list[dict[str, str]] = []
    mana_cost = _finite_float(stats.get("ManaCost"))
    mana_unres = _finite_float(stats.get("ManaUnreserved"))
    es_val = max(0.0, _finite_float(stats.get("EnergyShield")))
    es_cost = _finite_float(stats.get("ESCost"))
    eb_active = is_eldritch_battery_active(stats, tree_summary)
    # An active one already shows as Eldritch Battery in the stats; only one that is off needs a word.
    eb_flasks = [f for f in find_equipped_eb_flask_slots(items_res, equipped_slots_raw) if not f[2]]
    es_for_mana = max(0.0, es_val - es_cost)
    payable_pool = (mana_unres + es_for_mana) if eb_active else mana_unres
    eb_covers_mana = eb_active and es_for_mana >= mana_cost
    eb_flask_pool = mana_unres + es_for_mana

    raw_mana_regen = (
        stats.get("ManaRegenRecovery")
        if stats.get("ManaRegenRecovery") is not None
        else stats.get("ManaRegen")
    )
    mana_regen = _finite_float(raw_mana_regen)
    mana_leech = _finite_float(stats.get("ManaLeechGainRate"))
    speed = _finite_float(stats.get("Speed"))
    raw_life_unres = (
        stats.get("LifeUnreserved") if stats.get("LifeUnreserved") is not None else stats.get("Life")
    )
    life_unres = _finite_float(raw_life_unres, default=1.0)
    spend_info = _compute_effective_mana_spend(stats, skills_res_audit)
    spend_rate = float(spend_info["spendRate"])
    eff_casts = float(spend_info["effectiveCastsPerSec"])
    repeats = int(spend_info["repeats"])
    rec_rate = max(0.0, mana_regen) + mana_leech
    exceeds_recovery = (spend_rate > rec_rate * 1.10) if rec_rate > 0.0 else (spend_rate > 0.0)

    if es_cost > 0 and es_cost > es_val:
        issues.append(
            {
                "category": "Sustain",
                "severity": "HIGH",
                "message": f"Skill ES Cost ({es_cost:.0f}) exceeds Energy Shield ({es_val:.0f})",
            }
        )

    if mana_cost > 0 and mana_cost > payable_pool and life_unres > 0:
        if not eb_active and eb_flasks:
            eb_desc = ", ".join(f"{sn} ({ttl})" for sn, ttl, _ in eb_flasks)
            flask_no = eb_flasks[0][0].rsplit(" ", 1)[-1]
            pool_summary = _fmt_eb_pool_summary(es_val, es_for_mana, mana_unres, es_cost)
            if eb_flask_pool >= mana_cost:
                issues.append(
                    {
                        "category": "Sustain",
                        "severity": "LOW",
                        "message": (
                            f"Skill Mana Cost ({mana_cost:.0f}) exceeds Unreserved Mana ({mana_unres:.0f}) while "
                            f"{eb_desc} is off; the flask grants Eldritch Battery during Effect "
                            f"({pool_summary} >= {mana_cost:.0f}). To audit with it on: `pob item flask {flask_no} on --apply`"
                        ),
                    }
                )
            else:
                issues.append(
                    {
                        "category": "Sustain",
                        "severity": "HIGH",
                        "message": (
                            f"Skill Mana Cost ({mana_cost:.0f}) exceeds Unreserved Mana ({mana_unres:.0f}) "
                            f"(even with {eb_desc} Eldritch Battery: {pool_summary})"
                        ),
                    }
                )
        else:
            if eb_active and es_cost > 0:
                pool_msg = (
                    f"Eldritch Battery remaining ES + Unreserved Mana "
                    f"({es_for_mana:.0f} + {mana_unres:.0f} = {payable_pool:.0f} after {es_cost:.0f} ES Cost)"
                )
            elif eb_active:
                pool_msg = f"Eldritch Battery ES + Unreserved Mana ({es_val:.0f} + {mana_unres:.0f} = {payable_pool:.0f})"
            else:
                pool_msg = f"Unreserved Mana ({mana_unres:.0f})"
            issues.append(
                {
                    "category": "Sustain",
                    "severity": "HIGH",
                    "message": f"Skill Mana Cost ({mana_cost:.0f}) exceeds {pool_msg}",
                }
            )
    elif mana_cost > 0 and not eb_covers_mana and mana_regen < 0:
        issues.append(
            {
                "category": "Sustain",
                "severity": "MEDIUM",
                "message": f"Net Mana Regen is negative ({mana_regen:.1f}/s) with Mana Cost {mana_cost:.0f}",
            }
        )
    elif (
        mana_cost > 0
        and not eb_covers_mana
        and eff_casts > 0
        and exceeds_recovery
        and life_unres > 0
        and _unmodelled_spend_reason(stats, skills_res_audit, skill_flags) is None
    ):
        detail_bits: list[str] = []
        if repeats > 1:
            detail_bits.append(f"raw {speed:.2f}/s / {repeats}x repeats")
        if spend_info.get("cooldownCapped") and spend_info.get("effectiveCooldown"):
            detail_bits.append(f"capped by {float(spend_info['effectiveCooldown']):.2f}s CD")
        detail_suffix = f"; {', '.join(detail_bits)}" if detail_bits else ""
        issues.append(
            {
                "category": "Sustain",
                "severity": "LOW",
                "message": (
                    f"Mana spend rate ({spend_rate:.1f}/s = {mana_cost:.0f} * {eff_casts:.2f}/s{detail_suffix}) "
                    f"exceeds net Mana recovery + leech ({rec_rate:.1f}/s) "
                    "[estimate: ManaCost x cast rate, not engine-verified]"
                ),
            }
        )
    return issues


def collect_dewatered_eb_flask_note(
    dewatered_stats: dict[str, Any],
    *,
    tree_summary: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
    stripped_flasks: list[str] | None,
    existing_issues: list[dict[str, str]],
) -> dict[str, str] | None:
    """Emit a LOW Sustain note when dewatering stripped an active `Replica Sorrow of the Divine` that covered ManaCost."""
    if not stripped_flasks or is_eldritch_battery_active(dewatered_stats, tree_summary):
        return None
    if any("Eldritch Battery" in str(g.get("message") or "") for g in existing_issues):
        return None
    eb_flasks = find_equipped_eb_flask_slots(items_res, equipped_slots_raw)
    stripped_eb = [
        (sn, ttl)
        for sn, ttl, was_active in eb_flasks
        if was_active and any(str(sf).startswith(f"{sn} ") for sf in stripped_flasks)
    ]
    if not stripped_eb:
        return None

    mana_cost = _finite_float(dewatered_stats.get("ManaCost"))
    mana_unres = _finite_float(dewatered_stats.get("ManaUnreserved"))
    es_val = max(0.0, _finite_float(dewatered_stats.get("EnergyShield")))
    es_cost = _finite_float(dewatered_stats.get("ESCost"))
    raw_life_unres = (
        dewatered_stats.get("LifeUnreserved")
        if dewatered_stats.get("LifeUnreserved") is not None
        else dewatered_stats.get("Life")
    )
    life_unres = _finite_float(raw_life_unres, default=1.0)
    if mana_cost <= 0 or mana_cost <= mana_unres or life_unres <= 0:
        return None

    es_for_mana = max(0.0, es_val - es_cost)
    eb_pool = mana_unres + es_for_mana
    if eb_pool < mana_cost:
        return None

    eb_desc = ", ".join(f"{sn} ({ttl})" for sn, ttl in stripped_eb)
    keep_slot = stripped_eb[0][0]
    pool_summary = _fmt_eb_pool_summary(es_val, es_for_mana, mana_unres, es_cost)
    return {
        "category": "Sustain",
        "severity": "LOW",
        "message": (
            f"Dewatering stripped {eb_desc} ('Eldritch Battery during Effect'), leaving dewatered "
            f"Unreserved Mana ({mana_unres:.0f}) below Skill Mana Cost ({mana_cost:.0f}) while "
            f"ES + Unreserved Mana ({pool_summary} >= {mana_cost:.0f}) covers casting during flask effect; "
            f"verify flask uptime or run with --keep '{keep_slot}'"
        ),
    }
