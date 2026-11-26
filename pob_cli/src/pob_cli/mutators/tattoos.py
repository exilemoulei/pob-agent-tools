"""Validated PoE1 passive-tree Tattoo & Runegraft mutator (`pob tree tattoo`, `batch`/`sweep` op `tattoo`)."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..snapshot import run_mutation
from .common import _BatchCache
from .tattoos_validate import (
    _attach_req_deficits,
    _collect_req_deficits,
    _resolve_tattoo_query,
    _validate_tattoo_preflight,
)
from .tree import _resolve_node_token

__all__ = [
    "_CLEAR_TOKENS",
    "_attach_req_deficits",
    "_collect_req_deficits",
    "_resolve_tattoo_query",
    "apply_tattoo_in_place",
    "apply_tree_tattoo",
]

_CLEAR_TOKENS: frozenset[str] = frozenset({"clear", "reset", "remove", "none", "清除", "移除", "重置"})


def _check_tattoos_capability(worker: Any) -> None:
    caps = getattr(worker, "caps", None)
    if isinstance(caps, dict) and caps and caps.get("tattoos") is False:
        raise DaemonRequestError(
            "Current PoB engine does not support PoE1 tattoo RPCs (`caps.tattoos` is false)."
        )


def apply_tattoo_in_place(
    w: BridgeWorker,
    *,
    node: str | int,
    tattoo: str | None = None,
    reset: bool = False,
    show_legacy: bool = False,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    from ..analyzer.pitfalls_tattoos import load_tattoo_catalog
    from ..analyzer.tattoo_slots import BUCKET_MASTERY, _classify_raw_base_bucket, analyze_tattoo_slots
    from ..analyzer.tree_helpers import classify_base_target_type

    _check_tattoos_capability(w)
    tree_state = cache.get_tree() if cache is not None else w.call("get_tree_state", {})
    nodes_db = (w.get_cached_tree_data(tree_state.get("treeVersion")) or {}).get("nodes", {})
    dynamic_map = {str(n["id"]): n for n in (tree_state.get("dynamicNodes") or ()) if isinstance(n, dict) and "id" in n}

    nid, _ = _resolve_node_token(w, str(node).strip(), nodes_db, dynamic_map)
    sid = str(nid)
    raw_base = nodes_db.get(sid) if isinstance(nodes_db.get(sid), dict) else None
    dyn = dynamic_map.get(sid)
    tattoo_slots = analyze_tattoo_slots(tree_state, nodes_db if isinstance(nodes_db, dict) else {})

    is_clear = bool(reset) or (tattoo is not None and str(tattoo).strip().lower() in _CLEAR_TOKENS)
    if is_clear:
        w.call("tattoo_apply", {"id": nid, "reset": True})
        if cache is not None:
            cache.invalidate("tree", "items", "skills")
        return {
            "action": "clear",
            "nodeId": nid,
            "baseNodeName": (raw_base or {}).get("name"),
            "baseTargetType": classify_base_target_type(raw_base),
            "tattoo": None,
        }

    if tattoo is None or not str(tattoo).strip():
        raise DaemonRequestError("Specify a tattoo name/id or 'clear' (reset=True).")

    catalog = load_tattoo_catalog(w)
    live_opts: list[dict[str, Any]] | None = None
    if not catalog:
        try:
            opt_res = w.call("tattoo_options", {"id": nid, "showLegacy": bool(show_legacy)})
            if isinstance(opt_res, dict) and isinstance(opt_res.get("options"), list):
                live_opts = opt_res["options"]
        except Exception:
            live_opts = None

    rec = _resolve_tattoo_query(str(tattoo), catalog, live_options=live_opts, show_legacy=show_legacy)
    eff_legacy = bool(show_legacy or rec.get("legacy"))
    preflight_warnings = _validate_tattoo_preflight(
        nid,
        raw_base,
        dyn,
        tree_state,
        nodes_db,
        rec,
        tattoo_slots=tattoo_slots,
        catalog=catalog,
    )

    tattoo_key = str(rec.get("name") or rec.get("id"))
    raw_bucket = _classify_raw_base_bucket(raw_base) if isinstance(raw_base, dict) else None
    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    cur_ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else None
    # PoB's bridge.lua tattoo_target allows Mastery nodes only when unallocated or already tattooed.
    reallocate_mastery = bool(
        raw_bucket == BUCKET_MASTERY
        and cur_ov is not None
        and str(cur_ov.get("why") or "").lower() == "mastery"
        and not str(cur_ov.get("name") or "").lower().startswith("runegraft ")
    )

    def _checked_tree_click(phase: str) -> None:
        click_res = w.call("tree_click", {"id": nid})
        if isinstance(click_res, dict) and (click_res.get("needsMastery") or click_res.get("needsConfirm")):
            reason = "needsMastery" if click_res.get("needsMastery") else "needsConfirm"
            raise DaemonRequestError(
                f"Mastery node {nid} {phase} via tree_click returned `{reason}=true` and did not complete cleanly."
            )

    if reallocate_mastery:
        _checked_tree_click("deallocation")
    apply_params: dict[str, Any] = {"id": nid, "tattoo": tattoo_key}
    if eff_legacy:
        apply_params["showLegacy"] = True
    w.call("tattoo_apply", apply_params)
    if reallocate_mastery:
        _checked_tree_click("reallocation")

    if cache is not None:
        cache.invalidate("tree", "items", "skills")
    out: dict[str, Any] = {
        "action": "apply",
        "nodeId": nid,
        "baseNodeName": (raw_base or {}).get("name"),
        "baseTargetType": classify_base_target_type(raw_base),
        "tattoo": tattoo_key,
        "tattooZh": rec.get("zh_name"),
        "kind": rec.get("kind") or "tattoo",
        "stats": list(rec.get("stats") or ()),
        "statsZh": list(rec.get("stats_zh") or ()),
    }
    if preflight_warnings:
        out["warnings"] = preflight_warnings
    return out


def apply_tree_tattoo(
    worker: BridgeWorker,
    *,
    node: str | int,
    tattoo: str | None = None,
    reset: bool = False,
    show_legacy: bool = False,
    strict_req: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Inspect eligible tattoos for `node`, or apply/clear a tattoo with CoW verification & `[REQ-DEFICIT]` guard."""
    from ..analyzer.tattoo_slots import analyze_tattoo_slots
    from ..analyzer.tree_helpers import classify_base_target_type

    _check_tattoos_capability(worker)
    with worker.lock:
        if tattoo is None and not reset:
            tree_state = worker.call("get_tree_state", {})
            nodes_db = (worker.get_cached_tree_data(tree_state.get("treeVersion")) or {}).get("nodes", {})
            dynamic_map = {
                str(n["id"]): n
                for n in (tree_state.get("dynamicNodes") or ())
                if isinstance(n, dict) and "id" in n
            }
            nid, _ = _resolve_node_token(worker, str(node).strip(), nodes_db, dynamic_map)
            raw_base = nodes_db.get(str(nid)) if isinstance(nodes_db.get(str(nid)), dict) else None
            allocated = nid in {int(x) for x in (tree_state.get("allocatedNodes") or ()) if str(x).lstrip("-").isdigit()}
            overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
            slots = analyze_tattoo_slots(tree_state, nodes_db if isinstance(nodes_db, dict) else {})
            opt_res = worker.call("tattoo_options", {"id": nid, "showLegacy": bool(show_legacy)})
            return {
                "mode": "inspect",
                "nodeId": nid,
                "allocated": allocated,
                "baseNodeName": (raw_base or {}).get("name"),
                "baseTargetType": classify_base_target_type(raw_base),
                "currentOverride": overrides.get(str(nid)),
                "allowed": bool(opt_res.get("allowed", False)),
                "count": opt_res.get("count"),
                "options": list(opt_res.get("options") or ()),
                "tattooSlots": slots,
            }

        before_stats = (worker.call("get_stats", {}) or {}).get("stats") or {}

        def _do_tattoo(w: BridgeWorker) -> dict[str, Any]:
            detail = apply_tattoo_in_place(
                w,
                node=node,
                tattoo=tattoo,
                reset=reset,
                show_legacy=show_legacy,
            )
            after_stats = (w.call("get_stats", {}) or {}).get("stats") or {}
            _attach_req_deficits(detail, before_stats, after_stats, strict_req=strict_req)
            detail.setdefault("reqDeficits", [])
            return detail

        res = run_mutation(worker, _do_tattoo, apply=apply)
        detail = res.get("detail") or {}
        if detail.get("reqDeficits"):
            res["reqDeficits"] = detail["reqDeficits"]
        if detail.get("reqWarnings"):
            res["reqWarnings"] = list(detail["reqWarnings"])
        extra_warns = list(detail.get("warnings") or ())
        if extra_warns:
            existing = list(res.get("warnings") or ())
            for w_txt in extra_warns:
                if w_txt not in existing:
                    existing.append(w_txt)
            res["warnings"] = existing
        return res
