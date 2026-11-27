"""Unit & real-engine tests for PoE1 8p2s Large Cluster Jewel Front-2 Back-1 topology solver."""

from __future__ import annotations

import pytest
from engine_paths import find_engine_data

from pob_cli.trade_query.cluster_topology import (
    LARGE_CLUSTER_GEOMETRY,
    ClusterData,
    ClusterEnchant,
    ClusterNotable,
    _build_offline_fallback_data,
    compute_cluster_node_layout,
    load_cluster_data,
    solve_front2_back1,
)

# ---------------------------------------------------------------------------
# 1. Pure-Offline Geometry & Affix Legality Unit Tests
# ---------------------------------------------------------------------------


def test_buildsubgraph_8p2s_3_notable_front_back_front_geometry() -> None:
    sort_order = {"Sadist": 11386, "Widespread Destruction": 11391, "Snowstorm": 11395}
    layout = compute_cluster_node_layout(
        ["Snowstorm", "Sadist", "Widespread Destruction"],
        sort_order,
        passives=8,
        sockets=2,
        geometry=LARGE_CLUSTER_GEOMETRY,
    )
    assert layout.socket_indices == (4, 8)
    assert layout.small_indices == (0, 5, 7)
    assert [(p.name, p.index, p.position) for p in layout.notable_placements] == [
        ("Sadist", 2, "Front"),
        ("Widespread Destruction", 6, "Back"),
        ("Snowstorm", 10, "Front"),
    ]
    # Taking entrance(0) + Front1(2) + Sock1(4) + Front2(10) + Sock2(8) = 5 points (skips 5, 6, 7)
    assert layout.min_points_front_notables_and_sockets == 5
    # Taking all 3 notables including Back(6) requires walking through 5 or 7 -> 7 points
    assert layout.min_points_all_notables_and_sockets == 7


def test_buildsubgraph_8p2s_2_notable_puts_lower_notable_in_back() -> None:
    sort_order = {"Sadist": 11386, "Snowstorm": 11395}
    layout = compute_cluster_node_layout(
        ["Snowstorm", "Sadist"],
        sort_order,
        passives=8,
        sockets=2,
        geometry=LARGE_CLUSTER_GEOMETRY,
    )
    assert layout.socket_indices == (4, 8)
    assert [(p.name, p.index, p.position) for p in layout.notable_placements] == [
        ("Sadist", 6, "Back"),
        ("Snowstorm", 10, "Front"),
    ]
    assert layout.min_points_all_notables_and_sockets == 7


def test_solver_offline_prefix_prefix_requires_suffix_filler() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Lightning Damage", "Sadist", "Snowstorm", cluster_data=offline)
    assert sol.possible is True
    assert sol.required_back_affix == "Suffix"
    assert [n.name for n in sol.valid_back_notables] == [
        "Doryani's Lesson",
        "Disorienting Display",
        "Widespread Destruction",
    ]
    # Corrosive Elements (Prefix) and Prismatic Heart (Prefix) lie in between but are rejected!
    rej_map = {r.notable.name: r.reason for r in sol.rejected_sandwich}
    assert rej_map == {
        "Corrosive Elements": "prefix_cap_exceeded",
        "Prismatic Heart": "prefix_cap_exceeded",
    }
    assert sol.min_ilvl_ab == 68
    assert sol.min_ilvl_overall == 68


def test_solver_offline_prefix_suffix_requires_prefix_filler() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("12% increased Lightning Damage", "Doryani's Lesson", "Snowstorm", cluster_data=offline)
    assert sol.possible is True
    assert sol.required_back_affix == "Prefix"
    assert [n.name for n in sol.valid_back_notables] == ["Prismatic Heart"]
    rej_map = {r.notable.name: r.reason for r in sol.rejected_sandwich}
    assert rej_map == {
        "Disorienting Display": "suffix_notable_cap_exceeded",
        "Widespread Destruction": "suffix_notable_cap_exceeded",
    }


def test_solver_detects_affix_cap_ghost_impossibility() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Lightning Damage", "Snowstorm", "Scintillating Idea", cluster_data=offline)
    assert sol.possible is False
    assert sol.reason == "affix_cap_conflict"
    assert sol.valid_back_notables == []
    assert len(sol.rejected_sandwich) == 6
    assert all(r.reason == "prefix_cap_exceeded" for r in sol.rejected_sandwich)


def test_solver_detects_adjacent_sort_order_impossibility() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Lightning Damage", "Snowstorm", "Storm Drinker", cluster_data=offline)
    assert sol.possible is False
    assert sol.reason == "no_notables_between_sort_orders"


def test_solver_detects_two_suffix_notables_impossible() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Lightning Damage", "Doryani's Lesson", "Widespread Destruction", cluster_data=offline)
    assert sol.possible is False
    assert sol.reason == "two_suffix_notables_impossible"
    # Eval P2-2: must NOT emit misleading 2-notable layout warning or layout_2_notable
    assert sol.layout_2_notable is None
    assert not any("2-notable" in w for w in sol.warnings)


def test_solver_geometry_not_front2_for_one_or_zero_sockets() -> None:
    offline = _build_offline_fallback_data()
    for sock_cnt in (1, 0):
        sol = solve_front2_back1(
            "Lightning Damage",
            "Sadist",
            "Snowstorm",
            passives=8,
            sockets=sock_cnt,
            cluster_data=offline,
        )
        assert sol.possible is False
        assert sol.reason == "geometry_not_front2"
        assert sol.valid_back_notables == []
        assert sol.layout_3_notable is not None
        assert "Back" in sol.explanation
        assert "vs 5 points" not in " ".join(sol.warnings)


def test_solver_blocks_second_suffix_flag_drives_suffix_mutex() -> None:
    ench = ClusterEnchant(
        tag="custom_tag",
        name="Custom Damage",
        stats=("12% increased Custom Damage",),
        enchant_lines=("Added Small Passive Skills grant: 12% increased Custom Damage",),
    )
    notables = {
        "SufA": ClusterNotable("SufA", 100, "Suffix", 1, "GroupA", {"custom_tag": 500}, blocks_second_suffix=False),
        "SufMidFree": ClusterNotable(
            "SufMidFree", 105, "Suffix", 1, "GroupMid1", {"custom_tag": 500}, blocks_second_suffix=False
        ),
        "SufMidBlocked": ClusterNotable(
            "SufMidBlocked", 106, "Suffix", 1, "GroupMid2", {"custom_tag": 500}, blocks_second_suffix=True
        ),
        "PreB": ClusterNotable("PreB", 110, "Prefix", 1, "GroupB", {"custom_tag": 500}, blocks_second_suffix=False),
    }
    data = ClusterData(
        source="offline_fallback",
        large_geometry=LARGE_CLUSTER_GEOMETRY,
        large_enchants={"custom_tag": ench},
        notable_sort_order={k: v.sort_order for k, v in notables.items()},
        notables_by_name=notables,
    )
    sol = solve_front2_back1("Custom Damage", "SufA", "PreB", cluster_data=data)
    assert sol.possible is True
    assert [n.name for n in sol.valid_back_notables] == ["SufMidFree"]
    assert [(r.notable.name, r.reason) for r in sol.rejected_sandwich] == [
        ("SufMidBlocked", "suffix_notable_cap_exceeded")
    ]


def test_solver_zh_variants_traditional_and_fuzzy_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.game_data as gd

    monkeypatch.setattr(gd, "english_for_zh", lambda *_a, **_kw: None)
    offline = _build_offline_fallback_data()
    # Simplified variants ("风暴骑手", "多利亚尼之训") and Traditional ("閃電傷害", "風暴騎手", "多利亞尼之訓")
    for ench_q, a_q, b_q in (
        ("闪电伤害", "风暴骑手", "多利亚尼之训"),
        ("閃電傷害", "風暴騎手", "多利亞尼之訓"),
        ("闪电伤害", "暴风饮者", "棱光之心"),
    ):
        sol = solve_front2_back1(ench_q, a_q, b_q, cluster_data=offline)
        assert sol.enchant.tag == "affliction_lightning_damage"

    sol_sr = solve_front2_back1("閃電傷害", "風暴騎手", "多利亞尼之訓", cluster_data=offline)
    assert {sol_sr.notable_a.name, sol_sr.notable_b.name} == {"Doryani's Lesson", "Stormrider"}
    assert sol_sr.possible is True

    # Unknown notable error includes Top-3 fuzzy suggestions before the full pool hint
    with pytest.raises(ValueError, match="Did you mean:") as exc_info:
        solve_front2_back1("闪电伤害", "风暴骑兵", "多利亚尼之训", cluster_data=offline)
    sugg_zh = str(exc_info.value).split("Did you mean:", 1)[1].split("Pool for", 1)[0]
    assert "Stormrider" in sugg_zh

    with pytest.raises(ValueError, match="Did you mean:") as exc_en:
        solve_front2_back1("Lightning Damage", "Stormryder", "Sadist", cluster_data=offline)
    sugg_en = str(exc_en.value).split("Did you mean:", 1)[1].split("Pool for", 1)[0]
    assert "Stormrider" in sugg_en


def test_solver_enforces_same_mod_group_exclusion() -> None:
    ench = ClusterEnchant(
        tag="custom_tag",
        name="Custom Damage",
        stats=("12% increased Custom Damage",),
        enchant_lines=("Added Small Passive Skills grant: 12% increased Custom Damage",),
    )
    notables = {
        "Alpha": ClusterNotable("Alpha", 100, "Prefix", 1, "GroupA", {"custom_tag": 500}),
        "MidSame": ClusterNotable("MidSame", 105, "Suffix", 1, "GroupA", {"custom_tag": 500}, True),
        "MidOk": ClusterNotable("MidOk", 106, "Suffix", 75, "GroupMid", {"custom_tag": 500}, True),
        "Omega": ClusterNotable("Omega", 110, "Prefix", 50, "GroupB", {"custom_tag": 500}),
    }
    data = ClusterData(
        source="offline_fallback",
        large_geometry=LARGE_CLUSTER_GEOMETRY,
        large_enchants={"custom_tag": ench},
        notable_sort_order={k: v.sort_order for k, v in notables.items()},
        notables_by_name=notables,
    )
    sol = solve_front2_back1("Custom Damage", "Alpha", "Omega", cluster_data=data)
    assert sol.possible is True
    assert [n.name for n in sol.valid_back_notables] == ["MidOk"]
    assert [(r.notable.name, r.reason) for r in sol.rejected_sandwich] == [("MidSame", "same_mod_group")]
    assert sol.min_ilvl_overall == 75
    assert any("ilvl 75+" in w for w in sol.warnings)


def test_solver_ilvl_max_filtering_and_warning() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Minion Damage", "Renewal", "Feasting Fiends", ilvl_max=67, cluster_data=offline)
    assert sol.possible is True
    assert [n.name for n in sol.valid_back_notables] == ["Call to the Slaughter"]
    assert {n.name for n in sol.ilvl_filtered_out} == {"Primordial Bond", "Vicious Bite"}
    assert sol.min_ilvl_overall == 50


def test_solver_bilingual_zh_enchant_and_notables_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.game_data as gd

    monkeypatch.setattr(gd, "english_for_zh", lambda *_a, **_kw: None)
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("闪电伤害", "施虐者", "暴风雪", cluster_data=offline)
    assert sol.possible is True
    assert sol.enchant.tag == "affliction_lightning_damage"
    assert {sol.notable_a.name, sol.notable_b.name} == {"Sadist", "Snowstorm"}
    assert len(sol.valid_back_notables) == 3


def test_offline_inspired_oppression_on_elemental_damage() -> None:
    offline = _build_offline_fallback_data()
    sol = solve_front2_back1("Elemental Damage", "Sadist", "Inspired Oppression", cluster_data=offline)
    assert sol.possible is True
    assert "Widespread Destruction" in [n.name for n in sol.valid_back_notables]


def test_cluster_data_mappings_frozen_and_oserror_falls_back(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    offline = _build_offline_fallback_data()
    with pytest.raises(TypeError):
        offline.notables_by_name["Sadist"].weights["affliction_lightning_damage"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        offline.notables_by_name["Fake"] = offline.notables_by_name["Sadist"]  # type: ignore[index]

    data_dir = Path(str(tmp_path)) / "PoE1" / "Data"
    data_dir.mkdir(parents=True)
    cj = data_dir / "ClusterJewels.lua"
    mjc = data_dir / "ModJewelCluster.lua"
    cj.write_text("-- stub", encoding="utf-8")
    mjc.write_text("-- stub", encoding="utf-8")

    orig_read_text = Path.read_text

    def _boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "ModJewelCluster.lua":
            raise FileNotFoundError("vanished after stat")
        return orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _boom)
    loaded = load_cluster_data(pob_root=Path(str(tmp_path)))
    assert loaded.source == "offline_fallback"


def test_resolve_multiline_bow_damage_enchant() -> None:
    from pob_cli.trade_query.cluster_resolve import resolve_large_enchant

    bow_ench = ClusterEnchant(
        tag="affliction_bow_damage",
        name="Bow Damage",
        stats=("12% increased Damage with Bows", "12% increased Damage Over Time with Bow Skills"),
        enchant_lines=(
            "Added Small Passive Skills grant: 12% increased Damage with Bows",
            "Added Small Passive Skills grant: 12% increased Damage Over Time with Bow Skills",
        ),
        trade_option_id=4,
    )
    data = ClusterData(
        source="offline_fallback",
        large_geometry=LARGE_CLUSTER_GEOMETRY,
        large_enchants={"affliction_bow_damage": bow_ench},
        notable_sort_order={},
        notables_by_name={},
    )
    joined_stats = "12% increased Damage with Bows\n12% increased Damage Over Time with Bow Skills"
    joined_lines = (
        "Added Small Passive Skills grant: 12% increased Damage with Bows\n"
        "Added Small Passive Skills grant: 12% increased Damage Over Time with Bow Skills"
    )
    assert resolve_large_enchant(joined_stats, data).tag == "affliction_bow_damage"
    assert resolve_large_enchant(joined_lines, data).tag == "affliction_bow_damage"


def test_solver_rejects_invalid_passive_count_front_ilvl_and_invalid_alt_geometry() -> None:
    offline = _build_offline_fallback_data()
    sol_7p = solve_front2_back1("Lightning Damage", "Sadist", "Snowstorm", passives=7, sockets=2, cluster_data=offline)
    assert sol_7p.possible is False
    assert sol_7p.reason == "invalid_passive_count"
    assert sol_7p.layout_3_notable is None
    assert sol_7p.layout_2_notable is None

    # Front notable Doryani's Lesson requires ilvl 68 -> rejected at 67, valid at 68
    sol_67 = solve_front2_back1("Lightning Damage", "Doryani's Lesson", "Snowstorm", ilvl_max=67, cluster_data=offline)
    assert sol_67.possible is False
    assert sol_67.reason == "ilvl_max_too_low"
    sol_68 = solve_front2_back1("Lightning Damage", "Doryani's Lesson", "Snowstorm", ilvl_max=68, cluster_data=offline)
    assert sol_68.possible is True

    # 8p1s cannot place both front notables in Front even on an alternative enchant
    ench_a = ClusterEnchant("tag_a", "Enchant A", ("10% A",), ("Added Small Passive Skills grant: 10% A",))
    ench_b = ClusterEnchant("tag_b", "Enchant B", ("10% B",), ("Added Small Passive Skills grant: 10% B",))
    notables = {
        "PreLow": ClusterNotable("PreLow", 100, "Prefix", 1, "GA", {"tag_a": 100, "tag_b": 100}),
        "SufMid": ClusterNotable("SufMid", 105, "Suffix", 1, "GM", {"tag_b": 100}, blocks_second_suffix=True),
        "PreHigh": ClusterNotable("PreHigh", 110, "Prefix", 1, "GB", {"tag_a": 100, "tag_b": 100}),
    }
    data = ClusterData(
        source="offline_fallback",
        large_geometry=LARGE_CLUSTER_GEOMETRY,
        large_enchants={"tag_a": ench_a, "tag_b": ench_b},
        notable_sort_order={k: v.sort_order for k, v in notables.items()},
        notables_by_name=notables,
    )
    sol_alt_2s = solve_front2_back1("Enchant A", "PreLow", "PreHigh", passives=8, sockets=2, cluster_data=data)
    assert sol_alt_2s.possible is False
    assert [a["tag"] for a in sol_alt_2s.alternative_enchants] == ["tag_b"]

    sol_alt_1s = solve_front2_back1("Enchant A", "PreLow", "PreHigh", passives=8, sockets=1, cluster_data=data)
    assert sol_alt_1s.possible is False
    assert sol_alt_1s.alternative_enchants == []


# ---------------------------------------------------------------------------
# 2. Real PoB 3.30 ClusterJewels.lua + ModJewelCluster.lua Verification
# ---------------------------------------------------------------------------


def test_real_engine_cluster_data_and_front2_back1_solutions() -> None:
    engine = find_engine_data()
    if engine is None:
        pytest.skip("PoB engine data not installed")

    data = load_cluster_data(pob_root=engine)
    assert data.source == "local_engine"
    assert len(data.large_enchants) == 17
    assert len(data.notables_by_name) >= 290
    suffixes = [n for n in data.notables_by_name.values() if n.affix_type == "Suffix"]
    assert len(suffixes) == 17
    assert all(n.blocks_second_suffix for n in suffixes)

    # 1. Shield Attack cluster: Prodigious Defence (P, 11274) + Feed the Fury (P, 11377) -> 3 Suffixes
    sol_shield = solve_front2_back1(
        "Attack Damage while holding a Shield",
        "Prodigious Defence",
        "Feed the Fury",
        cluster_data=data,
    )
    assert sol_shield.possible is True
    assert [n.name for n in sol_shield.valid_back_notables] == [
        "Smite the Weak",
        "Heavy Hitter",
        "Martial Prowess",
    ]
    assert sol_shield.layout_3_notable is not None
    assert sol_shield.layout_3_notable.min_points_front_notables_and_sockets == 5

    # 2. Physical cluster: Iron Breaker (P, 11283) + Battle-Hardened (P, 11290) -> 3 Suffixes
    sol_phys = solve_front2_back1("Physical Damage", "Iron Breaker", "Battle-Hardened", cluster_data=data)
    assert sol_phys.possible is True
    assert [n.name for n in sol_phys.valid_back_notables] == [
        "Force Multiplier",
        "Furious Assault",
        "Grim Oath",
    ]

    # 3. Spell cluster: Practiced Caster (P, 11381) + Mage Hunter (P, 11527) -> 3 Suffixes
    sol_spell = solve_front2_back1("法术伤害", "Practiced Caster", "Mage Hunter", cluster_data=data)
    assert sol_spell.possible is True
    assert [n.name for n in sol_spell.valid_back_notables] == [
        "Thaumophage",
        "Essence Rush",
        "Sap Psyche",
    ]

    # 4. Bilingual CN/TW notable names & community variants via real engine
    sol_zh = solve_front2_back1("闪电伤害", "多里亚尼之训", "暴风雪", pob_root=engine, cluster_data=data)
    assert sol_zh.possible is True
    assert [n.name for n in sol_zh.valid_back_notables] == ["Prismatic Heart"]

    sol_tw = solve_front2_back1("閃電傷害", "風暴騎手", "多利亞尼之訓", pob_root=engine, cluster_data=data)
    assert sol_tw.possible is True
    assert {sol_tw.notable_a.name, sol_tw.notable_b.name} == {"Doryani's Lesson", "Stormrider"}
