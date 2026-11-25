"""One writer per engine folder, and an install or update that stops halfway leaves the old engine."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from test_updater import _make_fake_engine

from pob_cli import updater
from pob_cli.updater import bootstrap as bootstrap_mod
from pob_cli.updater.engine_lock import (
    EngineBusyError,
    backup_file,
    engine_update_lock,
    recover_idle_engine,
    restore_interrupted_apply,
)

OK_PROBE = {"ok": True, "pobVersion": "2.69.0"}


def _hold_lock_elsewhere(engine: Path) -> tuple[threading.Event, threading.Thread]:
    """Take the engine's lock on another thread, as a second process would, until the event is set."""
    taken, release = threading.Event(), threading.Event()

    def _holder() -> None:
        with engine_update_lock(engine):
            taken.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    assert taken.wait(timeout=10)
    return release, thread


def _stage(engine: Path, files: dict[str, bytes]) -> dict[Path, Path]:
    staged: dict[Path, Path] = {}
    for rel, content in files.items():
        src = engine / ".staging" / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(content)
        staged[Path(rel)] = src
    return staged


def test_a_second_writer_is_refused_and_the_same_one_may_nest(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    release, thread = _hold_lock_elsewhere(engine)
    try:
        with pytest.raises(EngineBusyError, match="installing or updating the engine"), engine_update_lock(engine):
            pass
    finally:
        release.set()
        thread.join(timeout=10)

    # Bootstrap seeds from a legacy pack and then updates the same folder under its own lock.
    with engine_update_lock(engine), engine_update_lock(engine):
        pass
    with engine_update_lock(engine):
        pass


def test_an_interrupted_swap_is_put_back_by_the_next_writer(tmp_path: Path) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"new main\n", "Data/Gems.lua": b"old gems\n"})
    main, gems = engine / "PoE1" / "Modules" / "Main.lua", engine / "PoE1" / "Data" / "Gems.lua"
    # What a killed apply leaves: Main.lua already replaced with its backup taken, Gems.lua's backup cut short.
    bak = engine / ".bak" / "PoE1"
    (bak / "Modules").mkdir(parents=True)
    (bak / "Modules" / "Main.lua").write_bytes(b"old main\n")
    (bak / "Data").mkdir()
    (bak / "Data" / "Gems.lua.pob-partial").write_bytes(b"old ge")

    with engine_update_lock(engine):
        assert main.read_bytes() == b"old main\n"
        assert gems.read_bytes() == b"old gems\n"
        assert not (engine / ".bak").exists()


def test_recovery_leaves_a_busy_or_clean_engine_alone(tmp_path: Path) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"new main\n"})
    assert recover_idle_engine(engine) == 0
    assert not (engine / ".update.lock").exists()  # a legacy pack is never written to

    backup_file(engine / "PoE1" / "Modules" / "Main.lua", engine / ".bak" / "PoE1" / "Modules" / "Main.lua")
    (engine / ".bak" / "PoE1" / "Modules" / "Main.lua").write_bytes(b"old main\n")
    release, thread = _hold_lock_elsewhere_without_restore(engine)
    try:
        assert recover_idle_engine(engine) == 0  # that backup belongs to the process at work
        assert (engine / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"new main\n"
    finally:
        release.set()
        thread.join(timeout=10)
    assert recover_idle_engine(engine) == 1
    assert (engine / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"old main\n"
    assert restore_interrupted_apply(engine) == 0


def _hold_lock_elsewhere_without_restore(engine: Path) -> tuple[threading.Event, threading.Thread]:
    """Hold only the lock file, like a process that is mid-swap and owns the backup folder."""
    from pob_cli.updater.self_update import _lock

    taken, release = threading.Event(), threading.Event()

    def _holder() -> None:
        with open(engine / ".update.lock", "a+b") as fh:
            _lock(fh)
            taken.set()
            release.wait(timeout=30)
            _lock(fh, release=True)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    assert taken.wait(timeout=10)
    return release, thread


def test_ctrl_c_during_the_probe_still_rolls_the_swap_back(tmp_path: Path) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"old main\n"})
    staged = _stage(engine, {"PoE1/Modules/Main.lua": b"new main\n", "PoE1/Modules/Added.lua": b"added\n"})

    def _interrupted(_engine: Path) -> dict[str, Any]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bootstrap_mod._apply_staged_files_with_gate(engine, staged, probe_fn=_interrupted)
    assert (engine / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"old main\n"
    assert not (engine / "PoE1" / "Modules" / "Added.lua").exists()
    assert not (engine / ".bak").exists() and not (engine / ".staging").exists()


def test_from_dir_that_fails_the_probe_leaves_the_engine_as_it_was(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    engine = _make_fake_engine(rt / "engine", files={"Modules/Main.lua": b"working main\n"})
    source = _make_fake_engine(tmp_path / "pack", files={"Modules/Main.lua": b"broken main\n", "Modules/Extra.lua": b"extra\n"})

    with pytest.raises(updater.EngineGateError, match="was put back as it was"):
        updater.bootstrap_engine(from_dir=source, runtime_dir=rt, probe_fn=lambda _p: {"ok": False, "error": "Build.Init"})
    assert (engine / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"working main\n"
    assert not (engine / "PoE1" / "Modules" / "Extra.lua").exists()
    assert not (engine / ".bak").exists() and not (engine / ".staging").exists()

    state = updater.bootstrap_engine(from_dir=source, runtime_dir=rt, probe_fn=lambda _p: dict(OK_PROBE))
    assert state["state"] == updater.STATE_UPDATED and state["source"] == f"offline:{source}"
    assert (engine / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"broken main\n"
    assert (engine / "PoE1" / "Modules" / "Extra.lua").read_bytes() == b"extra\n"
    assert (source / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"broken main\n"  # the source is only read


def test_bootstrap_into_a_busy_engine_reports_it(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    source = _make_fake_engine(tmp_path / "pack")
    release, thread = _hold_lock_elsewhere(rt / "engine")
    try:
        with pytest.raises(EngineBusyError):
            updater.bootstrap_engine(from_dir=source, runtime_dir=rt, probe_fn=lambda _p: dict(OK_PROBE))
        # The daemon's own entry point answers with a state instead of raising.
        state = updater.check_and_apply_updates(
            engine_dir=rt / "engine", runtime_dir=rt, probe_fn=lambda _p: dict(OK_PROBE)
        )
        assert state["state"] == updater.STATE_BOOTSTRAP_MISSING
        assert "installing or updating the engine" in state["networkError"]
    finally:
        release.set()
        thread.join(timeout=10)
