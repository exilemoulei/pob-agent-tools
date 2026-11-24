"""Trigger-cliff selection by main group, the compare attribution budget, and the empty-weights
guard of the surrogate pre-filter."""

from __future__ import annotations

from typing import Any

import pytest
from test_agent_probes_and_kernels import _CapsFakeWorker

from pob_cli import comparator
from pob_cli.analyzer.diagnose_probes import _extract_trigger_attack_aps
from pob_cli.trade_eval.evaluator import _prefilter_raw_items_for_eval

_NO_COC_GROUP = {"mainSocketGroup": 1, "groups": [{"index": 1, "isMain": True, "enabled": True, "gems": []}]}


def _cliff(trigger: str, aps: float, *, main: bool) -> dict[str, Any]:
    return {"skillName": "Spell", "triggerName": trigger, "sourceAps": aps, "mainGroup": main}


def _aps(cliffs: list[dict[str, Any]]) -> float | None:
    worker = _CapsFakeWorker(caps={"agentProbes": True})
    return _extract_trigger_attack_aps(
        worker, _NO_COC_GROUP, {"items": [], "slots": []}, {}, agent_probe={"triggerCliffs": cliffs}
    )


def test_trigger_aps_comes_from_the_main_group_cliff() -> None:
    off_group = _cliff("Cast On Critical Strike Support", 3.0, main=False)
    main_group = _cliff("Cast On Critical Strike Support", 8.0, main=True)
    assert _aps([off_group, main_group]) == pytest.approx(8.0)
    # A CoC setup in another group says nothing about the main skill: use the old measurement,
    # which finds no CoC in this main group.
    assert _aps([off_group]) is None
    # Item triggers fire from the build's attack wherever the spells sit.
    assert _aps([off_group, _cliff("Cospri's Malice", 5.46, main=False)]) == pytest.approx(5.46)
    assert _aps([_cliff("Cospri's Malice", 5.46, main=False), main_group]) == pytest.approx(8.0)


def test_compare_attribution_uses_only_the_remaining_budget() -> None:
    mw = _CapsFakeWorker(caps={"powerAttribution": True})
    rw = _CapsFakeWorker(caps={"powerAttribution": True})
    res = comparator.compare_builds(mw, rw, budget_s=5.0)
    assert "powerAttribution" not in res
    assert res["powerAttributionError"].startswith("skipped:")
    assert "power_diff_attribution" not in [m for m, _ in mw.calls]
    assert res["baselines"]["original"]

    timeouts: list[float] = []

    class _SlowWorker(_CapsFakeWorker):
        def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
            if method == "power_diff_attribution":
                timeouts.append(float(kw["timeout"]))
                raise TimeoutError("worker timed out")
            return super().call(method, params, **kw)

    res = comparator.compare_builds(_SlowWorker(caps={"powerAttribution": True}), rw, budget_s=40.0)
    assert res["powerAttributionError"] == "failed: worker timed out"
    assert "powerAttribution" not in res
    assert 15.0 <= timeouts[0] <= 40.0


def test_eval_prefilter_keeps_items_when_weights_are_empty() -> None:
    class _EmptyGrad(_CapsFakeWorker):
        def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
            if method == "compute_grad_build":
                return {"weights": {}, "error": "agentProbes capability unavailable on this engine worker"}
            return super().call(method, params, **kw)

    items = [{"raw": f"Rarity: RARE\nItem {i}\nRuby Ring\n+{i} to maximum Life"} for i in range(70)]
    out = _prefilter_raw_items_for_eval(_EmptyGrad(caps={"agentProbes": True}), items, 50)
    assert out == items
