"""Unit tests for conquered 0-stat passives, socket-radius bounds, mismatch hints, tree waste audit, and BOM-free JSON."""

from __future__ import annotations

import io
import json
import threading
from typing import Any

from pob_cli.analyzer.diagnose import _collect_diagnose_tree_and_skills
from pob_cli.analyzer.pitfalls_core import _detect_build_mechanics_and_pitfalls
from pob_cli.analyzer.pitfalls_tree import audit_conquered_zero_stat_nodes
from pob_cli.analyzer.tree_inspect import inspect_tree
from pob_cli.formatters.primitives import emit_json
from pob_cli.formatters.tree_md import format_tree_markdown


class _MockTreeWorker:
    """Deterministic in-memory mock BridgeWorker for passive tree & pitfall tests."""

    def __init__(
        self,
        *,
        tree_state: dict[str, Any],
        nodes_db: dict[str, Any],
        items_res: dict[str, Any] | None = None,
        hover_by_id: dict[int, dict[str, Any]] | None = None,
        fail_hover: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self._tree_state = tree_state
        self._nodes_db = nodes_db
        self._items_res = items_res or {"items": [], "slots": []}
        self._hover_by_id = hover_by_id or {}
        self._fail_hover = fail_hover

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_25", "nodes": self._nodes_db}

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method == "get_tree_state":
            return self._tree_state
        if method == "list_items":
            return self._items_res
        if method == "node_hover":
            if self._fail_hover:
                raise RuntimeError("node_hover unsupported by minimal mock")
            nid = int(params["id"])
            return self._hover_by_id.get(nid, {})
        return {}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for method, params in calls:
            try:
                out.append(self.call(method, params))
            except Exception as exc:
                if raise_on_error:
                    raise
                out.append({"_error": str(exc), "ok": False})
        return out


def _sample_conquered_worker() -> _MockTreeWorker:
    nodes_db = {
        "1": {"id": 1, "name": "Scion", "type": "ClassStart", "x": 0, "y": 0, "linked": [34171]},
        "26725": {
            "id": 26725,
            "name": "Jewel Socket",
            "nameZh": "珠宝插槽",
            "type": "Socket",
            "x": 0,
            "y": 0,
            "linked": [34171],
        },
        "34171": {
            "id": 34171,
            "name": "Strength",
            "nameZh": "力量",
            "type": "Normal",
            "x": 400,
            "y": 0,
            "stats": ["+10 to Strength"],
            "statsZh": ["+10 力量"],
            "linked": [1, 26725, 34172],
        },
        "34172": {
            "id": 34172,
            "name": "Maximum Life",
            "nameZh": "最大生命",
            "type": "Normal",
            "x": 900,
            "y": 0,
            "stats": ["6% increased maximum Life", "12% increased Cooldown Recovery Rate"],
            "statsZh": ["最大生命提高 6%"],
            "linked": [34171],
        },
        "50001": {
            "id": 50001,
            "name": "Slum Lord",
            "type": "Notable",
            "x": 1500,
            "y": 0,
            "stats": ["Minions deal 80% increased Damage"],
            "linked": [34172],
        },
        "61308": {
            "id": 61308,
            "name": "Amplify",
            "nameZh": "增幅",
            "type": "Notable",
            "x": 1900,
            "y": 0,
            "stats": ["10% increased Area of Effect", "20% increased Area Damage"],
            "statsZh": ["效果区域扩大 10%", "范围伤害提高 20%"],
            "linked": [],
        },
        "99001": {
            "id": 99001,
            "name": "Verdant Precision",
            "type": "Notable",
            "blighted": True,
            "x": 500,
            "y": 0,
            "stats": ["Anoint-only blighted notable"],
            "linked": [],
        },
    }
    tree_state = {
        "treeVersion": "3_25",
        "className": "Scion",
        "ascendClassName": "Ascendant",
        "points": {"used": 5, "usedMax": 123, "ascUsed": 0, "ascMax": 8},
        "allocatedNodes": [1, 26725, 34171, 34172, 50001],
        "sockets": [{"nodeId": 26725, "itemId": 1}],
        "overrides": {
            "34171": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "34172": {"why": "conquered", "name": "Price of Glory", "stats": [], "statsZh": []},
            "50001": {
                "why": "conquered",
                "name": "Slum Lord",
                "stats": ["Minions deal 80% increased Damage"],
            },
        },
    }
    items_res = {
        "items": [
            {
                "id": 1,
                "title": "Elegant Hubris",
                "baseName": "Timeless Jewel",
                "rarity": "UNIQUE",
                "raw": (
                    "Rarity: UNIQUE\n"
                    "Elegant Hubris\n"
                    "Timeless Jewel\n"
                    "Radius: Large\n"
                    "Commissioned 80000 coins to commemorate Cadiro\n"
                    "Passives in radius are Conquered by the Eternal Empire\n"
                ),
            }
        ],
        "slots": [{"name": "Jewel 26725", "nodeId": 26725, "selItemId": 1, "socketIndex": 1}],
    }
    return _MockTreeWorker(tree_state=tree_state, nodes_db=nodes_db, items_res=items_res)


def test_conquered_zero_stat_passives_do_not_leak_preconquered_stats() -> None:
    worker = _sample_conquered_worker()
    summary = inspect_tree(worker)

    normals = {n["id"]: n for n in summary["normalNodes"]}
    assert set(normals) == {34171, 34172}
    for nid in (34171, 34172):
        assert normals[nid]["overrideWhy"] == "conquered"
        assert normals[nid]["name"] == "Price of Glory"
        assert normals[nid]["stats"] == []
        assert normals[nid].get("statsZh") == []
        assert normals[nid].get("nameZh") in (None, "")

    # Searching pre-conquered stats/names must NOT match conquered Price of Glory nodes
    str_search = inspect_tree(worker, search="Strength")
    assert str_search["totalMatches"] == 0
    zh_search = inspect_tree(worker, search="力量")
    assert zh_search["totalMatches"] == 0

    # Summary Markdown surfaces conquered 0-stat small passives bullet
    md_summary = format_tree_markdown(summary)
    assert "- **Conquered 0-Stat Small Passives**: 2 allocated (Price of Glory: 34171, 34172)" in md_summary

    # Search Markdown renders _(0 stats — conquered)_ in Stats column
    pog_search = inspect_tree(worker, search="Price of Glory")
    assert pog_search["totalMatches"] == 2
    md_search = format_tree_markdown(pog_search, search_label="Price of Glory")
    assert "_(0 stats — conquered)_" in md_search
    assert "+10" not in md_search

    # Diagnose tree collector must also clear pre-conquered stats (no false CDR/Life leak)
    alloc_node_stats, _, _, _ = _collect_diagnose_tree_and_skills(
        worker, worker._tree_state, summary, {"groups": []}
    )
    stat_labels = [lbl for lbl, _ in alloc_node_stats]
    all_lines = [ln for _, lines in alloc_node_stats for ln in lines]
    assert "Price of Glory" not in stat_labels
    assert not any("Cooldown Recovery" in ln or "Strength" in ln for ln in all_lines)


def test_conquered_override_preserves_explicit_nonempty_stats_zh() -> None:
    worker = _sample_conquered_worker()
    worker._tree_state["overrides"]["34171"] = {
        "why": "conquered",
        "name": "Custom Conquered",
        "stats": [],
        "statsZh": ["自定义征服效果"],
    }
    summary = inspect_tree(worker)
    normals = {n["id"]: n for n in summary["normalNodes"]}
    assert normals[34171]["stats"] == []
    assert normals[34171]["statsZh"] == ["自定义征服效果"]


def test_socket_radius_excludes_blighted_and_marks_equipped_jewel_boundary() -> None:
    worker = _sample_conquered_worker()
    res = inspect_tree(worker, socket_radius="#1")

    sr = res["socketRadius"]
    matched_ids = {m["id"] for m in res["matches"]}
    # Blighted anoint-only notable 99001 at x=500 must be excluded
    assert 99001 not in matched_ids
    assert {34171, 34172, 50001, 61308}.issubset(matched_ids)

    # Equipped Elegant Hubris active radius metadata
    assert sr["equippedJewel"] == "Elegant Hubris"
    assert sr["equippedRadiusLabel"] == "Large"
    assert sr["equippedMaxRadius"] == 1800

    # All radius buckets remain present for backwards compatibility
    assert set(sr["radii"].keys()) == {"Small", "Medium", "Large", "Very Large", "Massive"}

    by_id = {m["id"]: m for m in res["matches"]}
    assert by_id[34171]["withinEquippedRadius"] is True
    assert by_id[34172]["withinEquippedRadius"] is True
    assert by_id[50001]["withinEquippedRadius"] is True
    assert by_id[61308]["withinEquippedRadius"] is False

    md = format_tree_markdown(res)
    assert "— Equipped: Elegant Hubris (Active Radius: Large)" in md
    assert "#### Radius `Large` (1 nodes)\n" in md
    assert "#### Radius `Very Large` (1 nodes) — outside equipped Large radius (unconquered)" in md
    assert "_(0 stats — conquered)_" in md

    # Empty socket sets equipped fields and withinEquippedRadius to None
    worker._items_res["slots"][0]["selItemId"] = 0
    worker._tree_state["sockets"][0]["itemId"] = 0
    empty_res = inspect_tree(worker, socket_radius="#1")
    assert empty_res["socketRadius"]["equippedJewel"] is None
    assert empty_res["socketRadius"]["equippedMaxRadius"] is None
    assert all(m["withinEquippedRadius"] is None for m in empty_res["matches"])


def test_allocated_search_mismatch_hint_both_directions() -> None:
    worker = _sample_conquered_worker()

    # 61308 (增幅) is unallocated -> --allocated --search "增幅" yields 0 matches + mismatch hint
    res_alloc = inspect_tree(worker, search="增幅", allocated=True)
    assert res_alloc["totalMatches"] == 0
    assert (
        res_alloc["allocationMismatchHint"]
        == "0 allocated matches; matching unallocated node(s) exist: 61308 (增幅). Omit --allocated to inspect."
    )
    md_alloc = format_tree_markdown(res_alloc, search_label="增幅")
    assert "0 allocated matches; matching unallocated node(s) exist: 61308 (增幅)." in md_alloc

    # 50001 (Slum Lord) is allocated -> --unallocated --search "Slum Lord" yields reverse hint
    res_unalloc = inspect_tree(worker, search="Slum Lord", allocated=False)
    assert res_unalloc["totalMatches"] == 0
    assert (
        res_unalloc["allocationMismatchHint"]
        == "0 unallocated matches; matching allocated node(s) exist: 50001 (Slum Lord). Omit --unallocated to inspect."
    )


def test_pitfalls_tree_dead_end_tail_cycle_and_load_bearing_bridge() -> None:
    # Graph:
    #   ClassStart(1) -- Real(10) -- Real(20) -- BridgeBlank(201) -- Notable(30)
    #                       |-- TailBlank(101) -- TailBlank(102) -- TailBlank(103)
    #                       |-- Real(40) -- LoopBlank(301) -- Real(41) -- Real(10)
    nodes_db = {
        "1": {"id": 1, "name": "Start", "type": "ClassStart", "linked": [10]},
        "10": {"id": 10, "name": "Path A", "type": "Normal", "linked": [1, 20, 101, 40, 41]},
        "20": {"id": 20, "name": "Path B", "type": "Normal", "linked": [10, 201]},
        "30": {"id": 30, "name": "Big Notable", "type": "Notable", "linked": [201]},
        "40": {"id": 40, "name": "Loop Left", "type": "Normal", "linked": [10, 301]},
        "41": {"id": 41, "name": "Loop Right", "type": "Normal", "linked": [10, 301]},
        "101": {"id": 101, "name": "Str", "type": "Normal", "linked": [10, 102]},
        "102": {"id": 102, "name": "Dex", "type": "Normal", "linked": [101, 103]},
        "103": {"id": 103, "name": "Int", "type": "Normal", "linked": [102]},
        "201": {"id": 201, "name": "Str", "type": "Normal", "linked": [20, 30]},
        "301": {"id": 301, "name": "Str", "type": "Normal", "linked": [40, 41]},
    }
    tree_state = {
        "treeVersion": "3_25",
        "allocatedNodes": [1, 10, 20, 30, 40, 41, 101, 102, 103, 201, 301],
        "overrides": {
            "101": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "102": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "103": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "201": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "301": {"why": "conquered", "name": "Price of Glory", "stats": []},
        },
    }
    hover_by_id = {
        101: {"depends": [101, 102, 103]},
        102: {"depends": [102, 103]},
        103: {"depends": [103]},
        201: {"depends": [201, 30]},  # load-bearing bridge -> must NOT be flagged removable
        301: {"depends": [301]},  # on a loop -> degree 2 -> redundant_loop_ids
    }
    worker = _MockTreeWorker(tree_state=tree_state, nodes_db=nodes_db, hover_by_id=hover_by_id)

    gaps, info = audit_conquered_zero_stat_nodes(worker, tree_state, nodes_db)
    assert info is not None
    assert info["deadEndIds"] == [101, 102, 103]
    assert info["redundantLoopIds"] == [301]
    assert 201 not in info["deadEndIds"]
    assert 201 not in info["redundantLoopIds"]

    assert len(gaps) == 1
    assert gaps[0]["category"] == "TreeWaste"
    assert gaps[0]["severity"] == "HIGH"
    assert "pob tree change --deallocate 101,102,103" in gaps[0]["message"]
    assert "301" in gaps[0]["message"]

    # Also verify orchestrator integration through pitfalls_core
    core_gaps, mechanics = _detect_build_mechanics_and_pitfalls(
        stats={},
        skills_res={"groups": []},
        items_res={"items": [], "slots": []},
        cfg_res={},
        worker=worker,
        tree_state=tree_state,
        nodes_db=nodes_db,
    )
    assert any(g["category"] == "TreeWaste" and g["severity"] == "HIGH" for g in core_gaps)
    assert mechanics["conqueredZeroStatWaste"]["deadEndIds"] == [101, 102, 103]


def test_pitfalls_tree_medium_severity_bridge_only_and_mock_resilience() -> None:
    nodes_db = {
        "1": {"id": 1, "name": "Start", "type": "ClassStart", "linked": [101, 201]},
        "30": {"id": 30, "name": "Notable", "type": "Notable", "linked": [201]},
        "101": {"id": 101, "name": "Str", "type": "Normal", "linked": [1]},
        "201": {"id": 201, "name": "Str", "type": "Normal", "linked": [1, 30]},
    }
    tree_state = {
        "treeVersion": "3_25",
        "allocatedNodes": [1, 30, 101, 201],
        "overrides": {
            "101": {"why": "conquered", "name": "Price of Glory", "stats": []},
            "201": {"why": "conquered", "name": "Price of Glory", "stats": []},
        },
    }

    # 1 dead-end node -> MEDIUM severity
    w_med = _MockTreeWorker(
        tree_state=tree_state,
        nodes_db=nodes_db,
        hover_by_id={101: {"depends": [101]}, 201: {"depends": [201, 30]}},
    )
    gaps_med, info_med = audit_conquered_zero_stat_nodes(w_med, tree_state, nodes_db)
    assert len(gaps_med) == 1
    assert gaps_med[0]["severity"] == "MEDIUM"
    assert info_med is not None and info_med["deadEndIds"] == [101]

    # Only load-bearing bridge allocated -> zero waste findings
    tree_bridge_only = {**tree_state, "allocatedNodes": [1, 30, 201]}
    gaps_none, info_none = audit_conquered_zero_stat_nodes(w_med, tree_bridge_only, nodes_db)
    assert gaps_none == []
    assert info_none is None

    # Mock worker failing or returning empty hover must degrade gracefully without raising
    w_fail = _MockTreeWorker(tree_state=tree_state, nodes_db=nodes_db, fail_hover=True)
    assert audit_conquered_zero_stat_nodes(w_fail, tree_state, nodes_db) == ([], None)
    w_empty = _MockTreeWorker(tree_state=tree_state, nodes_db=nodes_db, hover_by_id={})
    assert audit_conquered_zero_stat_nodes(w_empty, tree_state, nodes_db) == ([], None)


def test_emit_json_writes_bom_free_utf8_bytes_and_ps51_hint(monkeypatch: Any) -> None:
    raw_out = io.BytesIO()
    # Even if stdout TextIOWrapper uses utf-8-sig (which would prepend EF BB BF on text write),
    # emit_json must write raw UTF-8 directly to stdout.buffer with zero BOM.
    sig_stdout = io.TextIOWrapper(raw_out, encoding="utf-8-sig")
    raw_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", sig_stdout)
    monkeypatch.setattr("sys.stderr", raw_err)

    emit_json({"node": "增幅", "id": 61308}, compact=True)
    emitted_bytes = raw_out.getvalue()
    assert not emitted_bytes.startswith(b"\xef\xbb\xbf")
    assert emitted_bytes.startswith(b"{")
    assert json.loads(emitted_bytes.decode("utf-8")) == {"node": "增幅", "id": 61308}

    # Click-style stream fallback (module starting with 'click.') writes via text stream
    ClickStream = type("ClickStream", (io.StringIO,), {"__module__": "click.testing"})
    click_out = ClickStream()
    monkeypatch.setattr("sys.stdout", click_out)
    emit_json({"ok": True}, compact=True)
    assert json.loads(click_out.getvalue()) == {"ok": True}

    # Large JSON (>16 KB) stderr hint warns about Windows PowerShell 5.1 pipeline UTF-8 BOM
    raw_large = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw_large, encoding="utf-8"))
    raw_err_large = io.StringIO()
    monkeypatch.setattr("sys.stderr", raw_err_large)
    emit_json({"items": ["x" * 200 for _ in range(100)]})
    hint_text = raw_err_large.getvalue()
    assert "[pob hint]" in hint_text
    assert "PowerShell 5.1" in hint_text
    assert "BOM" in hint_text
