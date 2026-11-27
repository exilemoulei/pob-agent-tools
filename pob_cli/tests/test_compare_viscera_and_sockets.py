"""Tests for 3.30 Wickerman Viscera slot resolution, node:<nodeId> sockets, and compare diffs."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli.analyzer.inspectors import resolve_slot_from_items, resolve_slot_name
from pob_cli.comparator.compare import _build_compare_structural_diffs, compare_builds
from pob_cli.formatters import format_compare_markdown
from pob_cli.mutators.batch_ops import _op_item, _op_unequip
from pob_cli.mutators.batch_sweep import run_batch
from pob_cli.mutators.common import _BatchCache, _is_jewel_slot_name, _resolve_slot_from_items

POB_DIR = find_runnable_engine()


def _sample_items_with_empty_viscera_and_sockets() -> dict[str, Any]:
    return {
        "items": [
            {"id": 1, "name": "Vaal Regalia", "rarity": "RARE"},
            {"id": 2, "name": "Watcher's Eye", "baseName": "Prismatic Jewel", "rarity": "UNIQUE"},
        ],
        "slots": [
            {"name": "Body Armour", "selItemId": 1, "shown": True},
            {"name": "Helmet", "selItemId": 0, "shown": True},
            {"name": "Head", "selItemId": 0, "shown": True},
            {"name": "Body", "selItemId": 0, "shown": True},
            {"name": "Arm", "selItemId": 0, "shown": True},
            {"name": "Leg", "selItemId": 0, "shown": True},
            {"name": "Jewel 1", "socketIndex": 1, "nodeId": 33631, "selItemId": 2, "shown": True, "inactive": False},
            {"name": "Jewel 2", "socketIndex": 2, "nodeId": 55190, "selItemId": 0, "shown": True, "inactive": False},
            {"name": "Jewel 3", "socketIndex": 3, "nodeId": 2491, "selItemId": 0, "shown": False, "inactive": True},
        ],
    }


def test_viscera_empty_slot_and_body_armour_disambiguation() -> None:
    items_res = _sample_items_with_empty_viscera_and_sockets()

    for fn in (
        lambda q: resolve_slot_name(None, q, items_res=items_res),  # type: ignore[arg-type]
        lambda q: resolve_slot_from_items(items_res, q),
        lambda q: _resolve_slot_from_items(items_res, q),
    ):
        assert fn("Body") == "Body"
        assert fn("Head") == "Head"
        assert fn("Arm") == "Arm"
        assert fn("Leg") == "Leg"
        assert fn("viscera:body") == "Body"
        assert fn("VISCERA:HEAD") == "Head"
        assert fn("graft:arm") == "Arm"
        assert fn("graft:leg") == "Leg"
        assert fn("Body Armour") == "Body Armour"
        assert fn("body armour") == "Body Armour"
        assert fn("body") == "Body Armour"
        assert fn("chest") == "Body Armour"
        assert fn("胸甲") == "Body Armour"
        assert fn("衣服") == "Body Armour"


def test_pre330_hidden_viscera_slots_behavior() -> None:
    """Pre-3.30 PoB exposes Head/Body/Arm/Leg with shown=False, selItemId=0."""
    pre330_items = {
        "items": [{"id": 1, "name": "Vaal Regalia", "rarity": "RARE"}],
        "slots": [
            {"name": "Body Armour", "selItemId": 1, "shown": True},
            {"name": "Helmet", "selItemId": 0, "shown": True},
            {"name": "Head", "selItemId": 0, "shown": False},
            {"name": "Body", "selItemId": 0, "shown": False},
            {"name": "Arm", "selItemId": 0, "shown": False},
            {"name": "Leg", "selItemId": 0, "shown": False},
        ],
    }
    assert resolve_slot_from_items(pre330_items, "Body") == "Body Armour"
    assert resolve_slot_from_items(pre330_items, "Head") == "Helmet"
    for bad_q in ("Arm", "arm", "Leg", "leg", "viscera:body", "graft:head"):
        with pytest.raises(ValueError, match="not available"):
            resolve_slot_from_items(pre330_items, bad_q)
        with pytest.raises(ValueError, match="not available"):
            _resolve_slot_from_items(pre330_items, bad_q)


def test_node_id_socket_resolution_and_inactive_rejection() -> None:
    items_res = _sample_items_with_empty_viscera_and_sockets()

    for query, expected in (
        ("node:33631", "Jewel 1"),
        ("NODE:55190", "Jewel 2"),
        ("socket:33631", "Jewel 1"),
        ("Socket #55190", "Jewel 2"),
    ):
        assert resolve_slot_name(None, query, items_res=items_res) == expected  # type: ignore[arg-type]
        assert _resolve_slot_from_items(items_res, query) == expected
        assert _is_jewel_slot_name(query) is True

    # Unallocated / inactive socket (nodeId=2491) must be rejected with clear message.
    with pytest.raises(ValueError, match=r"nodeId=2491.*not allocated"):
        resolve_slot_from_items(items_res, "node:2491")
    with pytest.raises(ValueError, match=r"nodeId=2491.*not allocated"):
        _resolve_slot_from_items(items_res, "socket:2491")

    # Unknown nodeId lists only active/allocated socket nodeIds (not inactive 2491).
    with pytest.raises(ValueError, match=r"nodeId=99999.*33631.*55190") as exc_info:
        resolve_slot_from_items(items_res, "node:99999")
    assert "2491" not in str(exc_info.value)


class _BatchSlotFakeWorker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.last_good_code = "CODE_INIT"
        self.added_slots: list[str] = []
        self.unequipped_slots: list[str] = []
        self.tree_calls = 0

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        params = params or {}
        if method == "list_items":
            return _sample_items_with_empty_viscera_and_sockets()
        if method == "get_tree_state":
            self.tree_calls += 1
            return {"allocatedNodes": [33631], "points": {"used": 1, "usedMax": 123}}
        if method == "add_item":
            self.added_slots.append(str(params.get("slotName")))
            return {"ok": True, "item": {"id": 99, "name": "Test Jewel"}}
        if method == "unequip_slot":
            self.unequipped_slots.append(str(params.get("slotName")))
            return {"ok": True}
        if method == "export_code":
            return {"code": self.last_good_code}
        if method == "import_code":
            return {"ok": True}
        if method == "get_build_info":
            return {"level": 90, "className": "Witch", "ascendClassName": "Elementalist", "points": {"used": 1, "usedMax": 123}}
        if method == "get_stats":
            return {"stats": {"CombinedDPS": 1000.0, "TotalEHP": 5000.0, "Life": 4000.0}}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": []}
        if method == "list_config":
            return {"sections": [], "customMods": []}
        return {"ok": True}


def test_batch_resolves_empty_viscera_and_node_socket_and_invalidates_tree() -> None:
    worker = _BatchSlotFakeWorker()
    raw_jewel = "Rarity: RARE\nDoom Eye\nCrimson Jewel\nImplicits: 0\n+7% to maximum Life"
    raw_viscera = "Rarity: UNIQUE\nMaven's Orrery\nViscera\nImplicits: 0"
    res = run_batch(
        worker,  # type: ignore[arg-type]
        [
            {"op": "item", "slot": "Body", "raw": raw_viscera},
            {"op": "item", "slot": "node:33631", "raw": raw_jewel},
        ],
        apply=False,
    )
    assert res["applied"] is False
    assert worker.added_slots == ["Body", "Jewel 1"]

    # Explicitly assert _BatchCache.tree invalidation on equip and unequip via node:<nodeId>.
    cache = _BatchCache(worker)  # type: ignore[arg-type]
    cache.get_tree()
    assert cache.tree is not None and worker.tree_calls == 1
    _op_item(worker, {"op": "item", "slot": "node:33631", "raw": raw_jewel}, cache, 1)  # type: ignore[arg-type]
    assert cache.tree is None

    cache.get_tree()
    assert cache.tree is not None and worker.tree_calls == 2
    _op_unequip(worker, {"op": "unequip", "slot": "node:33631"}, cache, 2)  # type: ignore[arg-type]
    assert cache.tree is None
    assert worker.unequipped_slots == ["Jewel 1"]


def test_compare_structural_diffs_viscera_and_jewel_sockets() -> None:
    main_tree = {
        "points": {"used": 100, "usedMax": 123},
        "keystones": [],
        "ascendancyNodes": [],
        "notables": [],
        "masteries": [],
        "jewelSockets": [
            {"nodeId": 33631, "allocated": True},
            {"nodeId": 55190, "allocated": True},
            {"nodeId": 2491, "allocated": False},
        ],
    }
    ref_tree = {
        "points": {"used": 100, "usedMax": 123},
        "keystones": [],
        "ascendancyNodes": [],
        "notables": [],
        "masteries": [],
        "jewelSockets": [
            {"nodeId": 33631, "allocated": True},
            {"nodeId": 2491, "allocated": True},
        ],
    }
    main_items = {
        "items": [
            {"id": 1, "title": "Maven's Orrery", "rarity": "UNIQUE"},
            {"id": 2, "title": "Watcher's Eye", "baseName": "Prismatic Jewel", "rarity": "UNIQUE"},
            {"id": 3, "title": "Storm Eye", "baseName": "Crimson Jewel", "rarity": "RARE"},
        ],
        "slots": [
            {"name": "Body", "selItemId": 1},
            {"name": "Jewel 1", "socketIndex": 1, "nodeId": 33631, "selItemId": 2},
            {"name": "Jewel 2", "socketIndex": 2, "nodeId": 55190, "selItemId": 3},
            {"name": "Jewel 3", "socketIndex": 3, "nodeId": 2491, "selItemId": 0},
        ],
    }
    ref_items = {
        "items": [
            {"id": 10, "title": "Watcher's Eye", "baseName": "Prismatic Jewel", "rarity": "UNIQUE"},
            {"id": 11, "title": "Lethal Pride", "baseName": "Timeless Jewel", "rarity": "UNIQUE"},
        ],
        "slots": [
            {"name": "Body", "selItemId": 0},
            {"name": "Jewel 1", "socketIndex": 1, "nodeId": 2491, "selItemId": 11},
            {"name": "Jewel 2", "socketIndex": 2, "nodeId": 33631, "selItemId": 10},
        ],
    }
    skills = {"mainSocketGroup": 1, "groups": []}
    _, _, slot_diff, jewel_diff = _build_compare_structural_diffs(
        main_tree, ref_tree, skills, skills, main_items, ref_items
    )

    viscera_rows = [s for s in slot_diff if s["slot"] in ("Head", "Body", "Arm", "Leg")]
    assert len(viscera_rows) == 1
    assert viscera_rows[0]["slot"] == "Body"
    assert viscera_rows[0]["mainItem"] == "Maven's Orrery"
    assert viscera_rows[0]["refItem"] == "-"

    by_nid = {j["nodeId"]: j for j in jewel_diff}
    assert set(by_nid) == {2491, 33631, 55190}
    assert by_nid[33631]["status"] == "same"
    assert by_nid[55190]["status"] == "only_in_main"
    assert by_nid[2491]["status"] == "only_in_ref"

    md = format_compare_markdown(
        {
            "mainBuildInfo": {"level": 95, "ascendClassName": "Elementalist"},
            "refBuildInfo": {"level": 96, "ascendClassName": "Elementalist"},
            "baselines": {"original": [], "envAlignedToMain": {"rows": []}, "pinnacleDewatered": {"rows": []}},
            "treeDiff": {},
            "skillDiff": {},
            "itemSlotDiff": slot_diff,
            "jewelSocketDiff": jewel_diff,
        }
    )
    assert "#### Jewel Sockets Diff (by nodeId)" in md
    assert "node:2491" in md
    assert "Lethal Pride" in md
    assert "Maven's Orrery" in md


def test_compare_omits_viscera_when_neither_build_has_viscera_equipped() -> None:
    empty_tree = {"keystones": [], "ascendancyNodes": [], "notables": [], "masteries": [], "jewelSockets": []}
    empty_items = {
        "items": [],
        "slots": [
            {"name": "Body Armour", "selItemId": 0},
            {"name": "Head", "selItemId": 0},
            {"name": "Body", "selItemId": 0},
        ],
    }
    skills = {"mainSocketGroup": 1, "groups": []}
    _, _, slot_diff, jewel_diff = _build_compare_structural_diffs(
        empty_tree, empty_tree, skills, skills, empty_items, empty_items
    )
    assert all(s["slot"] not in ("Head", "Body", "Arm", "Leg") for s in slot_diff)
    assert jewel_diff == []
    assert callable(compare_builds)


@pytest.mark.skipif(POB_DIR is None, reason="Real runnable PoB engine not available")
def test_real_engine_330_viscera_and_inactive_socket_resolution(tmp_path: Path) -> None:
    from pob_cli import mutators
    from pob_cli.bridge_client import BridgeWorker

    assert POB_DIR is not None
    w = BridgeWorker(slot_name="main", pob_dir=POB_DIR, runtime_dir=tmp_path)
    try:
        w.start()
        mutators.create_new_build(w, class_name="Witch", ascendancy="Elementalist", level=90, apply=True)
        items_res = w.call("list_items", {})
        assert resolve_slot_name(w, "Body", items_res=items_res) == "Body"
        assert resolve_slot_name(w, "body", items_res=items_res) == "Body Armour"

        inactive_sock = next(
            (s for s in items_res.get("slots", []) if s.get("nodeId") is not None and s.get("inactive")),
            None,
        )
        assert inactive_sock is not None
        with pytest.raises(ValueError, match="not allocated"):
            resolve_slot_name(w, f"node:{inactive_sock['nodeId']}", items_res=items_res)
    finally:
        w.stop()
