"""One writer per engine folder, and an install or update that stops halfway leaves the old engine."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_updater import _make_fake_engine

import pob_cli.daemon as daemon_mod
from pob_cli import cli, updater
from pob_cli.updater import bootstrap as bootstrap_mod
from pob_cli.updater import engine_lock as engine_lock_mod
from pob_cli.updater.engine_lock import (
    EngineBusyError,
    EngineLockError,
    backup_file,
    discard_backup,
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
        # The stock hint for this state blames the network and sends the user to their proxy.
        assert state["userHint"] == state["networkError"] == updater.get_update_warning(state)
    finally:
        release.set()
        thread.join(timeout=10)


def _old_main_backed_up(engine: Path) -> tuple[Path, Path]:
    """An engine whose Main.lua was replaced by a run that died: the new file in place, the old one in `.bak`."""
    main = engine / "PoE1" / "Modules" / "Main.lua"
    bak = engine / ".bak" / "PoE1" / "Modules" / "Main.lua"
    bak.parent.mkdir(parents=True)
    bak.write_bytes(b"old main\n")
    return main, bak


def test_a_backup_that_cannot_go_back_is_kept_and_blocks_the_next_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"new main\n"})
    main, bak = _old_main_backed_up(engine)
    real_replace = os.replace

    def _file_in_use(src: Any, dst: Any) -> None:
        if Path(dst) == main:
            raise PermissionError(13, "The process cannot access the file", str(dst))
        real_replace(src, dst)

    # Windows refuses to replace a DLL or script a running engine has open.
    monkeypatch.setattr(engine_lock_mod.os, "replace", _file_in_use)
    assert restore_interrupted_apply(engine) == 0
    assert bak.read_bytes() == b"old main\n" and main.read_bytes() == b"new main\n"

    staged = _stage(engine, {"PoE1/Modules/Main.lua": b"newer main\n"})
    res = bootstrap_mod._apply_staged_files_with_gate(engine, staged, probe_fn=lambda _p: dict(OK_PROBE))
    assert res["ok"] is False and res["rolledBack"] is False and "pob stop" in res["error"]
    assert main.read_bytes() == b"new main\n" and bak.read_bytes() == b"old main\n"
    assert not (engine / ".staging").exists()

    monkeypatch.undo()
    assert restore_interrupted_apply(engine) == 1
    assert main.read_bytes() == b"old main\n" and not (engine / ".bak").exists()


def test_a_kept_swap_cannot_be_mistaken_for_an_interrupted_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"old main\n"})
    main = engine / "PoE1" / "Modules" / "Main.lua"
    staged = _stage(engine, {"PoE1/Modules/Main.lua": b"new main\n"})
    real_rmtree = engine_lock_mod.shutil.rmtree

    def _killed_while_deleting(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == ".bak-done" and Path(path).exists():
            return  # the process dies here, with the old files still on disk
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(engine_lock_mod.shutil, "rmtree", _killed_while_deleting)
    res = bootstrap_mod._apply_staged_files_with_gate(engine, staged, probe_fn=lambda _p: dict(OK_PROBE))
    assert res["ok"] is True
    assert not (engine / ".bak").exists()
    assert (engine / ".bak-done" / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"old main\n"

    monkeypatch.undo()
    with engine_update_lock(engine):
        assert main.read_bytes() == b"new main\n"  # the update that went through stays
        assert not (engine / ".bak-done").exists()

    discard_backup(engine)  # nothing to drop is fine
    assert not (engine / ".bak").exists()


def test_a_rollback_cut_short_keeps_its_backups_for_the_next_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"old main\n"})
    main = engine / "PoE1" / "Modules" / "Main.lua"
    staged = _stage(engine, {"PoE1/Modules/Main.lua": b"new main\n"})

    def _second_ctrl_c(*_a: Any, **_k: Any) -> None:
        raise KeyboardInterrupt

    def _interrupted(_engine: Path) -> dict[str, Any]:
        monkeypatch.setattr(bootstrap_mod.os, "replace", _second_ctrl_c)  # hits the rollback too
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bootstrap_mod._apply_staged_files_with_gate(engine, staged, probe_fn=_interrupted)
    monkeypatch.undo()
    assert main.read_bytes() == b"new main\n"
    assert (engine / ".bak" / "PoE1" / "Modules" / "Main.lua").read_bytes() == b"old main\n"

    with engine_update_lock(engine):
        assert main.read_bytes() == b"old main\n"
        assert not (engine / ".bak").exists()


def test_an_engine_folder_that_cannot_be_locked_gives_a_state_not_an_exception(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    engine = rt / "engine"
    (engine / ".update.lock").mkdir(parents=True)  # opening it for writing fails on every platform

    with pytest.raises(EngineLockError, match="Cannot write to the engine folder") as caught:
        with engine_update_lock(engine):
            pass
    assert not isinstance(caught.value, EngineBusyError)

    state = updater.check_and_apply_updates(engine_dir=engine, runtime_dir=rt, probe_fn=lambda _p: dict(OK_PROBE))
    assert state["state"] == updater.STATE_BOOTSTRAP_MISSING
    assert "Cannot write to the engine folder" in state["userHint"]


def test_the_cli_shows_why_an_update_was_skipped_and_exits_5_on_a_busy_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy = "Another pob process is installing or updating the engine in /e; let it finish first."
    monkeypatch.setattr(daemon_mod, "read_daemon_state", lambda *a, **k: None)
    monkeypatch.setattr(
        updater,
        "check_and_apply_updates",
        lambda **_: {"state": "up_to_date", "localPobVersion": "2.68.1", "updateAvailable": True, "updateSkipped": busy},
    )
    res = CliRunner().invoke(cli.main, ["update"])
    assert res.exit_code == 0, res.output
    assert "Update Skipped" in res.output and "let it finish first" in res.output

    monkeypatch.setattr(updater, "bootstrap_engine", lambda **_: (_ for _ in ()).throw(EngineBusyError(busy)))
    res_boot = CliRunner().invoke(cli.main, ["bootstrap"])
    assert res_boot.exit_code == 5, res_boot.output
    assert f"Error: {busy}" in res_boot.output and "Traceback" not in res_boot.output


class _IdleWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.slot_name, self.pob_root, self.runtime_dir = slot_name, pob_dir, runtime_dir
        self.seen_main: bytes | None = None

    def start(self) -> None:
        assert self.pob_root is not None
        self.seen_main = (self.pob_root / "PoE1" / "Modules" / "Main.lua").read_bytes()

    def stop(self) -> None:
        pass


def test_a_daemon_puts_an_interrupted_swap_back_before_its_first_worker_loads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _make_fake_engine(tmp_path / "engine", files={"Modules/Main.lua": b"new main\n"})
    _old_main_backed_up(engine)
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _IdleWorker)
    mgr = daemon_mod.DaemonManager(
        pob_dir=engine, token="tok", runtime_dir=tmp_path / "rt", offline=True, skip_update_check=True
    )
    mgr.start()
    assert mgr._workers["main"].seen_main == b"old main\n"
    assert not (engine / ".bak").exists()
