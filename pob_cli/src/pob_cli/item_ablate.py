"""Single-mod Copy-on-Write ablation (`pob item inspect <slot> --ablate`).

Measures each toggleable mod line's independent marginal impact on DPS, EHP,
resistances, attributes, and equipment/gem requirements using PoB's native
`ToggleDisplayItemModLine` inside a zero-residue `BuildTransaction`.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .analyzer.inspectors import resolve_slot_name
from .bridge_client import BridgeWorker
from .bridge_protocol import get_worker_caps
from .errors import DaemonRequestError
from .formatters import _parse_numeric_or_none, strip_pob_escapes
from .snapshot import (
    BuildScope,
    BuildTransaction,
    _finite_float,
    _safe_min_ele_max_hit,
    capture_digest,
    capture_direct_digest,
)

_EPS = 1e-4
_MAX_HIT_KEYS = (
    "PhysicalMaximumHitTaken",
    "FireMaximumHitTaken",
    "ColdMaximumHitTaken",
    "LightningMaximumHitTaken",
    "ChaosMaximumHitTaken",
)
_ANNULLABLE_CATEGORIES = frozenset({"explicit", "crafted"})
_NON_CRAFTABLE_RARITIES = frozenset({"UNIQUE", "RELIC"})
_TAG_PREFIX_RE = re.compile(r"^(?:\{[^}]+\})+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_UTILITY_GUARD_KEYWORDS = (
    "regen",
    "recover",
    "leech",
    "on hit",
    "on kill",
    "block",
    "mana cost",
    "flask",
    "charge",
    "ailment",
    "avoid",
    "stun",
    "curse",
    "cooldown",
    "area of effect",
)
_UTILITY_STAT_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lifeRegen", ("NetLifeRegen", "LifeRegenRecovery", "LifeRegen")),
    ("manaRegen", ("NetManaRegen", "ManaRegenRecovery", "ManaRegen")),
    ("esRegen", ("NetEnergyShieldRegen", "EnergyShieldRegen")),
    ("block", ("EffectiveBlockChance", "BlockChance")),
    ("spellBlock", ("EffectiveSpellBlockChance", "SpellBlockChance")),
    ("ward", ("Ward",)),
    ("manaCost", ("ManaCost", "SkillManaCost")),
    ("lifeOnHit", ("LifeOnHit",)),
    ("lifeLeech", ("LifeLeechGainRate",)),
)
_REQ_WARN_TOKENS = (
    "attribute requirement",
    "requirements",
    "don't have enough",
    "do not have enough",
    "cannot use",
    "strength requirement",
    "dexterity requirement",
    "intelligence requirement",
    "不足",
    "无法使用",
    "需求",
)


def _norm_mod_key(text: str) -> str:
    cleaned = _TAG_PREFIX_RE.sub("", strip_pob_escapes(text).strip())
    return " ".join(cleaned.split()).lower()


def _extract_fractured_keys(raw_text: str) -> set[str]:
    """Identify fractured mod lines from PoB raw item text (`{fractured}` prefix tag)."""
    out: set[str] = set()
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if "{fractured}" in s.lower():
            key = _norm_mod_key(s)
            if key:
                out.add(key)
    return out


def _slot_category(entry: dict[str, Any], fractured_keys: set[str]) -> str:
    kind = str(entry.get("kind") or "").strip().lower()
    section = str(entry.get("section") or "explicit").strip().lower()
    if kind == "crafted":
        return "crafted"
    if kind == "custom":
        return "custom"
    if kind == "crucible":
        return "crucible"
    text_key = _norm_mod_key(str(entry.get("text") or ""))
    if text_key and text_key in fractured_keys:
        return "fractured"
    return section or "explicit"


def _parse_item_craftability(raw_text: str, item_meta: dict[str, Any]) -> tuple[str, bool, bool, bool]:
    """Return `(rarity, corrupted, mirrored, craftable)` from raw PoB text and item metadata."""
    rarity = str(item_meta.get("rarity") or "").strip().upper()
    corrupted = bool(item_meta.get("corrupted"))
    mirrored = bool(item_meta.get("mirrored"))
    for raw_line in (raw_text or "").splitlines():
        s = raw_line.strip()
        low = s.lower()
        if not rarity and low.startswith("rarity:"):
            rarity = s.split(":", 1)[1].strip().upper()
        elif low == "corrupted":
            corrupted = True
        elif low == "mirrored":
            mirrored = True
    craftable = (rarity not in _NON_CRAFTABLE_RARITIES) and not corrupted and not mirrored
    return rarity, corrupted, mirrored, craftable


def _has_utility_guard(mod_text: str) -> bool:
    low = mod_text.lower()
    return any(kw in low for kw in _UTILITY_GUARD_KEYWORDS)


def _extract_utility_stats(stats: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for field, keys in _UTILITY_STAT_SPECS:
        val = 0.0
        for k in keys:
            if stats.get(k) is not None:
                val = _finite_float(stats.get(k))
                break
        out[field] = val
    return out


def _extract_movement_speed_mod(stats: dict[str, Any]) -> float:
    for key in ("EffectiveMovementSpeedMod", "MovementSpeedMod", "MovementSpeed"):
        if stats.get(key) is not None:
            return _finite_float(stats.get(key))
    return 0.0


def _extract_resists(stats: dict[str, Any]) -> dict[str, float]:
    return {
        "fire": _finite_float(stats.get("FireResist")),
        "cold": _finite_float(stats.get("ColdResist")),
        "lightning": _finite_float(stats.get("LightningResist")),
        "chaos": _finite_float(stats.get("ChaosResist")),
    }


def _extract_resist_overcaps(stats: dict[str, Any]) -> dict[str, float]:
    return {
        "fire": _finite_float(stats.get("FireResistOverCap")),
        "cold": _finite_float(stats.get("ColdResistOverCap")),
        "lightning": _finite_float(stats.get("LightningResistOverCap")),
        "chaos": _finite_float(stats.get("ChaosResistOverCap")),
    }


def _extract_attributes(stats: dict[str, Any]) -> dict[str, float]:
    out = {
        "str": _finite_float(stats.get("Str")),
        "dex": _finite_float(stats.get("Dex")),
        "int": _finite_float(stats.get("Int")),
        "omni": _finite_float(stats.get("Omni")),
        "reqStr": _finite_float(stats.get("ReqStr")),
        "reqDex": _finite_float(stats.get("ReqDex")),
        "reqInt": _finite_float(stats.get("ReqInt")),
        "reqOmni": _finite_float(stats.get("ReqOmni")),
    }
    return out


def _ehp_token(val: float | None) -> str:
    if val is None or math.isnan(val):
        return "nan"
    if math.isinf(val):
        return "inf" if val > 0 else "-inf"
    if abs(val - round(val)) < 1e-4:
        return str(round(val))
    return f"{val:.2f}".rstrip("0").rstrip(".")


def _stat_delta_or_changed(b_raw: Any, c_raw: Any) -> tuple[float | None, bool]:
    b_num = _parse_numeric_or_none(b_raw)
    c_num = _parse_numeric_or_none(c_raw)
    if b_num is not None and c_num is not None and math.isfinite(b_num) and math.isfinite(c_num):
        diff = c_num - b_num
        return round(diff, 2), abs(diff) > _EPS
    return None, _ehp_token(b_num) != _ehp_token(c_num)


def _build_baseline_block(digest: dict[str, Any]) -> dict[str, Any]:
    stats = digest.get("stats") or {}
    primary_key = str(digest.get("primaryDpsKey") or "CombinedDPS")
    attrs = _extract_attributes(stats)
    ehp_num = _parse_numeric_or_none(stats.get("TotalEHP"))
    ehp_inf = ehp_num is not None and math.isinf(ehp_num)
    total_ehp_val: float | str = (
        ("inf" if ehp_num > 0 else "-inf")
        if (ehp_inf and ehp_num is not None)
        else (round(ehp_num, 2) if (ehp_num is not None and math.isfinite(ehp_num)) else 0.0)
    )
    return {
        "primaryDpsKey": primary_key,
        "fullDps": round(_finite_float(stats.get("FullDPS")), 2),
        "combinedDps": round(_finite_float(stats.get("CombinedDPS")), 2),
        "primaryDps": round(_finite_float(stats.get(primary_key)), 2),
        "totalEhp": total_ehp_val,
        "totalEhpInfinite": ehp_inf,
        "life": round(_finite_float(stats.get("Life")), 2),
        "mana": round(_finite_float(stats.get("Mana")), 2),
        "energyShield": round(_finite_float(stats.get("EnergyShield")), 2),
        "spellSuppression": round(_finite_float(stats.get("EffectiveSpellSuppressionChance")), 2),
        "movementSpeedMod": round(_extract_movement_speed_mod(stats), 4),
        "resists": _extract_resists(stats),
        "resistOverCaps": _extract_resist_overcaps(stats),
        "attributes": attrs,
    }


def _detect_requirement_bricks(
    base_stats: dict[str, Any],
    cand_stats: dict[str, Any],
    base_warnings: set[str],
    cand_warnings: set[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for attr, req_key in (("Str", "ReqStr"), ("Dex", "ReqDex"), ("Int", "ReqInt"), ("Omni", "ReqOmni")):
        b_val = _finite_float(base_stats.get(attr))
        c_val = _finite_float(cand_stats.get(attr))
        c_req = _finite_float(cand_stats.get(req_key))
        b_req = _finite_float(base_stats.get(req_key))
        eff_req = c_req if c_req > 0 else b_req
        if eff_req > 0 and c_val < eff_req - 0.5:
            b_slack = b_val - b_req if b_req > 0 else 0.0
            c_slack = c_val - eff_req
            if b_val >= eff_req - 0.5 or c_slack < b_slack - 0.5:
                reasons.append(f"{attr} {round(c_val)} < Req {round(eff_req)} ({round(c_slack):+d})")

    base_has_cjk = any(_CJK_RE.search(w) for w in base_warnings)
    cand_has_cjk = any(_CJK_RE.search(w) for w in cand_warnings)
    locale_mismatch = bool(base_warnings and cand_warnings and base_has_cjk != cand_has_cjk)
    if not locale_mismatch:
        for warn in sorted(cand_warnings - base_warnings):
            low = warn.lower()
            if any(tok in low for tok in _REQ_WARN_TOKENS):
                reasons.append(warn)

    return bool(reasons), reasons


def _eval_single_mod_delta(
    *,
    entry: dict[str, Any],
    slot_category: str,
    base_digest: dict[str, Any],
    cand_digest: dict[str, Any],
) -> dict[str, Any]:
    b_stats = base_digest.get("stats") or {}
    c_stats = cand_digest.get("stats") or {}
    primary_key = str(base_digest.get("primaryDpsKey") or "CombinedDPS")

    base_dps = _finite_float(b_stats.get(primary_key))
    cand_dps = _finite_float(c_stats.get(primary_key))
    delta_dps = cand_dps - base_dps
    delta_dps_pct = (delta_dps / base_dps * 100.0) if abs(base_dps) > _EPS else 0.0

    base_ehp_num = _parse_numeric_or_none(b_stats.get("TotalEHP"))
    cand_ehp_num = _parse_numeric_or_none(c_stats.get("TotalEHP"))
    both_ehp_finite = (
        base_ehp_num is not None
        and cand_ehp_num is not None
        and math.isfinite(base_ehp_num)
        and math.isfinite(cand_ehp_num)
    )
    delta_ehp: float | None
    delta_ehp_pct: float | None
    ehp_transition: str | None = None
    if both_ehp_finite and base_ehp_num is not None and cand_ehp_num is not None:
        raw_d_ehp = cand_ehp_num - base_ehp_num
        delta_ehp = round(raw_d_ehp, 2)
        delta_ehp_pct = round((raw_d_ehp / base_ehp_num * 100.0) if abs(base_ehp_num) > _EPS else 0.0, 3)
        ehp_changed = abs(raw_d_ehp) > _EPS
    else:
        delta_ehp = None
        delta_ehp_pct = None
        b_tok, c_tok = _ehp_token(base_ehp_num), _ehp_token(cand_ehp_num)
        ehp_transition = f"{b_tok}->{c_tok}"
        ehp_changed = b_tok != c_tok

    max_hit_changed = any(_stat_delta_or_changed(b_stats.get(k), c_stats.get(k))[1] for k in _MAX_HIT_KEYS)
    phys_mh_d, _ = _stat_delta_or_changed(b_stats.get("PhysicalMaximumHitTaken"), c_stats.get("PhysicalMaximumHitTaken"))
    ele_mh_d, _ = _stat_delta_or_changed(_safe_min_ele_max_hit(b_stats), _safe_min_ele_max_hit(c_stats))
    chaos_mh_d, _ = _stat_delta_or_changed(b_stats.get("ChaosMaximumHitTaken"), c_stats.get("ChaosMaximumHitTaken"))

    b_res, c_res = _extract_resists(b_stats), _extract_resists(c_stats)
    d_res = {k: round(c_res[k] - b_res[k], 2) for k in ("fire", "cold", "lightning", "chaos")}
    b_ov, c_ov = _extract_resist_overcaps(b_stats), _extract_resist_overcaps(c_stats)
    d_ov = {k: round(c_ov[k] - b_ov[k], 2) for k in ("fire", "cold", "lightning", "chaos")}

    d_attr = {
        k: round(_finite_float(c_stats.get(sk)) - _finite_float(b_stats.get(sk)), 2)
        for k, sk in (("str", "Str"), ("dex", "Dex"), ("int", "Int"), ("omni", "Omni"))
    }
    d_life = round(_finite_float(c_stats.get("Life")) - _finite_float(b_stats.get("Life")), 2)
    d_mana = round(_finite_float(c_stats.get("Mana")) - _finite_float(b_stats.get("Mana")), 2)
    d_es = round(_finite_float(c_stats.get("EnergyShield")) - _finite_float(b_stats.get("EnergyShield")), 2)
    d_supp = round(
        _finite_float(c_stats.get("EffectiveSpellSuppressionChance"))
        - _finite_float(b_stats.get("EffectiveSpellSuppressionChance")),
        2,
    )
    d_ms_raw = _extract_movement_speed_mod(c_stats) - _extract_movement_speed_mod(b_stats)
    d_ms_pct = round(d_ms_raw * 100.0, 2)
    b_util, c_util = _extract_utility_stats(b_stats), _extract_utility_stats(c_stats)
    d_util = {k: round(c_util[k] - b_util[k], 2) for k in b_util if abs(c_util[k] - b_util[k]) > _EPS}

    bricks, brick_reasons = _detect_requirement_bricks(
        b_stats,
        c_stats,
        set(base_digest.get("warnings") or []),
        set(cand_digest.get("warnings") or []),
    )

    extra_dps_changed = any(
        abs(_finite_float(c_stats.get(k)) - _finite_float(b_stats.get(k))) > _EPS
        for k in ("FullDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS", "MinionCombinedDPS")
    )
    all_zero = (
        abs(delta_dps) <= _EPS
        and not extra_dps_changed
        and not ehp_changed
        and not max_hit_changed
        and not d_util
        and all(abs(v) <= _EPS for v in d_res.values())
        and all(abs(v) <= _EPS for v in d_ov.values())
        and all(abs(v) <= _EPS for v in d_attr.values())
        and abs(d_life) <= _EPS
        and abs(d_mana) <= _EPS
        and abs(d_es) <= _EPS
        and abs(d_supp) <= _EPS
        and abs(d_ms_raw) <= _EPS
    )
    is_dead = all_zero and not bricks

    mod_text = strip_pob_escapes(str(entry.get("text") or "")).strip()
    mod_text_zh = strip_pob_escapes(str(entry.get("textZh") or "")).strip()
    out: dict[str, Any] = {
        "lineIndex": int(entry.get("index") or 0),
        "modText": mod_text,
        "slotCategory": slot_category,
        "deltaDps": round(delta_dps, 2),
        "deltaDpsPct": round(delta_dps_pct, 3),
        "deltaEhp": delta_ehp,
        "deltaEhpPct": delta_ehp_pct,
        "deltaLife": d_life,
        "deltaMana": d_mana,
        "deltaEnergyShield": d_es,
        "deltaSuppression": d_supp,
        "deltaMovementSpeed": d_ms_pct,
        "deltaResists": d_res,
        "deltaResistOverCaps": d_ov,
        "deltaAttributes": d_attr,
        "bricksRequirements": bricks,
        "isDeadMod": is_dead,
    }
    if d_util:
        out["deltaUtility"] = d_util
    if ehp_transition is not None:
        out["ehpTransition"] = ehp_transition
    if max_hit_changed:
        out["deltaMaxHits"] = {"phys": phys_mh_d, "ele": ele_mh_d, "chaos": chaos_mh_d}
    if mod_text_zh and mod_text_zh != mod_text:
        out["modTextZh"] = mod_text_zh
    if brick_reasons:
        out["brickedReasons"] = brick_reasons
    return out


def ablate_item_mods(worker: BridgeWorker, slot_name: str) -> dict[str, Any]:
    """Toggle each mod line off on an equipped item in a 0-residue CoW transaction."""
    caps = get_worker_caps(worker)
    if not caps.get("itemModLineToggle"):
        raise DaemonRequestError(
            "Single-mod ablation (`--ablate`) requires engine capability `itemModLineToggle` "
            "(PoB 2.69+ / updated PobTools-zh bridge). Run `pob update` to upgrade your local engine."
        )

    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False) as tx:
        items_res = worker.call("list_items", {})
        resolved_slot = resolve_slot_name(worker, slot_name, items_res=items_res)
        slot_obj = next((s for s in (items_res.get("slots") or []) if s.get("name") == resolved_slot), None)
        item_id = slot_obj.get("selItemId") if isinstance(slot_obj, dict) else None
        if not item_id:
            raise DaemonRequestError(
                f"Slot '{resolved_slot}' is empty; equip an item before running `--ablate`."
            )
        item_id = int(item_id)
        item_meta = next((it for it in (items_res.get("items") or []) if it.get("id") == item_id), {})

        base_digest = capture_digest(worker)
        # Align baseline warnings with `capture_direct_digest` so zh-CN/zh-TW translated warnings
        # compare in the same locale/format as each ablated candidate pass.
        base_direct = capture_direct_digest(worker, base_digest=base_digest)
        eval_base_digest = {**base_digest, "warnings": list(base_direct.get("warnings") or base_digest.get("warnings") or [])}
        baseline = _build_baseline_block(base_digest)

        edit_state = worker.call("item_edit_begin", {"id": item_id})
        try:
            raw_text = str(edit_state.get("raw") or "")
            mod_lines = [ml for ml in (edit_state.get("modLines") or []) if isinstance(ml, dict)]
        finally:
            try:
                worker.call("item_edit_cancel", {})
            except Exception:
                pass

        fractured_keys = _extract_fractured_keys(raw_text)
        deltas: list[dict[str, Any]] = []

        for ml in mod_lines:
            if ml.get("disabled"):
                continue
            line_idx = int(ml.get("index") or 0)
            if line_idx <= 0:
                continue
            edit_open = False
            try:
                worker.call("item_edit_begin", {"id": item_id})
                edit_open = True
                worker.call("item_edit_set", {"modLine": {"index": line_idx, "enabled": False}})
                # `bridge.lua:5477` calls `AddDisplayItem(p.equip == false)`. Because `item_edit_begin`
                # preserves `newItem.id = src.id`, `ItemsTab:AddItem` replaces `tab.items[item_id]` in place
                # without re-auto-equipping the item into another slot (e.g. Ring 2 stays in Ring 2).
                worker.call("item_edit_commit", {"equip": True})
                edit_open = False
                cand_digest = capture_direct_digest(worker, base_digest=base_digest)
            finally:
                if edit_open:
                    try:
                        worker.call("item_edit_cancel", {})
                    except Exception:
                        pass
                tx.restore(rebuild=False)

            deltas.append(
                _eval_single_mod_delta(
                    entry=ml,
                    slot_category=_slot_category(ml, fractured_keys),
                    base_digest=eval_base_digest,
                    cand_digest=cand_digest,
                )
            )

        restore_check = tx.rollback_verified(base_digest)
        rarity, corrupted, mirrored, craftable = _parse_item_craftability(raw_text, item_meta)
        annullable_dead_count = sum(
            1
            for d in deltas
            if craftable
            and d["isDeadMod"]
            and d["slotCategory"] in _ANNULLABLE_CATEGORIES
            and not _has_utility_guard(str(d.get("modText") or ""))
        )
        item_name = strip_pob_escapes(
            str(item_meta.get("title") or item_meta.get("name") or resolved_slot)
        )
        item_name_zh = strip_pob_escapes(
            str(item_meta.get("titleZh") or item_meta.get("nameZh") or "")
        )
        out: dict[str, Any] = {
            "slotName": resolved_slot,
            "itemId": item_id,
            "itemName": item_name,
            "itemRarity": rarity or None,
            "itemCorrupted": corrupted,
            "itemMirrored": mirrored,
            "craftable": craftable,
            "raw": raw_text,
            "primaryDpsKey": baseline["primaryDpsKey"],
            "baseline": baseline,
            "ablation_deltas": deltas,
            "deadModCount": sum(1 for d in deltas if d["isDeadMod"]),
            "annullableDeadModCount": annullable_dead_count,
            "loadBearingCount": sum(1 for d in deltas if d["bricksRequirements"]),
            "restoreCheck": restore_check,
        }
        if item_name_zh and item_name_zh != item_name:
            out["itemNameZh"] = item_name_zh
        return out
