"""`BuildTransaction`: every edit of a slot's build runs inside one, and is rolled back or committed."""

from __future__ import annotations

import enum
from typing import Any

from .checkpoint import (
    BuildSnapshot,
    drop_snapshot,
    fall_back_to_code,
    restore_snapshot,
    supports_cow_snapshot,
    take_snapshot,
)
from .history import SlotHistory, history_of
from .verify import verify_build


class BuildScope(enum.Enum):
    """How far an edit reaches, which decides the checkpoint a transaction takes."""

    # Edits inside the active tree spec and item/skill/config sets of the loaded build: a Lua CoW
    # snapshot covers them.
    ACTIVE_SETS = 1
    # Switches to or adds a spec, item/skill/config set or loadout: Lua refuses a CoW snapshot of the
    # old sets, so an export_code checkpoint is taken instead.
    SETS = 2
    # Replaces the build (new_build, import): only a share code can bring the old one back.
    REPLACES_BUILD = 3

    @classmethod
    def widest(cls, scopes: Any) -> BuildScope:
        return max(scopes, key=lambda s: s.value, default=cls.ACTIVE_SETS)


class BuildTransaction:
    """Holds the worker lock and a base checkpoint; on exit the build is rolled back unless committed.

    Outside a transaction the live build equals the slot's committed build (`SlotHistory.committed`,
    mirrored as the worker's `last_good_code`), so the outermost transaction checkpoints by that
    code instead of exporting it, and a commit exports only the new build. A Lua CoW snapshot is
    used when the worker supports it, the scope allows it and no commit is expected. Transactions
    nest (a matrix inside a batch); a nested one checkpoints the build as it is then and cannot
    commit. `restore(rebuild=False)` leaves calc output stale until the next frame: `fresh()`, which
    `capture_digest` calls, rebuilds it before a read.
    """

    def __init__(
        self,
        worker: Any,
        *,
        scope: BuildScope = BuildScope.ACTIVE_SETS,
        will_commit: bool = False,
    ) -> None:
        self.worker = worker
        self.scope = scope
        self.will_commit = will_commit
        self.history: SlotHistory = history_of(worker)
        self.parent: BuildTransaction | None = None
        self.base: BuildSnapshot | None = None
        self.before_code: str | None = None
        self.stale = False
        # The digest of the build at the base checkpoint, when the caller took one: a rollback on an
        # exception then verifies a CoW restore against it and re-imports the code on drift.
        self.verify_digest: dict[str, Any] | None = None
        self.notes: list[str] = []
        self._nested: list[BuildSnapshot] = []
        self._entry_code: str | None = None
        self._sent_at: int | None = None
        self._closed = False
        self._committed = False
        self._lock: Any = None

    @property
    def use_cow(self) -> bool:
        return self.scope is BuildScope.ACTIVE_SETS and not self.will_commit and supports_cow_snapshot(self.worker)

    def _known_code(self) -> str | None:
        """The committed build's code, when it is known to be the live build."""
        code = self.history.committed
        if self.parent is None and code and getattr(self.worker, "last_good_code", None) == code:
            return code
        return None

    def _nothing_to_keep(self) -> bool:
        return (
            self.parent is None
            and self.will_commit
            and self.scope is BuildScope.REPLACES_BUILD
            and not self.history.committed
            and not getattr(self.worker, "last_good_code", None)
        )

    def __enter__(self) -> BuildTransaction:
        self._lock = self.worker.lock
        self._lock.__enter__()
        try:
            self.parent = self.history.active
            if self.parent is not None:
                self.parent.fresh()
            self._entry_code = getattr(self.worker, "last_good_code", None)
            known = self._known_code()
            if self._nothing_to_keep():
                # A load into a slot with no committed build: a failed import heals to a new build.
                self.base = None
            else:
                self.base = take_snapshot(self.worker, prefer_cow=self.use_cow, fresh_fallback=True, code=known)
                self.before_code = known or (self.base.token if self.base.kind == "code" else self.base.fallback_code)
            self._sent_at = self._requests_sent()
            self.history.active = self
        except BaseException as exc:
            self._lock.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        try:
            if not self._closed:
                try:
                    self._rollback_on_exit(exc)
                except Exception as restore_exc:
                    if exc is None:
                        raise
                    exc.add_note(f"Restoring the build afterwards also failed: {restore_exc}")
        finally:
            self.history.active = self.parent
            if self.parent is not None:
                self.parent.stale = self.parent.stale or self.stale
            elif self.stale and getattr(self.worker, "is_running", True):
                # The next command reads get_stats, which does not settle a pending rebuild. A
                # stopped engine is skipped: pumping would relaunch it under the lock, and a
                # restart re-imports the build with fresh output anyway.
                try:
                    self.fresh()
                except Exception:
                    # The slot's calc output may be stale: its read cache must not serve or store it.
                    self.history.stale = True
            if not self._committed and getattr(self.worker, "last_good_code", None) is not None:
                # Only this transaction's commit moves the restart code; one the bridge put aside
                # after the engine rejected it (None) stays dropped.
                self.worker.last_good_code = self._entry_code
            self._lock.__exit__(exc_type, exc, tb)
        return False

    def _rollback_on_exit(self, exc: BaseException | None) -> None:
        """Roll back an unclosed transaction; after an exception a CoW restore is verified (and repaired)."""
        untouched = self._sent_at is not None and self._requests_sent() == self._sent_at and not self._nested
        if (
            exc is None
            or untouched
            or self.verify_digest is None
            or self.base is None
            or self.base.kind != "cow"
        ):
            self.rollback()
            return
        check = self.rollback_verified(self.verify_digest)
        if not check.get("verified"):
            exc.add_note(f"Restoring the build afterwards could not be verified: {check.get('detail')}")
        elif check.get("fallback"):
            exc.add_note(f"The build was restored by re-importing its code: {check['fallback']}")

    def checkpoint(self) -> BuildSnapshot:
        """A checkpoint of the build as it is now, part way through this transaction's edits."""
        cp = take_snapshot(self.worker, prefer_cow=self.use_cow, fresh_fallback=True)
        self._nested.append(cp)
        return cp

    def drop(self, cp: BuildSnapshot | None) -> None:
        if cp is None:
            return
        if cp in self._nested:
            self._nested.remove(cp)
        drop_snapshot(self.worker, cp)

    def restore(self, cp: BuildSnapshot | None = None, *, rebuild: bool = True, drop: bool = False) -> str | None:
        """Put the build back to `cp` (the base by default); returns a note when a fallback was used.

        `rebuild=False` skips the recalculation a CoW restore would run, for a caller that edits
        again or reads through `direct_calc` next; the calc output stays stale until `fresh()`.
        """
        target = cp or self.base
        assert target is not None
        note = restore_snapshot(self.worker, target, drop=drop, rebuild=rebuild)
        if drop and target in self._nested:
            self._nested.remove(target)
        # cow_restore runs a frame only when it changed something, so a rebuilding restore that found
        # nothing to undo leaves an earlier lazy restore's output stale; an import always runs one.
        self.stale = target.kind == "cow" and (self.stale or not rebuild)
        if note:
            self.notes.append(note)
        return note

    def fresh(self) -> None:
        """Rebuild calc output a lazy restore left stale (one engine frame), before reading it."""
        if self.stale or self.history.stale:
            self.worker.call("pump", {"seconds": 0})
            self.stale = False
            self.history.stale = False

    def verify(self, before_digest: dict[str, Any], cp: BuildSnapshot | None = None) -> dict[str, Any]:
        """Check the build matches `before_digest` after a restore to `cp`; re-import it when a CoW restore drifted."""
        target = cp or self.base
        assert target is not None
        self.fresh()
        if target.kind != "cow":
            return verify_build(self.worker, before_digest, code=target.token)
        check = verify_build(self.worker, before_digest)
        if check.get("verified") or not target.fallback_code:
            return check
        note = fall_back_to_code(self.worker, target, f"CoW restore drifted ({check.get('detail')})")
        self.notes.append(note)
        check = verify_build(self.worker, before_digest, code=target.token)
        check["fallback"] = note
        return check

    def _drop_nested(self) -> None:
        while self._nested:
            drop_snapshot(self.worker, self._nested.pop())

    def _requests_sent(self) -> int | None:
        count = getattr(self.worker, "_req_counter", None)
        return count if isinstance(count, int) else None

    def rollback(self) -> str | None:
        """Restore the base checkpoint (rebuilding calc output) and end the transaction.

        When the worker counts its requests and none was sent since the checkpoint (a load refused
        by a guardrail or a failed download), the build is untouched and nothing is restored.
        """
        if self._sent_at is not None and self._requests_sent() == self._sent_at and not self._nested:
            self.release()
            return None
        self._closed = True
        self._drop_nested()
        return self.restore(drop=True) if self.base is not None else None

    def rollback_verified(self, before_digest: dict[str, Any]) -> dict[str, Any]:
        """`rollback`, then `verify` against `before_digest`; `fallback` names any fallback used."""
        note = self.rollback()
        check = self.verify(before_digest)
        if note and not check.get("fallback"):
            check["fallback"] = note
        return check

    def release(self) -> None:
        """End the transaction without restoring, for a build this transaction never changed."""
        self._closed = True
        self._drop_nested()
        drop_snapshot(self.worker, self.base)

    def _end_committed(self) -> None:
        if self.parent is not None:
            raise RuntimeError("a nested build transaction cannot commit")
        self.release()
        self._committed = True

    def _export(self) -> str:
        return str(self.worker.call("export_code", {}).get("code") or "")

    def commit(self, *, undoable: bool = True, after_code: str | None = None) -> None:
        """Keep the edited build: it becomes the committed build and the old one an undo step."""
        after = after_code or self._export()
        self._end_committed()
        self.history.commit(self.before_code, after, undoable=undoable)

    def commit_load(self, source_label: str | None) -> None:
        """Keep a newly loaded build as the slot's committed build with an empty history."""
        code = self._export()
        self._end_committed()
        self.history.load_build(code, source_label)

    def commit_step(self, action: str) -> None:
        """Keep the undo (or redo) step the engine just loaded and move the history by one."""
        current = self.before_code or ""
        self._end_committed()
        self.history.step(action, current)
