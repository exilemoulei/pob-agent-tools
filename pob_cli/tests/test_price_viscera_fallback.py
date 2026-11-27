"""Offline tests for 3.30 Greater Viscera prefix stripping and wombgift <-> viscera fallback."""

from __future__ import annotations

from typing import Any

import pytest

from pob_cli import external_data
from pob_cli.external_data.ninja_price import query_price, query_price_multi


def _mock_overviews() -> dict[str, list[dict[str, Any]]]:
    return {
        "Currency": [{"name": "Divine Orb", "chaos": 150.0}],
        "Viscera": [
            {"name": "Brute's Arm", "chaos": 220.0, "divineValue": 1.47},
            {"name": "Flaming Goat Hoof", "chaos": 180.0, "divineValue": 1.2},
            {"name": "Searing Exarch's Descry", "chaos": 600.0, "divineValue": 4.0},
        ],
        "Wombgift": [
            {"name": "Grafted Heart", "chaos": 95.0, "divineValue": 0.63},
        ],
        "SkillGem": [
            {"name": "Greater Multiple Projectiles Support", "chaos": 2.0},
        ],
        "ImbuedGem": [
            {"name": "Greater Spell Echo", "chaos": 450.0, "divineValue": 3.0},
        ],
    }


@pytest.fixture
def _patch_ninja(monkeypatch: pytest.MonkeyPatch) -> None:
    overviews = _mock_overviews()
    monkeypatch.setattr(external_data, "resolve_current_trade_league", lambda proxy=None: "Mirage")
    monkeypatch.setattr(external_data, "_fetch_dense_overviews", lambda league, proxy=None: overviews)


def test_wombgift_multi_falls_back_to_viscera_and_strips_greater(_patch_ninja: None) -> None:
    res = query_price_multi(
        ["Greater Brute's Arm", "Greater Flaming Goat Hoof", "Searing Exarch's Descry"],
        category="wombgift",
    )
    assert res["count"] == 3
    r0, r1, r2 = res["results"]
    assert r0["totalMatches"] == 1
    assert r0["matches"][0]["name"] == "Brute's Arm"
    assert r0["categoryFallback"] == "viscera"
    assert r0["resolvedQuery"] == "Brute's Arm"
    assert r0["resolvedAlias"] == {"from": "Greater Brute's Arm", "to": "Brute's Arm"}

    assert r1["totalMatches"] == 1
    assert r1["matches"][0]["name"] == "Flaming Goat Hoof"
    assert r1["categoryFallback"] == "viscera"
    assert r1["resolvedQuery"] == "Flaming Goat Hoof"

    assert r2["totalMatches"] == 1
    assert r2["matches"][0]["name"] == "Searing Exarch's Descry"
    assert r2["categoryFallback"] == "viscera"
    assert "resolvedQuery" not in r2


def test_greater_viscera_strips_prefix_in_all_category(_patch_ninja: None) -> None:
    res = query_price("Greater Brute's Arm", category="all")
    assert res["totalMatches"] == 1
    assert res["matches"][0]["name"] == "Brute's Arm"
    assert res["matches"][0]["type"] == "Viscera"
    assert res["resolvedQuery"] == "Brute's Arm"
    assert "categoryFallback" not in res


def test_greater_spell_echo_matches_directly_without_stripping(_patch_ninja: None) -> None:
    res = query_price("Greater Spell Echo", category="all")
    assert res["totalMatches"] == 1
    assert res["matches"][0]["name"] == "Greater Spell Echo"
    assert res["matches"][0]["type"] == "ImbuedGem"
    assert "resolvedQuery" not in res
    assert "resolvedAlias" not in res


def test_viscera_category_falls_back_to_wombgift(_patch_ninja: None) -> None:
    res = query_price("Grafted Heart", category="viscera")
    assert res["totalMatches"] == 1
    assert res["matches"][0]["name"] == "Grafted Heart"
    assert res["matches"][0]["type"] == "Wombgift"
    assert res["categoryFallback"] == "wombgift"


def test_detailed_sibling_fallback_preserves_stale_flag_on_stripped_retry(
    _patch_ninja: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.external_data import ninja_price as np_mod

    overviews = _mock_overviews()
    stash_calls: list[list[str]] = []

    def _fake_stash_batch(
        league: str, types: list[str] | tuple[str, ...], *, proxy: str | None = None, max_workers: int = 6
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str], set[str]]:
        t_list = list(types)
        stash_calls.append(t_list)
        data = {t: overviews.get(t, []) for t in t_list}
        stale = {"Viscera"} if "Viscera" in t_list else set()
        return data, stale, set()

    monkeypatch.setattr(np_mod, "_fetch_stash_overviews_batch", _fake_stash_batch)
    res = query_price("Greater Brute's Arm", category="wombgift", detailed=True)
    assert res["totalMatches"] == 1
    assert res["matches"][0]["name"] == "Brute's Arm"
    assert res["categoryFallback"] == "viscera"
    assert res["resolvedQuery"] == "Brute's Arm"
    assert res.get("stale") is True
    assert sum(1 for call in stash_calls if "Viscera" in call) == 1


def test_render_single_price_markdown_shows_category_fallback_note(_patch_ninja: None) -> None:
    from pob_cli.cli_data_render import _render_single_price_markdown

    fallback_res = query_price("Greater Brute's Arm", category="wombgift")
    md_fb = _render_single_price_markdown(fallback_res, "Greater Brute's Arm")
    assert "> [NOTE] Category fallback: `wombgift` -> `viscera` (0 matches in `wombgift`)." in md_fb

    direct_res = query_price("Greater Brute's Arm", category="all")
    md_direct = _render_single_price_markdown(direct_res, "Greater Brute's Arm")
    assert "Category fallback:" not in md_direct


def test_cmd_price_multi_renders_category_fallback_note(_patch_ninja: None) -> None:
    from click.testing import CliRunner

    from pob_cli.cli_data import cmd_price

    runner = CliRunner()
    res = runner.invoke(cmd_price, ["Greater Brute's Arm", "Grafted Heart", "--category", "wombgift"])
    assert res.exit_code == 0, res.output
    assert "> [NOTE] Category fallback: `wombgift` -> `viscera` (0 matches in `wombgift`)." in res.output
    assert "Grafted Heart" in res.output
