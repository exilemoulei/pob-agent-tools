"""Offline & real-engine tests for CN profile import: clusters, jump jewels, Runegrafts, -60% res, and reconciliation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import pytest
from engine_paths import find_runnable_engine, has_zh_data

from pob_cli import importers
from pob_cli.bridge_client import BridgeWorker
from pob_cli.importers_codec import xml_to_share_code
from pob_cli.profile_import import (
    Zh2EnDictionary,
    character_bundle_to_pob_xml,
    reconcile_import_integrity,
    resolve_cluster_hashes,
    translate_cn_bundle,
)
from pob_cli.profile_import.cluster_hashes import load_tree_cluster_meta

_MAP12_16 = (0, 1, 3, 4, 5, 7, 8, 9, 11, 12, 13, 15)
_PATH_7960 = [61525, 63965, 14151, 27564, 17735, 58402, 6764, 40366, 42760, 36678, 20987, 52502, 7960]
_PATH_55190 = [61525, 20228, 39916, 35556, 44908, 2913, 50862, 50422, 50570, 29353, 35288, 26712, 55190]
_PATH_MASTERY = [61525, 20228, 39916, 35556, 44908, 2913, 55913, 45646, 35192, 55002, 10245]
_PATH_33631 = [61525, 20228, 39916, 35556, 44908, 2913, 50862, 15405, 33631]
_PATH_36634 = [61525, 63965, 14151, 27564, 17735, 13009, 60398, 7388, 36634]
_PATH_26196 = [61525, 20228, 39916, 35556, 44908, 31931, 15064, 29199, 37999, 26196]


def _make_large_sg(
    proxy_id: int,
    start_oidx: int,
    gid: str,
    base_ex_id: int,
    child_socket: tuple[int, dict[str, int]] | None = None,
) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    g_nodes: list[str] = []
    for t in range(12):
        ex_id = base_ex_id + t
        if t == 4 and child_socket is not None:
            csid, cinfo = child_socket
            nodes[str(csid)] = {
                "skill": str(csid), "name": "Medium Jewel Socket", "isJewelSocket": True,
                "expansionJewel": {k: str(v) if k in ("parent", "proxy") else v for k, v in cinfo.items()},
                "group": gid, "orbit": 3, "orbitIndex": _MAP12_16[(t + start_oidx) % 12],
            }
            g_nodes.append(str(csid))
        elif t == 8:
            continue
        else:
            nodes[str(ex_id)] = {
                "skill": str(ex_id), "name": "Minion Damage", "group": gid,
                "orbit": 3, "orbitIndex": _MAP12_16[(t + start_oidx) % 12],
            }
            g_nodes.append(str(ex_id))
    return {"groups": {gid: {"proxy": str(proxy_id), "nodes": g_nodes}}, "nodes": nodes}


def _make_small_sg(proxy_id: int, start_oidx: int, gid: str) -> dict[str, Any]:
    return {
        "groups": {gid: {"proxy": str(proxy_id), "nodes": ["5001", "5002", "5003"]}},
        "nodes": {
            "5001": {"skill": "5001", "name": "Life", "group": gid, "orbit": 1, "orbitIndex": (0 + start_oidx) % 6},
            "5002": {"skill": "5002", "name": "Life", "group": gid, "orbit": 1, "orbitIndex": (4 + start_oidx) % 6},
            "5003": {"skill": "5003", "name": "Life", "group": gid, "orbit": 1, "orbitIndex": (2 + start_oidx) % 6},
        },
    }


def test_cluster_hashes_bit_exact_synthetic_ids_and_xml_roundtrip() -> None:
    child_info = {"index": 0, "parent": 7960, "proxy": 55706, "size": 1}
    sg1 = _make_large_sg(43989, 5, "exp_1", 1000, (29712, child_info))
    sm_sg = _make_small_sg(55706, 2, "exp_sm")
    passives = {
        "character": 5, "ascendancy": 3,
        "hashes": [61525, 7960, 10245],
        "hashes_ex": [1000, 1001, 29712, 5001, 5002, 5003],
        "mastery_effects": {},
        "skill_overrides": {"10245": {"name": "Runegraft of Bellows"}},
        "jewel_data": {
            "11": {"type": "JewelPassiveTreeExpansionLarge", "subgraph": sg1},
            "21": {"type": "JewelPassiveTreeExpansionSmall", "subgraph": sm_sg},
        },
        "items": [],
    }
    resolved = resolve_cluster_hashes(passives)
    assert resolved == [65568, 65569, 29712, 65536, 65540, 65538]

    xml_lv97 = character_bundle_to_pob_xml({"char": {"class": "Guardian", "level": 97}, "passiveSkills": passives, "items": {"items": []}})
    root97 = ET.fromstring(xml_lv97)
    spec_el = root97.find(".//Spec") or root97
    nodes_attr = spec_el.attrib["nodes"]
    assert "65568" in nodes_attr and "65536" in nodes_attr and "1000" not in nodes_attr.split(",")
    assert "10245," in spec_el.attrib["masteryEffects"]
    res_in = root97.find(".//ConfigSet/Input[@name='resistancePenalty']")
    assert res_in is not None and res_in.attrib.get("number") == "-60"

    xml_lv40 = character_bundle_to_pob_xml({"char": {"class": "Guardian", "level": 40}, "passiveSkills": passives, "items": {"items": []}})
    assert ET.fromstring(xml_lv40).find(".//ConfigSet/Input[@name='resistancePenalty']") is None


def test_reconcile_integrity_and_load_exposes_raw_hash_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    long_enchant = "Added Small Passive Skills grant: " + ("x" * 140)
    bundle = {
        "char": {"name": "Hero", "league": "Covens", "class": "Guardian", "level": 97},
        "passiveSkills": {
            "hashes": [1, 2, 3], "hashes_ex": [65568, 65569],
            "items": [{"x": 11, "baseType": "Large Cluster Jewel", "enchantMods": [long_enchant]}],
        },
        "items": {"items": []},
    }
    ok_res = {
        "imported": True, "allocatedNodeIds": [1, 2, 3, 65568, 65569], "droppedHashes": [],
        "clusterJewels": [{"nodeId": 7960, "name": "Large", "valid": True, "rawMods": []}],
    }
    warns_ok, rh, rex = reconcile_import_integrity(bundle, ok_res)
    assert warns_ok == [] and rh == 3 and rex == 2

    bad_res = {
        "imported": True, "allocatedNodeIds": [1, 2], "droppedHashes": [3],
        "clusterJewels": [{"nodeId": 7960, "name": "Broken", "valid": False, "rawMods": ["未知词缀"]}],
    }
    warns_bad, _, _ = reconcile_import_integrity(bundle, bad_res)
    assert any("[IMPORT-RECONCILE-WARN]" in w and "7960" in w and "未知词缀" in w for w in warns_bad)
    assert any("2/2 cluster nodes were not allocated" in w and "..." in w for w in warns_bad)
    assert any("unallocated mastery/runegraft overrides" in w and "3" in w for w in warns_bad)

    class _W:
        def __init__(self) -> None:
            self.caps = {"importCharacterJson": True}
            self.pob_root = None
            self.lock = self._L()

        class _L:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_a: Any) -> None:
                return None

        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
            if method == "import_character_json":
                return ok_res
            if method == "get_build_info":
                return {"className": "Templar", "ascendClassName": "Guardian", "level": 97, "treeVersion": "3_30"}
            return {"mainSocketGroup": {"index": 1, "list": []}, "mainSkill": {"index": 1, "list": []}}

    monkeypatch.setattr("pob_cli.profile_import.fetch_intl_characters", lambda *_a, **_k: ([bundle["char"]], False))
    monkeypatch.setattr("pob_cli.profile_import.fetch_intl_character_bundle", lambda *_a, **_k: bundle)
    out = importers.load_build_into_worker(_W(), source="https://www.pathofexile.com/account/view-profile/foo-1234", char_name="Hero")  # type: ignore[arg-type]
    assert out["rawHashesCount"] == 3 and out["rawHashesExCount"] == 2


@pytest.mark.skipif(
    find_runnable_engine() is None or not has_zh_data(find_runnable_engine()),
    reason="requires runnable PoB 2.69.0 engine with zh-rCN data",
)
def test_real_engine_cn_clusters_runegraft_putrid_seed_tattoos_and_lv97_res_penalty() -> None:
    pob_root = find_runnable_engine()
    assert pob_root is not None
    meta = load_tree_cluster_meta(pob_root, "3_30")
    slot_to_x = {nid: idx for idx, nid in enumerate(meta.jewel_slots)}
    child_med_sid, child_med_info = next(
        (sid, info) for sid, info in meta.exp_sockets.items() if info.get("parent") == 7960 and info.get("index") == 0
    )
    child_med_x = slot_to_x[child_med_sid]

    sg1 = _make_large_sg(43989, 5, "exp_1", 1000, (child_med_sid, child_med_info))
    sg2 = _make_large_sg(30275, 3, "exp_2", 2000)
    sm_sg = _make_small_sg(child_med_info["proxy"], meta.orbit_offsets[child_med_info["proxy"]][0], "exp_sm")
    hashes_ex = [1000, 1001, 1002, 1003, child_med_sid, 5001, 5002, 5003, 2000, 2001, 2002]
    main_hashes = list(dict.fromkeys(_PATH_7960 + _PATH_55190 + _PATH_MASTERY))

    cn_bundle = {
        "char": {"name": "CNGuardian", "class": "守护者", "level": 97, "league": "永火之咒"},
        "items": {"items": []},
        "passiveSkills": {
            "character": 5, "ascendancy": 3,
            "hashes": main_hashes, "hashes_ex": hashes_ex, "mastery_effects": {},
            "skill_overrides": {
                "10245": {"name": "咆哮之符文嫁接"},
                "20228": {"name": "巨蜥的尊贵文身"},
                "39916": {"name": "已删除的远古测试纹身"},
                "35556": {"name": "Nonexistent Legacy Override"},
            },
            "jewel_data": {
                "11": {"type": "JewelPassiveTreeExpansionLarge", "subgraph": sg1},
                str(child_med_x): {"type": "JewelPassiveTreeExpansionSmall", "subgraph": sm_sg},
                "13": {"type": "JewelPassiveTreeExpansionLarge", "subgraph": sg2},
            },
            "items": [
                {
                    "id": "c1", "ilvl": 84, "x": 11, "inventoryId": "PassiveJewels", "frameType": 2, "name": "星团一号", "baseType": "大型星团珠宝",
                    "enchantMods": ["增加 12 个天赋技能", "其中 2 个增加的天赋为珠宝槽", "增加的小天赋获得：召唤生物的伤害提高 10%"],
                    "explicitMods": ["增加的小天赋效果提高 35%"],
                },
                {
                    "id": "c2", "ilvl": 84, "x": child_med_x, "inventoryId": "PassiveJewels", "frameType": 2, "name": "小星团", "baseType": "小型星团珠宝",
                    "enchantMods": ["增加 3 个天赋技能", "增加的小天赋获得：生命上限提高 4%"],
                    "explicitMods": ["增加的小天赋还获得：+10 最大生命"],
                },
                {
                    "id": "c3", "ilvl": 84, "x": 13, "inventoryId": "PassiveJewels", "frameType": 2, "name": "星团二号", "baseType": "大型星团珠宝",
                    "enchantMods": ["增加 12 个天赋技能", "其中 2 个增加的天赋为珠宝槽", "增加的小天赋获得：锤类或短杖攻击造成的击中和异常状态伤害提高 12%\n长杖攻击造成的击中和异常状态伤害提高 12%"],
                    "explicitMods": ["其中 1 个增加的天赋为复兴"],
                },
            ],
        },
    }
    d = Zh2EnDictionary.from_engine_dir(pob_root)
    en_bundle, tw = translate_cn_bundle(cn_bundle, d)
    assert tw == ["[CN-TRANSLATE-WARN] skill_overrides[39916]: 已删除的远古测试纹身"]
    expected_ex = resolve_cluster_hashes(en_bundle["passiveSkills"], pob_root=pob_root)

    w = BridgeWorker(slot_name="test", pob_root=pob_root)
    w.start(restore_code="")
    try:
        res_lua = w.call("import_character_json", {"char": en_bundle["char"], "passiveSkills": en_bundle["passiveSkills"], "items": en_bundle["items"]})
        lua_alloc = set(res_lua["allocatedNodeIds"])
        assert all(nid in lua_alloc for nid in expected_ex), (
            [n for n in expected_ex if n not in lua_alloc],
            res_lua.get("clusterJewels"),
            res_lua.get("warnings"),
        )
        assert 10245 in lua_alloc
        assert any("[CN-TRANSLATE-WARN]" in m and "39916" in m for m in res_lua["warnings"])
        assert any("[IMPORT-OVERRIDE-SKIP]" in m and "35556" in m for m in res_lua["warnings"])
        rec_warns, rh, rex = reconcile_import_integrity(en_bundle, res_lua, pob_root=pob_root)
        assert rec_warns == [] and rh == len(main_hashes) and rex == len(hashes_ex)
        st = w.call("get_stats", {"keys": ["FireResist", "ColdResist", "LightningResist"]})["stats"]
        assert st["FireResist"] == -60 and st["ColdResist"] == -60 and st["LightningResist"] == -60

        # CoW snapshot round-trip: export_code -> import_code must preserve Runegraft 10245 & cluster synthetic IDs
        rt_code = w.call("export_code", {})["code"]
        w.call("import_code", {"code": rt_code, "mode": "replace"})
        rt_alloc = {int(n) for n in w.call("get_tree_state", {})["allocatedNodes"]}
        assert 10245 in rt_alloc and all(nid in rt_alloc for nid in expected_ex)

        # XML fallback parity: cluster synthetic IDs + Runegraft mastery + Lv97 -60% res penalty
        xml_text = character_bundle_to_pob_xml(en_bundle, pob_root=pob_root)
        w.call("import_code", {"code": xml_to_share_code(xml_text), "mode": "replace"})
        xml_alloc = {int(n) for n in w.call("get_tree_state", {})["allocatedNodes"]}
        assert all(nid in xml_alloc for nid in expected_ex)
        assert 10245 in xml_alloc
        st_xml = w.call("get_stats", {"keys": ["FireResist"]})["stats"]
        assert st_xml["FireResist"] == -60
    finally:
        w.stop()


@pytest.mark.skipif(
    find_runnable_engine() is None or not has_zh_data(find_runnable_engine()),
    reason="requires runnable PoB 2.69.0 engine with zh-rCN data",
)
def test_real_engine_cn_jump_jewels_putrid_seed_and_broken_cluster_warn() -> None:
    pob_root = find_runnable_engine()
    assert pob_root is not None
    d = Zh2EnDictionary.from_engine_dir(pob_root)
    jump_hashes = list(dict.fromkeys(_PATH_33631 + _PATH_36634 + _PATH_26196 + _PATH_7960 + [23185, 55866, 4713]))
    cn_jump = {
        "char": {"name": "JumpSeed", "class": "守护者", "level": 95, "league": "永火之咒"},
        "items": {"items": []},
        "passiveSkills": {
            "character": 5, "ascendancy": 3, "hashes": jump_hashes, "hashes_ex": [], "mastery_effects": {},
            "items": [
                {
                    "id": "il1", "ilvl": 84, "x": 19, "inventoryId": "PassiveJewels", "frameType": 3, "name": "直觉之跃", "baseType": "翠绿珠宝",
                    "properties": [{"name": "范围", "values": [["小", 0]]}],
                    "explicitMods": ["范围内的天赋技能无需连接到你的天赋树即可配置\n通途"],
                },
                {
                    "id": "th1", "ilvl": 84, "x": 1, "inventoryId": "PassiveJewels", "frameType": 3, "name": "希望之线", "baseType": "赤红珠宝",
                    "properties": [{"name": "范围", "values": [["可变（巨型）", 0]]}],
                    "explicitMods": ["只影响巨型圈内的天赋", "范围内的天赋技能无需连接到你的天赋树即可配置\n所有元素抗性 -15%\n通途"],
                },
                {
                    "id": "ie1", "ilvl": 84, "x": 18, "inventoryId": "PassiveJewels", "frameType": 3, "name": "绝望逃脱", "baseType": "翠绿珠宝",
                    "properties": [{"name": "范围", "values": [["小", 0]]}],
                    "explicitMods": ["狂热誓言范围内的核心天赋技能可以在\n未连结至天赋树的情况下配置\n通途"],
                },
                {
                    "id": "ps1", "ilvl": 84, "x": 11, "inventoryId": "PassiveJewels", "frameType": 3, "name": "腐败之种", "baseType": "菌丝核心",
                    "enchantMods": ["其中 1 个增加的天赋为灭世之语"],
                },
            ],
        },
    }
    en_jump, tw = translate_cn_bundle(cn_jump, d)
    assert tw == []
    w = BridgeWorker(slot_name="test", pob_root=pob_root)
    w.start(restore_code="")
    try:
        res = w.call("import_character_json", {"char": en_jump["char"], "passiveSkills": en_jump["passiveSkills"], "items": en_jump["items"]})
        alloc = set(res["allocatedNodeIds"])
        assert {23185, 55866, 4713}.issubset(alloc)
        assert reconcile_import_integrity(en_jump, res, pob_root=pob_root)[0] == []

        bad_bundle = {
            "char": {"name": "BadCluster", "class": "未知职业", "level": 90, "league": "Standard"},
            "items": {"items": []},
            "passiveSkills": {
                "character": 5, "ascendancy": 3, "hashes": _PATH_7960, "hashes_ex": [], "mastery_effects": {},
                "items": [{
                    "id": "bad1", "ilvl": 84, "x": 11, "inventoryId": "PassiveJewels", "frameType": 2, "name": "Bad", "baseType": "Large Cluster Jewel", "typeLine": "Large Cluster Jewel",
                    "enchantMods": ["Adds 12 Passive Skills", "增加的小天赋获得：完全未知的假词缀"],
                }],
            },
        }
        res_bad = w.call("import_character_json", {"char": bad_bundle["char"], "passiveSkills": bad_bundle["passiveSkills"], "items": bad_bundle["items"]})
        bi_bad = w.call("get_build_info", {})
        assert bi_bad["className"] == "Templar" and bi_bad["ascendClassName"] == "Guardian"
        warns_bad, _, _ = reconcile_import_integrity(bad_bundle, res_bad, pob_root=pob_root)
        assert any("[IMPORT-RECONCILE-WARN]" in m and "7960" in m and "完全未知的假词缀" in m for m in warns_bad)
    finally:
        w.stop()
