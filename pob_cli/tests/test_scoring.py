"""`--score` / `--floor`: parsing, weighted % math, and their use in `pob weights`."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

import pob_cli.cli as cli_mod
from pob_cli.cli import main
from pob_cli.cli_weights import compute_weights, format_weights_markdown
from pob_cli.scoring import floor_failures, parse_floors, parse_score, weighted_score


def test_parse_score_accepts_repeat_and_commas() -> None:
    assert parse_score(["CombinedDPS=1,TotalEHP=0.5", "Life=2"]) == {"CombinedDPS": 1.0, "TotalEHP": 0.5, "Life": 2.0}
    assert parse_score(None) == {}


@pytest.mark.parametrize("bad", ["CombinedDPS", "CombinedDPS=x", "Life=inf", "1bad=1", "Life=0"])
def test_parse_score_rejects_bad_terms(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_score([bad])


def test_parse_floors() -> None:
    assert parse_floors(["TotalEHP>=8,000", "ManaCost<=50"]) == [("TotalEHP", ">=", 8000.0), ("ManaCost", "<=", 50.0)]
    with pytest.raises(ValueError):
        parse_floors(["TotalEHP>8000"])


def test_weighted_score_and_floor_failures() -> None:
    assert weighted_score({"a": 10.0, "b": 4.0}, {"a": 1.0, "b": 0.5}) == 12.0
    assert weighted_score({"a": 10.0, "b": None}, {"a": 1.0, "b": 0.5}) is None  # unverified, never 0
    assert weighted_score({"a": 10.0}, {"a": 1.0, "b": 0.0}) == 10.0  # zero weight needs no value
    floors = [("x", ">=", 5.0), ("y", "<=", 1.0)]
    assert floor_failures({"x": 5.0, "y": 1.0}, floors) == []
    assert floor_failures({"x": 4.0, "y": None}, floors) == ["x>=5", "y<=1"]


_SWEEP = {
    "baseline": {"summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000, "combinedDps": 1000, "totalEhp": 10000}},
    "variants": [
        {"name": "dmg", "ok": True, "summary": {"primaryDps": 1100, "combinedDps": 1100, "totalEhp": 9000}},
        {"name": "life", "ok": True, "summary": {"primaryDps": 1000, "combinedDps": 1000, "totalEhp": 11500}},
        {"name": "both", "ok": True, "summary": {"primaryDps": 1040, "combinedDps": 1040, "totalEhp": 10400}},
    ],
}


def test_compute_weights_score_and_floor() -> None:
    w = compute_weights(_SWEEP, score={"CombinedDPS": 1.0, "TotalEHP": 0.5}, floors=[("TotalEHP", ">=", 9500.0)])
    # dmg: 10 - 5 = 5 but breaks the floor; life: 7.5; both: 4 + 2 = 6.
    assert [r["mod"] for r in w["weights"]] == ["life", "both", "dmg"]
    assert w["weights"][0]["score"] == pytest.approx(7.5)
    assert w["weights"][2]["floorFail"] == ["TotalEHP>=9500"]
    md = format_weights_markdown(w, target="main")
    assert "Score |" in md and "(breaks TotalEHP>=9500)" in md


def test_compute_weights_default_order_unchanged() -> None:
    assert [r["mod"] for r in compute_weights(_SWEEP)["weights"]] == ["dmg", "both", "life"]


def test_cmd_weights_rejects_unknown_score_stat_before_sweep(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_daemon_call", lambda action, *_a, **_k: calls.append(action) or {})
    r = CliRunner().invoke(main, ["weights", "--score", "Bogus=1"])
    assert r.exit_code == 1 and "Unknown --score/--floor stat Bogus" in r.output
    r2 = CliRunner().invoke(main, ["weights", "--floor", "TotalEHP>8000"])
    assert r2.exit_code == 1
    assert calls == []
