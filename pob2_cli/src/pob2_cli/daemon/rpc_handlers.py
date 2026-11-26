"""Single-slot and extensible RPC action dispatchers for ``pob2_cli.daemon.DaemonManager``."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..bridge_protocol import decode_share_code_xml, validate_rpc_method
from ..errors import CrossGameHallucinationError, DaemonRequestError
from ..game_profile import GAME_ID
from ..snapshot import (
    capture_digest,
    compact_digest_summary,
    history_of,
    load_build,
    redo_build,
    undo_build,
)
from .read_cache import compute_source_hash, is_live_source
from .runtime import normalize_slot_name

if TYPE_CHECKING:
    from .slots import DaemonManager

SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


def _h_load(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
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
    src_hash = compute_source_hash(params)
    slot_nm = normalize_slot_name(params.get("target") or getattr(w, "slot_name", None))
    read_cache = getattr(m, "_read_cache", None)
    if read_cache is not None:
        read_cache.record_slot_load(slot_nm, history_of(w).rev, src_hash, load_res, live=is_live_source(params))
    if src_hash and isinstance(load_res, dict):
        load_res["sourceHash"] = src_hash
    return m._attach_warning_if_degraded(load_res)


def _h_export(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    with w.lock:
        code_res = w.call("export_code", {})
        code_str = str(code_res.get("code") or "")
        xml_str = None
        if params.get("include_xml"):
            xml_res = w.call("save_xml", {})
            xml_str = xml_res.get("xml") or decode_share_code_xml(code_str)
        return {
            "game": GAME_ID,
            "code": code_str,
            "xml": xml_str,
            "pobDir": str(m.pob_dir) if m.pob_dir else None,
        }


def _h_undo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return undo_build(w)


def _h_redo(_m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    return redo_build(w)


def _h_summary(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    digest = capture_digest(w)
    out = compact_digest_summary(digest)
    try:
        out["engineDiagnostics"] = w.get_engine_diagnostics()
    except Exception:
        pass
    return m._attach_warning_if_degraded(out)


def _h_stats(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_stats", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_build_info(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_build_info", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_tree_state(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_tree_state", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_items(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("list_items", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_skills(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("list_skills", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_weapon_set_state(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("get_weapon_set_state", {}))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_node_power(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    rpc_params: dict[str, Any] = {}
    if params.get("stat"):
        rpc_params["stat"] = str(params["stat"])
    if params.get("max_depth") is not None:
        rpc_params["maxDepth"] = int(params["max_depth"])
    elif params.get("maxDepth") is not None:
        rpc_params["maxDepth"] = int(params["maxDepth"])
    res = dict(w.call("node_power", rpc_params, timeout=45.0))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_probe(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("agent_probe_build", {}, timeout=30.0))
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_diagnostics(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.get_engine_diagnostics())
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_new_build(m: DaemonManager, _p: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    res = dict(w.call("new_build", {}))
    code = str(w.call("export_code", {}).get("code") or "")
    history_of(w).load_build(code, "new_build")
    res["game"] = GAME_ID
    return m._attach_warning_if_degraded(res)


def _h_rpc(m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    method = str(params.get("method") or "").strip()
    if not method:
        raise DaemonRequestError("Action 'rpc' requires non-empty 'method'.")
    validate_rpc_method(method, w.caps)
    rpc_params = params.get("params")
    if rpc_params is not None and not isinstance(rpc_params, dict):
        raise DaemonRequestError("Action 'rpc' field 'params' must be a JSON object.")
    timeout = float(params["timeout"]) if params.get("timeout") is not None else None
    res = dict(w.call(method, rpc_params or {}, timeout=timeout))
    res.setdefault("game", GAME_ID)
    return m._attach_warning_if_degraded(res)


_SLOT_ACTION_HANDLERS: dict[str, SlotHandler] = {
    "load": _h_load,
    "export": _h_export,
    "undo": _h_undo,
    "redo": _h_redo,
    "summary": _h_summary,
    "stats": _h_stats,
    "build_info": _h_build_info,
    "tree_state": _h_tree_state,
    "items": _h_items,
    "skills": _h_skills,
    "weapon_set_state": _h_weapon_set_state,
    "node_power": _h_node_power,
    "probe": _h_probe,
    "diagnostics": _h_diagnostics,
    "new_build": _h_new_build,
    "rpc": _h_rpc,
}


def register_slot_handler(action: str, handler: SlotHandler) -> None:
    """Register an additional daemon slot RPC action (for extension layers)."""
    _SLOT_ACTION_HANDLERS[action] = handler


def dispatch_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    w: BridgeWorker,
) -> dict[str, Any]:
    """Execute a single-slot RPC action under its per-slot lock and reader barrier."""
    handler = _SLOT_ACTION_HANDLERS.get(action)
    if handler is None:
        raise ValueError(f"Unknown PoE2 daemon action '{action}'.")
    return handler(manager, params, w)
