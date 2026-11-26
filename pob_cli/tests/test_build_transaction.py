"""`BuildTransaction` / `SlotHistory`: rollback, commit, drift repair and slot persistence (offline fakes + live engine)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine
from test_cow_review_fixes import _FlakyCowWorker
from test_cow_snapshot_sweep import _CowFakeWorker

import pob_cli.daemon as daemon_mod
import pob_cli.snapshot.history as history_mod
from pob_cli.bridge_client import BridgeError, BridgeWorker
from pob_cli.bridge_protocol import read_slot_state
from pob_cli.comparator import run_matrix
from pob_cli.errors import DaemonRequestError
from pob_cli.mutators import run_batch, run_sweep
from pob_cli.snapshot import (
    BuildScope,
    BuildTransaction,
    SlotHistory,
    history_of,
    load_build,
    redo_build,
    run_mutation,
    undo_build,
)

POB_DIR = find_runnable_engine()

_CAPS = {"cowSnapshot": True, "directCalc": True}


def _set_dps(value: float) -> Any:
    def _fn(w: Any) -> None:
        w.dps = value

    return _fn


class _RefusingImportWorker(_CowFakeWorker):
    """Fake whose `import_code` fails for the codes in `refuse`, as the engine does for a bad tree version."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.refuse: set[str] = set()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if method == "import_code" and str((params or {}).get("code")) in self.refuse:
            self.calls.append((method, dict(params or {})))
            raise BridgeError("RPC 'import_code' failed: Engine failed to parse treeVersion 3_99")
        return super().call(method, params, timeout)


# ---------------------------------------------------------------------------
# Bug a: a failed undo/redo import must leave the history as it was
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["undo", "redo"])
def test_failed_undo_or_redo_import_keeps_history(action: str) -> None:
    w = _RefusingImportWorker(caps=_CAPS)
    run_mutation(w, _set_dps(130000.0), apply=True)
    run_mutation(w, _set_dps(140000.0), apply=True)
    hist = history_of(w)
    if action == "redo":
        undo_build(w)
    before = (list(hist.undo), list(hist.redo), hist.committed, hist.rev)
    live = w._serialize()
    w.refuse = {(hist.undo if action == "undo" else hist.redo)[-1]}

    with pytest.raises(BridgeError, match="treeVersion"):
        (undo_build if action == "undo" else redo_build)(w)
    assert (list(hist.undo), list(hist.redo), hist.committed, hist.rev) == before
    assert w.last_good_code == hist.committed
    assert w._serialize() == live

    # The stack still works once the engine accepts the code again.
    w.refuse = set()
    res = (undo_build if action == "undo" else redo_build)(w)
    assert res["action"] == action


def test_undo_and_redo_on_empty_stacks_are_request_errors() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    with pytest.raises(DaemonRequestError, match="Undo stack is empty"):
        undo_build(w)
    with pytest.raises(DaemonRequestError, match="Redo stack is empty"):
        redo_build(w)
    assert history_of(w).rev == 0
    assert w.calls == []  # refused before the engine is touched: no rollback import


class _CountingWorker(_CowFakeWorker):
    """Fake that counts the requests it was sent, as `BridgeWorker._req_counter` does."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._req_counter = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self._req_counter += 1
        return super().call(method, params, timeout)


@pytest.mark.parametrize(
    ("source", "error"),
    [("https://poe.ninja/poe2/builds/char/a/b", DaemonRequestError), (None, ValueError)],
    ids=["poe2-url", "no-source"],
)
def test_load_refused_before_the_engine_does_not_reimport(source: str | None, error: type[Exception]) -> None:
    w = _CountingWorker(caps=_CAPS)
    with pytest.raises(error):
        load_build(w, source=source)
    assert [m for m, _ in w.calls] == []


def test_load_that_reached_the_engine_still_rolls_back() -> None:
    w = _CountingWorker(caps=_CAPS)
    orig = w._serialize()
    w.dps = 1.0  # what a failing profile import might leave behind
    w.call("set_config", {"var": "usePowerCharges", "value": False})
    w.last_good_code = orig
    history_of(w).committed = orig

    def _boom(worker: Any, **_kw: Any) -> dict[str, Any]:
        worker.call("set_config", {"var": "usePowerCharges", "value": True})
        raise BridgeError("profile import failed half way")

    import pob_cli.importers as importers_mod

    real = importers_mod.load_build_into_worker
    importers_mod.load_build_into_worker = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(BridgeError):
            load_build(w, source="https://www.pathofexile.com/account/view-profile/x/characters")
    finally:
        importers_mod.load_build_into_worker = real
    assert w._serialize() == orig


# ---------------------------------------------------------------------------
# Bug b: the slot's revision survives an engine hot reload
# ---------------------------------------------------------------------------


class _StubWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        import threading

        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.alive = False
        self.last_good_code: str | None = None
        self.hello_info: dict[str, Any] = {"pobVersion": "2.68.1"}
        self.lock = threading.RLock()

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def ensure_alive(self) -> None:
        self.alive = True

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"level": 95}


def test_hot_reload_keeps_slot_rev_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubWorker)
    monkeypatch.setattr(daemon_mod, "check_and_apply_updates", lambda **kw: {"state": "updated"})
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)
    mgr.main_worker.start()
    hist = history_of(mgr.main_worker)
    hist.load_build("CODE_A", "pasted")
    hist.commit("CODE_A", "CODE_B")
    assert hist.rev == 2

    old = mgr.main_worker
    mgr._hot_reload_update()
    new = mgr.main_worker
    assert new is not old
    assert history_of(new) is hist
    assert hist.rev == 2 and hist.undo == ["CODE_A"] and hist.source_label == "pasted"
    assert new.last_good_code == "CODE_B"  # what the new engine restores on start

    hist.commit("CODE_B", "CODE_C")
    state = read_slot_state(rt / "slot_main.json")
    assert state is not None and state["rev"] == 3


def test_status_reports_a_build_put_aside_at_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=tmp_path / "rt", offline=True, skip_update_check=True)
    assert "restoreFailed" not in mgr._slot_status_entry("main", mgr.main_worker)
    failed = {"error": "Engine failed to parse treeVersion 3_99", "codeFile": "x.txt", "skipped": False, "ts": 1.0}
    mgr.main_worker.restore_failed = failed  # type: ignore[attr-defined]
    assert mgr._slot_status_entry("main", mgr.main_worker)["restoreFailed"] == failed


@pytest.fixture
def fake_code_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `load_build(xml_code=...)` take a fake worker's JSON: the importer decodes and guards real share codes."""
    import pob_cli.guardrails as guardrails_mod
    import pob_cli.importers as importers_mod

    monkeypatch.setattr(importers_mod, "share_code_to_xml", lambda _code: "<PathOfBuilding/>")
    monkeypatch.setattr(guardrails_mod, "validate_build_xml", lambda *_a, **_k: None)


@pytest.mark.parametrize("commit", ["apply", "no_undo", "load", "undo"])
@pytest.mark.usefixtures("fake_code_loads")
def test_committing_a_new_build_ends_a_put_aside_restore(commit: str) -> None:
    w = _CowFakeWorker(caps=_CAPS)
    if commit == "undo":
        run_mutation(w, _set_dps(5.0), apply=True)
    w.restore_failed = {"error": "Engine failed to parse treeVersion 3_99"}  # type: ignore[attr-defined]
    if commit == "apply":
        run_mutation(w, _set_dps(5.0), apply=True)
    elif commit == "no_undo":
        run_batch(w, [{"op": "config", "set": {"usePowerCharges": True}}], apply=True, no_undo=True)
    elif commit == "load":
        load_build(w, xml_code=_CowFakeWorker(caps={})._serialize())
    else:
        undo_build(w)
    assert w.restore_failed is None  # type: ignore[attr-defined]


def test_what_if_keeps_a_put_aside_restore() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    failed = {"error": "Engine failed to parse treeVersion 3_99"}
    w.restore_failed = failed  # type: ignore[attr-defined]
    run_mutation(w, _set_dps(5.0), apply=False, scope=BuildScope.SETS)
    with pytest.raises(DaemonRequestError):
        undo_build(w)
    assert w.restore_failed is failed  # type: ignore[attr-defined]


def test_slot_history_survives_a_daemon_restart(tmp_path: Path) -> None:
    h = SlotHistory("boss", tmp_path)
    h.load_build("CODE_A", "https://pobb.in/abc")
    h.commit("CODE_A", "CODE_B")
    h2 = SlotHistory.load("boss", tmp_path)
    assert (h2.committed, h2.undo, h2.redo, h2.rev, h2.source_label) == ("CODE_B", ["CODE_A"], [], 2, "https://pobb.in/abc")
    # A throwaway history is never written; a user slot named "test" is saved like any other.
    SlotHistory("scratch", tmp_path, persist=False).load_build("X", None)
    assert not (tmp_path / "slot_scratch.json").exists()
    SlotHistory("test", tmp_path).load_build("Y", None)
    assert SlotHistory.load("test", tmp_path).committed == "Y"


# ---------------------------------------------------------------------------
# Bug c: a sweep variant reads fresh calcs after a lazy CoW restore
# ---------------------------------------------------------------------------


class _FrameWorker(_CowFakeWorker):
    """Fake whose `get_stats` and `get_calcs` read the output of the last engine frame, as PoB does.

    Edits run a frame and `pump{seconds:0}` runs one. `cow_restore` runs one only when it changed
    something and was not asked for `rebuild:false` (lua/fast_calc_cow.lua), so a restore that
    finds the build already restored leaves an earlier lazy restore's output stale.
    """

    _FRAMES = frozenset({"set_config", "reset_config", "add_item", "import_code", "pump"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.frame_dps = self.dps

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        p = dict(params or {})
        if method == "get_calcs":
            self.calls.append((method, p))
            row = {"label": "Total DPS", "cells": [{"raw": str(self.frame_dps)}]}
            return {"sections": [{"id": "Offence", "subsections": [{"label": "Main", "rows": [row]}]}]}
        if method == "get_stats":
            self.calls.append((method, p))
            stats = self._stats_map()
            stats["CombinedDPS"] = stats["TotalDPS"] = self.frame_dps
            return {"stats": stats}
        if method == "pump":
            self.calls.append((method, p))
            res: dict[str, Any] = {}
        else:
            before = self._serialize()
            res = super().call(method, params, timeout)
            if method == "cow_restore" and p.get("rebuild", True) and self._serialize() != before:
                self.frame_dps = self.dps
        if method in self._FRAMES:
            self.frame_dps = self.dps
        return res


def test_rollback_after_a_lazy_restore_refreshes_calc_output() -> None:
    w = _FrameWorker(caps=_CAPS)
    res = run_sweep(
        w,
        [
            {"name": "Power", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
            {"name": "Control", "ops": []},
        ],
    )
    assert res["restoreCheck"]["verified"] is True
    assert not res["restoreCheck"].get("fallback") and not res.get("restoreFallbacks")
    assert [m for m, _ in w.calls].count("import_code") == 0
    assert w.frame_dps == w.dps


def test_error_after_a_lazy_restore_leaves_fresh_calc_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.mutators.batch_sweep as batch_sweep_mod
    from pob_cli.errors import CrossGameHallucinationError

    real = batch_sweep_mod._sweep_variant

    def _guarded(worker: Any, tx: Any, var: dict[str, Any], v_name: str, *a: Any) -> dict[str, Any]:
        if v_name == "PoE2 term":
            raise CrossGameHallucinationError(trigger="x", error="PoE2 term in a PoE1 sweep", remediation="drop it")
        return real(worker, tx, var, v_name, *a)

    monkeypatch.setattr(batch_sweep_mod, "_sweep_variant", _guarded)
    w = _FrameWorker(caps=_CAPS)
    with pytest.raises(CrossGameHallucinationError):
        run_sweep(
            w,
            [
                {"name": "Power", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
                {"name": "PoE2 term", "ops": []},
            ],
        )
    assert w.config_vars["usePowerCharges"] is False
    assert w.frame_dps == w.dps  # the next `pob stats` reads the committed build's numbers


def test_sweep_variant_after_lazy_restore_reads_fresh_calcs() -> None:
    w = _FrameWorker(caps=_CAPS)
    res = run_sweep(
        w,
        [
            {"name": "Power", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
            {"name": "Unchanged", "ops": []},
        ],
        calc_keywords=["Total DPS"],
    )
    power, unchanged = res["variants"]
    assert power["calcs"] != res["baseline"]["calcs"]
    assert unchanged["calcs"] == res["baseline"]["calcs"]
    methods = [m for m, _ in w.calls]
    assert {"rebuild": False} in [{"rebuild": p.get("rebuild")} for m, p in w.calls if m == "cow_restore"]
    assert methods.count("pump") == 1


def test_capture_digest_inside_a_transaction_refreshes_a_lazy_restore() -> None:
    w = _FrameWorker(caps=_CAPS)
    with BuildTransaction(w) as tx:
        w.call("set_config", {"var": "usePowerCharges", "value": True})
        tx.restore(rebuild=False)
        assert tx.stale and w.frame_dps != w.dps
        tx.fresh()
        assert not tx.stale and w.frame_dps == w.dps
        tx.rollback()



def test_exit_skips_the_refresh_frame_when_the_engine_is_down() -> None:
    w = _FrameWorker(caps=_CAPS)
    with BuildTransaction(w) as tx:
        w.call("set_config", {"var": "usePowerCharges", "value": True})
        tx.restore(rebuild=False)
        assert tx.stale
        # A crash that recovery gave up on: a pump would only relaunch it under the lock.
        w.is_running = False
    assert "pump" not in [m for m, _ in w.calls]

# ---------------------------------------------------------------------------
# Bug d: what-if and matrix repair a CoW restore that drifted
# ---------------------------------------------------------------------------


def test_what_if_repairs_a_drifted_cow_restore() -> None:
    w = _FlakyCowWorker(caps=_CAPS)
    orig = w._serialize()
    w.cow_restore_mode = "noop"
    res = run_mutation(w, _set_dps(999.0), apply=False)
    assert w._serialize() == orig
    assert "drifted" in res["restoreFallback"]
    assert [m for m, _ in w.calls].count("import_code") == 1


class _CrashOnRestoreWorker(_CowFakeWorker):
    """Fake whose `cow_restore` crashes the engine; the bridge restarts it on `last_good_code` and raises."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.generation = 1
        self.restore_failed: dict[str, Any] | None = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if method == "cow_restore":
            self.calls.append((method, dict(params or {})))
            self.generation += 1
            self._cow_snaps.clear()
            self._deserialize(str(self.last_good_code))
            raise BridgeError("Headless engine crashed during 'cow_restore': EOF")
        return super().call(method, params, timeout)


def test_restore_that_crashed_the_engine_is_not_imported_again() -> None:
    w = _CrashOnRestoreWorker(caps=_CAPS)
    orig = w._serialize()
    res = run_mutation(w, _set_dps(999.0), apply=False)
    assert w._serialize() == orig
    assert "restart already restored" in res["restoreFallback"]
    assert [m for m, _ in w.calls].count("import_code") == 0

    # A restart that could not restore the build (put aside) still re-imports.
    w.calls.clear()
    w.restore_failed = {"error": "x"}
    res = run_mutation(w, _set_dps(999.0), apply=False)
    assert "re-importing" in res["restoreFallback"]
    assert [m for m, _ in w.calls].count("import_code") == 1


def test_matrix_repairs_a_drifted_cow_restore() -> None:
    w = _FlakyCowWorker(caps=_CAPS)
    orig = w._serialize()
    w.cow_restore_mode = "noop"
    res = run_matrix(w, scenarios=["Original", "Power:usePowerCharges=true"])
    assert w._serialize() == orig
    assert res["restoreCheck"]["verified"] is True
    assert any("drifted" in note for note in res["restoreCheck"]["fallback"])


# ---------------------------------------------------------------------------
# Bug e: one slot save per apply or load
# ---------------------------------------------------------------------------


@pytest.fixture
def saved_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_CowFakeWorker, list[dict[str, Any]]]:
    writes: list[dict[str, Any]] = []
    real_write = history_mod.write_slot_state

    def _count(path: Path, **kw: Any) -> None:
        writes.append(kw)
        real_write(path, **kw)

    monkeypatch.setattr(history_mod, "write_slot_state", _count)
    w = _CowFakeWorker(caps=_CAPS)
    hist = SlotHistory("main", tmp_path)
    hist.committed = w._serialize()  # a build was loaded before
    hist.bind(w)
    return w, writes


@pytest.mark.usefixtures("fake_code_loads")
def test_apply_and_load_each_write_the_slot_once(saved_slot: tuple[_CowFakeWorker, list[dict[str, Any]]]) -> None:
    w, writes = saved_slot
    run_mutation(w, _set_dps(150000.0), apply=True)
    assert len(writes) == 1 and writes[0]["last_good_code"] == w._serialize()
    run_batch(w, [{"op": "config", "set": {"usePowerCharges": True}}], apply=True, no_undo=True)
    assert len(writes) == 2
    run_mutation(w, _set_dps(1.0), apply=False)
    assert len(writes) == 2

    other = _CowFakeWorker(caps=_CAPS)
    other.dps = 77.0
    load_build(w, xml_code=other._serialize())
    assert len(writes) == 3
    assert writes[-1]["undo_stack"] == [] and writes[-1]["last_good_code"] == other._serialize()
    assert [m for m, _ in w.calls].count("export_code") == 3  # one per commit, never one before it


# ---------------------------------------------------------------------------
# BuildTransaction contract
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_error_and_restores_last_good_code() -> None:
    w = _CowFakeWorker(caps={})
    orig = w._serialize()
    with pytest.raises(RuntimeError, match="boom"), BuildTransaction(w, scope=BuildScope.SETS):
        w.dps = 1.0
        w.last_good_code = "SOMETHING_THE_BRIDGE_HEALED_TO"
        raise RuntimeError("boom")
    assert w._serialize() == orig
    assert w.last_good_code == orig
    assert history_of(w).active is None


def test_nested_transaction_checkpoints_the_outer_edits_and_cannot_commit() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    orig = w._serialize()
    with BuildTransaction(w, will_commit=True) as outer:
        w.call("set_config", {"var": "usePowerCharges", "value": True})
        edited = w._serialize()
        with BuildTransaction(w) as inner:
            assert inner.parent is outer and inner.base is not None and inner.base.kind == "cow"
            w.dps = 1.0
            with pytest.raises(RuntimeError, match="nested"):
                inner.commit()
        assert w._serialize() == edited
        outer.commit()
    hist = history_of(w)
    assert hist.committed == edited and hist.undo == [orig] and hist.active is None


def test_apply_commits_set_switching_scope_without_cow() -> None:
    w = _CowFakeWorker(caps=_CAPS)
    res = run_mutation(w, _set_dps(5.0), apply=True, scope=BuildScope.SETS)
    assert res["applied"] is True and res["undoDepth"] == 1
    assert "cow_snapshot" not in [m for m, _ in w.calls]


# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_worker():
    if POB_DIR is None:
        pytest.skip("No runnable PoB engine found")
    worker = BridgeWorker(slot_name="test-build-tx", pob_dir=POB_DIR)
    try:
        worker.start()
        worker.call("add_group", {"label": "Main"})
        worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 1})
        worker.call("import_code", {"code": worker.call("export_code", {})["code"], "mode": "replace"})
        history_of(worker).load_build(worker.call("export_code", {})["code"], None)
        yield worker
    finally:
        worker.stop()


def test_live_sweep_unchanged_variant_reads_base_calcs(live_worker: BridgeWorker) -> None:
    res = run_sweep(
        live_worker,
        [
            {"name": "Power", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
            {"name": "Unchanged", "ops": []},
        ],
        calc_keywords=["Average Hit"],
    )
    assert res["succeeded"] == 2
    assert res["variants"][1]["calcs"] == res["baseline"]["calcs"]
    assert res["restoreCheck"]["verified"] is True


def test_live_failed_undo_import_keeps_history(live_worker: BridgeWorker) -> None:
    hist = history_of(live_worker)
    committed = hist.committed
    run_mutation(live_worker, lambda w: w.call("set_config", {"var": "usePowerCharges", "value": True}), apply=True)
    try:
        good_undo = hist.undo[-1]
        hist.undo[-1] = "not-a-share-code"
        before = (list(hist.undo), list(hist.redo), hist.committed, hist.rev)
        with pytest.raises(BridgeError):
            undo_build(live_worker)
        assert (list(hist.undo), list(hist.redo), hist.committed, hist.rev) == before
        hist.undo[-1] = good_undo
    finally:
        undo_build(live_worker)
        hist.redo.clear()
    assert hist.committed == committed
    assert live_worker.call("export_code", {})["code"] == committed


def _add_minion_main_group(w: BridgeWorker) -> None:
    w.call("add_group", {"label": "Probe minions"})
    w.call("add_gem", {"group": 2, "nameSpec": "Summon Skeletons", "level": 20, "quality": 0, "enabled": True})
    w.call("set_build_field", {"field": "mainSocketGroup", "value": 2})


def test_live_cow_restore_brings_back_the_exact_stat_key_set(live_worker: BridgeWorker) -> None:
    # The verifier compares the union of stat keys, so a restore must not leave keys an edit added or removed.
    before = set(live_worker.call("get_stats", {})["stats"])
    before_direct = set(live_worker.call("direct_calc", {})["stats"])
    sid = live_worker.call("cow_snapshot", {})["snapId"]
    _add_minion_main_group(live_worker)
    assert set(live_worker.call("get_stats", {})["stats"]) != before
    live_worker.call("cow_restore", {"snapId": sid, "drop": True})
    assert set(live_worker.call("get_stats", {})["stats"]) == before
    assert set(live_worker.call("direct_calc", {})["stats"]) == before_direct

    res = run_mutation(live_worker, _add_minion_main_group, apply=False)
    assert "restoreFallback" not in res
