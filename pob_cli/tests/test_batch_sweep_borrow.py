"""Tests for `pob borrow --tree/--scenario` and `op: "borrow"` in `pob batch` / `pob sweep`."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli.cli_compare import cmd_borrow
from pob_cli.comparator.borrow import borrow_from_ref, capture_ref_bundle
from pob_cli.daemon.rpc_dual import action_needs_ref_slot, dispatch_dual_slot_action
from pob_cli.errors import DaemonRequestError
from pob_cli.mutators.batch_sweep import run_batch, run_sweep
from pob_cli.mutators.batch_validate import validate_batch_op

POB_DIR = find_runnable_engine()


class _FakeBorrowWorker:
    """In-memory fake worker tracking RPC counts, items, skills, tree, and rollback."""

    def __init__(
        self,
        slot_name: str,
        *,
        dps: float = 100_000.0,
        ehp: float = 50_000.0,
        tree_nodes: list[int] | None = None,
        tree_url: str | None = None,
        has_body_viscera: bool = False,
        jewel_raw: str | None = None,
        main_gem: str = "Fireball",
    ) -> None:
        self.slot_name = slot_name
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.last_good_code: str | None = f"CODE_{slot_name.upper()}"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.dps = dps
        self.ehp = ehp
        self.class_id = 3
        self.asc_id = 2
        self.tree_nodes = list(tree_nodes or [54447, 33631])
        self.tree_url = tree_url
        self.has_body_viscera = has_body_viscera
        self.jewel_raw = jewel_raw
        self.main_gem = main_gem
        self.equipped: dict[str, str] = {}
        if has_body_viscera:
            self.equipped["Body"] = "Rarity: UNIQUE\nMaven's Orrery\nViscera\nImplicits: 0"
        if jewel_raw:
            self.equipped["Jewel 1"] = jewel_raw
        self.pasted_groups: list[str] = []
        self.imported_urls: list[str] = []
        self.main_group = 1
        self._saved: dict[str, Any] = self._snap()

    def _snap(self) -> dict[str, Any]:
        return {
            "dps": self.dps,
            "ehp": self.ehp,
            "tree_nodes": list(self.tree_nodes),
            "equipped": dict(self.equipped),
            "pasted_groups": list(self.pasted_groups),
            "main_group": self.main_group,
        }

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "export_code":
            self._saved = self._snap()
            return {"code": self.last_good_code or "CODE"}
        if method == "import_code":
            s = self._saved
            self.dps = s["dps"]
            self.ehp = s["ehp"]
            self.tree_nodes = list(s["tree_nodes"])
            self.equipped = dict(s["equipped"])
            self.pasted_groups = list(s["pasted_groups"])
            self.main_group = s["main_group"]
            return {"ok": True}
        if method == "get_build_info":
            return {
                "level": 95,
                "className": "Witch",
                "ascendClassName": "Elementalist",
                "points": {"used": len(self.tree_nodes), "usedMax": 123},
            }
        if method == "get_stats":
            bonus = 10_000.0 * len(self.equipped) + 5_000.0 * len(self.pasted_groups) + 1_000.0 * len(self.tree_nodes)
            return {
                "stats": {
                    "CombinedDPS": self.dps + bonus,
                    "TotalEHP": self.ehp + bonus * 0.5,
                    "Life": 4500.0,
                }
            }
        if method == "list_items":
            items = []
            slots = [
                {"name": "Body Armour", "selItemId": 0, "shown": True},
                {"name": "Body", "selItemId": 0, "shown": True},
                {"name": "Jewel 1", "socketIndex": 1, "nodeId": 33631, "selItemId": 0, "shown": True, "inactive": False},
            ]
            next_id = 1
            for s in slots:
                raw = self.equipped.get(s["name"])
                if raw:
                    items.append({"id": next_id, "name": raw.splitlines()[1], "type": "Jewel" if "Jewel" in s["name"] else "Viscera", "rarity": "UNIQUE"})
                    s["selItemId"] = next_id
                    next_id += 1
            return {"items": items, "slots": slots}
        if method == "item_raw":
            iid = int(params["id"])
            items_res = self.call("list_items", {})
            for s in items_res["slots"]:
                if s.get("selItemId") == iid:
                    return {"raw": self.equipped[s["name"]]}
            return {"raw": ""}
        if method == "add_item":
            sname = str(params.get("slotName") or "")
            self.equipped[sname] = str(params.get("raw") or "")
            return {"ok": True, "item": {"name": self.equipped[sname].splitlines()[1] if "\n" in self.equipped[sname] else "Item"}}
        if method == "list_skills":
            return {
                "mainSocketGroup": self.main_group,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "slot": "Body Armour",
                        "displayLabel": self.main_gem,
                        "gems": [{"index": 1, "name": self.main_gem, "level": 20, "quality": 20, "enabled": True}],
                    }
                ],
            }
        if method == "copy_group":
            return {"text": f"GROUP_TEXT_{self.main_gem}"}
        if method == "paste_group":
            self.pasted_groups.append(str(params.get("text") or ""))
            idx = 1 + len(self.pasted_groups)
            return {"index": idx, "group": {"index": idx, "displayLabel": self.main_gem}}
        if method == "set_group":
            return {"group": {"index": params.get("index"), "displayLabel": self.main_gem}}
        if method == "set_build_field":
            if params.get("field") == "mainSocketGroup":
                self.main_group = int(params["value"])
            return {"ok": True}
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "classId": self.class_id,
                "className": "Witch",
                "ascendClassId": self.asc_id,
                "ascendClassName": "Elementalist",
                "allocatedNodes": list(self.tree_nodes),
                "points": {"used": len(self.tree_nodes), "usedMax": 123},
            }
        if method == "export_tree_url":
            if self.tree_url:
                return {"url": self.tree_url}
            return {"error": "not supported"}
        if method == "import_tree_url":
            self.imported_urls.append(str(params.get("url") or ""))
            self.tree_nodes = [54447, 33631, 41263, 18769]
            return {"activeSpec": 2}
        if method == "reset_tree":
            self.tree_nodes = [54447]
            return {"allocatedNodes": list(self.tree_nodes)}
        if method == "tree_click":
            nid = int(params["id"])
            if nid not in self.tree_nodes:
                self.tree_nodes.append(nid)
            return {"allocatedNodes": list(self.tree_nodes)}
        if method == "list_config":
            return {"sections": [], "customMods": []}
        return {"ok": True}


def test_borrow_tree_and_no_tree_jewels() -> None:
    main_w = _FakeBorrowWorker("main", tree_nodes=[54447, 33631])
    ref_w = _FakeBorrowWorker(
        "ref",
        tree_nodes=[54447, 33631, 41263],
        jewel_raw="Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0",
    )

    res_with_jewels = borrow_from_ref(main_w, ref_w, borrow_tree=True, borrow_tree_jewels=True, apply=False)  # type: ignore[arg-type]
    assert res_with_jewels["applied"] is False
    assert res_with_jewels["detail"]["borrowedTree"]["allocatedCount"] == 3
    assert [it["slotName"] for it in res_with_jewels["detail"]["borrowedItems"]] == ["Jewel 1"]
    assert main_w.tree_nodes == [54447, 33631]
    assert main_w.equipped == {}

    res_no_jewels = borrow_from_ref(main_w, ref_w, borrow_tree=True, borrow_tree_jewels=False, apply=False)  # type: ignore[arg-type]
    assert res_no_jewels["detail"]["borrowedTree"]["allocatedCount"] == 3
    assert res_no_jewels["detail"]["borrowedItems"] == []


def test_borrow_scenario_and_reject_scenario_with_apply() -> None:
    main_w = _FakeBorrowWorker("main")
    ref_w = _FakeBorrowWorker("ref", has_body_viscera=True)

    with pytest.raises(ValueError, match="cannot be combined with --apply"):
        borrow_from_ref(main_w, ref_w, item_slots=["Body"], scenario="Pinnacle", apply=True)  # type: ignore[arg-type]

    runner = CliRunner()
    cli_res = runner.invoke(cmd_borrow, ["--item-slot", "Body", "--scenario", "Pinnacle", "--apply"])
    assert cli_res.exit_code == 1
    assert "cannot be combined with --apply" in cli_res.output

    cli_keep_err = runner.invoke(cmd_borrow, ["--item-slot", "Helmet", "--keep", "frenzy"])
    assert cli_keep_err.exit_code == 1
    assert "--keep" in cli_keep_err.output and "--scenario" in cli_keep_err.output

    res = borrow_from_ref(main_w, ref_w, item_slots=["Body"], scenario="Pinnacle", apply=False)  # type: ignore[arg-type]
    assert res["applied"] is False
    assert res["scenario"] == "Pinnacle"
    assert res["restoreCheck"]["verified"] is True
    assert res["detail"]["borrowedItems"][0]["slotName"] == "Body"


def test_batch_and_sweep_borrow_capture_ref_once() -> None:
    main_w = _FakeBorrowWorker("main")
    ref_w = _FakeBorrowWorker(
        "ref",
        has_body_viscera=True,
        jewel_raw="Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0",
        tree_nodes=[54447, 33631, 41263],
        tree_url="https://www.pathofexile.com/passive-skill-tree/AAAABgM",
        main_gem="Arc",
    )

    ref_bundle = capture_ref_bundle(ref_w)  # type: ignore[arg-type]
    ref_calls_after_capture = len(ref_w.calls)

    batch_res = run_batch(
        main_w,  # type: ignore[arg-type]
        [{"op": "borrow", "items": ["Body", "node:33631"], "skill_group": "main"}],
        ref_bundle=ref_bundle,
    )
    assert batch_res["applied"] is False
    step0 = batch_res["detail"]["steps"][0]
    assert step0["op"] == "borrow"
    assert [it["slotName"] for it in step0["borrowedItems"]] == ["Body", "Jewel 1"]
    assert step0["borrowedSkillGroup"]["switchedMain"] is True
    assert len(ref_w.calls) == ref_calls_after_capture

    sweep_res = run_sweep(
        main_w,  # type: ignore[arg-type]
        [
            {"name": "Borrow Body", "ops": [{"op": "borrow", "items": ["Body"]}]},
            {"name": "Borrow Jewel", "ops": [{"op": "transplant", "slot": "node:33631"}]},
            {"name": "Borrow Tree", "ops": [{"op": "borrow", "tree": True}]},
        ],
        ref_bundle=ref_bundle,
    )
    assert sweep_res["succeeded"] == 3
    assert sweep_res["failed"] == 0
    assert sweep_res["restoreCheck"]["verified"] is True
    # Zero additional RPCs to ref_worker across all 3 sweep variants!
    assert len(ref_w.calls) == ref_calls_after_capture


def test_borrow_batch_validation_and_daemon_guards() -> None:
    with pytest.raises(ValueError, match="borrows nothing"):
        validate_batch_op(1, "borrow", {"op": "borrow"})
    with pytest.raises(ValueError, match="unknown key"):
        validate_batch_op(1, "borrow", {"op": "borrow", "items": ["Body"], "bogus": 1})
    with pytest.raises(ValueError, match="must be a"):
        validate_batch_op(1, "borrow", {"op": "borrow", "items": 123})
    with pytest.raises(ValueError, match="boolean"):
        validate_batch_op(1, "borrow", {"op": "borrow", "tree": "notabool"})

    assert action_needs_ref_slot("batch", {"ops": [{"op": "borrow", "items": ["Body"]}]}) is True
    assert action_needs_ref_slot("batch", {"ops": [{"op": "config", "set": {"enemyIsBoss": "Pinnacle"}}]}) is False
    assert action_needs_ref_slot("sweep", {"variants": [{"name": "v1", "ops": [{"op": "transplant", "tree": True}]}]}) is True
    assert action_needs_ref_slot("sweep", {"variants": [{"name": "v1", "ops": [{"op": "flask", "index": 1, "active": False}]}]}) is False

    class _DummyMgr:
        @staticmethod
        def _attach_state_meta(res: dict[str, Any], _w: Any) -> dict[str, Any]:
            return res

    main_w = _FakeBorrowWorker("main")
    empty_ref = _FakeBorrowWorker("ref")
    empty_ref.last_good_code = None
    with pytest.raises(DaemonRequestError, match="No reference build loaded in 'ref'"):
        dispatch_dual_slot_action(
            _DummyMgr(),  # type: ignore[arg-type]
            "batch",
            {"ops": [{"op": "borrow", "items": ["Body"]}]},
            main_w,  # type: ignore[arg-type]
            empty_ref,  # type: ignore[arg-type]
        )


@pytest.mark.skipif(POB_DIR is None, reason="Real runnable PoB engine not available")
def test_real_engine_sweep_borrow_pinnacle_zero_residue(tmp_path: Path) -> None:
    from pob_cli import mutators
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.snapshot import capture_digest

    assert POB_DIR is not None
    main_w = BridgeWorker(slot_name="main", pob_dir=POB_DIR, runtime_dir=tmp_path)
    ref_w = BridgeWorker(slot_name="ref", pob_dir=POB_DIR, runtime_dir=tmp_path)
    try:
        main_w.start()
        ref_w.start()

        # Build A (main): Lv.90 Witch/Elementalist with Fireball
        mutators.create_new_build(main_w, class_name="Witch", ascendancy="Elementalist", level=90, apply=True)
        mutators.add_skill_group(main_w, gems=["Fireball:20:20"], label="Main", slot="Body Armour", make_main=True, apply=True)

        # Build B (ref): Lv.90 Witch/Elementalist with Arc + a Ring + a few passive nodes
        mutators.create_new_build(ref_w, class_name="Witch", ascendancy="Elementalist", level=90, apply=True)
        mutators.add_skill_group(ref_w, gems=["Arc:20:20"], label="RefMain", slot="Body Armour", make_main=True, apply=True)
        ring_raw = (
            "Rarity: RARE\nDoom Band\nDiamond Ring\nImplicits: 1\n"
            "25% increased Global Critical Strike Chance\n"
            "+70 to maximum Life\n+40% to Fire Resistance"
        )
        mutators.try_item(ref_w, raw_text=ring_raw, slot_name="Ring 1", apply=True)
        ref_tree = ref_w.call("get_tree_state", {})
        tree_nodes = ref_w.get_cached_tree_data(ref_tree.get("treeVersion")).get("nodes", {})
        alloc_set = {int(x) for x in ref_tree.get("allocatedNodes", [])}
        for nid in list(alloc_set):
            for nb in (tree_nodes.get(str(nid)) or {}).get("out", [])[:2]:
                try:
                    ref_w.call("tree_click", {"id": int(nb)})
                except Exception:
                    pass
        ref_w.last_good_code = ref_w.call("export_code", {})["code"]

        before = capture_digest(main_w)
        ref_bundle = capture_ref_bundle(ref_w)
        sweep_res = run_sweep(
            main_w,
            [
                {"name": "Borrow Ring 1", "ops": [{"op": "borrow", "items": ["Ring 1"]}]},
                {"name": "Borrow Main Skill", "ops": [{"op": "borrow", "skill_group": "main"}]},
                {"name": "Borrow Ref Tree", "ops": [{"op": "borrow", "tree": True}]},
            ],
            scenario="Pinnacle",
            ref_bundle=ref_bundle,
        )
        after = capture_digest(main_w)

        assert sweep_res["succeeded"] == 3, sweep_res["variants"]
        assert sweep_res["failed"] == 0
        assert sweep_res["restoreCheck"]["verified"] is True
        assert after["stats"]["CombinedDPS"] == pytest.approx(before["stats"]["CombinedDPS"], rel=1e-6)
        assert after["stats"]["TotalEHP"] == pytest.approx(before["stats"]["TotalEHP"], rel=1e-6)
    finally:
        main_w.stop()
        ref_w.stop()


class _ClusterSubSocketFakeWorker(_FakeBorrowWorker):
    """Fake worker where cluster sub-socket `nodeId=65537` only materializes AFTER outer socket `2491` has a Cluster Jewel."""

    def __init__(self, slot_name: str, *, with_cluster: bool = False, sec_asc_id: int = 0) -> None:
        self.sec_asc_id = sec_asc_id
        self.set_sec_calls: list[int] = []
        super().__init__(
            slot_name,
            tree_nodes=[54447, 2491, 65537] if with_cluster else [54447],
            tree_url="https://www.pathofexile.com/passive-skill-tree/AAAABgM" if with_cluster else None,
        )
        if with_cluster:
            self.equipped["Jewel 2491"] = "Rarity: RARE\nCluster Base\nLarge Cluster Jewel\nImplicits: 0\nAdds 8 Passive Skills"
            self.equipped["Jewel 65537"] = "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0"
        self._saved = self._snap()

    def _snap(self) -> dict[str, Any]:
        snap = super()._snap()
        snap["sec_asc_id"] = self.sec_asc_id
        return snap

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        params = dict(params or {})
        if method == "import_code":
            res = super().call(method, params, timeout)
            self.sec_asc_id = int(self._saved.get("sec_asc_id", 0))
            return res
        if method == "list_classes":
            return {"classPick": {"classId": 3, "ascendClassId": 2, "secondaryAscendClassId": self.sec_asc_id}}
        if method == "set_secondary_ascendancy":
            self.sec_asc_id = int(params["ascendClassId"])
            self.set_sec_calls.append(self.sec_asc_id)
            return {"ok": True}
        if method == "import_tree_url":
            self.tree_nodes = [54447, 2491]
            return {"activeSpec": 2}
        if method == "list_items":
            slots = [
                {"name": "Jewel 2491", "socketIndex": 1, "nodeId": 2491, "selItemId": 0, "shown": True, "inactive": False}
            ]
            if "Large Cluster Jewel" in self.equipped.get("Jewel 2491", ""):
                slots.append(
                    {"name": "Jewel 65537", "socketIndex": 2, "nodeId": 65537, "selItemId": 0, "shown": True, "inactive": False}
                )
            items = []
            nid_seq = 1
            for s in slots:
                raw = self.equipped.get(s["name"])
                if raw:
                    items.append({"id": nid_seq, "name": raw.splitlines()[1], "type": "Jewel", "rarity": "RARE"})
                    s["selItemId"] = nid_seq
                    nid_seq += 1
            return {"items": items, "slots": slots}
        return super().call(method, params, timeout)


def test_borrow_tree_cluster_subsocket_ordering_and_secondary_asc() -> None:
    main_w = _ClusterSubSocketFakeWorker("main", with_cluster=False, sec_asc_id=0)
    ref_w = _ClusterSubSocketFakeWorker("ref", with_cluster=True, sec_asc_id=3)

    res = borrow_from_ref(main_w, ref_w, borrow_tree=True, borrow_tree_jewels=True, apply=False)  # type: ignore[arg-type]
    assert [it["slotName"] for it in res["detail"]["borrowedItems"]] == ["Jewel 2491", "Jewel 65537"]
    assert main_w.set_sec_calls == [3]
    assert main_w.sec_asc_id == 0
    assert main_w.equipped == {}
    assert res["restoreCheck"]["verified"] is True

    # Explicitly borrowing a cluster sub-socket without its parent cluster jewel must still raise ValueError.
    with pytest.raises(ValueError, match="nodeId=65537"):
        borrow_from_ref(main_w, ref_w, item_slots=["node:65537"], apply=False)  # type: ignore[arg-type]


def test_borrow_tree_reset_and_click_class_mastery_3tier_and_partial_missing() -> None:
    class _ResetClickWorker(_FakeBorrowWorker):
        def __init__(self, slot: str, *, cid: int, aid: int, nodes: list[int], fail_nodes: set[int] | None = None) -> None:
            super().__init__(slot, tree_nodes=nodes, tree_url=None)
            self.class_id = cid
            self.asc_id = aid
            self.fail_nodes = set(fail_nodes or ())
            self.mastery_clicks: list[tuple[int, int]] = []
            self.class_calls: list[int] = []
            self.asc_calls: list[int] = []
            self._saved = self._snap()

        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            params = dict(params or {})
            if method == "set_class":
                self.class_id = int(params["classId"])
                self.class_calls.append(self.class_id)
                return {"ok": True}
            if method == "set_ascendancy":
                self.asc_id = int(params["ascendClassId"])
                self.asc_calls.append(self.asc_id)
                return {"ok": True}
            if method == "get_tree_state":
                res = super().call(method, params, timeout)
                if self.slot_name == "ref":
                    res["masteryEffects"] = {"9001": 555}
                return res
            if method == "tree_click":
                nid = int(params["id"])
                if nid in self.fail_nodes:
                    return {"allocatedNodes": list(self.tree_nodes)}
                if params.get("effect") is not None:
                    self.mastery_clicks.append((nid, int(params["effect"])))
                if nid not in self.tree_nodes:
                    self.tree_nodes.append(nid)
                return {"allocatedNodes": list(self.tree_nodes)}
            if method == "list_items":
                slots = [
                    {"name": "Jewel 2491", "socketIndex": 1, "nodeId": 2491, "selItemId": 0, "shown": True, "inactive": False},
                ]
                if "Large Cluster" in self.equipped.get("Jewel 2491", ""):
                    slots.append({"name": "Jewel 65537", "socketIndex": 2, "nodeId": 65537, "selItemId": 0, "shown": True, "inactive": False})
                if "Medium Cluster" in self.equipped.get("Jewel 65537", ""):
                    slots.append({"name": "Jewel 65600", "socketIndex": 3, "nodeId": 65600, "selItemId": 0, "shown": True, "inactive": False})
                items = []
                seq = 1
                for s in slots:
                    raw = self.equipped.get(s["name"])
                    if raw:
                        items.append({"id": seq, "name": raw.splitlines()[1], "type": "Jewel", "rarity": "RARE"})
                        s["selItemId"] = seq
                        seq += 1
                return {"items": items, "slots": slots}
            return super().call(method, params, timeout)

    main_w = _ResetClickWorker("main", cid=1, aid=1, nodes=[54447], fail_nodes={99991})
    ref_w = _ResetClickWorker("ref", cid=3, aid=2, nodes=[54447, 9001, 2491, 65537, 65600, 99991])
    ref_w.equipped["Jewel 2491"] = "Rarity: RARE\nL\nLarge Cluster Jewel\nImplicits: 0"
    ref_w.equipped["Jewel 65537"] = "Rarity: RARE\nM\nMedium Cluster Jewel\nImplicits: 0"
    ref_w.equipped["Jewel 65600"] = "Rarity: RARE\nS\nCrimson Jewel\nImplicits: 0"

    res = borrow_from_ref(main_w, ref_w, borrow_tree=True, borrow_tree_jewels=True, apply=False)  # type: ignore[arg-type]
    assert main_w.class_calls == [3] and main_w.asc_calls == [2]
    assert (9001, 555) in main_w.mastery_clicks
    assert [it["slotName"] for it in res["detail"]["borrowedItems"]] == ["Jewel 2491", "Jewel 65537", "Jewel 65600"]
    bt = res["detail"]["borrowedTree"]
    assert bt["method"] == "reset_and_click"
    assert bt["allocatedCount"] < bt["targetCount"]
    assert bt["missingNodes"] == [99991]
    assert any("99991" in n for n in res["detail"]["notices"])
    assert res["restoreCheck"]["verified"] is True


def test_borrow_strict_validation_and_source_copy_fail_fast() -> None:
    with pytest.raises(ValueError, match="skill_group"):
        validate_batch_op(1, "borrow", {"op": "borrow", "skill_group": True})
    with pytest.raises(ValueError, match="non-empty"):
        validate_batch_op(1, "borrow", {"op": "borrow", "items": ["   "]})
    with pytest.raises(ValueError, match="conflicting tree aliases"):
        validate_batch_op(1, "borrow", {"op": "borrow", "tree": False, "borrowTree": True})
    with pytest.raises(ValueError, match="conflicting tree_jewels aliases"):
        validate_batch_op(1, "borrow", {"op": "borrow", "tree": True, "tree_jewels": True, "treeJewels": False})
    with pytest.raises(ValueError, match="conflicting skill_group aliases"):
        validate_batch_op(1, "borrow", {"op": "borrow", "skill_group": "main", "skillGroup": "2"})

    # Missing raw text for item-granted skill group must fail fast with ValueError.
    main_w = _FakeBorrowWorker("main")
    bad_source_bundle = {
        "itemsRes": {"items": [], "slots": [{"name": "Weapon 1", "selItemId": 0}]},
        "itemsById": {},
        "slotsByName": {"Weapon 1": {"name": "Weapon 1", "selItemId": 0}},
        "itemRaws": {},
        "skillsRes": {
            "mainSocketGroup": 1,
            "groups": [{"index": 1, "isMain": True, "source": "Weapon 1", "slot": "Weapon 1", "displayLabel": "Death Aura"}],
        },
        "groupTexts": {},
        "treeState": {},
        "treeUrl": None,
    }
    with pytest.raises(ValueError, match="no raw item text"):
        run_batch(main_w, [{"op": "borrow", "skill_group": "main"}], ref_bundle=bad_source_bundle)  # type: ignore[arg-type]

    # Empty copy_group text for non-source skill group must fail fast with RuntimeError.
    bad_copy_bundle = {
        "itemsRes": {"items": [], "slots": []},
        "itemsById": {},
        "slotsByName": {},
        "itemRaws": {},
        "skillsRes": {
            "mainSocketGroup": 1,
            "groups": [{"index": 1, "isMain": True, "slot": "Body Armour", "displayLabel": "Arc"}],
        },
        "groupTexts": {},
        "treeState": {},
        "treeUrl": None,
    }
    with pytest.raises(RuntimeError, match="Failed to copy reference skill group"):
        run_batch(main_w, [{"op": "borrow", "skill_group": "main"}], ref_bundle=bad_copy_bundle)  # type: ignore[arg-type]


def test_sweep_rejects_variant_missing_ops_wrapper_and_prunes_extra_cluster_leaf() -> None:
    main_w = _FakeBorrowWorker("main")
    with pytest.raises(ValueError, match=r"without an 'ops' list"):
        run_sweep(
            main_w,  # type: ignore[arg-type]
            [{"name": "arm_only", "op": "borrow", "items": ["Arm"]}],
        )

    # Pure baseline variant without misplaced keys remains allowed.
    base_res = run_sweep(main_w, [{"name": "baseline"}])  # type: ignore[arg-type]
    assert base_res["succeeded"] == 1

    # Auto-pathed extra cluster leaf node 65701 is pruned cleanly without losing ref nodes.
    class _ExtraLeafWorker(_FakeBorrowWorker):
        def __init__(self, slot: str, *, nodes: list[int], url: str | None = None) -> None:
            super().__init__(slot, tree_nodes=nodes, tree_url=url)
            self._saved = self._snap()

        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            params = dict(params or {})
            if method == "import_tree_url":
                self.tree_nodes = [54447, 33631, 65701]
                return {"activeSpec": 2}
            if method == "tree_click":
                nid = int(params["id"])
                if nid in self.tree_nodes:
                    self.tree_nodes.remove(nid)
                else:
                    self.tree_nodes.append(nid)
                return {"allocatedNodes": list(self.tree_nodes)}
            return super().call(method, params, timeout)

    m_leaf = _ExtraLeafWorker("main", nodes=[54447])
    r_leaf = _ExtraLeafWorker("ref", nodes=[54447, 33631], url="https://www.pathofexile.com/passive-skill-tree/AAAABgM")
    res_leaf = borrow_from_ref(m_leaf, r_leaf, borrow_tree=True, borrow_tree_jewels=False, apply=False)  # type: ignore[arg-type]
    assert res_leaf["detail"]["borrowedTree"]["allocatedCount"] == 2
    assert res_leaf["detail"]["borrowedTree"]["targetCount"] == 2
    assert "missingNodes" not in res_leaf["detail"]["borrowedTree"]


@pytest.mark.skipif(POB_DIR is None, reason="Real runnable PoB engine not available")
def test_real_engine_borrow_tree_with_cluster_jewel_subsocket(tmp_path: Path) -> None:
    from pob_cli import mutators
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.snapshot import capture_digest

    assert POB_DIR is not None
    main_w = BridgeWorker(slot_name="main", pob_dir=POB_DIR, runtime_dir=tmp_path)
    ref_w = BridgeWorker(slot_name="ref", pob_dir=POB_DIR, runtime_dir=tmp_path)
    try:
        main_w.start()
        ref_w.start()

        mutators.create_new_build(main_w, class_name="Witch", ascendancy="Elementalist", level=90, apply=True)
        mutators.add_skill_group(main_w, gems=["Fireball:20:20"], label="Main", slot="Body Armour", make_main=True, apply=True)

        mutators.create_new_build(ref_w, class_name="Witch", ascendancy="Elementalist", level=90, apply=True)
        mutators.add_skill_group(ref_w, gems=["Fireball:20:20"], label="Main", slot="Body Armour", make_main=True, apply=True)
        # Find an outer Large Cluster socket dynamically and allocate it.
        ref_tree = ref_w.call("get_tree_state", {})
        outer_sock = next(
            (s for s in ref_tree.get("sockets", []) if s.get("expansion") and s.get("expansionSize") == 2),
            None,
        )
        assert outer_sock is not None
        outer_nid = int(outer_sock["nodeId"])
        ref_w.call("tree_click", {"id": outer_nid})
        large_cluster_raw = (
            "Rarity: RARE\nDoom Curio\nLarge Cluster Jewel\n"
            "Item Level: 84\n"
            "Implicits: 3\n"
            "{crafted}Adds 8 Passive Skills\n"
            "{crafted}2 Added Passive Skills are Jewel Sockets\n"
            "{crafted}Added Small Passive Skills grant: 12% increased Fire Damage\n"
            "+10 to Intelligence"
        )
        ref_items_before = ref_w.call("list_items", {})
        outer_slot = next(
            (s["name"] for s in ref_items_before.get("slots", []) if s.get("nodeId") is not None and int(s["nodeId"]) == outer_nid),
            None,
        )
        assert outer_slot is not None
        ref_w.call("add_item", {"raw": large_cluster_raw, "slotName": outer_slot, "equip": True})
        ref_tree_after = ref_w.call("get_tree_state", {})
        sub_node = next(
            (n for n in ref_tree_after.get("dynamicNodes", []) if "Jewel Socket" in str(n.get("name") or "")),
            None,
        )
        assert sub_node is not None, "Large Cluster Jewel should materialize an inner Jewel Socket dynamic node"
        sub_nid = int(sub_node["id"])
        ref_w.call("tree_click", {"id": sub_nid})
        ref_items = ref_w.call("list_items", {})
        sub_sock = next(
            (s for s in ref_items.get("slots", []) if s.get("nodeId") is not None and int(s["nodeId"]) == sub_nid),
            None,
        )
        assert sub_sock is not None
        inner_jewel_raw = "Rarity: RARE\nBlight Eye\nCrimson Jewel\nImplicits: 0\n+7% to maximum Life"
        ref_w.call("add_item", {"raw": inner_jewel_raw, "slotName": sub_sock["name"], "equip": True})
        ref_w.last_good_code = ref_w.call("export_code", {})["code"]

        before = capture_digest(main_w)
        res = borrow_from_ref(main_w, ref_w, borrow_tree=True, borrow_tree_jewels=True, apply=False)
        after = capture_digest(main_w)

        borrowed_slots = [it["slotName"] for it in res["detail"]["borrowedItems"]]
        assert outer_slot in borrowed_slots
        assert sub_sock["name"] in borrowed_slots
        assert res["applied"] is False
        assert not res.get("restoreWarning")
        assert after["stats"]["Life"] == pytest.approx(before["stats"]["Life"], rel=1e-6)
    finally:
        main_w.stop()
        ref_w.stop()
