"""Offline & real-engine tests for CN profile import: clusters, jump jewels, Runegrafts, -60% res, and reconciliation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import pytest

from pob_cli import importers
from pob_cli.profile_import import (
    character_bundle_to_pob_xml,
    reconcile_import_integrity,
    resolve_cluster_hashes,
)

_MAP12_16 = (0, 1, 3, 4, 5, 7, 8, 9, 11, 12, 13, 15)


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
