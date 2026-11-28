# SPDX-License-Identifier: GPL-3.0-only
"""Domain tests for `--section` and `--fields` projection across PoE2 core read commands."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from pob2_cli import cli, cli_inspect, cli_item
from pob2_cli.errors import DaemonRequestError
from pob2_cli.formatters.projection import project_payload, render_projected_markdown

_SAMPLE_SUMMARY: dict[str, Any] = {
    "ok": True,
    "game": "poe2",
    "character": {"name": "Invoker", "class": "Monk", "ascendancy": "Invoker", "level": 92},
    "offense": {"totalDps": 150000.0, "combinedDps": 180000.0, "critChance": 48.5},
    "defense": {"life": 2400.0, "energyShield": 4100.0, "totalEhp": 62000.0},
    "spirit": {"total": 160.0, "reserved": 120.0, "unreserved": 40.0},
    "findings": [
        {"category": "spirit_ok", "severity": "info"},
        {"category": "armour_dropoff", "severity": "warning"},
    ],
}


def test_project_payload_sections_and_nested_fields() -> None:
    sec_only = project_payload(_SAMPLE_SUMMARY, sections_raw="offense,defense")
    assert set(sec_only.keys()) == {"ok", "game", "offense", "defense"}
    assert sec_only["offense"]["totalDps"] == 150000.0

    flds = project_payload(_SAMPLE_SUMMARY, fields_raw="offense.totalDps,defense.life,spirit")
    assert set(flds.keys()) == {"ok", "game", "offense", "defense", "spirit"}
    assert flds["offense"] == {"totalDps": 150000.0}
    assert flds["defense"] == {"life": 2400.0}
    assert flds["spirit"]["unreserved"] == 40.0

    list_proj = project_payload(_SAMPLE_SUMMARY, fields_raw="findings.category")
    assert list_proj["findings"] == {"category": ["spirit_ok", "armour_dropoff"]}

    md = render_projected_markdown(sec_only, sections_raw="offense,defense")
    assert "offense" in md and "defense" in md and "150000.0" in md


def test_project_payload_rejects_invalid_section_or_field() -> None:
    with pytest.raises(DaemonRequestError) as exc_sec:
        project_payload(_SAMPLE_SUMMARY, sections_raw="nonexistent")
    assert "POE2-INVALID-SECTION" in str(exc_sec.value)
    assert "offense" in str(exc_sec.value)

    with pytest.raises(DaemonRequestError) as exc_fld:
        project_payload(_SAMPLE_SUMMARY, fields_raw="offense.doesNotExist")
    assert "POE2-INVALID-FIELD" in str(exc_fld.value)
    assert "totalDps" in str(exc_fld.value)

    with pytest.raises(DaemonRequestError) as exc_top:
        project_payload(_SAMPLE_SUMMARY, fields_raw="bogusRoot")
    assert "POE2-INVALID-FIELD" in str(exc_top.value)


@pytest.mark.parametrize(
    ("argv", "mock_payload", "section_arg", "field_arg", "expected_top"),
    [
        (["summary"], _SAMPLE_SUMMARY, "offense,spirit", "offense.totalDps,spirit.total", {"offense", "spirit"}),
        (
            ["audit"],
            {"ok": True, "game": "poe2", "dehydration": {"dpsInflationRatio": 1.1}, "findings": [], "resistances": {"allElementalCapped": True}},
            "resistances",
            "resistances.allElementalCapped",
            {"resistances"},
        ),
        (
            ["diagnose"],
            {"ok": True, "game": "poe2", "summary": _SAMPLE_SUMMARY, "audit": {"findings": []}, "notes": {"text": "hi"}},
            "summary,notes",
            "summary.offense.totalDps,notes.text",
            {"summary", "notes"},
        ),
        (
            ["compare", "--ref-target", "ref"],
            {"ok": True, "game": "poe2", "delta": {"dpsPct": 12.5}, "dimensions": [{"name": "Items", "deltaDps": 100.0}]},
            "delta",
            "delta.dpsPct",
            {"delta"},
        ),
        (
            ["items"],
            {"ok": True, "game": "poe2", "items": [{"id": 1, "name": "Wand"}], "slots": [{"name": "Weapon 1"}]},
            "items",
            "items.name",
            {"items"},
        ),
        (
            ["skills"],
            {"ok": True, "game": "poe2", "mainSocketGroup": 1, "groups": [{"label": "Spark"}]},
            "groups",
            "groups.label",
            {"groups"},
        ),
        (
            ["tree"],
            {"ok": True, "game": "poe2", "allocatedNodes": [1, 2], "classId": 10},
            "classId",
            "classId",
            {"classId"},
        ),
        (
            ["config"],
            {"ok": True, "game": "poe2", "sections": [{"name": "Combat"}], "customMods": ""},
            "sections",
            "sections.name",
            {"sections"},
        ),
        (
            ["item", "db", "--slot", "Amulet"],
            {"ok": True, "game": "poe2", "slot": "Amulet", "results": [{"name": "Astramentis", "deltaDps": 500}]},
            "results",
            "results.name",
            {"results"},
        ),
    ],
)
def test_core_read_commands_support_section_and_fields(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    mock_payload: dict[str, Any],
    section_arg: str,
    field_arg: str,
    expected_top: set[str],
) -> None:
    monkeypatch.setattr(cli_inspect, "daemon_call_cli", lambda _a, _p=None, **_kw: dict(mock_payload))
    monkeypatch.setattr(cli_item, "daemon_call_cli", lambda _a, _p=None, **_kw: dict(mock_payload))
    runner = CliRunner()

    r_json = runner.invoke(cli.main, [*argv, "--section", section_arg, "--fields", field_arg, "--json"])
    assert r_json.exit_code == 0, r_json.output
    assert r_json.stderr == ""
    data = json.loads(r_json.stdout)
    assert data["ok"] is True and data["game"] == "poe2"
    assert set(data.keys()) - {"ok", "game"} == expected_top

    r_human = runner.invoke(cli.main, [*argv, "--section", section_arg])
    assert r_human.exit_code == 0, r_human.output
    assert "Projected View" in r_human.stdout

    r_bad = runner.invoke(cli.main, [*argv, "--section", "no_such_section", "--json"])
    assert r_bad.exit_code == 1
    assert r_bad.stderr == ""
    err = json.loads(r_bad.stdout)
    assert err["ok"] is False and err["errorKind"] == "invalid_section"


def test_loop_guard_json_byte_determinism_and_markdown_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob2_cli.daemon.read_cache import LOOP_GUARD_WARNING, SlotReadCache

    cache = SlotReadCache(max_entries=16)

    def _cached_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = dict(params or {})
        hit = cache.lookup("main", rev=1, lang="zh-CN", action=action, params=p)
        if hit is not None:
            return hit
        payload = dict(_SAMPLE_SUMMARY) if action == "summary" else {"ok": True, "game": "poe2", "items": [{"id": 1, "name": "Wand"}], "slots": []}
        cache.store("main", rev=1, lang="zh-CN", action=action, params=p, payload=payload)
        return dict(payload)

    monkeypatch.setattr(cli_inspect, "daemon_call_cli", _cached_daemon)
    runner = CliRunner()

    # 3 consecutive summary --json calls must be 100% byte-identical with zero stderr
    r1 = runner.invoke(cli.main, ["summary", "--json"])
    r2 = runner.invoke(cli.main, ["summary", "--json"])
    r3 = runner.invoke(cli.main, ["summary", "--json"])
    assert r1.exit_code == r2.exit_code == r3.exit_code == 0
    assert r1.stderr == r2.stderr == r3.stderr == ""
    assert r1.stdout == r2.stdout == r3.stdout
    parsed = json.loads(r3.stdout)
    assert "cached" not in parsed and "loopGuardWarning" not in parsed

    # 3 consecutive items --json calls must also be 100% byte-identical
    i1 = runner.invoke(cli.main, ["items", "--json"])
    i2 = runner.invoke(cli.main, ["items", "--json"])
    i3 = runner.invoke(cli.main, ["items", "--json"])
    assert i1.exit_code == i2.exit_code == i3.exit_code == 0
    assert i1.stdout == i2.stdout == i3.stdout

    # Human Markdown mode still surfaces loop-guard notice on repeated calls
    r_md = runner.invoke(cli.main, ["summary"])
    assert r_md.exit_code == 0
    assert LOOP_GUARD_WARNING in r_md.stdout


def test_item_inspect_base_only_projection_fields() -> None:
    runner = CliRunner()
    r = runner.invoke(
        cli.main,
        [
            "item",
            "inspect",
            "--base",
            "Desolate Crossbow",
            "--json",
            "--fields",
            "baseType,quality,corrupted,sanctified,implicits,legalityIssues,warnings",
        ],
    )
    assert r.exit_code == 0, r.output
    assert r.stderr == ""
    data = json.loads(r.stdout)
    assert data["ok"] is True and data["game"] == "poe2"
    assert data["baseType"] == "Desolate Crossbow"
    assert data["quality"] == 0
    assert data["corrupted"] is False
    assert data["sanctified"] is False
    assert data["implicits"] == []
    assert data["legalityIssues"] == []
    assert data["warnings"] == []
