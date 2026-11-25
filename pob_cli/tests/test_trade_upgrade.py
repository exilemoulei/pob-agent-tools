"""`pob trade upgrade`: weakest slots -> live trade per slot -> budget plan + free moves, offline on fakes."""

from __future__ import annotations

import pytest

from pob_cli.trade_eval.upgrade import rank_weak_slots, unequip_loss


def test_unequip_loss_weights_dps_and_ehp() -> None:
    res = {"before": {"primaryDps": 100.0, "totalEhp": 1000.0}, "after": {"primaryDps": 90.0, "totalEhp": 800.0}}
    assert unequip_loss(res, {"dps": 1.0, "ehp": 0.25}) == pytest.approx(10.0 + 0.25 * 20.0)
    assert unequip_loss({"before": {}, "after": {}}, {"dps": 1.0}) is None


def test_rank_weak_slots_empty_first() -> None:
    items = {"slots": [
        {"name": "Helmet", "selItemId": 3}, {"name": "Boots", "selItemId": 0},
        {"name": "Amulet", "selItemId": 5}, {"name": "Weapon 2", "selItemId": 9},
        {"name": "Belt", "selItemId": 7, "inactive": True},
    ]}
    ranked = rank_weak_slots(items, {"Helmet": 4.0, "Amulet": 1.5}, top=2)
    assert [(r["slot"], r["tradeSlot"], r["reason"]) for r in ranked] == [
        ("Boots", "boots", "empty"), ("Amulet", "amulet", "lowest contribution")]


def test_rank_weak_slots_unknown_loss_last() -> None:
    items = {"slots": [{"name": "Helmet", "selItemId": 3}, {"name": "Gloves", "selItemId": 4}]}
    assert [r["slot"] for r in rank_weak_slots(items, {"Helmet": None, "Gloves": 2.0}, top=3)] == ["Gloves", "Helmet"]
