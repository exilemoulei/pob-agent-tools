"""Unit tests for upstream PoB kernels, agent probes, caps gating, and P3 K>=50 surrogate floor."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import analyzer, cli, cli_trade, comparator
from pob_cli.trade_eval import (
    Candidate,
    eval_trade_items,
)
from pob_cli.trade_eval.scoring import (
    MIN_SURROGATE_PREFILTER_K,
    compute_grad_weights,
    prefilter_candidates_with_surrogate,
    score_candidate_surrogate,
)
from pob_cli.trade_item import parse_item_text

LUA_DIR = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"


class _CapsFakeWorker:
    """Scripted fake BridgeWorker supporting optional capability flags and CoW/DirectCalc/Probes."""

    def __init__(self, caps: dict[str, Any] | None = None, *, omit_caps_attr: bool = False) -> None:
        self.lock = threading.RLock()
        if not omit_caps_attr:
            self.caps = dict(caps or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_good_code: str | None = "ORIG_LAST_GOOD"
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self._base_dps = 1_000_000.0
        self._cur_dps = self._base_dps
        self._snaps: dict[str, float] = {}
        self._snap_seq = 0
        self._items = {1: {"id": 1, "name": "Old Ring", "type": "Ring", "rarity": "RARE"}}
        self._slots = [{"name": "Ring 1", "label": "Ring 1", "selItemId": 1, "shown": True}]
        self._next_item_id = 10

    def get_cached_tree_data(self, _tv: Any = None) -> dict[str, Any]:
        return {"nodes": {}}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for method, params in calls:
            if raise_on_error:
                out.append(self.call(method, params or {}))
            else:
                try:
                    out.append(self.call(method, params or {}))
                except Exception as exc:
                    out.append({"ok": False, "_error": str(exc), "error": str(exc)})
        return out

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "get_stats":
            return {
                "stats": {
                    "CombinedDPS": self._cur_dps,
                    "TotalEHP": 50_000.0,
                    "Life": 4500.0,
                    "FireResist": 75.0,
                    "ColdResist": 75.0,
                    "LightningResist": 75.0,
                    "ChaosResist": 10.0,
                    "FireResistOverCap": 20.0,
                    "ColdResistOverCap": 20.0,
                    "LightningResistOverCap": 20.0,
                    "PhysicalMaximumHitTaken": 12_000.0,
                    "FireMaximumHitTaken": 30_000.0,
                    "ColdMaximumHitTaken": 30_000.0,
                    "LightningMaximumHitTaken": 30_000.0,
                    "ChaosMaximumHitTaken": 15_000.0,
                    "Str": 150.0,
                    "Dex": 150.0,
                    "Int": 150.0,
                    "ReqStr": 100.0,
                    "ReqDex": 100.0,
                    "ReqInt": 100.0,
                    "Speed": 7.5,
                }
            }
        if method == "direct_calc":
            return {"stats": {"CombinedDPS": self._cur_dps, "TotalEHP": 52_000.0, "Life": 4580.0}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_info":
            return {
                "level": 95,
                "className": "Shadow",
                "ascendClassName": "Assassin",
                "treeVersion": "3_30",
                "points": {"used": 115, "usedMax": 123, "ascUsed": 8, "ascMax": 8},
            }
        if method == "get_build_header":
            return {"level": 95}
        if method == "list_items":
            return {"items": list(self._items.values()), "slots": [dict(s) for s in self._slots]}
        if method == "item_raw":
            return {"raw": "Rarity: RARE\nOld Ring\nRuby Ring\nImplicits: 0\n+50 to maximum Life"}
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "slot": "Body Armour",
                        "gems": [
                            {"index": 1, "name": "Ice Nova", "enabled": True, "support": False},
                            {"index": 2, "name": "Cast On Critical Strike Support", "enabled": True, "support": True},
                        ],
                    }
                ],
            }
        if method == "list_config":
            return {"sections": [{"items": [{"var": "enemyIsBoss", "value": "None"}]}], "customMods": []}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_tree_state":
            return {"treeVersion": "3_30", "allocatedNodes": [], "dynamicNodes": [], "overrides": {}, "sockets": []}
        if method == "list_specs":
            return {"activeSpec": 1, "specs": [{"index": 1, "title": "Default"}]}
        if method in ("set_config", "reset_config", "set_custom_mods", "set_slot_active", "set_gem"):
            if method == "set_config" and p.get("var") == "enemyIsBoss":
                if p.get("value") == "Pinnacle":
                    self._cur_dps = 800_000.0
                elif p.get("value") == "Uber":
                    self._cur_dps = 550_000.0
            return {"ok": True}
        if method == "export_code":
            return {"code": "SNAP_CODE_1"}
        if method == "import_code":
            self._cur_dps = self._base_dps
            return {"ok": True}
        if method == "cow_snapshot":
            self._snap_seq += 1
            sid = f"cow_{self._snap_seq}"
            self._snaps[sid] = self._cur_dps
            return {"snapId": sid}
        if method == "cow_restore":
            sid = str(p["snapId"])
            self._cur_dps = self._snaps[sid]
            if p.get("drop"):
                self._snaps.pop(sid, None)
            return {"ok": True}
        if method == "cow_drop":
            self._snaps.pop(str(p["snapId"]), None)
            return {"ok": True}
        if method == "add_item":
            self._next_item_id += 1
            nid = self._next_item_id
            raw = str(p.get("raw") or "")
            m_life = re.search(r"\+(\d+)\s+to maximum Life", raw)
            bonus = float(m_life.group(1)) * 1000.0 if m_life else 5000.0
            self._cur_dps = self._base_dps + bonus
            self._items[nid] = {"id": nid, "name": f"Cand {nid}", "type": "Ring", "rarity": "RARE"}
            return {"item": self._items[nid]}
        if method == "equip_item":
            self._cur_dps = self._base_dps
            return {"ok": True}
        if method == "delete_item":
            self._items.pop(int(p["id"]), None)
            return {"ok": True}
        if method == "agent_probe_build":
            return {
                "resistSlack": {"Fire": {"final": 75, "total": 95, "cap": 75, "overcapSlack": 20}},
                "attrSlack": {"Str": {"current": 150, "required": 145, "slack": 5}},
                "resourceSlack": {"life": 4500, "manaUnreserved": 120},
                "loadBearingGear": [
                    {
                        "slot": "Amulet",
                        "itemName": "Jade Amulet",
                        "attrDelta": {"Str": 40, "Dex": 30, "Int": 0},
                        "brickedCount": 2,
                        "bricked": [{"source": "Gem", "name": "Determination 20/0"}],
                        "resistDeficits": {},
                    }
                ],
                "triggerCliffs": [
                    {
                        "skillName": "Ice Nova",
                        "triggerName": "Cast On Critical Strike Support",
                        "sourceAps": 8.12,
                        "maxSafeAps": 7.575,
                        "extraIcdrPctNeeded": 14,
                        "overspeedCliffExceeded": True,
                    }
                ],
            }
        if method == "eval_scenario_envelope":
            return {
                "scenarios": {
                    "Raw": {"dps": 1_200_000.0, "totalEhp": 60_000.0},
                    "PinnacleSustained": {"dps": 800_000.0, "totalEhp": 45_000.0},
                    "Burst": {"dps": 1_050_000.0, "totalEhp": 55_000.0},
                    "WorstHit": {"dps": 600_000.0, "totalEhp": 35_000.0},
                },
                "inflationRatio": 1.5,
            }
        if method == "compute_grad_build":
            return {
                "stat": "CombinedDPS",
                "baseStat": 1_000_000.0,
                "weights": {
                    "flat_life": {"scorePerUnit": 0.01},
                    "crit_multi": {"scorePerUnit": 0.05},
                    "fire_res": {"scorePerUnit": 0.002},
                },
            }
        if method == "power_diff_attribution":
            return {
                "stat": p.get("stat") or "CombinedDPS",
                "totalDiffs": 2,
                "diffs": [
                    {"category": "Items", "name": "Amulet", "impact": 180_000.0, "impactPct": 18.0},
                    {"category": "Tree", "name": "Heartseeker", "impact": 60_000.0, "impactPct": 6.0},
                ],
                "byCategory": {"Items": 1, "Tree": 1},
            }
        raise AssertionError(f"Unexpected RPC call: {method}({p})")


def test_lua_modules_contract_budgets_and_no_compare_prefix() -> None:
    uk_path = LUA_DIR / "upstream_kernels.lua"
    ap_path = LUA_DIR / "agent_probes.lua"
    assert uk_path.is_file()
    assert ap_path.is_file()

    for path in (uk_path, ap_path):
        raw = path.read_bytes()
        text = raw.decode("utf-8").replace("\r\n", "\n")
        lines = text.splitlines()
        crlf_bytes = max(len(raw), len(text.encode("utf-8")) + len(lines))
        assert len(lines) <= 650, f"{path.name} exceeds 650L: {len(lines)}"
        assert crlf_bytes <= 24000, f"{path.name} exceeds 24000B CRLF: {crlf_bytes}"
        assert not re.search(r"[A-Za-z]:[/\\]|/(?:Users|home)/", text), f"Local path leaked in {path.name}"

    uk_text = uk_path.read_text(encoding="utf-8")
    assert "function M.install_post_bridge" in uk_text
    assert "node_power = handle_node_power" in uk_text
    assert "power_diff_attribution = handle_power_diff_attribution" in uk_text
    assert "trade_generate_query = handle_trade_generate_query" in uk_text
    # R1 red line: never define any RPC starting with compare_ in M.rpc_handlers
    m_handlers = re.search(r"M\.rpc_handlers\s*=\s*\{(.*?)\}", uk_text, re.DOTALL)
    assert m_handlers is not None
    assert not re.search(r"\bcompare_[a-zA-Z0-9_]+\s*=", m_handlers.group(1))

    ap_text = ap_path.read_text(encoding="utf-8")
    assert "agent_probe_build = handle_agent_probe_build" in ap_text
    assert "eval_scenario_envelope = handle_eval_scenario_envelope" in ap_text
    assert "compute_grad_build = handle_compute_grad_build" in ap_text


def test_caps_gating_no_caps_attr_never_calls_new_rpcs() -> None:
    w = _CapsFakeWorker(omit_caps_attr=True)
    assert not hasattr(w, "caps")

    # Analyzer audit & trigger APS probe must not fail or call new RPCs
    aud = analyzer.audit_build(w, attribution=False)
    assert "agentProbe" not in aud
    assert "scenarioEnvelope" not in aud
    methods = [m for m, _ in w.calls]
    assert "agent_probe_build" not in methods
    assert "eval_scenario_envelope" not in methods

    # Surrogate weights return None cleanly
    assert compute_grad_weights(w) is None
    assert "compute_grad_build" not in [m for m, _ in w.calls]

    # Comparator compare_builds skips power_diff_attribution when cap absent
    mw = _CapsFakeWorker(omit_caps_attr=True)
    rw = _CapsFakeWorker(omit_caps_attr=True)
    cmp_res = comparator.compare_builds(mw, rw)
    assert "powerAttribution" not in cmp_res
    assert "power_diff_attribution" not in [m for m, _ in mw.calls]


def test_agent_probes_integration_in_diagnose_audit_and_matrix() -> None:
    w = _CapsFakeWorker(caps={"agentProbes": True, "cowSnapshot": True})
    aps = analyzer._extract_trigger_attack_aps(
        w,
        {"mainSocketGroup": 1, "groups": [{"index": 1, "isMain": True, "enabled": True, "gems": []}]},
        {"items": [], "slots": []},
        {},
    )
    assert aps == pytest.approx(8.12)
    assert any(m == "agent_probe_build" for m, _ in w.calls)

    w_diag = _CapsFakeWorker(caps={"agentProbes": True})
    diag = analyzer.diagnose_build(w_diag, no_rank=True)
    assert "agentProbe" in diag
    cats = {g["category"] for g in diag["healthGaps"]}
    assert "TriggerCliff" in cats
    assert "TriggerOverspeed" not in cats
    assert "LoadBearingGear" in cats

    w_aud = _CapsFakeWorker(caps={"agentProbes": True})
    aud = analyzer.audit_build(w_aud, attribution=False)
    assert aud["agentProbe"]["triggerCliffs"][0]["skillName"] == "Ice Nova"
    assert aud["scenarioEnvelope"]["inflationRatio"] == pytest.approx(1.5)

    w_mat = _CapsFakeWorker(caps={"agentProbes": True, "cowSnapshot": True})
    mat = comparator.run_matrix(w_mat, ["Original", "Pinnacle", "Uber"])
    assert mat["restoreCheck"]["verified"] is True
    assert mat["scenarioEnvelope"]["inflationRatio"] == pytest.approx(1.5)
    mat_methods = [m for m, _ in w_mat.calls]
    assert "cow_snapshot" in mat_methods
    assert "cow_restore" in mat_methods
    assert "import_code" not in mat_methods


def test_comparator_power_attribution_and_cluster_node_ranking() -> None:
    mw = _CapsFakeWorker(caps={"powerAttribution": True})
    rw = _CapsFakeWorker(caps={"powerAttribution": True})
    res = comparator.compare_builds(mw, rw)
    assert res["powerAttribution"]["byCategory"] == {"Items": 1, "Tree": 1}
    assert any(m == "power_diff_attribution" for m, _ in mw.calls)
    assert not any(m.startswith("compare_") for m, _ in mw.calls)

    class _ClusterNodeWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            if method == "node_power":
                return {
                    "stat": "CombinedDPS",
                    "stats": ["CombinedDPS"],
                    "report": [
                        {"id": 10, "name": "Heartseeker", "allocated": False, "pathDist": 2, "pathPower": 5.0, "power": 10.0},
                        {"id": 65540, "name": "Prismatic Heart", "allocated": False, "pathDist": "Cluster", "pathPower": 0.0, "power": 14.5},
                        {"id": 65541, "name": "Sadist", "allocated": False, "pathDist": "Cluster", "pathPower": 0.0, "power": 18.2},
                    ],
                }
            if method == "list_items":
                return {"slots": [], "items": []}
            return {}

    ranked = comparator.rank_nodes(_ClusterNodeWorker(), stat="CombinedDPS", max_dist=5, top=5)  # type: ignore[arg-type]
    assert [n["name"] for n in ranked["byPerPoint"]] == ["Heartseeker"]
    assert [n["name"] for n in ranked["topCluster"]] == ["Sadist", "Prismatic Heart"]


def test_p3_surrogate_k_ge_50_floor_and_cow_direct_calc_trade_eval() -> None:
    assert MIN_SURROGATE_PREFILTER_K == 50

    grad = {
        "stat": "CombinedDPS",
        "baseStat": 1_000_000.0,
        "weights": {"flat_life": {"scorePerUnit": 0.01}, "crit_multi": {"scorePerUnit": 0.05}},
    }

    def _mk_cand(i: int) -> Candidate:
        raw = f"Rarity: RARE\nRing {i}\nRuby Ring\nImplicits: 0\n+{10 + i} to maximum Life\n+{i % 35}% to Global Critical Strike Multiplier"
        item = parse_item_text(raw)
        return Candidate(raw={"id": f"r{i}"}, item=item, item_text=raw, score=0.0)

    cands_70 = [_mk_cand(i) for i in range(70)]
    # Asking for top_k=10 MUST enforce K >= 50 floor (Rule P3)
    filtered = prefilter_candidates_with_surrogate(cands_70, grad, top_k=10)
    assert len(filtered) == 50
    top_score = score_candidate_surrogate(filtered[0].item, grad)
    last_score = score_candidate_surrogate(filtered[-1].item, grad)
    assert top_score >= last_score

    # Fewer than 50 candidates keeps all
    assert len(prefilter_candidates_with_surrogate(cands_70[:30], grad, top_k=10)) == 30

    # eval_trade_items with cowSnapshot + directCalc + agentProbes + prefilter_k=15 -> evaluates 50 items via CoW + direct_calc!
    w = _CapsFakeWorker(caps={"cowSnapshot": True, "directCalc": True, "agentProbes": True})
    raw_items = [
        {"id": i, "raw": f"Rarity: RARE\nRing {i}\nRuby Ring\nImplicits: 0\n+{20 + i} to maximum Life"}
        for i in range(65)
    ]
    res = eval_trade_items(w, slot_name="Ring 1", items=raw_items, prefilter_k=15)
    assert len(res["results"]) == 50
    assert all(r["ok"] for r in res["results"])
    methods = [m for m, _ in w.calls]
    assert "compute_grad_build" in methods
    assert "cow_snapshot" in methods
    assert "direct_calc" in methods
    assert "cow_restore" in methods
    assert "import_code" not in methods
    # Top life items (highest surrogate score) must be included in the 50 kept
    kept_ids = {r["id"] for r in res["results"]}
    assert 64 in kept_ids and 63 in kept_ids
    assert 0 not in kept_ids


def test_cli_trade_search_auto_query_dump_and_surrogate_k_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        calls.append((action, dict(params or {})))
        if action == "trade_generate_query":
            return {
                "slotName": "Ring 1",
                "stat": "CombinedDPS",
                "queryJson": json.dumps({"query": {"stats": [{"type": "weight", "filters": [{"id": "explicit.stat_1", "value": {"weight": 2.5}}]}]}}),
                "filters": 1,
                "complexity": 69,
            }
        raise AssertionError(f"Unexpected action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)
    runner = CliRunner()
    res = runner.invoke(
        cli.main,
        ["trade", "search", "--slot", "ring1", "--auto-query", "--dump-query", "--budget", "5", "--currency", "divine"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["autoQuery"] is True
    assert payload["filters"] == 1
    assert payload["complexity"] == 69
    assert payload["query"]["query"]["filters"]["trade_filters"]["filters"]["price"] == {"option": "divine", "max": 5.0}
    assert isinstance(payload["statLabels"], dict)
    assert payload["topFilters"][0]["id"] == "explicit.stat_1"
    assert "label" in payload["topFilters"][0]
    assert payload["slotConstraints"]["loadBearingAttrs"] == {"Str": 0, "Dex": 0, "Int": 0}
    assert calls[0][0] == "trade_generate_query"

    # Live --auto-query (without --dump-query) must forward the PoB-generated query body to _execute_trade_api_query
    seen_auto_body: list[Any] = []
    from pob_cli.cli_trade import search_cmd as _sc

    monkeypatch.setattr(_sc, "_resolve_current_and_category", lambda **_kw: (None, None, None, "slot:main", "accessory.ring"))
    monkeypatch.setattr(
        _sc,
        "_execute_trade_api_query",
        lambda *_a, **kw: seen_auto_body.append(kw.get("auto_query_body")) or None,
    )
    res_live = runner.invoke(cli.main, ["trade", "search", "--slot", "ring1", "--auto-query", "--budget", "5", "--currency", "divine"])
    assert res_live.exit_code == 0, res_live.output
    assert isinstance(seen_auto_body[0], dict)
    assert seen_auto_body[0]["query"]["stats"][0]["type"] == "weight"
    assert seen_auto_body[0]["query"]["filters"]["trade_filters"]["filters"]["price"] == {"option": "divine", "max": 5.0}


def test_p1_1_exclude_periodic_reactive_triggers_and_p1_2_cospri_dedup() -> None:
    from pob_cli.analyzer.diagnose_probes import _gaps_from_agent_probe
    from pob_cli.analyzer.pitfalls_triggers import deduplicate_trigger_gaps

    # 1. P1-1: Automation / CWDT / Mark On Hit / Trigger Craft must NEVER emit TriggerCliff or leak APS
    probe_with_periodic = {
        "triggerCliffs": [
            {
                "skillName": "Arcane Cloak",
                "triggerName": "Automation",
                "sourceAps": 5.60,
                "maxSafeAps": 0.25,
                "overspeedCliffExceeded": True,
            },
            {
                "skillName": "Molten Shell",
                "triggerName": "Cast when Damage Taken",
                "sourceAps": 5.60,
                "maxSafeAps": 4.0,
                "overspeedCliffExceeded": True,
            },
            {
                "skillName": "Sniper's Mark",
                "triggerName": "Mark On Hit",
                "sourceAps": 5.60,
                "maxSafeAps": 0.25,
                "overspeedCliffExceeded": True,
            },
        ],
        "loadBearingGear": [],
    }
    assert _gaps_from_agent_probe(probe_with_periodic) == []

    # Multi-spell trigger cliff in same trigger groups into a single TriggerCliff warning
    multi_cliff_gaps = _gaps_from_agent_probe(
        {
            "triggerCliffs": [
                {
                    "skillName": "Freezing Pulse",
                    "triggerName": "Cospri's Malice",
                    "currentIcdrPct": 32,
                    "sourceAps": 10.58,
                    "maxSafeAps": 10.10,
                    "extraIcdrPctNeeded": 20,
                    "overspeedCliffExceeded": True,
                },
                {
                    "skillName": "Frostbite",
                    "triggerName": "Cospri's Malice",
                    "currentIcdrPct": 32,
                    "sourceAps": 10.58,
                    "maxSafeAps": 10.10,
                    "extraIcdrPctNeeded": 20,
                    "overspeedCliffExceeded": True,
                },
            ],
            "loadBearingGear": [],
        }
    )
    assert len(multi_cliff_gaps) == 1
    assert "Freezing Pulse, Frostbite" in multi_cliff_gaps[0]["message"]
    assert "at 32% CDR" in multi_cliff_gaps[0]["message"]

    w_periodic = _CapsFakeWorker(caps={"agentProbes": True})
    aps_none = analyzer._extract_trigger_attack_aps(
        w_periodic,
        {"mainSocketGroup": 1, "groups": [{"index": 1, "isMain": True, "enabled": True, "gems": []}]},
        {"items": [], "slots": []},
        {},
        agent_probe=probe_with_periodic,
    )
    assert aps_none is None

    # 2. P1-2: Deduplicate conflicting 0% CDR vs 32% CDR Cospri's Malice warnings while preserving offline fallback
    stale_0_cdr = {
        "category": "TriggerOverspeed",
        "severity": "HIGH",
        "message": (
            "Trigger Overspeed (Cospri's Malice, Weapon 1): Attack rate (5.46/s) "
            "exceeds trigger cap (3.79/s at 0% CDR, 8t = 264ms) — lower APS <= 3.79/s."
        ),
    }
    accurate_32_cdr = {
        "category": "TriggerOverspeed",
        "severity": "HIGH",
        "message": (
            "Trigger Overspeed (Cospri's Malice, Weapon 1): Attack rate (5.46/s) "
            "exceeds trigger cap (5.05/s at 32% CDR, 6t = 198ms) — lower APS <= 5.05/s."
        ),
    }
    probe_cospri_2spell = {
        "triggerCliffs": [
            {
                "skillName": "Freezing Pulse",
                "triggerName": "Cospri's Malice",
                "baseActionCd": 0.25,
                "currentIcdrPct": 32,
                "linkedTriggeredSpells": 2,
                "sourceAps": 5.46,
                "maxSafeAps": 10.10,
                "overspeedCliffExceeded": False,
            }
        ]
    }
    deduped = deduplicate_trigger_gaps([stale_0_cdr, accurate_32_cdr], probe_cospri_2spell)
    assert len(deduped) == 1
    assert "32% CDR" in deduped[0]["message"] and "5.05/s" in deduped[0]["message"]

    # Even if pitfalls only had 0% CDR (unparsed CDR source), agent_probe's 32% CDR upgrades it cleanly
    upgraded = deduplicate_trigger_gaps([stale_0_cdr], probe_cospri_2spell)
    assert len(upgraded) == 1
    assert "32% CDR" in upgraded[0]["message"] and "5.05/s" in upgraded[0]["message"]

    # Offline fallback (agent_probe=None) preserves pitfalls_triggers TriggerOverspeed
    w_offline = _CapsFakeWorker(omit_caps_attr=True)
    diag_offline = analyzer.diagnose_build(w_offline, no_rank=True)
    offline_overspeeds = [g for g in diag_offline["healthGaps"] if g["category"] == "TriggerOverspeed"]
    assert len(offline_overspeeds) == 1


def test_live_engine_p1_automation_cospri_and_auto_query_labels_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import tempfile
    import zlib

    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.updater import build_engine_command, resolve_engine_root

    eng = resolve_engine_root()
    try:
        build_engine_command(eng)
    except Exception:
        pytest.skip("Real PoE1 engine runtime not available")
    if not (eng / "PoE1" / "Data").is_dir():
        pytest.skip("Real PoE1 engine data not available")

    # Inline self-cast spell + Arcane Cloak (Automation) + load-bearing Dex Amulet + Chaos Res Ring
    b5_like_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Templar" ascendClassName="Hierophant" mainSocketGroup="1" viewMode="TREE"/>
  <Config activeConfigSet="1"><ConfigSet id="1">
    <Input boolean="true" name="usePowerCharges"/>
    <Input boolean="true" name="useEnduranceCharges"/>
  </ConfigSet></Config>
  <Tree activeSpec="1"><Spec classId="5" ascendClassId="2" treeVersion="3_30" nodes="19635"/></Tree>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Mind Pendant
Lapis Amulet
Implicits: 1
+30 to Intelligence
+80 to maximum Life
+110 to maximum Mana
+1 to Level of all Lightning Skill Gems
+55 to Dexterity
</Item>
    <Item id="2">Rarity: RARE
Font Band
Amethyst Ring
Implicits: 1
+23% to Chaos Resistance
+70 to maximum Life
+35% to Chaos Resistance
+45% to Fire Resistance
+45% to Cold Resistance
+45% to Lightning Resistance
</Item>
    <ItemSet id="1">
      <Slot name="Amulet" itemId="1"/>
      <Slot name="Ring 2" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1"><SkillSet id="1">
    <Skill enabled="true" slot="Body Armour" mainActiveSkill="1" includeInFullDPS="true">
      <Gem nameSpec="Ball Lightning" skillId="BallLightning" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Spell Echo" skillId="SupportSpellEcho" level="20" quality="20" enabled="true"/>
    </Skill>
    <Skill enabled="true" slot="Helmet">
      <Gem nameSpec="Arcane Cloak" skillId="ArcaneCloak" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Automation" skillId="Automation" level="20" quality="20" enabled="true"/>
    </Skill>
  </SkillSet></Skills>
</PathOfBuilding>"""

    # Inline Cyclone CoC + Cospri's Malice (Freezing Pulse + Frostbite, ~5.5 APS) + 32% CDR (16% Boots + 16% Belt)
    b2_like_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Shadow" ascendClassName="Assassin" mainSocketGroup="1" viewMode="TREE"/>
  <Config activeConfigSet="1"><ConfigSet id="1"><Input boolean="true" name="usePowerCharges"/></ConfigSet></Config>
  <Tree activeSpec="1"><Spec classId="6" ascendClassId="1" treeVersion="3_30" nodes="58229"/></Tree>
  <Items activeItemSet="1">
    <Item id="1" variant="2">Rarity: UNIQUE
Cospri's Malice
Jewelled Foil
Selected Variant: 2
Implicits: 1
+25% to Global Critical Strike Multiplier
Trigger a Socketed Cold Spell on Melee Critical Strike, with a 0.25 second Cooldown
+257 Intelligence Requirement
No Physical Damage
Adds 90 to 180 Cold Damage
14% increased Attack Speed
</Item>
    <Item id="2">Rarity: RARE
Chrono Treads
Sorcerer Boots
Implicits: 0
16% increased Cooldown Recovery Rate
+200 to Dexterity
+200 to Intelligence
</Item>
    <Item id="3">Rarity: RARE
Tempo Sash
Crystal Belt
Implicits: 0
16% increased Cooldown Recovery Rate
</Item>
    <ItemSet id="1">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Boots" itemId="2"/>
      <Slot name="Belt" itemId="3"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1"><SkillSet id="1">
    <Skill enabled="true" slot="Body Armour" mainActiveSkill="1" includeInFullDPS="true">
      <Gem nameSpec="Cyclone" skillId="Cyclone" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Cast On Critical Strike" skillId="SupportCastOnCriticalStrike" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Ice Spear" skillId="IceSpear" level="20" quality="20" enabled="true"/>
    </Skill>
    <Skill enabled="true" slot="Weapon 1" includeInFullDPS="true">
      <Gem nameSpec="Freezing Pulse" skillId="FreezingPulse" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Frostbite" skillId="Frostbite" level="20" quality="20" enabled="true"/>
    </Skill>
  </SkillSet></Skills>
</PathOfBuilding>"""

    def _encode_xml(xml_str: str) -> str:
        return base64.urlsafe_b64encode(zlib.compress(xml_str.encode("utf-8"), 9)).decode("ascii")

    with tempfile.TemporaryDirectory() as td:
        w = BridgeWorker(slot_name="test", pob_dir=eng, runtime_dir=Path(td))
        try:
            w.start(restore_code="")
            if not (w.caps.get("agentProbes") and w.caps.get("tradeAutoQuery")):
                pytest.skip("Engine does not advertise agentProbes + tradeAutoQuery extensions")
            # P1-1: Arcane Cloak (Automation) produces ZERO TriggerCliff / TriggerOverspeed gaps
            w.call("import_code", {"code": _encode_xml(b5_like_xml), "mode": "replace"})
            d_b5 = analyzer.diagnose_build(w, no_rank=True)
            trig_gaps_b5 = [
                g for g in d_b5["healthGaps"] if g["category"] in ("TriggerCliff", "TriggerOverspeed")
            ]
            assert trig_gaps_b5 == []
            assert d_b5["agentProbe"]["triggerCliffs"] == []

            # P1-4: trade_generate_query + `--auto-query --dump-query` labels, <=160 complexity, noise filter, slotConstraints
            tq_amu = w.call("trade_generate_query", {"slotName": "Amulet"})
            assert 1 <= tq_amu["filters"] <= 22
            assert tq_amu["complexity"] <= 160
            assert tq_amu["complexityWithinLimit"] is True
            q_parsed = json.loads(tq_amu["queryJson"])
            w_filters = q_parsed["stats"][0]["filters"]
            assert len(w_filters) == tq_amu["filters"]
            weights_list = [abs(f["value"]["weight"]) for f in w_filters]
            assert weights_list == sorted(weights_list, reverse=True)
            for f in w_filters:
                fid = f["id"]
                assert fid.startswith(("explicit.", "crafted.", "fractured.", "pseudo."))
                assert fid in tq_amu["statLabels"] and tq_amu["statLabels"][fid] != fid
                low_lbl = tq_amu["statLabels"][fid].lower()
                assert "per power charge" not in low_lbl
                assert "per endurance charge" not in low_lbl
                assert "minimum frenzy charge" not in low_lbl
            assert all(tf.get("label") and tf["label"] != tf["id"] for tf in tq_amu["topFilters"])

            sc = tq_amu["slotConstraints"]
            assert sc["isLoadBearing"] is True
            assert sc["loadBearingAttrs"]["Dex"] == 55
            assert "Dex" in sc["attrDeficits"] and sc["attrDeficits"]["Dex"]["minNeeded"] > 0

            # CLI end-to-end check for `--auto-query --dump-query`
            monkeypatch.setattr(cli_trade, "_daemon_call_safe", lambda _act, _p, **_: tq_amu)
            cli_res = CliRunner().invoke(
                cli.main, ["trade", "search", "--slot", "amulet", "--auto-query", "--dump-query"]
            )
            assert cli_res.exit_code == 0, cli_res.output
            dumped = json.loads(cli_res.output)
            assert dumped["complexity"] <= 160
            assert dumped["statLabels"]
            assert dumped["topFilters"][0]["label"]
            assert dumped["slotConstraints"]["isLoadBearing"] is True

            # P1-2: Cospri's Malice overspeed appears ONCE with accurate 32% CDR / cap 5.05 numbers
            w.call("import_code", {"code": _encode_xml(b2_like_xml), "mode": "replace"})
            d_b2 = analyzer.diagnose_build(w, no_rank=True)
            cospri_gaps = [
                g
                for g in d_b2["healthGaps"]
                if g["category"] in ("TriggerOverspeed", "TriggerCliff") and "Cospri" in g["message"]
            ]
            assert len(cospri_gaps) == 1
            assert "32% CDR" in cospri_gaps[0]["message"]
            assert "5.05" in cospri_gaps[0]["message"]
        finally:
            w._kill_unlocked()
