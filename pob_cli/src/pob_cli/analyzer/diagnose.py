"""One-shot comprehensive build diagnosis (`diagnose_build`)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data.fact_cards import normalize_stat_lines
from ..snapshot import _call_batch
from .dewater_auto import infer_auto_keep_and_flask_notes
from .diagnose_compare import (
    _build_diagnose_env,
    _extract_pantheon_info,
    _merge_traps_and_ask_grounding,
    _run_diagnose_rankings,
    deduplicate_diagnose_advisories,
    extract_equipped_unique_overrides,
)
from .diagnose_probes import (
    _detect_dead_main_supports,
    _extract_trigger_attack_aps,
    _gaps_from_agent_probe,
    _probe_agent_build,
    detect_volatile_vaal_opportunity,
)
from .inspectors import inspect_items
from .pitfalls_core import _detect_build_mechanics_and_pitfalls
from .pitfalls_triggers import deduplicate_trigger_gaps
from .summary import _is_tincture_item, extract_summary
from .tree_helpers import apply_node_override
from .tree_inspect import inspect_tree


def _collect_diagnose_items(
    items_info: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    unique_names: list[str] = []
    equipped_uniques_meta: list[dict[str, Any]] = []
    equipped_items_compact: list[dict[str, Any]] = []
    for s in items_info.get("equippedSlots") or []:
        it = s.get("item")
        if not isinstance(it, dict):
            continue
        uname = strip_pob_escapes(it.get("title") or it.get("name") or "")
        uname_zh = strip_pob_escapes(it.get("titleZh") or it.get("nameZh") or "") or None
        base_nm = strip_pob_escapes(it.get("baseName") or "") or None
        rarity = str(it.get("rarity") or "").upper()
        slot_nm = str(s.get("slotName") or "")
        if "Swap" not in slot_nm:
            eq_entry: dict[str, Any] = {
                "slot": slot_nm,
                "name": uname,
                "nameZh": uname_zh,
                "base": base_nm,
                "rarity": rarity or "-",
                "type": it.get("type") or "-",
            }
            if s.get("isFlask"):
                eq_entry["isFlask"] = True
                eq_entry["active"] = bool(s.get("active"))
                if _is_tincture_item(it):
                    eq_entry["isTincture"] = True
            if s.get("nodeId") is not None:
                eq_entry["nodeId"] = s.get("nodeId")
                eq_entry["socketIndex"] = s.get("socketIndex")
            equipped_items_compact.append(eq_entry)
        if rarity in ("UNIQUE", "RELIC") and uname and "Swap" not in slot_nm:
            unique_names.append(uname)
            equipped_uniques_meta.append(
                {
                    "slot": slot_nm,
                    "name": uname,
                    "nameZh": uname_zh,
                    "base": base_nm,
                }
            )
    return unique_names, equipped_uniques_meta, equipped_items_compact


def _collect_diagnose_tree_and_skills(
    worker: BridgeWorker,
    tree_state_diag: Any,
    tree_summary: dict[str, Any],
    all_skills_res: dict[str, Any],
) -> tuple[list[tuple[str, list[str]]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    alloc_node_stats: list[tuple[str, list[str]]] = []
    if isinstance(tree_state_diag, dict) and not tree_state_diag.get("_error"):
        try:
            tv_diag = tree_state_diag.get("treeVersion")
            nodes_db_diag = worker.get_cached_tree_data(tv_diag).get("nodes", {})
            ov_diag = tree_state_diag.get("overrides") or {}
            dyn_diag = {
                str(n["id"]): n
                for n in (tree_state_diag.get("dynamicNodes") or [])
                if isinstance(n, dict) and "id" in n
            }
            for raw_nid in tree_state_diag.get("allocatedNodes") or []:
                sid = str(int(raw_nid))
                raw_nd = nodes_db_diag.get(sid) or {}
                nd = dict(raw_nd)
                if sid in dyn_diag:
                    nd.update(dyn_diag[sid])
                if sid in ov_diag and isinstance(ov_diag[sid], dict):
                    apply_node_override(nd, ov_diag[sid], raw_nd)
                st_lines = normalize_stat_lines(nd.get("stats") or [])
                if st_lines:
                    lbl = strip_pob_escapes(str(nd.get("name") or sid))
                    alloc_node_stats.append((lbl, st_lines))
        except Exception:
            pass

    ascendancy_nodes_compact: list[dict[str, Any]] = []
    for an in tree_summary.get("ascendancyNodes") or []:
        if not isinstance(an, dict):
            continue
        an_type = str(an.get("type") or "Normal")
        if an_type in ("AscendClassStart", "ClassStart"):
            continue
        an_name = strip_pob_escapes(str(an.get("name") or "")).strip()
        an_name_zh = strip_pob_escapes(str(an.get("nameZh") or "")).strip() or None
        an_stats = normalize_stat_lines(an.get("stats") or [])
        an_stats_zh = normalize_stat_lines(an.get("statsZh") or [])
        if an_name == "Passive Point" and an_stats == ["Grants 1 Passive Skill Point"]:
            continue
        if not an_stats and not an_stats_zh:
            continue
        ascendancy_nodes_compact.append(
            {
                "id": an.get("id"),
                "name": an_name,
                "nameZh": an_name_zh,
                "asc": an.get("asc") or an.get("ascendancyName"),
                "type": an_type,
                "stats": an_stats,
                "statsZh": an_stats_zh or None,
            }
        )

    skill_groups_compact: list[dict[str, Any]] = []
    all_active_skill_names: list[str] = []
    for g in all_skills_res.get("groups") or []:
        if not isinstance(g, dict):
            continue
        g_gems_compact: list[dict[str, Any]] = []
        for gm in g.get("gems") or []:
            if not isinstance(gm, dict):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            gnm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip() or None
            g_en = bool(gm.get("enabled", True))
            if g_en and not gm.get("support") and gnm:
                all_active_skill_names.append(gnm)
            g_entry: dict[str, Any] = {
                "name": gnm,
                "nameZh": gnm_zh,
                "level": gm.get("level"),
                "quality": gm.get("quality"),
                "enabled": g_en,
                "support": bool(gm.get("support")),
            }
            if gm.get("fromItem"):
                g_entry["fromItem"] = True
            cnt = int(gm.get("count") or 1)
            if cnt > 1:
                g_entry["count"] = cnt
            g_gems_compact.append(g_entry)
        skill_groups_compact.append(
            {
                "index": g.get("index"),
                "isMain": bool(g.get("isMain")),
                "slot": g.get("slot") or "-",
                "enabled": bool(g.get("enabled", True)),
                "includeInFullDPS": bool(g.get("includeInFullDPS")),
                "source": bool(g.get("source")),
                "gems": g_gems_compact,
            }
        )
    return alloc_node_stats, ascendancy_nodes_compact, skill_groups_compact, all_active_skill_names


def _assemble_diagnose_mechanics(
    worker: BridgeWorker,
    *,
    summary: dict[str, Any],
    audit: dict[str, Any],
    info: dict[str, Any],
    mg: dict[str, Any],
    all_skills_res: dict[str, Any],
    items_raw_res: dict[str, Any],
    cfg_res_diag: dict[str, Any],
    tree_summary: dict[str, Any],
    items_info: dict[str, Any],
    alloc_node_stats: list[tuple[str, list[str]]],
    gaps: list[dict[str, str]],
    tree_state_diag: Any = None,
    ascendancy_nodes: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str], list[str], dict[str, Any]]:
    stats_full: dict[str, Any] = {}
    stats_full.update(summary.get("offence") or {})
    stats_full.update(summary.get("defence") or {})
    try:
        raw_stats_res = worker.call("get_stats", {})
        if isinstance(raw_stats_res, dict) and isinstance(raw_stats_res.get("stats"), dict):
            stats_full.update(raw_stats_res["stats"])
    except Exception:
        pass

    nodes_db_diag: dict[str, Any] | None = None
    if isinstance(tree_state_diag, dict) and not tree_state_diag.get("_error"):
        try:
            nodes_db_diag = worker.get_cached_tree_data(tree_state_diag.get("treeVersion")).get("nodes", {})
        except Exception:
            nodes_db_diag = None

    agent_probe = audit.get("agentProbe")
    if not isinstance(agent_probe, dict):
        agent_probe = _probe_agent_build(worker)
    trig_aps = _extract_trigger_attack_aps(
        worker, all_skills_res, items_raw_res, stats_full, agent_probe=agent_probe
    )
    stripped_flasks = list(((audit.get("inflationStripped") or {}).get("flasks")) or [])
    diag_mech_gaps, build_mechanics = _detect_build_mechanics_and_pitfalls(
        stats=stats_full,
        skills_res=all_skills_res,
        items_res=items_raw_res,
        cfg_res=cfg_res_diag,
        tree_summary=tree_summary,
        equipped_slots_raw=items_info.get("equippedSlots") or [],
        alloc_node_stats=alloc_node_stats,
        trigger_attack_aps=trig_aps,
        warnings=list(summary.get("warnings") or []),
        build_info=info,
        stripped_flasks=stripped_flasks,
        worker=worker,
        tree_state=tree_state_diag if isinstance(tree_state_diag, dict) else None,
        nodes_db=nodes_db_diag,
    )
    dead_sup_gaps, dead_sup_records = _detect_dead_main_supports(worker, mg, stats_full)
    if dead_sup_records:
        build_mechanics["deadSupports"] = dead_sup_records
    probe_gaps = _gaps_from_agent_probe(agent_probe)
    if agent_probe is not None:
        build_mechanics["agentProbe"] = agent_probe
    from .threshold_probes import compute_threshold_probes

    build_mechanics["_thresholdProbes"] = compute_threshold_probes(
        stats_full, skills_res=all_skills_res, items_res=items_raw_res, agent_probe=agent_probe
    )
    vv_hint = detect_volatile_vaal_opportunity(
        equipped_slots_raw=items_info.get("equippedSlots") or [],
        build_info=info,
        ascendancy_nodes=ascendancy_nodes,
        alloc_node_stats=alloc_node_stats,
    )
    if vv_hint is not None:
        build_mechanics["volatileVaal"] = vv_hint
    diag_mech_gaps = deduplicate_trigger_gaps(diag_mech_gaps, agent_probe)

    core_synergies: list[str] = []
    seen_syn_keys: set[str] = set()
    for syn in (*(build_mechanics.get("coreSynergies") or []), *(audit.get("core_synergies") or [])):
        s = str(syn or "").strip()
        if not s:
            continue
        m_tag = re.match(r"^(\[[^\]]+\])", s)
        key = m_tag.group(1) if m_tag else s
        if key in seen_syn_keys:
            continue
        seen_syn_keys.add(key)
        core_synergies.append(s)
    amulet_7th_link = list(build_mechanics.get("amulet7thLink") or [])

    from .pitfalls_triggers import _OVERSPEED_TRIG_RE, canonical_trigger_key

    evaluated_tkeys = {
        canonical_trigger_key(str(t.get("name") or ""))
        for t in (build_mechanics.get("triggers") or [])
        if isinstance(t, dict)
    } | {
        canonical_trigger_key(str(c.get("triggerName") or ""))
        for c in ((agent_probe or {}).get("triggerCliffs") or [])
        if isinstance(c, dict)
    }
    audit_extra_trig = []
    for ag in audit.get("healthIssues") or []:
        if isinstance(ag, dict) and ag.get("category") == "TriggerOverspeed":
            m_at = _OVERSPEED_TRIG_RE.match(str(ag.get("message") or ""))
            at_key = canonical_trigger_key(m_at.group(1)) if m_at else ""
            if not at_key or at_key not in evaluated_tkeys:
                audit_extra_trig.append(ag)

    existing_gap_msgs = {str(g.get("message") or "") for g in gaps}
    for mgap in (*diag_mech_gaps, *audit_extra_trig, *dead_sup_gaps, *probe_gaps):
        if mgap["message"] not in existing_gap_msgs:
            existing_gap_msgs.add(mgap["message"])
            gaps.append(mgap)

    return build_mechanics, core_synergies, amulet_7th_link, stats_full


def _attach_diagnose_socket_fields(
    out_diag: dict[str, Any],
    top_nodes: dict[str, Any] | None,
    tree_summary: dict[str, Any],
) -> None:
    sock_summary = (
        top_nodes.get("socketSummary")
        if isinstance(top_nodes, dict) and isinstance(top_nodes.get("socketSummary"), dict)
        else tree_summary.get("socketSummary")
    )
    nearby_socks = (
        top_nodes.get("nearbySockets")
        if isinstance(top_nodes, dict) and isinstance(top_nodes.get("nearbySockets"), list)
        else tree_summary.get("nearbySockets")
    )
    if isinstance(sock_summary, dict):
        out_diag["socketSummary"] = sock_summary
    if isinstance(nearby_socks, list):
        out_diag["nearbySockets"] = nearby_socks
    bm = out_diag.get("buildMechanics")
    if isinstance(bm, dict) and "_thresholdProbes" in bm:
        out_diag["thresholdProbes"] = bm.pop("_thresholdProbes")


def diagnose_build(
    worker: BridgeWorker,
    *,
    keep: Iterable[str] | str | None = None,
    auto_keep: bool = True,
    top: int = 5,
    no_rank: bool = False,
    ask: str | None = None,
    pob_dir: Any = None,
    runtime_dir: Any = None,
    target: str = "main",
) -> dict[str, Any]:
    """One-shot comprehensive build diagnosis for fast/weak LLM agents (`pob diagnose`)."""
    import pob_cli.analyzer as _analyzer

    from ..game_data import lookup_fact_cards_for_build

    top = max(1, int(top))
    summary = extract_summary(worker)
    items_info = inspect_items(worker, all_raw=True)
    try:
        tree_summary = inspect_tree(worker)
    except Exception:
        tree_summary = {}

    audit_defs = getattr(_analyzer.audit_build, "__kwdefaults__", None) or {}
    audit_kw: dict[str, Any] = {}
    if "auto_keep" in audit_defs:
        audit_kw["auto_keep"] = auto_keep
    if "include_envelope" in audit_defs:
        audit_kw["include_envelope"] = False
    if "pre_tree_summary" in audit_defs:
        audit_kw["pre_tree_summary"] = tree_summary
    if "pre_equipped_slots_raw" in audit_defs:
        audit_kw["pre_equipped_slots_raw"] = items_info.get("equippedSlots")
    audit = _analyzer.audit_build(worker, keep=keep, attribution=True, **audit_kw)
    with worker.lock:
        all_skills_res, cfg_res_diag, items_raw_res, tree_state_diag = _call_batch(
            worker,
            [
                ("list_skills", {}),
                ("list_config", {}),
                ("list_items", {}),
                ("get_tree_state", {}),
            ],
            raise_on_error=False,
        )
    if not isinstance(all_skills_res, dict) or all_skills_res.get("_error"):
        all_skills_res = {"groups": [summary["mainGroup"]] if summary.get("mainGroup") else []}
    if not isinstance(cfg_res_diag, dict) or cfg_res_diag.get("_error"):
        cfg_res_diag = {}
    if not isinstance(items_raw_res, dict) or items_raw_res.get("_error"):
        items_raw_res = {"items": [], "slots": []}

    info = summary.get("buildInfo") or {}
    pts = info.get("points") or {}
    used_pts, used_max = int(pts.get("used") or 0), int(pts.get("usedMax") or 0)

    gaps = [g for g in (audit.get("healthIssues") or []) if g.get("category") != "TriggerOverspeed"]
    if used_max > 0 and used_pts < used_max:
        gaps.append(
            {
                "category": "Points",
                "severity": "LOW",
                "message": f"Unallocated passive points: {used_pts}/{used_max} ({used_max - used_pts} remaining)",
            }
        )

    mg = summary.get("mainGroup") or {}
    main_gems_raw = [
        strip_pob_escapes(g.get("name") or g.get("nameSpec") or "")
        for g in (mg.get("gems") or [])
        if g.get("enabled", True) and (g.get("name") or g.get("nameSpec"))
    ]
    unique_names, equipped_uniques_meta, equipped_items_compact = _collect_diagnose_items(items_info)
    equipped_unique_overrides = extract_equipped_unique_overrides(items_info)

    fact_cards: dict[str, Any] = {"pobVersion": "unknown", "dataVersion": "unknown", "gems": [], "uniques": []}
    try:
        fc_defs = getattr(lookup_fact_cards_for_build, "__kwdefaults__", None) or {}
        fc_kw: dict[str, Any] = {"pob_dir": pob_dir, "runtime_dir": runtime_dir}
        if "equipped_uniques" in fc_defs:
            fc_kw["equipped_uniques"] = equipped_unique_overrides
        fact_cards = lookup_fact_cards_for_build(main_gems_raw, unique_names, **fc_kw)
    except Exception:
        pass

    alloc_node_stats, ascendancy_nodes_compact, skill_groups_compact, all_active_skill_names = (
        _collect_diagnose_tree_and_skills(worker, tree_state_diag, tree_summary, all_skills_res)
    )
    build_mechanics, core_synergies, amulet_7th_link, stats_full = _assemble_diagnose_mechanics(
        worker,
        summary=summary,
        audit=audit,
        info=info,
        mg=mg,
        all_skills_res=all_skills_res,
        items_raw_res=items_raw_res,
        cfg_res_diag=cfg_res_diag,
        tree_summary=tree_summary,
        items_info=items_info,
        alloc_node_stats=alloc_node_stats,
        gaps=gaps,
        tree_state_diag=tree_state_diag,
        ascendancy_nodes=ascendancy_nodes_compact,
    )

    keystone_names = [
        strip_pob_escapes(k.get("name") or "")
        for k in (tree_summary.get("keystones") or [])
        if isinstance(k, dict) and k.get("name")
    ]
    asc_class_token = strip_pob_escapes(str(info.get("ascendClassName") or ""))
    asc_node_names = [an["name"] for an in ascendancy_nodes_compact if an.get("name")]
    scan_corpus = " ; ".join(
        main_gems_raw
        + all_active_skill_names
        + unique_names
        + keystone_names
        + asc_node_names
        + ([asc_class_token] if asc_class_token else [])
    )
    advisories_merged, ask_grounding = _merge_traps_and_ask_grounding(
        scan_corpus=scan_corpus,
        ask=ask,
        fact_cards=fact_cards,
        pob_dir=pob_dir,
        runtime_dir=runtime_dir,
    )
    advisories_merged = deduplicate_diagnose_advisories(
        advisories_merged,
        core_synergies=core_synergies,
        build_mechanics=build_mechanics,
        health_gaps=gaps,
    )

    raw_attr = [r for r in (audit.get("waterAttribution") or []) if not r.get("error")]
    top_water = sorted(
        raw_attr,
        key=lambda r: (abs(float(r.get("dpsPct") or 0.0)) + abs(float(r.get("totalEhpPct") or 0.0))),
        reverse=True,
    )

    if no_rank:
        top_nodes, top_gems, rank_errors = None, None, []
    else:
        ak_diag = (
            infer_auto_keep_and_flask_notes(
                stats=stats_full, skills_res=all_skills_res, items_res=items_raw_res,
                build_info=info, tree_summary=tree_summary,
                equipped_slots_raw=items_info.get("equippedSlots"), alloc_node_stats=alloc_node_stats,
            )[0]
            if auto_keep
            else {}
        )
        top_nodes, top_gems, rank_errors = _run_diagnose_rankings(
            worker, audit, mg, top=top, keep=keep, auto_keep_reasons=ak_diag
        )

    out_diag: dict[str, Any] = {
        "env": _build_diagnose_env(worker, info, fact_cards, target),
        "buildInfo": info,
        "mainGroup": mg,
        "amulet_7th_link": amulet_7th_link,
        "keystones": keystone_names,
        "pantheon": _extract_pantheon_info(cfg_res_diag) or None,
        "ascendancyNodes": ascendancy_nodes_compact,
        "skillGroups": skill_groups_compact,
        "equippedItems": equipped_items_compact,
        "buildMechanics": build_mechanics,
        "core_synergies": core_synergies,
        "offence": summary.get("offence"),
        "minionOffence": summary.get("minionOffence"),
        "defence": summary.get("defence"),
        "original": audit.get("original"),
        "dewateredPinnacle": audit.get("dewateredPinnacle"),
        "inflationStripped": audit.get("inflationStripped"),
        "topWaterSources": top_water,
        "healthGaps": gaps,
        "factCards": {
            "gems": fact_cards.get("gems", []),
            "uniques": fact_cards.get("uniques", []),
            "equippedUniques": equipped_uniques_meta,
        },
        "advisories": advisories_merged,
        "topNodes": top_nodes,
        "topGems": top_gems,
        "rankErrors": rank_errors,
        "restoreCheck": audit.get("restoreCheck"),
    }
    if build_mechanics.get("agentProbe") is not None:
        out_diag["agentProbe"] = build_mechanics["agentProbe"]
    if ask_grounding is not None:
        out_diag["askGrounding"] = ask_grounding
    _attach_diagnose_socket_fields(out_diag, top_nodes, tree_summary)
    return out_diag
