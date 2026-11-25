"""Offline unit tests for unlocked trade fetch caps, `--surrogate-k`, unclamped `item inspect --limit`,
`cli_mutate_skills` module split, and `--allow-legacy` / `--allow-illegal-level` mutator escape flags."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli, cli_inspect_parts, cli_mutate_skills, cli_trade, item_inspect, mutators
from pob_cli.cli import _LAZY_SUBCOMMANDS, main
from pob_cli.daemon import rpc_handlers, rpc_mutate
from pob_cli.errors import LegacyPatchTrapError
from pob_cli.trade_client import MAX_FETCH_IDS, MAX_FETCH_MANY, TradeClient
from pob_cli.trade_client.utils import MAX_FETCH_MANY as UTILS_MAX_FETCH_MANY

MOCK_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
]


class _FakeSkillWorker:
    """Minimal headless bridge worker double for skill mutators and batch_ops."""

    def __init__(self, *, existing_gem_name: str = "Empower") -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._groups: list[dict[str, Any]] = [
            {
                "index": 1,
                "label": "Main",
                "enabled": True,
                "gems": [
                    {
                        "index": 1,
                        "name": existing_gem_name,
                        "nameSpec": existing_gem_name,
                        "level": 3,
                        "quality": 20,
                        "enabled": True,
                    }
                ],
            }
        ]

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "export_code":
            return {"code": "FAKE_BASE64_CODE"}
        if method == "import_code":
            return {"ok": True}
        if method == "get_build_info":
            return {"className": "Witch", "ascendClassName": "Elementalist", "level": 95, "mainSocketGroup": 1}
        if method == "get_stats":
            return {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0, "Life": 4_000.0}}
        if method == "list_warnings":
            return {"warnings": []}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": [dict(g, gems=list(g["gems"])) for g in self._groups]}
        if method == "list_items":
            return {"slots": [{"name": "Body Armour", "selItemId": 1}], "items": [{"id": 1, "raw": "Rarity: RARE\nCoat\nVaal Regalia"}]}
        if method == "gem_search":
            q = str(p.get("query") or "")
            return {"gems": [{"gemId": f"Metadata/Items/Gems/{q}", "name": q, "support": True}]}
        if method == "add_group":
            new_idx = len(self._groups) + 1
            self._groups.append({"index": new_idx, "label": p.get("label") or "", "enabled": True, "gems": []})
            return {"index": new_idx}
        if method == "set_group":
            return {"ok": True}
        if method == "add_gem":
            grp = next(g for g in self._groups if g["index"] == p["group"])
            gem_idx = len(grp["gems"]) + 1
            gem_obj = {
                "index": gem_idx,
                "name": p.get("nameSpec") or "Gem",
                "nameSpec": p.get("nameSpec") or "Gem",
                "level": p.get("level", 20),
                "quality": p.get("quality", 20),
                "enabled": p.get("enabled", True),
            }
            grp["gems"].append(gem_obj)
            return {"gem": gem_obj}
        if method == "set_gem":
            return {"gem": {"index": p.get("index", 1), "level": p.get("level", 20)}}
        if method == "delete_gem":
            return {"ok": True}
        return {"ok": True}


# ---------------------------------------------------------------------------
# 1. `cli_mutate_skills` split & `pob tree --limit` forwarding
# ---------------------------------------------------------------------------


def test_cli_mutate_skills_lazy_registration_and_tree_limit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _LAZY_SUBCOMMANDS["skill"][0] == ".cli_mutate_skills"
    assert _LAZY_SUBCOMMANDS["skill"][1] == "skill"
    assert cli_mutate_skills.skill.name == "skill"
    assert set(cli_mutate_skills.skill.commands) >= {"set", "add", "rm", "new-group", "replace-group"}

    runner = CliRunner()
    help_res = runner.invoke(main, ["skill", "--help"])
    assert help_res.exit_code == 0, help_res.output
    assert "replace-group" in help_res.output

    captured_tree: dict[str, Any] = {}

    def _fake_tree_inspect(*, limit: int = 100, **kwargs: Any) -> None:
        captured_tree["limit"] = limit
        captured_tree.update(kwargs)

    monkeypatch.setattr(cli_inspect_parts, "_run_tree_inspect", _fake_tree_inspect)
    tree_res = runner.invoke(main, ["tree", "--search", "Life", "--limit", "0"])
    assert tree_res.exit_code == 0, tree_res.output
    assert captured_tree["limit"] == 0


# ---------------------------------------------------------------------------
# 2. `MAX_FETCH_MANY == 100` & chunked fetching >20 IDs with rate limiter intact
# ---------------------------------------------------------------------------


def test_max_fetch_many_100_chunks_over_20_ids_with_rate_limiter(tmp_path: Path) -> None:
    assert MAX_FETCH_IDS == 10
    assert MAX_FETCH_MANY == 100
    assert UTILS_MAX_FETCH_MANY == 100

    seen_chunks: list[list[str]] = []
    sleeps: list[float] = []
    clock = {"t": 1_700_000_000.0}

    def _now() -> float:
        return clock["t"]

    def _sleep(sec: float) -> None:
        sleeps.append(sec)
        clock["t"] += sec

    def _handler(request: httpx.Request) -> httpx.Response:
        ids_part = request.url.path.split("/api/trade/fetch/")[1]
        batch = ids_part.split(",")
        seen_chunks.append(batch)
        hit_num = len(seen_chunks)
        # 5 hits per 10s with limit_margin=1 -> effective limit 4 -> 5th chunk triggers rate-limit sleep
        state_hits = ((hit_num - 1) % 4) + 1
        return httpx.Response(
            200,
            json={"result": [{"id": item_id} for item_id in batch]},
            headers={
                "X-Rate-Limit-Policy": "trade-fetch-request-limit",
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "5:10:60",
                "X-Rate-Limit-Ip-State": f"{state_hits}:10:0",
            },
        )

    client = TradeClient(
        cache_dir=tmp_path,
        sleep=_sleep,
        now=_now,
        transport=httpx.MockTransport(_handler),
    )

    ids_45 = [f"item_{i}" for i in range(45)]
    rows_45 = client.fetch_many(ids_45, "q_big", limit=45)
    assert [len(c) for c in seen_chunks] == [10, 10, 10, 10, 5]
    assert [r["id"] for r in rows_45] == ids_45
    assert len(sleeps) == 1  # Rate limiter respected across chunk #4 -> #5!

    seen_chunks.clear()
    clock["t"] += 30.0
    ids_130 = [f"x_{i}" for i in range(130)]
    rows_100 = client.fetch_many(ids_130, "q_cap", limit=130)
    assert [len(c) for c in seen_chunks] == [10] * 10
    assert len(rows_100) == 100


# ---------------------------------------------------------------------------
# 3. `pob trade search --surrogate-k` end-to-end wiring
# ---------------------------------------------------------------------------


def test_trade_search_surrogate_k_forwards_prefilter_k_and_raises_default_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: (MOCK_STATS, "mock"))
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Keepers")

    all_rows = {
        f"id_{i}": {
            "id": f"id_{i}",
            "item": {
                "name": f"Ring {i}",
                "typeLine": "Ruby Ring",
                "baseType": "Ruby Ring",
                "frameType": 2,
                "ilvl": 85,
                "identified": True,
                "explicitMods": [f"+{20 + i} to maximum Life"],
            },
            "listing": {"account": {"name": f"Seller#{i}"}, "price": {"amount": 10 + i, "currency": "chaos"}},
        }
        for i in range(70)
    }
    fetch_limits_seen: list[int] = []
    daemon_eval_calls: list[dict[str, Any]] = []

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q_sur", "total": len(all_rows), "result": list(all_rows)}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            fetch_limits_seen.append(limit)
            cap = min(limit, MAX_FETCH_MANY)
            return [all_rows[k] for k in ids[:cap]]

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        if action == "items":
            return {"raw": "Rarity: RARE\nOld\nRuby Ring\nImplicits: 0\n+30 to maximum Life"}
        if action == "trade_eval_items":
            daemon_eval_calls.append(p)
            items = list(p.get("items") or [])
            pk = p.get("prefilter_k")
            eff_k = max(50, int(pk)) if pk is not None else len(items)
            # Simulate surrogate keeping the highest-index (highest life) eff_k items
            kept = items[-eff_k:]
            return {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": 5000.0, "life": 4000.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "after": {
                            "primaryDpsKey": "CombinedDPS",
                            "primaryDps": 1000.0 + float(entry["id"]) * 10.0,
                            "totalEhp": 5100.0,
                            "life": 4050.0,
                        },
                        "diff": {},
                    }
                    for entry in kept
                ],
            }
        raise AssertionError(f"Unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    runner = CliRunner()
    # --surrogate-k 60 with default --fetch-limit raises fetch pool to 60 and succeeds under --pob on
    res_60 = runner.invoke(
        main,
        [
            "trade", "search", "--slot", "ring1", "--stat", "maximum Life=20",
            "--surrogate-k", "60", "--fetch-limit", "65", "--pob", "on", "--sort", "pob", "--json", "--compact",
        ],
    )
    assert res_60.exit_code == 0, res_60.output
    assert fetch_limits_seen[-1] == 65
    assert daemon_eval_calls[-1]["prefilter_k"] == 60
    assert len(daemon_eval_calls[-1]["items"]) == 65
    payload_60 = json.loads(res_60.stdout)
    assert payload_60["fetched"] == 65
    assert payload_60["candidates"][0]["pob"]["dps_after"] > 1500.0

    # --surrogate-k 30 without --fetch-limit raises fetch_limit to 50 (Rule P3 floor)
    res_30 = runner.invoke(
        main,
        ["trade", "search", "--slot", "ring1", "--stat", "maximum Life=20", "--surrogate-k", "30", "--pob", "on", "--json"],
    )
    assert res_30.exit_code == 0, res_30.output
    assert fetch_limits_seen[-1] == 50
    assert daemon_eval_calls[-1]["prefilter_k"] == 30

    # --surrogate-k 0 disables prefilter (keeps default fetch_limit=10 and omits prefilter_k)
    res_0 = runner.invoke(
        main,
        ["trade", "search", "--slot", "ring1", "--stat", "maximum Life=20", "--surrogate-k", "0", "--pob", "on", "--json"],
    )
    assert res_0.exit_code == 0, res_0.output
    assert fetch_limits_seen[-1] == 10
    assert "prefilter_k" not in daemon_eval_calls[-1]

    # Daemon _h_trade_eval_items forwards prefilter_k to trade_eval.eval_trade_items
    seen_eval_kw: dict[str, Any] = {}
    from pob_cli import trade_eval as te_mod

    monkeypatch.setattr(te_mod, "eval_trade_items", lambda _w, **kw: seen_eval_kw.update(kw) or {"results": []})
    rpc_handlers._h_trade_eval_items(SimpleNamespace(), {"slot": "Ring 1", "items": [], "prefilter_k": 55}, object())  # type: ignore[arg-type]
    assert seen_eval_kw["prefilter_k"] == 55


# ---------------------------------------------------------------------------
# 4. `pob item inspect --limit 50` (unclamped `min=1`)
# ---------------------------------------------------------------------------


def test_item_inspect_limit_unclamped_above_20(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_limit: list[int] = []

    def _fake_inspect(raw_text: str, *, slot: str | None = None, wants: Any = (), limit: int = 8) -> dict[str, Any]:
        captured_limit.append(limit)
        return {"slot": slot or "Ring 1", "limit": limit, "affixes": [], "suggestions": []}

    monkeypatch.setattr(item_inspect, "inspect_item", _fake_inspect)
    raw_ring = "Rarity: RARE\nTest Band\nRuby Ring\nItem Level: 85\nImplicits: 0\n+70 to maximum Life"

    runner = CliRunner()
    ok_res = runner.invoke(main, ["item", "inspect", "--raw", raw_ring, "--slot", "Ring 1", "--limit", "50", "--json"])
    assert ok_res.exit_code == 0, ok_res.output
    assert captured_limit == [50]
    assert json.loads(ok_res.output)["limit"] == 50

    bad_zero = runner.invoke(main, ["item", "inspect", "--raw", raw_ring, "--slot", "Ring 1", "--limit", "0"])
    assert bad_zero.exit_code == 2


# ---------------------------------------------------------------------------
# 5. `--allow-legacy` & `--allow-illegal-level` (CLI + daemon + `batch_ops`)
# ---------------------------------------------------------------------------


def test_cli_skill_allow_legacy_and_allow_illegal_level(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        daemon_calls.append((action, dict(params or {})))
        return {
            "mode": "WHAT-IF",
            "detail": {"effectiveLevel": (params or {}).get("level") or 20},
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000, "totalEhp": 5000, "life": 3000, "points": {}},
            "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1200, "totalEhp": 5000, "life": 3000, "points": {}},
            "diff": {"deltas": []},
        }

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    # Without --allow-legacy: still hard-blocked with exit 1
    blocked = runner.invoke(main, ["skill", "add", "--gem", "Divine Blessing", "--json"])
    assert blocked.exit_code == 1
    assert "legacy_patch_trap" in blocked.output

    # With --allow-legacy on skill add, set, new-group, replace-group: succeeds and emits [POE1-PATCH-WARN]
    res_add = runner.invoke(main, ["skill", "add", "--gem", "Divine Blessing", "--allow-legacy"])
    assert res_add.exit_code == 0, res_add.output
    assert "[POE1-PATCH-WARN]" in res_add.output
    assert daemon_calls[-1] == (
        "skill_add",
        {"target": "main", "group": "main", "gem": "Divine Blessing", "level": None, "quality": 20, "apply": False, "allow_legacy": True},
    )

    res_set = runner.invoke(
        main,
        ["skill", "set", "--gem", "1", "--new-gem", "Ancestral Protector", "--level", "10", "--allow-legacy", "--allow-illegal-level"],
    )
    assert res_set.exit_code == 0, res_set.output
    assert "[POE1-PATCH-WARN]" in res_set.output
    assert daemon_calls[-1][1]["allow_legacy"] is True
    assert daemon_calls[-1][1]["allow_illegal_level"] is True

    res_ng = runner.invoke(
        main,
        ["skill", "new-group", "--gem", "Divine Blessing:20:20", "--gem", "Empower:10:20", "--allow-legacy", "--allow-illegal-level"],
    )
    assert res_ng.exit_code == 0, res_ng.output
    assert "[POE1-PATCH-WARN]" in res_ng.output
    assert daemon_calls[-1][0] == "skill_new_group"
    assert daemon_calls[-1][1]["allow_legacy"] is True
    assert daemon_calls[-1][1]["allow_illegal_level"] is True

    res_rg = runner.invoke(
        main,
        ["skill", "replace-group", "--group", "main", "--gem", "Anomalous Spark:20:20", "--allow-legacy", "--allow-illegal-level"],
    )
    assert res_rg.exit_code == 0, res_rg.output
    assert "[POE1-PATCH-WARN]" in res_rg.output
    assert daemon_calls[-1][0] == "skill_replace_group"
    assert daemon_calls[-1][1]["allow_legacy"] is True
    assert daemon_calls[-1][1]["allow_illegal_level"] is True


def test_mutators_and_batch_ops_allow_legacy_and_illegal_level() -> None:
    # 1. _validate_gem_level direct check
    with pytest.raises(ValueError, match="Exceptional Support"):
        mutators._validate_gem_level("Empower", 10)
    lv, cautions = mutators._validate_gem_level("Empower", 10, allow_illegal_level=True)
    assert lv == 10
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in cautions)

    # 2. Direct mutator + daemon rpc_mutate forwarding
    w = _FakeSkillWorker(existing_gem_name="Empower")
    with pytest.raises(LegacyPatchTrapError):
        mutators.add_skill_gem(w, group_selector="1", gem_name="Divine Blessing")
    with pytest.raises(ValueError, match="Exceptional Support"):
        mutators.modify_skill(w, group_selector="1", gem_selector=1, level=10)

    mgr = SimpleNamespace()
    add_res = rpc_mutate._h_skill_add(
        mgr,  # type: ignore[arg-type]
        {"group": "1", "gem": "Divine Blessing", "allowLegacy": True},
        w,  # type: ignore[arg-type]
    )
    assert any("[POE1-PATCH-WARN]" in c for c in add_res.get("cautions", []))

    set_res = rpc_mutate._h_skill_set(
        mgr,  # type: ignore[arg-type]
        {"group": "1", "gem": 1, "level": 10, "allowIllegalLevel": True},
        w,  # type: ignore[arg-type]
    )
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in set_res.get("cautions", []))
    set_gem_calls = [p for m, p in w.calls if m == "set_gem"]
    assert set_gem_calls[-1]["level"] == 10

    # 3. batch_ops: default blocks legacy & illegal level; allowLegacy / allowIllegalLevel unlocks all skill ops
    w_batch = _FakeSkillWorker(existing_gem_name="Greater Spell Echo")
    with pytest.raises(LegacyPatchTrapError):
        mutators.run_batch(w_batch, [{"op": "skill_add", "group": 1, "gem": "Divine Blessing"}])
    with pytest.raises(ValueError, match="Exceptional Support"):
        mutators.run_batch(w_batch, [{"op": "skill_set", "group": 1, "gem": 1, "level": 20}])

    batch_res = mutators.run_batch(
        w_batch,
        [
            {"op": "skill_add", "group": 1, "gem": "Divine Blessing", "allowLegacy": True},
            {"op": "skill_add", "group": 1, "gem": "Empower", "level": 8, "allowIllegalLevel": True},
            {"op": "skill_set", "group": 1, "gem": 1, "level": 20, "allow_illegal_level": True},
            {
                "op": "add_group",
                "label": "Theory",
                "gems": ["Ancestral Protector:20:20", "Awakened Empower:10:20"],
                "allowLegacy": True,
                "allowIllegalLevel": True,
            },
            {
                "op": "replace_group",
                "group": 1,
                "gems": [{"name": "Phantasmal Unearth", "level": 21}, {"name": "Enlighten", "level": 10}],
                "allow_legacy": True,
                "allow_illegal_level": True,
            },
        ],
    )
    steps = batch_res["detail"]["steps"]
    assert len(steps) == 5
    assert any("[POE1-PATCH-WARN]" in c for c in steps[0]["cautions"])
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in steps[1]["cautions"])
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in steps[2]["cautions"])
    assert any("[POE1-PATCH-WARN]" in c for c in steps[3]["cautions"])
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in steps[3]["cautions"])
    assert any("[POE1-PATCH-WARN]" in c for c in steps[4]["cautions"])
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in steps[4]["cautions"])
