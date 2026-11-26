"""Strictly isolated `[Proxy Benchmark]` socket & outer cluster ROI templates (`R5` Iron Law)."""

from __future__ import annotations

from typing import Any

from .wheel_tags import BuildProfile

_CLUSTER_POINTS_ASSUMED = 5  # 8-passive Large Cluster: 2 smalls + 2 notables + 1 small (=5 pts inside cluster)


def build_proxy_jewel_template(profile: BuildProfile) -> tuple[str, list[str]]:
    """Build an explicit 3-mod Rare Jewel proxy template matching the build's defense + offense tags."""
    mods: list[str] = []
    labels: list[str] = []

    if profile.is_es_primary or profile.is_ci:
        mods.append("8% increased maximum Energy Shield")
        labels.append("8% ES")
    else:
        mods.append("7% increased maximum Life")
        labels.append("7% Life")

    if profile.is_minion:
        mods.append("Minions deal 16% increased Damage")
        mods.append("Minions have 6% increased Attack and Cast Speed")
        labels.extend(["16% Minion Dmg", "6% Minion Speed"])
    elif profile.is_dot and not profile.is_crit:
        primary_dt = next((d for d in ("fire", "cold", "chaos", "physical") if d in profile.damage_types), None)
        if primary_dt == "fire":
            mods.append("+6% to Fire Damage over Time Multiplier")
            mods.append("12% increased Fire Damage")
            labels.extend(["+6% Fire DoT Multi", "12% Fire Dmg"])
        elif primary_dt == "cold":
            mods.append("+6% to Cold Damage over Time Multiplier")
            mods.append("12% increased Cold Damage")
            labels.extend(["+6% Cold DoT Multi", "12% Cold Dmg"])
        elif primary_dt == "chaos":
            mods.append("+6% to Chaos Damage over Time Multiplier")
            mods.append("12% increased Chaos Damage")
            labels.extend(["+6% Chaos DoT Multi", "12% Chaos Dmg"])
        else:
            mods.append("+6% to Physical Damage over Time Multiplier")
            mods.append("12% increased Damage over Time")
            labels.extend(["+6% Phys DoT Multi", "12% DoT Dmg"])
    elif profile.is_attack and not profile.is_spell:
        if profile.is_crit:
            mods.append("+15% to Global Critical Strike Multiplier")
            mods.append("6% increased Attack Speed")
            labels.extend(["+15% Crit Multi", "6% IAS"])
        else:
            mods.append("14% increased Attack Damage")
            mods.append("6% increased Attack Speed")
            labels.extend(["14% Attack Dmg", "6% IAS"])
    else:
        if profile.is_crit:
            mods.append("+15% to Global Critical Strike Multiplier")
            mods.append("12% increased Spell Damage")
            labels.extend(["+15% Crit Multi", "12% Spell Dmg"])
        else:
            mods.append("12% increased Spell Damage")
            mods.append("4% increased Cast Speed")
            labels.extend(["12% Spell Dmg", "4% Cast Speed"])

    summary = "3-mod Rare Jewel (" + " + ".join(labels) + ")"
    return summary, mods


def build_proxy_large_cluster_template(profile: BuildProfile) -> tuple[str, list[str], int]:
    """Build an explicit 8-passive Large Cluster Jewel proxy template (2 notables + 3 smalls = 5 cluster pts)."""
    if profile.is_minion:
        return (
            "8p Large Cluster [Minion] (3x10% Minion Dmg + 2 notables = +5 cluster pts)",
            ["30% increased Minion Damage", "Minions deal 25% increased Damage", "Minions have 10% increased Attack and Cast Speed"],
            _CLUSTER_POINTS_ASSUMED,
        )
    if "lightning" in profile.damage_types:
        return (
            "8p Large Cluster [Lightning] (3x12% Light + 2 notables = +5 cluster pts)",
            ["36% increased Lightning Damage", "25% increased Lightning Damage", "10% increased Cast Speed", "Damage Penetrates 5% Lightning Resistance"],
            _CLUSTER_POINTS_ASSUMED,
        )
    if "cold" in profile.damage_types:
        return (
            "8p Large Cluster [Cold] (3x12% Cold + 2 notables = +5 cluster pts)",
            ["36% increased Cold Damage", "25% increased Cold Damage", "10% increased Cast Speed", "Damage Penetrates 5% Cold Resistance"],
            _CLUSTER_POINTS_ASSUMED,
        )
    if "fire" in profile.damage_types:
        return (
            "8p Large Cluster [Fire] (3x12% Fire + 2 notables = +5 cluster pts)",
            ["36% increased Fire Damage", "25% increased Fire Damage", "10% increased Cast Speed", "Damage Penetrates 5% Fire Resistance"],
            _CLUSTER_POINTS_ASSUMED,
        )
    if "chaos" in profile.damage_types:
        return (
            "8p Large Cluster [Chaos] (3x12% Chaos + 2 notables = +5 cluster pts)",
            ["36% increased Chaos Damage", "25% increased Chaos Damage", "+10% to Chaos Damage over Time Multiplier"],
            _CLUSTER_POINTS_ASSUMED,
        )
    if profile.is_attack and not profile.is_spell:
        return (
            "8p Large Cluster [Attack] (3x12% Attack + 2 notables = +5 cluster pts)",
            ["36% increased Attack Damage", "25% increased Attack Damage", "10% increased Attack Speed"],
            _CLUSTER_POINTS_ASSUMED,
        )
    return (
        "8p Large Cluster [Spell] (3x12% Spell + 2 notables = +5 cluster pts)",
        ["36% increased Spell Damage", "25% increased Spell Damage", "10% increased Cast Speed"],
        _CLUSTER_POINTS_ASSUMED,
    )


def prepare_proxy_socket_candidates(
    nearby_sockets: list[dict[str, Any]],
    profile: BuildProfile,
    *,
    max_basic_dist: int = 3,
    max_outer_dist: int = 4,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build `(eval_candidates, meta_list)` for reachable unallocated sockets (`[Proxy Benchmark]`)."""
    jewel_summary, jewel_mods = build_proxy_jewel_template(profile)
    cluster_summary, cluster_mods, cluster_pts = build_proxy_large_cluster_template(profile)

    eval_cands: list[dict[str, Any]] = []
    meta_list: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for s in nearby_sockets or []:
        if not isinstance(s, dict) or s.get("allocated"):
            continue
        nid = int(s.get("nodeId") or 0)
        if nid <= 0 or nid in seen_ids:
            continue
        kind = str(s.get("kind") or "basic")
        cost = int(s.get("cost") or 0)
        if cost <= 0:
            continue
        if kind == "large_cluster_outer":
            if cost > max_outer_dist:
                continue
            tpl_summary = cluster_summary
            extra_mods = cluster_mods
            assumed_pts = cost + cluster_pts
            pts_breakdown = f"{cost} travel + {cluster_pts} cluster = {assumed_pts} pts"
        else:
            if cost > max_basic_dist:
                continue
            tpl_summary = jewel_summary
            extra_mods = jewel_mods
            assumed_pts = cost
            pts_breakdown = f"{cost} travel/socket pts"

        seen_ids.add(nid)
        path_ids = [int(x) for x in (s.get("pathIds") or [nid])]
        if nid not in path_ids:
            path_ids.append(nid)
        key = f"proxy_sock:{nid}"
        eval_cands.append(
            {
                "key": key,
                "mode": "add",
                "nodeIds": path_ids,
                "extraModLines": extra_mods,
            }
        )
        meta_list.append(
            {
                "key": key,
                "nodeId": nid,
                "kind": kind,
                "sector": s.get("sector"),
                "travelCost": cost,
                "assumedPoints": assumed_pts,
                "pointsBreakdown": pts_breakdown,
                "pathIds": path_ids,
                "templateSummary": tpl_summary,
                "proxyMods": extra_mods,
                "proxyBenchmark": True,
            }
        )

    return eval_cands, meta_list
