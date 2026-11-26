"""Offline checks: `help --brief` filtering, the `--slot` build-slot alias guard, `fmt_num` below 1, diagnose resists."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pob_cli.cli import main as cli
from pob_cli.cli_common import _looks_like_equipment_slot
from pob_cli.formatters.diagnose_md import _fmt_diagnose_baseline
from pob_cli.formatters.primitives import fmt_delta, fmt_num


def _brief_line(output: str, name: str) -> str:
    return next(line for line in output.splitlines() if line.strip().startswith(name + " "))


def test_help_brief_keeps_subcommand_version_and_key_options() -> None:
    res = CliRunner().invoke(cli, ["help", "--brief"])
    assert res.exit_code == 0, res.output
    assert "--version" in _brief_line(res.output, "self-update")
    diag = _brief_line(res.output, "diagnose")
    for opt in ("--char", "--realm", "--json"):
        assert opt in diag
    data = _brief_line(res.output, "data")
    assert "--limit" in data and "--quality-breakpoints" in data
    assert "--target/--slot" not in res.output
    # The deprecated alias is not advertised as a separate token on build-slot commands.
    assert "--slot" not in _brief_line(res.output, "summary")


@pytest.mark.parametrize("value", ["gloves", "Gloves", "ring1", "Ring 1", "weapon2", "#1", "头盔", "頭盔", "flask3"])
def test_equipment_slot_names_are_detected(value: str) -> None:
    assert _looks_like_equipment_slot(value)


@pytest.mark.parametrize("value", ["main", "ref", "main_ref", "a", "build-2", "witch.v1"])
def test_build_slot_slugs_are_not_equipment(value: str) -> None:
    assert not _looks_like_equipment_slot(value)


def test_slot_alias_refuses_equipment_slot_with_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("pob_cli.cli._daemon_call", lambda action, *a, **k: calls.append(action) or {})
    res = CliRunner().invoke(cli, ["skill", "set", "--slot", "gloves", "--group", "main", "--level", "21"])
    assert res.exit_code == 1
    assert "selects a build slot" in res.output and "--target" in res.output
    assert calls == []


def test_slot_alias_still_selects_a_build_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake(action: str, params: dict | None = None, **_: object) -> dict:
        seen.append(dict(params or {}))
        return {}

    monkeypatch.setattr("pob_cli.cli._daemon_call", fake)
    CliRunner().invoke(cli, ["summary", "--slot", "ref", "--json"])
    assert seen and seen[0].get("target") == "ref"


def test_slot_alias_overrides_an_invalid_pob_slot_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr("pob_cli.cli._daemon_call", lambda action, params=None, **_: seen.append(dict(params or {})) or {})
    monkeypatch.setenv("POB_SLOT", "bad/slot")
    for flag in ("--slot", "--target"):
        seen.clear()
        res = CliRunner().invoke(cli, ["summary", flag, "ref", "--json"])
        assert seen and seen[0].get("target") == "ref", (flag, res.output)
    # With neither flag the bad default is still refused.
    res = CliRunner().invoke(cli, ["summary", "--json"])
    assert res.exit_code == 2 and "Invalid slot slug" in res.output


def test_slot_alias_and_target_conflict_exits_1() -> None:
    res = CliRunner().invoke(cli, ["summary", "--target", "a", "--slot", "b"])
    assert res.exit_code == 1
    assert "pass only --target" in res.output


def test_fmt_num_keeps_precision_below_one() -> None:
    assert fmt_num(0.033) == "0.033"
    assert fmt_num(-0.033) == "-0.033"
    assert fmt_num(0.5) == "0.5"
    assert fmt_num(0.12345) == "0.123"
    assert fmt_num(0.25, decimals=2) == "0.25"
    assert fmt_num(0.5, decimals=3) == "0.500"
    assert fmt_num(1e-12) == "0.0"
    # Unchanged at or above 1
    assert fmt_num(1.25) == "1.2"
    assert fmt_num(12.34, decimals=2) == "12.34"
    assert fmt_num(12345.6) == "12,346"
    assert fmt_delta(0.03, 0.033) == "+0.003 (+10.0%)"


def test_diagnose_baseline_labels_capped_resists_overcap_and_mana_unit() -> None:
    res = {
        "original": {},
        "dewateredPinnacle": {},
        "defence": {
            "Life": 5000,
            "FireResist": 75,
            "FireResistOverCap": 32,
            "ColdResist": 75,
            "ColdResistOverCap": 10,
            "LightningResist": 75,
            "LightningResistOverCap": 0,
            "ChaosResist": -20,
            "ChaosResistOverCap": 0,
        },
        "offence": {"ManaCost": 18},
    }
    text = "\n".join(_fmt_diagnose_baseline(res, compact=False))
    assert "Res F/C/L/Ch (capped) `75/75/75/-20%`" in text
    assert "(overcap `+32/+10/+0/+0%`)" in text
    assert "ManaCost `18/use`" in text
    res["defence"] = {k: v for k, v in res["defence"].items() if "OverCap" not in k}
    assert "overcap" not in "\n".join(_fmt_diagnose_baseline(res, compact=False))
