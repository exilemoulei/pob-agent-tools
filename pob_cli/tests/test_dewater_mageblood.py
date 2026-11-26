"""Unit tests for slot-exact Mageblood Magic Utility Flask dewatering filter."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pob_cli.analyzer import attribute_water, dewater_build_with_records
from pob_cli.analyzer.dewater_mageblood import is_magic_utility_flask, parse_mageblood_spec
from pob_cli.snapshot import capture_digest


class _MagebloodWorker:
    def __init__(self, belt_raw: str, flasks_spec: list[dict[str, Any]]) -> None:
        self.lock = threading.RLock()
        self.belt_raw = belt_raw
        self.flasks_spec = flasks_spec
        self.config: dict[str, Any] = {"enemyIsBoss": "Pinnacle"}

    def _stats(self) -> dict[str, Any]:
        ehp = 100_000.0
        for f in self.flasks_spec:
            if f["active"] and f["title"] == "Rumi's Concoction":
                ehp *= 1.25
        return {
            "CombinedDPS": 1_000_000.0,
            "TotalEHP": ehp,
            "Life": 5000.0,
            "PhysicalMaximumHitTaken": 12_000.0,
            "FireMaximumHitTaken": 30_000.0,
            "ColdMaximumHitTaken": 30_000.0,
            "LightningMaximumHitTaken": 30_000.0,
            "ChaosMaximumHitTaken": 25_000.0,
        }

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "list_config":
            return {"sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}], "customMods": []}
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method == "list_items":
            slots = []
            items = []
            for idx, f in enumerate(self.flasks_spec, 1):
                slots.append({"name": f["slot"], "isFlask": True, "active": f["active"], "selItemId": idx})
                items.append(
                    {
                        "id": idx,
                        "name": f["title"],
                        "title": f["title"],
                        "baseName": f["baseName"],
                        "rarity": f["rarity"],
                        "type": "Flask",
                    }
                )
            belt_id = len(items) + 1
            slots.append({"name": "Belt", "isFlask": False, "active": False, "selItemId": belt_id})
            items.append(
                {
                    "id": belt_id,
                    "name": "Mageblood",
                    "title": "Mageblood",
                    "baseName": "Heavy Belt",
                    "rarity": "UNIQUE",
                    "raw": self.belt_raw,
                }
            )
            return {"slots": slots, "items": items}
        if method == "set_slot_active":
            for f in self.flasks_spec:
                if f["slot"] == params["slotName"]:
                    f["active"] = bool(params["active"])
            return {}
        if method == "list_skills":
            return {"groups": []}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": []}
        if method in ("get_build_info", "get_build_header"):
            return {}
        raise AssertionError(f"unexpected rpc {method}")


def test_mageblood_variant_range_and_chinese_parsing() -> None:
    foulborn_raw = (
        "Rarity: UNIQUE\nMageblood\nHeavy Belt\nSelected Variant: 2\n"
        "{variant:1}Leftmost {range:0.5}(2-4) Magic Utility Flasks constantly apply their Flask Effects to you\n"
        "{variant:2}Rightmost {range:0.5}(2-4) Magic Utility Flasks constantly apply their Flask Effects to you\n"
    )
    assert parse_mageblood_spec(foulborn_raw) == ("right", 3)
    assert parse_mageblood_spec("Leftmost (2-4) Magic Utility Flasks constantly apply their Flask Effects to you") == ("left", 4)
    assert parse_mageblood_spec("Leftmost {range:0}(2-4) Magic Utility Flasks constantly apply their Flask Effects to you") == ("left", 2)
    assert parse_mageblood_spec("最右侧 3 瓶魔法功能药剂的效果持续作用于你") == ("right", 3)
    assert parse_mageblood_spec(None) == ("left", 4)

    assert is_magic_utility_flask({"rarity": "MAGIC", "baseName": "Topaz Flask"}) is True
    assert is_magic_utility_flask({"rarity": "UNIQUE", "baseName": "Granite Flask"}) is False
    assert is_magic_utility_flask({"rarity": "NORMAL", "baseName": "Diamond Flask"}) is False
    assert is_magic_utility_flask({"rarity": "MAGIC", "baseName": "Divine Life Flask"}) is False


def test_mageblood_strips_unique_normal_life_and_overflow_magic_flasks() -> None:
    flasks_rumis = [
        {"slot": "Flask 1", "active": True, "rarity": "MAGIC", "baseName": "Topaz Flask", "title": "Topaz Flask"},
        {"slot": "Flask 2", "active": True, "rarity": "MAGIC", "baseName": "Bismuth Flask", "title": "Bismuth Flask"},
        {"slot": "Flask 3", "active": True, "rarity": "UNIQUE", "baseName": "Granite Flask", "title": "Rumi's Concoction"},
        {"slot": "Flask 4", "active": True, "rarity": "MAGIC", "baseName": "Silver Flask", "title": "Silver Flask"},
        {"slot": "Flask 5", "active": True, "rarity": "MAGIC", "baseName": "Diamond Flask", "title": "Diamond Flask"},
    ]
    w = _MagebloodWorker("Leftmost 4 Magic Utility Flasks constantly apply their Flask Effects to you", flasks_rumis)
    stripped, records = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert {f["slot"]: f["active"] for f in w.flasks_spec} == {
        "Flask 1": True,
        "Flask 2": True,
        "Flask 3": False,
        "Flask 4": True,
        "Flask 5": True,
    }
    assert any("Flask 3" in s for s in stripped["flasks"])
    base = capture_digest(w)  # type: ignore[arg-type]
    rows = {r["key"]: r for r in attribute_water(w, records, base, "CombinedDPS")}  # type: ignore[arg-type]
    assert rows["Flask 3"]["totalEhpPct"] == pytest.approx(25.0)

    # Leftmost 2 with Life + Normal + 3 Magic Utility flasks -> exempts ONLY Flask 3 & Flask 4
    flasks_2 = [
        {"slot": "Flask 1", "active": True, "rarity": "MAGIC", "baseName": "Divine Life Flask", "title": "Life Flask"},
        {"slot": "Flask 2", "active": True, "rarity": "NORMAL", "baseName": "Diamond Flask", "title": "Diamond Flask"},
        {"slot": "Flask 3", "active": True, "rarity": "MAGIC", "baseName": "Topaz Flask", "title": "Topaz Flask"},
        {"slot": "Flask 4", "active": True, "rarity": "MAGIC", "baseName": "Silver Flask", "title": "Silver Flask"},
        {"slot": "Flask 5", "active": True, "rarity": "MAGIC", "baseName": "Quicksilver Flask", "title": "Quicksilver Flask"},
    ]
    w2 = _MagebloodWorker("Leftmost 2 Magic Utility Flasks constantly apply their Flask Effects to you", flasks_2)
    dewater_build_with_records(w2)  # type: ignore[arg-type]
    assert {f["slot"]: f["active"] for f in w2.flasks_spec} == {
        "Flask 1": False,
        "Flask 2": False,
        "Flask 3": True,
        "Flask 4": True,
        "Flask 5": False,
    }
