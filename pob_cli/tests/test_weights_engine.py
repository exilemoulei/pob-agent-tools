"""Real-engine `pob weights`: probe deltas come from PoB and the build is left untouched (skips without an engine)."""

from __future__ import annotations

import pytest
from engine_paths import find_runnable_engine

from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli_weights import build_preset_variants, build_weight_variants, compute_weights
from pob_cli.mutators import run_sweep
from pob_cli.snapshot import history_of

POB_DIR = find_runnable_engine()
_KEYS = ("CombinedDPS", "TotalEHP", "Life", "FireResist", "ColdResist")


@pytest.fixture(scope="module")
def live_worker():
    if POB_DIR is None:
        pytest.skip("No runnable PoB engine found")
    worker = BridgeWorker(slot_name="test", pob_dir=POB_DIR)
    try:
        worker.start()
        witch = next(c for c in worker.call("list_classes", {})["classes"] if c["name"] == "Witch")
        worker.call("set_class", {"classId": witch["id"]})
        worker.call("add_group", {"label": "Main Fireball"})
        worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 1})
        history_of(worker).load_build(worker.call("export_code", {})["code"], None)
        yield worker
    finally:
        worker.stop()


def _state(worker: BridgeWorker) -> tuple[int, dict[str, object], str]:
    stats = worker.call("get_stats", {})["stats"]
    return history_of(worker).rev, {k: stats.get(k) for k in _KEYS}, worker.call("export_code", {})["code"]


def test_live_weights_probe_deltas_and_zero_residue(live_worker: BridgeWorker) -> None:
    before = _state(live_worker)
    res = run_sweep(live_worker, build_weight_variants(["20% increased Spell Damage", "+50 to maximum Life"]))
    after = _state(live_worker)

    assert res["restoreCheck"]["verified"] is True
    w = {r["mod"]: r for r in compute_weights(res)["weights"]}
    assert w["20% increased Spell Damage"]["dpsPct"] > 0
    assert w["+50 to maximum Life"]["ehpPct"] > 0
    assert w["+50 to maximum Life"]["dpsPct"] == pytest.approx(0.0, abs=1e-9)
    assert after[0] == before[0]  # rev unchanged: a what-if never commits
    for key in _KEYS:
        assert after[1][key] == pytest.approx(before[1][key], rel=1e-9), key
    assert after[2] == before[2]


def test_live_weights_bandit_preset_zero_residue(live_worker: BridgeWorker) -> None:
    before = _state(live_worker)
    res = run_sweep(live_worker, build_preset_variants("bandit"))
    after = _state(live_worker)

    assert res["restoreCheck"]["verified"] is True
    assert all(v["ok"] for v in res["variants"]), [v.get("error") for v in res["variants"]]
    assert after == before
