"""PoE1 Large Cluster Jewel 8-passive 2-socket Front-2 Back-1 topology solver.

Faithfully mirrors PoB's ``PoE1/Modules/ItemTools.lua`` (``getClusterJewelNotableIndices``
and ``getClusterJewelNotablePositions``) and ``PoE1/Classes/PassiveSpec.lua`` (``BuildSubgraph``)
over ``ClusterJewels.lua`` and ``ModJewelCluster.lua``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cluster_data import (
    LARGE_CLUSTER_GEOMETRY,
    LARGE_ENCHANT_OPTION_IDS,
    ClusterData,
    ClusterEnchant,
    ClusterGeometry,
    ClusterNotable,
    _build_offline_fallback_data,
    load_cluster_data,
)
from .cluster_resolve import (
    resolve_cluster_notable,
    resolve_large_enchant,
    suggest_cluster_notables,
)

__all__ = [
    "LARGE_CLUSTER_GEOMETRY",
    "LARGE_ENCHANT_OPTION_IDS",
    "ClusterData",
    "ClusterEnchant",
    "ClusterGeometry",
    "ClusterLayout",
    "ClusterNotable",
    "ClusterSolution",
    "NotablePlacement",
    "RejectedCandidate",
    "_build_offline_fallback_data",
    "compute_cluster_node_layout",
    "load_cluster_data",
    "resolve_cluster_notable",
    "resolve_large_enchant",
    "solve_front2_back1",
    "suggest_cluster_notables",
]


@dataclass(frozen=True)
class NotablePlacement:
    name: str
    index: int
    position: str  # "Front" | "Back" | "End"
    sort_order: int


@dataclass(frozen=True)
class ClusterLayout:
    passives: int
    sockets: int
    notable_placements: tuple[NotablePlacement, ...]
    socket_indices: tuple[int, ...]
    small_indices: tuple[int, ...]
    entrance_index: int
    min_points_all_notables_and_sockets: int
    min_points_front_notables_and_sockets: int


@dataclass(frozen=True)
class RejectedCandidate:
    notable: ClusterNotable
    reason: str


@dataclass
class ClusterSolution:
    enchant: ClusterEnchant
    notable_a: ClusterNotable
    notable_b: ClusterNotable
    passives: int
    sockets: int
    ilvl_max: int | None
    possible: bool
    reason: str | None
    explanation: str
    required_back_affix: str | None
    valid_back_notables: list[ClusterNotable] = field(default_factory=list)
    rejected_sandwich: list[RejectedCandidate] = field(default_factory=list)
    ilvl_filtered_out: list[ClusterNotable] = field(default_factory=list)
    min_ilvl_ab: int = 1
    min_ilvl_overall: int | None = None
    warnings: list[str] = field(default_factory=list)
    layout_3_notable: ClusterLayout | None = None
    layout_2_notable: ClusterLayout | None = None
    alternative_enchants: list[dict[str, Any]] = field(default_factory=list)
    data_source: str = "local_engine"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataSource": self.data_source,
            "enchant": {
                "tag": self.enchant.tag,
                "name": self.enchant.name,
                "stats": list(self.enchant.stats),
                "tradeOptionId": self.enchant.trade_option_id,
            },
            "passives": self.passives,
            "sockets": self.sockets,
            "ilvlMax": self.ilvl_max,
            "frontNotables": [
                _notable_dict(self.notable_a, self.enchant.tag),
                _notable_dict(self.notable_b, self.enchant.tag),
            ],
            "possible": self.possible,
            "reason": self.reason,
            "explanation": self.explanation,
            "requiredBackAffix": self.required_back_affix,
            "validBackNotables": [_notable_dict(n, self.enchant.tag) for n in self.valid_back_notables],
            "rejectedSandwich": [
                {**_notable_dict(r.notable, self.enchant.tag), "rejectReason": r.reason}
                for r in self.rejected_sandwich
            ],
            "ilvlFilteredOut": [_notable_dict(n, self.enchant.tag) for n in self.ilvl_filtered_out],
            "minIlvlAB": self.min_ilvl_ab,
            "minIlvlOverall": self.min_ilvl_overall,
            "warnings": list(self.warnings),
            "layout3Notable": _layout_dict(self.layout_3_notable) if self.layout_3_notable else None,
            "layout2Notable": _layout_dict(self.layout_2_notable) if self.layout_2_notable else None,
            "alternativeEnchants": list(self.alternative_enchants),
        }


def _notable_dict(n: ClusterNotable, enchant_tag: str) -> dict[str, Any]:
    return {
        "name": n.name,
        "sortOrder": n.sort_order,
        "affixType": n.affix_type,
        "minIlvl": n.level,
        "group": n.group,
        "weight": n.weights.get(enchant_tag, 0),
        "statId": n.stat_id_hint,
    }


def _layout_dict(lay: ClusterLayout) -> dict[str, Any]:
    return {
        "passives": lay.passives,
        "sockets": lay.sockets,
        "entranceIndex": lay.entrance_index,
        "socketIndices": list(lay.socket_indices),
        "smallIndices": list(lay.small_indices),
        "notables": [
            {"name": p.name, "index": p.index, "position": p.position, "sortOrder": p.sort_order}
            for p in lay.notable_placements
        ],
        "minPointsAllNotablesAndSockets": lay.min_points_all_notables_and_sockets,
        "minPointsFrontNotablesAndSockets": lay.min_points_front_notables_and_sockets,
    }


def _min_ring_points(occupied_ring: list[int], targets: set[int]) -> int:
    """Minimum passive points on a closed cluster ring starting at index 0 to reach all ``targets``."""
    if not occupied_ring:
        return 0
    pos = {node_idx: i for i, node_idx in enumerate(occupied_ring)}
    required = sorted({pos[t] for t in targets if t in pos} | {0})
    n = len(occupied_ring)
    if len(required) <= 1:
        return len(required)
    max_gap = 0
    for i in range(len(required)):
        cur = required[i]
        nxt = required[(i + 1) % len(required)]
        gap = (nxt - cur) % n
        if gap > max_gap:
            max_gap = gap
    return n - max_gap + 1


def compute_cluster_node_layout(
    notables: Sequence[str],
    sort_order: Mapping[str, int],
    *,
    passives: int = 8,
    sockets: int = 2,
    geometry: ClusterGeometry = LARGE_CLUSTER_GEOMETRY,
) -> ClusterLayout:
    """Compute exact PoB socket/notable/small orbit indices and Front/Back positions."""
    ordered = sorted(notables, key=lambda n: sort_order.get(n, 999999))
    sock_set: set[int] = set()
    if geometry.size == "Large" and sockets == 1:
        sock_set.add(6)
    else:
        for idx in geometry.socket_indices[: max(0, sockets)]:
            sock_set.add(idx)

    notable_idx_list: list[int] = []
    for idx in geometry.notable_indices:
        if len(notable_idx_list) == len(ordered):
            break
        if idx not in sock_set:
            notable_idx_list.append(idx)
    notable_idx_list.sort()

    occupied = set(sock_set) | set(notable_idx_list)
    small_count = max(0, passives - len(sock_set) - len(notable_idx_list))
    small_idx_list: list[int] = []
    for idx in geometry.small_indices:
        if len(small_idx_list) == small_count:
            break
        if idx not in occupied:
            small_idx_list.append(idx)
            occupied.add(idx)

    placements: list[NotablePlacement] = []
    low_back = geometry.total_indices / 4.0
    high_back = geometry.total_indices * 3.0 / 4.0
    for name, node_idx in zip(ordered, notable_idx_list, strict=False):
        if geometry.size == "Small":
            pos = "End"
        elif low_back < node_idx < high_back:
            pos = "Back"
        else:
            pos = "Front"
        placements.append(
            NotablePlacement(name=name, index=node_idx, position=pos, sort_order=sort_order.get(name, 999999))
        )

    ring = sorted(occupied)
    all_targets = set(sock_set) | {p.index for p in placements}
    front_targets = set(sock_set) | {p.index for p in placements if p.position == "Front"}
    return ClusterLayout(
        passives=passives,
        sockets=sockets,
        notable_placements=tuple(placements),
        socket_indices=tuple(sorted(sock_set)),
        small_indices=tuple(sorted(small_idx_list)),
        entrance_index=0,
        min_points_all_notables_and_sockets=_min_ring_points(ring, all_targets),
        min_points_front_notables_and_sockets=_min_ring_points(ring, front_targets),
    )


def _affix_cap_violation(notables: Sequence[ClusterNotable]) -> str | None:
    """Enforce rare cluster affix limits (<=2 Prefix, <=2 Suffix, <=1 Suffix when blocks_second_suffix)."""
    prefixes = sum(1 for n in notables if n.affix_type == "Prefix")
    suffixes = sum(1 for n in notables if n.affix_type == "Suffix")
    if prefixes > 2:
        return "prefix_cap_exceeded"
    if suffixes > 2 or (suffixes >= 2 and any(n.blocks_second_suffix for n in notables)):
        return "suffix_notable_cap_exceeded"
    return None


def _required_back_affix(notable_a: ClusterNotable, notable_b: ClusterNotable) -> str:
    prefixes = sum(1 for n in (notable_a, notable_b) if n.affix_type == "Prefix")
    suffixes = sum(1 for n in (notable_a, notable_b) if n.affix_type == "Suffix")
    if prefixes >= 2:
        return "Suffix"
    if suffixes >= 2 or (suffixes == 1 and any(n.blocks_second_suffix for n in (notable_a, notable_b))):
        return "Prefix"
    return "Prefix or Suffix"


def _two_notable_back_warning(
    layout_2: ClusterLayout,
    *,
    sample_layout: ClusterLayout | None = None,
) -> str | None:
    back_2 = [p for p in layout_2.notable_placements if p.position == "Back"]
    if not back_2:
        return None
    b0 = back_2[0]
    if sample_layout is not None:
        return (
            f"On a 2-notable {layout_2.passives}p{layout_2.sockets}s jewel, {b0.name} lands in the Back "
            f"(index {b0.index}), requiring {layout_2.min_points_all_notables_and_sockets} points vs "
            f"{sample_layout.min_points_front_notables_and_sockets} points with a Front-2 Back-1 3rd notable."
        )
    return (
        f"On a 2-notable {layout_2.passives}p{layout_2.sockets}s jewel, {b0.name} lands in the Back "
        f"(index {b0.index}), requiring {layout_2.min_points_all_notables_and_sockets} points."
    )


def _find_alternative_enchants(
    notable_a: ClusterNotable,
    notable_b: ClusterNotable,
    current_tag: str,
    data: ClusterData,
    *,
    passives: int,
    sockets: int,
    ilvl_max: int | None,
) -> list[dict[str, Any]]:
    alts: list[dict[str, Any]] = []
    if _affix_cap_violation((notable_a, notable_b)) is not None:
        return alts
    low_ord, high_ord = sorted((notable_a.sort_order, notable_b.sort_order))
    for tag, ench in data.large_enchants.items():
        if tag == current_tag:
            continue
        if notable_a.weights.get(tag, 0) <= 0 or notable_b.weights.get(tag, 0) <= 0:
            continue
        fillers = [
            n
            for n in data.notables_by_name.values()
            if n.weights.get(tag, 0) > 0
            and low_ord < n.sort_order < high_ord
            and n.group not in (notable_a.group, notable_b.group)
            and _affix_cap_violation((notable_a, n, notable_b)) is None
            and (ilvl_max is None or n.level <= ilvl_max)
        ]
        if not fillers:
            continue
        sample_layout = compute_cluster_node_layout(
            [notable_a.name, fillers[0].name, notable_b.name],
            data.notable_sort_order,
            passives=passives,
            sockets=sockets,
            geometry=data.large_geometry,
        )
        pos_map = {p.name: p for p in sample_layout.notable_placements}
        pa, pb = pos_map.get(notable_a.name), pos_map.get(notable_b.name)
        if pa is None or pb is None or pa.position != "Front" or pb.position != "Front":
            continue
        alts.append(
            {
                "tag": tag,
                "name": ench.name,
                "validBackCount": len(fillers),
                "validBackNames": [f.name for f in sorted(fillers, key=lambda x: x.sort_order)],
            }
        )
    alts.sort(key=lambda x: (-x["validBackCount"], x["name"]))
    return alts


def _make_impossible(
    enchant: ClusterEnchant,
    n_low: ClusterNotable,
    n_high: ClusterNotable,
    *,
    passives: int,
    sockets: int,
    ilvl_max: int | None,
    reason: str,
    explanation: str,
    min_ilvl_ab: int,
    warnings: list[str],
    data_source: str,
    required_back_affix: str | None = None,
    rejected_sandwich: list[RejectedCandidate] | None = None,
    ilvl_filtered_out: list[ClusterNotable] | None = None,
    layout_3_notable: ClusterLayout | None = None,
    layout_2_notable: ClusterLayout | None = None,
    alternative_enchants: list[dict[str, Any]] | None = None,
) -> ClusterSolution:
    return ClusterSolution(
        enchant=enchant, notable_a=n_low, notable_b=n_high, passives=passives, sockets=sockets,
        ilvl_max=ilvl_max, possible=False, reason=reason, explanation=explanation,
        required_back_affix=required_back_affix,
        valid_back_notables=[],
        rejected_sandwich=rejected_sandwich or [],
        ilvl_filtered_out=ilvl_filtered_out or [],
        min_ilvl_ab=min_ilvl_ab,
        min_ilvl_overall=None,
        warnings=warnings,
        layout_3_notable=layout_3_notable,
        layout_2_notable=layout_2_notable,
        alternative_enchants=alternative_enchants or [],
        data_source=data_source,
    )


def solve_front2_back1(
    enchant_query: str,
    notable_a_query: str,
    notable_b_query: str,
    *,
    passives: int = 8,
    sockets: int = 2,
    ilvl_max: int | None = None,
    pob_root: Path | None = None,
    cluster_data: ClusterData | None = None,
) -> ClusterSolution:
    """Solve valid Back-seat filler notables ``C`` placing ``(A, B)`` on Front nodes of a Large Cluster Jewel."""
    data = cluster_data if cluster_data is not None else load_cluster_data(pob_root=pob_root)
    enchant = resolve_large_enchant(enchant_query, data, pob_root=pob_root)
    na = resolve_cluster_notable(notable_a_query, data, enchant=enchant, pob_root=pob_root)
    nb = resolve_cluster_notable(notable_b_query, data, enchant=enchant, pob_root=pob_root)

    if na.name == nb.name:
        raise ValueError(f"Both desired notables are identical ({na.name!r}); provide two distinct notables.")
    for n in (na, nb):
        if n.weights.get(enchant.tag, 0) <= 0:
            pool_names = sorted(x.name for x in data.notables_by_name.values() if x.weights.get(enchant.tag, 0) > 0)
            raise ValueError(
                f"Notable {n.name!r} cannot roll on Large Cluster enchant {enchant.name!r}. "
                f"Available notables: {', '.join(pool_names)}"
            )

    n_low, n_high = (na, nb) if na.sort_order < nb.sort_order else (nb, na)
    min_ilvl_ab = max(n_low.level, n_high.level)
    warnings: list[str] = []
    base_kw: dict[str, Any] = {
        "passives": passives, "sockets": sockets, "ilvl_max": ilvl_max,
        "min_ilvl_ab": min_ilvl_ab, "warnings": warnings, "data_source": data.source,
    }
    if not (data.large_geometry.min_nodes <= passives <= data.large_geometry.max_nodes):
        return _make_impossible(
            enchant, n_low, n_high, reason="invalid_passive_count",
            explanation=(
                f"Large Cluster Jewels only support {data.large_geometry.min_nodes}-{data.large_geometry.max_nodes} "
                f"added passive skills (requested {passives})."
            ),
            **base_kw,
        )
    if passives != 8 or sockets != 2:
        warnings.append(
            f"Non-standard geometry ({passives}p/{sockets}s): Front-2 Back-1 saves the most points on 8-passive 2-socket Large Clusters."
        )
    if n_low.group == n_high.group:
        return _make_impossible(
            enchant, n_low, n_high, reason="same_mod_group",
            explanation=f"{n_low.name} and {n_high.name} share ModGroup '{n_low.group}' and cannot coexist on one jewel.",
            **base_kw,
        )
    if _affix_cap_violation((n_low, n_high)) == "suffix_notable_cap_exceeded":
        return _make_impossible(
            enchant, n_low, n_high, reason="two_suffix_notables_impossible",
            explanation=(
                f"{n_low.name} and {n_high.name} are both Suffix notables; PoE1's 'has_affliction_notable2' "
                "rule caps every Cluster Jewel at 1 Suffix notable (so 2 Suffix notables can never coexist)."
            ),
            **base_kw,
        )

    layout_2 = compute_cluster_node_layout(
        [n_low.name, n_high.name], data.notable_sort_order, passives=passives, sockets=sockets, geometry=data.large_geometry
    )
    required_back_affix = _required_back_affix(n_low, n_high)
    if ilvl_max is not None and min_ilvl_ab > ilvl_max:
        return _make_impossible(
            enchant, n_low, n_high, reason="ilvl_max_too_low",
            explanation=f"{n_low.name} + {n_high.name} require at least ilvl {min_ilvl_ab}, which exceeds --ilvl-max {ilvl_max}.",
            required_back_affix=required_back_affix, layout_2_notable=layout_2, **base_kw,
        )

    pool = sorted(
        (n for n in data.notables_by_name.values() if n.weights.get(enchant.tag, 0) > 0),
        key=lambda x: x.sort_order,
    )
    sandwich = [n for n in pool if n_low.sort_order < n.sort_order < n_high.sort_order]

    valid: list[ClusterNotable] = []
    rejected: list[RejectedCandidate] = []
    ilvl_out: list[ClusterNotable] = []
    for cand in sandwich:
        if cand.group in (n_low.group, n_high.group):
            rejected.append(RejectedCandidate(cand, "same_mod_group"))
        elif (affix_err := _affix_cap_violation((n_low, cand, n_high))) is not None:
            rejected.append(RejectedCandidate(cand, affix_err))
        elif ilvl_max is not None and cand.level > ilvl_max:
            ilvl_out.append(cand)
        else:
            valid.append(cand)

    if valid:
        sample_layout = compute_cluster_node_layout(
            [n_low.name, valid[0].name, n_high.name],
            data.notable_sort_order,
            passives=passives,
            sockets=sockets,
            geometry=data.large_geometry,
        )
        pos_map = {p.name: p for p in sample_layout.notable_placements}
        pa, pb = pos_map.get(n_low.name), pos_map.get(n_high.name)
        if pa is None or pb is None or pa.position != "Front" or pb.position != "Front":
            if (w2 := _two_notable_back_warning(layout_2)) is not None:
                warnings.append(w2)
            placed_desc = ", ".join(f"{p.name} (idx {p.index}, {p.position})" for p in sample_layout.notable_placements)
            return _make_impossible(
                enchant, n_low, n_high, reason="geometry_not_front2",
                explanation=(
                    f"Geometry {passives}p/{sockets}s cannot place both desired notables in Front on a 3-notable jewel: "
                    f"computed placements are {placed_desc} (Front-2 requires 2 sockets)."
                ),
                required_back_affix=required_back_affix, rejected_sandwich=rejected, ilvl_filtered_out=ilvl_out,
                layout_3_notable=sample_layout, layout_2_notable=layout_2, **base_kw,
            )

        if (w2 := _two_notable_back_warning(layout_2, sample_layout=sample_layout)) is not None:
            warnings.append(w2)
        min_overall = min(max(min_ilvl_ab, c.level) for c in valid)
        if min_overall >= 75:
            warnings.append(f"Minimum required ilvl for this 3-notable combination is ilvl {min_overall}+ (scarcer/pricier bases).")
        high_lvl = [f"{c.name} (ilvl {c.level}+)" for c in valid if c.level >= 75]
        if high_lvl and min_overall < 75:
            warnings.append(f"High-ilvl fillers in pool: {', '.join(high_lvl)}.")
        if ilvl_out:
            warnings.append(
                f"Excluded {len(ilvl_out)} valid back-seat filler(s) above --ilvl-max {ilvl_max}: "
                + ", ".join(f"{c.name} (ilvl {c.level}+)" for c in ilvl_out)
            )
        return ClusterSolution(
            enchant=enchant, notable_a=n_low, notable_b=n_high, passives=passives, sockets=sockets,
            ilvl_max=ilvl_max, possible=True, reason=None,
            explanation=(
                f"{len(valid)} legal {required_back_affix} back-seat notable(s) place both "
                f"{n_low.name} (idx {pa.index}, {pa.position}) and {n_high.name} (idx {pb.index}, {pb.position}) "
                f"in front of the {sockets} socket(s) "
                f"({sample_layout.min_points_front_notables_and_sockets} pts vs "
                f"{layout_2.min_points_all_notables_and_sockets} pts on a 2-notable {passives}p{sockets}s jewel)."
            ),
            required_back_affix=required_back_affix,
            valid_back_notables=valid,
            rejected_sandwich=rejected,
            ilvl_filtered_out=ilvl_out,
            min_ilvl_ab=min_ilvl_ab,
            min_ilvl_overall=min_overall,
            warnings=warnings,
            layout_3_notable=sample_layout,
            layout_2_notable=layout_2,
            data_source=data.source,
        )

    if (w2 := _two_notable_back_warning(layout_2)) is not None:
        warnings.append(w2)
    if ilvl_out:
        reason = "ilvl_max_too_low"
        min_needed = min(c.level for c in ilvl_out)
        explanation = (
            f"Valid {required_back_affix} back-seat filler(s) exist ({', '.join(c.name for c in ilvl_out)}), "
            f"but all require ilvl >= {min_needed} which exceeds --ilvl-max {ilvl_max}."
        )
    elif not sandwich:
        reason = "no_notables_between_sort_orders"
        explanation = (
            f"No notables in '{enchant.name}' lie strictly between {n_low.name} (order {n_low.sort_order}) "
            f"and {n_high.name} (order {n_high.sort_order}); cannot place both in Front on a 3-notable jewel."
        )
    else:
        reason = "affix_cap_conflict"
        rej_desc = ", ".join(f"{r.notable.name} ({r.notable.affix_type})" for r in rejected[:5])
        explanation = (
            f"Notables between {n_low.name} ({n_low.affix_type}, {n_low.sort_order}) and "
            f"{n_high.name} ({n_high.affix_type}, {n_high.sort_order}) fail the 2P+1S affix cap "
            f"(need a {required_back_affix} filler; found: {rej_desc})."
        )

    alts = _find_alternative_enchants(
        n_low, n_high, enchant.tag, data, passives=passives, sockets=sockets, ilvl_max=ilvl_max
    )
    return _make_impossible(
        enchant, n_low, n_high, reason=reason, explanation=explanation,
        required_back_affix=required_back_affix, rejected_sandwich=rejected, ilvl_filtered_out=ilvl_out,
        layout_2_notable=layout_2, alternative_enchants=alts, **base_kw,
    )
