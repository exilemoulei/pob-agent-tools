"""Tests for Layer 1: bilingual/slang `pob price` resolution and Watcher's Eye aura disambiguation."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pob_cli.cli_trade import trade_group
from pob_cli.external_data import ninja_price
from pob_cli.game_data.slang import resolve_player_slang
from pob_cli.guardrails import GuardrailScope, check_input, warning_context
from pob_cli.trade_query import parse_stat_argument, resolve_requests, resolve_stat

_SAMPLE_AURA_STATS: list[dict[str, Any]] = [
    {
        "id": "explicit.stat_1873457881",
        "text": "#% additional Physical Damage Reduction while affected by Determination",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3742808908",
        "text": "+# to Armour while affected by Determination",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2110586221",
        "text": "+# to Maximum Endurance Charges while affected by Determination",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2690790844",
        "text": "#% increased Life Recovery Rate while affected by Vitality",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3656959867",
        "text": "#% of Damage leeched as Life while affected by Vitality",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_1368271171",
        "text": "Gain # Life per Enemy Hit while affected by Vitality",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3240073117",
        "text": "#% increased Life Recovery rate",
        "type": "explicit",
    },
]

_FAKE_DENSE: dict[str, list[dict[str, Any]]] = {
    "Currency": [{"name": "Divine Orb", "chaos": 150.0, "count": 999}],
    "UniqueWeapon": [
        {"name": "Legacy of the Rose", "baseType": "Judgement Staff", "chaos": 120.0, "count": 40},
        {"name": "Cane of Kulemak", "baseType": "Serpentine Staff", "chaos": 80.0, "count": 30},
    ],
    "UniqueJewel": [
        {"name": "Watcher's Eye", "baseType": "Prismatic Jewel", "chaos": 300.0, "count": 100},
        {"name": "Sublime Vision", "baseType": "Prismatic Jewel", "chaos": 4500.0, "count": 15},
        {"name": "Thread of Hope", "baseType": "Crimson Jewel", "chaos": 250.0, "count": 50},
        {"name": "Impossible Escape", "baseType": "Viridian Jewel", "chaos": 180.0, "count": 35},
    ],
    "ForbiddenJewel": [
        {"name": "Forbidden Flesh", "variant": "Profane Bloom", "chaos": 1800.0, "count": 12},
        {"name": "Forbidden Flame", "variant": "Profane Bloom", "chaos": 1600.0, "count": 10},
    ],
    "UniqueAccessory": [
        {"name": "Mageblood", "baseType": "Heavy Belt", "chaos": 30000.0, "count": 20},
        {"name": "Nimis", "baseType": "Topaz Ring", "chaos": 9000.0, "count": 18},
    ],
    "BaseType": [
        {"name": "Archdemon Crown", "variant": "84", "chaos": 45.0, "count": 25},
    ],
    "SkillGem": [
        {"name": "Lightning Tendrils of Eccentricity", "chaos": 30.0, "count": 50},
        {"name": "Awakened Enlighten Support", "chaos": 22500.0, "count": 5},
    ],
}

_FAKE_STASH_GEMS: list[dict[str, Any]] = [
    {
        "name": "Lightning Tendrils of Eccentricity",
        "gemLevel": 21,
        "gemQuality": 20,
        "corrupted": True,
        "chaosValue": 600.0,
        "listingCount": 14,
    },
    {
        "name": "Lightning Tendrils of Eccentricity",
        "gemLevel": 20,
        "gemQuality": 20,
        "corrupted": False,
        "chaosValue": 45.0,
        "listingCount": 40,
    },
]

_FAKE_STASH_WEAPONS: list[dict[str, Any]] = [
    {
        "name": "Legacy of the Rose",
        "baseType": "Judgement Staff",
        "links": 6,
        "chaosValue": 900.0,
        "listingCount": 8,
    },
    {
        "name": "Legacy of the Rose",
        "baseType": "Judgement Staff",
        "links": 0,
        "chaosValue": 120.0,
        "listingCount": 40,
    },
]


def test_slang_map_covers_build_copying_terms_and_traditional() -> None:
    expected = {
        "玫瑰杖": "Legacy of the Rose",
        "苦难杖": "Cane of Kulemak",
        "离心电能": "Lightning Tendrils of Eccentricity",
        "離心電能": "Lightning Tendrils of Eccentricity",
        "恶魔头": "Archdemon Crown",
        "惡魔頭": "Archdemon Crown",
        "法血": "Mageblood",
        "猎首": "Headhunter",
        "尼米斯": "Nimis",
        "斯瓦林": "Svalinn",
        "原罪": "Original Sin",
        "崇高愿景": "Sublime Vision",
        "希望之线": "Thread of Hope",
        "不可能的逃脱": "Impossible Escape",
        "禁断之肉": "Forbidden Flesh",
        "禁斷之肉": "Forbidden Flesh",
        "禁断之炎": "Forbidden Flame",
        "守望之眼": "Watcher's Eye",
        "三相珠宝": "Watcher's Eye",
        "三相珠寶": "Watcher's Eye",
    }
    for term, canon in expected.items():
        hit = resolve_player_slang(term)
        assert hit is not None, f"missing slang for {term}"
        assert hit[0] == canon


def test_price_bilingual_gem_variant_and_detailed_unique() -> None:
    with (
        patch("pob_cli.external_data._fetch_dense_overviews", return_value=_FAKE_DENSE),
        patch("pob_cli.external_data.resolve_current_trade_league", return_value="Mirage"),
        patch(
            "pob_cli.external_data.ninja_price._fetch_stash_overviews_batch",
            return_value=({"SkillGem": _FAKE_STASH_GEMS, "UniqueWeapon": _FAKE_STASH_WEAPONS}, set(), set()),
        ),
    ):
        gem_res = ninja_price.query_price("离心电能", variant="21/20")
        assert gem_res["resolvedQuery"] == "Lightning Tendrils of Eccentricity"
        assert gem_res["detailed"] is True
        assert gem_res["totalMatches"] == 1
        assert gem_res["matches"][0]["gemLevel"] == 21
        assert gem_res["matches"][0]["gemQuality"] == 20
        assert gem_res["matches"][0]["chaosValue"] == 600.0

        staff_res = ninja_price.query_price("玫瑰杖", detailed=True)
        assert staff_res["resolvedQuery"] == "Legacy of the Rose"
        assert staff_res["totalMatches"] == 2
        assert staff_res["matches"][0]["links"] == 6

        jewel_res = ninja_price.query_price("三相珠宝")
        assert jewel_res["resolvedQuery"] == "Watcher's Eye"
        assert jewel_res["matches"][0]["name"] == "Watcher's Eye"

        crown_res = ninja_price.query_price("恶魔头")
        assert crown_res["resolvedQuery"] == "Archdemon Crown"
        assert crown_res["matches"][0]["name"] == "Archdemon Crown"


def test_price_awakened_and_foulborn_chinese_prefixes() -> None:
    with (
        patch("pob_cli.external_data._fetch_dense_overviews", return_value=_FAKE_DENSE),
        patch("pob_cli.external_data.resolve_current_trade_league", return_value="Mirage"),
    ):
        awk_res = ninja_price.query_price("启蒙强辅")
        assert awk_res["resolvedQuery"] == "Awakened Enlighten Support"
        assert awk_res["matches"][0]["name"] == "Awakened Enlighten Support"

        fb_res = ninja_price.query_price("秽生 法血")
        assert fb_res["resolvedQuery"] == "Mageblood"
        assert fb_res["matches"][0]["name"] == "Mageblood"


def test_watchers_eye_brackets_and_specific_match() -> None:
    for q in (
        "受到【坚定】影响时，有额外物理伤害减免",
        "受【坚定】影响时，获得额外物理伤害减免",
        "受坚定影响时额外物理伤害减免",
        "受「坚定」影响时，有 #% 额外物理伤害减免",
    ):
        m = resolve_stat(q, _SAMPLE_AURA_STATS)
        assert m is not None, f"failed to match {q}"
        assert m.id == "explicit.stat_1873457881"
        assert not m.disambiguation_candidates

    m_vit_hit = resolve_stat("受【活力】影响时，每击中回复生命", _SAMPLE_AURA_STATS)
    assert m_vit_hit is not None
    assert m_vit_hit.id == "explicit.stat_1368271171"


def test_watchers_eye_ambiguous_shorthand_surfaces_disambiguation_candidates() -> None:
    for q in ("受活力影响时", "受到【活力】影响时", "while affected by Vitality"):
        m = resolve_stat(q, _SAMPLE_AURA_STATS)
        assert m is not None, f"failed to match {q}"
        assert m.disambiguation_aura == "vitality"
        assert len(m.disambiguation_candidates) >= 3
        ids = {c["id"] for c in m.disambiguation_candidates}
        assert "explicit.stat_2690790844" in ids
        assert "explicit.stat_3656959867" in ids
        assert "explicit.stat_1368271171" in ids


def test_trade_stats_cli_emits_disambiguation_candidates() -> None:
    runner = CliRunner()
    with patch("pob_cli.cli_trade.load_trade_stats", return_value=(_SAMPLE_AURA_STATS, "local_engine")):
        res = runner.invoke(trade_group, ["stats", "受活力影响时", "--json"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert len(payload["disambiguationCandidates"]) >= 3
        assert "disambiguationWarning" in payload

        # Below-threshold aura query still populates disambiguationCandidates with all stats for that aura
        res_miss = runner.invoke(trade_group, ["stats", "受【坚定】影响时，未知胡言乱语词缀", "--json"])
        assert res_miss.exit_code == 1
        payload_miss = json.loads(res_miss.output)
        assert payload_miss["match"] is None
        cand_ids = {c["id"] for c in payload_miss["disambiguationCandidates"]}
        assert "explicit.stat_1873457881" in cand_ids
        assert "explicit.stat_3742808908" in cand_ids
        assert "explicit.stat_2110586221" in cand_ids


def test_additional_physical_damage_reduction_no_armour_trap_warn() -> None:
    with warning_context() as warns:
        check_input(
            "#% additional Physical Damage Reduction while affected by Determination",
            scope=GuardrailScope.STAT_QUERY,
            expected_game="poe1",
        )
        assert not any(w.rule_id == "FF_POE1_ARMOUR_MITIGATION" for w in warns)

    with warning_context() as warns:
        check_input("+# to Armour", scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
        assert any(w.rule_id == "FF_POE1_ARMOUR_MITIGATION" for w in warns)


def test_bare_aura_stat_rejected_in_planner_while_specific_clause_passes() -> None:
    bare_req = parse_stat_argument("受活力影响时")
    with pytest.raises(ValueError, match="Ambiguous bare aura modifier") as exc_info:
        resolve_requests([bare_req], _SAMPLE_AURA_STATS)
    msg = str(exc_info.value)
    assert "explicit.stat_2690790844" in msg
    assert "explicit.stat_1368271171" in msg

    specific_req = parse_stat_argument("受【坚定】影响时，获得额外物理伤害减免")
    resolve_requests([specific_req], _SAMPLE_AURA_STATS)
    assert specific_req.match is not None
    assert specific_req.match.id == "explicit.stat_1873457881"


def test_ninja_price_rejects_partial_zh_fallback_and_empty_detailed_narrowing() -> None:
    class _FakeGameData:
        @staticmethod
        def normalize_tw_query(s: str) -> str:
            return s

        @staticmethod
        def resolve_player_slang(_s: str) -> None:
            return None

        @staticmethod
        def resolve_non_gear_en(_s: str) -> None:
            return None

        @staticmethod
        def english_for_zh(_s: str) -> None:
            return None

        @staticmethod
        def fold_han(s: str) -> str:
            return s.strip().lower()

        @staticmethod
        def query_local_data(kind: str, _q: str, limit: int = 5) -> dict[str, Any]:
            if kind == "unique":
                return {"results": [{"name": "Wrong Partial Unique", "zh_name": "完全不匹配的长名称"}]}
            return {"results": []}

    assert ninja_price._resolve_zh_entity_name("不存在的暗金", _FakeGameData) == (None, None)

    stash_calls: list[Any] = []
    with (
        patch("pob_cli.external_data._fetch_dense_overviews", return_value=_FAKE_DENSE),
        patch("pob_cli.external_data.resolve_current_trade_league", return_value="Mirage"),
        patch(
            "pob_cli.external_data.ninja_price._fetch_stash_overviews_batch",
            side_effect=lambda *a, **kw: (stash_calls.append(a) or ({}, set(), set())),
        ),
    ):
        miss = ninja_price.query_price("definitely-not-an-item-xyz", detailed=True)
        assert miss["totalMatches"] == 0
        assert miss["matches"] == []
        assert stash_calls == []
