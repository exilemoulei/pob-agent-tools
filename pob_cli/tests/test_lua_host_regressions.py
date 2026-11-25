"""Real-engine regressions for the native Lua host's own RPCs (lua/*.lua beside headless_runner).

Each test drives the shipped example build through a real engine and skips without one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from engine_paths import find_runnable_engine

from pob_cli.bridge_client import BridgeWorker

ENGINE = find_runnable_engine()
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "builds"
pytestmark = pytest.mark.skipif(ENGINE is None, reason="no runnable engine")


def _example_code() -> str:
    return (EXAMPLE / "dark_bargain_ci_skeleton_330_clean.code.txt").read_text(encoding="utf-8").strip()


def _example_nodes() -> list[int]:
    xml = (EXAMPLE / "dark_bargain_ci_skeleton_330_clean.xml").read_text(encoding="utf-8")
    spec = re.search(r"<Spec [^>]*\bnodes=\"([\d,]+)\"", xml)
    assert spec is not None
    return [int(n) for n in spec.group(1).split(",")]


@pytest.fixture(scope="module")
def worker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BridgeWorker]:
    assert ENGINE is not None
    w = BridgeWorker("lua-host-regressions", pob_dir=ENGINE, runtime_dir=tmp_path_factory.mktemp("rt"))
    w.start()
    yield w
    w.stop()


def _load_example(worker: BridgeWorker) -> None:
    worker.call("import_code", {"code": _example_code(), "mode": "replace"})


def test_import_character_json_lands_in_the_build(worker: BridgeWorker) -> None:
    # The host advertised importCharacterJson but read the build from `launch`, which has none.
    if not worker.caps.get("importCharacterJson"):
        pytest.skip("host has no import_character_json")
    worker.call("import_code", {"code": _example_code(), "mode": "new"})  # named "Imported build"
    name_before = worker.call("get_build_info", {})["buildName"]
    assert name_before != "Unnamed build"
    nodes = _example_nodes()
    ring = {
        "inventoryId": "Ring", "frameType": 2, "name": "Regression Loop", "typeLine": "Ruby Ring",
        "baseType": "Ruby Ring", "ilvl": 84, "identified": True, "x": 0, "y": 0, "w": 1, "h": 1,
        "implicitMods": ["+30% to Fire Resistance"], "explicitMods": ["+75 to maximum Life"],
    }
    res = worker.call(
        "import_character_json",
        {
            "char": {"name": "RegressionHero", "league": "Standard", "class": "Occultist", "level": 91},
            "passiveSkills": {"hashes": nodes, "hashes_ex": [], "mastery_effects": {}, "items": []},
            "items": {"items": [ring]},
        },
        timeout=60.0,
    )
    assert res["imported"] is True
    info = worker.call("get_build_info", {})
    assert info["className"] == "Witch" and info["ascendClassName"] == "Occultist" and info["level"] == 91
    assert info["buildName"] == name_before  # a fresh build, under the previous build's name and file
    # Cluster subgraph nodes need their jewels and masteries their effects; the rest is allocated.
    allocated = {int(n) for n in worker.call("get_tree_state", {})["allocatedNodes"]}
    assert allocated <= set(nodes) and len(allocated) >= 100
    items = worker.call("list_items", {})
    by_id = {it["id"]: it for it in items["items"]}
    ring_slot = next(s for s in items["slots"] if s["name"] == "Ring 1")
    assert by_id[ring_slot["selItemId"]]["title"] == "Regression Loop"
    # The import replaced the build: nothing of the example's gear is left.
    assert len(items["items"]) == 1
    assert worker.call("get_stats", {})["stats"]["Life"] > 0


def test_agent_probe_build_reads_conditions_with_var_lists(worker: BridgeWorker) -> None:
    # Mods conditioned on `varList` crashed the probe in ModStore:GetCondition.
    if not worker.caps.get("agentProbes"):
        pytest.skip("host has no agent probes")
    _load_example(worker)
    res = worker.call("agent_probe_build", {})
    assert res["slotsInspected"] > 0
    assert set(res["resistSlack"]) == {"Fire", "Cold", "Lightning", "Chaos"}
    gen = worker.call("trade_generate_query", {"slotName": "Ring 1"}, timeout=120.0)
    assert gen["slotConstraints"]["slot"] == "Ring 1"


def test_cow_restore_resets_weapon_swap(worker: BridgeWorker) -> None:
    if not worker.caps.get("cowSnapshot"):
        pytest.skip("host has no CoW snapshots")
    _load_example(worker)
    before = worker.call("get_stats", {})["stats"]["FullDPS"]
    snap = worker.call("cow_snapshot", {})["snapId"]
    worker.call("set_weapon_swap", {"on": True})
    assert worker.call("get_stats", {})["stats"]["FullDPS"] != before
    worker.call("cow_restore", {"snapId": snap, "drop": True})
    assert worker.call("list_items", {})["useSecondWeaponSet"] is False
    assert worker.call("get_stats", {})["stats"]["FullDPS"] == pytest.approx(before, rel=1e-9)
