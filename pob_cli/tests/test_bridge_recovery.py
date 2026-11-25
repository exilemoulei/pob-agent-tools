"""BridgeWorker crash and timeout recovery: one bounded owner, no nested boots, bad codes put aside."""

from __future__ import annotations

import io
import json
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import pob_cli.bridge_client as bc
from pob_cli.bridge_client import BridgeError, BridgeRejectedError, BridgeTimeoutError, BridgeWorker
from pob_cli.snapshot import SlotHistory

# A request the fake engine never answers, so the caller hits its RPC timeout.
_Hang = Callable[[str, dict[str, Any]], bool]


class _Engine:
    """Fake engine processes sharing one request log; `hang` decides which requests never get an answer."""

    def __init__(
        self,
        hang: _Hang,
        *,
        max_launches: int = 40,
        errors: dict[str, Any] | None = None,
        reject: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.hang = hang
        self.errors = errors or {}
        self.reject = reject or (lambda _m, _p: None)
        self.launches = 0
        self.max_launches = max_launches
        self.sent: list[tuple[int, str, Any]] = []

    def popen(self, *_a: Any, **_kw: Any) -> Any:
        self.launches += 1
        if self.launches > self.max_launches:
            raise OSError("launch cap reached")
        return _Proc(self, self.launches)


class _Proc:
    def __init__(self, engine: _Engine, launch: int) -> None:
        self.engine = engine
        self.launch = launch
        self.stderr = io.StringIO("")
        self.stdin = self
        self._alive = True
        self._q: queue.Queue[str | None] = queue.Queue()
        self._q.put(json.dumps({"event": "gate_result", "data": {"ok": True}}) + "\n")
        self._q.put(json.dumps({"event": "hello", "data": {"pobVersion": "2.69.0", "buildLoaded": True}}) + "\n")
        self.stdout = iter(self._q.get, None)

    def write(self, data: str) -> int:
        for line in data.splitlines():
            if not line.strip():
                continue
            req = json.loads(line)
            method, params = req["method"], req.get("params") or {}
            self.engine.sent.append((self.launch, method, params.get("code")))
            if self.engine.hang(method, params):
                continue
            error = self.engine.errors.get(method) or self.engine.reject(method, params)
            if error is not None:
                self._q.put(json.dumps({"id": req["id"], "error": error}) + "\n")
                continue
            result = {"caps": {}, "pobVersion": "2.69.0"} if method == "version" else {}
            self._q.put(json.dumps({"id": req["id"], "ok": True, "result": result}) + "\n")
        return len(data)

    def flush(self) -> None:
        pass

    def poll(self) -> int | None:
        return None if self._alive else 0

    def kill(self) -> None:
        if self._alive:
            self._alive = False
            self._q.put(None)

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: _Engine, *, slot: str = "main") -> BridgeWorker:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bc, "build_engine_command", lambda _root: (["fake"], {}))
    monkeypatch.setattr(bc.subprocess, "Popen", engine.popen)
    monkeypatch.setattr(bc, "validate_import_xml_guardrail", lambda _code: None)
    monkeypatch.setattr(bc, "stage_tree_versions_hint", lambda _code, _dir: None)
    monkeypatch.setattr(bc, "check_tree_version_mismatch", lambda *_a: None)
    # Real RPC timeouts are 10-15 s; the fake engine hangs for good, so any short timeout shows the same path.
    orig = BridgeWorker._raw_call_unlocked

    def short(self: BridgeWorker, method: str, params: Any = None, timeout: float = 15.0, **kw: Any) -> Any:
        return orig(self, method, params, timeout=min(timeout, 0.2), **kw)

    monkeypatch.setattr(BridgeWorker, "_raw_call_unlocked", short)
    return BridgeWorker(slot_name=slot, pob_dir=eng, runtime_dir=tmp_path / "rt")


def _imports(engine: _Engine, code: str) -> int:
    return sum(1 for _launch, m, c in engine.sent if m == "import_code" and c == code)


def test_hanging_saved_build_boots_empty_without_nested_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(lambda m, p: m == "import_code" and p.get("code") == "BADCODE")
    w = _worker(tmp_path, monkeypatch, engine)
    w.last_good_code = "BADCODE"

    w.start()

    # One launch for the hung restore, one clean relaunch; before the fix this nested until the cap.
    assert engine.launches == 2
    assert _imports(engine, "BADCODE") == 1
    assert w.is_running
    assert w.last_good_code is None
    assert w.restore_failed is not None
    aside = Path(w.restore_failed["codeFile"])
    assert aside.read_text(encoding="utf-8") == "BADCODE"
    assert engine.sent[-1][1] == "new_build"
    w.stop()


def test_bad_slot_file_is_skipped_on_the_next_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda m, p: m == "import_code" and p.get("code") == "BADCODE")
    w = _worker(tmp_path, monkeypatch, engine)
    SlotHistory("main", w.runtime_dir).load_build("BADCODE", None)
    SlotHistory.load("main", w.runtime_dir).bind(w)
    w.start()
    assert w.restore_failed is not None
    w.stop()

    # A new daemon reads the same slot file: the code that hung is not tried again.
    launches = engine.launches
    w2 = BridgeWorker(slot_name="main", pob_dir=w.pob_root, runtime_dir=tmp_path / "rt")
    SlotHistory.load("main", w2.runtime_dir).bind(w2)
    w2.start()
    assert engine.launches == launches + 1
    assert _imports(engine, "BADCODE") == 1
    assert w2.last_good_code is None
    assert w2.restore_failed is not None and w2.restore_failed.get("skipped") is True
    w2.stop()


def test_rpc_timeout_recovers_once_and_restores_the_last_good_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(lambda m, _p: m == "get_calcs")
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="GOODCODE")
    gen = w.generation

    with pytest.raises(BridgeTimeoutError):
        w.call("get_calcs", {})

    assert w.generation == gen + 1
    assert engine.launches == 2
    assert _imports(engine, "GOODCODE") == 2  # the first boot and the one restart
    assert w.last_good_code == "GOODCODE"
    w.stop()


def test_timed_out_import_restores_the_previous_build_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda m, p: m == "import_code" and p.get("code") == "NEWCODE")
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="GOODCODE")
    engine.sent.clear()

    with pytest.raises(BridgeError):
        w.call("import_code", {"code": "NEWCODE", "mode": "replace"})

    # The restart already re-imported GOODCODE; the import heal must not run a second time.
    assert _imports(engine, "GOODCODE") == 1
    assert engine.launches == 2
    assert w.last_good_code == "GOODCODE"
    w.stop()


def test_recovery_gives_up_after_bounded_tries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda m, _p: m == "get_calcs")
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="")
    engine.hang = lambda m, _p: m in ("get_calcs", "version")

    with pytest.raises(BridgeTimeoutError):
        w.call("get_calcs", {})

    assert engine.launches == 1 + bc._RECOVERY_TRIES
    assert not w.is_running
    w.stop()


def test_generation_counts_every_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda _m, _p: False)
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    assert w.generation == 0
    w.start(restore_code="")
    assert w.generation == 1
    w.stop()
    w.start(restore_code="")
    assert w.generation == 2
    w.stop()


def test_engine_rejection_keeps_the_worker_and_carries_the_lua_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = "\nstack traceback:\n\t[C]: in function 'error'"
    engine = _Engine(
        lambda _m, _p: False,
        errors={
            "import_code": {"code": "lua_error", "message": "not a Path of Building code" + trace},
            "get_calcs": {"code": "lua_error", "message": "bridge.lua:9: attempt to index a nil value" + trace},
        },
    )
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="")
    gen = w.generation

    with pytest.raises(BridgeRejectedError) as bad_input:
        w.call("import_code", {"code": "garbage", "mode": "replace"})
    assert bad_input.value.kind == "request" and bad_input.value.code == "lua_error"
    assert "stack traceback" not in str(bad_input.value)

    with pytest.raises(BridgeRejectedError) as engine_bug:
        w.call("get_calcs", {})
    assert engine_bug.value.kind == "daemon" and "stack traceback" in str(engine_bug.value)

    assert w.generation == gen and w.is_running and engine.launches == 1
    w.stop()


def test_real_engine_rejects_a_bad_share_code_as_a_request_error(tmp_path: Path) -> None:
    from engine_paths import find_runnable_engine

    eng = find_runnable_engine()
    if eng is None:
        pytest.skip("no runnable PoB engine (bootstrap one or set POB_ROOT)")
    w = BridgeWorker(slot_name="test", pob_dir=eng, runtime_dir=tmp_path / "rt")
    try:
        w.start(restore_code="")
        gen = w.generation
        with pytest.raises(BridgeRejectedError) as caught:
            w.call("import_code", {"code": "not-a-build-code", "mode": "replace"})
        assert caught.value.kind == "request"
        assert caught.value.code == "lua_error"
        assert "not a Path of Building code" in str(caught.value)
        assert w.is_running and w.generation == gen
        assert w.call("get_build_info", {}) is not None
    finally:
        w.stop()


_BAD_CODE = {"code": "lua_error", "message": "not a Path of Building code\nstack traceback:\n\t[C]: in function 'error'"}


def _reject_code(code: str) -> Callable[[str, dict[str, Any]], Any]:
    return lambda m, p: _BAD_CODE if m == "import_code" and p.get("code") == code else None


def test_rejected_import_on_a_stopped_worker_still_heals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda _m, _p: False, reject=_reject_code("NEWCODE"))
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="GOODCODE")
    w._kill_unlocked()  # the engine died between calls
    engine.sent.clear()

    with pytest.raises(BridgeRejectedError):
        w.call("import_code", {"code": "NEWCODE", "mode": "replace"})

    # The start before the import restored GOODCODE, but the rejected import (b:Shutdown()) undid it.
    methods = [(m, c) for _l, m, c in engine.sent]
    after = methods[methods.index(("import_code", "NEWCODE")) + 1 :]
    assert after == [("new_build", None), ("import_code", "GOODCODE")]
    assert w.last_good_code == "GOODCODE"
    w.stop()


def test_slow_reimport_during_heal_is_recovered_not_put_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hung = {"n": 0}

    def hang_heal_once(m: str, p: dict[str, Any]) -> bool:
        # The heal's re-import (mode replace) is slow once; the restart's boot restore is fine.
        if m == "import_code" and p.get("code") == "GOODCODE" and p.get("mode") == "replace" and hung["n"] == 0:
            hung["n"] += 1
            return True
        return False

    engine = _Engine(hang_heal_once, reject=_reject_code("NEWCODE"))
    w = _worker(tmp_path, monkeypatch, engine)
    w.start(restore_code="GOODCODE")

    with pytest.raises(BridgeRejectedError):
        w.call("import_code", {"code": "NEWCODE", "mode": "replace"})

    assert w.restore_failed is None
    assert w.last_good_code == "GOODCODE"
    assert not (tmp_path / "rt" / "slot_main.restore_failed.txt").exists()
    assert w.is_running and engine.launches == 2
    w.stop()


def test_tree_version_hint_keeps_the_rejection_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(lambda _m, _p: False, reject=_reject_code("NEWCODE"))
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="")
    hint = "Build requires treeVersion='9_99', which is newer than local engine max TreeData='3_26'."
    checks = {"n": 0}

    def hint_after_the_rpc(*_a: Any) -> str | None:
        checks["n"] += 1
        return hint if checks["n"] > 1 else None

    monkeypatch.setattr(bc, "check_tree_version_mismatch", hint_after_the_rpc)
    with pytest.raises(BridgeRejectedError) as caught:
        w.call("import_code", {"code": "NEWCODE", "mode": "replace"})
    assert caught.value.kind == "request" and hint in str(caught.value)

    # The pre-check of the user's own code is a bad request too, not an engine fault.
    monkeypatch.setattr(bc, "check_tree_version_mismatch", lambda *_a: hint)
    with pytest.raises(BridgeRejectedError) as pre:
        w.call("import_code", {"code": "OTHER", "mode": "replace"})
    assert pre.value.kind == "request" and hint in str(pre.value)
    with pytest.raises(BridgeRejectedError) as pre_batch:
        w.call_batch([("import_code", {"code": "OTHER", "mode": "replace"})])
    assert pre_batch.value.kind == "request"
    w.stop()


def test_batch_rejection_is_classified_through_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(
        lambda _m, _p: False,
        reject=_reject_code("NEWCODE"),
        errors={"get_calcs": {"code": "lua_error", "message": "bridge.lua:9: attempt to index a nil value"}},
    )
    w = _worker(tmp_path, monkeypatch, engine, slot="test")
    w.start(restore_code="")

    with pytest.raises(BridgeRejectedError) as bad_input:
        w.call_batch([("get_build_info", {}), ("import_code", {"code": "NEWCODE", "mode": "replace"})])
    assert bad_input.value.kind == "request" and bad_input.value.code == "lua_error"
    assert "stack traceback" not in str(bad_input.value)

    with pytest.raises(BridgeRejectedError) as engine_bug:
        w.call_batch([("get_calcs", {})])
    assert engine_bug.value.kind == "daemon"

    results = w.call_batch([("import_code", {"code": "NEWCODE", "mode": "replace"})], raise_on_error=False)
    assert results[0]["ok"] is False and "not a Path of Building code" in results[0]["error"]
    w.stop()


def test_a_later_import_does_not_clear_a_put_aside_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A what-if rollback re-imports the empty build while the saved one is still put aside: only a
    # committed new build (SlotHistory) ends the put-aside state.
    engine = _Engine(lambda m, p: m == "import_code" and p.get("code") == "BADCODE")
    w = _worker(tmp_path, monkeypatch, engine)
    w.last_good_code = "BADCODE"
    w.start()
    assert w.restore_failed is not None
    w.call("import_code", {"code": "EMPTYBUILD", "mode": "replace"})
    assert w.restore_failed is not None
    w.stop()
