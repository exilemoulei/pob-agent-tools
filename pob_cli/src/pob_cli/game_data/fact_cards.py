"""Gem and Unique 3.30 fact card builders, stat line normalizer, and ground context formatter."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .constants import _POB_ESCAPE_RE, MOD_TAG_PREFIX_RE

_INTERNAL_GEM_STAT_SUBSTRINGS: tuple[str, ...] = (
    "show_average_damage",
    "_is_gem",
    "display_",
    "_display",
    "stat_description",
    "quality_display_",
    "is_area_damage",
    "skill_can_fire_",
    "visual_hit_effect",
    "console_skill_dont_chase",
)

_DANGLING_STAT_CONT_RE = re.compile(
    r"\b(?:against|while|when|between|towards|without|during|into|onto|upon|than)\s*$",
    re.IGNORECASE,
)
_MOD_VARIANT_TAG_RE = re.compile(r"\{variant:([0-9,]+)\}")
_MOD_NUM_TEMPLATE_RE = re.compile(r"\([0-9.\-+%–]+\)|[+-]?\d+(?:\.\d+)?%?")
_GENERIC_STAT_LINE_RE = re.compile(
    r"^(?:"
    r"\+\(?\d[\d.\-()]*\)?\s+to\s+(?:strength|dexterity|intelligence|all\s+attributes|accuracy\s+rating|armour|evasion\s+rating)\b"
    r"|\+\(?\d[\d.\-()]*\)?%?\s+to\s+(?:fire|cold|lightning|chaos|all\s+elemental)\s+resistances?\b"
    r"|\+\(?\d[\d.\-()]*\)?\s+to\s+maximum\s+(?:life|mana|energy\s+shield)\b"
    r"|\(?\d[\d.\-()]*\)?%\s+increased\s+(?:armour|evasion(?:\s+rating)?|energy\s+shield|rarity\s+of\s+items\s+found|stun\s+and\s+block\s+recovery|light\s+radius)\b"
    r"|reflects\s+\(?\d[\d.\-()\s]*\)?\w+\s+damage\s+to\s+melee\s+attackers\b"
    r")",
    re.IGNORECASE,
)


def _is_internal_gem_stat(stat_key: Any) -> bool:
    low = str(stat_key or "").strip().lower()
    return (not low) or any(tok in low for tok in _INTERNAL_GEM_STAT_SUBSTRINGS)


def normalize_stat_lines(lines: Iterable[Any]) -> list[str]:
    """Normalize internal newlines in stat lines and stitch dangling line-wrap continuations."""
    out: list[str] = []
    for raw in lines:
        no_tags = _POB_ESCAPE_RE.sub("", MOD_TAG_PREFIX_RE.sub("", str(raw or "")))
        cleaned = " ".join(part.strip() for part in no_tags.splitlines() if part.strip())
        if not cleaned:
            continue
        if out and _DANGLING_STAT_CONT_RE.search(out[-1]):
            out[-1] = f"{out[-1]} {cleaned}"
        else:
            out.append(cleaned)
    return out


def _dedup_and_prioritize_unique_lines(raw_lines: Iterable[Any], variants: list[Any] | None = None) -> list[str]:
    raw_list = [str(x or "") for x in raw_lines if str(x or "").strip()]
    if not raw_list:
        return []
    variants_list = [str(v or "").strip() for v in (variants or [])]
    active_var_ids: set[int] = set()
    if variants_list:
        non_pre = {idx for idx, lbl in enumerate(variants_list, start=1) if not lbl.lower().startswith("pre ")}
        active_var_ids = non_pre or {len(variants_list)}
    else:
        seen_vids = {
            int(tok)
            for ln in raw_list
            for m in _MOD_VARIANT_TAG_RE.finditer(ln)
            for tok in m.group(1).split(",")
            if tok.isdigit()
        }
        if len(seen_vids) > 1:
            active_var_ids = {max(seen_vids)}

    filtered_raw: list[str] = []
    for ln in raw_list:
        v_matches = _MOD_VARIANT_TAG_RE.findall(ln)
        if v_matches and active_var_ids:
            line_vids = {int(tok) for grp in v_matches for tok in grp.split(",") if tok.isdigit()}
            if line_vids and not (line_vids & active_var_ids):
                continue
        filtered_raw.append(ln)

    cleaned = normalize_stat_lines(filtered_raw)
    deduped: list[str] = []
    tmpl_pos: dict[str, int] = {}
    for ln in cleaned:
        tmpl = re.sub(r"\s+", " ", _MOD_NUM_TEMPLATE_RE.sub("#", ln.lower())).strip()
        if tmpl in tmpl_pos:
            deduped[tmpl_pos[tmpl]] = ln
        else:
            tmpl_pos[tmpl] = len(deduped)
            deduped.append(ln)

    mechanic_lines = [ln for ln in deduped if not _GENERIC_STAT_LINE_RE.search(ln)]
    generic_lines = [ln for ln in deduped if _GENERIC_STAT_LINE_RE.search(ln)]
    return mechanic_lines + generic_lines


def _format_gem_fact_card(gem_obj: dict[str, Any], *, matched_by: str | None = None) -> dict[str, Any]:
    lvls = gem_obj.get("levels") or {}
    nat_max = gem_obj.get("natural_max_level")
    lv20 = (
        lvls.get("20")
        or (lvls.get(str(nat_max)) if nat_max is not None else None)
        or lvls.get("1")
        or (next(iter(lvls.values())) if lvls else {})
    )
    filtered_lv_stats: dict[str, Any] = {}
    for k, v in (lv20.get("stats") or {}).items():
        if not _is_internal_gem_stat(k):
            filtered_lv_stats[str(k)] = v
            if len(filtered_lv_stats) >= 6:
                break
    if len(filtered_lv_stats) < 6:
        for cs in gem_obj.get("constant_stats") or []:
            if not isinstance(cs, dict):
                continue
            ck = str(cs.get("stat") or "").strip()
            if ck and not _is_internal_gem_stat(ck) and ck not in filtered_lv_stats:
                filtered_lv_stats[ck] = cs.get("value")
                if len(filtered_lv_stats) >= 6:
                    break
    q_stats = [
        q
        for q in (gem_obj.get("quality_stats") or [])
        if isinstance(q, dict) and not _is_internal_gem_stat(q.get("stat"))
    ]
    card: dict[str, Any] = {
        "kind": "gem",
        "name": gem_obj.get("display_name") or gem_obj.get("name"),
        "zhName": gem_obj.get("zh_name"),
        "isSupport": bool(gem_obj.get("is_support")),
        "color": gem_obj.get("color"),
        "tags": gem_obj.get("tag_string"),
        "castTime": gem_obj.get("cast_time"),
        "cooldown": gem_obj.get("cooldown"),
        "storedUses": gem_obj.get("stored_uses"),
        "isExceptional": bool(gem_obj.get("is_exceptional")),
        "legacyRemoved328": bool(gem_obj.get("legacy_removed_328")),
        "naturalMaxLevel": nat_max,
        "corruptedMaxLevel": gem_obj.get("corrupted_max_level"),
        "critChance": gem_obj.get("crit_chance") or lv20.get("critChance"),
        "damageEffectiveness": gem_obj.get("damage_effectiveness") or lv20.get("damageEffectiveness"),
        "manaMultiplier": gem_obj.get("mana_multiplier"),
        "lv20Cost": lv20.get("cost"),
        "lv20Stats": filtered_lv_stats,
        "quality20": [
            {
                "stat": q.get("stat"),
                "perQuality": q.get("per_quality"),
                "at20": q.get("at_20_quality"),
                "at40": round(float(q.get("per_quality") or 0.0) * 40, 4),
            }
            for q in q_stats[:3]
        ],
    }
    variants = gem_obj.get("variants") or []
    if variants:
        card["variants"] = [
            f"{v.get('name')} ({v['zh_name']})" if v.get("zh_name") else str(v.get("name"))
            for v in variants[:4]
        ]
    if matched_by:
        card["matchedBy"] = matched_by
    return card


def _format_unique_fact_card(
    u_obj: dict[str, Any],
    *,
    matched_by: str | None = None,
    equipped_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    has_equipped = isinstance(equipped_override, dict) and bool(
        (equipped_override.get("explicits") or []) or (equipped_override.get("implicits") or [])
    )
    if has_equipped and equipped_override is not None:
        implicits = normalize_stat_lines(equipped_override.get("implicits") or [])[:4]
        explicits = normalize_stat_lines(equipped_override.get("explicits") or [])[:10]
    else:
        implicits = _dedup_and_prioritize_unique_lines(u_obj.get("implicits") or [], u_obj.get("variants"))[:3]
        explicits = _dedup_and_prioritize_unique_lines(u_obj.get("explicits") or [], u_obj.get("variants"))[:8]
    ov = equipped_override or {}
    card: dict[str, Any] = {
        "kind": "unique",
        "name": u_obj.get("name") or ov.get("name"),
        "zhName": u_obj.get("zh_name") or ov.get("zhName"),
        "baseType": ov.get("baseType") or u_obj.get("base_type"),
        "baseTypeZh": u_obj.get("base_type_zh"),
        "itemClass": u_obj.get("item_class"),
        "reqLevel": u_obj.get("req_level"),
        "implicits": [s for s in implicits if s],
        "explicits": [s for s in explicits if s],
    }
    if has_equipped:
        card["fromBuild"] = True
    if matched_by:
        card["matchedBy"] = matched_by
    return card


def _format_ground_context_block(
    *,
    pob_ver: str,
    data_ver: str,
    gamedata_available: bool,
    warnings: list[dict[str, Any]],
    resolved_slang: list[dict[str, str]],
    entities: list[dict[str, Any]],
) -> str:
    ctx_lines = [f"[Env: PoE1 3.30 | PoB {pob_ver} | Data {data_ver}]"]
    if not gamedata_available:
        ctx_lines.append(
            "- [INFO] Local PoE1 game data not bootstrapped yet; run `pob bootstrap` to attach live gem/unique fact cards."
        )
    for w in warnings:
        ctx_lines.append(f"- {w['tag']} `{w['trigger']}`: {w['message']}")
    if resolved_slang:
        slang_bits = ", ".join(f"{s['slang']}={s['canonical']}" for s in resolved_slang[:6])
        ctx_lines.append(f"- Slang: {slang_bits}")
    for ent in entities:
        zh_p = f" ({ent['zhName']})" if ent.get("zhName") else ""
        if ent["kind"] == "gem":
            bits = []
            if ent.get("isSupport"):
                bits.append("Support")
            if ent.get("castTime") is not None:
                bits.append(f"Cast={ent['castTime']}s")
            if ent.get("tags"):
                bits.append(f"Tags={ent['tags']}")
            st_items = list((ent.get("lv20Stats") or {}).items())[:4]
            if st_items:
                bits.append("Lv20: " + ", ".join(f"{k}={v}" for k, v in st_items))
            ctx_lines.append(f"- [Gem] {ent['name']}{zh_p}: " + " | ".join(bits))
        else:
            mods_preview = "; ".join(normalize_stat_lines(ent.get("explicits") or [])[:4])
            ctx_lines.append(f"- [Unique] {ent['name']}{zh_p} ({ent.get('baseType') or '-'}): {mods_preview}")
    return "\n".join(ctx_lines)
