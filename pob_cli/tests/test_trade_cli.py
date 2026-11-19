"""CLI tests for `pob trade` (faked client, no network, no daemon)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.trade_api import CookieRequired, NoResults

FIXTURES = Path(__file__).parent / "fixtures"
STATS = json.loads((FIXTURES / "trade_stats.json").read_text(encoding="utf-8"))
FETCH = json.loads((FIXTURES / "trade_fetch.json").read_text(encoding="utf-8"))
GLOVES_TEXT = (FIXTURES / "item_gloves.txt").read_text(encoding="utf-8")


class FakeClient:
    """Deterministic stand-in for TradeClient."""

    instances: list["FakeClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.searches = 0
        self.fetches = 0
        FakeClient.instances.append(self)

    def stats(self, force_refresh: bool = False):
        return STATS["result"]

    def default_league(self) -> str:
        return "TestLeague"

    def search(self, league: str, query: dict):
        self.searches += 1
        self.last_query = query
        return {"id": "qid", "result": ["row_fire", "row_cold", "row_unique"], "total": 3}

    def fetch(self, ids: list[str], query_id: str):
        self.fetches += 1
        return FETCH["result"]

    def search_url(self, league: str, query_id: str) -> str:
        return f"https://example.test/{league}/{query_id}"


class NoResultsClient(FakeClient):
    def search(self, league: str, query: dict):
        raise NoResults()


class CookieClient(FakeClient):
    def search(self, league: str, query: dict):
        raise CookieRequired(403, "challenge")


@pytest.fixture(autouse=True)
def patch_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli_trade, "TradeClient", FakeClient)
    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", lambda: "TestLeague")
    monkeypatch.setattr(cli_trade, "_build_label", lambda: "Test Witch (level 90)")
    FakeClient.instances = []


def invoke(args: list[str]):
    return CliRunner().invoke(cli_trade.trade, args)


def test_dump_query_builds_count_group_without_searching() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--allow-conversion",
            "--dump-query",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    groups = payload["query"]["query"]["stats"]
    assert groups[0]["type"] == "count"
    assert len(groups[0]["filters"]) == 3
    assert payload["conversion"]["enabled"] is True
    assert payload["category"] == "armour.gloves"
    assert FakeClient.instances[0].searches == 0


def test_search_json_reports_conversion_and_candidates() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--allow-conversion",
            "--no-compare",
            "--pob",
            "off",
            "--json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["league"] == "TestLeague"
    assert payload["total"] == 3
    assert payload["fetched"] == 3
    by_name = {candidate["name"]: candidate for candidate in payload["candidates"]}
    fire = by_name["Doom Hand (Slink Gloves)"]
    assert len(fire["conversions"]) == 1
    assert fire["conversions"][0]["target"] == "cold"
    assert fire["conversions"][0]["tier"] == "S3"
    assert fire["conversions"][0]["rerolled"] is True
    unique = by_name["Atziri's Acuities (Legion Gloves)"]
    assert unique["conversions"] == []
    cold = by_name["Rift Claw (Slink Gloves)"]
    assert cold["conversions"] == []


def test_search_plain_query_has_and_group() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--dump-query",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    groups = payload["query"]["query"]["stats"]
    assert groups[0]["type"] == "and"
    assert payload["conversion"]["enabled"] is False


def test_search_markdown_mentions_unverified() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to maximum Life=60",
            "--no-compare",
            "--pob",
            "off",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Gear upgrade candidates" in result.output
    assert "not 0 upgrade" in result.output


def test_search_no_results_exit_code() -> None:
    cli_trade.TradeClient = NoResultsClient  # type: ignore[assignment]
    result = invoke(["search", "--slot", "gloves", "--stat", "to maximum Life=60", "--no-compare"])
    assert result.exit_code == 2


def test_search_cookie_required_exit_code() -> None:
    cli_trade.TradeClient = CookieClient  # type: ignore[assignment]
    result = invoke(["search", "--slot", "gloves", "--stat", "to maximum Life=60", "--no-compare"])
    assert result.exit_code == 3


def test_missing_slot_exit_code() -> None:
    result = invoke(["search", "--stat", "to maximum Life=60"])
    assert result.exit_code == 1


def test_convert_command_json() -> None:
    result = invoke(["convert", "--text", GLOVES_TEXT, "--to", "cold", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"] == "cold"
    assert len(payload["conversions"]) == 1
    assert "+35% to Cold Resistance" in payload["text"]


def test_convert_command_writes_file(tmp_path) -> None:
    out = tmp_path / "converted.txt"
    result = invoke(
        ["convert", "--text", GLOVES_TEXT, "--to", "cold", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "+35% to Cold Resistance" in out.read_text(encoding="utf-8")


def test_stats_command() -> None:
    result = invoke(["stats", "to Cold Resistance", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["match"]["id"] == "explicit.stat_3372524247"


def test_dump_query_adds_open_suffix_pseudo_filter() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--open-suffix",
            "--dump-query",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["openAffixes"] == {"prefix": False, "suffix": True, "any": False}
    pseudo_ids = [
        filter_["id"]
        for group in payload["query"]["query"]["stats"]
        for filter_ in group["filters"]
        if filter_["id"].startswith("pseudo.")
    ]
    assert "pseudo.pseudo_number_of_empty_suffix_mods" in pseudo_ids


def test_search_json_includes_bench_options() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--no-compare",
            "--pob",
            "off",
            "--json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["benchOptions"]
    assert any(option["affix"] == "suffix" for option in payload["benchOptions"])
    assert any("cold resistance" in option["text"].lower() for option in payload["benchOptions"])


def test_search_markdown_shows_bench_section() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--open-suffix",
            "--no-compare",
            "--pob",
            "off",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Crafting bench options" in result.output
    assert "open suffix" in result.output.lower()


def test_no_bench_hint_suppresses_options() -> None:
    result = invoke(
        [
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=40",
            "--no-bench-hint",
            "--no-compare",
            "--pob",
            "off",
            "--json",
        ]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["benchOptions"] == []
