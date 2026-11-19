"""Unit tests for candidate comparison, PoB verification parsing and reports."""

from __future__ import annotations

from typing import Any

from pob_cli.poe_item import convert_item_text, parse_item_text
from pob_cli.trade_compare import (
    Candidate,
    compare_items,
    format_report,
    parse_weights,
    report_json,
    score_deltas,
    sort_candidates,
    verify_candidate,
)

CURRENT_TEXT = "\n".join(
    [
        "Rarity: RARE",
        "Old Gloves",
        "Slink Gloves",
        "Item Level: 80",
        "+20% to Cold Resistance",
        "+50 to maximum Life",
    ]
)

CANDIDATE_TEXT = "\n".join(
    [
        "Rarity: RARE",
        "New Gloves",
        "Slink Gloves",
        "Item Level: 84",
        "+30% to Cold Resistance",
        "+70 to maximum Life",
        "20% increased Attack Speed",
    ]
)


def make_candidate(text: str = CANDIDATE_TEXT, price: float = 10.0) -> Candidate:
    return Candidate(
        raw={},
        item=parse_item_text(text),
        item_text=text,
        price_amount=price,
        price_currency="chaos",
        score=1.0,
    )


def test_compare_items_finds_gains_and_losses() -> None:
    current = parse_item_text(CURRENT_TEXT)
    candidate = parse_item_text(CANDIDATE_TEXT)
    deltas = compare_items(current, candidate)
    by_template = {delta.template: delta for delta in deltas}

    assert by_template["#% to cold resistance"].delta == 10
    assert by_template["# to maximum life"].delta == 20
    assert by_template["#% increased attack speed"].delta == 20
    assert all(delta.contribution > 0 for delta in deltas)


def test_compare_items_without_current_item() -> None:
    deltas = compare_items(None, parse_item_text(CANDIDATE_TEXT))
    assert {delta.template for delta in deltas} == {
        "#% to cold resistance",
        "# to maximum life",
        "#% increased attack speed",
    }


def test_score_and_weights() -> None:
    current = parse_item_text(CURRENT_TEXT)
    candidate = parse_item_text(CANDIDATE_TEXT)
    deltas = compare_items(current, candidate)
    assert score_deltas(deltas) > 0
    heavy = compare_items(current, candidate, weights={**parse_weights([]), "resist": 2.0})
    assert score_deltas(heavy) > score_deltas(deltas)


def test_verify_candidate_parses_mutation() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_item_try(action: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((action, params))
        return {
            "mutation": {
                "before": {
                    "primaryDpsKey": "CombinedDPS",
                    "primaryDps": 1000.0,
                    "totalEhp": 5000.0,
                    "life": 500.0,
                },
                "after": {
                    "primaryDpsKey": "CombinedDPS",
                    "primaryDps": 1100.0,
                    "totalEhp": 5200.0,
                    "life": 520.0,
                },
                "diff": {"newWarnings": ["Attribute requirements not met"]},
            }
        }

    candidate = make_candidate()
    evaluation = verify_candidate(candidate, slot="Gloves", item_try=fake_item_try)
    assert calls and calls[0][0] == "item_try"
    assert calls[0][1]["apply"] is False
    assert calls[0][1]["slot"] == "Gloves"
    assert evaluation.dps_pct == 10.0
    assert evaluation.ehp_pct == 4.0
    assert evaluation.life_delta == 20.0
    assert evaluation.warnings == ("Attribute requirements not met",)


def test_sort_candidates() -> None:
    cheap = make_candidate(price=5.0)
    cheap.score = 1.0
    pricey = make_candidate(price=15.0)
    pricey.score = 3.0
    verified = make_candidate(price=20.0)
    verified.score = 0.5
    verified.pob = verify_candidate(
        verified,
        slot="Gloves",
        item_try=lambda action, params, **kwargs: {
            "mutation": {
                "before": {"primaryDps": 1000.0, "totalEhp": 5000.0, "life": 500.0},
                "after": {"primaryDps": 1200.0, "totalEhp": 5100.0, "life": 500.0},
            }
        },
    )

    assert [candidate.score for candidate in sort_candidates([cheap, pricey], "score")] == [3.0, 1.0]
    assert [candidate.price_amount for candidate in sort_candidates([pricey, cheap], "price")] == [
        5.0,
        15.0,
    ]
    assert sort_candidates([cheap, verified], "pob")[0] is verified


def test_report_json_includes_conversion_and_pob() -> None:
    candidate = make_candidate()
    converted = convert_item_text(candidate.item_text, "fire")
    candidate.conversions = converted.conversions
    candidate.pob = verify_candidate(
        candidate,
        slot="Gloves",
        item_try=lambda action, params, **kwargs: {
            "mutation": {
                "before": {"primaryDps": 100.0, "totalEhp": 200.0, "life": 300.0},
                "after": {"primaryDps": 90.0, "totalEhp": 210.0, "life": 301.0},
            }
        },
    )
    payload = report_json([candidate])[0]
    assert payload["conversions"][0]["target"] == "fire"
    assert payload["pob"]["dps_pct"] == -10.0
    assert payload["price"] == {"amount": 10.0, "currency": "chaos"}
    assert payload["item_text"] == CANDIDATE_TEXT


def test_format_report_marks_unverified_candidates() -> None:
    candidate = make_candidate()
    candidate.deltas = compare_items(parse_item_text(CURRENT_TEXT), candidate.item)
    report = format_report(
        build_label="Test Witch (level 90)",
        slot="gloves",
        current=parse_item_text(CURRENT_TEXT),
        candidates=[candidate],
        league="Standard",
        budget=20,
        currency="chaos",
        search_url="https://www.pathofexile.com/trade/search/Standard/q1",
        total=10,
        fetched=3,
    )
    assert "PoB" in report
    assert "not verified" in report or "`-`" in report
    assert "Test Witch" in report


def test_format_report_includes_open_affix_and_bench_options() -> None:
    candidate = make_candidate()
    candidate.deltas = compare_items(parse_item_text(CURRENT_TEXT), candidate.item)
    report = format_report(
        build_label="Test Witch",
        slot="gloves",
        current=parse_item_text(CURRENT_TEXT),
        candidates=[candidate],
        league="Standard",
        budget=20,
        currency="chaos",
        search_url="",
        total=10,
        fetched=3,
        open_affixes={"prefix": False, "suffix": True, "any": False},
        bench_options=[
            {
                "affix": "suffix",
                "text": "+(16-20)% to Cold Resistance",
                "textZh": "+(16—20)% 冰霜抗性",
                "values": [{"min": 16.0, "max": 20.0}],
                "cost": "1x Orb of Transmutation",
            }
        ],
    )
    assert "Open affixes: suffix" in report
    assert "Crafting bench options for gloves" in report
    assert "[suffix] +(16-20)% to Cold Resistance" in report
    assert "Bench modifiers roll below" in report
