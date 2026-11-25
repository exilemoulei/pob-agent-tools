"""Single-slot, dual-slot, and game_data RPC dispatch handlers for `DaemonManager`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeError, BridgeWorker
from ..bridge_protocol import get_worker_caps
from ..errors import CrossGameHallucinationError, DaemonRequestError
from ..snapshot import history_of
from .read_cache import compute_source_hash, is_live_source
from .rpc_mutate import MUTATE_ACTION_HANDLERS, norm_keep
from .runtime import normalize_slot_name

if TYPE_CHECKING:
    from .slots import DaemonManager

_SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


def dispatch_game_data(manager: DaemonManager, params: dict[str, Any]) -> dict[str, Any]:
    from .. import game_data

    with manager._lock:
        pob_dir = manager.pob_dir
        rt_dir = manager.runtime_dir
    if params.get("rebuild"):
        game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=rt_dir, force_rebuild=True)
    queries = params.get("queries")
    limit_val = int(params["limit"]) if params.get("limit") is not None else 25
    if isinstance(queries, list) and len(queries) > 1:
        return game_data.query_multi(
            db=None,
            kind=str(params.get("kind") or ""),
            queries=[str(q) for q in queries],
            pob_dir=pob_dir,
            runtime_dir=rt_dir,
            slot=params.get("slot"),
            ilvl=int(params["ilvl"]) if params.get("ilvl") is not None else None,
            limit=limit_val,
        )
    single_q = str(queries[0]) if isinstance(queries, list) and queries else str(params.get("query") or "")
    return game_data.query_local_data(
        kind=str(params.get("kind") or ""),
        query=single_q,
        pob_dir=pob_dir,
        runtime_dir=rt_dir,
        slot=params.get("slot"),
        ilvl=int(params["ilvl"]) if params.get("ilvl") is not None else None,
        limit=limit_val,
    )


def dispatch_dual_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    mw: BridgeWorker,
    rw: BridgeWorker,
) -> dict[str, Any]:
    from ..comparator import borrow_from_ref, compare_builds

    if action == "compare":
        cmp_res = compare_builds(
            mw,
            rw,
            ref_source=params.get("ref_source"),
            ref_xml_code=params.get("ref_xml_code"),
            keep=norm_keep(params),
            auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
        )
        return manager._attach_state_meta(cmp_res, mw)

    if action == "borrow":
        if not rw.last_good_code:
            ref_slot = getattr(rw, "slot_name", None) or "ref"
            raise DaemonRequestError(
                f"No reference build loaded in '{ref_slot}' slot. "
                f"Run `pob load <source> --target {ref_slot}` or `pob compare <source>` first."
            )
        b_res = borrow_from_ref(
            mw,
            rw,
            item_slots=params.get("item_slots"),
            skill_group=params.get("skill_group"),
            apply=bool(params.get("apply", False)),
        )
        return manager._attach_state_meta(b_res, mw)

    if action == "diagnose":
        from ..analyzer import diagnose_compare_builds

        top_val = int(params["top"]) if params.get("top") is not None else 5
        main_slot_name = normalize_slot_name(
            params.get("main_target") or params.get("target") or getattr(mw, "slot_name", None)
        )
        default_ref_name = "ref" if main_slot_name == "main" else f"{main_slot_name}_ref"
        ref_slot_name = normalize_slot_name(
            params.get("ref_target") or getattr(rw, "slot_name", None) or default_ref_name
        )
        read_cache = getattr(manager, "_read_cache", None)
        main_load_params = {
            "source": params.get("source"),
            "xml_code": params.get("xml_code"),
            "char_name": params.get("char_name"),
            "league": params.get("league"),
            "realm": params.get("realm"),
            "source_hash": params.get("source_hash"),
        }
        ref_load_params = {
            "source": params.get("compare_source"),
            "xml_code": params.get("compare_xml_code"),
            "source_hash": params.get("compare_source_hash"),
        }
        main_hash = compute_source_hash(main_load_params)
        ref_hash = compute_source_hash(ref_load_params)
        skip_main = bool(
            read_cache is not None
            and main_hash
            and read_cache.matching_slot_load(
                main_slot_name, history_of(mw).rev, bool(getattr(mw, "last_good_code", None)), main_hash
            )
            is not None
        )
        skip_ref = bool(
            read_cache is not None
            and ref_hash
            and read_cache.matching_slot_load(
                ref_slot_name, history_of(rw).rev, bool(getattr(rw, "last_good_code", None)), ref_hash
            )
            is not None
        )
        diag_cmp = diagnose_compare_builds(
            mw,
            rw,
            source=None if skip_main else params.get("source"),
            xml_code=None if skip_main else params.get("xml_code"),
            compare_source=None if skip_ref else params.get("compare_source"),
            compare_xml_code=None if skip_ref else params.get("compare_xml_code"),
            char_name=params.get("char_name"),
            league=params.get("league"),
            realm=None if skip_main else params.get("realm"),
            keep=norm_keep(params),
            auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
            top=top_val,
            no_rank=bool(params.get("no_rank", params.get("noRank", True))),
            ask=params.get("ask"),
            pob_dir=manager.pob_dir,
            runtime_dir=manager.runtime_dir,
            target=main_slot_name,
            ref_target=ref_slot_name,
        )
        if read_cache is not None:
            if main_hash and not skip_main:
                read_cache.record_slot_load(
                    main_slot_name, history_of(mw).rev, main_hash, live=is_live_source(main_load_params)
                )
            if ref_hash and not skip_ref:
                read_cache.record_slot_load(
                    ref_slot_name, history_of(rw).rev, ref_hash, live=is_live_source(ref_load_params)
                )
        return manager._attach_warning_if_degraded(diag_cmp)

    raise ValueError(f"Unknown dual-slot daemon action '{action}'.")


def _h_load(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..snapshot import load_build

    try:
        load_res = load_build(
            w,
            source=params.get("source"),
            xml_code=params.get("xml_code"),
            char_name=params.get("char_name"),
            league=params.get("league"),
            realm=params.get("realm"),
        )
    except CrossGameHallucinationError:
        raise
    except Exception as exc:
        warn = m._current_update_warning()
        if warn:
            guidance = (
                f"\n[Engine Update Warning] Local PoB engine could not update due to GitHub network failure: {warn}\n"
                "Guidance for AI Agent: Tell the user to configure a proxy (HTTPS_PROXY / POB_PROXY) and run "
                "`pob update --proxy http://127.0.0.1:<port>` (or `pob bootstrap --from-dir <local_dir>`). "
                "Do NOT assume the build code is corrupted, and NEVER manually parse Build.xml to compute stats (R5)."
            )
            if isinstance(exc, BridgeError):
                # Same exception, so an engine rejection keeps its errorKind.
                exc.args = (f"{exc}{guidance}",)
                raise
            if isinstance(exc, (DaemonRequestError, ValueError)):
                raise DaemonRequestError(f"{exc}{guidance}") from exc
            raise RuntimeError(f"{exc}{guidance}") from exc
        raise
    src_hash = compute_source_hash(params)
    slot_nm = normalize_slot_name(params.get("target") or getattr(w, "slot_name", None))
    read_cache = getattr(m, "_read_cache", None)
    if read_cache is not None:
        read_cache.record_slot_load(slot_nm, history_of(w).rev, src_hash, load_res, live=is_live_source(params))
    if src_hash and isinstance(load_res, dict):
        load_res["sourceHash"] = src_hash
    return m._attach_warning_if_degraded(load_res)


def _h_export(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..importers import share_code_to_xml

    with w.lock:
        code_res = w.call("export_code", {})
        code_str = code_res.get("code", "")
        xml_str = None
        if params.get("include_xml"):
            xml_res = w.call("save_xml", {})
            xml_str = xml_res.get("xml") or share_code_to_xml(code_str)
        return {"code": code_str, "xml": xml_str, "pobDir": str(m.pob_dir) if m.pob_dir else None}


def _h_undo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..snapshot import undo_build

    return undo_build(w)


def _h_redo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..snapshot import redo_build

    return redo_build(w)


def _h_summary(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import analyzer

    return m._attach_warning_if_degraded(analyzer.extract_summary(w))


def _h_audit(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import audit_build

    return audit_build(
        w,
        keep=norm_keep(params),
        attribution=bool(params.get("attribution", True)),
        auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
    )


def _h_diagnose(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import diagnose_build
    from ..snapshot import load_build

    slot_nm = normalize_slot_name(params.get("target") or getattr(w, "slot_name", None))
    src = params.get("source")
    xml_code = params.get("xml_code")
    has_load_arg = bool(src or xml_code or (params.get("realm") and str(params.get("realm")).lower() == "cn"))
    src_hash = compute_source_hash(params) if has_load_arg else None
    read_cache = getattr(m, "_read_cache", None)
    load_meta: dict[str, Any] = {}
    if has_load_arg:
        matched_meta = (
            read_cache.matching_slot_load(
                slot_nm, history_of(w).rev, bool(getattr(w, "last_good_code", None)), src_hash
            )
            if read_cache is not None
            else None
        )
        if matched_meta is not None:
            load_meta = matched_meta
        else:
            load_meta = load_build(
                w,
                source=src,
                xml_code=xml_code,
                char_name=params.get("char_name"),
                league=params.get("league"),
                realm=params.get("realm"),
            )
            if read_cache is not None:
                read_cache.record_slot_load(
                    slot_nm, history_of(w).rev, src_hash, load_meta, live=is_live_source(params)
                )
    top_val = int(params["top"]) if params.get("top") is not None else 5
    diag_res = diagnose_build(
        w,
        keep=norm_keep(params),
        auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
        top=top_val,
        no_rank=bool(params.get("no_rank", params.get("noRank", False))),
        ask=params.get("ask"),
        pob_dir=m.pob_dir,
        runtime_dir=m.runtime_dir,
        target=slot_nm,
    )
    if isinstance(diag_res, dict):
        if src_hash:
            diag_res["sourceHash"] = src_hash
        if load_meta.get("importNotes"):
            diag_res["importNotes"] = load_meta["importNotes"]
        if load_meta.get("translateWarnings"):
            diag_res["translateWarnings"] = load_meta["translateWarnings"]
        if load_meta.get("character"):
            diag_res["character"] = load_meta["character"]
    return m._attach_warning_if_degraded(diag_res)


def _h_items(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import inspect_items

    return m._attach_warning_if_degraded(
        inspect_items(
            w,
            slot_name=params.get("slot"),
            raw=bool(params.get("raw", False)),
            all_raw=bool(params.get("allRaw", params.get("all_raw", False))),
        )
    )


def _h_skills(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import inspect_skills

    return inspect_skills(w, group_selector=params.get("group"))


def _h_tree(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import inspect_tree

    limit_raw = params.get("limit")
    return inspect_tree(
        w,
        search=params.get("search"),
        queries=params.get("queries"),
        node_types=params.get("node_types") or params.get("nodeTypes") or params.get("types"),
        allocated=params.get("allocated"),
        max_cost=(
            int(params["max_cost"])
            if params.get("max_cost") is not None
            else (int(params["maxCost"]) if params.get("maxCost") is not None else None)
        ),
        show_path=bool(params.get("show_path", params.get("showPath", False))),
        masteries=bool(params.get("masteries", False)),
        socket_radius=(
            params.get("socket_radius") if params.get("socket_radius") is not None else params.get("socketRadius")
        ),
        limit=int(limit_raw) if limit_raw is not None else 100,
    )


def _h_config_list(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import inspect_config

    show_all = bool(params.get("includeDefaults", params.get("include_defaults", params.get("show_all", False))))
    return inspect_config(
        w,
        include_defaults=show_all,
        show_all=show_all,
        search=params.get("search"),
        with_options=bool(params.get("with_options", params.get("withOptions", False))),
    )


def _h_calc(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer import search_calcs_breakdown

    keywords = params.get("keywords")
    keyword = params.get("keyword")
    if keyword is None and isinstance(keywords, list) and keywords:
        keyword = str(keywords[0])
    limit_val = int(params["limit"]) if params.get("limit") is not None else 20
    max_bd_raw = params.get("max_breakdowns", params.get("maxBreakdowns"))
    max_bd_val = int(max_bd_raw) if max_bd_raw is not None else 30
    return search_calcs_breakdown(
        w,
        keyword=str(keyword or ""),
        keywords=keywords,
        limit=limit_val,
        max_breakdowns=max_bd_val,
        no_breakdown=bool(params.get("no_breakdown", params.get("noBreakdown", False))),
    )


def _h_matrix(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import run_matrix

    return run_matrix(
        w,
        scenarios=params.get("scenarios"),
        full=bool(params.get("full", False)),
        keep=norm_keep(params),
        auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
    )


def _h_rank_nodes(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import rank_nodes

    return rank_nodes(
        w,
        stat=str(params.get("stat") or "CombinedDPS"),
        max_dist=int(params["max_dist"]) if params.get("max_dist") is not None else 5,
        top=int(params["top"]) if params.get("top") is not None else 15,
    )


def _h_rank_gems(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import rank_gems

    return rank_gems(
        w,
        group_selector=str(params.get("group") or "main"),
        slot_idx=params.get("slot_idx"),
        add_link=bool(params.get("add_link", False)),
        field=str(params.get("field") or "CombinedDPS"),
        top=int(params["top"]) if params.get("top") is not None else 15,
        include_legacy=bool(params.get("include_legacy", False)),
    )


def _h_timeless_list(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..timeless import describe_worker

    return describe_worker(w, pob_dir=m.pob_dir, runtime_dir=m.runtime_dir)


def _h_trade_eval_items(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import trade_eval

    raw_pk = params.get("prefilter_k") if params.get("prefilter_k") is not None else params.get("prefilterK")
    raw_pl = params.get("pob_limit") if params.get("pob_limit") is not None else params.get("pobLimit")
    return trade_eval.eval_trade_items(
        w,
        slot_name=params.get("slot") or params.get("slotName"),
        items=list(params.get("items") or []),
        prefilter_k=int(raw_pk) if raw_pk is not None else None,
        pob_limit=int(raw_pl) if raw_pl is not None else None,
    )


def _h_trade_generate_query(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    if not get_worker_caps(w).get("tradeAutoQuery"):
        return {"error": "tradeAutoQuery capability unavailable on this engine worker"}
    rpc_params: dict[str, Any] = {
        "slotName": params.get("slotName") or params.get("slot_name") or params.get("slot") or "",
    }
    for key in ("stat", "includeEhp", "ehpWeight", "includeCorrupted", "league", "reuseModData", "rarity", "maxFilters"):
        if params.get(key) is not None:
            rpc_params[key] = params[key]
    return w.call("trade_generate_query", rpc_params, timeout=60.0)


def _h_compute_grad_build(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    if not get_worker_caps(w).get("agentProbes"):
        return {"weights": {}, "error": "agentProbes capability unavailable on this engine worker"}
    rpc_params: dict[str, Any] = {}
    for key in ("stat", "ehpWeight", "includeResists"):
        if params.get(key) is not None:
            rpc_params[key] = params[key]
    return w.call("compute_grad_build", rpc_params, timeout=30.0)


_SLOT_ACTION_HANDLERS: dict[str, _SlotHandler] = {
    "load": _h_load,
    "export": _h_export,
    "undo": _h_undo,
    "redo": _h_redo,
    "summary": _h_summary,
    "audit": _h_audit,
    "diagnose": _h_diagnose,
    "items": _h_items,
    "skills": _h_skills,
    "tree": _h_tree,
    "config_list": _h_config_list,
    "calc": _h_calc,
    "matrix": _h_matrix,
    "rank_nodes": _h_rank_nodes,
    "rank_gems": _h_rank_gems,
    "timeless_list": _h_timeless_list,
    "trade_eval_items": _h_trade_eval_items,
    "trade_generate_query": _h_trade_generate_query,
    "compute_grad_build": _h_compute_grad_build,
    **MUTATE_ACTION_HANDLERS,
}


def dispatch_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    w: BridgeWorker,
) -> dict[str, Any]:
    """Execute a single-slot RPC action under its per-slot lock and reader barrier."""
    handler = _SLOT_ACTION_HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"Unknown daemon action '{action}'.")
    return handler(manager, params, w)
