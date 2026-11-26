"""A slot's committed build, undo/redo stacks, revision and source label: the one owner of slot state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge_protocol import MAX_UNDO_STEPS, compact_source_label, read_slot_state, write_slot_state
from ..updater import get_runtime_dir


class SlotHistory:
    """The committed build of one slot and how it got there; the only writer of `slot_<slug>.json`.

    `committed` is the share code of the build the slot holds outside a transaction. The worker
    keeps a copy as `last_good_code`, the build it restores after an engine crash; `bind` and every
    change here keep that copy equal. Each change bumps `rev` (the daemon's read cache compares revisions)
    and saves the slot file once. `persist=False` keeps a throwaway history (tests, scratch engines)
    off disk; any slot name, "test" included, persists by default.
    """

    def __init__(self, slot_name: str = "main", runtime_dir: Path | None = None, *, persist: bool = True) -> None:
        self.slot_name = slot_name
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self.persist = persist
        # Set when the last save of the slot file failed; reported as `persistWarning` until a save succeeds.
        self.persist_warning: str | None = None
        # Set when a transaction could not rebuild calc output it left stale; the daemon's read cache
        # then neither serves nor stores this slot. Cleared by the next rebuild or commit.
        self.stale = False
        self.committed: str | None = None
        self.undo: list[str] = []
        self.redo: list[str] = []
        self.rev = 0
        self.source_label: str | None = None
        # The innermost open BuildTransaction on this slot's worker (runtime only, never saved).
        self.active: Any = None
        self._worker: Any = None

    @property
    def path(self) -> Path:
        return get_runtime_dir(self.runtime_dir) / f"slot_{self.slot_name}.json"

    @classmethod
    def load(cls, slot_name: str, runtime_dir: Path | None = None, *, persist: bool = True) -> SlotHistory:
        """The slot's history as last saved (empty when there is no slot file)."""
        hist = cls(slot_name, runtime_dir, persist=persist)
        data = read_slot_state(hist.path) if hist.persist else None
        if data:
            hist.committed = data.get("last_good_code") or None
            hist.undo = [c for c in data.get("undo_stack") or [] if isinstance(c, str)]
            hist.redo = [c for c in data.get("redo_stack") or [] if isinstance(c, str)]
            hist.source_label = compact_source_label(data.get("source_label"))
            raw_rev = data.get("rev")
            if isinstance(raw_rev, int) and not isinstance(raw_rev, bool) and raw_rev >= 0:
                hist.rev = raw_rev
        return hist

    def bind(self, worker: Any) -> None:
        """Make this the history of `worker`, which then restores the committed build when it (re)starts."""
        self._worker = worker
        worker.slot_history = self
        worker.last_good_code = self.committed

    def _changed(self) -> None:
        self.rev += 1
        self.stale = False
        if self._worker is not None:
            self._worker.last_good_code = self.committed
            if getattr(self._worker, "restore_failed", None) is not None:
                # A new committed build replaces the one put aside at boot.
                self._worker.restore_failed = None
        self.save()

    def save(self) -> None:
        if not self.persist:
            return
        self.source_label = compact_source_label(self.source_label)
        ok = write_slot_state(
            self.path,
            slot_name=self.slot_name,
            last_good_code=self.committed,
            undo_stack=self.undo,
            redo_stack=self.redo,
            source_label=self.source_label,
            rev=self.rev,
        )
        if ok is False:
            self.persist_warning = (
                f"The change is live in the daemon but could not be saved to {self.path}; "
                "it and its undo history will be lost when the daemon restarts."
            )
        else:
            self.persist_warning = None

    def load_build(self, code: str | None, source_label: str | None) -> None:
        """A new build replaced the slot: it becomes the committed build with an empty history."""
        self.committed = code or None
        self.source_label = source_label
        self.undo.clear()
        self.redo.clear()
        self._changed()

    def commit(self, before: str | None, after: str | None, *, undoable: bool = True) -> None:
        """An applied change: `after` becomes the committed build and `before` the next undo step."""
        if undoable and before:
            self.undo.append(before)
            del self.undo[:-MAX_UNDO_STEPS]
        self.redo.clear()
        self.committed = after or None
        self._changed()

    def step(self, action: str, current: str) -> str:
        """Move one undo (or redo) step after the engine loaded it; returns the code now committed."""
        src, dst = (self.undo, self.redo) if action == "undo" else (self.redo, self.undo)
        target = src.pop()
        dst.append(current)
        del dst[:-MAX_UNDO_STEPS]
        self.committed = target
        self._changed()
        return target

    def depths(self) -> dict[str, Any]:
        """Undo/redo depths, plus `persistWarning` while the slot file could not be saved."""
        out: dict[str, Any] = {"undoDepth": len(self.undo), "redoDepth": len(self.redo)}
        if self.persist_warning:
            out["persistWarning"] = self.persist_warning
        return out


def history_of(worker: Any) -> SlotHistory:
    """The history the daemon bound to `worker`, or a fresh unsaved one (tests, throwaway engines)."""
    hist = getattr(worker, "slot_history", None)
    if isinstance(hist, SlotHistory):
        return hist
    hist = SlotHistory(
        str(getattr(worker, "slot_name", "") or "test"), getattr(worker, "runtime_dir", None), persist=False
    )
    hist.committed = getattr(worker, "last_good_code", None) or None
    hist._worker = worker
    try:
        worker.slot_history = hist
    except AttributeError:
        pass
    return hist


def rebuild_if_stale(worker: Any, *, required: bool = True) -> None:
    """Rebuild calc output a failed transaction exit left stale, before a request outside one reads it.

    A failure is the engine's (the request fails, exit 5) rather than old numbers passed off as
    current. `required=False` is for a request that replaces the build anyway.
    """
    hist = history_of(worker)
    if not hist.stale or hist.active is not None:
        return
    try:
        worker.call("pump", {"seconds": 0})
    except Exception:
        if required:
            raise
        return
    hist.stale = False
