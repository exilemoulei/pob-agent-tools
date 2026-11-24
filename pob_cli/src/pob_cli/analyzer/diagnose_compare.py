"""Dual-build A/B diagnosis (`diagnose_compare_builds`) and diagnosis enrichment helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from .constants import _PANTHEON_HINTS


def _merge_traps_and_ask_grounding(
    *,
    scan_corpus: str,
    ask: str | None,
    fact_cards: dict[str, Any],
    pob_dir: Any,
    runtime_dir: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    from ..game_data import ground_text_entities
    from ..guardrails import scan_text_traps

    trap_scan = scan_text_traps(scan_corpus)
    advisories_merged: list[dict[str, Any]] = []
    seen_adv_rules: set[str] = set()
    for w in trap_scan.get("warnings") or []:
        rid = str(w.get("ruleId") or w.get("trigger") or "")
        if rid and rid in seen_adv_rules:
            continue
        if rid:
            seen_adv_rules.add(rid)
        advisories_merged.append(w)

    ask_grounding: dict[str, Any] | None = None
    ask_clean = str(ask or "").strip()
    if ask_clean:
        try:
            g_ask = ground_text_entities(ask_clean, limit=8, pob_dir=pob_dir, runtime_dir=runtime_dir)
            for w in g_ask.get("warnings") or []:
                rid = str(w.get("ruleId") or w.get("trigger") or "")
                if rid and rid in seen_adv_rules:
                    continue
                if rid:
                    seen_adv_rules.add(rid)
                advisories_merged.append(w)
            gem_cards = list(fact_cards.get("gems") or [])
            uniq_cards = list(fact_cards.get("uniques") or [])
            seen_gem_names = {str(c.get("name") or "").lower() for c in gem_cards}
            seen_uniq_names = {str(c.get("name") or "").lower() for c in uniq_cards}
            added_entities: list[str] = []
            for ent in g_ask.get("entities") or []:
                enm = str(ent.get("name") or "")
                elow = enm.lower()
                if ent.get("kind") == "gem" and elow and elow not in seen_gem_names:
                    seen_gem_names.add(elow)
                    gem_cards.append(ent)
                    added_entities.append(enm)
                elif ent.get("kind") == "unique" and elow and elow not in seen_uniq_names:
                    seen_uniq_names.add(elow)
                    uniq_cards.append(ent)
                    added_entities.append(enm)
            fact_cards["gems"] = gem_cards
            fact_cards["uniques"] = uniq_cards
            ask_grounding = {
                "query": ask_clean,
                "resolvedSlang": g_ask.get("resolvedSlang") or [],
                "hasAlerts": bool(g_ask.get("hasAlerts")),
                "addedEntities": added_entities,
                "elapsedMs": g_ask.get("elapsedMs"),
            }
        except Exception:
            ask_grounding = {"query": ask_clean, "resolvedSlang": [], "hasAlerts": False, "addedEntities": []}
    return advisories_merged, ask_grounding


def _extract_pantheon_info(cfg_res_diag: dict[str, Any]) -> dict[str, str]:
    pantheon_info: dict[str, str] = {}
    cfg_var_entries: list[dict[str, Any]] = []
    if isinstance(cfg_res_diag, dict):
        for sec in cfg_res_diag.get("sections") or []:
            if isinstance(sec, dict):
                cfg_var_entries.extend(v for v in (sec.get("items") or sec.get("vars") or []) if isinstance(v, dict))
        cfg_var_entries.extend(v for v in (cfg_res_diag.get("config") or []) if isinstance(v, dict))
    for ci in cfg_var_entries:
        vr = str(ci.get("var") or "")
        vl = str(ci.get("value") or "").strip()
        if vr == "pantheonMajorGod" and vl and vl.lower() != "none":
            pantheon_info["major"] = vl
            if vl in _PANTHEON_HINTS:
                pantheon_info["majorHint"] = _PANTHEON_HINTS[vl]
        elif vr == "pantheonMinorGod" and vl and vl.lower() != "none":
            pantheon_info["minor"] = vl
            if vl in _PANTHEON_HINTS:
                pantheon_info["minorHint"] = _PANTHEON_HINTS[vl]
    return pantheon_info


def _run_diagnose_rankings(
    worker: BridgeWorker,
    audit: dict[str, Any],
    mg: dict[str, Any],
    *,
    top: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    from ..comparator import rank_gems, rank_nodes

    top_nodes: dict[str, Any] | None = None
    top_gems: dict[str, Any] | None = None
    rank_errors: list[str] = []
    primary_key = str((audit.get("original") or {}).get("primaryDpsKey") or "CombinedDPS")
    node_stat = "CombinedDPS" if primary_key in ("CombinedDPS", "TotalDPS", "TotalDotDPS", "FullDPS") else primary_key
    gem_field = "FullDPS" if primary_key == "FullDPS" else "CombinedDPS"
    try:
        top_nodes = rank_nodes(worker, stat=node_stat, max_dist=5, top=top)
        if isinstance(top_nodes, dict) and top_nodes.get("error"):
            rank_errors.append(f"rank_nodes: {top_nodes['error']}")
            top_nodes = None
    except Exception as exc:
        rank_errors.append(f"rank_nodes: {exc}")
        top_nodes = None
    try:
        if mg and (mg.get("gems") or []):
            top_gems = rank_gems(
                worker,
                group_selector="main",
                slot_idx=None,
                add_link=True,
                field=gem_field,
                top=top,
                exclude_archetype_swaps=True,
            )
    except Exception as exc:
        rank_errors.append(f"rank_gems: {exc}")
        top_gems = None
    return top_nodes, top_gems, rank_errors


def _build_diagnose_env(
    worker: BridgeWorker,
    info: dict[str, Any],
    fact_cards: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    hello = getattr(worker, "hello_info", None) or {}
    pob_ver = fact_cards.get("pobVersion")
    if not pob_ver or pob_ver == "unknown":
        pob_ver = hello.get("pobVersion") or "unknown"
    tree_ver = info.get("treeVersion") or "3_30"
    patch_str = str(tree_ver).replace("_", ".") if str(tree_ver).startswith("3_") else "3.30"
    worker_rev = getattr(worker, "rev", None)
    env: dict[str, Any] = {
        "game": "poe1",
        "patch": patch_str,
        "pobVersion": pob_ver,
        "dataVersion": fact_cards.get("dataVersion", "unknown"),
        "treeVersion": tree_ver,
        "slot": target,
    }
    if isinstance(worker_rev, int) and not isinstance(worker_rev, bool):
        env["rev"] = worker_rev
    return env


def diagnose_compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    compare_source: str | None = None,
    compare_xml_code: str | None = None,
    keep: Iterable[str] | str | None = None,
    top: int = 5,
    no_rank: bool = True,
    ask: str | None = None,
    pob_dir: Any = None,
    runtime_dir: Any = None,
    target: str = "main",
    ref_target: str = "ref",
) -> dict[str, Any]:
    """One-shot dual-build 3-baseline A/B comparison + concurrent Pinnacle diagnosis of both builds."""
    import pob_cli.analyzer as _analyzer

    from ..comparator import _run_worker_pair, compare_builds
    from ..importers import load_build_into_worker

    def _load_into(worker: Any, src: str | None, code: str | None) -> None:
        if src or code:
            load_build_into_worker(worker, source=src, xml_code=code)
            if hasattr(worker, "bump_rev"):
                worker.bump_rev()
                if hasattr(worker, "save_slot_state"):
                    worker.save_slot_state()

    _run_worker_pair(
        main_worker,
        ref_worker,
        lambda: _load_into(main_worker, source, xml_code),
        lambda: _load_into(ref_worker, compare_source, compare_xml_code),
    )
    comparison = compare_builds(main_worker, ref_worker)

    def _diag_a() -> dict[str, Any]:
        return _analyzer.diagnose_build(
            main_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=ask,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=target,
        )

    def _diag_b() -> dict[str, Any]:
        return _analyzer.diagnose_build(
            ref_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=None,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=ref_target,
        )

    diag_a, diag_b = _run_worker_pair(main_worker, ref_worker, _diag_a, _diag_b)
    return {
        "mode": "compare",
        "target": target,
        "refTarget": ref_target,
        "env": diag_a.get("env"),
        "comparison": comparison,
        "buildA": diag_a,
        "buildB": diag_b,
        "askGrounding": diag_a.get("askGrounding"),
    }
