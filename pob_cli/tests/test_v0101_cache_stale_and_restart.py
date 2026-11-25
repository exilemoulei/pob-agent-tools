"""Unit tests for v0.10.1 SlotReadCache stale-tx guard, worker-restart invalidation, and partial compare errors."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import pob_cli.comparator as comparator_mod
import pob_cli.daemon as daemon_mod
from pob_cli.snapshot import BuildTransaction, history_of


class _RestartableSlotWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.alive = True
        self._proc = object()
        self.caps = {"cowSnapshot": True}
        self.generation = 1
        self.restart_on_ensure = False
        self.clear_code_on_restart = False
        self.last_used = time.time()
        self.last_good_code: str | None = f"CODE_{slot_name}"
        self.hello_info: dict[str, Any] = {"pobVersion": "2.69.0"}
        self.locale = "en"
        self.lock = threading.RLock()

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def ensure_alive(self) -> None:
        self.alive = True
        if self.restart_on_ensure:
            self.restart_on_ensure = False
            self.generation += 1
            if self.clear_code_on_restart:
                self.last_good_code = None

    def set_locale(self, lang: str) -> dict[str, Any]:
        self.locale = lang
        return {"ok": True, "lang": lang}

    def call(self, method: str, _params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if method == "cow_snapshot":
            return {"snapId": "cow_1"}
        if method in ("cow_restore", "cow_drop", "pump"):
            return {"ok": True}
        return {"ok": True}


def test_stale_tx_worker_restart_and_partial_compare_bypass_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _RestartableSlotWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    sum_calls = 0
    cmp_calls = 0
    fail_attr = False

    def _fake_summary(w: Any) -> dict[str, Any]:
        nonlocal sum_calls
        sum_calls += 1
        return {"slot": w.slot_name, "seq": sum_calls}

    def _fake_compare(_mw: Any, _rw: Any, **_kw: Any) -> dict[str, Any]:
        nonlocal cmp_calls
        cmp_calls += 1
        out: dict[str, Any] = {"baselines": {"original": []}, "seq": cmp_calls}
        if fail_attr:
            out["powerAttributionError"] = "budget exhausted"
        return out

    monkeypatch.setattr("pob_cli.analyzer.extract_summary", _fake_summary)
    monkeypatch.setattr(comparator_mod, "compare_builds", _fake_compare)

    mw = mgr.get_worker("main")
    rw = mgr.get_worker("ref")
    history_of(mw).load_build("MAIN_1", "main")
    history_of(rw).load_build("REF_1", "ref")

    # 1. Warm single-slot & compare caches at settled rev
    s1 = mgr.dispatch("summary", {"target": "main"})
    s2 = mgr.dispatch("summary", {"target": "main"})
    assert sum_calls == 1 and "cached" not in s1 and s2["cached"] is True

    c1 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    c2 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert cmp_calls == 1 and "cached" not in c1 and c2["cached"] is True

    # 2. Real `BuildTransaction.restore(rebuild=False)` leaves `tx.stale = True` (`history.active` open):
    #    BOTH lookup and store MUST be bypassed on single-slot reads and dual-slot `compare`!
    mgr._read_cache.clear_all()
    with BuildTransaction(mw) as tx:
        tx.restore(rebuild=False)
        assert tx.stale is True
        assert history_of(mw).active is tx

        s_stale_1 = mgr.dispatch("summary", {"target": "main"})
        s_stale_2 = mgr.dispatch("summary", {"target": "main"})
        assert sum_calls == 3 and "cached" not in s_stale_1 and "cached" not in s_stale_2
        assert len(mgr._read_cache._entries) == 0

        c_stale_1 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
        c_stale_2 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
        assert cmp_calls == 3 and "cached" not in c_stale_1 and "cached" not in c_stale_2
        assert len(mgr._read_cache._entries) == 0

    # Also verify `ref` open/stale transaction bypasses compare cache even when `main` is settled
    with BuildTransaction(rw) as rtx:
        rtx.restore(rebuild=False)
        assert rtx.stale is True
        c_ref_stale = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
        assert cmp_calls == 4 and "cached" not in c_ref_stale
        assert len(mgr._read_cache._entries) == 0

    # 3. Outside transaction (settled): transient `powerAttributionError` must NOT be cached indefinitely
    fail_attr = True
    c_err = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert c_err["powerAttributionError"] == "budget exhausted" and "cached" not in c_err
    fail_attr = False
    c_ok = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    c_ok_hit = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert cmp_calls == 6 and "powerAttributionError" not in c_ok and c_ok_hit["cached"] is True

    # 4. Worker crash/restart inside `ensure_alive()` invalidates slot + compare cache (even when rev is unchanged)
    mgr.dispatch("summary", {"target": "main"})
    assert len(mgr._read_cache._entries) == 2
    mw.restart_on_ensure = True  # type: ignore[attr-defined]
    s_restarted = mgr.dispatch("summary", {"target": "main"})
    assert "cached" not in s_restarted
    c_after_restart = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert "cached" not in c_after_restart

    # Failed restore clearing `last_good_code` on `ref` also bypasses compare cache
    rw.restart_on_ensure = True  # type: ignore[attr-defined]
    rw.clear_code_on_restart = True  # type: ignore[attr-defined]
    c_unloaded_1 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    c_unloaded_2 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert "cached" not in c_unloaded_1 and "cached" not in c_unloaded_2
