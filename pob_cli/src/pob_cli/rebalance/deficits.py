"""Build attribute, resistance, suppression, and reservation deficit extraction for `pob rebalance`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..analyzer.static_checks import _resist_cap_and_gap
from ..analyzer.summary import _has_doryanis_prototype_equipped
from ..analyzer.threshold_probes import compute_threshold_probes
from ..formatters import _parse_numeric_or_none
from ..snapshot import _finite_float

HARD_ATTR_KEYS: tuple[str, ...] = ("Str", "Dex", "Int", "Omni")
ELE_RES_KEYS: tuple[str, ...] = ("Fire", "Cold", "Lightning")
ALL_RES_KEYS: tuple[str, ...] = ("Fire", "Cold", "Lightning", "Chaos")


@dataclass(frozen=True)
class RebalanceStep:
    """One atomic low-cost build adjustment candidate (Bench, Harvest, Catalyst, or Tattoo)."""

    tier: int
    kind: str
    slot: str
    target_key: str
    title: str
    est_cost_chaos: float
    cost_label: str
    covers: dict[str, float]
    apply_spec: dict[str, Any]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tier": self.tier,
            "kind": self.kind,
            "slot": self.slot,
            "targetKey": self.target_key,
            "title": self.title,
            "estCostChaos": round(self.est_cost_chaos, 1),
            "costLabel": self.cost_label,
            "covers": {k: round(v, 1) for k, v in self.covers.items()},
            "applySpec": dict(self.apply_spec),
        }
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass
class BuildDeficits:
    """Structured snapshot of attribute, resistance, and optional threshold gaps & slack."""

    attributes: dict[str, dict[str, float]] = field(default_factory=dict)
    resistances: dict[str, dict[str, float]] = field(default_factory=dict)
    optional: dict[str, Any] = field(default_factory=dict)
    has_doryani: bool = False
    is_chaos_immune: bool = False
    include_chaos: bool = False
    chaos_target: float = 0.0
    include_suppression: bool = False

    def hard_attr_deficits(self) -> dict[str, float]:
        return {k: v["deficit"] for k, v in self.attributes.items() if v.get("deficit", 0.0) > 0.0}

    def hard_resist_deficits(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for elem in ELE_RES_KEYS:
            if elem == "Lightning" and self.has_doryani:
                continue
            gap = float((self.resistances.get(elem) or {}).get("deficit") or 0.0)
            if gap > 0.0:
                out[elem] = gap
        if self.include_chaos and not self.is_chaos_immune:
            c_gap = float((self.resistances.get("Chaos") or {}).get("deficit") or 0.0)
            if c_gap > 0.0:
                out["Chaos"] = c_gap
        return out

    def need_map(self) -> dict[str, float]:
        """Map of every active target stat key -> required positive gain."""
        needs: dict[str, float] = {}
        needs.update(self.hard_attr_deficits())
        needs.update(self.hard_resist_deficits())
        if self.include_suppression:
            supp_gap = float((self.optional.get("spellSuppression") or {}).get("gapTo100Pct") or 0.0)
            if supp_gap > 0.0:
                needs["SpellSuppression"] = supp_gap
        return needs

    @property
    def has_hard_deficits(self) -> bool:
        return bool(self.hard_attr_deficits() or self.hard_resist_deficits())

    def hard_gap_total(self) -> float:
        return sum(self.hard_attr_deficits().values()) + sum(self.hard_resist_deficits().values())

    def resist_slack(self, elem: str) -> float:
        return float((self.resistances.get(elem) or {}).get("slack") or 0.0)

    def attr_surplus(self, attr: str) -> float:
        return float((self.attributes.get(attr) or {}).get("surplus") or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hasHardDeficits": self.has_hard_deficits,
            "hardGapTotal": round(self.hard_gap_total(), 1),
            "needs": {k: round(v, 1) for k, v in self.need_map().items()},
            "attributes": self.attributes,
            "resistances": self.resistances,
            "optional": self.optional,
            "hasDoryanisPrototype": self.has_doryani,
            "isChaosImmune": self.is_chaos_immune,
        }


def extract_build_deficits(
    stats: dict[str, Any],
    *,
    items_res: dict[str, Any] | None = None,
    skills_res: dict[str, Any] | None = None,
    agent_probe: dict[str, Any] | None = None,
    include_chaos: bool = False,
    chaos_target: float = 0.0,
    include_suppression: bool = False,
) -> BuildDeficits:
    """Extract exact attribute, resistance, and optional suppression/MRE deficits & slack from PoB stats."""
    st = stats if isinstance(stats, dict) else {}
    has_doryani = _has_doryanis_prototype_equipped(items_res or {})

    attributes: dict[str, dict[str, float]] = {}
    for attr in HARD_ATTR_KEYS:
        cur = _finite_float(st.get(attr), default=0.0)
        req = _finite_float(st.get(f"Req{attr}"), default=0.0)
        if attr == "Omni" and cur <= 0.0 and req <= 0.0:
            continue
        deficit = max(0.0, math.ceil(req - cur)) if req > cur else 0.0
        surplus = max(0.0, math.floor(cur - req)) if cur >= req else 0.0
        attributes[attr] = {
            "current": round(cur, 1),
            "required": round(req, 1),
            "deficit": float(deficit),
            "surplus": float(surplus),
        }

    resistances: dict[str, dict[str, float]] = {}
    for elem in ELE_RES_KEYS:
        val = _finite_float(st.get(f"{elem}Resist"), default=0.0)
        total_raw = st.get(f"{elem}ResistTotal")
        total_val = _finite_float(total_raw, default=val) if total_raw is not None else val
        over_raw = st.get(f"{elem}ResistOverCap")
        over = _finite_float(over_raw, default=0.0) if over_raw is not None else 0.0
        cap, gap = _resist_cap_and_gap(st, elem, val)
        if total_raw is not None and total_val < cap:
            gap = max(gap, cap - total_val)
        if elem == "Lightning" and has_doryani:
            gap = 0.0
        cur_disp = min(val, total_val) if total_raw is not None else val
        slack = max(0.0, over if over_raw is not None else max(0.0, total_val - cap))
        resistances[elem] = {
            "current": round(cur_disp, 1),
            "cap": round(cap, 1),
            "overcap": round(over, 1),
            "deficit": round(max(0.0, gap), 1),
            "slack": round(slack, 1),
        }

    chaos_val = _finite_float(st.get("ChaosResist"), default=0.0)
    chaos_total_raw = st.get("ChaosResistTotal")
    if chaos_total_raw is not None:
        chaos_val = min(chaos_val, _finite_float(chaos_total_raw, default=chaos_val))
    chaos_over_raw = st.get("ChaosResistOverCap")
    chaos_over = _finite_float(chaos_over_raw, default=0.0) if chaos_over_raw is not None else 0.0
    chaos_cap = _finite_float(st.get("ChaosResistMax"), default=75.0) or 75.0
    life_val = _finite_float(st.get("Life"), default=0.0)
    chaos_mh = _parse_numeric_or_none(st.get("ChaosMaximumHitTaken"))
    is_ci = (life_val <= 1.0 and chaos_mh is not None and math.isinf(chaos_mh)) or chaos_val >= 100.0
    c_target = float(chaos_target) if include_chaos else 0.0
    chaos_gap = 0.0 if is_ci else max(0.0, c_target - chaos_val)
    resistances["Chaos"] = {
        "current": round(chaos_val, 1),
        "cap": round(chaos_cap, 1),
        "overcap": round(chaos_over, 1),
        "deficit": round(chaos_gap, 1),
        "slack": round(max(0.0, chaos_over), 1),
        "negativeGap": 0.0 if is_ci else round(max(0.0, -chaos_val), 1),
    }

    probes = compute_threshold_probes(st, skills_res=skills_res, items_res=items_res, agent_probe=agent_probe)
    optional: dict[str, Any] = {
        "spellSuppression": probes.get("spellSuppression"),
        "manaReservation": probes.get("manaReservation"),
    }

    return BuildDeficits(
        attributes=attributes,
        resistances=resistances,
        optional=optional,
        has_doryani=has_doryani,
        is_chaos_immune=is_ci,
        include_chaos=include_chaos,
        chaos_target=c_target,
        include_suppression=include_suppression,
    )
