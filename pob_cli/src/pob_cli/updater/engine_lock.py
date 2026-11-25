"""One writer at a time per engine folder, and recovery of an update that was cut short."""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .constants import UpdateError
from .self_update import _lock

BACKUP_DIRNAME = ".bak"
STAGING_DIRNAME = ".staging"
LOCK_FILENAME = ".update.lock"
_PARTIAL_SUFFIX = ".pob-partial"

_held = threading.local()


class EngineBusyError(UpdateError):
    """Another process or thread is installing or updating the same engine folder."""


@contextmanager
def engine_update_lock(engine_dir: Path) -> Iterator[None]:
    """Hold `engine_dir` for one install or update: staging, swap, probe and rollback.

    Bootstrap and update share `<engine>/.staging` and `<engine>/.bak`, so two of them at once
    delete each other's downloads and backups. The OS drops the lock when its process ends, and
    whoever takes it next first puts back what an interrupted run had replaced. The lock file
    stays: deleting it would let a third run lock a new file while a second holds the old one.
    """
    eng = Path(engine_dir)
    key = os.path.normcase(os.path.abspath(eng))
    held: set[str] = _held.__dict__.setdefault("keys", set())
    if key in held:  # bootstrap seeds from a legacy pack, then updates the same folder
        yield
        return
    try:
        eng.mkdir(parents=True, exist_ok=True)
        fh = open(eng / LOCK_FILENAME, "a+b")
    except OSError as exc:
        raise UpdateError(f"Cannot write to the engine folder {eng}: {exc}") from exc
    with fh:
        try:
            _lock(fh)
        except OSError as exc:
            raise EngineBusyError(
                f"Another pob process is installing or updating the engine in {eng}; let it finish first."
            ) from exc
        held.add(key)
        try:
            restore_interrupted_apply(eng)
            yield
        finally:
            held.discard(key)
            with suppress(OSError):
                _lock(fh, release=True)


def backup_file(src: Path, bak: Path) -> None:
    """Copy `src` to `bak` so that `bak` only ever exists whole."""
    bak.parent.mkdir(parents=True, exist_ok=True)
    partial = bak.with_name(bak.name + _PARTIAL_SUFFIX)
    shutil.copy2(src, partial)
    os.replace(partial, bak)


def restore_interrupted_apply(engine_dir: Path) -> int:
    """Put back the files an update had replaced when it was cut short; return how many.

    A finished apply, kept or rolled back, removes its backup folder. One that is still there
    marks a run killed between its first replaced file and its probe, which leaves the engine a
    mix of two versions that still passes for a valid one. Files the dead run added stay: nothing
    recorded them, and the next update check finds whatever is missing next to them.
    """
    eng = Path(engine_dir)
    bak_dir = eng / BACKUP_DIRNAME
    if not bak_dir.is_dir():
        return 0
    restored = 0
    for bak in sorted(p for p in bak_dir.rglob("*") if p.is_file()):
        if bak.name.endswith(_PARTIAL_SUFFIX):
            continue  # its copy was cut short, so the engine still holds the original
        dst = eng / bak.relative_to(bak_dir)
        with suppress(OSError):
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(bak, dst)
            restored += 1
    shutil.rmtree(bak_dir, ignore_errors=True)
    return restored


def recover_idle_engine(engine_dir: Path) -> int:
    """Restore an interrupted apply in `engine_dir` unless another process is at work in it."""
    if not (Path(engine_dir) / BACKUP_DIRNAME).is_dir():
        return 0  # the usual case, and the only one for a read-only or legacy engine: write nothing
    eng = Path(engine_dir)
    key = os.path.normcase(os.path.abspath(eng))
    if key in _held.__dict__.get("keys", ()):
        return 0
    try:
        fh = open(eng / LOCK_FILENAME, "a+b")
    except OSError:
        return 0
    with fh:
        try:
            _lock(fh)
        except OSError:
            return 0
        try:
            return restore_interrupted_apply(eng)
        finally:
            with suppress(OSError):
                _lock(fh, release=True)
