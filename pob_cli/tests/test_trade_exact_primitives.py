"""Tests for Layer 2: enumerated option stats, exact item primitives, domain/range stats, and Foulborn guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli.cli_trade import trade_group
from pob_cli.trade_query import (
    build_query,
    detect_and_strip_foulborn,
    load_local_trade_stats,
    parse_stat_argument,
    plan_stats,
    resolve_option_stat,
    resolve_trade_name_and_base,
    strip_foulborn_replaced_mods,
)

_SAMPLE_OPTION_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "enchant.stat_3086156145",
        "text": "Adds # Passive Skills",
        "type": "enchant",
    },
    {
        "id": "explicit.stat_3086156145",
        "text": "Adds # Passive Skills",
        "type": "explicit",
    },
    {
        "id": "enchant.stat_3948993189|49",
        "text": "Added Small Passive Skills grant: +12% to Chaos Resistance",
        "type": "enchant",
    },
    {
        "id": "enchant.stat_3948993189|3",
        "text": "Added Small Passive Skills grant: 12% increased Lightning Damage",
        "type": "enchant",
    },
    {
        "id": "explicit.stat_2460506030|47389",
        "text": "Allocates Profane Bloom if you have the matching modifier on Forbidden Flame",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_1190333629|47389",
        "text": "Allocates Profane Bloom if you have the matching modifier on Forbidden Flesh",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3642528642|1",
        "text": "Only affects Passives in Small Ring",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3642528642|4",
        "text": "Only affects Passives in Very Large Ring",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3642528642|5",
        "text": "Only affects Passives in Massive Ring",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2422708892|54307",
        "text": "Passive Skills in Radius of Acrobatics can be Allocated without being connected to your tree Passage",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2901986750",
        "text": "+#% to all Elemental Resistances",
        "type": "explicit",
    },
    {
        "id": "implicit.stat_2901986750",
        "text": "+#% to all Elemental Resistances",
        "type": "implicit",
    },
    {
        "id": "pseudo.pseudo_tangled_implicit_tier",
        "text": "Has Eater of Worlds Implicit Tier",
        "type": "pseudo",
        "option": {
            "options": [
                {"id": 1, "text": "Lesser"},
                {"id": 2, "text": "Greater"},
                {"id": 3, "text": "Grand"},
            ]
        },
    },
]


def test_resolve_option_stats_pipe_and_nested() -> None:
    m_flame = resolve_option_stat(
        "Allocates # if you have the matching modifier on Forbidden Flame",
        _SAMPLE_OPTION_ENTRIES,
        option_value="Profane Bloom",
    )
    assert m_flame is not None
    assert m_flame.id == "explicit.stat_2460506030"
    assert m_flame.option_id == 47389

    m_ring = resolve_option_stat("Only Affects Passives in Very Large Ring", _SAMPLE_OPTION_ENTRIES)
    assert m_ring is not None
    assert m_ring.id == "explicit.stat_3642528642"
    assert m_ring.option_id == 4

    m_ring_zh = resolve_option_stat(
        "Only Affects Passives in # Ring",
        _SAMPLE_OPTION_ENTRIES,
        option_value="极大",
    )
    assert m_ring_zh is not None
    assert m_ring_zh.option_id == 4

    m_cluster = resolve_option_stat(
        "Added Small Passive Skills grant: #",
        _SAMPLE_OPTION_ENTRIES,
        option_value="+12% to Chaos Resistance",
    )
    assert m_cluster is not None
    assert m_cluster.id == "enchant.stat_3948993189"
    assert m_cluster.option_id == 49

    m_imp = resolve_option_stat(
        "Passives in Radius of # can be Allocated without being connected to your tree",
        _SAMPLE_OPTION_ENTRIES,
        option_value="Acrobatics",
    )
    assert m_imp is not None
    assert m_imp.id == "explicit.stat_2422708892"
    assert m_imp.option_id == 54307

    m_nested = resolve_option_stat(
        "Has Eater of Worlds Implicit Tier",
        _SAMPLE_OPTION_ENTRIES,
        option_value="Grand",
    )
    assert m_nested is not None
    assert m_nested.id == "pseudo.pseudo_tangled_implicit_tier"
    assert m_nested.option_id == 3


def test_parse_stat_domain_range_and_option_builds_clean_query() -> None:
    req_dom = parse_stat_argument("enchant:Adds # Passive Skills=8..8")
    assert req_dom.domain == "enchant"
    assert req_dom.min_value == 8.0
    assert req_dom.max_value == 8.0

    req_upper = parse_stat_argument("implicit:+#% to all Elemental Resistances=..-10")
    assert req_upper.domain == "implicit"
    assert req_upper.min_value is None
    assert req_upper.max_value == -10.0

    req_opt = parse_stat_argument("Allocates # if you have the matching modifier on Forbidden Flame=Profane Bloom")
    assert req_opt.option_value == "Profane Bloom"
    assert req_opt.min_value is None

    plan = plan_stats([req_dom, req_upper, req_opt], _SAMPLE_OPTION_ENTRIES, rarity="any")
    body = build_query(
        plan=plan,
        category=None,
        rarity="any",
        name="Forbidden Flame",
        links=6,
        foulborn=True,
        corrupted="no",
    )
    q = body["query"]
    filters = q["stats"][0]["filters"]
    assert filters[0] == {"id": "enchant.stat_3086156145", "value": {"min": 8.0, "max": 8.0}, "disabled": False}
    assert filters[1] == {"id": "implicit.stat_2901986750", "value": {"max": -10.0}, "disabled": False}
    assert filters[2] == {"id": "explicit.stat_2460506030", "value": {"option": 47389}, "disabled": False}
    assert q["filters"]["socket_filters"]["filters"]["links"] == {"min": 6}
    assert q["filters"]["misc_filters"]["filters"]["foulborn_item"] == {"option": "true"}
    assert q["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "false"}


def test_foulborn_detection_and_replaced_mod_stripping(tmp_path: Path) -> None:
    clean, is_fb = detect_and_strip_foulborn("Foulborn Mageblood")
    assert clean == "Mageblood"
    assert is_fb is True

    clean_zh, is_fb_zh = detect_and_strip_foulborn("秽生 法血")
    assert clean_zh == "法血"
    assert is_fb_zh is True

    en_name, en_base, fb_flag, cat = resolve_trade_name_and_base("秽生 法血", None)
    assert en_name == "Mageblood"
    assert en_base is None
    assert fb_flag is True
    assert cat is None

    # Fake PoB engine with ModFoulbornMap.lua
    data_dir = tmp_path / "PoE1" / "Data"
    data_dir.mkdir(parents=True)
    (data_dir / "ModFoulbornMap.lua").write_text(
        'return {\n'
        '  ["Mageblood"] = {\n'
        '    "Rightmost (2-4) Magic Utility Flasks constantly apply their Flask Effects to you",\n'
        '  },\n'
        '  ["Alpha\'s Howl"] = {\n'
        '    "32% increased Life Reservation Efficiency of Skills",\n'
        '    "+2 to Level of Socketed Minion Gems",\n'
        '  },\n'
        '}\n',
        encoding="utf-8",
    )
    raw_lines = [
        "+45 to Dexterity",
        "+22% to Fire Resistance",
        "Leftmost 4 Magic Utility Flasks constantly apply their Flask Effects to you",
        "{mutated}Rightmost 4 Magic Utility Flasks constantly apply their Flask Effects to you",
    ]
    kept, stripped = strip_foulborn_replaced_mods("Foulborn Mageblood", raw_lines, pob_dir=tmp_path)
    assert len(stripped) == 1
    assert "Leftmost 4" in stripped[0]
    assert any("Rightmost 4" in ln for ln in kept)
    assert "+45 to Dexterity" in kept


def test_cli_trade_search_dump_query_exact_gem_and_foulborn() -> None:
    runner = CliRunner()
    with patch("pob_cli.cli_trade.load_trade_stats", return_value=(_SAMPLE_OPTION_ENTRIES, "local_engine")):
        res_gem = runner.invoke(
            trade_group,
            [
                "search",
                "--base",
                "离心电能",
                "--gem-level",
                "21",
                "--gem-quality",
                "20",
                "--corrupted",
                "yes",
                "--securable",
                "--dump-query",
            ],
        )
        assert res_gem.exit_code == 0, res_gem.output
        payload = json.loads(res_gem.output)
        q = payload["query"]["query"]
        assert q["type"] == "Lightning Tendrils of Eccentricity"
        assert q["status"]["option"] == "securable"
        assert q["filters"]["type_filters"]["filters"]["category"] == {"option": "gem"}
        assert q["filters"]["misc_filters"]["filters"]["gem_level"] == {"min": 21}
        assert q["filters"]["misc_filters"]["filters"]["quality"] == {"min": 20}
        assert q["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "true"}

        res_fb = runner.invoke(
            trade_group,
            [
                "search",
                "--name",
                "Foulborn Mageblood",
                "--links",
                "6",
                "--stat",
                "Only Affects Passives in Very Large Ring",
                "--dump-query",
            ],
        )
        assert res_fb.exit_code == 0, res_fb.output
        payload_fb = json.loads(res_fb.output)
        q_fb = payload_fb["query"]["query"]
        assert q_fb["name"] == "Mageblood"
        assert q_fb["filters"]["misc_filters"]["filters"]["foulborn_item"] == {"option": "true"}
        assert q_fb["filters"]["socket_filters"]["filters"]["links"] == {"min": 6}
        assert q_fb["stats"][0]["filters"][0] == {
            "id": "explicit.stat_3642528642",
            "value": {"option": 4},
            "disabled": False,
        }


def test_real_engine_trade_site_stats_options_and_foulborn_map() -> None:
    engine_root = find_engine_data()
    if engine_root is None:
        pytest.skip("No local PoB engine found")
    entries = load_local_trade_stats(pob_root=engine_root)
    assert entries
    m_ring = resolve_option_stat("Only Affects Passives in Very Large Ring", entries)
    assert m_ring is not None and m_ring.id == "explicit.stat_3642528642" and m_ring.option_id == 4
    m_flame = resolve_option_stat(
        "Allocates # if you have the matching modifier on Forbidden Flame",
        entries,
        option_value="Profane Bloom",
    )
    assert m_flame is not None and m_flame.id == "explicit.stat_2460506030" and isinstance(m_flame.option_id, int)
    m_cluster = resolve_option_stat(
        "Added Small Passive Skills grant: #",
        entries,
        option_value="+12% to Chaos Resistance",
    )
    assert m_cluster is not None and m_cluster.id == "enchant.stat_3948993189" and m_cluster.option_id == 49
    kept, stripped = strip_foulborn_replaced_mods(
        "Foulborn Alpha's Howl",
        [
            "+2 to Level of Socketed Aura Gems",
            "16% increased Mana Reservation Efficiency of Skills",
            "{mutated}32% increased Life Reservation Efficiency of Skills",
            "Cannot be Frozen",
        ],
        pob_dir=engine_root,
    )
    assert any("Mana Reservation Efficiency" in s for s in stripped)
    assert any("Life Reservation Efficiency" in k for k in kept)
    assert "Cannot be Frozen" in kept


def test_foulborn_non_replaced_mod_preserved_and_cli_exact_edge_cases(tmp_path: Path) -> None:
    data_dir = tmp_path / "PoE1" / "Data"
    data_dir.mkdir(parents=True)
    (data_dir / "ModFoulbornMap.lua").write_text(
        'return {\n  ["Mageblood"] = {\n    "Rightmost (2-4) Magic Utility Flasks constantly apply their Flask Effects to you",\n  },\n}\n',
        encoding="utf-8",
    )
    # Unrelated explicit mods on a Foulborn unique must NOT be stripped
    kept, stripped = strip_foulborn_replaced_mods(
        "Foulborn Mageblood",
        [
            "+48 to Dexterity",
            "+24% to Cold Resistance",
            "Flasks cannot be Used",
            "{mutated}Rightmost 4 Magic Utility Flasks constantly apply their Flask Effects to you",
        ],
        pob_dir=tmp_path,
    )
    assert stripped == []
    assert len(kept) == 4

    runner = CliRunner()
    res_entries = [
        *_SAMPLE_OPTION_ENTRIES,
        {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
        {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
        {"id": "explicit.stat_1671376347", "text": "+#% to Lightning Resistance", "type": "explicit"},
    ]
    with patch("pob_cli.cli_trade.load_trade_stats", return_value=(res_entries, "local_engine")):
        # 1. --foulborn without --rarity defaults to rarity=unique (not nonunique)
        r_fb = runner.invoke(trade_group, ["search", "--category", "belt", "--foulborn", "--dump-query"])
        assert r_fb.exit_code == 0, r_fb.output
        q_fb = json.loads(r_fb.output)["query"]["query"]
        assert q_fb["filters"]["type_filters"]["filters"]["rarity"] == {"option": "unique"}
        assert q_fb["filters"]["misc_filters"]["filters"]["foulborn_item"] == {"option": "true"}

        # 2. --no-category --gem-quality 20 preserves explicit no-category
        r_nocat = runner.invoke(trade_group, ["search", "--no-category", "--gem-quality", "20", "--dump-query"])
        assert r_nocat.exit_code == 0, r_nocat.output
        q_nocat = json.loads(r_nocat.output)["query"]["query"]
        assert "category" not in (q_nocat.get("filters", {}).get("type_filters", {}).get("filters", {}))
        assert q_nocat["filters"]["misc_filters"]["filters"]["quality"] == {"min": 20}

        # 3. Convertible resistance range `=..30` preserves max=30 and omits spurious min=1
        req_conv = parse_stat_argument("+#% to Fire Resistance=..30")
        plan_conv = plan_stats([req_conv], res_entries, allow_conversion=True, rarity="rare")
        body_conv = build_query(plan=plan_conv, category="armour.gloves", rarity="rare")
        conv_filters = body_conv["query"]["stats"][0]["filters"]
        assert len(conv_filters) == 3
        assert all(flt["value"] == {"max": 30.0} for flt in conv_filters)

        # 4. --auto-query preserves --links & --foulborn in generated query
        fake_auto = {
            "query": {"query": {"status": {"option": "available"}, "stats": [{"type": "weight", "filters": []}]}, "sort": {"statgroup.0": "desc"}},
        }
        with patch("pob_cli.cli_trade.search_cmd._try_auto_trade_query", return_value=fake_auto):
            r_auto = runner.invoke(
                trade_group,
                ["search", "--slot", "body_armour", "--auto-query", "--links", "6", "--foulborn", "--dump-query"],
            )
            assert r_auto.exit_code == 0, r_auto.output
            q_auto = json.loads(r_auto.output)["query"]["query"]
            assert q_auto["filters"]["socket_filters"]["filters"]["links"] == {"min": 6}
            assert q_auto["filters"]["misc_filters"]["filters"]["foulborn_item"] == {"option": "true"}

        # 5. Explicit --rarity nonunique is preserved alongside --gem-quality
        r_nonuniq = runner.invoke(
            trade_group,
            ["search", "--category", "armour.helmet", "--gem-quality", "20", "--rarity", "nonunique", "--dump-query"],
        )
        assert r_nonuniq.exit_code == 0, r_nonuniq.output
        q_nu = json.loads(r_nonuniq.output)["query"]["query"]
        assert q_nu["filters"]["type_filters"]["filters"]["rarity"] == {"option": "nonunique"}

    # 6. Explicit domain never leaks to another domain, and option cache key tracks nested option IDs
    assert resolve_option_stat("Only Affects Passives in Very Large Ring", _SAMPLE_OPTION_ENTRIES, domain="enchant") is None
    with pytest.raises(ValueError, match="Could not map"):
        plan_stats([parse_stat_argument("crafted:+#% to all Elemental Resistances=10")], _SAMPLE_OPTION_ENTRIES)
    alt_entries = [dict(e) for e in _SAMPLE_OPTION_ENTRIES]
    alt_entries[-1] = {
        "id": "pseudo.pseudo_tangled_implicit_tier",
        "text": "Has Eater of Worlds Implicit Tier",
        "type": "pseudo",
        "option": {"options": [{"id": 1, "text": "Lesser"}, {"id": 2, "text": "Greater"}, {"id": 4, "text": "Grand"}]},
    }
    m_alt = resolve_option_stat("Has Eater of Worlds Implicit Tier", alt_entries, option_value="Grand")
    assert m_alt is not None and m_alt.option_id == 4


def test_real_engine_all_5_option_templates_contract() -> None:
    engine_root = find_engine_data()
    if engine_root is None:
        pytest.skip("No local PoB engine found")
    entries = load_local_trade_stats(pob_root=engine_root)
    assert entries

    m_cluster = resolve_option_stat(
        "Added Small Passive Skills grant: #",
        entries,
        option_value="+12% to Chaos Resistance",
        domain="enchant",
    )
    assert m_cluster is not None and m_cluster.id == "enchant.stat_3948993189" and isinstance(m_cluster.option_id, int)

    m_ring = resolve_option_stat("Only Affects Passives in Massive Ring", entries, domain="explicit")
    assert m_ring is not None and m_ring.id == "explicit.stat_3642528642" and isinstance(m_ring.option_id, int)

    m_imp = resolve_option_stat(
        "Passive Skills in Radius of # can be Allocated without being connected to your tree",
        entries,
        option_value="Acrobatics",
        domain="explicit",
    )
    assert m_imp is not None and m_imp.id == "explicit.stat_2422708892" and isinstance(m_imp.option_id, int)

    m_flame = resolve_option_stat(
        "Allocates # if you have the matching modifier on Forbidden Flame",
        entries,
        option_value="Profane Bloom",
        domain="explicit",
    )
    assert m_flame is not None and m_flame.id == "explicit.stat_2460506030" and isinstance(m_flame.option_id, int)

    m_flesh = resolve_option_stat(
        "Allocates # if you have the matching modifier on Forbidden Flesh",
        entries,
        option_value="Profane Bloom",
        domain="explicit",
    )
    assert m_flesh is not None and m_flesh.id == "explicit.stat_1190333629" and isinstance(m_flesh.option_id, int)
