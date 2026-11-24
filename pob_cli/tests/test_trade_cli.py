"""CLI integration tests for `pob trade` and `pob bench` (fully offline, mocked network & daemon)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.trade_client import CookieRequired, NoResults, RateLimited, TradeClient

SAMPLE_STATS = [
    {
        "label": "Explicit",
        "entries": [
            {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
            {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
            {"id": "explicit.stat_1671376347", "text": "+#% to Lightning Resistance", "type": "explicit"},
            {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
            {"id": "explicit.stat_681332047", "text": "#% increased Attack Speed", "type": "explicit"},
            {"id": "explicit.stat_210067635", "text": "#% increased Attack Speed (Local)", "type": "explicit"},
        ],
    },
    {
        "label": "Pseudo",
        "entries": [
            {"id": "pseudo.pseudo_number_of_empty_suffix_mods", "text": "# Empty Suffix Modifiers", "type": "pseudo"},
            {"id": "pseudo.pseudo_number_of_empty_prefix_mods", "text": "# Empty Prefix Modifiers", "type": "pseudo"},
        ],
    },
]

SAMPLE_FETCH_ROWS = [
    {
        "id": "row_fire",
        "item": {
            "name": "Doom Hand",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 84,
            "properties": [{"name": "Evasion Rating", "values": [["240", 1]]}],
            "explicitMods": [
                {
                    "description": "+42% to Fire Resistance",
                    "flags": {},
                    "mods": [{"name": "of the Kiln", "tier": "S2", "level": 72, "magnitudes": [{"min": "42", "max": "45"}]}],
                },
                {
                    "description": "+85 to maximum Life",
                    "flags": {},
                    "mods": [{"name": "Athlete's", "tier": "P2", "level": 64, "magnitudes": [{"min": "80", "max": "89"}]}],
                },
                {
                    "description": "14% increased Attack Speed",
                    "flags": {},
                    "mods": [{"name": "of Grandmastery", "tier": "S1", "level": 77, "magnitudes": [{"min": "14", "max": "16"}]}],
                },
            ],
        },
        "listing": {"price": {"amount": 15, "currency": "chaos"}},
    },
    {
        "id": "row_cold",
        "item": {
            "name": "Rift Claw",
            "typeLine": "Slink Gloves",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 82,
            "explicitMods": [
                {"description": "+35% to Cold Resistance", "flags": {}},
                {"description": "+65 to maximum Life", "flags": {}},
            ],
        },
        "listing": {"price": {"amount": 8, "currency": "chaos"}},
    },
]

CURRENT_GLOVES_RAW = """Rarity: RARE
Old Hand
Slink Gloves
Item Level: 75
Implicits: 0
+50 to maximum Life
+25% to Cold Resistance
"""


def _mock_trade_stats(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], str]:
    flat: list[dict[str, Any]] = []
    for g in SAMPLE_STATS:
        flat.extend(g["entries"])
    return flat, "mock_local"


def test_cli_help_registers_trade_and_bench() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    assert "trade" in res.output
    assert "bench" in res.output


def test_trade_stats_bilingual_and_slot_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    runner = CliRunner()

    res_weapon = runner.invoke(main, ["trade", "stats", "攻击速度", "--slot", "weapon1", "--json"])
    assert res_weapon.exit_code == 0, res_weapon.output
    data_w = json.loads(res_weapon.output)
    assert data_w["match"]["id"] == "explicit.stat_210067635"

    res_gloves = runner.invoke(main, ["trade", "stats", "攻击速度", "--slot", "gloves", "--json"])
    assert res_gloves.exit_code == 0, res_gloves.output
    data_g = json.loads(res_gloves.output)
    assert data_g["match"]["id"] == "explicit.stat_681332047"


def test_trade_search_dump_query_is_zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("network must not be called during --dump-query")

    monkeypatch.setattr(httpx.Client, "request", _boom)
    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", _boom)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "冰霜抗性=40",
            "--budget",
            "20",
            "--allow-conversion",
            "--open-suffix",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    q = payload["query"]["query"]
    assert q["status"] == {"option": "available"}
    # Default currency=chaos_equiv omits price.option
    assert q["filters"]["trade_filters"]["filters"]["price"] == {"max": 20.0}
    # --allow-conversion injects corrupted=false and mirrored=false
    assert q["filters"]["misc_filters"]["filters"] == {
        "corrupted": {"option": "false"},
        "mirrored": {"option": "false"},
    }
    assert any(g["type"] == "count" and g["value"] == {"min": 1} for g in q["stats"])
    assert payload["openAffixes"]["suffix"] is True
    assert payload["benchOptions"]


def test_trade_search_rejects_unique_with_allow_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--rarity",
            "unique",
            "--stat",
            "to Cold Resistance=30",
            "--allow-conversion",
            "--dump-query",
        ],
    )
    assert res.exit_code == 1


def test_trade_search_full_loop_with_batch_pob_and_ci_inf_json_safety(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q123", "total": 2, "result": ["row_fire", "row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return SAMPLE_FETCH_ROWS[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    rpc_calls: list[str] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        rpc_calls.append(action)
        if action == "items":
            return {"raw": CURRENT_GLOVES_RAW}
        if action == "status":
            return {"main": {"buildInfo": {"className": "Witch", "ascendClassName": "Necromancer", "level": 95}}}
        if action == "trade_eval_items":
            items = (params or {}).get("items") or []
            return {
                "slotName": "Gloves",
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": "inf", "life": 1.0},
                        "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_120_000.0 - 50_000 * idx, "totalEhp": "inf", "life": 1.0},
                        "diff": {"newWarnings": [], "resolvedWarnings": []},
                    }
                    for idx, entry in enumerate(items)
                ],
            }
        raise AssertionError(f"Unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "冰霜抗性=30",
            "--want",
            "Attack Speed",
            "--allow-conversion",
            "--pob",
            "on",
            "--sort",
            "pob",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "trade_eval_items" in rpc_calls
    data = json.loads(res.output)
    # Re-serialize strictly forbidding NaN/Infinity (CI inf build safety)
    json.dumps(data, allow_nan=False)
    assert data["league"] == "Covens"
    assert len(data["candidates"]) == 2
    top = data["candidates"][0]
    assert top["pob"]["dps_pct"] == 12.0
    assert top["pob"]["ehp_pct"] is None
    assert top["pob"]["ehp_after"] == "inf"
    assert top["conversions"] and top["conversions"][0]["rerolled"] is True


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (NoResults(), 2),
        (CookieRequired(403, "cf"), 3),
        (RateLimited(30), 4),
    ],
)
def test_trade_search_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected_code: int,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _ErrClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            raise exc

    monkeypatch.setattr(cli_trade, "TradeClient", _ErrClient)
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "off"],
    )
    assert res.exit_code == expected_code


def test_trade_search_pob_on_exits_6_on_daemon_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [SAMPLE_FETCH_ROWS[1]]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    monkeypatch.setattr(
        cli_trade,
        "_daemon_call_safe",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("daemon offline")),
    )

    runner = CliRunner()
    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--no-compare", "--pob", "on"],
    )
    assert res.exit_code == 6


def test_trade_convert_and_bench_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    out_file = tmp_path / "converted.txt"
    raw = "Rarity: RARE\nDoom Hand\nSlink Gloves\nImplicits: 0\n+42% to Fire Resistance\n"
    res = runner.invoke(
        main,
        ["trade", "convert", "--text", raw, "--to", "cold", "--out", str(out_file), "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["conversions"][0]["target"] == "cold"
    assert data["conversions"][0]["rerolled"] is True
    assert "+42% to Cold Resistance" in out_file.read_text(encoding="utf-8")

    # Bench list & suggest (EN + ZH)
    b_list = runner.invoke(main, ["bench", "list", "--slot", "gloves", "--affix", "suffix", "--limit", "5", "--json"])
    assert b_list.exit_code == 0, b_list.output
    bl_data = json.loads(b_list.output)
    assert bl_data["count"] > 0
    assert all(r["affix"] == "suffix" for r in bl_data["recipes"])

    b_sugg = runner.invoke(main, ["bench", "suggest", "--slot", "gloves", "--stat", "冰霜抗性", "--json"])
    assert b_sugg.exit_code == 0, b_sugg.output
    bs_data = json.loads(b_sugg.output)
    assert bs_data["best"]["affix"] == "suffix"
    assert any("{crafted}" in ln for ln in bs_data["best"]["craftedLinesMin"])


def test_trade_search_non_json_skips_redundant_status_and_supports_named_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    monkeypatch.setenv("POB_SLOT", "worker-alpha")

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q9", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [SAMPLE_FETCH_ROWS[1]]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        rpc_calls.append((action, dict(params or {})))
        if action == "items":
            return {"raw": CURRENT_GLOVES_RAW}
        if action == "trade_eval_items":
            items = (params or {}).get("items") or []
            return {
                "slotName": "Gloves",
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": 50_000.0, "life": 4000.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000.0, "totalEhp": 50_000.0, "life": 4000.0},
                        "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_050_000.0, "totalEhp": 52_000.0, "life": 4100.0},
                        "diff": {"newWarnings": [], "resolvedWarnings": []},
                    }
                    for entry in items
                ],
            }
        raise AssertionError(f"Unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    runner = CliRunner()
    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--pob", "on"],
    )
    assert res.exit_code == 0, res.output
    actions = [a for a, _ in rpc_calls]
    assert "items" in actions
    assert "trade_eval_items" in actions
    assert "status" not in actions
    assert all(p.get("target") == "worker-alpha" for _, p in rpc_calls)


def test_cli_lazy_imports_and_cold_startup_isolation() -> None:
    import subprocess
    import sys

    code = (
        "import sys, pob_cli.cli; "
        "banned = [m for m in ('httpx', 'pob_cli.cli_trade', 'pob_cli.bench', "
        "'pob_cli.poedb_client', 'pob_cli.game_data', 'pob_cli.daemon') if m in sys.modules]; "
        "assert not banned, f'Eagerly imported: {banned}'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_formatters_emit_json_sweep_matrix_and_tree(tmp_path: Path) -> None:
    from pob_cli.formatters import (
        emit_json,
        format_matrix_markdown,
        format_sweep_markdown,
        format_tree_markdown,
    )

    out_file = tmp_path / "out.json"
    text = emit_json({"dps": float("inf"), "nan": float("nan"), "ok": True}, compact=True, out=out_file)
    assert "\n" not in text
    assert '"dps":"inf"' in text
    raw_bytes = out_file.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 without BOM
    parsed = json.loads(raw_bytes.decode("utf-8"))
    assert parsed == {"dps": "inf", "nan": None, "ok": True}

    # Compact matrix payload
    compact_matrix = {
        "baseline": "Original",
        "scenarios": [
            {
                "name": "Original",
                "summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000, "totalEhp": 80_000, "life": 4500, "energyShield": 1200},
                "coreStats": {"AverageDamage": 200_000, "Speed": 5.0, "PhysicalMaximumHitTaken": 18000, "FireMaximumHitTaken": 45000, "ChaosMaximumHitTaken": 22000},
            },
            {
                "name": "Pinnacle",
                "summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 700_000, "totalEhp": 60_000, "life": 4500, "energyShield": 1200},
                "coreStats": {"AverageDamage": 140_000, "Speed": 5.0, "PhysicalMaximumHitTaken": 14000, "FireMaximumHitTaken": 32000, "ChaosMaximumHitTaken": 18000},
            },
        ],
    }
    md_m = format_matrix_markdown(compact_matrix, target="worker1")
    assert "worker1" in md_m
    assert "Pinnacle" in md_m
    assert "-30.0%" in md_m

    # Sweep payload with one error variant
    sweep_res = {
        "scenario": "Pinnacle",
        "baseline": {"name": "Base", "summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 500_000, "totalEhp": 50_000, "life": 4000, "energyShield": 0}},
        "calcKeywords": ["Mana"],
        "variants": [
            {"name": "Arc", "ok": True, "summary": {"primaryDps": 600_000, "totalEhp": 52_000, "life": 4000, "energyShield": 0}, "calcs": {"Mana": 120}},
            {"name": "BadGem", "ok": False, "error": "Gem not found"},
        ],
    }
    md_s = format_sweep_markdown(sweep_res, target="exp1")
    assert "Arc" in md_s and "ERROR" in md_s and "Gem not found" in md_s

    # Tree markdown with path & masteryEffects
    tree_res = {
        "totalMatches": 1,
        "matches": [
            {
                "id": 12345,
                "name": "Life Mastery",
                "type": "Mastery",
                "allocated": False,
                "cost": 3,
                "path": [100, 200, 12345],
                "stats": ["Life Mastery"],
                "masteryEffects": [
                    {"effect": 47642, "stats": ["+50 to maximum Life"]},
                    {"effect": 53188, "stats": ["15% increased maximum Life if no Life Modifiers on Equipped Body Armour"]},
                ],
            }
        ],
    }
    md_t = format_tree_markdown(tree_res, search_label="Life", show_path=True, masteries=True)
    assert "100 -> 200 -> 12345" in md_t
    assert "12345:47642" in md_t
    assert "12345:53188" in md_t


def test_new_cli_commands_and_global_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pob_cli.cli as cli_mod

    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_call(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        if action == "new_build":
            return {
                "mode": "apply",
                "before": {},
                "after": {
                    "primaryDpsKey": "CombinedDPS",
                    "primaryDps": 0,
                    "totalEhp": 0,
                    "life": 50,
                    "points": {"used": 0, "usedMax": 123, "ascUsed": 0, "ascMax": 8},
                },
                "diff": {"deltas": []},
                "detail": {
                    "buildInfo": {
                        "className": "Templar",
                        "classNameZh": "圣堂武僧",
                        "ascendClassName": "Hierophant",
                        "ascendClassNameZh": "圣宗",
                        "level": 95,
                        "points": {"used": 0, "usedMax": 123, "ascUsed": 0, "ascMax": 8},
                    },
                    "level": 95,
                },
            }
        if action == "sweep":
            return {"variants": [{"name": "v1", "ok": True, "summary": {"primaryDps": 1000, "totalEhp": 5000}}]}
        if action == "batch":
            return {"mode": "what-if", "before": {}, "after": {}, "diff": {"deltas": []}, "returns": {"export": {"code": "ABC"}}}
        if action in ("skill_new_group", "skill_replace_group", "item_unequip", "tree_change"):
            return {"mode": "what-if", "before": {}, "after": {}, "diff": {"deltas": []}}
        if action == "tree":
            return {"totalMatches": 0, "matches": []}
        if action == "config_list":
            return {"activeCount": 0, "totalCount": 10, "variables": []}
        if action == "calc":
            return {"totalMatches": 0, "matches": []}
        if action == "matrix":
            return {"baseline": "Original", "scenarios": []}
        if action == "item_db":
            return {"dbType": "UNIQUE", "total": 0, "matches": []}
        return {}

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    runner = CliRunner()

    # 1. pob new with named --slot alias + --compact + --out
    out_new = tmp_path / "new.json"
    r1 = runner.invoke(main, ["new", "--class", "Templar", "--asc", "Hierophant", "--level", "95", "--slot", "archmage-1", "--json", "--compact", "--out", str(out_new)])
    assert r1.exit_code == 0, r1.output
    assert captured[-1] == ("new_build", {"target": "archmage-1", "class_name": "Templar", "ascendancy": "Hierophant", "secondary_ascendancy": None, "level": 95, "apply": True})
    assert "\n" not in out_new.read_text(encoding="utf-8").strip()

    # 2. pob batch via stdin ('--file -') + --no-undo + --return
    r2 = runner.invoke(main, ["batch", "--file", "-", "--apply", "--no-undo", "--return", "summary,export", "--json"], input='[{"op":"set_level","level":98}]')
    assert r2.exit_code == 0, r2.output
    assert captured[-1][0] == "batch"
    assert captured[-1][1]["no_undo"] is True
    assert captured[-1][1]["return_sections"] == ["summary", "export"]

    # 3. pob sweep via stdin + --calc + --scenario
    r3 = runner.invoke(main, ["sweep", "--file", "-", "--calc", "DPS", "--calc", "Mana", "--scenario", "Pinnacle", "--json"], input='[{"name":"v1","ops":[]}]')
    assert r3.exit_code == 0, r3.output
    assert captured[-1][0] == "sweep"
    assert captured[-1][1]["calc_keywords"] == ["DPS", "Mana"]
    assert captured[-1][1]["scenario"] == "Pinnacle"

    # 4. skill new-group & replace-group & item unequip & item db flags
    r4 = runner.invoke(main, ["skill", "new-group", "--label", "Main", "--slot", "Body Armour", "--gem", "Arc:21:20", "--gem", "Archmage Support:20:20", "--json"])
    assert r4.exit_code == 0, r4.output
    assert captured[-1][1]["gems"] == [{"name": "Arc", "level": 21, "quality": 20}, {"name": "Archmage Support", "level": 20, "quality": 20}]

    r5 = runner.invoke(main, ["skill", "replace-group", "--group", "main", "--gem", "Ball Lightning:21:20", "--json"])
    assert r5.exit_code == 0, r5.output
    assert captured[-1][0] == "skill_replace_group"

    r6 = runner.invoke(main, ["item", "unequip", "--slot", "Weapon 2", "--json"])
    assert r6.exit_code == 0, r6.output
    assert captured[-1] == ("item_unequip", {"target": "main", "slot": "Weapon 2", "apply": False})

    r7 = runner.invoke(main, ["item", "db", "Mageblood", "--with-tooltips", "--with-raw", "--json"])
    assert r7.exit_code == 0, r7.output
    assert captured[-1][1]["with_tooltips"] is True and captured[-1][1]["with_raw"] is True

    # 5. tree filters + tree change cascade guard + config search + variadic calc + matrix --full
    r8 = runner.invoke(main, ["tree", "--search", "Life", "--search", "Mana", "--type", "Notable", "--unallocated", "--max-cost", "4", "--show-path", "--masteries", "--json"])
    assert r8.exit_code == 0, r8.output
    assert captured[-1][1]["queries"] == ["Life", "Mana"]
    assert captured[-1][1]["node_types"] == ["Notable"]
    assert captured[-1][1]["allocated"] is False
    assert captured[-1][1]["max_cost"] == 4
    assert captured[-1][1]["show_path"] is True and captured[-1][1]["masteries"] is True

    r9 = runner.invoke(main, ["tree", "change", "--deallocate", "123", "--no-allow-cascade", "--json"])
    assert r9.exit_code == 0, r9.output
    assert captured[-1][1]["allow_cascade"] is False

    r10 = runner.invoke(main, ["config", "list", "--search", "shock", "--with-options", "--json"])
    assert r10.exit_code == 0, r10.output
    assert captured[-1][1]["search"] == "shock" and captured[-1][1]["with_options"] is True

    r11 = runner.invoke(main, ["calc", "DPS", "Mana", "--limit", "5", "--no-breakdown", "--json"])
    assert r11.exit_code == 0, r11.output
    assert captured[-1][1]["keywords"] == ["DPS", "Mana"] and captured[-1][1]["no_breakdown"] is True

    r12 = runner.invoke(main, ["matrix", "--full", "--json"])
    assert r12.exit_code == 0, r12.output
    assert captured[-1][1]["full"] is True


def test_cli_variadic_data_price_wiki_poedb_and_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.daemon as daemon_mod
    import pob_cli.external_data as ext_mod
    import pob_cli.game_data as gd_mod
    import pob_cli.poedb_client as pc_mod

    # Variadic data (single vs multi)
    monkeypatch.setattr(
        gd_mod,
        "query_local_data",
        lambda kind, q, **_kw: {"kind": kind, "query": q, "count": 1, "results": [{"name": q}]},
    )
    if hasattr(gd_mod, "query_local_data_multi"):
        monkeypatch.setattr(
            gd_mod,
            "query_local_data_multi",
            lambda kind, qs, **_kw: {"kind": kind, "queries": list(qs), "results": {q: {"kind": kind, "count": 1, "results": [{"name": q}]} for q in qs}},
        )
    if hasattr(gd_mod, "query_multi"):
        monkeypatch.setattr(
            gd_mod,
            "query_multi",
            lambda *args, **_kw: {
                "kind": args[1] if len(args) >= 3 else args[0],
                "queries": list(args[2] if len(args) >= 3 else args[1]),
                "results": {q: {"kind": "unique", "count": 1, "results": [{"name": q}]} for q in (args[2] if len(args) >= 3 else args[1])},
            },
        )

    runner = CliRunner()
    d_single = runner.invoke(main, ["data", "unique", "Mageblood", "--json"])
    assert d_single.exit_code == 0, d_single.output
    assert json.loads(d_single.output)["query"] == "Mageblood"

    d_multi = runner.invoke(main, ["data", "unique", "Mageblood", "Headhunter", "--json"])
    assert d_multi.exit_code == 0, d_multi.output
    dm_payload = json.loads(d_multi.output)
    assert "Mageblood" in dm_payload["results"] and "Headhunter" in dm_payload["results"]

    # Variadic price, wiki, poedb + meta --proxy
    monkeypatch.setattr(ext_mod, "query_price", lambda q, **_kw: {"league": "Test", "totalMatches": 1, "matches": [{"name": q, "type": "Unique", "chaosValue": 100}]})
    if hasattr(ext_mod, "query_price_multi"):
        monkeypatch.setattr(ext_mod, "query_price_multi", lambda qs, **_kw: {"queries": list(qs), "results": {q: {"league": "Test", "totalMatches": 1, "matches": [{"name": q, "type": "Unique", "chaosValue": 100}]} for q in qs}})
    monkeypatch.setattr(ext_mod, "query_wiki", lambda q, **_kw: {"title": q, "url": f"https://poewiki.net/wiki/{q}", "summary": f"About {q}"})
    if hasattr(ext_mod, "query_wiki_multi"):
        monkeypatch.setattr(ext_mod, "query_wiki_multi", lambda qs, **_kw: {"queries": list(qs), "results": {q: {"title": q, "url": f"https://poewiki.net/wiki/{q}", "summary": f"About {q}"} for q in qs}})
    monkeypatch.setattr(ext_mod, "query_meta", lambda **_kw: {"league": "Test", "totalBuilds": 10, "ascendancies": [], "topSkills": []})
    monkeypatch.setattr(pc_mod, "query_poedb", lambda q, **_kw: {"title": q, "slug": q, "url": f"https://poedb.tw/{q}"})
    if hasattr(pc_mod, "query_poedb_multi"):
        monkeypatch.setattr(pc_mod, "query_poedb_multi", lambda qs, **_kw: {"queries": list(qs), "results": {q: {"title": q, "slug": q, "url": f"https://poedb.tw/{q}"} for q in qs}})

    assert runner.invoke(main, ["price", "Divine Orb", "Mageblood", "--proxy", "http://127.0.0.1:7890", "--json"]).exit_code == 0
    assert runner.invoke(main, ["wiki", "Archmage", "Mind over Matter", "--proxy", "http://127.0.0.1:7890", "--json"]).exit_code == 0
    assert runner.invoke(main, ["poedb", "Archmage", "Indigon", "--json"]).exit_code == 0
    assert runner.invoke(main, ["meta", "--proxy", "http://127.0.0.1:7890", "--json"]).exit_code == 0

    # pob serve delegates to run_daemon_forever (or run_daemon_server fallback)
    serve_called: list[dict[str, Any]] = []
    monkeypatch.setattr(
        daemon_mod,
        "run_daemon_forever",
        lambda **kw: serve_called.append(kw),
        raising=False,
    )
    r_serve = runner.invoke(main, ["serve", "--offline", "--proxy", "http://127.0.0.1:7890"])
    assert r_serve.exit_code == 0, r_serve.output
    assert serve_called and serve_called[0]["offline"] is True and serve_called[0]["proxy"] == "http://127.0.0.1:7890"


def test_batch_no_undo_and_return_export_markdown_and_slug_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pob_cli.cli as cli_mod
    from pob_cli.formatters import format_sweep_markdown

    # 1. P0 #2: batch --apply --no-undo with before=None, diff=None renders cleanly in Markdown
    #    and P2 #13: returns["export"] as str renders code preview cleanly
    def _fake_batch_call(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert action == "batch"
        return {
            "mode": "apply",
            "noUndo": True,
            "before": None,
            "after": {
                "primaryDpsKey": "CombinedDPS",
                "primaryDps": 1_250_000,
                "totalEhp": 75_000,
                "life": 4200,
                "points": {"used": 115, "usedMax": 123, "ascUsed": 8, "ascMax": 8},
            },
            "diff": None,
            "returns": {"export": "eNrtW1tv4zgS_" + ("A" * 80)},
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_batch_call)
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["batch", "--json-ops", '[{"op":"set_level","level":98}]', "--apply", "--no-undo", "--return", "export"],
    )
    assert res.exit_code == 0, res.output
    assert "1,250,000" in res.output
    assert "(no-undo)" in res.output
    assert "--no-undo" in res.output
    assert "eNrtW1tv4zgS_" in res.output
    assert "93 chars" in res.output

    # 2. P2 #11: format_sweep_markdown extracts values[0]["raw"]/text (never dict repr) and computes EleMaxHit
    sweep_payload = {
        "scenario": "Pinnacle",
        "baseline": {
            "name": "Base",
            "summary": {
                "primaryDpsKey": "CombinedDPS",
                "primaryDps": 800_000,
                "totalEhp": 60_000,
                "life": 4000,
                "energyShield": 1000,
                "physMaxHit": 15_000,
                "fireMaxHit": 42_000,
                "coldMaxHit": 38_000,
                "lightningMaxHit": 45_000,
                "chaosMaxHit": 20_000,
            },
        },
        "calcKeywords": ["Mana"],
        "variants": [
            {
                "name": "Archmage",
                "ok": True,
                "summary": {
                    "primaryDpsKey": "CombinedDPS",
                    "primaryDps": 1_200_000,
                    "totalEhp": 65_000,
                    "life": 4000,
                    "energyShield": 1500,
                    "physMaxHit": 16_000,
                    "eleMaxHit": 41_000,
                    "chaosMaxHit": 22_000,
                },
                "calcs": {
                    "Mana": [
                        {
                            "sectionId": "Mana",
                            "rowLabel": "Mana Cost",
                            "values": [{"raw": "142", "text": "142"}],
                        }
                    ]
                },
            }
        ],
    }
    md_s = format_sweep_markdown(sweep_payload, target="s1")
    assert "142" in md_s
    assert "sectionId" not in md_s
    assert "38,000" in md_s
    assert "41,000" in md_s

    # 3. P2 #14: reject '.' and '..' in slot slug validation (cli.py & cli_trade.py)
    for bad_slug in (".", ".."):
        r_cli = runner.invoke(main, ["summary", "--target", bad_slug])
        assert r_cli.exit_code != 0, f"cli summary accepted {bad_slug!r}"
        r_trade = runner.invoke(
            main, ["trade", "search", "--slot", "gloves", "--target", bad_slug, "--dump-query"]
        )
        assert r_trade.exit_code != 0, f"trade search accepted {bad_slug!r}"


def test_carbon_p1_new_calc_compare_borrow_and_tree_group_propagation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pob_cli.cli as cli_mod

    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_call(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        if action == "new_build":
            return {
                "mode": "apply",
                "before": {},
                "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 0, "totalEhp": 0, "life": 50, "points": {"used": 0, "usedMax": 123, "ascUsed": 0, "ascMax": 8}},
                "diff": {"deltas": []},
                "detail": {
                    "buildInfo": {
                        "className": "Templar",
                        "classNameZh": "圣堂武僧",
                        "ascendClassName": "Hierophant",
                        "ascendClassNameZh": "圣宗",
                        "level": 92,
                    },
                    "level": 92,
                },
            }
        if action == "calc":
            kws = (params or {}).get("keywords") or []
            if len(kws) > 1:
                dps_rows = [
                    {"sectionGroup": "Offence", "subsection": "Skill", "rowLabel": f"DPS Row {i}", "cells": [{"text": str(1000 * i)}]}
                    for i in range(1, 4)
                ]
                mana_rows = [
                    {"sectionGroup": "Offence", "subsection": "Mana", "rowLabel": "Mana Cost", "cells": [{"text": "42"}]},
                ]
                return {
                    "keyword": kws[0],
                    "keywords": kws,
                    "totalMatches": 4,
                    "results": [
                        {"keyword": "DPS", "totalMatches": 3, "matches": dps_rows},
                        {"keyword": "Mana", "totalMatches": 1, "matches": mana_rows},
                    ],
                    "matches": dps_rows + mana_rows,
                }
            return {
                "keyword": kws[0],
                "totalMatches": 1,
                "matches": [
                    {"sectionGroup": "Offence", "subsection": "Mana", "rowLabel": "Mana Cost", "cells": [{"text": "42"}]},
                ],
            }
        if action == "compare":
            return {
                "mainBuildInfo": {"level": 95, "ascendClassName": "Hierophant"},
                "refBuildInfo": {"level": 98, "ascendClassName": "Inquisitor"},
                "baselines": {
                    "original": [{"label": "DPS", "mainStr": "1M", "refStr": "2M", "diffRefVsMainStr": "+100%"}],
                    "envAlignedToMain": {"rows": []},
                    "pinnacleDewatered": {"rows": []},
                },
                "treeDiff": {"keystones": {"onlyInMain": [], "onlyInRef": []}, "notables": {"sharedCount": 5, "onlyInMain": [], "onlyInRef": []}},
                "skillDiff": {"mainGroupMain": {"index": 1, "displayLabel": "Arc"}, "refGroupMain": {"index": 1, "displayLabel": "Spark"}},
                "itemSlotDiff": [{"slot": "Amulet", "mainItem": "A", "refItem": "B"}],
            }
        if action == "borrow":
            return {"mode": "what-if", "before": {}, "after": {}, "diff": {"deltas": []}, "detail": {"notices": ["Copied Amulet"]}}
        if action in ("tree_change", "tree_set_class"):
            return {"mode": "what-if", "before": {}, "after": {}, "diff": {"deltas": []}, "detail": {"changes": ["Witch -> Necromancer"]}}
        if action == "load":
            return {"importMethod": "code", "buildInfo": None, "buildHeader": None}
        if action == "items":
            return {"slot": None, "item": None}
        return {}

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    runner = CliRunner()

    # [P1-1] pob new Markdown reads nested detail.buildInfo & detail.level
    r_new = runner.invoke(main, ["new", "--asc", "Hierophant"])
    assert r_new.exit_code == 0, r_new.output
    assert "圣堂武僧 / 圣宗" in r_new.output
    assert "| Level" in r_new.output and "92" in r_new.output

    # [P1-2] Variadic pob calc renders per-keyword subheaders without truncating 2nd keyword
    r_calc_multi = runner.invoke(main, ["calc", "DPS", "Mana", "--limit", "2"])
    assert r_calc_multi.exit_code == 0, r_calc_multi.output
    assert "### DPS" in r_calc_multi.output
    assert "### Mana" in r_calc_multi.output
    assert "DPS Row 1" in r_calc_multi.output and "DPS Row 2" in r_calc_multi.output
    assert "DPS Row 3" not in r_calc_multi.output
    assert "Mana Cost" in r_calc_multi.output

    # Single-keyword calc stays unchanged (no nested per-keyword subheader)
    r_calc_single = runner.invoke(main, ["calc", "Mana"])
    assert r_calc_single.exit_code == 0, r_calc_single.output
    assert "### Calcs Breakdown for `Mana`" in r_calc_single.output
    assert "Mana Cost" in r_calc_single.output

    # [P1-3] pob compare & pob borrow forward --target / --ref-target & POB_SLOT, interpolate Markdown
    monkeypatch.setenv("POB_SLOT", "worker-alpha")
    r_cmp = runner.invoke(main, ["compare", "--ref-target", "ref-beta"])
    assert r_cmp.exit_code == 0, r_cmp.output
    assert captured[-1] == ("compare", {"target": "worker-alpha", "ref_target": "ref-beta"})
    assert "`worker-alpha`" in r_cmp.output and "`ref-beta`" in r_cmp.output

    r_brw = runner.invoke(main, ["borrow", "--target", "worker-gamma", "--ref-target", "ref-beta", "--item-slot", "Amulet"])
    assert r_brw.exit_code == 0, r_brw.output
    assert captured[-1] == ("borrow", {"target": "worker-gamma", "ref_target": "ref-beta", "item_slots": ["Amulet"], "skill_group": None, "apply": False})
    assert "`ref-beta` -> `worker-gamma`" in r_brw.output

    # [P2-2] pob tree group flags propagate into change & set-class (and subcommand flags override)
    tree_out = tmp_path / "tree_change.json"
    r_tc = runner.invoke(main, ["tree", "--target", "slot-parent", "--json", "--compact", "--out", str(tree_out), "change", "--allocate", "12345"])
    assert r_tc.exit_code == 0, r_tc.output
    assert captured[-1][0] == "tree_change" and captured[-1][1]["target"] == "slot-parent"
    raw_tc = tree_out.read_text(encoding="utf-8").strip()
    assert "\n" not in raw_tc and json.loads(raw_tc)["mode"] == "what-if"

    r_tc_override = runner.invoke(main, ["tree", "--target", "slot-parent", "change", "--target", "slot-child", "--allocate", "12345", "--json"])
    assert r_tc_override.exit_code == 0, r_tc_override.output
    assert captured[-1][1]["target"] == "slot-child"

    r_sc = runner.invoke(main, ["tree", "--slot", "slot-asc", "--json", "set-class", "Witch", "Necromancer"])
    assert r_sc.exit_code == 0, r_sc.output
    assert captured[-1][0] == "tree_set_class" and captured[-1][1]["target"] == "slot-asc"
    assert json.loads(r_sc.output)["mode"] == "what-if"

    # [P2-3] Null-safe buildInfo/buildHeader/slot in cmd_load and cmd_items
    r_load = runner.invoke(main, ["load", "eNrtDummyCode"])
    assert r_load.exit_code == 0, r_load.output
    r_items = runner.invoke(main, ["items", "--slot", "Ring 1"])
    assert r_items.exit_code == 0, r_items.output
    assert "Slot `Ring 1` is empty." in r_items.output


def test_carbon_p1_trade_bench_compact_out_proxy_and_client_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)

    created_clients: list[Any] = []
    exited_clients: list[Any] = []
    league_proxy_seen: list[str | None] = []

    def _fake_resolve_league(*, proxy: str | None = None, **_kw: Any) -> str:
        league_proxy_seen.append(proxy)
        return ""  # force fallback to client.default_league()

    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", _fake_resolve_league)

    class _TrackingClient(TradeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created_clients.append(self)

        def default_league(self) -> str:
            return "Mirage"

        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q77", "total": 1, "result": ["row_cold"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [SAMPLE_FETCH_ROWS[1]]

        def __exit__(self, *exc_info: Any) -> None:
            exited_clients.append(self)
            super().__exit__(*exc_info)

    monkeypatch.setattr(cli_trade, "TradeClient", _TrackingClient)
    runner = CliRunner()

    # [P1-4 + P2-10] trade stats with root & subcommand --compact / --out + TradeClient closed
    stats_out = tmp_path / "sub" / "stats.json"
    r_st = runner.invoke(main, ["--compact", "--out", str(stats_out), "trade", "stats", "攻击速度", "--slot", "gloves", "--json"])
    assert r_st.exit_code == 0, r_st.output
    st_text = stats_out.read_text(encoding="utf-8").strip()
    assert "\n" not in st_text and json.loads(st_text)["match"]["id"] == "explicit.stat_681332047"
    assert len(created_clients) == 1 and exited_clients == created_clients

    # [P1-4] trade search --dump-query with subcommand --compact --out
    dump_out = tmp_path / "nested" / "query.json"
    r_dq = runner.invoke(main, ["trade", "search", "--slot", "gloves", "--stat", "冰霜抗性=40", "--dump-query", "--compact", "--out", str(dump_out)])
    assert r_dq.exit_code == 0, r_dq.output
    dq_text = dump_out.read_text(encoding="utf-8").strip()
    assert "\n" not in dq_text and json.loads(dq_text)["slot"] == "gloves"

    # [P1-4 + P1-5 + P2-10] trade search --proxy forwards proxy to resolve_current_trade_league,
    # opens only ONE TradeClient (reused for default_league fallback), closes it, and honors Markdown --out
    created_clients.clear()
    exited_clients.clear()
    league_proxy_seen.clear()
    md_out = tmp_path / "reports" / "trade.md"
    r_sr = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "to Cold Resistance=30",
            "--no-compare",
            "--pob",
            "off",
            "--proxy",
            "http://127.0.0.1:7890",
            "--out",
            str(md_out),
        ],
    )
    assert r_sr.exit_code == 0, r_sr.output
    assert league_proxy_seen == ["http://127.0.0.1:7890"]
    assert len(created_clients) == 1 and exited_clients == created_clients
    assert "Mirage" in md_out.read_text(encoding="utf-8")

    # [P1-4] trade convert --out creates parent dirs + honors --compact (root & subcommand)
    conv_out = tmp_path / "deep" / "dir" / "converted.txt"
    raw_item = "Rarity: RARE\nDoom Hand\nSlink Gloves\nImplicits: 0\n+42% to Fire Resistance\n"
    r_cv = runner.invoke(main, ["trade", "convert", "--text", raw_item, "--to", "cold", "--out", str(conv_out), "--json", "--compact"])
    assert r_cv.exit_code == 0, r_cv.output
    assert "\n" not in r_cv.output.strip()
    assert json.loads(r_cv.output)["target"] == "cold"
    assert "+42% to Cold Resistance" in conv_out.read_text(encoding="utf-8")

    root_conv_out = tmp_path / "root_conv.json"
    r_cv_root = runner.invoke(main, ["--compact", "--out", str(root_conv_out), "trade", "convert", "--text", raw_item, "--to", "cold", "--json"])
    assert r_cv_root.exit_code == 0, r_cv_root.output
    rc_txt = root_conv_out.read_text(encoding="utf-8").strip()
    assert "\n" not in rc_txt and json.loads(rc_txt)["target"] == "cold"

    # [P1-4] bench list & bench suggest honor --compact and --out
    bl_out = tmp_path / "bench" / "list.json"
    r_bl = runner.invoke(main, ["bench", "list", "--slot", "gloves", "--limit", "3", "--json", "--compact", "--out", str(bl_out)])
    assert r_bl.exit_code == 0, r_bl.output
    bl_txt = bl_out.read_text(encoding="utf-8").strip()
    assert "\n" not in bl_txt and json.loads(bl_txt)["count"] > 0

    bs_out = tmp_path / "bench" / "suggest.md"
    r_bs = runner.invoke(main, ["--out", str(bs_out), "bench", "suggest", "--slot", "gloves", "--stat", "冰霜抗性"])
    assert r_bs.exit_code == 0, r_bs.output
    assert "Best bench craft" in bs_out.read_text(encoding="utf-8")


def test_carbon_p2_sweep_dict_scenario_none_deltas_tuples_query_alignment_and_load_lazy_httpx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    import pob_cli.cli as cli_mod
    import pob_cli.external_data as ext_mod
    import pob_cli.game_data as gd_mod
    import pob_cli.poedb_client as pc_mod
    from pob_cli.formatters import (
        format_matrix_markdown,
        format_tree_markdown,
        sanitize_for_output,
        to_json_str,
    )

    # [P2-1] pob sweep preserves dict scenario & format_sweep_markdown renders name/base
    captured_sweep: list[dict[str, Any]] = []

    def _fake_sweep(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        assert action == "sweep"
        captured_sweep.append(dict(params or {}))
        return {
            "scenario": (params or {}).get("scenario"),
            "baseline": {"name": "Base", "summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 500_000, "totalEhp": 50_000}},
            "variants": [
                {"name": "Partial", "ok": True, "summary": {"primaryDps": None, "totalEhp": None}},
            ],
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_sweep)
    sweep_file = tmp_path / "sweep.json"
    sc_dict = {"name": "Wither15", "base": "Pinnacle", "config": {"multiplierWitheredStackCount": 15}}
    sweep_file.write_text(json.dumps({"scenario": sc_dict, "variants": [{"name": "Partial", "ops": []}]}), encoding="utf-8")

    runner = CliRunner()
    r_sw = runner.invoke(main, ["sweep", "--file", str(sweep_file)])
    assert r_sw.exit_code == 0, r_sw.output
    assert captured_sweep[0]["scenario"] == sc_dict
    assert "Scenario: `Wither15`" in r_sw.output
    assert "-100.0%" not in r_sw.output

    # [P2-3] Matrix & sweep None guard, points=None guard, tuple sanitize/JSON
    md_mat = format_matrix_markdown(
        {
            "baseline": "Original",
            "scenarios": [
                {"name": "Original", "summary": {"primaryDps": 1_000_000, "totalEhp": 50_000}},
                {"name": "Partial", "summary": {"primaryDps": None, "totalEhp": None}},
            ],
        }
    )
    assert "-100.0%" not in md_mat

    md_tree = format_tree_markdown({"points": None, "keystones": None})
    assert "0/0" in md_tree

    assert sanitize_for_output(("^xFF0000Red", "^7White")) == ["Red", "White"]
    assert json.loads(to_json_str({"vals": (float("inf"), float("nan"), "^7ok")}, compact=True)) == {
        "vals": ["inf", None, "ok"]
    }

    # [#41 P1-6] Stripped query lookup & blank-arg alignment in cmd_data, cmd_price, cmd_wiki, cmd_poedb
    monkeypatch.setattr(
        gd_mod,
        "query_local_data_multi",
        lambda kind, qs, **_kw: {
            "kind": kind,
            "queries": [q.strip() for q in qs if q.strip()],
            "results": {q.strip(): {"kind": kind, "count": 1, "results": [{"name": q.strip()}]} for q in qs if q.strip()},
        },
        raising=False,
    )
    r_data = runner.invoke(main, ["data", "gem", " Fireball ", "Arc"])
    assert r_data.exit_code == 0, r_data.output
    assert "`Fireball` — `1` hits" in r_data.output
    assert "`Arc` — `1` hits" in r_data.output

    monkeypatch.setattr(
        ext_mod,
        "query_price_multi",
        lambda qs, **_kw: {
            "queries": ["Divine Orb", "Mageblood"],
            "results": [
                {"league": "Test", "totalMatches": 1, "matches": [{"name": "Divine Orb", "type": "Currency", "chaosValue": 150}]},
                {"league": "Test", "totalMatches": 1, "matches": [{"name": "Mageblood", "type": "Unique", "chaosValue": 30000}]},
            ],
        },
        raising=False,
    )
    r_pr = runner.invoke(main, ["price", "   ", " Divine Orb ", "Mageblood"])
    assert r_pr.exit_code == 0, r_pr.output
    assert "`Divine Orb`" in r_pr.output and "`Mageblood`" in r_pr.output

    monkeypatch.setattr(
        ext_mod,
        "query_wiki_multi",
        lambda qs, **_kw: {
            "queries": ["Archmage", "Mind over Matter"],
            "results": [
                {"title": "Archmage", "url": "https://poewiki.net/wiki/Archmage", "summary": "Archmage info"},
                {"title": "Mind over Matter", "url": "https://poewiki.net/wiki/MoM", "summary": "MoM info"},
            ],
        },
        raising=False,
    )
    r_wk = runner.invoke(main, ["wiki", "   ", " Archmage ", "Mind over Matter"])
    assert r_wk.exit_code == 0, r_wk.output
    assert "Archmage info" in r_wk.output and "MoM info" in r_wk.output

    monkeypatch.setattr(
        pc_mod,
        "query_poedb_multi",
        lambda qs, **_kw: {
            "queries": ["Archmage", "Indigon"],
            "results": [
                {"title": "Archmage", "slug": "Archmage", "url": "https://poedb.tw/Archmage"},
                {"title": "Indigon", "slug": "Indigon", "url": "https://poedb.tw/Indigon"},
            ],
        },
        raising=False,
    )
    r_pd = runner.invoke(main, ["poedb", "   ", " Archmage ", "Indigon"])
    assert r_pd.exit_code == 0, r_pd.output
    assert "PoEDB: Archmage" in r_pd.output and "PoEDB: Indigon" in r_pd.output

    # Client micro-win: _prepare_load_source on URL/Base64 does not eagerly import httpx
    code = (
        "import sys; from pob_cli.cli import _prepare_load_source; "
        "assert _prepare_load_source('https://pobb.in/abc') == ('https://pobb.in/abc', None); "
        "assert _prepare_load_source('eNrtBase64Code') == ('eNrtBase64Code', None); "
        "assert 'httpx' not in sys.modules, 'httpx eagerly imported!'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_trade_search_weighted_sum_cli_and_pseudo_sum_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    captured_queries: list[dict[str, Any]] = []

    class _SumClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            captured_queries.append(query)
            return {"id": "qsum", "total": 2, "result": ["r_low", "r_high"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [
                {
                    "id": "r_low",
                    "item": {
                        "name": "Low Sum",
                        "typeLine": "Slink Gloves",
                        "rarity": "Rare",
                        "frameType": 2,
                        "ilvl": 82,
                        "pseudoMods": ["Sum: 55"],
                        "explicitMods": [
                            {"description": "+70 to maximum Life"},
                            {"description": "+30% to Fire Resistance"},
                            {"description": "+25% to Cold Resistance"},
                        ],
                    },
                    "listing": {"price": {"amount": 5, "currency": "chaos"}},
                },
                {
                    "id": "r_high",
                    "item": {
                        "name": "High Sum",
                        "typeLine": "Slink Gloves",
                        "rarity": "Rare",
                        "frameType": 2,
                        "ilvl": 84,
                        "pseudoMods": ["Sum: 88"],
                        "explicitMods": [
                            {"description": "+70 to maximum Life"},
                            {"description": "+45% to Fire Resistance"},
                            {"description": "+43% to Cold Resistance"},
                        ],
                    },
                    "listing": {"price": {"amount": 20, "currency": "chaos"}},
                },
            ]

    monkeypatch.setattr(cli_trade, "TradeClient", _SumClient)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "gloves",
            "--stat",
            "+# to maximum Life=60",
            "--sum",
            "+#% to Fire Resistance|+#% to Cold Resistance>=50",
            "--search-sort",
            "sum",
            "--sort",
            "sum",
            "--pob",
            "off",
            "--no-compare",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured_queries[0]["sort"] == {"statgroup.0": "desc"}
    assert captured_queries[0]["query"]["stats"][0]["type"] == "weight"
    data = json.loads(res.output)
    assert [c["name"] for c in data["candidates"]] == ["High Sum (Slink Gloves)", "Low Sum (Slink Gloves)"]
    assert [c["weighted_sum"] for c in data["candidates"]] == [88.0, 55.0]





