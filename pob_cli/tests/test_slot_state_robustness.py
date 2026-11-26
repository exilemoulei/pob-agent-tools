"""Slot persistence and rollback robustness: save failures, exception-path verify, stale flag, daemon.json mode."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_cow_review_fixes import _FlakyCowWorker
from test_cow_snapshot_sweep import _CowFakeWorker

import pob_cli.bridge_protocol as bp
import pob_cli.snapshot.history as history_mod
from pob_cli.bridge_boot import _persists
from pob_cli.daemon.read_cache import _is_hist_unsettled
from pob_cli.daemon.runtime import write_private_json
from pob_cli.snapshot import BuildTransaction, SlotHistory, history_of, run_mutation

_CAPS = {"cowSnapshot": True, "directCalc": True}


def _bump(w: Any) -> None:
    w.dps += 15000.0


# --- 3: a failed slot save is reported, not swallowed ----------------------------------------


def test_write_slot_state_reports_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "slot_main.json"
    kw: dict[str, Any] = {"slot_name": "main", "last_good_code": "A", "undo_stack": [], "redo_stack": [], "source_label": None, "rev": 1}
    assert bp.write_slot_state(path, **kw) is True

    def _boom(*_a: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bp.os, "replace", _boom)
    assert bp.write_slot_state(path, **kw) is False
    assert "could not save slot 'main'" in capsys.readouterr().err
    assert [p.name for p in tmp_path.iterdir()] == ["slot_main.json"]  # no tmp file left behind


def test_apply_surfaces_persist_warning_and_keeps_the_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(history_mod, "write_slot_state", lambda *_a, **_k: False)
    w = _CowFakeWorker(caps=_CAPS)
    hist = SlotHistory("main", tmp_path)
    hist.committed = w._serialize()
    hist.bind(w)
    res = run_mutation(w, _bump, apply=True)
    assert res["applied"] is True
    assert "could not be saved" in res["persistWarning"]
    assert hist.rev == 1 and hist.committed == w._serialize()

    monkeypatch.setattr(history_mod, "write_slot_state", lambda *_a, **_k: True)
    res = run_mutation(w, _bump, apply=True)
    assert "persistWarning" not in res


# --- 4: an exception after a CoW checkpoint verifies the restore ------------------------------


def test_exception_path_verifies_cow_restore_and_reimports_on_drift() -> None:
    w = _FlakyCowWorker(caps=_CAPS)
    orig = w._serialize()
    w.cow_restore_mode = "drift"  # cow_restore answers ok but leaves the build mutated

    def _bump_then_fail(worker: Any) -> None:
        worker.dps += 15000.0
        raise ValueError("bad step")

    with pytest.raises(ValueError, match="bad step") as info:
        run_mutation(w, _bump_then_fail, apply=False)
    assert w._serialize() == orig
    assert [m for m, _ in w.calls].count("import_code") == 1
    assert any("re-importing" in n for n in getattr(info.value, "__notes__", []))


def test_exception_path_with_a_clean_cow_restore_does_not_reimport() -> None:
    w = _FlakyCowWorker(caps=_CAPS)
    orig = w._serialize()

    def _fail(worker: Any) -> None:
        worker.dps += 1.0
        raise ValueError("bad step")

    with pytest.raises(ValueError):
        run_mutation(w, _fail, apply=False)
    assert w._serialize() == orig
    assert [m for m, _ in w.calls].count("import_code") == 0


# --- 9: persistence is an explicit flag, not the slot name ------------------------------------


def test_slot_named_test_persists_and_throwaway_history_does_not(tmp_path: Path) -> None:
    SlotHistory.load("test", tmp_path).load_build("CODE", None)
    assert SlotHistory.load("test", tmp_path).committed == "CODE"
    SlotHistory("main", tmp_path / "other", persist=False).load_build("CODE", None)
    assert not (tmp_path / "other").exists()

    class _W:
        slot_name = "test"

    w = _W()
    assert _persists(w) is False  # an unbound scratch worker
    SlotHistory("test", tmp_path).bind(w)
    assert _persists(w) is True
    assert history_of(_CowFakeWorker(caps=_CAPS)).persist is False


# --- 10: a failed rebuild marks the slot stale for the read cache -----------------------------


def test_failed_fresh_marks_history_stale_until_next_rebuild() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    w.last_good_code = w._serialize()
    hist = history_of(w)
    real_call = w.call
    fail_pump = {"on": True}

    def _call(method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if method == "pump" and fail_pump["on"]:
            raise RuntimeError("engine busy")
        return real_call(method, params, timeout)

    w.call = _call  # type: ignore[method-assign]
    with BuildTransaction(w) as tx:
        tx.release()
        tx.stale = True
    assert hist.stale is True
    assert _is_hist_unsettled(hist)

    fail_pump["on"] = False
    with BuildTransaction(w) as tx:
        tx.fresh()
        tx.release()
    assert hist.stale is False
    assert not _is_hist_unsettled(hist)


def test_commit_clears_history_stale() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    hist = history_of(w)
    hist.stale = True
    run_mutation(w, _bump, apply=True)
    assert hist.stale is False


# --- 8: daemon.json is private --------------------------------------------------------------


def test_write_private_json_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "daemon.json"
    stale_tmp = path.with_suffix(f".{os.getpid()}.tmp")
    stale_tmp.write_text("{}", encoding="utf-8")
    if os.name != "nt":
        os.chmod(stale_tmp, 0o644)
    write_private_json(path, {"token": "secret", "port": 1})
    assert bp.json.loads(path.read_text(encoding="utf-8"))["token"] == "secret"
    assert not stale_tmp.exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_hot_reload_rewrite_keeps_daemon_json_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.daemon import slot_updates

    state = tmp_path / "daemon.json"
    write_private_json(state, {"pid": os.getpid(), "token": "secret", "pob_dir": "old"})
    monkeypatch.setattr(slot_updates, "_daemon_state_file", lambda _rt=None: state)
    old_umask = os.umask(0o022)
    try:
        slot_updates.update_daemon_state_pob_dir(SimpleNamespace(runtime_dir=tmp_path, pob_dir=tmp_path / "new"))
    finally:
        os.umask(old_umask)
    data = bp.json.loads(state.read_text(encoding="utf-8"))
    assert data["token"] == "secret" and data["pob_dir"].endswith("new")
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
