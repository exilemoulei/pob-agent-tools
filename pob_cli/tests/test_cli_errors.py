"""Daemon errors must surface as clean `Error: ...` messages, never Python tracebacks.

Two kinds are kept apart:
- request rejected by a healthy daemon (DaemonRequestError) -> exit 1 (arg/state error)
- the daemon itself failing (not running, boot failure, timeout, transport error) -> exit 5
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path
from socketserver import TCPServer
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli_trade, daemon, updater
from pob_cli.bridge_protocol import (
    ENGINE_REJECTED_HINT,
    BridgeError,
    BridgeRejectedError,
    BridgeTimeoutError,
    rpc_rejection,
)
from pob_cli.cli import DAEMON_FAILURE_EXIT_CODE, main
from pob_cli.daemon import BootstrapMissingError, DaemonRequestError
from pob_cli.trade_client import TradeClient


def _raising(exc: BaseException):
    def _fake(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        raise exc

    return _fake


def _post_rpc_stdlib(port: int, token: str, body_obj: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    raw_body = json.dumps(body_obj).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
    try:
        conn.request(
            "POST",
            "/rpc",
            body=raw_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw_body)),
                "X-PoB-Token": token,
            },
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload
    finally:
        conn.close()


# --- request rejected by the daemon -> exit 1 ---------------------------------------------


def test_switch_main_loadout_without_synced_loadouts_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon, "call_daemon", _raising(DaemonRequestError("Build has no synced loadouts available."))
    )

    res = CliRunner().invoke(main, ["switch-main", "--target", "ref", "--loadout", "Late Upgrades"])

    assert res.exit_code == 1
    assert "Error: Build has no synced loadouts available." in res.output
    assert "Traceback" not in res.output
    assert "daemon failure" not in res.output


@pytest.mark.parametrize(
    "argv",
    [
        ["summary"],
        ["skills"],
        ["calc", "Ignite"],
        ["switch-main", "--spec", "missing"],
    ],
)
def test_rejected_requests_are_clean_for_any_command(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(
        daemon, "call_daemon", _raising(DaemonRequestError("No build loaded in slot 'main'"))
    )

    res = CliRunner().invoke(main, argv)

    assert res.exit_code == 1, res.output
    assert "Error: No build loaded in slot 'main'" in res.output
    assert "Traceback" not in res.output


def test_empty_rejection_message_names_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "call_daemon", _raising(DaemonRequestError("")))

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error: Daemon rejected 'summary'" in res.output


# --- the daemon itself failed -> exit 5 ----------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Timed out waiting for PoB daemon to start. Check daemon.log."),
        RuntimeError("PoB daemon exited during startup (code 1)."),
        RuntimeError("PoB daemon state file disappeared after startup."),
        TimeoutError("read timed out"),
        ConnectionRefusedError("connection refused"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
    ],
)
def test_daemon_failures_are_distinct_from_rejections(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(daemon, "call_daemon", _raising(exc))

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res.output
    assert "Error: PoB daemon failure during 'summary':" in res.output
    assert str(exc) in res.output
    assert "pob stop" in res.output
    assert "Traceback" not in res.output


def test_bootstrap_missing_still_prints_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon,
        "call_daemon",
        _raising(BootstrapMissingError("engine missing", update_status={"state": "bootstrap_missing"})),
    )

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1
    assert "Error:" not in res.output
    assert "daemon failure" not in res.output


# --- RPC errors carry an explicit request/failure classification ---------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"ok": False, "error": "Build has no synced loadouts available.", "errorKind": "request"},
            DaemonRequestError,
        ),
        (
            {"ok": False, "error": "Headless engine crashed", "type": "BridgeError", "errorKind": "daemon"},
            RuntimeError,
        ),
        (
            {"ok": False, "error": "Unexpected failure", "type": "RuntimeError", "errorKind": "daemon"},
            RuntimeError,
        ),
        ({"ok": False, "error": "Old daemon response"}, RuntimeError),
    ],
)
def test_call_daemon_classifies_rpc_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
    expected: type[Exception],
) -> None:
    monkeypatch.setattr(
        daemon,
        "read_daemon_state",
        lambda *a, **k: {"pid": os.getpid(), "port": 1, "token": "t"},
    )
    monkeypatch.setattr(
        daemon,
        "_http_post_rpc",
        lambda *a, **k: (400, payload),
    )

    with pytest.raises(expected, match=payload["error"]) as caught:
        daemon.call_daemon("switch_main", {"loadout": "x"}, auto_start=False, runtime_dir=tmp_path)

    assert isinstance(caught.value, DaemonRequestError) == (expected is DaemonRequestError)
    assert issubclass(DaemonRequestError, RuntimeError)  # `pob update` / `pob stop` still catch it


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (ValueError("Unknown spec"), "request"),
        (DaemonRequestError("Undo stack is empty."), "request"),
        (daemon.BridgeError("Headless engine crashed"), "daemon"),
        (RuntimeError("Unexpected failure"), "daemon"),
    ],
)
def test_rpc_handler_marks_error_kind(failure: Exception, kind: str) -> None:
    class FailingManager:
        token = "test-token"

        def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            raise failure

    server = TCPServer(("127.0.0.1", 0), daemon.make_handler(FailingManager()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_rpc_stdlib(
            server.server_address[1],
            "test-token",
            {"action": "summary", "params": {}},
        )
        assert status == 400
        assert body["errorKind"] == kind
        assert body["error"] == str(failure)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- engine rejections: a healthy engine refusing bad input exits 1, faults exit 5 ----------

_LUA_TRACE = "\nstack traceback:\n\t[C]: in function 'error'\n\tbridge.lua:2046: in function 'decode_share_code'"


@pytest.mark.parametrize(
    ("err_obj", "kind", "shown"),
    [
        ({"code": "lua_error", "message": "not a Path of Building code" + _LUA_TRACE}, "request", None),
        ({"code": "lua_error", "message": "empty code" + _LUA_TRACE}, "request", None),
        ({"code": "lua_error", "message": "code did not decode to a build: bad tag" + _LUA_TRACE}, "request", None),
        ({"code": "bad_request", "message": "missing method"}, "request", None),
        # lua_error is also a PoB bug or missing data, so anything not in the table stays a fault.
        ({"code": "lua_error", "message": "bridge.lua:12: attempt to index a nil value" + _LUA_TRACE}, "daemon", "stack"),
        ({"code": "lua_error", "message": "unknown method: frobnicate" + _LUA_TRACE}, "daemon", "stack"),
        ({"code": "no_bridge", "message": "bridge.lua did not register a dispatcher"}, "daemon", None),
        ({"code": "bad_json", "message": "invalid JSON"}, "daemon", None),
        ("corrupt", "daemon", None),  # legacy pob-zh.exe hosts answer with a bare string
    ],
)
def test_engine_error_kinds_table(err_obj: Any, kind: str, shown: str | None) -> None:
    exc = rpc_rejection("import_code", err_obj)
    assert isinstance(exc, BridgeRejectedError) and isinstance(exc, BridgeError)
    assert exc.kind == kind
    assert exc.code == (err_obj.get("code") if isinstance(err_obj, dict) else None)
    assert str(exc).startswith("RPC 'import_code' failed: ")
    if kind == "request":
        assert "stack traceback" not in str(exc)
    if shown == "stack":
        assert "stack traceback" in str(exc)


@pytest.mark.parametrize(
    ("failure", "kind", "code"),
    [
        (BridgeRejectedError("RPC 'import_code' failed: empty code", code="lua_error", kind="request"), "request", "lua_error"),
        (BridgeRejectedError("RPC 'get_calcs' failed: boom", code="lua_error"), "daemon", "lua_error"),
        (BridgeTimeoutError("RPC 'get_calcs' timed out after 15.0s"), "daemon", None),
    ],
)
def test_rpc_handler_maps_engine_rejections(failure: Exception, kind: str, code: str | None) -> None:
    class FailingManager:
        token = "test-token"

        def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            raise failure

    server = TCPServer(("127.0.0.1", 0), daemon.make_handler(FailingManager()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_rpc_stdlib(server.server_address[1], "test-token", {"action": "load", "params": {}})
        assert status == 400
        assert body["errorKind"] == kind
        assert body["type"] == type(failure).__name__
        assert body.get("engineCode") == code
        assert body["error"].startswith(str(failure))
        assert (ENGINE_REJECTED_HINT in body["error"]) == (kind == "request")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_engine_rejected_input_exits_1_with_an_input_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {
        "ok": False,
        "error": f"RPC 'import_code' failed: not a Path of Building code\n{ENGINE_REJECTED_HINT}",
        "type": "BridgeRejectedError",
        "errorKind": "request",
        "engineCode": "lua_error",
    }
    monkeypatch.setattr(daemon, "read_daemon_state", lambda *a, **k: {"pid": os.getpid(), "port": 1, "token": "t"})
    monkeypatch.setattr(daemon, "_http_post_rpc", lambda *a, **k: (400, payload))
    monkeypatch.setattr(updater, "load_update_state", lambda *a, **k: {})

    res = CliRunner().invoke(main, ["summary"])

    assert res.exit_code == 1, res.output
    assert "not a Path of Building code" in res.output
    assert ENGINE_REJECTED_HINT in res.output
    assert "Run `pob stop` and retry" not in res.output
    assert "Traceback" not in res.output


# --- cmd_load / cmd_update / cmd_stop / batch item_edit / trade --pob on -------------------


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("Headless engine crashed"),
        TimeoutError("read timed out"),
        http.client.RemoteDisconnected("Remote end closed connection"),
    ],
)
def test_cmd_load_preserves_daemon_failure_exit_5(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(daemon, "call_daemon", _raising(exc))
    monkeypatch.setattr(updater, "load_update_state", lambda *a, **k: {})

    res = CliRunner().invoke(main, ["load", "eNrtFakeBuildCode"])

    assert res.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res.output
    assert "Error: PoB daemon failure during 'load':" in res.output
    assert str(exc) in res.output
    assert "pob stop" in res.output
    assert "Traceback" not in res.output


def test_cmd_load_request_rejection_exits_1_and_emits_degraded_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(daemon, "call_daemon", _raising(DaemonRequestError("Invalid share code")))
    monkeypatch.setattr(
        updater,
        "load_update_state",
        lambda *a, **k: {"state": "degraded_network_error", "userHint": "Set proxy"},
    )
    monkeypatch.setattr(daemon, "emit_stderr_update_alert", lambda state, **_: emitted.append(state))

    res = CliRunner().invoke(main, ["load", "eNrtFakeBuildCode"])

    assert res.exit_code == 1, res.output
    assert "Error: Invalid share code" in res.output
    assert "daemon failure" not in res.output
    assert "Traceback" not in res.output
    assert len(emitted) == 1 and emitted[0]["state"] == "degraded_network_error"


def test_cmd_update_handles_daemon_errors_and_not_running_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daemon,
        "read_daemon_state",
        lambda *a, **k: {"pid": os.getpid(), "port": 1, "token": "t"},
    )

    # 1. "not running" falls back cleanly to local check_and_apply_updates
    fallback_called: list[bool] = []

    def fake_local_update(**_: Any) -> dict[str, Any]:
        fallback_called.append(True)
        return {
            "state": "up_to_date",
            "localPobVersion": "2.68.0",
            "remotePobVersion": "2.68.0",
            "updateAvailable": False,
        }

    monkeypatch.setattr(daemon, "call_daemon", _raising(RuntimeError("PoB daemon is not running.")))
    monkeypatch.setattr(updater, "check_and_apply_updates", fake_local_update)

    res_fb = CliRunner().invoke(main, ["update", "--check", "--json"])
    assert res_fb.exit_code == 0, res_fb.output
    assert fallback_called == [True]
    assert json.loads(res_fb.output)["state"] == "up_to_date"

    # 2. DaemonRequestError -> exit 1 without falling back or printing Traceback
    fallback_called.clear()
    monkeypatch.setattr(daemon, "call_daemon", _raising(DaemonRequestError("Invalid proxy URL")))
    res_req = CliRunner().invoke(main, ["update", "--check"])
    assert res_req.exit_code == 1, res_req.output
    assert "Error: Invalid proxy URL" in res_req.output
    assert "Traceback" not in res_req.output
    assert fallback_called == []

    # 3. Daemon/transport failure -> exit 5 without Traceback
    for exc in (
        RuntimeError("Hot-reload worker crashed"),
        TimeoutError("update RPC timed out"),
        http.client.RemoteDisconnected("closed"),
    ):
        monkeypatch.setattr(daemon, "call_daemon", _raising(exc))
        res_fail = CliRunner().invoke(main, ["update", "--check"])
        assert res_fail.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res_fail.output
        assert str(exc) in res_fail.output
        assert "Traceback" not in res_fail.output
        assert fallback_called == []


def test_cmd_stop_handles_daemon_errors_and_not_running_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. "not running" preserves clean exit 0 fallback
    monkeypatch.setattr(daemon, "call_daemon", _raising(RuntimeError("PoB daemon is not running.")))
    res_ok = CliRunner().invoke(main, ["stop", "--json"])
    assert res_ok.exit_code == 0, res_ok.output
    payload = json.loads(res_ok.output)
    assert payload["stopped"] is True
    assert "not running" in payload.get("note", "").lower()

    # 2. Wedged live daemon / transport errors -> clean exit 5 without Traceback
    for exc in (
        RuntimeError("Daemon stop error: worker deadlock"),
        TimeoutError("stop RPC timed out"),
        http.client.RemoteDisconnected("closed"),
    ):
        monkeypatch.setattr(daemon, "call_daemon", _raising(exc))
        res_err = CliRunner().invoke(main, ["stop"])
        assert res_err.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res_err.output
        assert str(exc) in res_err.output
        assert "Traceback" not in res_err.output


def test_batch_item_edit_empty_slot_returns_request_error_kind_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class EmptySlotWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.last_good_code = "SNAP_1"
            self.slot_name = "main"

        def ensure_alive(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "export_code":
                return {"code": "SNAP_1"}
            if method == "import_code":
                return {}
            if method == "list_items":
                return {
                    "slots": [{"name": "Ring 1", "selItemId": None, "isFlask": False}],
                    "items": [],
                }
            if method == "get_stats":
                return {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0, "Life": 4000.0}}
            if method == "get_sidebar":
                return {"warnings": []}
            if method in ("get_build_info", "get_build_header"):
                return {}
            raise AssertionError(f"unexpected call {method}")

    manager = daemon.DaemonManager(
        pob_dir=tmp_path,
        token="test-token",
        runtime_dir=tmp_path,
        skip_update_check=True,
    )
    manager._workers["main"] = EmptySlotWorker()  # type: ignore[assignment]

    server = TCPServer(("127.0.0.1", 0), daemon.make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        ops = [{"op": "item_edit", "slot": "Ring 1", "addMods": ["+10 to maximum Life"]}]
        status, body = _post_rpc_stdlib(
            port,
            "test-token",
            {"action": "batch", "params": {"ops": ops}},
        )
        assert status == 400
        assert body["ok"] is False
        assert body["errorKind"] == "request"
        assert body["type"] == "DaemonRequestError"
        assert "has no equipped item to edit" in body["error"]

        # End-to-end through CLI + call_daemon against this live RPC handler -> exit 1 (not 5)
        monkeypatch.setattr(
            daemon,
            "read_daemon_state",
            lambda *a, **k: {"pid": os.getpid(), "port": port, "token": "test-token"},
        )
        res = CliRunner().invoke(main, ["batch", "--json-ops", json.dumps(ops)])
        assert res.exit_code == 1, res.output
        assert "has no equipped item to edit" in res.output
        assert "daemon failure" not in res.output
        assert "Traceback" not in res.output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "exc",
    [
        DaemonRequestError("No build loaded in slot 'main'"),
        RuntimeError("daemon offline"),
    ],
)
def test_trade_search_pob_on_exits_6_on_daemon_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    sample_stats = [
        {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
        {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    ]
    sample_row = {
        "id": "row_cold",
        "item": {
            "name": "Rift Claw",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 82,
            "explicitMods": [
                {"description": "+35% to Cold Resistance", "flags": {}},
                {"description": "+65 to maximum Life", "flags": {}},
            ],
        },
        "listing": {"price": {"amount": 8, "currency": "chaos"}},
    }

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [sample_row]

    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *a, **k: (sample_stats, "mock_local"))
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *a, **k: "Covens")
    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    monkeypatch.setattr(daemon, "call_daemon", _raising(exc))
    monkeypatch.setattr(cli_trade, "call_daemon", _raising(exc))

    res = CliRunner().invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=30",
            "--no-compare",
            "--pob",
            "on",
        ],
    )

    assert res.exit_code == 6, res.output
    assert "PoB verification failed" in res.output
    assert "Traceback" not in res.output


# --- Wave 4 fixes: config count bools, sweep restoreCheck, corrupt daemon.json, load errors, trade bootstrap ---


def test_config_bool_on_count_var_and_vaal_support_guard() -> None:
    from pob_cli.mutators import _apply_config_in_place, _BatchCache, _is_vaal_gem_entry

    assert _is_vaal_gem_entry({"name": "Vaal Sacrifice", "support": True, "skillId": "SupportVaalSacrifice"}) is False
    assert _is_vaal_gem_entry({"name": "Vaal Temptation", "support": True}) is False
    assert _is_vaal_gem_entry({"nameZh": "强辅·瓦尔迅捷", "support": False}) is True
    assert _is_vaal_gem_entry({"name": "Vaal Haste", "support": False}) is True

    class _CountCfgWorker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.boss_list_reads = 0

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "list_config":
                self.boss_list_reads += 1
                items: list[dict[str, Any]] = [
                    {"var": "conditionStationary", "type": "count", "value": None},
                    {"var": "enemyPhysicalHitDamage", "type": "integer", "value": None},
                    {"var": "customFloatVar", "type": "float", "value": None},
                    {"var": "usePowerCharges", "type": "check", "value": False},
                    {
                        "var": "enemyIsBoss",
                        "type": "list",
                        "value": "None",
                        "list": [{"val": "None", "label": "No"}, {"val": "Pinnacle", "label": "Pinnacle"}],
                    },
                ]
                if self.boss_list_reads >= 2:
                    items.append({"var": "postBossVar", "type": "count", "value": None})
                return {"sections": [{"items": items}]}
            if method in ("set_config", "reset_config"):
                return {"ok": True}
            raise AssertionError(f"Unexpected call {method}")

    w = _CountCfgWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]
    _apply_config_in_place(
        w,  # type: ignore[arg-type]
        [
            ("conditionStationary", "true"),
            ("conditionStationary", "false"),
            ("conditionStationary", True),
            ("conditionStationary", False),
            ("conditionStationary", "5"),
            ("enemyPhysicalHitDamage", "true"),
            ("customFloatVar", "false"),
            ("usePowerCharges", "true"),
            ("usePowerCharges", "false"),
            ("enemyIsBoss", "Pinnacle"),
        ],
        cache=cache,
    )
    edits = [(m, p) for m, p in w.calls if m != "list_config"]
    assert edits == [
        ("set_config", {"var": "conditionStationary", "value": 1}),
        ("reset_config", {"var": "conditionStationary"}),
        ("set_config", {"var": "conditionStationary", "value": 1}),
        ("reset_config", {"var": "conditionStationary"}),
        ("set_config", {"var": "conditionStationary", "value": 5}),
        ("set_config", {"var": "enemyPhysicalHitDamage", "value": 1}),
        ("reset_config", {"var": "customFloatVar"}),
        ("set_config", {"var": "usePowerCharges", "value": True}),
        ("set_config", {"var": "usePowerCharges", "value": False}),
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
    ]
    # touched_boss refreshed cache.known_config_items via _index_config_vars
    assert cache.known_config_items is not None
    assert "postbossvar" in cache.known_config_items


def test_run_sweep_computes_and_returns_restore_check() -> None:
    from pob_cli.mutators import run_sweep

    class _SweepWorker:
        def __init__(self, *, break_restore: bool = False) -> None:
            self.lock = threading.RLock()
            self.last_good_code = "CODE_100000"  # the committed build is the live one
            self.dps = 100_000.0
            self.break_restore = break_restore
            self.export_calls_order: list[str] = []
            self.last_good_during_verify: str | None = None

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            p = params or {}
            if method == "export_code":
                self.export_calls_order.append("export")
                if self.break_restore and len(self.export_calls_order) >= 2:
                    return {"code": "DRIFTED_CODE"}
                return {"code": f"CODE_{int(self.dps)}"}
            if method == "import_code":
                self.export_calls_order.append("import")
                if self.break_restore:
                    self.dps = 999_999.0
                else:
                    self.dps = float(str(p["code"]).split("_")[1])
                self.last_good_code = p["code"]
                return {"ok": True}
            if method == "list_config":
                return {"sections": [{"items": [{"var": "usePowerCharges", "type": "check", "value": False}]}]}
            if method == "set_config":
                if p.get("var") == "usePowerCharges" and p.get("value"):
                    self.dps += 20_000.0
                return {"ok": True}
            if method == "get_stats":
                return {"stats": {"CombinedDPS": self.dps, "TotalEHP": 50_000.0, "Life": 4000.0}}
            if method == "get_sidebar":
                return {"warnings": [], "rows": []}
            if method in ("get_build_info", "get_build_header"):
                return {}
            raise AssertionError(f"Unexpected call {method}")

    # 1. Mutated sweep verifies restore and preserves prev last_good_code
    w_ok = _SweepWorker()
    res_ok = run_sweep(
        w_ok,  # type: ignore[arg-type]
        [{"name": "Charges", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]}],
        base_ops=[{"op": "config", "set": {"usePowerCharges": True}}],
    )
    assert res_ok["restoreCheck"]["verified"] is True
    assert w_ok.last_good_code == "CODE_100000"

    # 2. Failed rollback surfaces restoreCheck.verified == False with differing stats
    w_bad = _SweepWorker(break_restore=True)
    res_bad = run_sweep(
        w_bad,  # type: ignore[arg-type]
        [{"name": "Noop", "ops": []}],
    )
    assert res_bad["restoreCheck"]["verified"] is False
    assert "CombinedDPS" in res_bad["restoreCheck"].get("differing", [])
    assert w_bad.last_good_code == "CODE_100000"


@pytest.mark.parametrize(
    "corrupt_state",
    [
        {"pid": os.getpid()},
        {"pid": os.getpid(), "port": None, "token": "t"},
        {"pid": os.getpid(), "port": "bad", "token": "t"},
        {"pid": os.getpid(), "port": 12345},
    ],
)
def test_corrupt_daemon_json_unlinks_self_heals_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corrupt_state: dict[str, Any],
) -> None:
    state_file = tmp_path / "daemon.json"

    # 1. _probe_daemon returns None without leaking KeyError/TypeError/ValueError
    assert daemon._probe_daemon(corrupt_state) is None

    # 2. stop_daemon unlinks corrupt daemon.json and returns cleanly
    state_file.write_text(json.dumps(corrupt_state), encoding="utf-8")
    stop_res = daemon.stop_daemon(runtime_dir=tmp_path)
    assert stop_res == {"stopped": False, "staleCleaned": True}
    assert not state_file.exists()

    # 3. call_daemon(..., auto_start=False) unlinks corrupt daemon.json and raises clean RuntimeError
    state_file.write_text(json.dumps(corrupt_state), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PoB daemon is not running"):
        daemon.call_daemon("status", auto_start=False, runtime_dir=tmp_path)
    assert not state_file.exists()

    # 4. call_daemon(..., auto_start=True) unlinks corrupt daemon.json and self-heals via _ensure_daemon
    state_file.write_text(json.dumps(corrupt_state), encoding="utf-8")
    ensure_called: list[bool] = []

    def _fake_ensure(rt_dir: Path, **_: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        ensure_called.append(True)
        assert not (rt_dir / "daemon.json").exists()
        return {"pid": os.getpid(), "port": 12345, "token": "fresh"}, {"pid": os.getpid(), "ok": True}

    monkeypatch.setattr(daemon, "_ensure_daemon", _fake_ensure)
    res = daemon.call_daemon("status", auto_start=True, runtime_dir=tmp_path, emit_cold_start_alert=False)
    assert ensure_called == [True]
    assert res["ok"] is True


def test_malformed_rpc_result_and_cmd_stop_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Malformed daemon RPC response: missing 'result'"):
        daemon._rpc_result({"ok": True})

    monkeypatch.setattr(daemon, "call_daemon", _raising(ValueError("invalid literal for int()")))
    res = CliRunner().invoke(main, ["stop"])
    assert res.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res.output
    assert "Error: PoB daemon failure during 'stop':" in res.output
    assert "Traceback" not in res.output


def test_pob_load_missing_at_file_and_invalid_share_code_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pob_cli.importers import xml_to_share_code

    # 1. Missing @file exits 1 with clean Error (never reaches daemon)
    res_missing = CliRunner().invoke(main, ["load", "@nonexistent_build_file_xyz.xml"])
    assert res_missing.exit_code == 1, res_missing.output
    assert "Error: Build file not found: nonexistent_build_file_xyz.xml" in res_missing.output
    assert "Traceback" not in res_missing.output
    assert "daemon failure" not in res_missing.output

    # 2. Invalid raw share code exits 1 (errorKind="request"), whereas BridgeError on valid share code exits 5
    class _CrashImportWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.last_good_code = None
            self.slot_name = "main"

        def ensure_alive(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "import_code":
                raise daemon.BridgeError("Engine failed to parse treeVersion 3_99")
            raise AssertionError(f"Unexpected call {method}")

    manager = daemon.DaemonManager(
        pob_dir=tmp_path,
        token="test-token",
        runtime_dir=tmp_path,
        skip_update_check=True,
    )
    manager._workers["main"] = _CrashImportWorker()  # type: ignore[assignment]

    server = TCPServer(("127.0.0.1", 0), daemon.make_handler(manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(
            daemon,
            "read_daemon_state",
            lambda *a, **k: {"pid": os.getpid(), "port": port, "token": "test-token"},
        )
        monkeypatch.setattr(updater, "load_update_state", lambda *a, **k: {})

        # Malformed share code -> request error -> exit 1
        res_bad = CliRunner().invoke(main, ["load", "not-a-valid-share-code"])
        assert res_bad.exit_code == 1, res_bad.output
        assert "Error: Invalid PoB share code:" in res_bad.output
        assert "Traceback" not in res_bad.output
        assert "daemon failure" not in res_bad.output

        # Valid share code that triggers BridgeError in import_code -> daemon failure -> exit 5
        valid_code = xml_to_share_code("<PathOfBuilding/>")
        res_crash = CliRunner().invoke(main, ["load", valid_code])
        assert res_crash.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res_crash.output
        assert "Error: PoB daemon failure during 'load':" in res_crash.output
        assert "treeVersion 3_99" in res_crash.output
        assert "Traceback" not in res_crash.output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_verify_candidates_batch_propagates_bootstrap_missing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.trade_eval import Candidate, verify_candidates_batch
    from pob_cli.trade_item import parse_item_text

    raw_gloves = "Rarity: RARE\nRift Claw\nSlink Gloves\nImplicits: 0\n+35% to Cold Resistance\n+65 to maximum Life\n"
    cand = Candidate(raw={}, item=parse_item_text(raw_gloves), item_text=raw_gloves)

    # 1. Direct propagation from verify_candidates_batch
    with pytest.raises(BootstrapMissingError):
        verify_candidates_batch(
            [cand],
            slot="Gloves",
            daemon_call=_raising(
                BootstrapMissingError("engine missing", update_status={"state": "bootstrap_missing"})
            ),
        )

    # 2. End-to-end CLI `pob trade search` emits bootstrap guidance instead of swallowing it
    sample_stats = [
        {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
    ]
    sample_row = {
        "id": "row_cold",
        "item": {
            "name": "Rift Claw",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 82,
            "explicitMods": [{"description": "+35% to Cold Resistance", "flags": {}}],
        },
        "listing": {"price": {"amount": 8, "currency": "chaos"}},
    }

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [sample_row]

    boot_exc = BootstrapMissingError("engine missing", update_status={"state": "bootstrap_missing"})
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *a, **k: (sample_stats, "mock_local"))
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *a, **k: "Covens")
    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    monkeypatch.setattr(daemon, "call_daemon", _raising(boot_exc))
    monkeypatch.setattr(cli_trade, "call_daemon", _raising(boot_exc))

    res_on = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "on"],
    )
    assert res_on.exit_code == 6, res_on.output
    assert "bootstrap" in res_on.output.lower()
    assert "Traceback" not in res_on.output

    res_auto = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "auto"],
    )
    assert res_auto.exit_code == 0, res_auto.output
    assert "bootstrap" in res_auto.output.lower()
    assert "Traceback" not in res_auto.output


def test_mutators_validation_and_serve_bootstrap_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.mutators import _apply_class_ascendancy_in_place, _BatchCache, _execute_batch_ops

    class _MiniWorker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [{"index": 1, "label": "Main", "gems": []}],
                }
            if method == "gem_search":
                return {"gems": [{"name": "Fireball", "gemId": "Gem/Fireball"}]}
            if method == "add_gem":
                return {"gem": {"index": 1, "name": "Fireball"}}
            if method == "list_classes":
                return {
                    "current": {"classId": 999, "className": "Unknown"},
                    "classes": [
                        {
                            "id": 3,
                            "name": "Witch",
                            "ascendancies": [{"id": 2, "name": "Elementalist"}],
                        }
                    ],
                    "secondary": [],
                }
            if method in ("set_class", "set_ascendancy"):
                return {"ok": True}
            raise AssertionError(f"Unexpected call {method}")

    w = _MiniWorker()
    # Non-dict "set" in batch config raises ValueError (not AttributeError)
    with pytest.raises(ValueError, match="expects 'set' to be a JSON object"):
        _execute_batch_ops(w, [{"op": "config", "set": ["bad"]}], _BatchCache(w))  # type: ignore[arg-type]

    # Null level/quality/count in batch addGem falls back to defaults instead of TypeError
    _execute_batch_ops(
        w,  # type: ignore[arg-type]
        [{"op": "skill", "group": "main", "addGem": "Fireball", "level": None, "quality": None, "count": None}],
        _BatchCache(w),  # type: ignore[arg-type]
    )
    add_gem_params = next(p for m, p in w.calls if m == "add_gem")
    assert add_gem_params["level"] == 20
    assert add_gem_params["quality"] == 20
    assert add_gem_params["count"] == 1

    # Unknown current classId falls back to cross-class ascendancy resolution (no AssertionError)
    cls_res = _apply_class_ascendancy_in_place(w, ascendancy_name="Elementalist")  # type: ignore[arg-type]
    assert any("Elementalist" in c for c in cls_res["changes"])

    # cmd_serve catches BootstrapMissingError (exit 1) and OSError (exit 5)
    monkeypatch.setattr(
        daemon,
        "run_daemon_forever",
        lambda **_: (_ for _ in ()).throw(
            BootstrapMissingError("missing engine", update_status={"state": "bootstrap_missing"})
        ),
    )
    res_serve_boot = CliRunner().invoke(main, ["serve"])
    assert res_serve_boot.exit_code == 1, res_serve_boot.output
    assert "bootstrap" in res_serve_boot.output.lower()
    assert "Traceback" not in res_serve_boot.output

    monkeypatch.setattr(
        daemon,
        "run_daemon_forever",
        lambda **_: (_ for _ in ()).throw(OSError("Address already in use")),
    )
    res_serve_os = CliRunner().invoke(main, ["serve"])
    assert res_serve_os.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res_serve_os.output
    assert "Error: PoB daemon failed to start: Address already in use" in res_serve_os.output
    assert "Traceback" not in res_serve_os.output

    # cmd_bootstrap catches UpdateError (exit 1) and EngineGateError (exit 5)
    monkeypatch.setattr(daemon, "read_daemon_state", lambda *a, **k: None)
    monkeypatch.setattr(
        updater,
        "bootstrap_engine",
        lambda **_: (_ for _ in ()).throw(updater.UpdateError("Offline source directory is invalid")),
    )
    res_boot_upd = CliRunner().invoke(main, ["bootstrap"])
    assert res_boot_upd.exit_code == 1, res_boot_upd.output
    assert "Error: Offline source directory is invalid" in res_boot_upd.output
    assert "Traceback" not in res_boot_upd.output

    monkeypatch.setattr(
        updater,
        "bootstrap_engine",
        lambda **_: (_ for _ in ()).throw(updater.EngineGateError("gate_result timed out")),
    )
    res_boot_gate = CliRunner().invoke(main, ["bootstrap"])
    assert res_boot_gate.exit_code == DAEMON_FAILURE_EXIT_CODE == 5, res_boot_gate.output
    assert "Error: PoB engine bootstrap gate check failed: gate_result timed out" in res_boot_gate.output
    assert "Traceback" not in res_boot_gate.output
