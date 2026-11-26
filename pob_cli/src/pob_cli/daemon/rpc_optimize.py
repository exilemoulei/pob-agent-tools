"""Ranking and optimisation RPC handlers (rank-nodes, respec, rank-gems) for `DaemonManager`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker

if TYPE_CHECKING:
    from .slots import DaemonManager

_SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


def _h_rank_nodes(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import rank_nodes, rank_nodes_scored, rank_refunds
    from ..scoring import floors_from_json

    max_dist = int(params["max_dist"]) if params.get("max_dist") is not None else 5
    top = int(params["top"]) if params.get("top") is not None else 15
    score = {str(k): float(v) for k, v in (params.get("score") or {}).items()}
    floors = floors_from_json(params.get("floors"))
    stat = str(params.get("stat") or "CombinedDPS")
    if params.get("refund"):
        return rank_refunds(w, score=score or None, floors=floors, top=top)
    if score or floors:
        # --floor alone keeps --stat as the objective.
        return rank_nodes_scored(w, score=score or {stat: 1.0}, floors=floors, max_dist=max_dist, top=top)
    return rank_nodes(w, stat=stat, max_dist=max_dist, top=top)


def _h_respec(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import plan_respec
    from ..scoring import floors_from_json

    return plan_respec(
        w,
        score={str(k): float(v) for k, v in (params.get("score") or {}).items()} or None,
        floors=floors_from_json(params.get("floors")),
        rounds=int(params["rounds"]) if params.get("rounds") is not None else 3,
        max_dist=int(params["max_dist"]) if params.get("max_dist") is not None else 5,
    )


def _h_rank_gems(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..comparator import optimize_links, rank_gems

    if params.get("optimize"):
        return optimize_links(
            w,
            group_selector=str(params.get("group") or "main"),
            field=str(params.get("field") or "CombinedDPS"),
            passes=int(params["passes"]) if params.get("passes") is not None else 2,
            include_legacy=bool(params.get("include_legacy", False)),
        )

    return rank_gems(
        w,
        group_selector=str(params.get("group") or "main"),
        slot_idx=params.get("slot_idx"),
        add_link=bool(params.get("add_link", False)),
        field=str(params.get("field") or "CombinedDPS"),
        top=int(params["top"]) if params.get("top") is not None else 15,
        include_legacy=bool(params.get("include_legacy", False)),
    )


OPTIMIZE_ACTION_HANDLERS: dict[str, _SlotHandler] = {
    "rank_nodes": _h_rank_nodes,
    "respec": _h_respec,
    "rank_gems": _h_rank_gems,
}
