"""Offline & real-engine tests for Layer 2: `pob tree tattoo`, pre-flight guards, `[REQ-DEFICIT]`, `batch`/`sweep` op `tattoo`."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

import pob_cli.analyzer.pitfalls_tattoos as pt_mod
import pob_cli.cli_tree as cli_tree_mod
from pob_cli.analyzer.pitfalls_tattoos import _index_tattoo_records
from pob_cli.cli_tree import grp_tree
from pob_cli.daemon.rpc_mutate import MUTATE_ACTION_HANDLERS
from pob_cli.errors import DaemonRequestError
from pob_cli.mutators import apply_tree_tattoo, run_batch, run_sweep

REAL_ENGINE = find_runnable_engine()
HAS_REAL_POB = REAL_ENGINE is not None
EXAMPLE_CODE_PATH = Path(__file__).resolve().parents[2] / "examples" / "builds" / "dark_bargain_ci_skeleton_330_clean.code.txt"


def _sample_catalog() -> dict[str, dict[str, Any]]:
    return _index_tattoo_records(
        [
            {
                "id": "dex_naturalist",
                "name": "Tattoo of the Tawhoa Naturalist",
                "zh_name": "塔赫亚自然术士的文身",
                "kind": "tattoo",
                "target_type": "Small Dexterity",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "stats": ["8% increased Life Recovery from Flasks"],
                "stats_zh": ["药剂给予的生命恢复提高 8%"],
                "full_stats": ["8% increased Life Recovery from Flasks"],
                "legacy": False,
            },
            {
                "id": "dex_cold_res",
                "name": "Tattoo of the Arohongui Scout",
                "zh_name": "阿罗宏圭斥候的文身",
                "kind": "tattoo",
                "target_type": "Small Dexterity",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "stats": ["+6% to Cold Resistance"],
                "stats_zh": ["+6% 冰霜抗性"],
                "full_stats": ["+6% to Cold Resistance"],
                "legacy": False,
            },
            {
                "id": "int_lightning_res",
                "name": "Tattoo of the Valako Stormrider",
                "zh_name": "瓦拉库风暴骑士的文身",
                "kind": "tattoo",
                "target_type": "Small Intelligence",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "stats": ["+6% to Lightning Resistance"],
                "stats_zh": ["+6% 闪电抗性"],
                "full_stats": ["+6% to Lightning Resistance"],
                "legacy": False,
            },
            {
                "id": "ks_soul",
                "name": "Journey Tattoo of the Soul",
                "zh_name": "灵魂之旅的文身",
                "kind": "tattoo",
                "target_type": "Keystone",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": "Limited to 1 Keystone Tattoo",
                "stats": ["+30 to maximum Life"],
                "stats_zh": ["+30 最大生命"],
                "full_stats": ["+30 to maximum Life", "Limited to 1 Keystone Tattoo"],
                "legacy": False,
            },
            {
                "id": "ks_mind",
                "name": "Journey Tattoo of the Mind",
                "zh_name": "心智之旅的文身",
                "kind": "tattoo",
                "target_type": "Keystone",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": "Limited to 1 Keystone Tattoo",
                "stats": ["+30 to maximum Mana"],
                "stats_zh": ["+30 最大魔力"],
                "full_stats": ["+30 to maximum Mana", "Limited to 1 Keystone Tattoo"],
                "legacy": False,
            },
            {
                "id": "makanga_min7",
                "name": "Tattoo of the Tawhoa Makanga",
                "zh_name": "塔赫亚玛坎加的文身",
                "kind": "tattoo",
                "target_type": "Small Dexterity",
                "target_value": "",
                "min_connected": 7,
                "max_connected": 100,
                "limit_text": None,
                "stats": ["+1% to maximum Chaos Resistance"],
                "stats_zh": ["+1% 混沌抗性上限"],
                "full_stats": ["+1% to maximum Chaos Resistance"],
                "legacy": False,
            },
            {
                "id": "makanga_max1",
                "name": "Tattoo of the Ngamahu Makanga",
                "zh_name": "努葛玛呼玛坎加的文身",
                "kind": "tattoo",
                "target_type": "Small Strength",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 1,
                "limit_text": None,
                "stats": ["+1% to maximum Fire Resistance"],
                "stats_zh": ["+1% 火焰抗性上限"],
                "full_stats": ["+1% to maximum Fire Resistance"],
                "legacy": False,
            },
        ]
    )


class _FakeTattooWorker:
    def __init__(self, *, caps: dict[str, Any] | None = None, dex_tight: bool = False) -> None:
        self.lock = threading.RLock()
        self.caps = caps if caps is not None else {"tattoos": True}
        self._nodes_db: dict[str, Any] = {
            "1": {"id": 1, "name": "Ranger", "type": "ClassStart", "linked": [100]},
            "3656": {"id": 3656, "name": "Intelligence", "type": "Normal", "stats": ["+10 to Intelligence"], "linked": [1, 100]},
            "100": {"id": 100, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1, 3656]},
            "101": {"id": 101, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [100]},
            "102": {"id": 102, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [101]},
            "200": {"id": 200, "name": "Hybrid", "type": "Normal", "stats": ["+10 to Strength and Intelligence"], "linked": [1]},
            "500": {"id": 500, "name": "Acrobatics", "type": "Keystone", "linked": [100]},
            "501": {"id": 501, "name": "Phase Acrobatics", "type": "Keystone", "linked": [101]},
        }
        self._overrides: dict[str, dict[str, Any]] = {
            "101": {"why": "tattoo", "name": "Tattoo of the Tawhoa Naturalist"},
            "102": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "500": {"why": "tattoo", "name": "Journey Tattoo of the Soul"},
        }
        self._allocated = [1, 3656, 100, 101, 102, 200, 500, 501, 9001]
        self._dex_tight = dex_tight
        self._snapshots: dict[str, dict[str, dict[str, Any]]] = {}
        self._snap_seq = 0
        self.apply_calls: list[dict[str, Any]] = []

    def _stats(self) -> dict[str, Any]:
        dex_tats = sum(1 for sid in ("100", "101") if (self._overrides.get(sid) or {}).get("why") == "tattoo")
        dex_val = (155 if self._dex_tight else 200) - dex_tats * 10
        cold_bonus = 6 if (self._overrides.get("100") or {}).get("name") == "Tattoo of the Arohongui Scout" else 0
        dps = 1_000_000.0 + (20_000.0 if (self._overrides.get("100") or {}).get("name") == "Tattoo of the Tawhoa Naturalist" else 0.0)
        return {
            "CombinedDPS": dps,
            "FullDPS": dps,
            "TotalDPS": dps,
            "TotalEHP": 50000.0 + cold_bonus * 500.0,
            "Life": 4000.0,
            "EnergyShield": 0.0,
            "Str": 100.0,
            "ReqStr": 80.0,
            "Dex": float(dex_val),
            "ReqDex": 140.0,
            "Int": 100.0,
            "ReqInt": 80.0,
            "FireResist": 75.0,
            "ColdResist": 70.0 + cold_bonus,
            "LightningResist": 75.0,
            "ChaosResist": 0.0,
        }

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_30", "nodes": self._nodes_db}

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = params or {}
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "className": "Ranger",
                "ascendClassName": "Pathfinder",
                "allocatedNodes": list(self._allocated),
                "overrides": copy.deepcopy(self._overrides),
                "dynamicNodes": [{"id": 9001, "name": "Dexterity", "type": "Normal", "expansionSkill": True}],
            }
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_build_info":
            return {"className": "Ranger", "ascendClassName": "Pathfinder", "level": 95, "points": {"used": 8, "usedMax": 123}}
        if method == "get_warnings":
            return {"warnings": []}
        if method == "list_skills":
            return {"groups": [], "mainSocketGroup": 1}
        if method == "list_items":
            return {"items": [], "slots": []}
        if method == "list_config":
            return {"sections": [], "customMods": []}
        if method == "export_code":
            self._snap_seq += 1
            tok = f"SNAP_{self._snap_seq}"
            self._snapshots[tok] = copy.deepcopy(self._overrides)
            return {"code": tok}
        if method == "import_code":
            tok = str(p.get("code") or "")
            if tok in self._snapshots:
                self._overrides = copy.deepcopy(self._snapshots[tok])
            return {"ok": True}
        if method == "tattoo_options":
            return {
                "id": int(p["id"]),
                "allowed": True,
                "count": "Tattoo Count: 2/50",
                "options": [
                    {"index": 1, "id": "Tattoo of the Tawhoa Naturalist", "name": "Tattoo of the Tawhoa Naturalist", "nameZh": "塔赫亚自然术士的文身", "lines": ["8% increased Life Recovery from Flasks"]},
                ],
            }
        if method == "tattoo_apply":
            self.apply_calls.append(dict(p))
            sid = str(int(p["id"]))
            if p.get("reset"):
                self._overrides.pop(sid, None)
            else:
                self._overrides[sid] = {"why": "tattoo", "name": str(p["tattoo"])}
            return {"ok": True}
        return {"ok": True}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        _ = (timeout, raise_on_error)
        return [self.call(m, p) for m, p in calls]


def test_preflight_rejects_cross_attr_cluster_conquered_limit_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: _sample_catalog())
    w = _FakeTattooWorker()

    # Cross-attribute rejection prints exact node base AND current tree free/tattooed inventory
    with pytest.raises(DaemonRequestError) as exc_info:
        apply_tree_tattoo(w, node=3656, tattoo="Tattoo of the Tawhoa Naturalist")  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "Node 3656 base is Small Intelligence (+10 Int)" in msg
    assert "requires Small Dexterity (+10 Dex)" in msg
    assert "Current tree slots:" in msg and "Small Dex 1 free (1 tattooed)" in msg and "Small Int 1 free" in msg
    assert w.apply_calls == []

    # Hybrid attribute, cluster expansionSkill, conquered timeless, duplicate Keystone limit
    with pytest.raises(DaemonRequestError, match="hybrid attribute"):
        apply_tree_tattoo(w, node=200, tattoo="Tattoo of the Tawhoa Naturalist")  # type: ignore[arg-type]
    with pytest.raises(DaemonRequestError, match="expansionSkill"):
        apply_tree_tattoo(w, node=9001, tattoo="Tattoo of the Tawhoa Naturalist")  # type: ignore[arg-type]
    with pytest.raises(DaemonRequestError, match="Timeless Jewel"):
        apply_tree_tattoo(w, node=102, tattoo="Tattoo of the Tawhoa Naturalist")  # type: ignore[arg-type]
    with pytest.raises(DaemonRequestError, match="Limited to 1 keystone tattoo"):
        apply_tree_tattoo(w, node=501, tattoo="Journey Tattoo of the Mind")  # type: ignore[arg-type]

    # Re-tattooing the SAME Keystone node 500 with another Keystone Tattoo is allowed!
    res_same = apply_tree_tattoo(w, node=500, tattoo="Journey Tattoo of the Mind", apply=False)  # type: ignore[arg-type]
    assert res_same["applied"] is False
    assert w._overrides["500"]["name"] == "Journey Tattoo of the Soul"  # zero residue rollback!

    # Engine without caps.tattoos fails cleanly
    w_nocap = _FakeTattooWorker(caps={"tattoos": False})
    with pytest.raises(DaemonRequestError, match=r"caps\.tattoos"):
        apply_tree_tattoo(w_nocap, node=100, tattoo="Tattoo of the Tawhoa Naturalist")  # type: ignore[arg-type]


def test_preflight_min_max_connected_allocated_and_incomplete_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: _sample_catalog())
    w = _FakeTattooWorker()
    # Add 8 neighbor nodes (601..608) in nodes_db, node 700 (Small Dex) linked to all 8, node 701 (Small Str) linked to 3
    for i in range(601, 609):
        w._nodes_db[str(i)] = {"id": i, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [700]}
    w._nodes_db["700"] = {"id": 700, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": list(range(601, 609))}
    w._nodes_db["701"] = {"id": 701, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [601, 602, 603]}

    # 8 static links, only 2 allocated -> min_connected=7 MUST reject
    w._allocated = [1, 700, 601, 602]
    with pytest.raises(DaemonRequestError, match=r"MinimumConnected >= 7"):
        apply_tree_tattoo(w, node=700, tattoo="Tattoo of the Tawhoa Makanga")  # type: ignore[arg-type]

    # 7 of 8 allocated -> min_connected=7 passes
    w._allocated = [1, 700, 601, 602, 603, 604, 605, 606, 607]
    res_min_ok = apply_tree_tattoo(w, node=700, tattoo="Tattoo of the Tawhoa Makanga", apply=False)  # type: ignore[arg-type]
    assert res_min_ok["detail"]["tattoo"] == "Tattoo of the Tawhoa Makanga"

    # 3 static links, 1 allocated -> max_connected=1 passes; 2 allocated -> rejected
    w._allocated = [1, 701, 601]
    res_max_ok = apply_tree_tattoo(w, node=701, tattoo="Tattoo of the Ngamahu Makanga", apply=False)  # type: ignore[arg-type]
    assert res_max_ok["detail"]["tattoo"] == "Tattoo of the Ngamahu Makanga"

    w._allocated = [1, 701, 601, 602]
    with pytest.raises(DaemonRequestError, match=r"MaximumConnected <= 1"):
        apply_tree_tattoo(w, node=701, tattoo="Tattoo of the Ngamahu Makanga")  # type: ignore[arg-type]

    # Incomplete nodes_db (603 missing) -> skip adjacency check instead of falling back to len(linked)=3
    del w._nodes_db["603"]
    res_partial = apply_tree_tattoo(w, node=701, tattoo="Tattoo of the Ngamahu Makanga", apply=False)  # type: ignore[arg-type]
    assert res_partial["detail"]["tattoo"] == "Tattoo of the Ngamahu Makanga"


def test_tattoo_req_deficit_cli_batch_and_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: _sample_catalog())
    w_tight = _FakeTattooWorker(dex_tight=True)

    # What-if surfaces [REQ-DEFICIT] warning and rolls back with zero residue
    res_warn = apply_tree_tattoo(w_tight, node=100, tattoo="塔赫亚自然术士的文身", apply=False)  # type: ignore[arg-type]
    assert res_warn["applied"] is False
    assert any("[REQ-DEFICIT]" in m and "Dex" in m for m in res_warn.get("reqWarnings") or ())
    assert "100" not in w_tight._overrides

    # --strict-req vetoes with DaemonRequestError and leaves 0 residue
    with pytest.raises(DaemonRequestError, match=r"\[REQ-DEFICIT\]"):
        apply_tree_tattoo(w_tight, node=100, tattoo="Tattoo of the Tawhoa Naturalist", strict_req=True, apply=True)  # type: ignore[arg-type]
    assert "100" not in w_tight._overrides

    # Batch + Sweep op: "tattoo"
    w = _FakeTattooWorker(dex_tight=False)
    batch_res = run_batch(
        w,  # type: ignore[arg-type]
        ops=[{"op": "tattoo", "node": 100, "tattoo": "Tattoo of the Arohongui Scout"}],
        apply=False,
    )
    assert batch_res["applied"] is False
    assert batch_res["detail"]["steps"][0]["tattoo"] == "Tattoo of the Arohongui Scout"
    assert "100" not in w._overrides

    sweep_res = run_sweep(
        w,  # type: ignore[arg-type]
        variants=[
            {"name": "flask-life", "ops": [{"op": "tattoo", "node": 100, "tattoo": "Tattoo of the Tawhoa Naturalist"}]},
            {"name": "cold-res", "ops": [{"op": "tattoo", "node": 100, "tattoo": "Tattoo of the Arohongui Scout"}]},
        ],
    )
    assert len(sweep_res["variants"]) == 2
    assert all(v.get("ok", True) and not v.get("error") for v in sweep_res["variants"])
    assert "100" not in w._overrides

    # Daemon handler + CLI `pob tree tattoo`
    monkeypatch.setattr(
        cli_tree_mod,
        "_invoke_tree_daemon",
        lambda action, params=None, **_kw: MUTATE_ACTION_HANDLERS[action](None, params or {}, w),  # type: ignore[arg-type]
    )
    runner = CliRunner()
    r_insp = runner.invoke(grp_tree, ["tattoo", "--node", "100"])
    assert r_insp.exit_code == 0 and "Tattoo of the Tawhoa Naturalist" in r_insp.output
    r_json = runner.invoke(grp_tree, ["tattoo", "--node", "100", "--tattoo", "Tattoo of the Tawhoa Naturalist", "--json"])
    assert r_json.exit_code == 0
    payload = json.loads(r_json.output)
    assert payload["applied"] is False and payload["detail"]["tattoo"] == "Tattoo of the Tawhoa Naturalist"


def test_batch_tattoo_strict_req_rollback_and_non_strict_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: _sample_catalog())
    w_tight = _FakeTattooWorker(dex_tight=True)

    # Non-strict batch tattoo step attaches reqDeficits, reqWarnings, and warnings
    non_strict = run_batch(
        w_tight,  # type: ignore[arg-type]
        ops=[{"op": "tattoo", "node": 100, "tattoo": "Tattoo of the Tawhoa Naturalist"}],
        apply=False,
    )
    step = non_strict["detail"]["steps"][0]
    assert step["reqDeficits"] and step["reqDeficits"][0]["attribute"] == "Dex"
    assert any("[REQ-DEFICIT]" in m for m in step.get("reqWarnings") or ())
    assert any("[REQ-DEFICIT]" in m for m in step.get("warnings") or ())
    assert "100" not in w_tight._overrides

    # strictReq=True raises DaemonRequestError inside transaction and rolls back with zero residue
    with pytest.raises(DaemonRequestError, match=r"\[REQ-DEFICIT\]"):
        run_batch(
            w_tight,  # type: ignore[arg-type]
            ops=[{"op": "tattoo", "node": 100, "tattoo": "Tattoo of the Tawhoa Naturalist", "strictReq": True}],
            apply=True,
        )
    assert "100" not in w_tight._overrides


def test_live_options_ambiguity_and_mastery_tree_click_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.mutators.tattoos import _resolve_tattoo_query

    # live_options fallback enforces tiered ambiguity rejection (two prefix/substring hits)
    live_opts = [
        {"id": "Tattoo of the Tawhoa Naturalist", "name": "Tattoo of the Tawhoa Naturalist", "lines": []},
        {"id": "Tattoo of the Tawhoa Scout", "name": "Tattoo of the Tawhoa Scout", "lines": []},
    ]
    with pytest.raises(DaemonRequestError, match="Ambiguous tattoo"):
        _resolve_tattoo_query("Tawhoa", {}, live_options=live_opts)
    exact = _resolve_tattoo_query("Tattoo of the Tawhoa Scout", {}, live_options=live_opts)
    assert exact["name"] == "Tattoo of the Tawhoa Scout"

    # Mastery reallocation raises DaemonRequestError if tree_click returns needsMastery/needsConfirm
    cat = _index_tattoo_records(
        [
            {
                "id": "rg_treachery",
                "name": "Runegraft of Treachery",
                "zh_name": "背叛之符文嵌板",
                "kind": "runegraft",
                "target_type": "Mastery",
                "target_value": "",
                "min_connected": 0,
                "max_connected": 100,
                "limit_text": None,
                "stats": ["15% increased Reservation Efficiency of Skills"],
                "legacy": False,
            }
        ]
    )
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: cat)
    w = _FakeTattooWorker()
    w._nodes_db["800"] = {"id": 800, "name": "Life Mastery", "type": "Mastery", "linked": [1]}
    w._allocated.append(800)
    w._overrides["800"] = {"why": "mastery", "name": "Life Mastery"}
    orig_call = w.call

    def _click_needs_mastery(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "tree_click":
            return {"ok": True, "needsMastery": True}
        return orig_call(method, params)

    monkeypatch.setattr(w, "call", _click_needs_mastery)
    with pytest.raises(DaemonRequestError, match="needsMastery=true"):
        apply_tree_tattoo(w, node=800, tattoo="Runegraft of Treachery", apply=False)  # type: ignore[arg-type]


def test_connectivity_five_allocated_of_eight_and_unverified_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt_mod, "load_tattoo_catalog", lambda _w=None: _sample_catalog())
    w = _FakeTattooWorker()
    for i in range(601, 609):
        w._nodes_db[str(i)] = {"id": i, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [700]}
    w._nodes_db["700"] = {"id": 700, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": list(range(601, 609))}

    # 8 static linked neighbors, only 5 allocated -> MUST reject MinimumConnected >= 7
    w._allocated = [1, 700, 601, 602, 603, 604, 605]
    with pytest.raises(DaemonRequestError, match=r"5 allocated adjacent passive\(s\).*MinimumConnected >= 7"):
        apply_tree_tattoo(w, node=700, tattoo="Tattoo of the Tawhoa Makanga")  # type: ignore[arg-type]

    # Incomplete nodes_db (neighbor 608 missing) -> emits [UNVERIFIED-CONNECTIVITY] warning in detail and top-level
    del w._nodes_db["608"]
    res_unv = apply_tree_tattoo(w, node=700, tattoo="Tattoo of the Tawhoa Makanga", apply=False)  # type: ignore[arg-type]
    assert any("[UNVERIFIED-CONNECTIVITY]" in m for m in res_unv["detail"].get("warnings") or ())
    assert any("[UNVERIFIED-CONNECTIVITY]" in m for m in res_unv.get("warnings") or ())


@pytest.mark.skipif(not HAS_REAL_POB, reason="No runnable real PoB engine found")
def test_real_pob_tree_tattoo_what_if_apply_clear_and_sweep(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker

    assert REAL_ENGINE is not None
    w = BridgeWorker("tattoo-mut-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        code = EXAMPLE_CODE_PATH.read_text(encoding="utf-8").strip()
        w.call("import_code", {"code": code, "mode": "replace"})
        ts = w.call("get_tree_state", {})
        ndb = w.get_cached_tree_data(ts.get("treeVersion")).get("nodes", {})

        int_nid = next(
            int(nid)
            for nid in ts.get("allocatedNodes", [])
            if isinstance(ndb.get(str(int(nid))), dict)
            and ndb[str(int(nid))].get("type") == "Normal"
            and ndb[str(int(nid))].get("name") == "Intelligence"
        )
        # 1) Cross-attribute rejection with live inventory hint
        with pytest.raises(DaemonRequestError) as exc_info:
            apply_tree_tattoo(w, node=int_nid, tattoo="Tattoo of the Tawhoa Naturalist")
        assert "Small Intelligence" in str(exc_info.value) and "Current tree slots:" in str(exc_info.value)

        # 2) Valid Small Intelligence tattoo what-if (must leave 0 residue)
        before_ov = copy.deepcopy(w.call("get_tree_state", {}).get("overrides", {}).get(str(int_nid)))
        res_wi = apply_tree_tattoo(w, node=int_nid, tattoo="Tattoo of the Valako Stormrider", apply=False)
        assert res_wi["applied"] is False
        assert res_wi["detail"]["tattoo"] == "Tattoo of the Valako Stormrider"
        after_wi_ov = w.call("get_tree_state", {}).get("overrides", {}).get(str(int_nid))
        assert after_wi_ov == before_ov

        # 3) Sweep comparing 2 Small Intelligence tattoos on the same node
        sweep = run_sweep(
            w,
            variants=[
                {"name": "lightning-res", "ops": [{"op": "tattoo", "node": int_nid, "tattoo": "Tattoo of the Valako Stormrider"}]},
                {"name": "clear-to-int", "ops": [{"op": "tattoo", "node": int_nid, "tattoo": "clear"}]},
            ],
        )
        assert len(sweep["variants"]) == 2
        assert all(not v.get("error") for v in sweep["variants"])
        assert w.call("get_tree_state", {}).get("overrides", {}).get(str(int_nid)) == before_ov

        # 4) --apply persists, then clear removes override
        res_ap = apply_tree_tattoo(w, node=int_nid, tattoo="Tattoo of the Valako Stormrider", apply=True)
        assert res_ap["applied"] is True
        assert w.call("get_tree_state", {})["overrides"][str(int_nid)]["name"] == "Tattoo of the Valako Stormrider"
        res_cl = apply_tree_tattoo(w, node=int_nid, tattoo="clear", apply=True)
        assert res_cl["detail"]["action"] == "clear"
        assert str(int_nid) not in w.call("get_tree_state", {}).get("overrides", {})
    finally:
        w.stop()
