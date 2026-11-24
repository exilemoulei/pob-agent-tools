"""Unit tests for pob_cli.trade_eval (scoring, inf/NaN-safe PoB eval, batch RPC, reporting)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from pob_cli.trade_eval import (
    DEFAULT_WEIGHTS,
    POB_EHP_WEIGHT,
    PREFERRED_BONUS,
    Candidate,
    PobEval,
    _pob_eval_from_snapshot,
    _safe_pct,
    compare_items,
    format_report,
    parse_weights,
    preferred_templates,
    report_json,
    score_deltas,
    sort_candidates,
    verify_candidate,
    verify_candidates_batch,
)
from pob_cli.trade_item import Conversion, parse_item_text


def _make_item(text: str):
    return parse_item_text(text)


def _make_candidate(
    text: str,
    *,
    price_amount: float | None = 50.0,
    price_currency: str | None = "chaos",
    score: float = 0.0,
    url: str = "https://www.pathofexile.com/trade/search/Standard/abc",
    listing_id: str | None = "listing-1",
    conversions: list[Conversion] | None = None,
    conversion_warnings: list[str] | None = None,
    pob: PobEval | None = None,
    pob_error: str | None = None,
    current_text: str | None = None,
) -> Candidate:
    item = _make_item(text)
    current = _make_item(current_text) if current_text else None
    deltas = compare_items(current, item) if current_text is not None else []
    computed_score = score if current_text is None else score_deltas(deltas)
    return Candidate(
        raw={"id": listing_id or "x"},
        item=item,
        item_text=text,
        price_amount=price_amount,
        price_currency=price_currency,
        score=computed_score,
        deltas=deltas,
        conversions=list(conversions or []),
        conversion_warnings=list(conversion_warnings or []),
        url=url,
        listing_id=listing_id,
        pob=pob,
        pob_error=pob_error,
    )


# ---------------------------------------------------------------------------
# 1. Weights & Mod Comparison (PoE1 clean + substring preferred_templates)
# ---------------------------------------------------------------------------


def test_default_weights_poe1_clean_no_spirit() -> None:
    assert "spirit" not in DEFAULT_WEIGHTS
    assert DEFAULT_WEIGHTS["life"] == 1.0
    assert DEFAULT_WEIGHTS["skill_level"] == 1.2
    assert DEFAULT_WEIGHTS["penalty"] == -0.5

    custom = parse_weights(["life=2.5", "SPEED=1.25"])
    assert custom["life"] == 2.5
    assert custom["speed"] == 1.25
    assert custom["resist"] == DEFAULT_WEIGHTS["resist"]

    with pytest.raises(ValueError, match="Unknown weight category"):
        parse_weights(["spirit=1.0"])
    with pytest.raises(ValueError, match="--weight needs"):
        parse_weights(["life"])
    with pytest.raises(ValueError, match="must be finite"):
        parse_weights(["life=inf"])
    with pytest.raises(ValueError, match="must be finite"):
        parse_weights(["life=nan"])


def test_compare_items_want_attack_speed_substring_boost() -> None:
    """Regression test for PR #1 bug: --want 'Attack Speed' must boost '#% increased attack speed'."""
    current = _make_item(
        """
        Rarity: RARE
        Old Grip
        Titan Gauntlets
        Implicits: 0
        +50 to maximum Life
        10% increased Attack Speed
        """
    )
    candidate = _make_item(
        """
        Rarity: RARE
        New Grip
        Titan Gauntlets
        Implicits: 0
        +50 to maximum Life
        16% increased Attack Speed
        """
    )

    base_deltas = compare_items(current, candidate)
    assert len(base_deltas) == 1
    assert base_deltas[0].template == "#% increased attack speed"
    assert base_deltas[0].delta == pytest.approx(6.0)
    assert base_deltas[0].is_gain is True
    base_contrib = base_deltas[0].contribution
    assert base_contrib == pytest.approx(6.0 * DEFAULT_WEIGHTS["speed"])

    prefs = preferred_templates(["Attack Speed"])
    assert prefs == {"attack speed"}

    boosted_deltas = compare_items(current, candidate, preferred=prefs)
    assert len(boosted_deltas) == 1
    assert boosted_deltas[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)
    assert score_deltas(boosted_deltas) == pytest.approx(round(base_contrib * PREFERRED_BONUS, 2))

    # Also works if caller passes raw mixed-case keyword directly or full mod text with numbers
    direct_boost = compare_items(current, candidate, preferred={"Attack Speed"})
    assert direct_boost[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)

    exact_pref = preferred_templates(["+16% increased Attack Speed"])
    assert exact_pref == {"#% increased attack speed"}
    exact_boost = compare_items(current, candidate, preferred=exact_pref)
    assert exact_boost[0].contribution == pytest.approx(base_contrib * PREFERRED_BONUS)


# ---------------------------------------------------------------------------
# 2. inf / NaN-Safe PobEval (CI builds)
# ---------------------------------------------------------------------------


def test_safe_pct_edge_cases() -> None:
    assert _safe_pct(1000.0, 1150.0) == 15.0
    assert _safe_pct(1000.0, 900.0) == -10.0
    assert _safe_pct("inf", "inf") is None
    assert _safe_pct(math.inf, math.inf) is None
    assert _safe_pct("inf", 50000.0) is None
    assert _safe_pct(50000.0, "inf") is None
    assert _safe_pct("-inf", "inf") is None
    assert _safe_pct("nan", 100.0) is None
    assert _safe_pct(math.nan, 100.0) is None
    assert _safe_pct(0.0, 0.0) == 0.0
    assert _safe_pct(0.0, 250.0) is None
    assert _safe_pct(None, 100.0) is None


def test_pob_eval_ci_build_inf_ehp_and_strict_json() -> None:
    """CI builds report TotalEHP='inf'; PobEval must never emit NaN/Infinity in Python or JSON."""
    pob = _pob_eval_from_snapshot(
        before={"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
        after={"primaryDpsKey": "CombinedDPS", "primaryDps": 1_125_000.0, "totalEhp": "inf", "life": 1.0},
        diff={"newWarnings": ["Missing 12 Int"], "resolvedWarnings": ["Uncapped Cold Res"]},
    )

    assert pob.dps_pct == pytest.approx(12.5)
    assert pob.ehp_pct is None
    assert pob.ehp_pct_str == "0% (inf)"
    assert pob.dps_pct_str == "+12.5%"
    assert pob.life_delta == 0.0
    assert math.isfinite(pob.combined_pct)
    assert pob.combined_pct == pytest.approx(12.5)
    assert pob.warnings == ("Missing 12 Int",)
    assert pob.resolved_warnings == ("Uncapped Cold Res",)

    # Direct construction with raw float('inf') and float('nan') must also serialize strictly
    raw_nonfinite = PobEval(
        dps_key="FullDPS",
        dps_before=math.inf,
        dps_after=50000.0,
        ehp_before=math.inf,
        ehp_after=math.inf,
        life_before=math.nan,
        life_after=1.0,
    )
    assert raw_nonfinite.dps_pct is None
    assert raw_nonfinite.ehp_pct is None
    assert raw_nonfinite.ehp_pct_str == "0% (inf)"
    assert raw_nonfinite.dps_pct_str == "inf -> 50,000"
    assert raw_nonfinite.life_delta == 0.0
    assert math.isfinite(raw_nonfinite.combined_pct)

    serialized_eval = json.dumps(pob.as_dict(), allow_nan=False)
    parsed_eval = json.loads(serialized_eval)
    assert parsed_eval["ehp_before"] == "inf"
    assert parsed_eval["ehp_after"] == "inf"
    assert parsed_eval["ehp_pct"] is None
    assert parsed_eval["dps_pct"] == 12.5

    serialized_raw = json.dumps(raw_nonfinite.as_dict(), allow_nan=False)
    parsed_raw = json.loads(serialized_raw)
    assert parsed_raw["dps_before"] == "inf"
    assert parsed_raw["ehp_before"] == "inf"
    assert parsed_raw["life_before"] == "nan"

    cand = _make_candidate(
        "Rarity: RARE\nCI Crown\nHubris Circlet\nImplicits: 0\n+250 to maximum Energy Shield",
        pob=pob,
    )
    payload = report_json([cand])
    dumped = json.dumps(payload, allow_nan=False)
    loaded = json.loads(dumped)
    assert loaded[0]["pob"]["ehp_before"] == "inf"
    assert loaded[0]["pob"]["ehp_pct"] is None


def test_pob_eval_finite_to_inf_ehp_transition() -> None:
    pob = PobEval(
        dps_key="CombinedDPS",
        dps_before=500_000.0,
        dps_after=550_000.0,
        ehp_before=50_000.0,
        ehp_after="inf",
        life_before=4000.0,
        life_after=4120.0,
    )
    assert pob.dps_pct == pytest.approx(10.0)
    assert pob.ehp_pct is None
    assert pob.ehp_pct_str == "50,000 -> inf"
    assert pob.life_delta == pytest.approx(120.0)
    assert pob.combined_pct == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 3. Batch & Single PoB Verification (trade_eval_items + fallback to item_try)
# ---------------------------------------------------------------------------


def test_verify_candidates_batch_single_rpc() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life", listing_id="a")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life", listing_id="b")
    c3 = _make_candidate("Rarity: RARE\nItem C\nRuby Ring\nImplicits: 0\n+80 to maximum Life", listing_id="c")

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, params))
        assert action == "trade_eval_items"
        return {
            "slotName": "Ring 1",
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": "inf", "life": 4000.0},
            "results": [
                {
                    "id": 0,
                    "ok": True,
                    "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": "inf", "life": 4000.0},
                    "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1100.0, "totalEhp": "inf", "life": 4120.0},
                    "diff": {"newWarnings": ["Low Dex"], "resolvedWarnings": []},
                },
                {
                    "id": 1,
                    "ok": False,
                    "error": "Could not parse item text",
                },
            ],
        }

    verify_candidates_batch([c1, c2, c3], slot="Ring 1", target="main", limit=2, daemon_call=fake_daemon)

    assert len(calls) == 1
    assert calls[0][0] == "trade_eval_items"
    assert len(calls[0][1]["items"]) == 2
    assert c1.pob is not None
    assert c1.pob.dps_pct == pytest.approx(10.0)
    assert c1.pob.ehp_pct is None
    assert c1.pob.warnings == ("Low Dex",)
    assert c1.pob_error is None

    assert c2.pob is None
    assert c2.pob_error == "Could not parse item text"

    # Candidate 3 was outside limit=2 and must remain untouched
    assert c3.pob is None
    assert c3.pob_error is None


def test_verify_candidates_batch_fallback_to_item_try() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life", listing_id="a")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life", listing_id="b")

    calls: list[str] = []

    def legacy_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(action)
        if action == "trade_eval_items":
            raise RuntimeError("Unknown daemon action 'trade_eval_items'.")
        assert action == "item_try"
        if "Item B" in params["raw"]:
            raise RuntimeError("Bridge parse error")
        return {
            "mutation": {
                "before": {"primaryDpsKey": "FullDPS", "primaryDps": 2000.0, "totalEhp": 50000.0, "life": 3500.0},
                "after": {"primaryDpsKey": "FullDPS", "primaryDps": 2200.0, "totalEhp": 55000.0, "life": 3600.0},
                "diff": {"newWarnings": [], "resolvedWarnings": ["Low Fire Res"]},
            }
        }

    verify_candidates_batch([c1, c2], slot="Ring 1", target="main", limit=5, daemon_call=legacy_daemon)

    assert calls == ["trade_eval_items", "item_try", "item_try"]
    assert c1.pob is not None
    assert c1.pob.dps_key == "FullDPS"
    assert c1.pob.dps_pct == pytest.approx(10.0)
    assert c1.pob.ehp_pct == pytest.approx(10.0)
    assert c1.pob.combined_pct == pytest.approx(10.0 + POB_EHP_WEIGHT * 10.0)
    assert c1.pob.resolved_warnings == ("Low Fire Res",)
    assert c1.pob_error is None

    assert c2.pob is None
    assert c2.pob_error == "Bridge parse error"


def test_verify_candidates_batch_error_and_limit_zero() -> None:
    c1 = _make_candidate("Rarity: RARE\nItem A\nRuby Ring\nImplicits: 0\n+60 to maximum Life")
    c2 = _make_candidate("Rarity: RARE\nItem B\nRuby Ring\nImplicits: 0\n+70 to maximum Life")

    called = False

    def fail_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        raise RuntimeError("Invalid slot 'Bogus'")

    verify_candidates_batch([c1, c2], slot="Bogus", limit=0, daemon_call=fail_daemon)
    assert called is False
    assert c1.pob is None and c1.pob_error is None

    verify_candidates_batch([c1, c2], slot="Bogus", limit=1, daemon_call=fail_daemon)
    assert called is True
    assert c1.pob is None
    assert c1.pob_error == "Invalid slot 'Bogus'"
    assert c2.pob is None and c2.pob_error is None

    # Partial batch response (missing id=1)
    c3 = _make_candidate("Rarity: RARE\nItem C\nRuby Ring\nImplicits: 0\n+60 to maximum Life")
    c4 = _make_candidate("Rarity: RARE\nItem D\nRuby Ring\nImplicits: 0\n+70 to maximum Life")

    def partial_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100.0, "totalEhp": 1000.0, "life": 1000.0},
            "results": [
                {
                    "id": 0,
                    "ok": True,
                    "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 110.0, "totalEhp": 1000.0, "life": 1000.0},
                    "diff": {},
                }
            ],
        }

    verify_candidates_batch([c3, c4], slot="Ring 1", limit=2, daemon_call=partial_daemon)
    assert c3.pob is not None and c3.pob.dps_pct == pytest.approx(10.0)
    assert c4.pob is None and c4.pob_error == "Missing PoB evaluation result."


def test_verify_candidate_single() -> None:
    cand = _make_candidate("Rarity: RARE\nSolo Ring\nRuby Ring\nImplicits: 0\n+60 to maximum Life")

    def fake_daemon(action: str, params: dict[str, Any]) -> dict[str, Any]:
        assert action == "item_try"
        assert params["apply"] is False
        assert params["slot"] == "Ring 1"
        return {
            "mutation": {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 500.0, "totalEhp": 20000.0, "life": 3000.0},
                "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 525.0, "totalEhp": 21000.0, "life": 3100.0},
                "diff": {"newWarnings": []},
            }
        }

    pob = verify_candidate(cand, slot="Ring 1", daemon_call=fake_daemon)
    assert pob.dps_pct == pytest.approx(5.0)
    assert pob.ehp_pct == pytest.approx(5.0)
    assert pob.life_delta == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 4. Sorting (score, price, pob — NaN-free)
# ---------------------------------------------------------------------------


def test_sort_candidates_modes() -> None:
    pob_ci = PobEval("CombinedDPS", 1000.0, 1150.0, "inf", "inf", 1.0, 1.0)  # combined 15.0
    pob_low = PobEval("CombinedDPS", 1000.0, 1020.0, 40000.0, 44000.0, 4000.0, 4050.0)  # 2 + 0.25*10 = 4.5

    c_expensive = _make_candidate(
        "Rarity: RARE\nExpensive\nRuby Ring\nImplicits: 0\n+70 to maximum Life",
        price_amount=200.0,
        score=50.0,
        pob=pob_low,
    )
    c_mid = _make_candidate(
        "Rarity: RARE\nCI Upgrade\nRuby Ring\nImplicits: 0\n+60 to maximum Life",
        price_amount=80.0,
        score=30.0,
        pob=pob_ci,
    )
    c_unpriced = _make_candidate(
        "Rarity: RARE\nUnpriced\nRuby Ring\nImplicits: 0\n+90 to maximum Life",
        price_amount=None,
        price_currency=None,
        score=90.0,
        pob=None,
    )

    by_score = sort_candidates([c_mid, c_expensive, c_unpriced], sort="score")
    assert [c.display_name for c in by_score] == [
        "Unpriced (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "CI Upgrade (Ruby Ring)",
    ]

    by_price = sort_candidates([c_unpriced, c_expensive, c_mid], sort="price")
    assert [c.display_name for c in by_price] == [
        "CI Upgrade (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "Unpriced (Ruby Ring)",
    ]

    by_pob = sort_candidates([c_unpriced, c_expensive, c_mid], sort="pob")
    assert [c.display_name for c in by_pob] == [
        "CI Upgrade (Ruby Ring)",
        "Expensive (Ruby Ring)",
        "Unpriced (Ruby Ring)",
    ]


# ---------------------------------------------------------------------------
# 5. Markdown Report (with/without PoB, conversions, bench options, pipe escaping)
# ---------------------------------------------------------------------------


def test_format_report_without_pob_and_empty() -> None:
    empty_md = format_report(
        build_label="Demo Build",
        slot="Ring 1",
        current=None,
        candidates=[],
        league="Standard",
        status="securable",
        budget=None,
        currency="chaos",
        search_url="",
        total=0,
        fetched=0,
    )
    assert "No analysable candidates." in empty_md
    assert "- Status: securable" in empty_md
    assert "- Budget: unlimited" in empty_md

    cand = _make_candidate(
        "Rarity: RARE\nPlain | Ring\nRuby Ring\nImplicits: 0\n+75 to maximum Life",
        current_text="Rarity: RARE\nOld Ring\nRuby Ring\nImplicits: 0\n+40 to maximum Life",
    )
    md = format_report(
        build_label="Life Slayer",
        slot="Ring 1",
        current=_make_item("Rarity: RARE\nOld Ring\nRuby Ring\nImplicits: 0\n+40 to maximum Life"),
        candidates=[cand],
        league="Mirage",
        budget=100.0,
        currency="chaos",
        search_url="https://www.pathofexile.com/trade/search/Mirage/xyz",
        total=12,
        fetched=1,
    )
    table_lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert table_lines
    assert "PoB DPS" not in table_lines[0]
    assert "PoB EHP" not in table_lines[0]
    assert "Plain \\| Ring" in md
    assert "- Budget: 100 chaos" in md
    assert "Mod score is a weighted template delta" in md


def test_format_report_with_pob_conversions_bench_and_escaping() -> None:
    current = _make_item(
        """
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """
    )
    conv = Conversion(
        template="#% to cold resistance",
        converted_template="#% to fire resistance",
        element="cold",
        target="fire",
        group="resistance",
        subkind="resistance",
        values=(46.0,),
        cost="300 Lifeforce (Primal/Vivid/Wild matching target)",
        patch="3.30",
        line="+46% to Fire Resistance",
        tier="S1",
        tier_range="46-48",
    )
    pob_ci = PobEval(
        dps_key="CombinedDPS",
        dps_before=1_000_000.0,
        dps_after=1_085_000.0,
        ehp_before="inf",
        ehp_after="inf",
        life_before=1.0,
        life_after=1.0,
        warnings=("Attribute requirement | Str short",),
        resolved_warnings=("Fire Resistance uncapped",),
    )

    c1 = _make_candidate(
        """
        Rarity: RARE
        Storm | Band
        Amethyst Ring
        Implicits: 0
        +75 to maximum Life
        +46% to Fire Resistance
        """,
        current_text="""
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """,
        price_amount=1.5,
        price_currency="divine",
        conversions=[conv],
        conversion_warnings=["Item has a crafted target modifier; remove it at the crafting bench before Harvest conversion."],
        pob=pob_ci,
    )
    c2 = _make_candidate(
        """
        Rarity: RARE
        Unverified Band
        Amethyst Ring
        Implicits: 0
        +65 to maximum Life
        """,
        current_text="""
        Rarity: RARE
        Old Band
        Amethyst Ring
        Implicits: 0
        +50 to maximum Life
        +35% to Fire Resistance
        """,
        price_amount=40.0,
        price_currency="chaos",
        pob=None,
        pob_error="Timed out evaluating item",
    )

    bench_options = [
        {
            "affix": "prefix",
            "text": "+(55-70) to maximum Life",
            "textZh": "+(55-70) 最大生命",
            "values": [{"min": 55, "max": 70}],
            "craftedLinesMin": ["{crafted}+55 to maximum Life"],
            "cost": "4x Chaos Orb",
        },
        {
            "affix": "suffix",
            "text": "+(20-25)% to Fire Resistance",
            "values": [{"min": 20, "max": 25}],
            "cost": "2x Orb of Alchemy",
        },
    ]

    md = format_report(
        build_label="CI Trickster",
        slot="Ring 1",
        current=current,
        candidates=[c1, c2],
        league="Mirage",
        status="securable",
        budget=5.0,
        currency="divine",
        search_url="https://www.pathofexile.com/trade/search/Mirage/q123",
        total=42,
        fetched=2,
        conversion_note="Harvest resistance swap enabled (cold/lightning -> fire)",
        open_affixes={"prefix": True, "suffix": False},
        bench_options=bench_options,
    )

    assert "PoB DPS" in md and "PoB EHP" in md
    assert "0% (inf)" in md
    assert "+8.5%" in md
    assert "Storm \\| Band" in md
    assert "Attribute requirement \\| Str short" in md
    assert "PoB resolved warnings: Fire Resistance uncapped" in md
    assert "PoB verification failed: Timed out evaluating item" in md
    assert "tier S1; range 46-48; 3.30 rules; same tier, roll re-rolled inside the tier" in md
    assert "remove it at the crafting bench before Harvest conversion." in md
    assert "{crafted}+55 to maximum Life" in md
    assert "{crafted}+20% to Fire Resistance" in md
    assert "Open affixes: prefix" in md
    assert "- Status: securable" in md

    # Verify JSON payload for the full mixed report is strictly RFC-8259 valid
    raw_json = json.dumps(report_json([c1, c2]), allow_nan=False)
    decoded = json.loads(raw_json)
    assert len(decoded) == 2
    assert decoded[0]["pob"]["ehp_pct"] is None
    assert decoded[0]["conversions"][0]["tier"] == "S1"
    assert decoded[0]["conversions"][0]["tier_range"] == "46-48"
    assert decoded[1]["pob"] is None
    assert decoded[1]["pob_error"] == "Timed out evaluating item"


# ---------------------------------------------------------------------------
# 6. Pipelined Snapshot, Analyzer, Comparator & Fast In-Place eval_trade_items
# ---------------------------------------------------------------------------


class _FakeBatchWorker:
    """In-memory mock BridgeWorker tracking both single `call()` and pipelined `call_batch()` calls."""

    def __init__(
        self,
        *,
        support_batch: bool = True,
        translate_enabled: bool | None = True,
        stats: dict[str, Any] | None = None,
        lock: Any | None = None,
    ) -> None:
        import threading

        self.lock = lock if lock is not None else threading.RLock()
        self.caps = {"translateToggle": True}
        self.translate_enabled = translate_enabled
        self.hello_info = {"pobVersion": "2.68.1"}
        self.single_calls: list[tuple[str, dict[str, Any]]] = []
        self.batch_calls: list[list[tuple[str, dict[str, Any]]]] = []
        self.all_methods: list[str] = []
        self._support_batch = support_batch
        self._code_ver = 0
        self._current_code = "BASE_CODE"
        self.last_good_code = "ORIG_LAST_GOOD"
        self._base_stats = dict(
            stats
            or {
                "CombinedDPS": 1_000_000.0,
                "TotalDPS": 1_000_000.0,
                "AverageDamage": 200_000.0,
                "Speed": 5.0,
                "TotalEHP": "inf",
                "Life": 4000.0,
                "EnergyShield": 1000.0,
                "FireResist": 75.0,
                "ColdResist": 75.0,
                "LightningResist": 75.0,
                "ChaosResist": 20.0,
                "PhysicalMaximumHitTaken": 18000.0,
                "FireMaximumHitTaken": 40000.0,
                "ColdMaximumHitTaken": 35000.0,
                "LightningMaximumHitTaken": 38000.0,
                "ChaosMaximumHitTaken": "inf",
            }
        )
        self._state_stats = dict(self._base_stats)
        self._next_item_id = 100
        self._items: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "Old Ring", "title": "Old Ring", "baseName": "Ruby Ring", "type": "Ring", "rarity": "RARE"},
            2: {"id": 2, "name": "Old Bow", "title": "Old Bow", "baseName": "Thicket Bow", "type": "Bow", "rarity": "RARE"},
        }
        self._slots: list[dict[str, Any]] = [
            {"name": "Ring 1", "label": "Ring 1", "selItemId": 1, "shown": True},
            {"name": "Ring 2", "label": "Ring 2", "selItemId": None, "shown": True},
            {"name": "Weapon 1", "label": "Weapon 1", "selItemId": 2, "shown": True},
            {"name": "Flask 1", "label": "Flask 1", "isFlask": True, "active": True, "selItemId": None, "shown": True},
            {"name": "Jewel 1000", "label": "Jewel 1", "nodeId": 1000, "socketIndex": 1, "selItemId": None, "shown": True},
        ]
        self._tree_nodes: dict[str, dict[str, Any]] = {
            "1000": {"id": 1000, "name": "Jewel Socket", "type": "Socket", "x": 0.0, "y": 0.0, "stats": []},
            "1001": {"id": 1001, "name": "Close Notable", "nameZh": "近点核心", "type": "Notable", "x": 500.0, "y": 0.0, "stats": ["+30 to maximum Life"]},
            "1002": {"id": 1002, "name": "Medium Keystone", "type": "Keystone", "x": 1200.0, "y": 0.0, "stats": ["Chaos Inoculation"]},
            "1003": {"id": 1003, "name": "Large Notable", "type": "Notable", "x": 1700.0, "y": 0.0, "stats": ["+20% to Fire Resistance"]},
            "1004": {"id": 1004, "name": "Very Large Notable", "type": "Notable", "x": 2200.0, "y": 0.0, "stats": ["10% increased Attack Speed"]},
            "1005": {"id": 1005, "name": "Massive Notable", "type": "Notable", "x": 2700.0, "y": 0.0, "stats": ["+50 to Intelligence"]},
            "1006": {"id": 1006, "name": "Too Far Notable", "type": "Notable", "x": 4000.0, "y": 0.0, "stats": ["+10 to Strength"]},
            "2000": {
                "id": 2000,
                "name": "Life Mastery",
                "nameZh": "生命专精",
                "type": "Mastery",
                "x": 600.0,
                "y": 600.0,
                "stats": [],
                "masteryEffects": [
                    {"effect": 55001, "stats": ["+50 to maximum Life"], "statsZh": ["+50 最大生命"]},
                    {"effect": 55002, "stats": ["15% increased maximum Life if no Life Modifiers on Body Armour"], "statsZh": ["胸甲无生命词缀时最大生命提高 15%"]},
                    {"effect": 55003, "stats": ["10% more Maximum Life"], "statsZh": ["最大生命额外提高 10%"]},
                ],
            },
            "3000": {"id": 3000, "name": "Arcane Blessing", "type": "Notable", "asc": "Hierophant", "x": 100.0, "y": 100.0, "stats": ["Gain Arcane Surge"]},
        }
        self._allocated = {1000, 1001, 3000}
        self._code_snapshots: dict[str, dict[str, Any]] = {"BASE_CODE": dict(self._base_stats)}

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_26", "nodes": self._tree_nodes}

    def _exec(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.all_methods.append(method)
        if method == "get_stats":
            return {"stats": dict(self._state_stats)}
        if method == "get_sidebar":
            return {"rows": [], "warnings": [{"text": "Low Dex"}]}
        if method == "get_build_info":
            return {"level": 95, "className": "Templar", "ascendClassName": "Hierophant", "treeVersion": "3_26", "points": {"used": 110, "usedMax": 123, "ascUsed": 8, "ascMax": 8}}
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1, "list": ["Group 1"]}}
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "label": "Main",
                        "enabled": True,
                        "gems": [
                            {"index": 1, "name": "Arc", "level": 20, "quality": 20, "enabled": True, "support": False},
                            {"index": 2, "name": "Spell Echo", "level": 20, "quality": 20, "enabled": True, "support": True},
                        ],
                    }
                ],
            }
        if method == "list_specs":
            return {"activeSpec": 1, "specs": [{"index": 1, "title": "Default"}]}
        if method == "list_items":
            return {"activeItemSetId": 1, "itemSets": [], "items": list(self._items.values()), "slots": [dict(s) for s in self._slots]}
        if method == "item_raw":
            iid = int(params["id"])
            return {"raw": f"Rarity: RARE\nItem #{iid}\nRuby Ring\nImplicits: 0\n+50 to maximum Life"}
        if method == "gem_tooltip":
            return {"dps": {"pct": 12.5, "delta": 125000.0}}
        if method == "gem_candidate_tooltip":
            return {"dps": {"pct": 15.0, "delta": 150000.0}}
        if method == "get_gem_options":
            return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
        if method == "set_gem_options":
            return {"ok": True}
        if method == "gem_search":
            return {
                "dpsField": "CombinedDPS",
                "baseDps": 1_000_000.0,
                "gems": [
                    {"gemId": f"gem_{i}", "name": f"Support {i}", "dps": 1_100_000 + i * 1000}
                    for i in range(6)
                ],
            }
        if method == "get_tree_state":
            return {
                "treeVersion": "3_26",
                "className": "Templar",
                "ascendClassName": "Hierophant",
                "points": {"used": 110, "usedMax": 123},
                "allocatedNodes": sorted(self._allocated),
                "dynamicNodes": [],
                "overrides": {},
                "sockets": [{"nodeId": 1000}],
            }
        if method == "node_hover":
            nid = int(params["id"])
            cost = 0 if nid in self._allocated else (nid % 7) + 1
            return {"id": nid, "allocated": nid in self._allocated, "cost": cost, "path": [1000, nid] if cost else [], "depends": [nid] if nid in self._allocated else []}
        if method == "list_config":
            return {
                "activeConfigSetId": 1,
                "configSets": [{"id": 1, "title": "Default"}],
                "customMods": [],
                "sections": [
                    {
                        "name": "General",
                        "nameZh": "常规",
                        "items": [
                            {
                                "var": "enemyIsBoss",
                                "label": "Is the enemy a Boss?",
                                "labelZh": "敌人是否为首领",
                                "type": "list",
                                "value": "None",
                                "visible": True,
                                "list": [
                                    {"val": "None", "label": "Standard Boss"},
                                    {"val": "Pinnacle", "label": "Guardian/Pinnacle Boss"},
                                    {"val": "Uber", "label": "Uber Pinnacle Boss"},
                                ],
                            },
                            {
                                "var": "usePowerCharges",
                                "label": "Do you use Power Charges?",
                                "labelZh": "是否使用暴击球",
                                "type": "check",
                                "value": True,
                                "visible": True,
                            },
                            {
                                "var": "multiplierWitheredStackCount",
                                "label": "# of Withered Stacks",
                                "labelZh": "凋零层数",
                                "type": "count",
                                "value": None,
                                "visible": True,
                            },
                        ],
                    }
                ],
            }
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method in ("reset_config", "set_config", "set_custom_mods", "set_slot_active", "set_gem", "party_action"):
            if method == "set_config" and params.get("var") == "enemyIsBoss":
                if params.get("value") == "Pinnacle":
                    self._state_stats["CombinedDPS"] = 800_000.0
                elif params.get("value") == "Uber":
                    self._state_stats["CombinedDPS"] = 500_000.0
            if method == "set_slot_active" and params.get("active") is False:
                self._state_stats["CombinedDPS"] = min(float(self._state_stats["CombinedDPS"]), 700_000.0)
            return {"ok": True}
        if method == "export_code":
            self._code_ver += 1
            code = f"SNAP_{self._code_ver}"
            self._code_snapshots[code] = dict(self._state_stats)
            self.last_good_code = code
            return {"code": code}
        if method == "import_code":
            code = params["code"]
            self._current_code = code
            self._state_stats = dict(self._code_snapshots.get(code, self._base_stats))
            self.last_good_code = code
            return {"ok": True}
        if method == "add_item":
            self._next_item_id += 1
            nid = self._next_item_id
            raw = params["raw"]
            bonus = 100_000.0 if "Upgrade" in raw else 10_000.0
            self._state_stats["CombinedDPS"] = float(self._base_stats["CombinedDPS"]) + bonus
            self._state_stats["Life"] = float(self._base_stats["Life"]) + 80.0
            itype = "Bow" if "Bow" in raw else "Ring"
            self._items[nid] = {"id": nid, "name": f"Cand {nid}", "type": itype, "rarity": "RARE"}
            slot_name = params.get("slotName")
            for s in self._slots:
                if s["name"] == slot_name:
                    s["selItemId"] = nid
            return {"item": self._items[nid]}
        if method == "equip_item":
            slot_name = params["slotName"]
            iid = int(params["id"])
            for s in self._slots:
                if s["name"] == slot_name:
                    s["selItemId"] = iid
            self._state_stats = dict(self._base_stats)
            return {"ok": True}
        if method == "delete_item":
            self._items.pop(int(params["id"]), None)
            return {"ok": True}
        if method == "get_calcs":
            return {
                "sections": [
                    {
                        "si": 1,
                        "id": "Skill",
                        "group": "Offence",
                        "subsections": [
                            {
                                "ui": 1,
                                "label": "Speed",
                                "labelZh": "速度",
                                "rows": [
                                    {
                                        "ri": 1,
                                        "label": "Cast Rate",
                                        "labelZh": "施法速度",
                                        "cells": [
                                            {"ci": 1, "text": "3.201 秒", "raw": "3.201 s", "hasBreakdown": True},
                                        ],
                                    },
                                    {
                                        "ri": 2,
                                        "label": "Crit Chance",
                                        "labelZh": "暴击率",
                                        "cells": [
                                            {"ci": 1, "text": "75.5%", "raw": "75.5%", "hasBreakdown": True},
                                        ],
                                    },
                                    {
                                        "ri": 3,
                                        "label": "Combined DPS",
                                        "labelZh": "综合秒伤",
                                        "cells": [
                                            {"ci": 1, "text": "1,234,567", "raw": "1,234,567", "hasBreakdown": True},
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        if method == "calcs_breakdown":
            return {"sections": [{"type": "text", "lines": [f"Breakdown {params['ri']}"]}]}
        raise AssertionError(f"Unexpected RPC method: {method}({params})")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        p = params or {}
        self.single_calls.append((method, p))
        return self._exec(method, p)

    def __getattr__(self, name: str) -> Any:
        if name == "call_batch" and self._support_batch:
            def _batch(
                calls: list[tuple[str, dict[str, Any] | None]],
                timeout: float = 30.0,
                raise_on_error: bool = True,
            ) -> list[dict[str, Any]]:
                norm = [(m, p or {}) for m, p in calls]
                self.batch_calls.append(norm)
                out = []
                for m, p in norm:
                    if raise_on_error:
                        out.append(self._exec(m, p))
                    else:
                        try:
                            out.append(self._exec(m, p))
                        except Exception as exc:
                            out.append({"_error": str(exc)})
                return out
            return _batch
        raise AttributeError(name)


def test_snapshot_pipelined_digest_and_compact_summary() -> None:
    from pob_cli.snapshot import capture_digest, compact_digest_summary

    w = _FakeBatchWorker(support_batch=True)
    dig = capture_digest(w)
    assert len(w.batch_calls) == 1
    assert [m for m, _ in w.batch_calls[0]] == ["get_stats", "get_sidebar", "get_build_info", "get_build_header"]
    assert dig["totalEhp"] == "inf"
    assert dig["primaryDps"] == 1_000_000.0
    assert dig["warnings"] == ["Low Dex"]

    compact = compact_digest_summary(dig)
    assert compact["build"]["className"] == "Templar"
    assert compact["defence"]["TotalEHP"] == "inf"
    assert compact["defence"]["EleMaxHit"] == 35000.0
    assert compact["coreStats"]["CombinedDPS"] == 1_000_000.0
    # Strictly RFC-8259 JSON-safe
    json.dumps(compact, allow_nan=False)

    # Fallback works seamlessly on workers without call_batch
    w_legacy = _FakeBatchWorker(support_batch=False)
    dig_legacy = capture_digest(w_legacy)
    assert dig_legacy["primaryDps"] == 1_000_000.0
    assert len(w_legacy.single_calls) == 4


def test_analyzer_pipelined_summary_items_skills_config_tree_calcs() -> None:
    from pob_cli.analyzer import (
        extract_summary,
        inspect_calcs,
        inspect_config,
        inspect_items,
        inspect_skills,
        inspect_tree,
    )

    # 1. extract_summary drops unused list_config and pipelines in 1 batch
    w = _FakeBatchWorker()
    summary = extract_summary(w)
    assert len(w.batch_calls) == 1
    assert "list_config" not in w.all_methods
    assert summary["offence"]["CombinedDPS"] == 1_000_000.0

    # 2. inspect_items(all_raw=True) pipelines item_raw in 1 batch
    w = _FakeBatchWorker()
    items_res = inspect_items(w, all_raw=True)
    assert items_res["equippedCount"] == 2
    raw_batches = [b for b in w.batch_calls if b and b[0][0] == "item_raw"]
    assert len(raw_batches) == 1 and len(raw_batches[0]) == 2

    # 3. inspect_skills pipelines gem_tooltip in 1 batch
    w = _FakeBatchWorker()
    sk_res = inspect_skills(w, group_selector="main")
    assert len(sk_res["group"]["gems"]) == 2
    tip_batches = [b for b in w.batch_calls if b and b[0][0] == "gem_tooltip"]
    assert len(tip_batches) == 1 and len(tip_batches[0]) == 2

    # 4. inspect_config omits options by default, keeps options on search or with_options=True
    w = _FakeBatchWorker()
    cfg_default = inspect_config(w)
    boss_var = next(v for v in cfg_default["variables"] if v["var"] == "enemyIsBoss")
    assert "options" not in boss_var

    cfg_with_opts = inspect_config(w, with_options=True)
    boss_var_opts = next(v for v in cfg_with_opts["variables"] if v["var"] == "enemyIsBoss")
    assert len(boss_var_opts["options"]) == 3

    cfg_search = inspect_config(w, search="wither")
    assert [v["var"] for v in cfg_search["variables"]] == ["multiplierWitheredStackCount"]

    cfg_search_boss = inspect_config(w, search="首领")
    assert cfg_search_boss["variables"][0]["var"] == "enemyIsBoss"
    assert len(cfg_search_boss["variables"][0]["options"]) == 3

    # 5. inspect_tree: drops list_specs, pipelines node_hover, multi-query, show_path, masteries, socket_radius
    w = _FakeBatchWorker()
    t_multi = inspect_tree(
        w,
        queries=["Chaos Inoculation", "Life Mastery"],
        show_path=True,
        masteries=True,
    )
    assert "list_specs" not in w.all_methods
    assert t_multi["totalMatches"] == 2
    hover_batches = [b for b in w.batch_calls if b and b[0][0] == "node_hover"]
    assert len(hover_batches) == 1 and len(hover_batches[0]) == 2
    mastery_node = next(m for m in t_multi["matches"] if m["id"] == 2000)
    assert len(mastery_node["masteryEffects"]) == 3
    assert mastery_node["masteryEffects"][0]["effect"] == 55001
    assert mastery_node["path"] == [1000, 2000]

    # Node type + allocated filter
    w = _FakeBatchWorker()
    t_asc = inspect_tree(w, node_types=["Ascendancy"], allocated=True)
    assert [m["id"] for m in t_asc["matches"]] == [3000]

    # Socket radius across Small / Medium / Large / Very Large / Massive
    w = _FakeBatchWorker()
    t_rad = inspect_tree(w, socket_radius="#1")
    assert t_rad["socketRadius"]["socketNodeId"] == 1000
    assert t_rad["socketRadius"]["id"] == 1000
    by_id = {m["id"]: m for m in t_rad["matches"]}
    assert by_id[1001]["radiusTier"] == "Small"
    assert by_id[1002]["radiusTier"] == "Medium"
    assert by_id[1003]["radiusTier"] == "Large"
    assert by_id[1004]["radiusTier"] == "Very Large"
    assert by_id[1005]["radiusTier"] == "Massive"
    assert 1006 not in by_id  # outside 2880 Massive radius
    assert 3000 not in by_id  # ascendancy node excluded
    # Verify format_tree_markdown renders socket_radius & masteries cleanly without TypeError
    from pob_cli.formatters import format_tree_markdown

    rad_md = format_tree_markdown(t_rad)
    assert "Jewel Socket Radius: `1000`" in rad_md
    assert "Radius `Small`" in rad_md
    multi_md = format_tree_markdown(t_multi, show_path=True, masteries=True)
    assert "2000:55001" in multi_md
    assert "1000 -> 2000" in multi_md

    # 6. inspect_calcs: skips version{} when translate_enabled tracked, variadic keywords, raw+value, no_breakdown
    w = _FakeBatchWorker(translate_enabled=True)
    calc_multi = inspect_calcs(w, keywords=["Cast Rate", "Crit Chance"])
    assert "version" not in w.all_methods
    assert calc_multi["keywords"] == ["Cast Rate", "Crit Chance"]
    bd_batches = [b for b in w.batch_calls if b and b[0][0] == "calcs_breakdown"]
    assert len(bd_batches) == 1 and len(bd_batches[0]) == 2
    cast_cell = calc_multi["results"][0]["matches"][0]["cells"][0]
    assert cast_cell["text"] == "3.201 秒"
    assert cast_cell["raw"] == "3.201 s"
    assert cast_cell["value"] == pytest.approx(3.201)
    crit_cell = calc_multi["results"][1]["matches"][0]["cells"][0]
    assert crit_cell["value"] == pytest.approx(75.5)

    w_nobd = _FakeBatchWorker(translate_enabled=True)
    calc_nobd = inspect_calcs(w_nobd, "Combined DPS", no_breakdown=True)
    assert "calcs_breakdown" not in w_nobd.all_methods
    assert calc_nobd["matches"][0]["cells"][0]["value"] == pytest.approx(1_234_567.0)


def test_comparator_rank_gems_matrix_cache_and_concurrent_compare() -> None:
    from pob_cli.comparator import compare_builds, rank_gems, run_matrix

    # 1. rank_gems pipelines top-5 gem_candidate_tooltip in ONE batch
    w = _FakeBatchWorker()
    rg = rank_gems(w)
    tip_batches = [b for b in w.batch_calls if b and b[0][0] == "gem_candidate_tooltip"]
    assert len(tip_batches) == 1 and len(tip_batches[0]) == 5
    assert rg["candidates"][0]["tooltipDps"]["pct"] == 15.0

    # 2. run_matrix caches Pinnacle base ONCE across multiple Pinnacle/Uber scenarios and emits compact JSON by default
    w = _FakeBatchWorker()
    mat = run_matrix(w, ["Original", "Pinnacle", "Uber", "PinnacleWither:Pinnacle,multiplierWitheredStackCount=10", "FlasksOff"])
    assert mat["full"] is False
    # Dewater read batch (list_config) runs ONLY ONCE despite 3 Pinnacle/Uber scenarios!
    dewater_reads = [b for b in w.batch_calls if b and b[0][0] == "list_config"]
    assert len(dewater_reads) == 1
    # Compact output omits raw 200-key stats & buildHeader while preserving summary + coreStats + digest headline
    sc0 = mat["scenarios"][0]
    assert "summary" in sc0 and "coreStats" in sc0 and "deltasVsFirst" in sc0
    assert "stats" not in sc0["digest"]
    assert "buildHeader" not in sc0["digest"]
    assert sc0["digest"]["primaryDps"] == 1_000_000.0
    assert w.last_good_code == "ORIG_LAST_GOOD"

    # Full mode preserves complete digest
    mat_full = run_matrix(w, ["Original", "Pinnacle"], full=True)
    assert mat_full["full"] is True
    assert "stats" in mat_full["scenarios"][0]["digest"]
    assert "buildHeader" in mat_full["scenarios"][0]["digest"]
    assert w.last_good_code == "ORIG_LAST_GOOD"

    # 3. compare_builds works both with separate locks (concurrent) and shared lock (fallback) + handles 'inf' EHP
    mw = _FakeBatchWorker()
    rw = _FakeBatchWorker()
    cmp_res = compare_builds(mw, rw)
    assert "pinnacleDewatered" in cmp_res["baselines"]
    assert mw.last_good_code == "ORIG_LAST_GOOD"
    assert rw.last_good_code == "ORIG_LAST_GOOD"
    json.dumps(cmp_res, allow_nan=False)

    rw_shared = _FakeBatchWorker(lock=mw.lock)
    cmp_shared = compare_builds(mw, rw_shared)
    assert "original" in cmp_shared["baselines"]


def test_eval_trade_items_fast_inplace_swap_and_complex_fallback() -> None:
    from pob_cli.trade_eval import eval_trade_items

    # 1. Standard Ring 1 slot with known prev_item_id: fast in-place swap for 3 items, only 1 final import_code!
    w = _FakeBatchWorker()
    items = [
        {"id": 0, "raw": "Rarity: RARE\nUpgrade Ring A\nRuby Ring\nImplicits: 0\n+70 to maximum Life"},
        {"id": 1, "raw": "Rarity: RARE\nUpgrade Ring B\nRuby Ring\nImplicits: 0\n+80 to maximum Life"},
        {"id": 2, "raw": "Rarity: RARE\nPlain Ring C\nRuby Ring\nImplicits: 0\n+50 to maximum Life"},
    ]
    res = eval_trade_items(w, slot_name="Ring 1", items=items)
    assert len(res["results"]) == 3
    assert all(r["ok"] for r in res["results"])
    assert res["results"][0]["after"]["primaryDps"] == 1_100_000.0
    assert res["results"][2]["after"]["primaryDps"] == 1_010_000.0
    # Fast swap batches (equip_item + delete_item) used between items 0->1 and 1->2
    swap_batches = [b for b in w.batch_calls if len(b) == 2 and b[0][0] == "equip_item" and b[1][0] == "delete_item"]
    assert len(swap_batches) == 2
    # Final safety-net import_code called ONLY ONCE (not 3 times!)
    import_count = sum(1 for m in w.all_methods if m == "import_code")
    assert import_count == 1
    assert w.last_good_code == "ORIG_LAST_GOOD"

    # 2. Complex 2H Weapon 1 slot falls back cleanly to full import_code isolation per item
    w_2h = _FakeBatchWorker()
    bow_items = [
        {"id": 0, "raw": "Rarity: RARE\nBow A\nThicket Bow\nImplicits: 0\n+100 to maximum Life"},
        {"id": 1, "raw": "Rarity: RARE\nBow B\nThicket Bow\nImplicits: 0\n+120 to maximum Life"},
    ]
    res_2h = eval_trade_items(w_2h, slot_name="Weapon 1", items=bow_items)
    assert len(res_2h["results"]) == 2
    assert all(r["ok"] for r in res_2h["results"])
    swap_batches_2h = [b for b in w_2h.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]
    assert len(swap_batches_2h) == 0
    assert sum(1 for m in w_2h.all_methods if m == "import_code") == 2

    # 3. Item granting a skill on a Ring slot forces full import_code isolation for subsequent candidates
    w_grant = _FakeBatchWorker()
    grant_items = [
        {"id": 0, "raw": "Rarity: UNIQUE\nGrant Ring\nUnset Ring\nImplicits: 0\nGrants Level 20 Aspect of the Spider Skill"},
        {"id": 1, "raw": "Rarity: RARE\nPlain Ring\nRuby Ring\nImplicits: 0\n+50 to maximum Life"},
    ]
    res_grant = eval_trade_items(w_grant, slot_name="Ring 1", items=grant_items)
    assert len(res_grant["results"]) == 2
    assert all(r["ok"] for r in res_grant["results"])
    assert sum(1 for m in w_grant.all_methods if m == "import_code") == 2

    # 4. 'Grants ... Skill' without 'Level' (e.g. Death Aura) also triggers full import_code isolation
    w_grant_nolv = _FakeBatchWorker()
    grant_nolv_items = [
        {"id": 0, "raw": "Rarity: UNIQUE\nDeath's Oath\nAstral Plate\nImplicits: 0\nGrants Death Aura Skill"},
        {"id": 1, "raw": "Rarity: RARE\nPlain Ring\nRuby Ring\nImplicits: 0\n+50 to maximum Life"},
    ]
    res_nolv = eval_trade_items(w_grant_nolv, slot_name="Ring 1", items=grant_nolv_items)
    assert len(res_nolv["results"]) == 2
    assert sum(1 for m in w_grant_nolv.all_methods if m == "import_code") == 2


def test_eval_trade_items_abyssal_inactive_vs_active_and_dual_wield_guard() -> None:
    from pob_cli.analyzer import inspect_items
    from pob_cli.trade_eval import eval_trade_items

    # 1. Primary gear slot with PoB's default inactive=True Abyssal Socket placeholder MUST still use fast-swap!
    w_inactive = _FakeBatchWorker()
    w_inactive._items[10] = {"id": 10, "name": "Old Belt", "type": "Belt", "rarity": "RARE"}
    w_inactive._slots.extend(
        [
            {"name": "Belt", "label": "Belt", "selItemId": 10, "shown": True},
            {"name": "Belt Abyssal Socket 1", "label": "Belt Abyssal Socket 1", "inactive": True, "selItemId": None},
        ]
    )
    belt_items = [
        {"id": 0, "raw": "Rarity: RARE\nUpgrade Belt A\nLeather Belt\nImplicits: 0\n+90 to maximum Life"},
        {"id": 1, "raw": "Rarity: RARE\nUpgrade Belt B\nLeather Belt\nImplicits: 0\n+95 to maximum Life"},
    ]
    res_inact = eval_trade_items(w_inactive, slot_name="Belt", items=belt_items)
    assert all(r["ok"] for r in res_inact["results"])
    swap_inact = [b for b in w_inactive.batch_calls if len(b) == 2 and b[0][0] == "equip_item" and b[1][0] == "delete_item"]
    assert len(swap_inact) == 1
    assert sum(1 for m in w_inactive.all_methods if m == "import_code") == 1

    # 2. Active (inactive=False) Abyssal Socket on Belt (e.g. Stygian Vise) MUST disable fast-swap!
    w_active = _FakeBatchWorker()
    w_active._items[10] = {"id": 10, "name": "Stygian Belt", "type": "Belt", "rarity": "RARE"}
    w_active._slots.extend(
        [
            {"name": "Belt", "label": "Belt", "selItemId": 10, "shown": True},
            {"name": "Belt Abyssal Socket 1", "label": "Belt Abyssal Socket 1", "inactive": False, "selItemId": None},
        ]
    )
    res_act = eval_trade_items(w_active, slot_name="Belt", items=belt_items)
    assert all(r["ok"] for r in res_act["results"])
    swap_act = [b for b in w_active.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]
    assert len(swap_act) == 0
    assert sum(1 for m in w_active.all_methods if m == "import_code") == 2

    # 3. Dual-wield guard: Weapon 1 + Weapon 2 equipped -> changing Weapon 1 class disables fast-swap; same class keeps fast-swap
    class _WeaponWorker(_FakeBatchWorker):
        def _exec(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "add_item":
                self._next_item_id += 1
                nid = self._next_item_id
                raw = params["raw"]
                itype = "Wand" if "Wand" in raw else "Claw"
                self._items[nid] = {"id": nid, "name": f"Wep {nid}", "type": itype, "rarity": "RARE"}
                for s in self._slots:
                    if s["name"] == params.get("slotName"):
                        s["selItemId"] = nid
                return {"item": self._items[nid]}
            return super()._exec(method, params)

    # 3a. Class change (Claw -> Wand) with Weapon 2 equipped -> disables fast-swap
    w_dw_chg = _WeaponWorker()
    w_dw_chg._items[20] = {"id": 20, "name": "Main Claw", "type": "Claw", "rarity": "RARE"}
    w_dw_chg._items[21] = {"id": 21, "name": "Offhand Claw", "type": "Claw", "rarity": "RARE"}
    w_dw_chg._slots = [
        {"name": "Weapon 1", "label": "Weapon 1", "selItemId": 20, "shown": True},
        {"name": "Weapon 2", "label": "Weapon 2", "selItemId": 21, "shown": True},
    ]
    wep_change_items = [
        {"id": 0, "raw": "Rarity: RARE\nCaster Wand\nProphecy Wand\nImplicits: 0\n+20% Spell Damage"},
        {"id": 1, "raw": "Rarity: RARE\nAttack Claw\nImperial Claw\nImplicits: 0\n+20% Attack Speed"},
    ]
    res_dw_chg = eval_trade_items(w_dw_chg, slot_name="Weapon 1", items=wep_change_items)
    assert all(r["ok"] for r in res_dw_chg["results"])
    assert len([b for b in w_dw_chg.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]) == 0
    assert sum(1 for m in w_dw_chg.all_methods if m == "import_code") == 2

    # 3b. Same class (Claw -> Claw) with Weapon 2 equipped -> fast-swap stays enabled!
    w_dw_same = _WeaponWorker()
    w_dw_same._items[20] = {"id": 20, "name": "Main Claw", "type": "Claw", "rarity": "RARE"}
    w_dw_same._items[21] = {"id": 21, "name": "Offhand Claw", "type": "Claw", "rarity": "RARE"}
    w_dw_same._slots = [
        {"name": "Weapon 1", "label": "Weapon 1", "selItemId": 20, "shown": True},
        {"name": "Weapon 2", "label": "Weapon 2", "selItemId": 21, "shown": True},
    ]
    same_cls_items = [
        {"id": 0, "raw": "Rarity: RARE\nClaw A\nImperial Claw\nImplicits: 0\n+20% Attack Speed"},
        {"id": 1, "raw": "Rarity: RARE\nClaw B\nImperial Claw\nImplicits: 0\n+25% Attack Speed"},
    ]
    res_dw_same = eval_trade_items(w_dw_same, slot_name="Weapon 1", items=same_cls_items)
    assert all(r["ok"] for r in res_dw_same["results"])
    assert len([b for b in w_dw_same.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]) == 1
    assert sum(1 for m in w_dw_same.all_methods if m == "import_code") == 1

    # 4. inspect_items includes buildInfo in both multi-slot and single-slot modes (0 extra tick cost)
    w_info = _FakeBatchWorker()
    all_res = inspect_items(w_info, all_raw=True)
    assert all_res["buildInfo"]["className"] == "Templar"
    single_res = inspect_items(w_info, slot_name="Ring 2")
    assert single_res["buildInfo"]["ascendClassName"] == "Hierophant"


def test_call_batch_ok_false_error_handling_in_analyzer_and_comparator() -> None:
    from pob_cli.analyzer import inspect_skills, inspect_tree, search_calcs_breakdown
    from pob_cli.comparator import rank_gems

    class _PartialOkFalseWorker(_FakeBatchWorker):
        def call_batch(
            self,
            calls: list[tuple[str, dict[str, Any] | None]],
            timeout: float = 30.0,
            raise_on_error: bool = True,
        ) -> list[dict[str, Any]]:
            out = []
            for m, p in calls:
                if m in ("gem_tooltip", "node_hover", "calcs_breakdown", "gem_candidate_tooltip"):
                    out.append({"ok": False, "error": f"{m} failed"})
                else:
                    out.append(self._exec(m, p or {}))
            return out

    w = _PartialOkFalseWorker()
    sk = inspect_skills(w, group_selector="main")
    assert all(g["tooltipDps"] is None for g in sk["group"]["gems"])

    tr = inspect_tree(w, search="Chaos Inoculation", show_path=True)
    assert tr["totalMatches"] == 1
    assert "cost" not in tr["matches"][0]

    calc = search_calcs_breakdown(w, "Cast Rate")
    assert calc["totalMatches"] == 1
    assert "breakdown" not in calc["matches"][0]["cells"][0]

    rg = rank_gems(w)
    assert all("tooltipDps" not in c for c in rg["candidates"])


def test_inspect_tree_max_cost_proximity_presort_bfs_and_euclidean() -> None:
    from pob_cli.analyzer import inspect_tree

    # Case 1: BFS hop distance over adjacency (linked + out/in) finds high-ID nearby notables
    # even when >100 low-ID distant notables exist!
    w_bfs = _FakeBatchWorker()
    nodes: dict[str, dict[str, Any]] = {
        "1000": {"id": 1000, "name": "Start", "type": "Normal", "x": 0.0, "y": 0.0, "linked": [90001, 50000]},
        "90001": {"id": 90001, "name": "Near Notable 1", "type": "Notable", "x": 100.0, "y": 0.0, "linked": [1000, 90002]},
        "90002": {"id": 90002, "name": "Near Notable 2", "type": "Notable", "x": 200.0, "y": 0.0, "out": [90001]},
        "50000": {"id": 50000, "name": "Bridge", "type": "Normal", "x": 50.0, "y": 50.0, "in": [90003]},
        "90003": {"id": 90003, "name": "Near Notable 3", "type": "Notable", "x": 150.0, "y": 50.0, "out": [50000]},
    }
    # Add 100 low-ID distant notables (IDs 1..100) connected in a far chain
    prev_far = 50000
    for i in range(1, 11):
        fid = 50000 + i
        nodes[str(fid)] = {"id": fid, "name": f"Far Road {i}", "type": "Normal", "x": float(i * 100), "y": 500.0, "linked": [prev_far]}
        prev_far = fid
    for i in range(1, 101):
        nodes[str(i)] = {"id": i, "name": f"Distant Notable {i}", "type": "Notable", "x": 2000.0 + i, "y": 2000.0, "linked": [prev_far]}
    w_bfs._tree_nodes = nodes
    w_bfs._allocated = {1000}

    orig_exec = w_bfs._exec

    def _bfs_exec(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "node_hover":
            nid = int(params["id"])
            cost_map = {1000: 0, 90001: 1, 90002: 2, 90003: 2}
            cost = cost_map.get(nid, 12)
            return {"id": nid, "allocated": nid == 1000, "cost": cost, "path": []}
        return orig_exec(method, params)

    w_bfs._exec = _bfs_exec  # type: ignore[method-assign]
    res_bfs = inspect_tree(w_bfs, node_types=["Notable"], allocated=False, max_cost=2)
    matched_ids = [m["id"] for m in res_bfs["matches"]]
    assert matched_ids == [90001, 90002, 90003]

    # Case 2: Euclidean fallback when adjacency is absent
    w_euc = _FakeBatchWorker()
    euc_nodes: dict[str, dict[str, Any]] = {
        "1000": {"id": 1000, "name": "Start", "type": "Normal", "x": 0.0, "y": 0.0},
        "95001": {"id": 95001, "name": "Close Notable A", "type": "Notable", "x": 80.0, "y": 20.0},
        "95002": {"id": 95002, "name": "Close Notable B", "type": "Notable", "x": 120.0, "y": 40.0},
    }
    for i in range(1, 101):
        euc_nodes[str(i)] = {"id": i, "name": f"Far Notable {i}", "type": "Notable", "x": 5000.0 + i, "y": 5000.0}
    w_euc._tree_nodes = euc_nodes
    w_euc._allocated = {1000}

    orig_euc_exec = w_euc._exec

    def _euc_exec(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "node_hover":
            nid = int(params["id"])
            cost = 1 if nid in (95001, 95002) else 15
            return {"id": nid, "allocated": False, "cost": cost, "path": []}
        return orig_euc_exec(method, params)

    w_euc._exec = _euc_exec  # type: ignore[method-assign]
    res_euc = inspect_tree(w_euc, node_types=["Notable"], allocated=False, max_cost=2)
    assert [m["id"] for m in res_euc["matches"]] == [95001, 95002]


def test_apply_env_config_preserves_zero_overrides() -> None:
    from pob_cli.comparator import _apply_env_config

    w = _FakeBatchWorker()
    _apply_env_config(
        w,
        {
            "enemyIsBoss": "Pinnacle",
            "enemyFireResist": 0,
            "enemyChaosResist": 0,
            "enemyLevel": None,
            "EHPUnluckyWorstOf": False,
            "deliriousPercentage": "",
        },
    )
    assert len(w.batch_calls) == 1
    by_var = {p["var"]: (m, p) for m, p in w.batch_calls[0]}
    assert by_var["enemyFireResist"] == ("set_config", {"var": "enemyFireResist", "value": 0})
    assert by_var["enemyChaosResist"] == ("set_config", {"var": "enemyChaosResist", "value": 0})
    assert by_var["enemyIsBoss"] == ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})
    assert by_var["enemyLevel"] == ("reset_config", {"var": "enemyLevel"})
    assert by_var["EHPUnluckyWorstOf"] == ("reset_config", {"var": "EHPUnluckyWorstOf"})
    assert by_var["deliriousPercentage"] == ("reset_config", {"var": "deliriousPercentage"})


def test_eval_trade_items_sourceless_explode_group_and_chinese_raw_disable_fast_swap() -> None:
    from pob_cli.trade_eval import eval_trade_items

    # 1. Source-less Explode skill group (source="Explode", slot=None) disables fast-swap on Body Armour
    class _ExplodeSkillWorker(_FakeBatchWorker):
        def _exec(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "list_skills":
                return {
                    "mainSocketGroup": 2,
                    "groups": [
                        {"index": 1, "label": "On Kill Monster Explosion", "source": "Explode", "slot": None, "enabled": True, "gems": []},
                        {"index": 2, "isMain": True, "label": "Main", "enabled": True, "gems": []},
                    ],
                }
            return super()._exec(method, params)

    w_exp = _ExplodeSkillWorker()
    w_exp._items[30] = {"id": 30, "name": "Explode Chest", "type": "Body Armour", "rarity": "RARE"}
    w_exp._slots.append({"name": "Body Armour", "label": "Body Armour", "selItemId": 30, "shown": True})
    chest_items = [
        {"id": 0, "raw": "Rarity: RARE\nPlain Chest A\nVaal Regalia\nImplicits: 0\n+100 to maximum Life"},
        {"id": 1, "raw": "Rarity: RARE\nPlain Chest B\nVaal Regalia\nImplicits: 0\n+110 to maximum Life"},
    ]
    res_exp = eval_trade_items(w_exp, slot_name="Body Armour", items=chest_items)
    assert all(r["ok"] for r in res_exp["results"])
    assert len([b for b in w_exp.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]) == 0
    assert sum(1 for m in w_exp.all_methods if m == "import_code") == 2

    # 2. English 'Explode' mod and Chinese complex raw keywords disable fast-swap
    for raw_text in (
        "Rarity: RARE\nCrusader Chest\nAstral Plate\nImplicits: 0\nEnemies you Kill Explode, dealing 3% of their Life as Physical Damage",
        "稀 有 度: 传奇\nSpider Ring\n潜能之戒\nImplicits: 0\n赋予 20 级蜘蛛之势技能",
        "Rarity: RARE\nCN Explode\n星芒战铠\nImplicits: 0\n被击杀的敌人会爆炸",
        "Rarity: RARE\nCN Abyss\n深渊腰带\nImplicits: 0\n拥有 1 个深渊插槽",
        "Rarity: RARE\nTW Abyss\n深淵腰帶\nImplicits: 0\n擁有 1 個深淵插槽",
        "Rarity: RARE\nTW Grant\n潛能之戒\nImplicits: 0\n賦予 20 級蜘蛛之勢技能",
    ):
        w_cx = _FakeBatchWorker()
        res_cx = eval_trade_items(
            w_cx,
            slot_name="Ring 1",
            items=[
                {"id": 0, "raw": raw_text},
                {"id": 1, "raw": "Rarity: RARE\nPlain Ring\nRuby Ring\nImplicits: 0\n+50 to maximum Life"},
            ],
        )
        assert all(r["ok"] for r in res_cx["results"])
        assert len([b for b in w_cx.batch_calls if len(b) == 2 and b[0][0] == "equip_item"]) == 0
        assert sum(1 for m in w_cx.all_methods if m == "import_code") == 2
