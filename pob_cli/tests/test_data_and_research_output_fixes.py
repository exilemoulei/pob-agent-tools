"""Regressions for `pob data`, `pob wiki` and `pob diagnose --compare --section` output found after v0.10.1."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli import cli, cli_inspect, game_data
from pob_cli.external_data.meta_wiki import _extract_wiki_sections_and_summary
from pob_cli.formatters.gamedata_md import _fmt_game_data_mod
from pob_cli.game_data.mod_guidance import _attach_mod_drop_only_guidance, _build_trade_stat_arg
from pob_cli.trade_query.planner import parse_stat_argument

REAL_POB_DIR = find_engine_data()
needs_engine_data = pytest.mark.skipif(REAL_POB_DIR is None, reason="Real PoE1 engine data not available")


@pytest.mark.parametrize(
    ("stat_line", "text", "minimum"),
    [
        # A roll below 1 needs its own minimum: `--stat` asks for at least 1 otherwise.
        ("(0.3-0.5)% of Chaos Damage Leeched as Life", "#% of Chaos Damage Leeched as Life", 0.3),
        ("0.5% of Chaos Damage Leeched as Life", "#% of Chaos Damage Leeched as Life", 0.5),
        ("Regenerate (0.6-1)% of Life per second", "Regenerate #% of Life per second", 0.6),
        ("+(60-69) to maximum Life", "+# to maximum Life", 1.0),
        ("35% increased Movement Speed", "#% increased Movement Speed", 35.0),
        ("+1 to Level of all Spell Skill Gems", "+1 to Level of all Spell Skill Gems", 1.0),
    ],
)
def test_trade_ground_truth_hint_finds_rolls_below_one(stat_line: str, text: str, minimum: float) -> None:
    stat_arg = _build_trade_stat_arg("", [{"stats": [stat_line]}], [])
    request = parse_stat_argument(stat_arg)
    assert (request.text, request.min_value) == (text, minimum)


@needs_engine_data
def test_quality_scan_limit_zero_returns_every_match() -> None:
    capped = game_data.query_gem_quality_breakpoints(active_only=True, limit=10, pob_dir=REAL_POB_DIR)
    every = game_data.query_gem_quality_breakpoints(active_only=True, limit=0, pob_dir=REAL_POB_DIR)
    assert capped["count"] == 10 < capped["totalMatched"]
    assert every["count"] == every["totalMatched"] == capped["totalMatched"]
    assert len({r["name"] for r in every["results"]}) == every["count"]


@pytest.mark.parametrize("argv", [["data", "gem", "Fireball", "--limit", "-1"], ["price", "Mageblood", "--limit", "0"]])
def test_out_of_range_limit_is_a_usage_error(argv: list[str]) -> None:
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 2
    assert "--limit" in result.output


def _wiki_page(sections: int, body_chars: int) -> str:
    body = "x" * body_chars
    return "<p>Lead paragraph.</p>" + "".join(
        f"<h2>Mechanics part {i}</h2><p>{body}</p>" for i in range(sections)
    )


def test_wiki_mechanics_sections_stay_within_max_chars() -> None:
    page = _wiki_page(sections=8, body_chars=5000)

    lead = len("Lead paragraph.")
    summary, sections = _extract_wiki_sections_and_summary(page, max_len=2000)
    assert len(summary) <= 2000
    # `pob wiki` prints the lead paragraph and then every section in full.
    assert lead + sum(len(s["text"]) for s in sections) == 2000
    assert sections[0]["heading"] == "Mechanics part 0"

    _, default_sections = _extract_wiki_sections_and_summary(page)
    assert lead + sum(len(s["text"]) for s in default_sections) == 12000

    long_lead = "<p>" + "y" * 3000 + "</p>" + page
    lead_summary, lead_sections = _extract_wiki_sections_and_summary(long_lead, max_len=1000)
    assert len(lead_summary) <= 1000
    assert len(lead_summary.split("\n\n## ", 1)[0]) + sum(len(s["text"]) for s in lead_sections) <= 1000

    _, unlimited = _extract_wiki_sections_and_summary(page, max_len=0)
    assert [len(s["text"]) for s in unlimited] == [5000] * 8


def _diagnosis(skill: str, dps: int) -> dict[str, Any]:
    return {
        "env": {"game": "poe1", "patch": "3.30", "pobVersion": "2.69.0", "dataVersion": "3.30", "slot": "main", "rev": 1},
        "buildInfo": {"level": 95, "className": "Marauder", "ascendClassName": "Chieftain", "points": {"used": 118, "usedMax": 118}},
        "mainGroup": {"index": 1, "displayLabel": skill, "slot": "Body Armour", "gems": []},
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": dps, "totalEhp": 90_000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": dps // 2, "totalEhp": 80_000},
        "healthGaps": [{"severity": "HIGH", "category": "Resistance", "message": f"{skill} build: Chaos Resist uncapped"}],
        "equippedItems": [],
        "skillGroups": [],
        "factCards": {"gems": [], "uniques": []},
        "advisories": [],
    }


@pytest.mark.parametrize("section", ["summary", "gaps"])
def test_compare_section_keeps_each_build_under_its_own_heading(section: str) -> None:
    res = {
        "mode": "compare",
        "target": "main",
        "refTarget": "ref",
        "comparison": {},
        "buildA": _diagnosis("Righteous Fire", 1_111_111),
        "buildB": _diagnosis("Cyclone", 7_777_777),
        "updateWarning": "Engine update failed; using the previous engine.",
    }
    md = cli_inspect._slice_diagnose_compare_markdown(res, section)
    assert md.startswith("[Env:") and "> [WARN] Engine update failed" in md.split("## Build A Diagnosis")[0]

    head_a, head_b = md.index("## Build A Diagnosis (`main`)"), md.index("## Build B Diagnosis (`ref`)")
    assert head_a < head_b
    part_a, part_b = md[head_a:head_b], md[head_b:]
    marker_a, marker_b = ("1,111,111", "7,777,777") if section == "summary" else ("Righteous Fire build", "Cyclone build")
    assert marker_a in part_a and marker_b not in part_a
    assert marker_b in part_b and marker_a not in part_b
    wanted = "#### 1." if section == "summary" else "#### 2."
    assert wanted in part_a and wanted in part_b
    assert ("Dual-Build 3-Baseline Comparison" in md) is (section == "summary")


def test_only_dropped_explicit_mods_are_called_drop_only() -> None:
    eldritch = {"id": "E1", "source": "Eldritch", "stats": ["12% increased Attack Speed"], "weights": {}}
    delve = {"id": "D1", "source": "Delve", "drop_origin": "Delve", "stats": ["+1 to maximum number of Skeletons"], "weights": {}}

    out: dict[str, Any] = {}
    _attach_mod_drop_only_guidance(out, [eldritch], [], "attack speed", "gloves")
    assert "dropOnlyNote" not in out and "dropOnlyReason" not in out

    out = {}
    _attach_mod_drop_only_guidance(out, [delve], [], "skeletons", "body_armour")
    assert out["dropOnlyReason"] == "hit" and "pob trade search" in out["dropOnlyNote"]

    lines = "\n".join(_fmt_game_data_mod([eldritch, delve])).splitlines()
    row_e = next(ln for ln in lines if "12% increased Attack Speed" in ln)
    row_d = next(ln for ln in lines if "Skeletons" in ln)
    assert "drop-only" not in row_e and "Eldritch (no roll weight)" in row_e
    assert "drop-only" in row_d
