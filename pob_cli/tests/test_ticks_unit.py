"""Breakpoint-boundary unit tests for `pob_cli.ticks` (0.033s server-tick cooldown math, daemon-free)."""

from __future__ import annotations

import math

import pytest

from pob_cli.ticks import POE1_SERVER_TICK_SEC, build_server_tick_table, compute_server_tick_cooldown


def test_server_tick_constant_is_33ms_not_one_thirtieth() -> None:
    assert POE1_SERVER_TICK_SEC == 0.033
    assert POE1_SERVER_TICK_SEC != pytest.approx(1.0 / 30.0)


def test_exact_tick_multiple_stays_on_single_tick() -> None:
    out = compute_server_tick_cooldown(0.033, 0.0)
    assert out["ticks"] == 1
    assert out["effectiveCdSec"] == 0.033
    assert out["effectiveCdMs"] == 33
    assert out["maxTriggersPerSec"] == pytest.approx(30.3)
    assert out["nextBreakpoint"] is None


def test_just_over_tick_boundary_rounds_up_to_two_ticks() -> None:
    out = compute_server_tick_cooldown(0.034, 0.0)
    assert out["ticks"] == 2
    assert out["effectiveCdMs"] == 66
    assert out["effectiveCdSec"] == 0.066


def test_one_second_base_cooldown_quantizes_to_31_ticks() -> None:
    out = compute_server_tick_cooldown(1.0, 0.0)
    assert out["rawCdSec"] == 1.0
    assert out["ticks"] == 31
    assert out["effectiveCdSec"] == 1.023
    assert out["effectiveCdMs"] == 1023
    assert out["maxTriggersPerSec"] == pytest.approx(0.98)
    assert out["effectiveCapPerSec"] == pytest.approx(0.98)


def test_two_percent_cdr_drops_one_tick_and_reports_next_breakpoint() -> None:
    out = compute_server_tick_cooldown(1.0, 2.0)
    assert out["ticks"] == 30
    assert out["effectiveCdMs"] == 990
    assert out["maxTriggersPerSec"] == pytest.approx(1.01)
    nxt = out["nextBreakpoint"]
    assert nxt is not None
    assert nxt["targetTicks"] == 29
    assert nxt["requiredCdrPct"] == 5
    assert nxt["cdrNeededPct"] == 3


def test_next_breakpoint_absent_at_single_tick_floor() -> None:
    out = compute_server_tick_cooldown(0.02, 500.0)
    assert out["ticks"] == 1
    assert out["nextBreakpoint"] is None


def test_degenerate_inputs_are_clamped() -> None:
    out = compute_server_tick_cooldown(-5.0, -10.0)
    assert out["baseCdSec"] == 0.001
    assert out["globalCdrPct"] == 0.0
    assert out["ticks"] == 1
    assert out["effectiveCdMs"] == 33


def test_triggered_spells_scales_cap_only() -> None:
    one = compute_server_tick_cooldown(1.0, 0.0, triggered_spells=1)
    three = compute_server_tick_cooldown(1.0, 0.0, triggered_spells=3)
    assert three["ticks"] == one["ticks"]
    assert three["triggeredSpells"] == 3
    assert three["effectiveCapPerSec"] == pytest.approx(one["effectiveCapPerSec"] * 3)


def test_triggered_spells_zero_falls_back_to_one() -> None:
    out = compute_server_tick_cooldown(1.0, 0.0, triggered_spells=0)
    assert out["triggeredSpells"] == 1


def test_aps_overspeed_flag_and_note() -> None:
    fast = compute_server_tick_cooldown(1.0, 0.0, aps=1.5)
    assert fast["overspeed"] is True
    assert fast["apsRatioToCap"] == pytest.approx(1.531)
    assert "overspeedNote" in fast

    safe = compute_server_tick_cooldown(1.0, 0.0, aps=0.9)
    assert safe["overspeed"] is False
    assert safe["apsRatioToCap"] == pytest.approx(0.918)
    assert "overspeedNote" not in safe


def test_aps_none_or_unusable_is_omitted() -> None:
    for aps in (None, 0.0, float("nan"), float("inf"), -1.0):
        out = compute_server_tick_cooldown(1.0, 0.0, aps=aps)
        assert "aps" not in out
        assert "overspeed" not in out


def test_table_rows_cover_ticks_descending_and_dedupe_cdr() -> None:
    table = build_server_tick_table(1.0)
    assert table["serverTickMs"] == 33
    assert table["current"] is None
    rows = table["breakpoints"]
    assert len(rows) == 16
    ticks = [row["ticks"] for row in rows]
    assert ticks == sorted(ticks, reverse=True)
    assert ticks[0] == 16
    assert ticks[-1] == 1
    cdrs = [row["minCdrPct"] for row in rows]
    assert cdrs == sorted(cdrs)
    assert len(set(cdrs)) == len(cdrs)
    assert rows[0]["minCdrPct"] == 90
    assert rows[-1]["minCdrPct"] == 2931
    assert rows[0]["effectiveCdMs"] == 16 * 33
    assert rows[-1]["maxTriggersPerSec"] == pytest.approx(30.3)


def test_table_max_ticks_clamps_rows_but_not_current() -> None:
    table = build_server_tick_table(1.0, cdr_pct=0.0, max_ticks=3)
    rows = table["breakpoints"]
    assert [row["ticks"] for row in rows] == [3, 2, 1]
    assert rows[0]["minCdrPct"] == 911
    assert table["current"] is not None
    assert table["current"]["ticks"] == 31


def test_table_short_base_cooldown_yields_single_zero_cdr_row() -> None:
    table = build_server_tick_table(0.02)
    rows = table["breakpoints"]
    assert len(rows) == 1
    assert rows[0]["ticks"] == 1
    assert rows[0]["minCdrPct"] == 0


def test_table_current_falls_back_to_zero_cdr_when_only_aps_given() -> None:
    table = build_server_tick_table(1.0, aps=0.9)
    current = table["current"]
    assert current is not None
    assert current["globalCdrPct"] == 0.0
    assert current["aps"] == pytest.approx(0.9)


def test_table_triggered_spells_propagates_to_rows_and_current() -> None:
    table = build_server_tick_table(1.0, cdr_pct=2.0, triggered_spells=4)
    assert table["triggeredSpells"] == 4
    assert table["current"] is not None
    assert table["current"]["effectiveCapPerSec"] == pytest.approx(1.01 * 4)
    assert table["breakpoints"][0]["maxApsCap"] == pytest.approx(table["breakpoints"][0]["maxTriggersPerSec"] * 4)


def test_table_cwc_note_states_fixed_interval() -> None:
    note = build_server_tick_table(1.0)["cwcNote"]
    assert "FIXED" in note
    assert "320ms" in note
    assert math.isclose(POE1_SERVER_TICK_SEC * 1000, 33.0)
