"""Pure unit tests for `mutators/item_db_resolve.py` (aliases, caching, baseline math, variant compaction)."""

from __future__ import annotations

from typing import Any

import pytest

from pob_cli.bridge_protocol import BridgeRejectedError
from pob_cli.mutators.item_db_resolve import (
    _compact_active_variants_raw,
    _compute_power_delta,
    _extract_baseline_for_sort,
    _normalize_db_slot_label,
    _resolve_db_rpc_plan,
    _resolve_sort_mode,
)

_SHUFFLED_OPTS: dict[str, Any] = {
    "kind": "unique",
    "slot": [
        {"label": "Weapon 1", "labelZh": "主手"},
        {"label": "Any slot", "labelZh": "任意槽位"},
        {"label": "Helmet", "labelZh": "头盔"},
    ],
    "type": [{"label": "Armour", "labelZh": "护甲"}, {"label": "Any type", "labelZh": "任意类型"}],
    "league": [{"label": "Any league", "labelZh": "任意赛季"}],
    "requirement": [{"label": "Any requirements", "labelZh": "任意需求"}],
    "obtainable": [{"label": "Any source", "labelZh": "任意来源"}, {"label": "Obtainable", "labelZh": "可获取"}],
    "searchMode": [{"label": "Anywhere", "labelZh": "任意位置"}],
    "sort": [
        {"label": "Sort by Name", "labelZh": "按名称排序", "sortMode": "NAME", "stat": None},
        {"label": "Sort by Full DPS", "labelZh": "按完整 DPS", "sortMode": "FullDPS", "stat": "FullDPS"},
        {"label": "Sort by Combined DPS", "labelZh": "按综合 DPS", "sortMode": "CombinedDPS", "stat": "CombinedDPS"},
        {"label": "Sort by Effective Hit Pool", "labelZh": "按有效血池", "sortMode": "TotalEHP", "stat": "TotalEHP"},
        {"label": "Sort by Minion Life", "labelZh": "按召唤物生命", "sortMode": "MinionLife", "stat": "MinionLife"},
    ],
}


class _OptsWorker:
    def __init__(self, opts: dict[str, Any] | None) -> None:
        self.opts = opts
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if self.opts is None:
            raise BridgeRejectedError("unknown method", code="bad_request")
        return self.opts


@pytest.mark.parametrize(
    ("raw_slot", "expected"),
    [
        ("Ring 1", "Ring"), ("ring2", "Ring"), ("Flask 3", "Flask"), ("Jewel 2", "Jewel"), ("#1", "Jewel"),
        ("head", "Helmet"), ("body", "Body Armour"), ("chest", "Body Armour"), ("mh", "Weapon 1"), ("oh", "Weapon 2"),
        ("头盔", "Helmet"), ("胸甲", "Body Armour"), ("衣服", "Body Armour"), ("项链", "Amulet"), ("戒指", "Ring"),
        ("手套", "Gloves"), ("鞋子", "Boots"), ("腰带", "Belt"), ("药水", "Flask"), ("珠宝", "Jewel"), ("主手", "Weapon 1"), ("副手", "Weapon 2"),
    ],
)
def test_normalize_db_slot_label(raw_slot: str, expected: str) -> None:
    assert _normalize_db_slot_label(raw_slot) == expected


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("name", "Name"), ("Sort by Name", "Name"), ("名称", "Name"), ("按名称", "Name"), ("按名称排序", "Name"),
        ("dps", "FullDPS"), ("combined", "CombinedDPS"), ("ehp", "TotalEHP"), ("minionlife", "MinionLife"), ("miniondps", "FullDPS"),
    ],
)
def test_resolve_sort_mode_aliases_and_chinese_name(alias: str, expected: str) -> None:
    assert _resolve_sort_mode(_SHUFFLED_OPTS["sort"], alias)[0] == expected
    opts_minion = [*_SHUFFLED_OPTS["sort"], {"label": "Sort by Minion Combined DPS", "sortMode": "MinionCombinedDPS", "stat": "MinionCombinedDPS"}]
    assert _resolve_sort_mode(opts_minion, "minion")[0] == "MinionCombinedDPS"
    with pytest.raises(ValueError, match="Unknown --sort metric"):
        _resolve_sort_mode(_SHUFFLED_OPTS["sort"], "bogus_metric")


def test_resolve_db_rpc_plan_caches_options_matches_labels_and_guards_legacy() -> None:
    w = _OptsWorker(_SHUFFLED_OPTS)
    # Default path resolves by label even when indices are shuffled (Any slot=2, Any type=2, Obtainable=2).
    rpc1, flt1, sort1, _ = _resolve_db_rpc_plan(
        w, kind="unique", slot_name=None, item_type=None, league=None,
        requirement=None, obtainable=None, search_mode=None, sort_mode=None,
    )
    assert (rpc1["slot"], rpc1["type"], rpc1["obtainable"], sort1) == (2, 2, 2, "Name")
    assert flt1["obtainable"] == "Obtainable"
    # Second call hits worker._item_db_opts_cache (still only 1 RPC call).
    rpc2, _, sort2, _ = _resolve_db_rpc_plan(
        w, kind="unique", slot_name="Helmet", item_type=None, league=None,
        requirement=None, obtainable=None, search_mode=None, sort_mode="dps",
    )
    assert (rpc2["slot"], sort2) == (3, "FullDPS")
    assert len(w.calls) == 1

    # Rare DB accepts "Sort by Name" / "Name" but rejects stat sorts.
    _, _, sort_rare, _ = _resolve_db_rpc_plan(
        w, kind="rare", slot_name=None, item_type=None, league=None,
        requirement=None, obtainable=None, search_mode=None, sort_mode="Sort by Name",
    )
    assert sort_rare == "Name"
    with pytest.raises(ValueError, match="UNIQUE database"):
        _resolve_db_rpc_plan(
            w, kind="rare", slot_name=None, item_type=None, league=None,
            requirement=None, obtainable=None, search_mode=None, sort_mode="dps",
        )

    # Legacy worker without item_db_options: plain query allowed, filtered/sorted raises ValueError!
    w_leg = _OptsWorker(None)
    rpc_leg, _, sort_leg, _ = _resolve_db_rpc_plan(
        w_leg, kind="unique", slot_name=None, item_type=None, league=None,
        requirement=None, obtainable=None, search_mode=None, sort_mode=None,
    )
    assert (rpc_leg["slot"], sort_leg) == (1, "Name")
    with pytest.raises(ValueError, match="item_db_options"):
        _resolve_db_rpc_plan(
            w_leg, kind="unique", slot_name="Helmet", item_type=None, league=None,
            requirement=None, obtainable=None, search_mode=None, sort_mode="dps",
        )


def test_baseline_and_delta_math_edge_cases() -> None:
    stats = {
        "FullDPS": 0, "CombinedDPS": 500_000.0, "MinionCombinedDPS": 200_000.0,
        "TotalDot": 100_000.0, "MinionTotalDot": 50_000.0, "PhysicalTakenHit": 2200.0,
        "MinionLife": 12_000.0, "TotalEHP": "inf",
    }
    assert _extract_baseline_for_sort(stats, "FullDPS") == pytest.approx(700_000.0)
    assert _extract_baseline_for_sort({"FullDPS": 900_000.0, "CombinedDPS": 500_000.0}, "FullDPS") == pytest.approx(900_000.0)
    assert _extract_baseline_for_sort(stats, "TotalDot") == pytest.approx(150_000.0)
    assert _extract_baseline_for_sort(stats, "PhysicalTakenHit") == pytest.approx(-2200.0)
    assert _extract_baseline_for_sort(stats, "MinionLife") == pytest.approx(12_000.0)
    ehp_base = _extract_baseline_for_sort(stats, "TotalEHP")
    assert _compute_power_delta("inf", ehp_base) == ("inf", 0.0, 0.0)
    assert _compute_power_delta(80_000.0, ehp_base) == (80_000.0, None, None)
    assert _compute_power_delta(100.0, 0.0) == (100.0, 100.0, None)


def test_compact_active_variants_raw_shrinks_oversized_multi_variant_item() -> None:
    small = "Rarity: UNIQUE\nGoldrim\nLeather Cap\n+35% to all Elemental Resistances\n"
    assert _compact_active_variants_raw(small) == small

    hdr = [
        "Rarity: UNIQUE", "Pearl of Tsoatha", "Prismatic Ring", "Has Alt Variant: true",
        "Selected Variant: 1", "Selected Alt Variant: 250", "Implicits: 1",
    ]
    var_hdrs = [f"Variant: Boots / Support Gem Option Number {i:03d}" for i in range(1, 451)]
    mods = [
        "+(8-10)% to all Elemental Resistances",
        "{variant:1,250}Shared active line",
        *[f"{{variant:{i}}}Skills Socketed in your Boots are Supported by level 20 Support Gem Number {i:03d} with extra descriptive padding" for i in range(1, 451)],
    ]
    huge_raw = "\n".join([*hdr, *var_hdrs, *mods]) + "\n"
    assert len(huge_raw.encode("utf-8")) > 64_000

    compacted = _compact_active_variants_raw(huge_raw)
    assert len(compacted.encode("utf-8")) < 5_000
    assert "Selected Variant: 1" in compacted
    assert "Selected Alt Variant: 2" in compacted
    assert "Variant: Boots / Support Gem Option Number 001" in compacted
    assert "Variant: Boots / Support Gem Option Number 250" in compacted
    assert "Option Number 099" not in compacted
    assert "{variant:1,2}Shared active line" in compacted
    assert "{variant:2}Skills Socketed in your Boots are Supported by level 20 Support Gem Number 250" in compacted
    assert "+(" in compacted
