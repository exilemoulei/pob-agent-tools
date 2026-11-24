from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

import pob_cli.cli as cli_mod
from pob_cli.cli import main
from pob_cli.cli_weights import (
    DEFAULT_PROBES,
    build_weight_variants,
    compute_weights,
    format_weights_markdown,
)


def test_build_weight_variants_appends_custom_mod() -> None:
    v = build_weight_variants(["+50 to maximum Life"])
    assert v == [{"name": "+50 to maximum Life", "ops": [{"op": "config", "appendCustomMods": "+50 to maximum Life"}]}]


def test_compute_weights_sorts_and_keeps_unverified_rows() -> None:
    res = {
        "baseline": {"summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000, "totalEhp": 10000}},
        "variants": [
            {"name": "life", "ok": True, "summary": {"primaryDps": 1000, "totalEhp": 11000}},
            {"name": "dmg", "ok": True, "summary": {"primaryDps": 1100, "totalEhp": 10000}},
            {"name": "bad", "ok": False, "error": "boom"},
            {"name": "nodps", "ok": True, "summary": {"primaryDps": None, "totalEhp": 10000}},
        ],
    }
    w = compute_weights(res)
    assert [r["mod"] for r in w["weights"]] == ["dmg", "life", "nodps", "bad"]
    assert w["weights"][0]["dpsDelta"] == 100 and abs(w["weights"][0]["dpsPct"] - 10.0) < 1e-9
    assert w["weights"][1]["ehpPct"] == 10.0
    assert w["weights"][2]["dpsDelta"] is None  # unverified stays None, never 0
    assert w["weights"][3]["error"] == "boom"


def test_format_weights_keeps_table_rows_intact() -> None:
    w = {"weights": [{"mod": "a|b", "ok": False, "error": "lua error: x|y\nstack traceback:\n\t[C]: in ?"}]}
    md = format_weights_markdown(w, target="main")
    row = next(ln for ln in md.splitlines() if ln.startswith("| a"))
    assert row == "| a\\|b | error: lua error: x\\|y | - |"
    assert "stack traceback" not in md


def test_cmd_weights_calls_sweep_with_probes(monkeypatch: Any) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        return {
            "baseline": {"summary": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000, "totalEhp": 5000}},
            "variants": [{"name": v["name"], "ok": True, "summary": {"primaryDps": 1050, "totalEhp": 5000}} for v in params["variants"]],
            "restoreCheck": {"verified": True},
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    runner = CliRunner()

    r = runner.invoke(main, ["weights", "--json"])
    assert r.exit_code == 0, r.output
    action, params = captured[-1]
    assert action == "sweep" and len(params["variants"]) == len(DEFAULT_PROBES)
    assert json.loads(r.output)["weights"][0]["dpsPct"] == 5.0

    r2 = runner.invoke(main, ["weights", "--mod", "+1 to Level of all Skill Gems", "--scenario", "Pinnacle"])
    assert r2.exit_code == 0, r2.output
    assert captured[-1][1]["scenario"] == "Pinnacle"
    assert [v["name"] for v in captured[-1][1]["variants"]] == ["+1 to Level of all Skill Gems"]
    assert "+50 (+5.00%)" in r2.output


def test_cmd_weights_preset_builds_config_variants(monkeypatch: Any) -> None:
    from pob_cli.cli_weights import build_preset_variants

    assert build_preset_variants("bandit")[1] == {"name": "bandit=Alira", "ops": [{"op": "config", "set": {"bandit": "Alira"}}]}
    captured: list[dict[str, Any]] = []

    def _fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append(dict(params or {}))
        return {"baseline": {"summary": {"primaryDps": 1000, "totalEhp": 5000}},
                "variants": [{"name": v["name"], "ok": True, "summary": {"primaryDps": 1000, "totalEhp": 5000}} for v in params["variants"]]}

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    r = CliRunner().invoke(main, ["weights", "--preset", "pantheon-major"])
    assert r.exit_code == 0, r.output
    assert [v["name"] for v in captured[-1]["variants"]][1] == "pantheonMajorGod=TheBrineKing"
    r2 = CliRunner().invoke(main, ["weights", "--preset", "bandit", "--mod", "+1 Life"])
    assert r2.exit_code == 1 and len(captured) == 1


def test_cmd_weights_jewels_preset_unequips_each_jewel(monkeypatch: Any) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    items = {"equippedSlots": [
        {"slotName": "Helmet", "itemId": 1, "nodeId": None, "item": {"name": "Hat"}},
        {"slotName": "Jewel 26725", "itemId": 2, "nodeId": 26725, "item": {"name": "Gloom Eye"}},
        {"slotName": "Jewel 2491", "itemId": None, "nodeId": 2491},
        {"slotName": "Belt Abyssal Socket 1", "itemId": 3, "nodeId": None, "item": {"name": "Ghastly Eye"}},
    ]}

    def _fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        if action == "items":
            return items
        return {"baseline": {"summary": {"primaryDps": 1000, "totalEhp": 5000}},
                "variants": [{"name": v["name"], "ok": True, "summary": {"primaryDps": 900, "totalEhp": 5000}} for v in params["variants"]]}

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_call)
    r = CliRunner().invoke(main, ["weights", "--preset", "jewels", "--json"])
    assert r.exit_code == 0, r.output
    ops = [v["ops"][0] for v in captured[-1][1]["variants"]]
    assert ops == [{"op": "unequip", "slot": "Jewel 26725"}, {"op": "unequip", "slot": "Belt Abyssal Socket 1"}]
    assert json.loads(r.output)["weights"][0]["dpsPct"] == -10.0
