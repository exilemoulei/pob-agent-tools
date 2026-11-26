"""Slot-level build commands on `BuildTransaction`: what-if/apply mutations, undo/redo and loads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from .digest import capture_digest, diff_digest
from .history import history_of
from .transaction import BuildScope, BuildTransaction


def _headline(digest: dict[str, Any]) -> dict[str, Any]:
    return {
        "primaryDpsKey": digest["primaryDpsKey"],
        "primaryDps": digest["primaryDps"],
        "totalEhp": digest["totalEhp"],
        "life": digest["life"],
        "points": digest.get("buildInfo", {}).get("points", {}),
    }


def run_mutation(
    worker: BridgeWorker,
    mutate_fn: Callable[[BridgeWorker], Any],
    *,
    apply: bool = False,
    scope: BuildScope = BuildScope.ACTIVE_SETS,
) -> dict[str, Any]:
    """Execute `mutate_fn(worker)` as a what-if (rolled back and verified) or, with `apply`, a commit.

    A what-if takes a Lua CoW snapshot when the worker supports one and `scope` allows it, and a
    CoW restore that drifted is repaired by re-importing the build. `apply` keeps the change: the
    committed build becomes an undo step and the slot file is saved once.
    """
    with BuildTransaction(worker, scope=scope, will_commit=apply) as tx:
        digest_before = capture_digest(worker)
        tx.verify_digest = digest_before
        action_detail = mutate_fn(worker)
        digest_after = capture_digest(worker)
        diff = diff_digest(digest_before, digest_after)

        if apply:
            tx.commit()
            mode_str = "APPLIED"
        else:
            tx.rollback()
            if tx.base is not None and tx.base.kind == "cow":
                check = tx.verify(digest_before)
                if not check.get("verified"):
                    tx.notes.append(f"restore check: {check.get('detail')}")
            mode_str = "WHAT-IF (not applied; pass --apply to persist)"

        out = {
            "applied": apply,
            "mode": mode_str,
            "detail": action_detail,
            "before": _headline(digest_before),
            "after": _headline(digest_after),
            "diff": diff,
            **tx.history.depths(),
        }
        if tx.notes:
            out["restoreFallback"] = "; ".join(tx.notes)
        return out


def _step_history(worker: BridgeWorker, *, action: str) -> dict[str, Any]:
    with worker.lock:
        history = history_of(worker)
        if not (history.undo if action == "undo" else history.redo):
            # Refused before a checkpoint, so there is nothing to roll back.
            raise DaemonRequestError(f"{action.capitalize()} stack is empty.")
        return _load_step(worker, action)


def _load_step(worker: BridgeWorker, action: str) -> dict[str, Any]:
    with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=True) as tx:
        src_stack = tx.history.undo if action == "undo" else tx.history.redo
        digest_before = capture_digest(worker)
        # The history moves only once the engine has loaded the step: a failed import leaves both stacks as they were.
        worker.call("import_code", {"code": src_stack[-1], "mode": "replace"})
        tx.commit_step(action)
        return {
            "action": action,
            "diff": diff_digest(digest_before, capture_digest(worker)),
            **tx.history.depths(),
        }


def undo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Load the previous undo step and make it the committed build."""
    return _step_history(worker, action="undo")


def redo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Load the next redo step and make it the committed build."""
    return _step_history(worker, action="redo")


def load_build(
    worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
) -> dict[str, Any]:
    """Load a build into the slot (`importers.load_build_into_worker`) and commit it with an empty history."""
    from ..importers import load_build_into_worker, load_source_label

    with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=True) as tx:
        out = load_build_into_worker(
            worker, source=source, xml_code=xml_code, char_name=char_name, league=league, realm=realm
        )
        tx.commit_load(load_source_label(source, out.get("character")))
        return out
