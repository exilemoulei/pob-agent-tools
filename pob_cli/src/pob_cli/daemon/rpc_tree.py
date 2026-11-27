"""Passive tree RPC dispatch handlers for `DaemonManager` (`tree`, `tree_prune`, `tree_diff`, `tree_rank_wheels`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError

if TYPE_CHECKING:
    from .slots import DaemonManager

_SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


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


def _h_tree_prune(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer.tree_prune import prune_tree

    return prune_tree(
        w,
        top=int(params["top"]) if params.get("top") is not None else 15,
        max_branch_pts=int(params["max_branch_pts"])
        if params.get("max_branch_pts") is not None
        else (int(params["maxBranchPts"]) if params.get("maxBranchPts") is not None else 5),
        sort_by=str(params.get("sort") or "dps"),
        include_unsafe=bool(params.get("include_unsafe", params.get("includeUnsafe", False))),
        target_slot=str(params.get("target") or getattr(w, "slot_name", None) or "main"),
    )


def _h_tree_diff(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer.tree_diff import diff_tree_specs

    spec_a = str(params.get("spec_a") or "").strip()
    spec_b = str(params.get("spec_b") or "").strip()
    if not spec_a or not spec_b:
        raise DaemonRequestError("Both --spec-a and --spec-b are required for single-slot spec diff.")
    return diff_tree_specs(w, spec_a=spec_a, spec_b=spec_b)


def _h_tree_rank_wheels(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator.wheel_ranking import rank_wheels

    return rank_wheels(
        w,
        top=int(params["top"]) if params.get("top") is not None else 12,
        max_dist=int(params["max_dist"])
        if params.get("max_dist") is not None
        else (int(params["maxDist"]) if params.get("maxDist") is not None else 12),
        max_pts=int(params["max_pts"])
        if params.get("max_pts") is not None
        else (int(params["maxPts"]) if params.get("maxPts") is not None else 10),
        sort_by=str(params.get("sort") or "dps"),
        all_tags=bool(params.get("all_tags", params.get("allTags", False))),
        include_proxy_sockets=bool(
            params.get("include_proxy_sockets", params.get("includeProxySockets", True))
        ),
        target_slot=str(params.get("target") or getattr(w, "slot_name", None) or "main"),
    )


def _h_tree_tattoo_slots(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..analyzer.tattoo_slots import inspect_build_tattoo_slots

    return inspect_build_tattoo_slots(
        w,
        target_slot=str(params.get("target") or getattr(w, "slot_name", None) or "main"),
    )


TREE_ACTION_HANDLERS: dict[str, _SlotHandler] = {
    "tree": _h_tree,
    "tree_prune": _h_tree_prune,
    "tree_diff": _h_tree_diff,
    "tree_rank_wheels": _h_tree_rank_wheels,
    "tree_tattoo_slots": _h_tree_tattoo_slots,
}
