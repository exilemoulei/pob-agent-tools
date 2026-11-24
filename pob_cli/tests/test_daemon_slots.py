"""Tests for case-insensitive daemon slot normalization and dispatch consistency."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import pob_cli.analyzer as analyzer_mod
import pob_cli.daemon as daemon_mod


class _StubSlotWorker:
    def __init__(
        self,
        slot_name: str = "main",
        pob_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.alive = False
        self.last_used = time.time()
        self.last_good_code: str | None = f"code-{slot_name}"
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.source_label: str | None = None
        self.hello_info: dict[str, Any] = {"pobVersion": "2.68.1"}
        self.rev: int = 0
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

    def bump_rev(self) -> int:
        self.rev += 1
        return self.rev

    def save_slot_state(self) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "export_code":
            return {"code": f"exported-{self.slot_name}"}
        return {"level": 95, "slot": self.slot_name}


def _make_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> daemon_mod.DaemonManager:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubSlotWorker)
    mgr = daemon_mod.DaemonManager(
        pob_dir=eng,
        token="tok",
        runtime_dir=rt,
        offline=True,
        skip_update_check=True,
    )
    mgr.main_worker.start()
    return mgr


def test_daemon_mixed_case_slots_share_worker_lock_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = _make_manager(tmp_path, monkeypatch)

    w_upper = mgr.get_worker("Foo-Bar")
    w_lower = mgr.get_worker("foo-bar")
    assert w_upper is w_lower
    assert w_upper.slot_name == "foo-bar"

    exp_upper = mgr.dispatch("export", {"target": "Foo-Bar"})
    exp_lower = mgr.dispatch("export", {"target": "foo-bar"})
    assert exp_upper["code"] == "exported-foo-bar"
    assert exp_lower["code"] == "exported-foo-bar"

    status = mgr.dispatch("status", {})
    assert "foo-bar" in status["slots"]
    assert "Foo-Bar" not in status["slots"]
    assert set(mgr._slot_locks.keys()) == {"main", "foo-bar"}


def test_daemon_diagnose_normalizes_single_and_dual_slot_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = _make_manager(tmp_path, monkeypatch)
    seen_single: list[str] = []
    seen_dual: list[tuple[str, str, str, str]] = []

    def _fake_diagnose_build(worker: Any, **kwargs: Any) -> dict[str, Any]:
        seen_single.append(str(kwargs.get("target")))
        return {"env": {"slot": kwargs.get("target")}, "workerSlot": worker.slot_name}

    def _fake_diagnose_compare(mw: Any, rw: Any, **kwargs: Any) -> dict[str, Any]:
        seen_dual.append((mw.slot_name, rw.slot_name, str(kwargs.get("target")), str(kwargs.get("ref_target"))))
        return {"target": kwargs.get("target"), "refTarget": kwargs.get("ref_target")}

    monkeypatch.setattr(analyzer_mod, "diagnose_build", _fake_diagnose_build)
    monkeypatch.setattr(analyzer_mod, "diagnose_compare_builds", _fake_diagnose_compare)

    single_res = mgr.dispatch("diagnose", {"target": "Alpha"})
    assert seen_single == ["alpha"]
    assert single_res["env"]["slot"] == "alpha"
    assert single_res["targetSlot"] == "alpha"
    assert single_res["workerSlot"] == "alpha"

    dual_res = mgr.dispatch(
        "diagnose",
        {"target": "Alpha", "ref_target": "Beta", "compare_source": "pob://ref"},
    )
    assert dual_res["target"] == "alpha"
    assert dual_res["refTarget"] == "beta"

    dual_main_default = mgr.dispatch(
        "diagnose",
        {"target": "MAIN", "compare_source": "pob://ref"},
    )
    assert dual_main_default["target"] == "main"
    assert dual_main_default["refTarget"] == "ref"

    dual_same_slot = mgr.dispatch(
        "diagnose",
        {"main_target": "Gamma", "ref_target": "gamma", "compare_source": "pob://ref"},
    )
    assert dual_same_slot["target"] == "gamma"
    assert dual_same_slot["refTarget"] == "gamma"

    assert seen_dual == [
        ("alpha", "beta", "alpha", "beta"),
        ("main", "ref", "main", "ref"),
        ("gamma", "gamma", "gamma", "gamma"),
    ]


def test_cli_slot_slug_types_and_defaults_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.cli_common import _SLOT_SLUG_TYPE, _default_target
    from pob_cli.cli_trade.common import _TARGET_SLOT_TYPE, _default_target_slot

    assert _SLOT_SLUG_TYPE.convert("MAIN", None, None) == "main"
    assert _SLOT_SLUG_TYPE.convert("Foo-Bar.1", None, None) == "foo-bar.1"
    assert _TARGET_SLOT_TYPE.convert("MAIN", None, None) == "main"
    assert _TARGET_SLOT_TYPE.convert("Alpha_2", None, None) == "alpha_2"

    monkeypatch.setenv("POB_SLOT", "  MAIN ")
    assert _default_target() == "main"
    assert _default_target_slot() == "main"

    monkeypatch.setenv("POB_SLOT", "MyBuild")
    assert _default_target() == "mybuild"
    assert _default_target_slot() == "mybuild"
