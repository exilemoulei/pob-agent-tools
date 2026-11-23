"""Protocol, red-line blocklist, color-strip, and codec unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from pob_cli.bridge_client import FORBIDDEN_METHODS, BridgeError, BridgeWorker
from pob_cli.cli import _prepare_load_source
from pob_cli.external_data import _strip_html, query_price
from pob_cli.formatters import fmt_delta, sanitize_for_output, strip_pob_escapes, to_json_str
from pob_cli.importers import POE_NINJA_CHAR_RE, share_code_to_xml, xml_to_share_code


def test_forbidden_methods_blocked() -> None:
    """Verify R1, R3, R4, R7, R11 static blocklist and R2 dynamic caps.sidebarBreakdown gating."""
    worker = BridgeWorker(slot_name="test")
    expected_blocked = [
        "compare_load",
        "compare_state",
        "compare_select",
        "compare_remove",
        "compare_set",
        "compare_tree",
        "compare_items",
        "compare_skills",
        "compare_config",
        "compare_use",
        "config_mod_search",
        "config_mod_add",
        "set_pob_options",
        "oauth_logout",
        "import_from_uri",
        "save_build_as",
    ]
    for method in expected_blocked:
        assert method in FORBIDDEN_METHODS
        with pytest.raises(BridgeError):
            worker.call(method, {})

    # Dynamic R2: sidebar_breakdown is NOT in static FORBIDDEN_METHODS, but raises when caps.sidebarBreakdown is False
    assert "sidebar_breakdown" not in FORBIDDEN_METHODS

    class _FakeProc:
        def poll(self) -> None:
            return None

    worker._proc = _FakeProc()  # type: ignore[assignment]
    worker.caps = {"sidebarBreakdown": False}
    with pytest.raises(BridgeError, match="R2: sidebarBreakdown"):
        worker.call("sidebar_breakdown", {})


def test_color_escape_stripping_and_raw_preservation() -> None:
    """Verify R9: strip ^xRRGGBB and ^0..^9 in display fields while preserving raw/xml/code/notes/text."""
    colored = "^x33FF77+120^7 to ^1Maximum Life^0"
    assert strip_pob_escapes(colored) == "+120 to Maximum Life"

    payload = {
        "title": "^xD02090Foulborn Skin of the Lords^7",
        "raw": "Rarity: UNIQUE\n^xD02090Foulborn Skin of the Lords^7",
        "xml": "<PathOfBuilding>^x123456</PathOfBuilding>",
        "code": "eNrt^7abc",
        "notes": "Keep ^xABCDEF color in notes",
        "text": "Keep ^7notes text",
        "nested": [{"label": "^2Fire Resist^7"}],
    }
    cleaned = sanitize_for_output(payload)
    assert cleaned["title"] == "Foulborn Skin of the Lords"
    assert cleaned["nested"][0]["label"] == "Fire Resist"
    # Raw keys must remain untouched
    assert cleaned["raw"] == payload["raw"]
    assert cleaned["xml"] == payload["xml"]
    assert cleaned["code"] == payload["code"]
    assert cleaned["notes"] == payload["notes"]
    assert cleaned["text"] == payload["text"]

    # External JSON with sanitize=False must preserve math exponents like 10^6
    ext_json = to_json_str({"summary": "10^6 damage"}, sanitize=False)
    assert "10^6 damage" in ext_json


def test_xml_share_code_roundtrip() -> None:
    """Verify zlib + urlsafe base64 share code round-trip."""
    sample_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<PathOfBuilding><Build level="95" className="Witch"/></PathOfBuilding>'
    code = xml_to_share_code(sample_xml)
    restored = share_code_to_xml(code)
    assert restored == sample_xml


def test_utf8_bom_xml_file_routes_to_xml_not_base64(tmp_path: Path) -> None:
    """Verify Windows UTF-8 BOM XML files and raw BOM strings decode cleanly as XML, not Base64."""
    xml_body = '<?xml version="1.0" encoding="UTF-8"?>\n<PathOfBuilding><Build level="99" className="Scion"/></PathOfBuilding>'
    bom_file = tmp_path / "bom_build.xml"
    bom_file.write_bytes(b"\xef\xbb\xbf" + xml_body.encode("utf-8"))

    src1, xml_code1 = _prepare_load_source(str(bom_file))
    assert src1 is None and xml_code1 is not None
    assert share_code_to_xml(xml_code1) == xml_body

    src2, xml_code2 = _prepare_load_source(f"@{bom_file}")
    assert src2 is None and xml_code2 is not None
    assert share_code_to_xml(xml_code2) == xml_body

    src3, xml_code3 = _prepare_load_source("\ufeff" + xml_body)
    assert src3 is None and xml_code3 is not None
    assert share_code_to_xml(xml_code3) == xml_body


def test_poe_ninja_character_url_regex() -> None:
    """Verify poe.ninja character URL parsing."""
    url = "https://poe.ninja/poe1/builds/mirage/character/ExileAcc-1234/MyNecroChar?i=0&timemachine=week-2"
    m = POE_NINJA_CHAR_RE.match(url)
    assert m is not None
    assert m.group("league") == "mirage"
    assert m.group("account") == "ExileAcc-1234"
    assert m.group("name") == "MyNecroChar"


def test_fmt_delta() -> None:
    assert fmt_delta(1000, 1250) == "+250 (+25.0%)"
    assert fmt_delta(1000, 1000) == "0"


def test_mocked_query_price_all_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify P1-7: category='all' searches DivinationCard, ClusterJewel, ForbiddenJewel, and Scarab."""
    fake_overviews = {
        "Currency": [{"name": "Divine Orb", "chaos": 150.0, "count": 5000, "graph": [1.5, 2.4]}],
        "DivinationCard": [{"name": "The Apothecary", "chaos": 6000.0, "count": 42, "graph": [-3.2]}],
        "ClusterJewel": [{"name": "Large Cluster Jewel", "variant": "8 Passive", "chaos": 45.0, "count": 120, "graph": [0.0]}],
    }
    monkeypatch.setattr("pob_cli.external_data._fetch_dense_overviews", lambda league, ttl_sec=900: fake_overviews)
    res = query_price("The Apothecary", league="Mirage", category="all")
    assert res["totalMatches"] == 1
    hit = res["matches"][0]
    assert hit["name"] == "The Apothecary"
    assert hit["type"] == "DivinationCard"
    assert hit["divineValue"] == pytest.approx(40.0)
    assert hit["trend24hPct"] == -3.2


def test_strip_html_wiki() -> None:
    raw = "<div><style>.foo{color:red}</style><p>Mind Over Matter takes <b>40%</b> of Damage from Mana before Life (10^6).</p></div>"
    cleaned = _strip_html(raw)
    assert "Mind Over Matter takes 40% of Damage from Mana before Life (10^6)." in cleaned


def test_bridge_worker_call_batch_single_write_and_state_sync() -> None:
    """Verify BridgeWorker.call_batch pipelines N RPCs in one stdin write+flush, matches by id, and syncs state."""
    import json

    worker = BridgeWorker(slot_name="test")
    writes: list[str] = []
    flushes = 0

    class _FakeStdin:
        def write(self, data: str) -> int:
            writes.append(data)
            return len(data)

        def flush(self) -> None:
            nonlocal flushes
            flushes += 1
            # Parse all sent lines and enqueue responses out-of-order + an interleaved async event
            worker._stdout_queue.put({"event": "heartbeat", "data": {}})
            sent = [json.loads(ln) for ln in writes[-1].splitlines() if ln.strip()]
            for req in reversed(sent):
                method = req["method"]
                if method == "set_translate":
                    enabled_val = bool((req.get("params") or {}).get("enabled", False))
                    if enabled_val:
                        # Legacy key fallback check
                        worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"translate": True}})
                    else:
                        # Real bridge.lua:310 response shape uses "enabled"
                        worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"enabled": False}})
                elif method == "export_code":
                    worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"code": "BATCH_CODE_123"}})
                elif method == "import_code":
                    worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"loaded": True}})
                elif method == "get_stats":
                    worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"count": 42}})
                elif method == "fail_op":
                    worker._stdout_queue.put({"id": req["id"], "ok": False, "error": {"message": "boom"}})
                else:
                    worker._stdout_queue.put({"id": req["id"], "ok": True, "result": {"echo": method}})

    class _FakeProc:
        stdin = _FakeStdin()

        def poll(self) -> None:
            return None

    worker._proc = _FakeProc()  # type: ignore[assignment]
    worker.caps = {"sidebarBreakdown": False}

    results = worker.call_batch(
        [
            ("get_stats", {}),
            ("set_translate", {"enabled": False}),
            ("export_code", {}),
            ("import_code", {"code": "RAW_TEMP_CODE", "mode": "replace"}),
        ]
    )
    assert len(writes) == 1
    assert flushes == 1
    assert len(writes[0].splitlines()) == 4
    assert results == [
        {"count": 42},
        {"enabled": False},
        {"code": "BATCH_CODE_123"},
        {"loaded": True},
    ]
    assert worker.translate_enabled is False
    # P1-3: raw export_code / import_code must NOT implicitly mutate last_good_code
    assert worker.last_good_code is None

    # Fallback: set_translate returning legacy "translate" key still syncs translate_enabled
    res_t = worker.call("set_translate", {"enabled": True})
    assert res_t == {"translate": True}
    assert worker.translate_enabled is True

    # Partial error handling: raise_on_error=False returns structured error entry (with _error) without raising
    partial = worker.call_batch([("get_stats", {}), ("fail_op", {})], raise_on_error=False)
    assert partial[0] == {"count": 42}
    assert partial[1]["ok"] is False
    assert "boom" in partial[1]["error"]
    assert partial[1]["_error"] == partial[1]["error"]

    # Partial error handling: raise_on_error=True raises BridgeError
    with pytest.raises(BridgeError, match="boom"):
        worker.call_batch([("get_stats", {}), ("fail_op", {})], raise_on_error=True)

    # Forbidden methods & dynamic R2 are still enforced inside call_batch
    with pytest.raises(BridgeError, match="R1"):
        worker.call_batch([("compare_load", {})])
    with pytest.raises(BridgeError, match="R2: sidebarBreakdown"):
        worker.call_batch([("sidebar_breakdown", {})])


def test_bridge_worker_start_restore_code_skips_redundant_new_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify BridgeWorker.start(restore_code=...) skips new_build when buildLoaded=True and falls back on error."""
    import io
    import json
    import queue as _queue

    import pob_cli.bridge_client as bc

    fake_root = tmp_path / "eng"
    (fake_root / "PoE1").mkdir(parents=True)
    monkeypatch.setattr(bc, "build_engine_command", lambda _root: (["fake-cmd"], {}))

    sent_methods: list[str] = []

    def _make_fake_popen(fail_import: bool = False):
        class _FakeProc:
            def __init__(self, *_a, **_kw) -> None:
                self.stderr = io.StringIO("")
                self._alive = True
                self.stdin = self
                self._q: _queue.Queue[str | None] = _queue.Queue()
                self._q.put(json.dumps({"event": "gate_result", "data": {"ok": True}}) + "\n")
                self._q.put(
                    json.dumps(
                        {"event": "hello", "data": {"pobVersion": "2.68.1", "bridge": "0.2.0", "buildLoaded": True}}
                    )
                    + "\n"
                )
                self.stdout = self._iter_stdout()

            def _iter_stdout(self):
                while True:
                    item = self._q.get()
                    if item is None:
                        break
                    yield item

            def write(self, data: str) -> int:
                for line in data.splitlines():
                    if not line.strip():
                        continue
                    req = json.loads(line)
                    m = req["method"]
                    sent_methods.append(m)
                    if m == "version":
                        resp = {
                            "id": req["id"],
                            "ok": True,
                            "result": {
                                "pobVersion": "2.68.1",
                                "translate": True,
                                "caps": {"translateToggle": True},
                            },
                        }
                    elif m == "import_code":
                        if fail_import:
                            resp = {"id": req["id"], "ok": False, "error": "corrupt"}
                        else:
                            resp = {"id": req["id"], "ok": True, "result": {"loaded": True}}
                    else:
                        resp = {"id": req["id"], "ok": True, "result": {}}
                    self._q.put(json.dumps(resp) + "\n")
                return len(data)

            def flush(self) -> None:
                pass

            def poll(self) -> int | None:
                return None if self._alive else 0

            def kill(self) -> None:
                self._alive = False
                self._q.put(None)

            def wait(self, timeout: float | None = None) -> int:
                return 0

        def _factory(*args, **kwargs):
            return _FakeProc(*args, **kwargs)

        return _factory

    monkeypatch.setattr(bc.subprocess, "Popen", _make_fake_popen(fail_import=False))
    w = BridgeWorker(slot_name="test", pob_dir=fake_root)
    w.start(restore_code="RESTORED_BUILD_CODE")
    assert sent_methods == ["version", "import_code"]
    assert w.translate_enabled is True
    assert w.last_good_code == "RESTORED_BUILD_CODE"
    w.stop()

    # Fallback: if import_code fails during restore, falls back to new_build and clears last_good_code (P1-3)
    sent_methods.clear()
    monkeypatch.setattr(bc.subprocess, "Popen", _make_fake_popen(fail_import=True))
    w2 = BridgeWorker(slot_name="test", pob_dir=fake_root)
    w2.last_good_code = "STALE_BROKEN_CODE"
    w2.start(restore_code="BROKEN_CODE")
    assert sent_methods == ["version", "import_code", "new_build"]
    assert w2.last_good_code is None
    w2.stop()


def test_daemon_spawn_breakaway_fallback_on_access_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify _spawn_daemon_process retries without CREATE_BREAKAWAY_FROM_JOB on WinError 5."""
    import io

    import pob_cli.daemon as daemon_mod

    if daemon_mod.sys.platform != "win32":
        pytest.skip("Win32 CREATE_BREAKAWAY_FROM_JOB fallback test")

    seen_flags: list[int] = []

    def _fake_popen(cmd, **kwargs):
        flags = int(kwargs.get("creationflags", 0))
        seen_flags.append(flags)
        if flags & daemon_mod._CREATE_BREAKAWAY_FROM_JOB:
            err = OSError("Access is denied")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err
        return object()

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", _fake_popen)
    proc = daemon_mod._spawn_daemon_process(["python", "-m", "pob_cli.daemon"], tmp_path, io.StringIO())
    assert proc is not None
    assert len(seen_flags) == 2
    assert bool(seen_flags[0] & daemon_mod._CREATE_BREAKAWAY_FROM_JOB) is True
    assert bool(seen_flags[1] & daemon_mod._CREATE_BREAKAWAY_FROM_JOB) is False


def test_daemon_pid_liveness_and_fast_stale_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify _is_pid_alive works cross-platform and call_daemon unlinks dead PIDs without TCP connect."""
    import json
    import os
    import time

    import pob_cli.daemon as daemon_mod

    assert daemon_mod._is_pid_alive(os.getpid()) is True
    assert daemon_mod._is_pid_alive(0) is False
    assert daemon_mod._is_pid_alive(-42) is False
    assert daemon_mod._is_pid_alive(999_999_999) is False

    rt = tmp_path / "rt"
    rt.mkdir()
    state_file = rt / "daemon.json"
    state_file.write_text(
        json.dumps({"pid": 999_999_999, "port": 59999, "token": "dead-tok"}),
        encoding="utf-8",
    )

    tcp_called = False

    def _forbid_http(*_a, **_kw):
        nonlocal tcp_called
        tcp_called = True
        raise AssertionError("Should NOT attempt TCP connect for a dead PID!")

    monkeypatch.setattr(daemon_mod, "_http_post_rpc", _forbid_http)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="not running"):
        daemon_mod.call_daemon("status", {}, auto_start=False, runtime_dir=rt)
    elapsed = time.monotonic() - t0
    assert tcp_called is False
    assert not state_file.exists()
    assert elapsed < 0.5


def test_daemon_multi_slot_parallel_locks_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify named multi-slot workers run concurrently under per-slot locks and expose backward-compat status."""
    import threading
    import time

    import pob_cli.daemon as daemon_mod

    assert daemon_mod.normalize_slot_name(None) == "main"
    assert daemon_mod.normalize_slot_name("MAIN") == "main"
    assert daemon_mod.normalize_slot_name("archmage_v2-test.1") == "archmage_v2-test.1"
    assert daemon_mod.normalize_slot_name("Foo") == daemon_mod.normalize_slot_name("foo") == "foo"
    for bad in ("../escape", "bad/slot", "has space", "..", "."):
        with pytest.raises(ValueError):
            daemon_mod.normalize_slot_name(bad)

    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"

    entered_barrier = threading.Barrier(2, timeout=3.0)

    class _ConcurrentFakeWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.alive = False
            self.last_used = time.time()
            self.last_good_code: str | None = f"code-{slot_name}"
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, object] = {"pobVersion": "2.68.1"}
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

        def call(self, method: str, params: dict | None = None) -> dict:
            if method == "export_code":
                # Both named slots must arrive at the barrier concurrently (proving per-slot parallelism!)
                entered_barrier.wait()
                return {"code": f"exported-{self.slot_name}"}
            return {"level": 95, "slot": self.slot_name}

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _ConcurrentFakeWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)
    mgr.main_worker.start()

    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def _run_slot(slot: str) -> None:
        try:
            results[slot] = mgr.dispatch("export", {"target": slot})
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_run_slot, args=("agent-a",))
    t2 = threading.Thread(target=_run_slot, args=("agent-b",))
    t1.start()
    t2.start()
    t1.join(timeout=4.0)
    t2.join(timeout=4.0)

    assert not errors, f"Parallel named slots failed or serialized: {errors}"
    assert results["agent-a"]["code"] == "exported-agent-a"
    assert results["agent-b"]["code"] == "exported-agent-b"

    status = mgr.dispatch("status", {})
    assert "main" in status and "ref" in status and "slots" in status
    assert set(status["slots"].keys()) >= {"main", "agent-a", "agent-b"}
    assert status["slots"]["agent-a"]["alive"] is True

    # Idle recycle removes stale non-main slots while preserving main
    mgr._workers["agent-a"].last_used = time.time() - (daemon_mod.REF_IDLE_TIMEOUT_SEC + 10.0)
    assert mgr.check_idle() is False
    assert "agent-a" not in mgr._workers
    assert "agent-b" in mgr._workers
    assert "main" in mgr._workers


def test_daemon_and_updater_do_not_eagerly_import_httpx() -> None:
    """Verify importing pob_cli.daemon and pob_cli.updater in a fresh interpreter never imports httpx."""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pob_cli.daemon, pob_cli.updater; assert 'httpx' not in sys.modules, 'httpx was eagerly imported!'",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"Eager httpx import regression: {proc.stderr}"


def test_bridge_worker_runtime_dir_slot_state_and_kill_resets_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify BridgeWorker honors explicit runtime_dir for slot_state_path, resets translate_enabled on kill, and checks ver_info.buildLoaded."""
    import io
    import json
    import queue as _queue

    import pob_cli.bridge_client as bc
    import pob_cli.daemon as daemon_mod

    env_rt = tmp_path / "env_rt"
    explicit_rt = tmp_path / "explicit_rt"
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    monkeypatch.setenv("POB_RUNTIME_DIR", str(env_rt))

    w = BridgeWorker(slot_name="alpha", pob_dir=eng, runtime_dir=explicit_rt)
    assert w.slot_state_path == explicit_rt / "slot_alpha.json"
    w.last_good_code = "ALPHA_CODE"
    w.save_slot_state()
    assert (explicit_rt / "slot_alpha.json").is_file()
    assert not (env_rt / "slot_alpha.json").exists()

    # DaemonManager propagates its runtime_dir into created BridgeWorkers
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=explicit_rt, offline=True, skip_update_check=True)
    assert mgr.main_worker.runtime_dir == explicit_rt
    assert mgr.main_worker.slot_state_path == explicit_rt / "slot_main.json"

    # When ver_info reports buildLoaded=False, start(restore_code=...) runs new_build before import_code,
    # and stop()/_kill_unlocked() resets translate_enabled to None.
    monkeypatch.setattr(bc, "build_engine_command", lambda _root: (["fake-cmd"], {}))
    sent_methods: list[str] = []

    class _FakeProc:
        def __init__(self, *_a, **_kw) -> None:
            self.stderr = io.StringIO("")
            self._alive = True
            self.stdin = self
            self._q: _queue.Queue[str | None] = _queue.Queue()
            self._q.put(json.dumps({"event": "gate_result", "data": {"ok": True}}) + "\n")
            self._q.put(json.dumps({"event": "hello", "data": {"pobVersion": "2.68.1", "bridge": "0.2.0"}}) + "\n")
            self.stdout = self._iter_stdout()

        def _iter_stdout(self):
            while True:
                item = self._q.get()
                if item is None:
                    break
                yield item

        def write(self, data: str) -> int:
            for line in data.splitlines():
                if not line.strip():
                    continue
                req = json.loads(line)
                m = req["method"]
                sent_methods.append(m)
                if m == "version":
                    resp = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"pobVersion": "2.68.1", "buildLoaded": False, "translate": True, "caps": {"translateToggle": True}},
                    }
                else:
                    resp = {"id": req["id"], "ok": True, "result": {}}
                self._q.put(json.dumps(resp) + "\n")
            return len(data)

        def flush(self) -> None:
            pass

        def poll(self) -> int | None:
            return None if self._alive else 0

        def kill(self) -> None:
            self._alive = False
            self._q.put(None)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(bc.subprocess, "Popen", _FakeProc)
    w2 = BridgeWorker(slot_name="test", pob_dir=eng, runtime_dir=explicit_rt)
    w2.start(restore_code="CODE_AFTER_NEW_BUILD")
    assert sent_methods == ["version", "new_build", "import_code"]
    assert w2.translate_enabled is True
    w2.stop()
    assert w2.translate_enabled is None


def test_daemon_rw_reentrancy_under_concurrent_reload_and_busy_status(tmp_path: Path) -> None:
    """Verify nested _reader_barrier() never deadlocks when an exclusive writer arrives, and status reports busy=True."""
    import threading
    import time

    import pob_cli.daemon as daemon_mod

    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"

    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    reader_in_outer = threading.Event()
    writer_waiting = threading.Event()
    writer_entered = threading.Event()
    nested_ok = threading.Event()
    errs: list[BaseException] = []

    def _reader() -> None:
        try:
            with mgr._reader_barrier():
                reader_in_outer.set()
                assert writer_waiting.wait(timeout=3.0)
                # Give writer time to set _reloading = True and block on _active_ops > 0
                deadline = time.monotonic() + 2.0
                while not mgr._reloading and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert mgr._reloading is True
                # Nested re-entry on same thread MUST NOT deadlock!
                with mgr._reader_barrier():
                    assert not writer_entered.is_set()
                    nested_ok.set()
        except BaseException as exc:
            errs.append(exc)

    def _writer() -> None:
        try:
            assert reader_in_outer.wait(timeout=3.0)
            writer_waiting.set()
            with mgr._exclusive_barrier():
                writer_entered.set()
        except BaseException as exc:
            errs.append(exc)

    t_r = threading.Thread(target=_reader)
    t_w = threading.Thread(target=_writer)
    t_r.start()
    t_w.start()
    t_r.join(timeout=4.0)
    t_w.join(timeout=4.0)

    assert not errs, f"RW barrier error: {errs}"
    assert nested_ok.is_set()
    assert writer_entered.is_set()
    assert mgr._active_ops == 0
    assert mgr._reader_depths == {}

    # Non-blocking _slot_status_entry returns busy=True without stalling when another thread holds slot_lock
    slot_lock = mgr._get_slot_lock("main")
    locked = threading.Event()
    release_lock = threading.Event()

    class _AliveWorker:
        last_good_code = "CODE"
        source_label = None

        def __init__(self) -> None:
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.hello_info: dict[str, object] = {}

        def is_alive(self) -> bool:
            return True

        def call(self, *_a, **_kw) -> dict:
            raise AssertionError("Should not call get_build_info while slot lock is held!")

    fake_w = _AliveWorker()
    mgr._workers["main"] = fake_w  # type: ignore[assignment]

    def _holder() -> None:
        with slot_lock:
            locked.set()
            release_lock.wait(timeout=3.0)

    th = threading.Thread(target=_holder)
    th.start()
    assert locked.wait(timeout=2.0)
    try:
        entry = mgr._slot_status_entry("main", fake_w)  # type: ignore[arg-type]
        assert entry["alive"] is True
        assert entry.get("busy") is True
        assert entry["buildInfo"] is None
    finally:
        release_lock.set()
        th.join(timeout=2.0)


def test_call_daemon_timeout_preserves_live_daemon_and_403_404_respawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify P1-2 (TimeoutError preserves live daemon.json; HTTP 403/404 unlinks & respawns) and P1-1 warm degraded alert."""
    import json
    import os

    import pob_cli.daemon as daemon_mod

    rt = tmp_path / "rt"
    rt.mkdir()
    state_file = rt / "daemon.json"
    state_file.write_text(
        json.dumps({"pid": os.getpid(), "port": 51234, "token": "live-tok"}),
        encoding="utf-8",
    )

    ensure_calls = 0

    def _forbid_ensure(*_a, **_kw):
        nonlocal ensure_calls
        ensure_calls += 1
        return {"pid": os.getpid(), "port": 51235, "token": "new-tok"}, None

    monkeypatch.setattr(daemon_mod, "_ensure_daemon", _forbid_ensure)
    monkeypatch.setattr(
        daemon_mod,
        "_http_post_rpc",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("RPC timed out")),
    )

    with pytest.raises(TimeoutError, match="RPC timed out"):
        daemon_mod.call_daemon("item_db", {"query": "Mageblood"}, auto_start=True, runtime_dir=rt)

    assert state_file.exists(), "Live daemon.json must NOT be unlinked on TimeoutError"
    assert ensure_calls == 0, "Must NOT spawn duplicate daemon on TimeoutError while PID is alive"

    # HTTP 403 / 404 (reused port / foreign process) MUST unlink stale daemon.json and respawn
    for foreign_status in (403, 404):
        state_file.write_text(
            json.dumps({"pid": os.getpid(), "port": 51234, "token": "stale-tok"}),
            encoding="utf-8",
        )
        calls: list[int] = []

        def _rpc_foreign_then_ok(
            port: int,
            token: str,
            payload: dict,
            _calls: list[int] = calls,
            _status: int = foreign_status,
            **_kw: object,
        ):
            _calls.append(port)
            if port == 51234:
                return _status, {"ok": False, "error": f"HTTP {_status}"}
            return 200, {"ok": True, "result": {"respawned": True}}

        monkeypatch.setattr(daemon_mod, "_http_post_rpc", _rpc_foreign_then_ok)
        res = daemon_mod.call_daemon("summary", {}, auto_start=True, runtime_dir=rt, emit_cold_start_alert=False)
        assert res == {"respawned": True}
        assert calls == [51234, 51235]
        assert not state_file.exists()

    # Warm call_daemon emits Chinese stderr alert once per checkedAt when degraded
    daemon_mod.reset_stderr_alert_flag()
    state_file.write_text(
        json.dumps({"pid": os.getpid(), "port": 51236, "token": "warm-tok"}),
        encoding="utf-8",
    )
    degraded_payload = {
        "updateStatus": {
            "state": "degraded_network_error",
            "localPobVersion": "2.68.1",
            "checkedAt": 1700000001.0,
            "userHint": "GitHub 连接失败，请配置代理",
        },
        "updateWarning": "GitHub 连接失败，请配置代理",
    }
    monkeypatch.setattr(
        daemon_mod,
        "_http_post_rpc",
        lambda *_a, **_kw: (200, {"ok": True, "result": degraded_payload}),
    )
    daemon_mod.call_daemon("status", {}, auto_start=False, runtime_dir=rt, emit_cold_start_alert=True)
    err1 = capsys.readouterr().err
    assert "GitHub 连接失败，请配置代理" in err1

    # Same checkedAt on second warm call does NOT spam stderr
    daemon_mod.call_daemon("status", {}, auto_start=False, runtime_dir=rt, emit_cold_start_alert=True)
    err2 = capsys.readouterr().err
    assert err2 == ""
    daemon_mod.reset_stderr_alert_flag()


def test_daemon_dispatch_zero_coercions_kwargs_and_borrow_slot_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P1-4 (0 is preserved, not coerced via `or`) and P2-1 (kwargs forwarding & ref_slot interpolation)."""
    import pob_cli.comparator as comp_mod
    import pob_cli.daemon as daemon_mod
    import pob_cli.game_data as gd_mod
    import pob_cli.mutators as mut_mod

    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"

    class _StubWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.last_good_code: str | None = None
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, object] = {}
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def ensure_alive(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.alive = False

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    captured: dict[str, dict] = {}
    monkeypatch.setattr(mut_mod, "add_skill_gem", lambda _w, **kw: captured.setdefault("skill_add", kw) or {"ok": True})
    monkeypatch.setattr(mut_mod, "anoint_item", lambda _w, **kw: captured.setdefault("anoint", kw) or {"ok": True})
    monkeypatch.setattr(comp_mod, "rank_nodes", lambda _w, **kw: captured.setdefault("rank_nodes", kw) or {"ok": True})
    monkeypatch.setattr(gd_mod, "query_local_data", lambda **kw: captured.setdefault("gd_single", kw) or {"ok": True})
    monkeypatch.setattr(gd_mod, "query_multi", lambda **kw: captured.setdefault("gd_multi", kw) or {"ok": True})
    monkeypatch.setattr(mut_mod, "add_skill_group", lambda _w, **kw: captured.setdefault("new_group", kw) or {"ok": True})
    monkeypatch.setattr(mut_mod, "replace_skill_group", lambda _w, **kw: captured.setdefault("replace_group", kw) or {"ok": True})

    mgr.dispatch("skill_add", {"gem": "Enlighten", "level": 0, "quality": 0})
    assert captured["skill_add"]["level"] == 0
    assert captured["skill_add"]["quality"] == 0

    mgr.dispatch("item_anoint", {"notable": "Beef", "index": 0})
    assert captured["anoint"]["anoint_index"] == 0

    mgr.dispatch("rank_nodes", {"max_dist": 0, "top": 0})
    assert captured["rank_nodes"]["max_dist"] == 0
    assert captured["rank_nodes"]["top"] == 0

    mgr.dispatch("game_data", {"kind": "gem", "query": "Arc", "limit": 0})
    assert captured["gd_single"]["limit"] == 0
    mgr.dispatch("game_data", {"kind": "gem", "queries": ["Arc", "Spark"], "limit": 0})
    assert captured["gd_multi"]["limit"] == 0

    mgr.dispatch("skill_new_group", {"gems": ["Arc"], "make_main": True})
    assert captured["new_group"]["make_main"] is True

    mgr.dispatch("skill_replace_group", {"group": "main", "gems": ["Arc"], "enabled": False})
    assert captured["replace_group"]["enabled"] is False

    # P2-1: borrow error interpolates actual ref_target slot name
    with pytest.raises(RuntimeError, match=r"No reference build loaded in 'boss_ref' slot.*--target boss_ref"):
        mgr.dispatch("borrow", {"ref_target": "boss_ref", "item_slots": ["Amulet"]})


def test_daemon_shutdown_concurrency_aborts_inflight_and_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P2-2: shutdown_all() aborts in-flight worker before barrier and concurrent callers wait for teardown."""
    import threading
    import time

    import pob_cli.daemon as daemon_mod

    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"

    in_rpc = threading.Event()
    rpc_unblocked = threading.Event()
    stop_finished = threading.Event()

    class _BlockingWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.last_good_code: str | None = None
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, object] = {}
            self.aborted = False

        def is_alive(self) -> bool:
            return not self.aborted

        def ensure_alive(self) -> None:
            pass

        def abort(self) -> None:
            self.aborted = True
            rpc_unblocked.set()

        def stop(self) -> None:
            time.sleep(0.15)
            stop_finished.set()

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _BlockingWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    def _long_reader() -> None:
        with mgr._reader_barrier():
            in_rpc.set()
            rpc_unblocked.wait(timeout=5.0)

    t_reader = threading.Thread(target=_long_reader)
    t_reader.start()
    assert in_rpc.wait(timeout=2.0)

    t_s1 = threading.Thread(target=mgr.shutdown_all)
    t_s2 = threading.Thread(target=mgr.shutdown_all)
    t_s1.start()
    time.sleep(0.03)
    t_s2.start()

    # Second concurrent caller must NOT return before stop_finished is set!
    t_s2.join(timeout=3.0)
    assert not t_s2.is_alive()
    assert stop_finished.is_set(), "Concurrent shutdown_all() returned before teardown completed!"
    t_s1.join(timeout=3.0)
    t_reader.join(timeout=3.0)
    assert rpc_unblocked.is_set()

    # BridgeWorker._recover_after_crash_unlocked skips start() once abort()/stop() marks _closing=True
    bw = BridgeWorker(slot_name="test", pob_dir=eng)
    start_called = False
    monkeypatch.setattr(bw, "start", lambda **_kw: (_ for _ in ()).throw(AssertionError("Should not restart when closing")))
    bw.abort()
    bw._recover_after_crash_unlocked()
    assert start_called is False


def test_daemon_edge_hardening_pid_bounds_http_handler_and_lua_arg_reset(tmp_path: Path) -> None:
    """Verify P2-3 (headless_runner.lua resets arg={}) and P2-4 (PID 32-bit bound, Content-Length 400, BrokenPipe)."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    import pob_cli.daemon as daemon_mod

    # P2-4: Oversized 64-bit PID must return False instead of raising OverflowError on Windows
    assert daemon_mod._is_pid_alive(1 << 40) is False
    assert daemon_mod._is_pid_alive(0x1_0000_0000) is False

    # P2-4: Malformed Content-Length returns HTTP 400 JSON cleanly
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="secret-tok", runtime_dir=tmp_path / "rt", offline=True, skip_update_check=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon_mod.make_handler(mgr))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        conn.request("POST", "/rpc", body=b"{}", headers={"X-PoB-Token": "secret-tok", "Content-Length": "not-an-int"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 400
        assert "Invalid Content-Length" in body
    finally:
        server.shutdown()
        server.server_close()

    # P2-3: headless_runner.lua clears global `arg = {}` before `dofile("Launch.lua")`
    lua_path = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua" / "headless_runner.lua"
    lua_src = lua_path.read_text(encoding="utf-8")
    arg_reset_pos = lua_src.find("arg = {}")
    launch_pos = lua_src.find('dofile("Launch.lua")')
    assert arg_reset_pos != -1 and launch_pos != -1 and arg_reset_pos < launch_pos
