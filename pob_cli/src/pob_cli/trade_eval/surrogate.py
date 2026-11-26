"""Linear `grad Build` surrogate pre-filter with local/global quality provenance."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from ..trade_item import Item

if TYPE_CHECKING:
    from .scoring import Candidate

MIN_SURROGATE_PREFILTER_K = 50

_QUALITY_LINE_RE = re.compile(r"^quality\s*:\s*\+?(\d+)\s*%", re.IGNORECASE)
_WEAPON_PROP_PREFIXES = ("physical damage:", "elemental damage:", "attacks per second:", "weapon range:")
_ARMOUR_PROP_PREFIXES = ("armour:", "evasion rating:", "evasion:", "energy shield:", "ward:")


def compute_grad_weights(
    worker: Any,
    *,
    stat: str | None = None,
    ehp_weight: float = 0.5,
    include_resists: bool = True,
) -> dict[str, Any] | None:
    """Query `compute_grad_build` marginal sensitivity weights when `worker.caps.agentProbes` is active."""
    caps = getattr(worker, "caps", None) or {}
    if not caps.get("agentProbes"):
        return None
    params: dict[str, Any] = {
        "ehpWeight": float(ehp_weight),
        "includeResists": bool(include_resists),
    }
    if stat:
        params["stat"] = stat
    try:
        res = worker.call("compute_grad_build", params)
        if isinstance(res, dict) and isinstance(res.get("weights"), dict) and not res.get("_error"):
            return res
    except Exception:
        pass
    return None


def _item_quality_context(item: Item) -> tuple[bool, bool, int, bool]:
    """Return `(is_weapon, is_armour, listed_quality, unmodifiable)` from parsed item headers."""
    listed_q = 0
    is_weapon = False
    is_armour = False
    unmodifiable = bool(item.corrupted or item.mirrored)
    for line in item.lines:
        low = line.strip().lower()
        if low == "unmodifiable":
            unmodifiable = True
        elif low.startswith(_WEAPON_PROP_PREFIXES):
            is_weapon = True
        elif low.startswith(_ARMOUR_PROP_PREFIXES):
            is_armour = True
        elif m := _QUALITY_LINE_RE.match(low):
            listed_q = int(m.group(1))
    return is_weapon, is_armour, listed_q, unmodifiable


def _extract_surrogate_features_split(item: Item) -> tuple[dict[str, float], dict[str, float], bool, bool, int, bool]:
    """Extract coarse 24-dim features split by local vs global quality provenance."""
    is_weapon, is_armour, listed_q, unmodifiable = _item_quality_context(item)
    local_feats: dict[str, float] = {}
    global_feats: dict[str, float] = {}

    def _add(bucket: dict[str, float], key: str, amount: float) -> None:
        if math.isfinite(amount) and amount != 0.0:
            bucket[key] = bucket.get(key, 0.0) + amount

    for mod in item.mods:
        tmpl = mod.template
        vals = [float(v) for v in (mod.values or ()) if math.isfinite(float(v))]
        if not vals:
            continue
        val = sum(vals) / len(vals)
        if not math.isfinite(val):
            continue
        if "adds # to # physical damage" in tmpl:
            # Weapon local flat phys lacks "to attacks"/"to spells"; armour/jewellery flat attack phys is global.
            is_local_phys = is_weapon and "to attacks" not in tmpl and "to spells" not in tmpl
            _add(local_feats if is_local_phys else global_feats, "flat_phys", val)
        elif "adds # to # fire damage" in tmpl:
            _add(global_feats, "flat_fire", val)
        elif "adds # to # cold damage" in tmpl:
            _add(global_feats, "flat_cold", val)
        elif "adds # to # lightning damage" in tmpl:
            _add(global_feats, "flat_light", val)
        elif "adds # to # chaos damage" in tmpl:
            _add(global_feats, "flat_chaos", val)
        elif "increased spell damage" in tmpl:
            _add(global_feats, "inc_spell_dmg", val)
        elif "increased attack" in tmpl and "damage" in tmpl:
            _add(global_feats, "inc_attack_dmg", val)
        elif "increased" in tmpl and "damage" in tmpl:
            _add(global_feats, "inc_damage", val)
        elif "critical strike chance" in tmpl:
            _add(global_feats, "inc_crit_chance", val)
        elif "critical strike multiplier" in tmpl:
            _add(global_feats, "crit_multi", val)
        elif "attack speed" in tmpl or "cast speed" in tmpl:
            _add(global_feats, "inc_speed", -val if "reduced" in tmpl else val)
        elif "to level of" in tmpl and "gem" in tmpl:
            _add(global_feats, "gem_level", val)
        elif "penetrates" in tmpl and "resistance" in tmpl:
            _add(global_feats, "minus_enemy_res", val)
        elif "to maximum life" in tmpl:
            _add(global_feats, "flat_life", val)
        elif "increased maximum life" in tmpl:
            _add(global_feats, "inc_life", val)
        elif "to maximum energy shield" in tmpl:
            _add(local_feats if is_armour else global_feats, "flat_es", val)
        elif "increased" in tmpl and "energy shield" in tmpl:
            is_local_es = is_armour and "maximum energy shield" not in tmpl
            _add(local_feats if is_local_es else global_feats, "inc_es", val)
        elif "to armour" in tmpl:
            _add(local_feats if is_armour else global_feats, "flat_armour", val)
        elif "to evasion" in tmpl:
            _add(local_feats if is_armour else global_feats, "flat_evasion", val)
        elif "to maximum mana" in tmpl:
            _add(global_feats, "flat_mana", val)
        elif "to all elemental resistances" in tmpl:
            _add(global_feats, "fire_res", val)
            _add(global_feats, "cold_res", val)
            _add(global_feats, "light_res", val)
        elif "to fire resistance" in tmpl:
            _add(global_feats, "fire_res", val)
        elif "to cold resistance" in tmpl:
            _add(global_feats, "cold_res", val)
        elif "to lightning resistance" in tmpl:
            _add(global_feats, "light_res", val)
        elif "to chaos resistance" in tmpl:
            _add(global_feats, "chaos_res", val)
        elif "to strength" in tmpl:
            _add(global_feats, "flat_life", 0.5 * val)
        elif "to intelligence" in tmpl:
            _add(global_feats, "flat_mana", 0.5 * val)
            _add(global_feats, "inc_es", 0.2 * val)
    return local_feats, global_feats, is_weapon, is_armour, listed_q, unmodifiable


def extract_item_surrogate_features(item: Item) -> dict[str, float]:
    """Extract coarse 24-dim feature values from parsed `Item` mods for linear surrogate pre-ranking."""
    local_feats, global_feats, *_ = _extract_surrogate_features_split(item)
    merged = dict(global_feats)
    for k, v in local_feats.items():
        merged[k] = merged.get(k, 0.0) + v
    return merged


def _weight_scalar(entry: Any) -> float:
    if isinstance(entry, (int, float)):
        return float(entry) if math.isfinite(float(entry)) else 0.0
    if isinstance(entry, dict):
        for key in ("scorePerUnit", "score_per_unit", "dStatPerUnit", "relDpsPerUnit"):
            raw = entry.get(key)
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                return float(raw)
    return 0.0


def score_candidate_surrogate(
    item: Item,
    grad_weights: dict[str, Any],
    *,
    normalize_quality: bool = False,
) -> float:
    """Compute linear dot-product pre-filter score, applying quality scaling only to local contributions."""
    raw_weights = grad_weights.get("weights") if isinstance(grad_weights.get("weights"), dict) else grad_weights
    if not isinstance(raw_weights, dict):
        return 0.0
    local_feats, global_feats, is_weapon, is_armour, listed_q, unmodifiable = _extract_surrogate_features_split(item)
    if is_weapon or is_armour:
        eff_q = max(listed_q, 20) if (normalize_quality and not unmodifiable) else listed_q
        q_mult = 1.0 + eff_q / 100.0
    else:
        q_mult = 1.0
    total = 0.0
    for dim, val in global_feats.items():
        if dim in raw_weights:
            total += val * _weight_scalar(raw_weights[dim])
    for dim, val in local_feats.items():
        if dim in raw_weights:
            total += (val * q_mult) * _weight_scalar(raw_weights[dim])
    return total if math.isfinite(total) else 0.0


def prefilter_candidates_with_surrogate(
    candidates: list[Candidate],
    grad_weights: dict[str, Any] | None,
    *,
    top_k: int = MIN_SURROGATE_PREFILTER_K,
    normalize_quality: bool = False,
) -> list[Candidate]:
    """Pre-filter candidates using linear `grad Build` weights with a hard `K >= 50` floor (Rule P3)."""
    effective_k = max(MIN_SURROGATE_PREFILTER_K, int(top_k))
    if not grad_weights or len(candidates) <= effective_k:
        return list(candidates)
    raw_w = grad_weights.get("weights") if isinstance(grad_weights.get("weights"), dict) else grad_weights
    if not isinstance(raw_w, dict) or not raw_w:
        return list(candidates)
    scored = [
        (score_candidate_surrogate(cand.item, grad_weights, normalize_quality=normalize_quality), idx, cand)
        for idx, cand in enumerate(candidates)
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:effective_k]]
