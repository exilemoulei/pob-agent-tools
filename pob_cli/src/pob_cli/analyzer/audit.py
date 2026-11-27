"""Per-item PoB water attribution and full static + de-watered Pinnacle build audit."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none
from ..snapshot import (
    BuildTransaction,
    _call_batch,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    capture_digest,
    diff_digest,
)
from .dewater_attribution import attribute_water as attribute_water
from .dewater_auto import (
    best_effort_audit_tree_and_raw,
    infer_auto_keep_and_flask_notes,
)
from .eb_sustain import collect_dewatered_eb_flask_note, collect_mana_sustain_issues
from .pitfalls_core import _detect_build_mechanics_and_pitfalls
from .pitfalls_triggers import confirm_trigger_estimates
from .static_checks import _has_hollow_palm_keystone, _resist_cap_and_gap
from .summary import _has_doryanis_prototype_equipped
from .threshold_probes import compute_threshold_probes


def _collect_static_health_issues(
    stats: dict[str, Any],
    info: dict[str, Any],
    items_res: dict[str, Any],
    skills_res_audit: dict[str, Any] | None,
    warnings: list[str],
    tree_summary: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    skill_flags: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    health_issues: list[dict[str, str]] = []
    has_doryani = _has_doryanis_prototype_equipped(items_res)

    for elem in ("Fire", "Cold", "Lightning"):
        if elem == "Lightning" and has_doryani:
            continue
        val = _finite_float(stats.get(f"{elem}Resist"))
        over = _finite_float(stats.get(f"{elem}ResistOverCap"))
        cap, gap = _resist_cap_and_gap(stats, elem, val)
        if gap > 0:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "HIGH",
                    "message": f"{elem} Resist is uncapped: {val:.0f}% of {cap:.0f}% max (gap: {gap:.0f}%)",
                }
            )
        elif over < 15:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "LOW",
                    "message": f"{elem} Resist overcap is thin: +{over:.0f}% over cap (vulnerable to curses/exposure)",
                }
            )

    chaos_res = _finite_float(stats.get("ChaosResist"))
    life_val = _finite_float(stats.get("Life"))
    chaos_mh_raw = _parse_numeric_or_none(stats.get("ChaosMaximumHitTaken"))
    is_chaos_immune = (life_val <= 1.0 and chaos_mh_raw is not None and math.isinf(chaos_mh_raw)) or chaos_res >= 100
    if chaos_res < 0 and not is_chaos_immune:
        health_issues.append(
            {
                "category": "Resistance",
                "severity": "MEDIUM",
                "message": f"Chaos Resist is negative: {chaos_res:.0f}%",
            }
        )

    for attr in ("Str", "Dex", "Int"):
        cur = _finite_float(stats.get(attr))
        req = _finite_float(stats.get(f"Req{attr}"))
        if req > cur:
            health_issues.append(
                {
                    "category": "Attribute",
                    "severity": "HIGH",
                    "message": f"{attr} insufficient: have {cur:.0f}, require {req:.0f}",
                }
            )

    hit_chance = stats.get("HitChance")
    if hit_chance is not None:
        hc_val = _finite_float(hit_chance, default=100.0)
        if hc_val < 100.0:
            health_issues.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": f"Hit Chance is {hc_val:.1f}% (< 100%)",
                }
            )

    health_issues.extend(
        collect_mana_sustain_issues(
            stats,
            skills_res_audit,
            tree_summary=tree_summary,
            items_res=items_res,
            equipped_slots_raw=equipped_slots_raw,
            skill_flags=skill_flags,
        )
    )

    pts = info.get("points", {})
    used, used_max = int(pts.get("used") or 0), int(pts.get("usedMax") or 0)
    asc_used, asc_max = int(pts.get("ascUsed") or 0), int(pts.get("ascMax") or 0)
    if used > used_max:
        health_issues.append(
            {
                "category": "Points",
                "severity": "HIGH",
                "message": f"Over-allocated passive points: {used}/{used_max}",
            }
        )
    if asc_used < asc_max:
        health_issues.append(
            {
                "category": "Points",
                "severity": "LOW",
                "message": f"Unallocated Ascendancy points: {asc_used}/{asc_max}",
            }
        )

    for w in warnings:
        health_issues.append({"category": "EngineWarning", "severity": "HIGH", "message": w})

    primary_slots = {"Weapon 1", "Body Armour", "Helmet", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt"}
    hollow_slots = {"Weapon 1", "Gloves"}
    worn = {s.get("name") for s in items_res.get("slots", []) if s.get("selItemId") and not s.get("inactive")}
    if _has_hollow_palm_keystone(tree_summary) and not (hollow_slots & worn):
        # Hollow Palm Technique wants both slots empty; with one filled it is off and the other is a real gap.
        primary_slots -= hollow_slots
    for slot in items_res.get("slots", []):
        if slot.get("name") in primary_slots and not slot.get("inactive") and not slot.get("selItemId"):
            health_issues.append(
                {
                    "category": "Equipment",
                    "severity": "MEDIUM",
                    "message": f"Equipment slot '{slot['name']}' is empty",
                }
            )

    if isinstance(tree_summary, dict):
        sock_by_id = {
            int(s["nodeId"]): s
            for s in (tree_summary.get("jewelSockets") or [])
            if isinstance(s, dict) and s.get("nodeId") is not None
        }
        empty_ids = list((tree_summary.get("socketSummary") or {}).get("allocatedEmptyIds") or [])
        if not empty_ids:
            empty_ids = [
                nid
                for nid, s in sock_by_id.items()
                if s.get("allocated")
                and not s.get("occupied")
                and not s.get("itemId")
                and not s.get("name")
                and s.get("kind") != "charm"
            ]
        for nid in sorted({int(x) for x in empty_ids}):
            s = sock_by_id.get(nid) or {}
            idx_part = f" (#{s['socketIndex']})" if s.get("socketIndex") else ""
            kind_part = str(s.get("kind") or "basic")
            if s.get("sector"):
                kind_part = f"{kind_part}, {s['sector']}"
            health_issues.append(
                {
                    "category": "PassiveTree",
                    "severity": "MEDIUM",
                    "message": (
                        f"[EMPTY-ALLOCATED-SOCKET] Jewel socket nodeId={nid}{idx_part} ({kind_part}) "
                        "is allocated on the passive tree with no jewel socketed."
                    ),
                }
            )
    return health_issues


def _build_audit_panel(digest: dict[str, Any]) -> dict[str, Any]:
    st = digest["stats"]
    ele = _safe_min_ele_max_hit(st)
    return {
        "primaryDpsKey": digest["primaryDpsKey"],
        "primaryDps": digest["primaryDps"],
        "fullDps": _json_safe_stat(st.get("FullDPS")) if st.get("FullDPS") is not None else None,
        "combinedDps": _json_safe_stat(st.get("CombinedDPS")) if st.get("CombinedDPS") is not None else None,
        "totalDps": _json_safe_stat(st.get("TotalDPS")) if st.get("TotalDPS") is not None else None,
        "minionCombinedDps": _json_safe_stat(st.get("MinionCombinedDPS")) if st.get("MinionCombinedDPS") is not None else None,
        "totalEhp": digest["totalEhp"],
        "life": digest["life"],
        "physMaxHit": _json_safe_stat(st.get("PhysicalMaximumHitTaken")) if st.get("PhysicalMaximumHitTaken") is not None else None,
        "eleMaxHit": ele if ele is not None else 0.0,
        "chaosMaxHit": _json_safe_stat(st.get("ChaosMaximumHitTaken")) if st.get("ChaosMaximumHitTaken") is not None else None,
    }


def audit_build(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    attribution: bool = True,
    *,
    auto_keep: bool = True,
    include_envelope: bool = True,
    pre_tree_summary: dict[str, Any] | None = None,
    pre_equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit."""
    import pob_cli.analyzer as _analyzer

    with BuildTransaction(worker) as tx:
        items_res = worker.call("list_items", {})
        digest_before = capture_digest(worker)
        stats = digest_before["stats"]
        info = digest_before["buildInfo"]
        try:
            skills_res_audit, cfg_res_audit = _call_batch(
                worker,
                [("list_skills", {}), ("list_config", {})],
            )
        except Exception:
            skills_res_audit, cfg_res_audit = None, None

        eff_tree, eff_raw, alloc_st = best_effort_audit_tree_and_raw(
            worker,
            items_res,
            pre_tree_summary=pre_tree_summary,
            pre_equipped_slots_raw=pre_equipped_slots_raw,
        )
        health_issues = _collect_static_health_issues(
            stats,
            info,
            items_res,
            skills_res_audit,
            digest_before["warnings"],
            tree_summary=eff_tree,
            equipped_slots_raw=eff_raw,
            skill_flags=digest_before.get("skillFlags"),
        )

        ak_reasons, flask_notes = (
            infer_auto_keep_and_flask_notes(
                stats=stats,
                skills_res=skills_res_audit,
                items_res=items_res,
                build_info=info,
                tree_summary=eff_tree,
                equipped_slots_raw=eff_raw,
                alloc_node_stats=alloc_st,
            )
            if auto_keep
            else ({}, {})
        )
        dw_defs = getattr(_analyzer.dewater_build_with_records, "__kwdefaults__", None) or {}
        dw_kw: dict[str, Any] = {}
        if "auto_keep_reasons" in dw_defs:
            dw_kw["auto_keep_reasons"] = ak_reasons
        if "flask_notes" in dw_defs:
            dw_kw["flask_notes"] = flask_notes
        if "equipped_slots_raw" in dw_defs:
            dw_kw["equipped_slots_raw"] = eff_raw
        stripped, records = _analyzer.dewater_build_with_records(worker, keep=keep, **dw_kw)
        digest_dewatered = capture_digest(worker)
        eb_dew_note = collect_dewatered_eb_flask_note(
            digest_dewatered["stats"],
            tree_summary=eff_tree,
            items_res=items_res,
            equipped_slots_raw=eff_raw,
            stripped_flasks=list((stripped or {}).get("flasks") or []),
            existing_issues=health_issues,
        )
        if eb_dew_note is not None:
            health_issues.append(eb_dew_note)
        dewater_diff = diff_digest(digest_before, digest_dewatered)
        water_attribution = (
            _analyzer.attribute_water(worker, records, digest_dewatered, digest_before["primaryDpsKey"])
            if attribution
            else []
        )
        restore_check = tx.rollback_verified(digest_before)

        mech_gaps, mech_audit = _detect_build_mechanics_and_pitfalls(
            stats=stats,
            skills_res=skills_res_audit,
            items_res=items_res,
            cfg_res=cfg_res_audit,
            warnings=digest_before["warnings"],
            build_info=info,
            stripped_flasks=list((stripped or {}).get("flasks") or []),
        )
        health_issues.extend(mech_gaps)
        audit_core_synergies = list(mech_audit.get("coreSynergies") or [])
        agent_probe_res: dict[str, Any] | None = None
        scenario_envelope_res: dict[str, Any] | None = None
        if (getattr(worker, "caps", None) or {}).get("agentProbes"):
            try:
                p_res = worker.call("agent_probe_build", {})
                if isinstance(p_res, dict) and not p_res.get("_error"):
                    agent_probe_res = p_res
            except Exception:
                pass
            confirm_trigger_estimates(health_issues, agent_probe_res)
            if include_envelope:
                try:
                    env_res = worker.call("eval_scenario_envelope", {})
                    if isinstance(env_res, dict) and not env_res.get("_error"):
                        scenario_envelope_res = env_res
                except Exception:
                    pass

        out_audit: dict[str, Any] = {
            "healthIssues": health_issues,
            "core_synergies": audit_core_synergies,
            "thresholdProbes": compute_threshold_probes(
                stats, skills_res=skills_res_audit, items_res=items_res, agent_probe=agent_probe_res
            ),
            "inflationStripped": stripped,
            "waterAttribution": water_attribution,
            "restoreCheck": restore_check,
            "original": _build_audit_panel(digest_before),
            "dewateredPinnacle": _build_audit_panel(digest_dewatered),
            "dewaterDiff": dewater_diff,
        }
        if agent_probe_res is not None:
            out_audit["agentProbe"] = agent_probe_res
        if scenario_envelope_res is not None:
            out_audit["scenarioEnvelope"] = scenario_envelope_res
        return out_audit
