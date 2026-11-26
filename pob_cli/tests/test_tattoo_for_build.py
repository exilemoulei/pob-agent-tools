"""Offline & real-engine tests for `analyzer/tattoo_slots.py`, `tree_tattoo_slots`, stale-daemon upgrade, and `--for-build`."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

import pob_cli.cli_data as cli_data_mod
import pob_cli.daemon as daemon_pkg
import pob_cli.game_data as gd_mod
from pob_cli.analyzer.tattoo_slots import (
    BUCKET_KEYSTONE,
    BUCKET_MASTERY,
    BUCKET_NOTABLE_DEX,
    BUCKET_SMALL_DEX,
    BUCKET_SMALL_INT,
    BUCKET_SMALL_STR,
    analyze_tattoo_slots,
    annotate_data_response_for_build,
    inspect_build_tattoo_slots,
)
from pob_cli.cli_data import cmd_data
from pob_cli.daemon.rpc_tree import TREE_ACTION_HANDLERS
from pob_cli.errors import DaemonRequestError
from pob_cli.formatters.gamedata_md import format_build_tattoo_slots_callout
from pob_cli.game_data import _create_schema
from pob_cli.game_data.tattoos import _insert_tattoos_table, query_tattoo_data

REAL_ENGINE = find_runnable_engine()
HAS_REAL_POB_330 = REAL_ENGINE is not None and (
    (REAL_ENGINE / "PoE1" / "TreeData" / "3_30").is_dir() or (REAL_ENGINE / "TreeData" / "3_30").is_dir()
)
EXAMPLE_CODE_PATH = Path(__file__).resolve().parents[2] / "examples" / "builds" / "dark_bargain_ci_skeleton_330_clean.code.txt"


class _FakeTreeWorker:
    def __init__(self, tree_state: dict[str, Any], nodes_db: dict[str, Any], *, build_loaded: bool | None = None) -> None:
        self.lock = threading.RLock()
        self.slot_name = "smoke"
        self.build_loaded = build_loaded
        self._tree_state, self._nodes_db = tree_state, nodes_db

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "get_tree_state"
        return self._tree_state

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_30", "nodes": self._nodes_db}


def _build_tree() -> tuple[dict[str, Any], dict[str, Any]]:
    nodes_db: dict[str, Any] = {
        "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [100]},
        "2": {"id": 2, "name": "Jewel Socket", "type": "Socket", "linked": [100]},
        "3": {"id": 3, "name": "Strength", "type": "Normal", "asc": "Pathfinder", "linked": [100]},
        "4": {"id": 4, "name": "Strength", "type": "Normal", "expansion": True, "linked": [100]},
        "5": {"id": 5, "name": "Strength", "type": "Normal", "proxy": True, "linked": [100]},
        "6": {"id": 6, "name": "Strength", "type": "Normal", "blighted": True, "linked": [100]},
        "700": {"id": 700, "name": "Agility", "type": "Notable", "stats": ["+30 to Dexterity"], "linked": [100, 101]},
        "800": {"id": 800, "name": "Life Mastery", "type": "Mastery", "linked": [100]},
        "801": {"id": 801, "name": "Flask Mastery", "type": "Mastery", "linked": [101]},
        "850": {"id": 850, "name": "Phase Acrobatics", "type": "Keystone", "linked": [100]},
    }
    allocated = [1, 2, 3, 4, 5, 6, 700, 800, 801, 850, 9001]
    # Real bridge.lua:get_tree_state override shapes ({why, name, nameZh, stats, statsZh}).
    overrides: dict[str, dict[str, Any]] = {
        "800": {"why": "mastery", "name": "Life Mastery", "nameZh": "生命专精", "stats": ["+30 to maximum Life"], "statsZh": []},
        # PassiveSpec.lua:1450 leaves masterySelections[id] set, so bridge.lua emits why="mastery" for a Runegraft.
        "801": {"why": "mastery", "name": "Runegraft of Treachery", "nameZh": "背叛之符文嵌板", "stats": ["15% increased Reservation Efficiency of Skills"], "statsZh": []},
        "850": {"why": "tattoo", "name": "Acrobatics", "nameZh": "移形换影", "stats": [], "statsZh": []},
    }
    for i in range(29):
        nid = 100 + i
        nodes_db[str(nid)] = {"id": nid, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": list(range(1, 8)) if i == 0 else [1, 2]}
        allocated.append(nid)
        overrides[str(nid)] = {"why": "tattoo", "name": "Tattoo of the Tawhoa Herbalist", "nameZh": "塔赫亚草药师的文身", "stats": [], "statsZh": []}
    for i in range(2):
        nid = 200 + i
        nodes_db[str(nid)] = {"id": nid, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 2]}
        allocated.append(nid)
        overrides[str(nid)] = {"why": "conquered", "name": "Price of Glory", "nameZh": "荣耀的代价", "stats": [], "statsZh": []}
    for i in range(4):
        nid = 300 + i
        nodes_db[str(nid)] = {"id": nid, "name": "Intelligence", "type": "Normal", "stats": ["+10 to Intelligence"], "linked": [1, 2]}
        allocated.append(nid)
        overrides[str(nid)] = {"why": "tattoo", "name": "Tattoo of the Rongokurai Turtle", "nameZh": "隆戈库莱巨龟的文身", "stats": [], "statsZh": []}
    tree_state = {
        "treeVersion": "3_30", "className": "Ranger", "ascendClassName": "Pathfinder",
        "allocatedNodes": allocated, "overrides": overrides,
        "dynamicNodes": [{"id": 9001, "name": "Strength", "type": "Normal", "expansionSkill": True, "linked": [2]}],
    }
    return tree_state, nodes_db


def test_tattoo_slots_inventory_real_bridge_shapes_and_unloaded_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    tree_state, nodes_db = _build_tree()
    worker = _FakeTreeWorker(tree_state, nodes_db)
    slots = inspect_build_tattoo_slots(worker, target_slot="agy11_smoke")
    assert TREE_ACTION_HANDLERS["tree_tattoo_slots"](None, {"target": "agy11_smoke"}, worker) == slots  # type: ignore[arg-type]
    # 29 Small Dex + 4 Small Int + 1 Keystone tattoo = 34; Runegraft (why="mastery") -> runegraftsUsed == 1
    assert slots["buildLoaded"] is True and slots["tattoosUsed"] == 34 and slots["runegraftsUsed"] == 1
    b = slots["buckets"]
    assert b[BUCKET_SMALL_STR]["eligible"] == 0
    assert (b[BUCKET_SMALL_DEX]["eligible"], b[BUCKET_SMALL_DEX]["free"], b[BUCKET_SMALL_DEX]["tattooed"], b[BUCKET_SMALL_DEX]["conquered"]) == (29, 0, 29, 2)
    assert (b[BUCKET_SMALL_INT]["eligible"], b[BUCKET_SMALL_INT]["tattooed"]) == (4, 4)
    assert (b[BUCKET_KEYSTONE]["eligible"], b[BUCKET_KEYSTONE]["free"], b[BUCKET_KEYSTONE]["tattooed"]) == (1, 0, 1)
    assert (b[BUCKET_NOTABLE_DEX]["free"], b[BUCKET_MASTERY]["free"], b[BUCKET_MASTERY]["tattooed"]) == (1, 1, 1)

    makanga = {"kind": "tattoo", "results": [{"name": "Honoured Tattoo of the Makanga", "target_type": "Small Attribute", "target_value": "", "min_connected": 7, "max_connected": 100}]}
    annotate_data_response_for_build(makanga, slots)
    assert makanga["results"][0]["eligibleNodes"] == 1 and makanga["results"][0]["availableOnTree"] is True

    # P1-1: Unloaded / ClassStart-only / empty build must NOT falsely stamp [0 available on current tree]
    for empty_ts, flg in (({"allocatedNodes": [], "overrides": {}}, None), ({"allocatedNodes": [1], "overrides": {}}, None), (tree_state, False)):
        empty_slots = analyze_tattoo_slots(empty_ts, nodes_db, target_slot="empty", build_loaded=flg)
        assert empty_slots["buildLoaded"] is False
        rec = {"name": "Tattoo of the Ngamahu Firewalker", "target_type": "Small Strength", "target_value": ""}
        annotate_data_response_for_build({"kind": "tattoo", "results": [rec]}, empty_slots)
        assert "availableOnTree" not in rec and "eligibleNodes" not in rec
    monkeypatch.setenv("POB_LANG", "zh-CN")
    warn_line = format_build_tattoo_slots_callout(empty_slots)
    assert "[WARN] No build (or 0 allocated passive nodes) loaded in slot `empty`" in warn_line and "未加载构筑" in warn_line


def test_cli_for_build_warnings_target_guard_and_bilingual_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    tree_state, nodes_db = _build_tree()
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_tattoos_table(
        conn,
        [
            {"id": "Nat", "name": "Tattoo of the Tawhoa Naturalist", "zh_name": "塔赫亚自然术士的文身", "kind": "tattoo", "override_type": "TawhoaTattoo", "target_type": "Small Dexterity", "target_value": "", "legacy": False, "stats": ["8% increased Life Recovery from Flasks"], "stats_zh": ["药剂给予的生命恢复提高 8%"], "limit_text": None, "limit_text_zh": None, "full_stats": ["8% increased Life Recovery from Flasks"], "min_connected": 0, "max_connected": 100},
            {"id": "Fire", "name": "Tattoo of the Ngamahu Firewalker", "zh_name": "努葛玛呼烈焰行者的文身", "kind": "tattoo", "override_type": "NgamahuTattoo", "target_type": "Small Strength", "target_value": "", "legacy": False, "stats": ["+6% to Fire Resistance"], "stats_zh": ["+6% 火焰抗性"], "limit_text": None, "limit_text_zh": None, "full_stats": ["+6% to Fire Resistance"], "min_connected": 0, "max_connected": 100},
        ],
    )
    daemon_calls: list[tuple[str, dict[str, Any] | None]] = []

    def _q_local(kind: str, query: str = "", **kw: Any) -> dict[str, Any]:
        recs = query_tattoo_data(conn, query, kind="runegraft" if kind == "runegraft" else kw.get("tattoo_kind", "all"), slot_filter=kw.get("slot"), mod_filter=kw.get("mod_filter"), include_legacy=bool(kw.get("include_legacy")), limit=int(kw.get("limit", 25)))
        return {"kind": kind, "query": query, "pobVersion": "2.69", "dataVersion": "3.30", "count": len(recs), "results": recs}

    def _q_multi(kind: str, queries: list[str], **kw: Any) -> dict[str, Any]:
        grouped = {q: _q_local(kind, q, **kw) for q in queries}
        return {"kind": kind, "queries": queries, "pobVersion": "2.69", "dataVersion": "3.30", "count": sum(v["count"] for v in grouped.values()), "results": grouped}

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        daemon_calls.append((action, params))
        out = analyze_tattoo_slots(tree_state, nodes_db, target_slot=str((params or {}).get("target") or "main"))
        out["slotDriftWarning"] = "Slot drift detected"
        return out

    monkeypatch.setattr(gd_mod, "query_local_data", _q_local)
    monkeypatch.setattr(gd_mod, "query_local_data_multi", _q_multi, raising=False)
    monkeypatch.setattr(cli_data_mod, "invoke_daemon", _fake_daemon)
    monkeypatch.setenv("POB_LANG", "zh-CN")

    runner = CliRunner()
    try:
        assert runner.invoke(cmd_data, ["tattoo", "药水生命"]).exit_code == 0 and not daemon_calls
        assert runner.invoke(cmd_data, ["gem", "Fireball", "--for-build"]).exit_code == 2
        # P3-5: --target without --for-build fails cleanly
        r_tguard = runner.invoke(cmd_data, ["tattoo", "火抗", "--target", "ref"])
        assert r_tguard.exit_code == 2 and "requires `--for-build`" in r_tguard.output

        # P2-1 & P3-6: slotDriftWarning prepended in Markdown, bilingual glosses present in zh-CN
        r_multi = runner.invoke(cmd_data, ["tattoo", "药水生命", "火抗", "--for-build", "--target", "agy11_smoke"])
        assert r_multi.exit_code == 0, r_multi.output
        assert "[WARN] Slot drift detected" in r_multi.output
        assert "[0 available on current tree] (当前树 0 可用)" in r_multi.output
        assert "0 free / 29 tattooed [re-tattoo only] (仅可重涂) (2 conquered)" in r_multi.output

        # P2-1 JSON: slotDriftWarning promoted to top-level and stripped from buildTattooSlots
        r_json = runner.invoke(cmd_data, ["tattoo", "火抗", "--for-build", "--target", "agy11_smoke", "--json"])
        payload = json.loads(r_json.output)
        assert r_json.exit_code == 0 and payload["slotDriftWarning"] == "Slot drift detected"
        assert "slotDriftWarning" not in payload["buildTattooSlots"]

        monkeypatch.setenv("POB_LANG", "en")
        r_en = runner.invoke(cmd_data, ["tattoo", "火抗", "--for-build", "--target", "agy11_smoke"])
        assert "[0 available on current tree]" in r_en.output and "当前树" not in r_en.output
    finally:
        conn.close()


def test_stale_daemon_unknown_action_restarts_and_preserves_slots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[int, str, dict[str, Any]]] = []
    stopped: list[Path] = []
    ensured: list[Path] = []
    alive_pids: set[int] = {999999}
    stop_succeeds = True
    load_fails = False
    old_slots_resp: dict[str, Any] = {"main": {"hasLoadedBuild": True}, "empty": {"hasLoadedBuild": False}}
    old_state = {"pid": 999999, "port": 19101, "token": "old-tok"}
    new_state = {"pid": 999998, "port": 19102, "token": "new-tok"}

    monkeypatch.setattr("pob_cli.daemon.client._is_pid_alive", lambda pid: pid in alive_pids)
    monkeypatch.setattr(daemon_pkg, "read_daemon_state", lambda _rt: old_state)

    def _fake_stop(rt: Path) -> dict[str, Any]:
        stopped.append(rt)
        if stop_succeeds:
            alive_pids.discard(999999)
            return {"stopped": True}
        return {"stopped": False}

    monkeypatch.setattr(daemon_pkg, "stop_daemon", _fake_stop)
    monkeypatch.setattr(daemon_pkg, "_ensure_daemon", lambda rt, runtime_dir_arg=None: ensured.append(rt) or (new_state, None))

    def _fake_http(port: int, _tok: str, payload: dict[str, Any], timeout: float = 150.0) -> tuple[int, dict[str, Any]]:
        act, prm = str(payload["action"]), dict(payload.get("params") or {})
        calls.append((port, act, prm))
        if port == 19101:
            if act == "status":
                return 200, {"ok": True, "result": {"slots": old_slots_resp}}
            if act == "export":
                return 200, {"ok": True, "result": {"code": "SAVED_MAIN_CODE"}}
            return 400, {"ok": False, "errorKind": "request", "error": f"Unknown daemon action '{act}'."}
        if act == "status":
            return 200, {"ok": True, "result": {"slots": {}}}
        if act == "load":
            if load_fails:
                return 500, {"ok": False, "error": "boom during replay"}
            return 200, {"ok": True, "result": {"loaded": True}}
        return 200, {"ok": True, "result": {"buildLoaded": True, "tattoosUsed": 7}}

    monkeypatch.setattr(daemon_pkg, "_http_post_rpc", _fake_http)

    def _reset(sub: str) -> Path:
        calls.clear()
        stopped.clear()
        ensured.clear()
        alive_pids.add(999999)
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    # (a) Persisted slot_main.json: skip export & load replay so SlotHistory restores undo/redo natively
    rt_a = _reset("a_persisted")
    (rt_a / "slot_main.json").write_text(json.dumps({"last_good_code": "DISK_CODE", "undo_stack": ["U1"], "rev": 2}), encoding="utf-8")
    res_a = daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_a, emit_cold_start_alert=False)
    assert res_a == {"buildLoaded": True, "tattoosUsed": 7}
    assert [(p, a) for p, a, _ in calls] == [(19101, "tree_tattoo_slots"), (19101, "status"), (19102, "tree_tattoo_slots")]

    # (b) Unpersisted legacy slot: export -> stop -> restart -> status -> load -> retry, recovery file unlinked
    rt_b = _reset("b_legacy")
    res_b = daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_b, emit_cold_start_alert=False)
    assert res_b == {"buildLoaded": True, "tattoosUsed": 7}
    assert [(p, a) for p, a, _ in calls] == [
        (19101, "tree_tattoo_slots"), (19101, "status"), (19101, "export"),
        (19102, "status"), (19102, "load"), (19102, "tree_tattoo_slots"),
    ]
    assert calls[4][2]["source"] == "SAVED_MAIN_CODE" and not (rt_b / "recovery_slots.json").exists()

    # (c) Unpersisted slot with undoDepth > 0: abort before stop_daemon with actionable pob stop error
    rt_c = _reset("c_undo")
    old_slots_resp = {"main": {"hasLoadedBuild": True, "undoDepth": 2}}
    with pytest.raises(DaemonRequestError, match="pob stop"):
        daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_c, emit_cold_start_alert=False)
    assert not stopped and not ensured
    old_slots_resp = {"main": {"hasLoadedBuild": True}}

    # (d) stop_daemon failure with old PID still alive: abort without spawning a second daemon
    rt_d = _reset("d_stop_fail")
    stop_succeeds = False
    with pytest.raises(RuntimeError, match="Failed to stop stale PoB daemon"):
        daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_d, emit_cold_start_alert=False)
    assert len(stopped) == 1 and not ensured
    stop_succeeds = True

    # (e) Replay load failure: raise error and preserve recovery_slots.json backup on disk
    rt_e = _reset("e_replay_fail")
    load_fails = True
    with pytest.raises(RuntimeError, match=r"recovery_slots\.json"):
        daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_e, emit_cold_start_alert=False)
    assert json.loads((rt_e / "recovery_slots.json").read_text(encoding="utf-8")) == {"main": "SAVED_MAIN_CODE"}
    load_fails = False

    # (e2) Boot failure after stop_daemon: recovery_slots.json was ALREADY persisted before stop_daemon
    rt_e2 = _reset("e2_boot_fail")
    monkeypatch.setattr(daemon_pkg, "_ensure_daemon", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boot failed")))
    with pytest.raises(RuntimeError, match="boot failed"):
        daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_e2, emit_cold_start_alert=False)
    assert json.loads((rt_e2 / "recovery_slots.json").read_text(encoding="utf-8")) == {"main": "SAVED_MAIN_CODE"}
    monkeypatch.setattr(daemon_pkg, "_ensure_daemon", lambda rt, runtime_dir_arg=None: ensured.append(rt) or (new_state, None))

    # (f) Unregistered bogus action raises immediately without stopping/restarting
    rt_f = _reset("f_bogus")
    with pytest.raises(DaemonRequestError, match="Unknown daemon action 'nonexistent_xyz'"):
        daemon_pkg.call_daemon("nonexistent_xyz", {}, runtime_dir=rt_f, emit_cold_start_alert=False)
    assert not stopped and not ensured

    # (g) Concurrent replacement daemon (live PID 888888): neither stop_daemon nor _spawn_daemon_process is called
    rt_g = _reset("g_concurrent")
    alive_pids.add(888888)
    repl_state = {"pid": 888888, "port": 19102, "token": "new-tok"}
    state_reads = iter([old_state, repl_state, repl_state])
    spawned: list[Any] = []
    monkeypatch.setattr(daemon_pkg, "read_daemon_state", lambda _rt: next(state_reads, repl_state))
    monkeypatch.setattr(daemon_pkg, "_ensure_daemon", daemon_pkg.client._ensure_daemon)
    monkeypatch.setattr("pob_cli.daemon.client._spawn_daemon_process", lambda *a, **kw: spawned.append(a))
    res_g = daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_g, emit_cold_start_alert=False)
    assert res_g == {"buildLoaded": True, "tattoosUsed": 7} and not stopped and not spawned

    # (h) Merge unresolved prior recovery_slots.json (excluding now-persisted slots) & keep daemon.json if PID changed after stop
    rt_h = _reset("h_merge_and_keep_new_state")
    (rt_h / "recovery_slots.json").write_text(json.dumps({"aux": "PRIOR_AUX", "persisted": "STALE_P"}), encoding="utf-8")
    (rt_h / "slot_persisted.json").write_text(json.dumps({"last_good_code": "DISK_P"}), encoding="utf-8")
    (rt_h / "daemon.json").write_text(json.dumps(repl_state), encoding="utf-8")
    h_reads = iter([old_state, old_state, repl_state])
    monkeypatch.setattr(daemon_pkg, "read_daemon_state", lambda _rt: next(h_reads, repl_state))
    monkeypatch.setattr(daemon_pkg, "_ensure_daemon", lambda rt, runtime_dir_arg=None: ensured.append(rt) or (new_state, None))
    res_h = daemon_pkg.call_daemon("tree_tattoo_slots", {"target": "main"}, runtime_dir=rt_h, emit_cold_start_alert=False)
    assert res_h == {"buildLoaded": True, "tattoosUsed": 7} and (rt_h / "daemon.json").exists()
    replayed = {prm["target"]: prm["source"] for p, a, prm in calls if p == 19102 and a == "load"}
    assert replayed == {"aux": "PRIOR_AUX", "main": "SAVED_MAIN_CODE"} and not (rt_h / "recovery_slots.json").exists()


@pytest.mark.skipif(not HAS_REAL_POB_330, reason="Requires PoB 3.30+ engine")
def test_real_pob_inspect_build_tattoo_slots_end_to_end(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker

    assert REAL_ENGINE is not None
    w = BridgeWorker("tattoo-slots-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        # Fresh empty engine has 0 non-start allocated nodes -> buildLoaded must be False
        empty = inspect_build_tattoo_slots(w, target_slot="tattoo-slots-e2e")
        assert empty["buildLoaded"] is False

        # Shipped 3.30 example build contains Small Int/Str/Dex tattoos + Runegraft of Treachery on Mastery 63861
        code = EXAMPLE_CODE_PATH.read_text(encoding="utf-8").strip()
        w.call("import_code", {"code": code, "mode": "replace"})
        loaded = inspect_build_tattoo_slots(w, target_slot="tattoo-slots-e2e")
        assert loaded["buildLoaded"] is True
        assert loaded["tattoosUsed"] >= 15 and loaded["runegraftsUsed"] >= 1
        assert loaded["buckets"][BUCKET_SMALL_INT]["tattooed"] >= 10
        assert loaded["buckets"][BUCKET_MASTERY]["tattooed"] >= 1
    finally:
        w.stop()
