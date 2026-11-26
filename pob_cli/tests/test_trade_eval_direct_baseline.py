"""Trade candidates measured by `direct_calc` are compared with a baseline from the same pass."""

from __future__ import annotations

from typing import Any

from test_cow_snapshot_sweep import _CowFakeWorker

from pob_cli.trade_eval import eval_trade_items


class _TwoPassWorker(_CowFakeWorker):
    """The frame's sidebar warns in the display language, `direct_calc` in English, as the engine does."""

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        res = super().call(method, params, timeout)
        if method == "get_sidebar":
            return {"warnings": ["你未满足 末日之咬 的力量需求"], "rows": []}
        if method == "direct_calc":
            return {**res, "warnings": ["You do not meet the Strength requirement of Doom Bite"]}
        return res


def test_an_unchanged_warning_is_not_reported_as_new_and_resolved() -> None:
    w = _TwoPassWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.slot_items["Ring 1"] = "Rarity: RARE\nOld Loop\nIron Ring"

    out = eval_trade_items(w, slot_name="Ring 1", items=[{"id": "a", "raw": "Rarity: RARE\nNew Loop\nIron Ring"}])

    result = out["results"][0]
    assert result["ok"] is True
    diff = result["diff"]
    assert not diff.get("newWarnings") and not diff.get("resolvedWarnings")
    assert diff.get("pointsBefore") == diff.get("pointsAfter")
    # One direct_calc for the baseline, one for the candidate.
    assert [m for m, _ in w.calls].count("direct_calc") == 2
