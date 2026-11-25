"""Tests for P0-1 (PoE1 false friends & 3.23-3.25 legacy patch traps), P0-2 (CLI self-healing,
slot/flag aliases, player slang, zero-empty-result suggestions, `pob ground`), P1-1 (`pob diagnose`),
and P1-2 (`skills/pob/SKILL.md` slim router skill).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli import analyzer, cli, game_data, trade_query
from pob_cli.errors import CrossGameHallucinationError, HallucinationWarning, LegacyPatchTrapError
from pob_cli.formatters import format_diagnose_markdown, format_ground_markdown
from pob_cli.guardrails import (
    GuardrailScope,
    check_input,
    normalize_equipment_slot,
    warning_context,
)

REAL_POB_DIR = find_engine_data()
HAS_REAL_POB = REAL_POB_DIR is not None

REPO_ROOT = Path(__file__).resolve().parents[2]


def _extract_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = output.find("{", idx)
        if start == -1:
            break
        try:
            obj, _ = decoder.raw_decode(output[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = start + 1
    raise AssertionError(f"Expected JSON in output, got:\n{output}")


# ---------------------------------------------------------------------------
# P0-1: Same-Name False Friends & Legacy Patch Traps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "scope", "expected_rule"),
    [
        ("support gem", GuardrailScope.DATA_QUERY, "FF_POE1_SOCKETS_DUPLICATE_SUPPORTS"),
        ("重复辅助", GuardrailScope.DATA_QUERY, "FF_POE1_SOCKETS_DUPLICATE_SUPPORTS"),
        ("damage conversion", GuardrailScope.CALC_QUERY, "FF_POE1_DAMAGE_CONVERSION_CHAIN"),
        ("伤害转化", GuardrailScope.CALC_QUERY, "FF_POE1_DAMAGE_CONVERSION_CHAIN"),
        ("Eldritch Battery", GuardrailScope.CALC_QUERY, "FF_POE1_ELDRITCH_BATTERY"),
        ("异能魔力", GuardrailScope.DATA_QUERY, "FF_POE1_ELDRITCH_BATTERY"),
        ("Chaos Inoculation", GuardrailScope.CALC_QUERY, "FF_POE1_CHAOS_INOCULATION"),
        ("异灵之体", GuardrailScope.DATA_QUERY, "FF_POE1_CHAOS_INOCULATION"),
        ("混沌接种", GuardrailScope.DATA_QUERY, "FF_POE1_CHAOS_INOCULATION"),
        ("Mind Over Matter", GuardrailScope.CALC_QUERY, "FF_POE1_MIND_OVER_MATTER"),
        ("心灵升华", GuardrailScope.DATA_QUERY, "FF_POE1_MIND_OVER_MATTER"),
        ("心灵灭界", GuardrailScope.DATA_QUERY, "FF_POE1_MIND_OVER_MATTER"),
        ("Acrobatics", GuardrailScope.DATA_QUERY, "FF_POE1_ACROBATICS"),
        ("移形换影", GuardrailScope.CALC_QUERY, "FF_POE1_ACROBATICS"),
        ("杂耍", GuardrailScope.DATA_QUERY, "FF_POE1_ACROBATICS"),
        ("Armour", GuardrailScope.CALC_QUERY, "FF_POE1_ARMOUR_MITIGATION"),
        ("护甲", GuardrailScope.STAT_QUERY, "FF_POE1_ARMOUR_MITIGATION"),
        ("Mageblood", GuardrailScope.DATA_QUERY, "FF_POE1_MAGEBLOOD"),
        ("法血", GuardrailScope.DATA_QUERY, "FF_POE1_MAGEBLOOD"),
        ("Frenzy Charge", GuardrailScope.CONFIG_VAR, "FF_POE1_CHARGES_PASSIVE_BONUS"),
        ("狂怒球", GuardrailScope.CALC_QUERY, "FF_POE1_CHARGES_PASSIVE_BONUS"),
    ],
)
def test_poe1_false_friends_warn_without_blocking(
    query: str,
    scope: GuardrailScope,
    expected_rule: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with warning_context() as warns:
        returned = check_input(query, scope=scope, expected_game="poe1")
    assert any(w.rule_id == expected_rule for w in returned)
    assert any(w.rule_id == expected_rule for w in warns)
    assert all(isinstance(w, HallucinationWarning) for w in returned)
    err = capsys.readouterr().err
    assert "[POE1-TRAP-WARN]" in err


@pytest.mark.parametrize(
    ("query", "scope", "expected_rule"),
    [
        ("helmet enchant", GuardrailScope.DATA_QUERY, "PATCH_323_LAB_ENCHANTS_REMOVED"),
        ("迷宫附魔", GuardrailScope.DATA_QUERY, "PATCH_323_LAB_ENCHANTS_REMOVED"),
        ("Anomalous Spark", GuardrailScope.GEM_NAME, "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Divergent Haste", GuardrailScope.GEM_NAME, "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Phantasmal Cremation", GuardrailScope.GEM_NAME, "PATCH_323_ALT_QUALITY_REMOVED"),
        ("异常品质", GuardrailScope.DATA_QUERY, "PATCH_323_ALT_QUALITY_REMOVED"),
        ("分歧电弧", GuardrailScope.GEM_NAME, "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Vortex", GuardrailScope.GEM_NAME, "PATCH_323_VORTEX_CAST_TIME"),
        ("漩涡", GuardrailScope.GEM_NAME, "PATCH_323_VORTEX_CAST_TIME"),
        ("Call to Arms", GuardrailScope.GEM_NAME, "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS"),
        ("左键瞬发", GuardrailScope.DATA_QUERY, "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS"),
        ("Awakened Sextant", GuardrailScope.DATA_QUERY, "PATCH_324_SEXTANTS_REMOVED"),
        ("六分仪", GuardrailScope.DATA_QUERY, "PATCH_324_SEXTANTS_REMOVED"),
        ("Divine Blessing", GuardrailScope.GEM_NAME, "PATCH_325_DIVINE_BLESSING_REMOVED"),
        ("神圣祝福", GuardrailScope.GEM_NAME, "PATCH_325_DIVINE_BLESSING_REMOVED"),
        ("Ancestral Protector", GuardrailScope.GEM_NAME, "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("先祖卫士", GuardrailScope.GEM_NAME, "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("Ancestral Warchief", GuardrailScope.GEM_NAME, "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("先祖战士长", GuardrailScope.GEM_NAME, "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("Hatred", GuardrailScope.GEM_NAME, "PATCH_325_HATRED_REWORK"),
        ("憎恨", GuardrailScope.GEM_NAME, "PATCH_325_HATRED_REWORK"),
        ("Endurance Charge", GuardrailScope.CALC_QUERY, "PATCH_325_ENDURANCE_CHARGE_REWORK"),
        ("耐力球", GuardrailScope.CONFIG_VAR, "PATCH_325_ENDURANCE_CHARGE_REWORK"),
    ],
)
def test_legacy_patch_traps_emit_patch_warn_on_queries(
    query: str,
    scope: GuardrailScope,
    expected_rule: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with warning_context() as warns:
        returned = check_input(query, scope=scope, expected_game="poe1", block_legacy_gems=False)
    assert any(w.rule_id == expected_rule and w.category == "legacy_patch" for w in returned)
    assert any(w.rule_id == expected_rule for w in warns)
    err = capsys.readouterr().err
    assert "[POE1-PATCH-WARN]" in err


@pytest.mark.parametrize(
    ("gem", "expected_rule"),
    [
        ("Divine Blessing", "PATCH_325_DIVINE_BLESSING_REMOVED"),
        ("神圣祝福", "PATCH_325_DIVINE_BLESSING_REMOVED"),
        ("Ancestral Protector", "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("先祖卫士", "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("Ancestral Warchief", "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("先祖战士长", "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
        ("Anomalous Spark", "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Divergent Inspiration", "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Phantasmal Unearth", "PATCH_323_ALT_QUALITY_REMOVED"),
        ("Call to Arms", "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS"),
        ("武装召唤", "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS"),
    ],
)
def test_removed_legacy_gems_hard_blocked_in_skill_add_and_set(gem: str, expected_rule: str) -> None:
    runner = CliRunner()
    res_add = runner.invoke(cli.main, ["skill", "add", "--gem", gem, "--json"])
    assert res_add.exit_code == 1
    payload = _extract_json(res_add.output)
    assert payload["errorKind"] == "legacy_patch_trap"
    assert payload["ruleId"] == expected_rule
    assert payload["ok"] is False

    res_set = runner.invoke(cli.main, ["skill", "set", "--gem", "1", "--new-gem", gem, "--json"])
    assert res_set.exit_code == 1
    payload_set = _extract_json(res_set.output)
    assert payload_set["errorKind"] == "legacy_patch_trap"
    assert payload_set["ruleId"] == expected_rule


@pytest.mark.parametrize(
    ("valid_term", "scope"),
    [
        ("Eternal Blessing", GuardrailScope.GEM_NAME),
        ("永恒祝福", GuardrailScope.GEM_NAME),
        ("Guardian's Blessing", GuardrailScope.GEM_NAME),
        ("Ancestral Call", GuardrailScope.GEM_NAME),
        ("先祖召唤", GuardrailScope.GEM_NAME),
        ("Ancestral Cry", GuardrailScope.GEM_NAME),
        ("先祖战吼", GuardrailScope.GEM_NAME),
        ("Ancestral Bond", GuardrailScope.DATA_QUERY),
        ("Deadly Ailments", GuardrailScope.GEM_NAME),
        ("致命异常状态", GuardrailScope.GEM_NAME),
        ("Unbound Ailments", GuardrailScope.GEM_NAME),
        ("异常爆发", GuardrailScope.GEM_NAME),
        ("Mirror Arrow", GuardrailScope.GEM_NAME),
        ("魅影射击", GuardrailScope.GEM_NAME),
        ("Blade Vortex", GuardrailScope.GEM_NAME),
        ("飞刃风暴", GuardrailScope.GEM_NAME),
        ("Orb of Storms", GuardrailScope.GEM_NAME),
        ("风暴漩涡", GuardrailScope.GEM_NAME),
        ("Rage Vortex", GuardrailScope.GEM_NAME),
        ("怒火漩涡", GuardrailScope.GEM_NAME),
        ("The Hateful Accuser", GuardrailScope.DATA_QUERY),
        ("憎恨控诉", GuardrailScope.DATA_QUERY),
        ("Body Armour", GuardrailScope.CALC_QUERY),
        ("胸甲", GuardrailScope.STAT_QUERY),
        ("Automation", GuardrailScope.GEM_NAME),
        ("自动化", GuardrailScope.GEM_NAME),
        ("Autoexertion", GuardrailScope.GEM_NAME),
        ("自动增助", GuardrailScope.GEM_NAME),
    ],
)
def test_zero_false_positives_on_valid_330_poe1_entities(valid_term: str, scope: GuardrailScope) -> None:
    with warning_context() as warns:
        returned = check_input(valid_term, scope=scope, expected_game="poe1", block_legacy_gems=True)
    assert returned == []
    assert warns == []


def test_legacy_patch_trap_error_roundtrip() -> None:
    exc = LegacyPatchTrapError(
        trigger="Divine Blessing",
        patch_version="3.25",
        error="Removed in 3.25",
        remediation="Use Eternal Blessing",
        equivalent_command='pob data gem "Eternal Blessing"',
        rule_id="PATCH_325_DIVINE_BLESSING_REMOVED",
    )
    d = exc.to_dict()
    assert d["errorKind"] == "legacy_patch_trap"
    assert d["patchVersion"] == "3.25"
    assert "[POE1-PATCH-BLOCK]" in exc.format_human()
    assert LegacyPatchTrapError.from_dict(d).to_dict() == d


# ---------------------------------------------------------------------------
# P0-2: Slot & Flag Aliases, Click Error Recovery, Slang, Suggestions, Ground
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_slot", "expected"),
    [
        ("ring1", "Ring 1"),
        ("左戒指", "Ring 1"),
        ("ring2", "Ring 2"),
        ("右戒指", "Ring 2"),
        ("chest", "Body Armour"),
        ("body", "Body Armour"),
        ("衣服", "Body Armour"),
        ("胸甲", "Body Armour"),
        ("helm", "Helmet"),
        ("头盔", "Helmet"),
        ("gloves", "Gloves"),
        ("手套", "Gloves"),
        ("boots", "Boots"),
        ("鞋子", "Boots"),
        ("amulet", "Amulet"),
        ("项链", "Amulet"),
        ("belt", "Belt"),
        ("腰带", "Belt"),
        ("weapon1", "Weapon 1"),
        ("主手", "Weapon 1"),
        ("weapon2", "Weapon 2"),
        ("offhand", "Weapon 2"),
        ("副手", "Weapon 2"),
        ("flask1", "Flask 1"),
        ("flask5", "Flask 5"),
        ("#1", "#1"),
    ],
)
def test_normalize_equipment_slot_aliases(raw_slot: str, expected: str) -> None:
    assert normalize_equipment_slot(raw_slot) == expected


def test_trade_query_accepts_cn_and_tolerant_slots() -> None:
    assert trade_query.category_for_slot("衣服") == "armour.chest"
    assert trade_query.category_for_slot("左戒指") == "accessory.ring"
    assert trade_query.category_for_slot("盾牌") == "armour.shield"
    assert trade_query.pob_slot_for_slot("副手") == "Weapon 2"
    assert trade_query.pob_slot_for_slot("offhand") == "Weapon 2"
    assert trade_query.pob_slot_for_slot("头盔") == "Helmet"


def test_poe2_slots_still_hard_blocked_after_aliases() -> None:
    for bad in ("charm1", "focus", "quarterstaff"):
        with pytest.raises(CrossGameHallucinationError):
            check_input(bad, scope=GuardrailScope.SLOT, expected_game="poe1")


def test_cli_option_aliases_and_click_error_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        return {"applied": False, "mode": "WHAT-IF", "before": {}, "after": {}, "diff": {}, "detail": {}}

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    # --oil alias for --notable on item anoint
    r1 = runner.invoke(cli.main, ["item", "anoint", "--oil", "Whispers of Doom", "--json"])
    assert r1.exit_code == 0, r1.output
    assert captured[-1][0] == "item_anoint"
    assert captured[-1][1]["notable"] == "Whispers of Doom"

    # --gem-name alias on skill add / set / rm
    r2 = runner.invoke(cli.main, ["skill", "add", "--gem-name", "Arc", "--json"])
    assert r2.exit_code == 0, r2.output
    assert captured[-1][1]["gem"] == "Arc"

    r3 = runner.invoke(cli.main, ["skill", "set", "--gem-name", "1", "--level", "21", "--json"])
    assert r3.exit_code == 0, r3.output
    assert captured[-1][1]["gem"] == "1"

    r4 = runner.invoke(cli.main, ["skill", "rm", "--gem-name", "Arc", "--json"])
    assert r4.exit_code == 0, r4.output
    assert captured[-1][1]["gem"] == "Arc"

    # Click error recovery: unknown subcommand & typo option preserve exit code 2 + Did you mean
    r_cmd = runner.invoke(cli.main, ["diag"])
    assert r_cmd.exit_code == 2
    assert "Did you mean" in r_cmd.output and "diagnose" in r_cmd.output

    r_opt = runner.invoke(cli.main, ["data", "gem", "Arc", "--limt", "5"])
    assert r_opt.exit_code == 2
    assert "Did you mean" in r_opt.output and "--limit" in r_opt.output


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
@pytest.mark.parametrize(
    ("kind", "slang", "expected_en"),
    [
        ("unique", "法血", "Mageblood"),
        ("unique", "猎首", "Headhunter"),
        ("unique", "尼米斯", "Nimis"),
        ("unique", "原罪", "Original Sin"),
        ("unique", "百属", "Astramentis"),
        ("unique", "全知", "Crystallised Omniscience"),
        ("unique", "星尘", "Ashes of the Stars"),
        ("gem", "大法师", "Archmage"),
        ("gem", "电弧", "Arc"),
        ("gem", "闪打", "Flicker Strike"),
        ("gem", "正火", "Righteous Fire"),
        ("gem", "龙卷", "Tornado Shot"),
        ("translate", "法压", "Spell Suppression"),
        ("translate", "暴伤", "Critical Strike Multiplier"),
        ("translate", "杂耍", "Acrobatics"),
        ("translate", "心灵灭界", "Mind Over Matter"),
        ("translate", "混沌接种", "Chaos Inoculation"),
    ],
)
def test_player_slang_auto_resolution_in_game_data(kind: str, slang: str, expected_en: str) -> None:
    res = game_data.query_local_data(kind, slang, pob_dir=REAL_POB_DIR, limit=5)
    assert res["count"] >= 1, f"Slang {slang!r} ({kind}) returned 0 results"
    if kind == "translate":
        assert any(expected_en.lower() in (r.get("en") or "").lower() for r in res["results"])
    else:
        assert any(expected_en.lower() in (r.get("name") or "").lower() for r in res["results"])


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
def test_zero_empty_result_suggestions_and_hint() -> None:
    res = game_data.query_local_data("unique", "Mageblod", pob_dir=REAL_POB_DIR, limit=5)
    assert res["results"] == []
    assert res["count"] == 0
    assert isinstance(res.get("suggestions"), list) and len(res["suggestions"]) >= 1
    assert res["suggestions"][0]["name"] == "Mageblood"
    assert "Did you mean" in (res.get("hint") or "")

    runner = CliRunner()
    cli_res = runner.invoke(cli.main, ["data", "unique", "Mageblod"])
    assert cli_res.exit_code == 0
    assert "Did You Mean" in cli_res.output
    assert "Mageblood" in cli_res.output


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
def test_ground_text_entities_speed_and_accuracy() -> None:
    prompt = "我玩正火想用神圣祝福开憎恨，再带法血和先祖战士长，异能魔力是不是把护盾转成魔力？还能用精魂吗？"
    # Warm up index once
    first = game_data.ground_text_entities(prompt, pob_dir=REAL_POB_DIR)
    t0 = time.perf_counter()
    res = game_data.ground_text_entities(prompt, pob_dir=REAL_POB_DIR)
    warm_ms = (time.perf_counter() - t0) * 1000.0

    assert first["hasAlerts"] is True
    assert warm_ms < 25.0, f"Warm ground_text_entities took {warm_ms:.2f}ms"
    assert res["env"]["game"] == "poe1"
    assert any(t["ruleId"] == "POE2_SPIRIT_RESERVATION" for t in res["poe2Traps"])
    patch_ids = {p["ruleId"] for p in res["patchTraps"]}
    assert {
        "PATCH_325_DIVINE_BLESSING_REMOVED",
        "PATCH_325_ANCESTRAL_TOTEMS_REMOVED",
        "PATCH_325_HATRED_REWORK",
    }.issubset(patch_ids)
    ff_ids = {f["ruleId"] for f in res["falseFriends"]}
    assert "FF_POE1_ELDRITCH_BATTERY" in ff_ids
    ent_names = {e["name"] for e in res["entities"]}
    assert "Mageblood" in ent_names
    assert "Righteous Fire" in ent_names
    assert "Hatred" in ent_names
    # Removed legacy Divine Blessing must NOT appear as an active gem card
    assert "Divine Blessing Support" not in ent_names
    assert "[Env: PoE1" in res["contextBlock"]
    assert "[POE1-PATCH-WARN]" in res["contextBlock"]

    # CLI JSON & Markdown check
    runner = CliRunner()
    r_json = runner.invoke(cli.main, ["ground", prompt, "--json"])
    assert r_json.exit_code == 0
    parsed = _extract_json(r_json.output)
    assert parsed["hasAlerts"] is True
    assert parsed["gamedataAvailable"] is True
    assert format_ground_markdown(parsed).startswith("[Env: PoE1")


def test_ground_text_entities_without_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pob ground` must degrade cleanly before `pob bootstrap` (no PoE1/Data on disk)."""
    empty_engine = tmp_path / "missing-engine"
    monkeypatch.setenv("POB_ROOT", str(empty_engine))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("POB_LEGACY_DIR", "")

    res = game_data.ground_text_entities(
        "我玩正火想用 Divine Blessing 开 Hatred 再带法血",
        pob_dir=empty_engine,
        runtime_dir=tmp_path / "rt",
    )
    assert res["gamedataAvailable"] is False
    assert res["env"]["pobVersion"] == "unbootstrapped"
    assert res["env"]["dataVersion"] == "none"
    assert res["entities"] == []
    assert res["hasAlerts"] is True
    patch_ids = {p["ruleId"] for p in res["patchTraps"]}
    assert {"PATCH_325_DIVINE_BLESSING_REMOVED", "PATCH_325_HATRED_REWORK"}.issubset(patch_ids)
    slang_canon = {s["canonical"] for s in res["resolvedSlang"]}
    assert {"Righteous Fire", "Mageblood"}.issubset(slang_canon)
    assert "pob bootstrap" in res["contextBlock"]

    runner = CliRunner()
    cli_res = runner.invoke(cli.main, ["ground", "Divine Blessing with Hatred", "--json"])
    assert cli_res.exit_code == 0, cli_res.output
    parsed = _extract_json(cli_res.output)
    assert parsed["gamedataAvailable"] is False
    assert parsed["hasAlerts"] is True
    assert any(p["ruleId"] == "PATCH_325_DIVINE_BLESSING_REMOVED" for p in parsed["patchTraps"])


# ---------------------------------------------------------------------------
# P1-1: `pob diagnose`
# ---------------------------------------------------------------------------


def test_diagnose_cli_and_markdown_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_diag = {
        "env": {
            "game": "poe1",
            "patch": "3.30",
            "pobVersion": "2.68.1",
            "dataVersion": "zh-1.0.0-test",
            "treeVersion": "3_30",
            "slot": "main",
        },
        "buildInfo": {
            "level": 95,
            "className": "Witch",
            "classNameZh": "女巫",
            "ascendClassName": "Elementalist",
            "ascendClassNameZh": "元素使",
            "treeVersion": "3_30",
            "points": {"used": 115, "usedMax": 118, "ascUsed": 8, "ascMax": 8},
        },
        "mainGroup": {
            "index": 1,
            "slot": "Body Armour",
            "displayLabel": "Vortex",
            "gems": [
                {"name": "Vortex", "nameZh": "漩涡", "level": 20, "quality": 20, "enabled": True},
                {"name": "Swift Affliction", "nameZh": "极速苦痛", "level": 20, "quality": 20, "enabled": True},
            ],
        },
        "keystones": ["Eldritch Battery"],
        "original": {
            "primaryDpsKey": "CombinedDPS",
            "primaryDps": 2_500_000,
            "totalEhp": 80_000,
            "physMaxHit": 18_000,
            "eleMaxHit": 42_000,
            "chaosMaxHit": 12_000,
        },
        "dewateredPinnacle": {
            "primaryDpsKey": "CombinedDPS",
            "primaryDps": 1_200_000,
            "totalEhp": 45_000,
            "physMaxHit": 12_000,
            "eleMaxHit": 30_000,
            "chaosMaxHit": 9_000,
        },
        "inflationStripped": {"config": ["useFrenzyCharges=True -> reset"], "flasks": [], "skills": [], "party": [], "kept": [], "keepRejected": []},
        "topWaterSources": [
            {"dimension": "config", "label": "useFrenzyCharges=True", "dpsDelta": 400_000, "dpsPct": 33.3, "totalEhpDelta": 0, "totalEhpPct": 0.0}
        ],
        "healthGaps": [
            {"severity": "HIGH", "category": "Resistance", "message": "Cold Resist is uncapped: 68% (gap: 7%)"},
            {"severity": "LOW", "category": "Points", "message": "Unallocated passive points: 115/118 (3 remaining)"},
        ],
        "factCards": {
            "gems": [
                {"kind": "gem", "name": "Vortex", "zhName": "漩涡", "isSupport": False, "castTime": 0.6, "tags": "Spell, Cold", "lv20Stats": {"spell_minimum_base_cold_damage": 933}}
            ],
            "uniques": [
                {"kind": "unique", "name": "Mageblood", "zhName": "法师之血", "baseType": "Heavy Belt", "explicits": ["Leftmost (2-4) Magic Utility Flasks constantly apply their Flask Effects to you"]}
            ],
            "equippedUniques": [],
        },
        "advisories": [
            {"tag": "[POE1-PATCH-WARN]", "trigger": "vortex", "message": "Since 3.23, Vortex has castTime = 0.6s."}
        ],
        "topNodes": {
            "stat": "CombinedDPS",
            "maxDist": 5,
            "byPerPoint": [{"id": 12345, "name": "Heart of Ice", "nameZh": "冰霜之心", "pathDist": 2, "pathPower": 4.2, "power": 8.4}],
        },
        "topGems": {
            "dpsField": "CombinedDPS",
            "mode": "add_link (slot #3)",
            "candidates": [{"name": "Empower Support", "nameZh": "赋予(辅)", "delta": 180_000, "pct": 15.0}],
        },
        "rankErrors": [],
        "restoreCheck": {"verified": True},
    }

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        calls.append((action, dict(params or {})))
        return fake_diag

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    r_md = runner.invoke(cli.main, ["diagnose", "--top", "5", "--keep", "useFrenzyCharges"])
    assert r_md.exit_code == 0, r_md.output
    assert calls[-1][0] == "diagnose"
    assert calls[-1][1]["keep"] == ["useFrenzyCharges"]
    assert "[Env: PoE1 3.30 (NOT PoE2) | PoB 2.68.1" in r_md.output
    assert "Panel vs True Pinnacle Boss Baseline" in r_md.output
    assert "Hard Defensive & Mechanic Gaps" in r_md.output
    assert "Fact Cards & Patch Advisories" in r_md.output
    assert "Top ROI Upgrades" in r_md.output
    assert "Cast=0.6s" in r_md.output

    r_json = runner.invoke(cli.main, ["diagnose", "--no-rank", "--json"])
    assert r_json.exit_code == 0
    assert calls[-1][1]["no_rank"] is True
    parsed = _extract_json(r_json.output)
    assert parsed["env"]["game"] == "poe1"
    assert len(parsed["healthGaps"]) == 2

    # PoE2 URL passed to diagnose must still be hard-blocked!
    r_poe2 = runner.invoke(cli.main, ["diagnose", "https://poe2db.tw/pob/abc", "--json"])
    assert r_poe2.exit_code == 1
    assert _extract_json(r_poe2.output)["errorKind"] == "cross_game_hallucination"


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
def test_round1_empirical_fixes_legacy_row_stdout_warn_and_dedup() -> None:
    # 1. Legacy Divine Blessing row in `pob data gem` is explicitly tagged legacyOnly + rendered in stdout Markdown
    res_db = game_data.query_local_data("gem", "神圣祝福", pob_dir=REAL_POB_DIR)
    assert res_db["results"]
    assert res_db["results"][0].get("legacyOnly") is True
    assert res_db["results"][0].get("removedInPatch") == "3.25"
    assert "Standard" in (res_db["results"][0].get("legacyNote") or "")

    runner = CliRunner()
    r_md = runner.invoke(cli.main, ["data", "gem", "神圣祝福"])
    assert r_md.exit_code == 0
    assert "[POE1-PATCH-WARN]" in r_md.output
    assert "[LEGACY / REMOVED IN 3.25]" in r_md.output

    # 2. Variadic `pob data` deduplicates top-level warnings by (ruleId, trigger)
    multi = game_data.query_multi(None, "gem", ["Hatred", "Hatred Support"], pob_dir=REAL_POB_DIR)
    warns = multi.get("warnings") or []
    keys = [(w.get("ruleId"), w.get("trigger")) for w in warns]
    assert len(keys) == len(set(keys))


def test_rank_gems_filters_archetype_swaps_for_self_cast() -> None:
    from pob_cli import comparator

    class _FakeWorker:
        def __init__(self) -> None:
            import threading

            self.lock = threading.RLock()

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [
                        {
                            "index": 1,
                            "isMain": True,
                            "displayLabel": "Volatile Dead",
                            "gems": [
                                {"index": 1, "name": "Volatile Dead of Seething", "enabled": True, "support": False},
                                {"index": 2, "name": "Archmage Support", "enabled": True, "support": True},
                            ],
                        }
                    ],
                }
            if method == "get_gem_options":
                return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
            if method == "set_gem_options":
                return {"ok": True}
            if method == "gem_search":
                return {
                    "dpsField": "FullDPS",
                    "baseDps": 1_000_000,
                    "gems": [
                        {"name": "Cast when Stunned", "nameZh": "晕眩时施放(辅)", "delta": 1_200_000, "pct": 120.0},
                        {"name": "Spell Totem", "nameZh": "法术图腾(辅)", "delta": 500_000, "pct": 50.0},
                        {"name": "Elemental Penetration", "nameZh": "元素穿透", "delta": 350_000, "pct": 35.0},
                        {"name": "Controlled Destruction", "nameZh": "精准破坏", "delta": 300_000, "pct": 30.0},
                    ],
                }
            return {}

    res = comparator.rank_gems(
        _FakeWorker(),  # type: ignore[arg-type]
        group_selector="main",
        add_link=True,
        field="FullDPS",
        top=3,
        exclude_archetype_swaps=True,
    )
    cand_names = [c["name"] for c in res["candidates"]]
    assert "Cast when Stunned" not in cand_names
    assert "Spell Totem" not in cand_names
    assert cand_names == ["Elemental Penetration", "Controlled Destruction"]
    assert any("Cast when Stunned" in s for s in (res.get("filteredArchetypeSwaps") or []))


# ---------------------------------------------------------------------------
# P1-2: Slim Router Skill (`skills/pob/SKILL.md` + `references/`)
# ---------------------------------------------------------------------------


def test_dsh_slim_router_skill_structure() -> None:
    skill_dir = REPO_ROOT / "skills" / "pob"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"Missing {skill_md}"
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 90, f"SKILL.md has {len(lines)} lines (must be <= 90)"
    assert lines[0].strip() == "---"
    end_fm = lines.index("---", 1)
    fm = "\n".join(lines[1:end_fm])
    assert "name: pob-poe1" in fm
    assert "description:" in fm
    assert "PoE1" in fm and "法血" in fm

    for ref_name in (
        "whatif-and-batch.md",
        "trade-and-bench.md",
        "timeless.md",
        "poe1-330-anti-hallucination.md",
    ):
        ref_path = skill_dir / "references" / ref_name
        assert ref_path.is_file() and ref_path.stat().st_size > 200, f"Missing/empty reference: {ref_path}"


# ---------------------------------------------------------------------------
# P2: Live DS 4.1F Torture-Run Fixes (3.30 Mechanics, CDR Ticks, Quality BP,
#     Rich Diagnose, Bloodsoaked Tinctures, Trigger Leak, Base64 Truncation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cdr_pct", "exp_ticks", "exp_ms", "exp_next_cdr", "exp_next_ticks"),
    [
        (0, 16, 528, 2, 15),
        (50, 11, 363, 52, 10),
        (52, 10, 330, 69, 9),
        (68, 10, 330, 69, 9),
        (69, 9, 297, 90, 8),
        (90, 8, 264, 117, 7),
    ],
)
def test_server_tick_cooldown_exact_33ms_math(
    cdr_pct: float,
    exp_ticks: int,
    exp_ms: int,
    exp_next_cdr: int,
    exp_next_ticks: int,
) -> None:
    res = analyzer.compute_server_tick_cooldown(0.50, cdr_pct)
    assert res["serverTickSec"] == 0.033
    assert res["ticks"] == exp_ticks
    assert res["effectiveCdMs"] == exp_ms
    assert res["nextBreakpoint"]["requiredCdrPct"] == exp_next_cdr
    assert res["nextBreakpoint"]["targetTicks"] == exp_next_ticks
    assert res["nextBreakpoint"]["cdrNeededPct"] == int(exp_next_cdr - cdr_pct)


def test_detect_build_mechanics_sword_of_ages_dead_arcane_surge_swap_and_inversion() -> None:
    items_res = {
        "slots": [
            {"name": "Weapon 1", "selItemId": 1},
            {"name": "Boots", "selItemId": 2},
            {"name": "Belt", "selItemId": 3},
            {"name": "Body", "selItemId": 4},
        ],
        "items": [
            {"id": 1, "name": "The Sword of Ages", "title": "The Sword of Ages", "rarity": "UNIQUE", "type": "Two Handed Sword"},
            {"id": 2, "name": "Blood Hoof", "rarity": "RARE", "type": "Boots"},
            {"id": 3, "name": "Rift Leash", "rarity": "RARE", "type": "Belt"},
            {"id": 4, "name": "Greater Severed Stinger", "rarity": "UNIQUE", "type": "Viscera"},
        ],
    }
    raw_slots = [
        {"slotName": "Weapon 1", "raw": "Rarity: UNIQUE\nThe Sword of Ages\nSockets: W-R-G-G-B-W\nTrigger Level 20 Temporal Mirage on using a Movement Attack"},
        {"slotName": "Boots", "raw": "7% increased Cooldown Recovery Rate\n15% increased Cooldown Recovery Rate of Travel Skills"},
        {"slotName": "Belt", "raw": "{crafted}11% increased Cooldown Recovery Rate"},
        {"slotName": "Body", "raw": "Hits have 14% chance to treat Enemy Monster Lightning Resistance values as inverted"},
    ]
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Weapon 1",
                "enabled": True,
                "gems": [
                    {"name": "Firestorm of Pelting", "level": 18, "quality": 20, "enabled": True, "support": False},
                    {"name": "Greater Spell Cascade", "level": 1, "quality": 0, "enabled": True, "support": True},
                    {"name": "Arcane Surge", "level": 17, "quality": 0, "enabled": True, "support": True},
                    {"name": "Invert the Rules", "level": 2, "quality": 20, "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "slot": "Weapon 1 Swap",
                "enabled": True,
                "gems": [{"name": "Archmage", "nameZh": "大法师（辅）", "level": 19, "enabled": True, "support": True}],
            },
            {
                "index": 3,
                "slot": "Gloves",
                "enabled": True,
                "gems": [{"name": "Conductivity", "level": 19, "enabled": True, "support": False}],
            },
        ],
    }
    alloc_nodes = [
        ("Like Clockwork", ["50% increased Cooldown Recovery Rate"]),
        ("Perfect Crime", ["Grants Level 20 Summon Triggerbots", "30% less Damage with Triggered Spells"]),
    ]
    gaps, mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res=skills_res,
        items_res=items_res,
        cfg_res={"customMods": []},
        tree_summary={"ascendancyNodes": [{"id": 28535, "name": "Perfect Crime", "asc": "Saboteur"}]},
        equipped_slots_raw=raw_slots,
        alloc_node_stats=alloc_nodes,
    )
    gap_msgs = [g["message"] for g in gaps]
    assert any("Dead Arcane Surge Support" in m for m in gap_msgs)
    assert any("global 12-storm cap" in m for m in gap_msgs)
    assert any("Level 1 support gem" in m for m in gap_msgs)
    assert any("6 sockets but only 4 gem(s)" in m for m in gap_msgs)
    assert any("Orphan support gem(s) in 'Weapon 1 Swap'" in m for m in gap_msgs)
    assert any("Resistance Inversion anti-synergy" in m for m in gap_msgs)
    assert any("43% more Spell Damage" in m for m in gap_msgs)

    soa = mech["swordOfAges"]
    # Local Travel Skill CDR (15%) MUST be excluded -> 50 + 7 + 11 = 68%
    assert soa["globalCdrPct"] == 68.0
    assert soa["temporalMirageCooldown"]["ticks"] == 10
    assert soa["temporalMirageCooldown"]["effectiveCdMs"] == 330
    assert soa["temporalMirageCooldown"]["nextBreakpoint"]["requiredCdrPct"] == 69


def test_detect_dark_bargain_double_count_and_bloodsoaked_tincture_preservation() -> None:
    # 1. Dark Bargain count=3 + Greater Spell Echo double-count warning
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 64},
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 1,
                    "isMain": True,
                    "slot": "Body Armour",
                    "enabled": True,
                    "includeInFullDPS": True,
                    "gems": [
                        {"name": "Dark Bargain", "level": 21, "count": 3, "enabled": True, "support": False},
                        {"name": "Greater Spell Echo", "level": 3, "enabled": True, "support": True},
                    ],
                }
            ],
        },
        items_res={"slots": [], "items": []},
        cfg_res={"customMods": []},
    )
    assert any(g["category"] == "PoBWater" and "double-counts FullDPS by 3x" in g["message"] for g in gaps)

    # 2. Bloodsoaked Blade preserves active Tincture in dewater_build_with_records while stripping normal flask
    class _TinctureFakeWorker:
        def __init__(self) -> None:
            import threading
            self.lock = threading.RLock()
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            self.calls.append((method, dict(params or {})))
            if method == "list_config":
                return {"sections": [], "customMods": []}
            if method == "list_items":
                return {
                    "slots": [
                        {"name": "Flask 1", "isFlask": True, "active": True, "selItemId": 10},
                        {"name": "Flask 4", "isFlask": True, "active": True, "selItemId": 11},
                    ],
                    "items": [
                        {"id": 10, "name": "Sap of the Seasons", "type": "Tincture"},
                        {"id": 11, "name": "Diamond Flask", "type": "Flask"},
                    ],
                }
            if method == "list_skills":
                return {"mainSocketGroup": 1, "groups": []}
            if method == "get_party":
                return {"actor": {}}
            if method == "get_tree_state":
                return {"allocatedNodes": [13019], "treeVersion": "3_30"}
            return {"ok": True}

        def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
            return {"nodes": {"13019": {"name": "Bloodsoaked Blade", "type": "Keystone"}}}

    fw = _TinctureFakeWorker()
    stripped, records = analyzer.dewater_build_with_records(fw)  # type: ignore[arg-type]
    assert any("Flask 1" in k and "Bloodsoaked Blade" in k for k in stripped["kept"])
    assert any("Flask 4" in f for f in stripped["flasks"])
    assert all("Flask 1" not in r["label"] for r in records)
    assert any("Flask 4" in r["label"] for r in records)


def test_rank_gems_from_item_trigger_does_not_unlock_socketed_triggers() -> None:
    from pob_cli import comparator

    class _ItemTriggerWorker:
        def __init__(self) -> None:
            import threading
            self.lock = threading.RLock()

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [
                        {
                            "index": 1,
                            "isMain": True,
                            "displayLabel": "Volatile Dead of Seething",
                            "gems": [
                                {"index": 1, "name": "Volatile Dead of Seething", "enabled": True, "support": False},
                                {"index": 2, "name": "SupportUniqueSuspendInTimeCanUse", "enabled": True, "support": True, "fromItem": True},
                            ],
                        }
                    ],
                }
            if method == "get_gem_options":
                return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
            if method == "set_gem_options":
                return {"ok": True}
            if method == "gem_search":
                return {
                    "dpsField": "CombinedDPS",
                    "baseDps": 100_000,
                    "gems": [
                        {"name": "Cast while Channelling", "delta": 90_000, "pct": 90.0},
                        {"name": "Cast when Damage Taken", "delta": 85_000, "pct": 85.0},
                        {"name": "Cast on Ward Break", "delta": 80_000, "pct": 80.0},
                        {"name": "Archmage Support", "delta": 75_000, "pct": 75.0},
                    ],
                }
            return {}

    res = comparator.rank_gems(
        _ItemTriggerWorker(),  # type: ignore[arg-type]
        group_selector="main",
        add_link=True,
        top=3,
        exclude_archetype_swaps=True,
    )
    assert [c["name"] for c in res["candidates"]] == ["Archmage Support"]
    filtered = res.get("filteredArchetypeSwaps") or []
    assert any("Cast while Channelling" in s for s in filtered)
    assert any("Cast on Ward Break" in s for s in filtered)


def test_base64_source_label_compaction_and_graft_slot_resolution() -> None:
    from pob_cli.bridge_protocol import compact_source_label

    huge_b64 = "eNrt" + ("A" * 500)
    assert compact_source_label(huge_b64) == f"share_code({len(huge_b64)} chars)"
    assert compact_source_label("https://pobb.in/abc123") == "https://pobb.in/abc123"
    assert compact_source_label("C:/pob/builds/test.xml") == "C:/pob/builds/test.xml"

    # Graft slot vs Body Armour disambiguation
    assert normalize_equipment_slot("body") == "Body Armour"
    assert normalize_equipment_slot("graft_body") == "Body"
    assert normalize_equipment_slot("肢体躯干") == "Body"
    slots_with_graft = {
        "slots": [
            {"name": "Body Armour", "selItemId": 1, "shown": True},
            {"name": "Body", "selItemId": 2, "shown": True},
            {"name": "Head", "selItemId": 3, "shown": True},
        ]
    }
    assert analyzer.resolve_slot_name(None, "Body", items_res=slots_with_graft) == "Body"  # type: ignore[arg-type]
    assert analyzer.resolve_slot_name(None, "body", items_res=slots_with_graft) == "Body Armour"  # type: ignore[arg-type]
    assert analyzer.resolve_slot_name(None, "graft_body", items_res=slots_with_graft) == "Body"  # type: ignore[arg-type]


def test_diagnose_markdown_ci_and_rich_sections_no_scientific_notation() -> None:
    fake_ci_diag = {
        "env": {"game": "poe1", "patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "zh-test", "treeVersion": "3_30", "slot": "main"},
        "buildInfo": {"level": 97, "className": "Scion", "classNameZh": "贵族", "ascendClassName": "Reliquarian", "ascendClassNameZh": "守藏者", "points": {"used": 123, "usedMax": 126, "ascUsed": 8, "ascMax": 8}},
        "mainGroup": {"index": 1, "slot": "Body Armour", "displayLabel": "Dark Bargain", "gems": [{"name": "Dark Bargain", "level": 21, "quality": 20, "enabled": True}]},
        "keystones": ["Chaos Inoculation", "Bloodsoaked Blade"],
        "ascendancyNodes": [
            {"id": 25795, "name": "The Living Blade", "nameZh": "活木之刃", "asc": "Reliquarian", "stats": ["You can have 2 additional Tinctures active"]},
            {"id": 44903, "name": "Toad's Price", "nameZh": "蟾蜍的代价", "asc": "KingInTheMists", "stats": ["Utility Flasks are Disabled"]},
        ],
        "original": {"primaryDpsKey": "FullDPS", "primaryDps": 72_108_100, "totalEhp": "inf", "physMaxHit": 37_989, "eleMaxHit": 60_625, "chaosMaxHit": "inf"},
        "dewateredPinnacle": {"primaryDpsKey": "FullDPS", "primaryDps": 14_071_200, "totalEhp": 141_966, "physMaxHit": 23_024, "eleMaxHit": 34_456, "chaosMaxHit": "inf"},
        "defence": {"Life": 1, "EnergyShield": 9699, "Mana": 823, "ManaUnreserved": 173, "FireResist": 82, "ColdResist": 87, "LightningResist": 83, "ChaosResist": -26, "EffectiveSpellSuppressionChance": 0, "EffectiveBlockChance": 75, "EffectiveSpellBlockChance": 75, "NetLifeRegen": 0, "NetManaRegen": 326},
        "offence": {"ManaCost": 64},
        "skillGroups": [
            {"index": 1, "isMain": True, "slot": "Body Armour", "enabled": True, "includeInFullDPS": True, "gems": [{"name": "Dark Bargain", "level": 21, "quality": 20, "count": 3, "enabled": True}]},
            {"index": 2, "isMain": False, "slot": "Weapon 1 Swap", "enabled": True, "gems": [{"name": "Archmage", "level": 19, "quality": 0, "enabled": False}]},
        ],
        "equippedItems": [
            {"slot": "Body", "name": "Maven's Orrery", "type": "Viscera", "rarity": "UNIQUE"},
            {"slot": "Flask 1", "name": "Sap of the Seasons", "isFlask": True, "isTincture": True, "active": True, "rarity": "UNIQUE"},
            {"slot": "Jewel 61419", "nodeId": 61419, "name": "Lethal Pride", "nameZh": "致命的骄傲", "rarity": "UNIQUE"},
        ],
        "inflationStripped": {"config": [], "flasks": [], "skills": [], "party": [], "kept": [], "keepRejected": []},
        "healthGaps": [],
        "factCards": {"gems": [], "uniques": [], "equippedUniques": []},
        "advisories": [],
    }
    md = format_diagnose_markdown(fake_ci_diag)
    assert "72,108,100" in md and "7.21081e+07" not in md
    assert "100(CI)%" in md
    assert "活木之刃 (The Living Blade)" in md
    assert "You can have 2 additional Tinctures active" in md
    assert "蟾蜍的代价 (Toad's Price)" in md
    assert "3.30 Wickerman Grafts" in md and "Maven's Orrery" in md
    assert "[Tincture][ON]" in md
    assert "x3" in md and "[OFF]" in md


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 engine data not available")
def test_gem_quality_breakpoints_and_330_slang_grounding() -> None:
    # 1. Stratified active-only quality breakpoints (<100ms, includes integer_step + more_multiplier + qualityRule)
    res = game_data.query_gem_quality_breakpoints(active_only=True, limit=30, pob_dir=REAL_POB_DIR)
    assert res["count"] == 30
    assert res["elapsedMs"] < 250.0
    assert "grants_active_skill" in res["qualityRule"]
    assert all(not r["isSupport"] for r in res["results"])
    cats = {r["topCategory"] for r in res["results"]}
    assert {"integer_step", "more_multiplier", "high_value"}.issubset(cats)
    # No internal quality_display_* pollution
    for r in res["results"]:
        for qs in r["qualityStats"]:
            assert not qs["stat"].lower().startswith("quality_display_")

    # 2. Bilingual --search-quality
    proj_res = game_data.query_gem_quality_breakpoints("投射物", active_only=True, limit=10, pob_dir=REAL_POB_DIR)
    assert proj_res["count"] >= 5
    assert any("projectile" in qs["stat"].lower() for r in proj_res["results"] for qs in r["qualityStats"])

    # 3. CLI `pob data gem --quality-breakpoints --active-only`
    runner = CliRunner()
    r_cli = runner.invoke(cli.main, ["data", "gem", "--quality-breakpoints", "--active-only", "--limit", "10"])
    assert r_cli.exit_code == 0, r_cli.output
    assert "PoE1 3.30 Gem Quality Rule" in r_cli.output

    # 4. Expanded 3.30 CN slang & patch traps
    for kind, slang, exp in [
        ("unique", "万古之剑", "The Sword of Ages"),
        ("unique", "靛蓝头", "Indigon"),
        ("gem", "沸燃", "Volatile Dead of Seething"),
        ("gem", "逆转规则", "Invert the Rules"),
        ("unique", "枯萎尖叫", "Screams of the Desiccated"),
        ("unique", "四季之侵", "Sap of the Seasons"),
        ("gem", "暗夜血契", "Dark Bargain"),
        ("unique", "恨意", "Taste of Hate"),
        ("unique", "卫道之袍", "Cloak of Defiance"),
    ]:
        q = game_data.query_local_data(kind, slang, pob_dir=REAL_POB_DIR, limit=5)
        assert q["count"] >= 1, f"3.30 slang {slang!r} ({kind}) missed"
        assert any(exp.lower() in (it.get("name") or "").lower() for it in q["results"])

    g_330 = game_data.ground_text_entities(
        "圣宗3.30的Sanctuary of Thought还有50%减耗吗？万古之剑配卫道之袍和恨意，+20%所有主动技能石品质对大法师辅助生效吗？",
        pob_dir=REAL_POB_DIR,
    )
    pids = {t["ruleId"] for t in g_330["patchTraps"]}
    assert {
        "PATCH_330_SANCTUARY_OF_THOUGHT",
        "PATCH_330_CLOAK_OF_DEFIANCE",
        "PATCH_325_TASTE_OF_HATE",
        "PATCH_330_SWORD_OF_AGES_TEMPORAL_MIRAGE",
        "PATCH_330_SKILL_GEM_QUALITY_SCOPE",
    }.issubset(pids)


def test_traditional_slot_names_resolve_like_simplified_ones() -> None:
    """The slot aliases are Simplified and the engine's labels Traditional: either script must match both."""
    from pob_cli.mutators.common import _resolve_slot_from_items
    from pob_cli.turn_receipts import classify_cli_args

    items = {
        "slots": [
            {"name": "Helmet", "labelZh": "頭部"},
            {"name": "Flask 1", "labelZh": "藥劑 1"},
            {"name": "Jewel 7", "labelZh": "珠寶插槽 7"},
            {"name": "Jewel 9", "label": "珠寶插槽 9"},
        ]
    }
    for query, expected in (
        ("頭盔", "Helmet"),
        ("头盔", "Helmet"),
        ("藥劑1", "Flask 1"),
        ("珠宝插槽 7", "Jewel 7"),
        ("珠寶插槽 7", "Jewel 7"),
        ("珠寶插槽 9", "Jewel 9"),
    ):
        assert analyzer.resolve_slot_name(None, query, items_res=items) == expected  # type: ignore[arg-type]
        assert _resolve_slot_from_items(items, query) == expected
    assert classify_cli_args(["item", "inspect", "--slot", "頭盔"])["inspectedSlots"] == ["Helmet"]
