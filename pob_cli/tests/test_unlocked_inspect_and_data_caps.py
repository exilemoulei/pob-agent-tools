"""Unit tests for unlocked inspect/data/research caps, daemon (slot, rev) read cache, and fast diagnose."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import pob_cli.analyzer as analyzer_mod
import pob_cli.daemon as daemon_mod
from pob_cli import cli, cli_data, cli_data_render, cli_research, external_data, poedb_client
from pob_cli.analyzer.calcs import search_calcs_breakdown
from pob_cli.analyzer.tree_inspect import inspect_tree
from pob_cli.cli_inspect_parts import _run_tree_inspect
from pob_cli.daemon.read_cache import LOOP_GUARD_WARNING, compute_source_hash
from pob_cli.external_data.meta_wiki import _extract_wiki_sections_and_summary
from pob_cli.game_data.queries_core import _query_mods, _query_uniques
from pob_cli.game_data.sqlite_lock import _create_schema


def _crlf_bytes(path: Path) -> int:
    raw = path.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    return max(len(raw), len(text.encode("utf-8")) + len(text.splitlines()))


def test_cli_research_split_lazy_wiring_and_budgets() -> None:
    for cmd_name in ("poedb", "wiki", "research"):
        assert cli._LAZY_SUBCOMMANDS[cmd_name][0] == ".cli_research"

    assert cli_data_render.run_dual_research is cli_research.run_dual_research
    assert cli_data_render._render_research_markdown is cli_research._render_research_markdown
    assert cli_data_render._render_single_wiki_markdown is cli_research._render_single_wiki_markdown
    assert cli_data.cmd_poedb is cli_research.cmd_poedb
    assert cli_data.cmd_wiki is cli_research.cmd_wiki
    assert cli_data.cmd_research is cli_research.cmd_research

    pkg_dir = Path(cli.__file__).resolve().parent
    for fname in ("cli_data.py", "cli_data_render.py", "cli_research.py"):
        assert _crlf_bytes(pkg_dir / fname) <= 18_000, f"{fname} exceeded 18,000B CRLF"
    assert _crlf_bytes(pkg_dir / "analyzer" / "tree_inspect.py") < 23_500


def test_inspect_tree_default_100_and_unlimited_zero_scales_hover_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes_db = {
        str(1000 + i): {
            "id": 1000 + i,
            "name": f"Mana Cluster {i}",
            "type": "Notable" if i % 2 == 0 else "Normal",
            "stats": ["+10 to maximum Mana"],
            "x": float(i),
            "y": 0.0,
        }
        for i in range(260)
    }
    hovered_ids: list[int] = []

    class _FakeTreeWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def get_cached_tree_data(self, _tv: str | None) -> dict[str, Any]:
            return {"nodes": nodes_db}

        def call_batch(self, calls: list[tuple[str, dict[str, Any]]], **_: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for method, params in calls:
                if method == "get_tree_state":
                    out.append({"treeVersion": "3_30", "allocatedNodes": [1000], "overrides": {}, "dynamicNodes": [], "sockets": []})
                elif method == "list_items":
                    out.append({"slots": []})
                elif method == "node_hover":
                    nid = int(params["id"])
                    hovered_ids.append(nid)
                    out.append({"ok": True, "cost": 2, "path": [1000, nid]})
            return out

    w = _FakeTreeWorker()

    res_default = inspect_tree(w, search="Mana")  # type: ignore[arg-type]
    assert res_default["totalMatches"] == 260
    assert len(res_default["matches"]) == 100
    assert len(hovered_ids) == 200  # max(limit * 2, 100) = 200

    hovered_ids.clear()
    res_all = inspect_tree(w, search="Mana", limit=0, show_path=True)  # type: ignore[arg-type]
    assert res_all["totalMatches"] == 260
    assert len(res_all["matches"]) == 260
    assert len(hovered_ids) == 260  # limit <= 0 hovers all candidates
    assert all(m.get("cost") == 2 and m.get("path") for m in res_all["matches"])

    daemon_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        cli,
        "_daemon_call",
        lambda action, params=None, **_: (daemon_calls.append((action, dict(params or {}))) or {"totalMatches": 0, "matches": []}),
    )
    _run_tree_inspect(
        searches=("Mana",),
        node_types=(),
        allocated=None,
        max_cost=None,
        show_path=False,
        masteries=False,
        socket_radius=None,
        target="main",
        as_json=True,
        compact=True,
        out_path=None,
        limit=0,
    )
    assert daemon_calls[-1][0] == "tree"
    assert daemon_calls[-1][1]["limit"] == 0


def test_calc_max_breakdowns_default_30_and_zero_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "ri": i,
            "label": f"Mana Stat {i}",
            "labelZh": f"魔力属性 {i}",
            "cells": [{"ci": 1, "raw": str(i * 10), "text": str(i * 10), "hasBreakdown": True}],
        }
        for i in range(1, 46)
    ]
    bd_requested: list[int] = []

    class _FakeCalcWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.caps = {"translateToggle": False}

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            if method == "get_calcs":
                return {"sections": [{"si": 1, "id": "Mana", "group": "Defence", "subsections": [{"ui": 1, "label": "Mana", "rows": rows}]}]}
            return {}

        def call_batch(self, calls: list[tuple[str, dict[str, Any]]], **_: Any) -> list[dict[str, Any]]:
            for m, p in calls:
                if m == "calcs_breakdown":
                    bd_requested.append(int(p["ri"]))
            return [{"ok": True, "sections": [{"type": "text", "lines": ["base = 100"]}]} for _ in calls]

    w = _FakeCalcWorker()

    res_def = search_calcs_breakdown(w, "Mana Stat", limit=45)  # type: ignore[arg-type]
    assert len(res_def["matches"]) == 45
    assert len(bd_requested) == 30
    assert sum(1 for m in res_def["matches"] if "breakdown" in m["cells"][0]) == 30

    bd_requested.clear()
    res_unlim = search_calcs_breakdown(w, "Mana Stat", limit=0, max_breakdowns=0)  # type: ignore[arg-type]
    assert len(res_unlim["matches"]) == 45
    assert len(bd_requested) == 45
    assert sum(1 for m in res_unlim["matches"] if "breakdown" in m["cells"][0]) == 45

    seen_rpc: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "_daemon_call",
        lambda action, params=None, **_: (seen_rpc.append(dict(params or {})) or {"totalMatches": 0, "matches": []}),
    )
    runner = CliRunner()
    r = runner.invoke(cli.main, ["calc", "Mana", "--limit", "0", "--max-breakdowns", "0", "--json"])
    assert r.exit_code == 0, r.output
    assert seen_rpc[-1]["limit"] == 0
    assert seen_rpc[-1]["max_breakdowns"] == 0


def test_wiki_poedb_research_unlocked_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)

    mech_body = "M" * 5000
    extra_body = "E" * 11000
    raw_html = (
        f"<p>{'L' * 2200}</p>"
        f"<h2>Skill functions and interactions</h2><p>{mech_body}</p>"
        f"<h2>Acquisition</h2><p>{extra_body}</p>"
    )

    sum_def, secs_def = _extract_wiki_sections_and_summary(raw_html)
    assert 3500 < len(sum_def) <= 12000
    assert len(secs_def[0]["text"]) == 5000  # proportional section cap (~6171) preserves full 5000-char mechanics!

    sum_all, secs_all = _extract_wiki_sections_and_summary(raw_html, max_len=0)
    assert len(sum_all) > 17000
    assert extra_body in sum_all
    assert len(secs_all[0]["text"]) == 5000

    class _FakeHttpClient:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def get(self, _url: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
            p = params or {}
            if p.get("action") == "opensearch":
                return type(
                    "R",
                    (),
                    {
                        "raise_for_status": lambda self: None,
                        "json": lambda self: ["Arc", ["Arc"], [""], ["https://www.poewiki.net/wiki/Arc"]],
                    },
                )()
            return type(
                "R",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {"parse": {"title": "Arc", "text": {"*": raw_html}}},
                },
            )()

    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)

    runner = CliRunner()
    r_def = runner.invoke(cli.main, ["wiki", "Arc", "--json"])
    assert r_def.exit_code == 0, r_def.output
    assert len(json.loads(r_def.stdout)["summary"]) == 12000

    # Calling with --max-chars 0 must NOT return the truncated 12000-char cache entry
    r_unlim = runner.invoke(cli.main, ["wiki", "Arc", "--max-chars", "0", "--json"])
    assert r_unlim.exit_code == 0, r_unlim.output
    assert len(json.loads(r_unlim.stdout)["summary"]) > 17000

    # PoEDB: default limit 150, properties & versionHistory capped at 100 (or all when limit=0)
    props_html = "".join(f'<div class="property">Prop {i}: {i}</div>' for i in range(135))
    vh_rows = "".join(f"<tr><td>3.{i}</td><td><ul><li>Change {i}</li></ul></td></tr>" for i in range(135))
    poedb_html = f"<h1>TestBase</h1>{props_html}<h2>Version history</h2><table>{vh_rows}</table>"
    entries = [{"slug": "TestBase", "cnLabel": "测试基底", "usLabel": "TestBase", "cnDesc": "Wands", "usDesc": "Wands"}]
    monkeypatch.setattr(poedb_client.client, "_fetch_poedb_page", lambda slug, **_: ("https://poedb.tw/cn/TestBase", poedb_html))

    pdb_150 = poedb_client.client._query_poedb_with_entries("TestBase", entries)
    assert len(pdb_150["properties"]) == 100
    assert len(pdb_150["versionHistory"]) == 100

    pdb_all = poedb_client.client._query_poedb_with_entries("TestBase", entries, limit_mods=0)
    assert len(pdb_all["properties"]) == 135
    assert len(pdb_all["versionHistory"]) == 135

    seen_research: dict[str, Any] = {}
    monkeypatch.setattr(
        poedb_client,
        "query_poedb",
        lambda q, **kw: (seen_research.update({"poedb_kw": kw}) or {"query": q, "slug": q, "title": q, "url": "-"}),
    )
    monkeypatch.setattr(
        external_data,
        "query_wiki",
        lambda q, **kw: (seen_research.update({"wiki_kw": kw}) or {"query": q, "title": q, "url": "-", "summary": "ok"}),
    )
    r_res = runner.invoke(cli.main, ["research", "Arc", "--limit", "0", "--max-chars", "0", "--json"])
    assert r_res.exit_code == 0, r_res.output
    assert seen_research["poedb_kw"]["limit_mods"] == 0
    assert seen_research["wiki_kw"]["max_chars"] == 0


def test_sqlite_candidate_pool_exceeds_old_800_ceiling() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _create_schema(conn)
        mod_rows = []
        for i in range(1200):
            is_exact = i >= 1195
            stat_en = "maximum life" if is_exact else f"minion maximum life variant {i}"
            mid = f"ExactLife{i}" if is_exact else f"FillerMod{i:04d}"
            obj = {"id": mid, "source": "Explicit", "level": 80, "stats": [stat_en], "stats_zh": [], "weights": {"default": 1000}}
            mod_rows.append(
                (
                    mid,
                    "Explicit",
                    "Prefix",
                    "LifeGroup",
                    "lifegroup",
                    mid,
                    mid.lower(),
                    80,
                    stat_en,
                    "",
                    '{"default":1000}',
                    "[]",
                    "[]",
                    stat_en,
                    "",
                    stat_en,
                    json.dumps(obj),
                )
            )
        conn.executemany("INSERT INTO mods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mod_rows)
        conn.execute("INSERT INTO mods_fts(mods_fts) VALUES('rebuild')")

        uniq_rows = []
        for i in range(1100):
            is_exact = i >= 1095
            nm = f"ExactLifeUnique{i}" if is_exact else f"Broad Life Relic {i:04d}"
            obj = {"name": nm, "base_type": "Belt", "implicits": [], "explicits": ["+50 to maximum Life"]}
            uniq_rows.append(
                (nm, nm.lower(), None, None, None, None, "Belt", "Belt", "belt", 0, f"{nm.lower()} maximum life", json.dumps(obj))
            )
        conn.executemany("INSERT INTO uniques VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", uniq_rows)
        conn.execute("INSERT INTO uniques_fts(uniques_fts) VALUES('rebuild')")

        # Under old LIMIT 800 / LIMIT 600, rows >= 800 were dropped before Python ranking!
        top_mods = _query_mods(conn, "maximum life", None, None, 10)
        assert any(m["id"].startswith("ExactLife") for m in top_mods)

        all_mods = _query_mods(conn, "maximum life", None, None, 0)
        assert len(all_mods) == 1200

        top_uniques = _query_uniques(conn, "exactlifeunique", None, None, 10)
        assert len(top_uniques) == 5

        all_uniques = _query_uniques(conn, "", None, None, 0, mod_filter="maximum life")
        assert len(all_uniques) == 1100
    finally:
        conn.close()


class _StubDaemonWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.alive = True
        self.last_used = time.time()
        self.last_good_code: str | None = None
        self.hello_info: dict[str, Any] = {"pobVersion": "2.68.1"}
        self.lock = threading.RLock()

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def ensure_alive(self) -> None:
        self.alive = True

    def call(self, _method: str, _params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {"ok": True}


def test_daemon_read_cache_loop_guard_and_idempotent_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubDaemonWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    load_calls: list[str] = []
    summary_calls = 0
    diag_calls = 0

    def _fake_load(w: Any, *, source: str | None = None, xml_code: str | None = None, **_: Any) -> dict[str, Any]:
        from pob_cli.snapshot import history_of

        code = str(xml_code or source or "")
        load_calls.append(code)
        history_of(w).load_build(code, "test")
        return {"code": code, "importNotes": ["Loaded cleanly"]}

    def _fake_summary(_w: Any) -> dict[str, Any]:
        nonlocal summary_calls
        summary_calls += 1
        return {"buildInfo": {"level": 95}, "offence": {"CombinedDPS": 1_000_000}, "defence": {"TotalEHP": 50_000}}

    def _fake_diagnose(_w: Any, **kw: Any) -> dict[str, Any]:
        nonlocal diag_calls
        diag_calls += 1
        return {
            "env": {"game": "poe1", "patch": "3.30", "slot": kw.get("target", "main")},
            "buildInfo": {"level": 95},
            "noRankSeen": kw.get("no_rank"),
        }

    import pob_cli.snapshot as snapshot_mod

    monkeypatch.setattr(snapshot_mod, "load_build", _fake_load)
    monkeypatch.setattr(analyzer_mod, "extract_summary", _fake_summary)
    monkeypatch.setattr(analyzer_mod, "diagnose_build", _fake_diagnose)

    # 1. Explicit load sets rev=1 and sourceHash
    ld = mgr.dispatch("load", {"target": "main", "xml_code": "BUILD_CODE_ALPHA"})
    assert ld["rev"] == 1
    assert ld["sourceHash"] == compute_source_hash({"xml_code": "BUILD_CODE_ALPHA"})
    assert len(load_calls) == 1

    # 2. Summary read cache + 3rd-call soft loopGuardWarning (<2ms)
    s1 = mgr.dispatch("summary", {"target": "main"})
    assert summary_calls == 1
    assert "cached" not in s1
    assert "loopGuardWarning" not in s1

    t0 = time.perf_counter()
    s2 = mgr.dispatch("summary", {"target": "main"})
    hit_ms = (time.perf_counter() - t0) * 1000.0
    assert summary_calls == 1
    assert s2["cached"] is True
    assert "loopGuardWarning" not in s2
    assert hit_ms < 20.0

    s3 = mgr.dispatch("summary", {"target": "main"})
    assert summary_calls == 1
    assert s3["cached"] is True
    assert s3["loopGuardWarning"] == LOOP_GUARD_WARNING

    # 3. Idempotent diagnose <source> with identical content hash skips load_build and preserves rev=1!
    d1 = mgr.dispatch("diagnose", {"target": "main", "source": "BUILD_CODE_ALPHA", "no_rank": True, "section": "summary"})
    assert len(load_calls) == 1  # NOT re-imported!
    assert d1["rev"] == 1
    assert diag_calls == 1
    assert d1["importNotes"] == ["Loaded cleanly"]

    # Different --section values hit the same (slot, rev) compute cache WITHOUT triggering loopGuardWarning!
    d2 = mgr.dispatch("diagnose", {"target": "main", "xml_code": "BUILD_CODE_ALPHA", "no_rank": True, "section": "gaps"})
    d3 = mgr.dispatch("diagnose", {"target": "main", "no_rank": True, "section": "items"})
    assert diag_calls == 1
    assert d2["cached"] is True and "loopGuardWarning" not in d2
    assert d3["cached"] is True and "loopGuardWarning" not in d3

    # Repeating the identical section 3 times DOES trigger loopGuardWarning
    d_g2 = mgr.dispatch("diagnose", {"target": "main", "no_rank": True, "section": "gaps"})
    d_g3 = mgr.dispatch("diagnose", {"target": "main", "no_rank": True, "section": "gaps"})
    assert "loopGuardWarning" not in d_g2
    assert d_g3["loopGuardWarning"] == LOOP_GUARD_WARNING

    # 4. Diagnose with a NEW build source re-imports and bumps rev=2 (invalidating rev=1 cache)
    d_new = mgr.dispatch("diagnose", {"target": "main", "xml_code": "BUILD_CODE_BETA", "no_rank": True})
    assert len(load_calls) == 2
    assert d_new["rev"] == 2
    assert diag_calls == 2
    assert "cached" not in d_new


def test_cli_diagnose_rank_defaults_sections_and_loop_guard_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake_res: dict[str, Any] = {
        "env": {"game": "poe1", "patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "slot": "main", "rev": 1},
        "buildInfo": {"level": 95, "className": "Marauder", "ascendClassName": "Chieftain", "points": {"used": 118, "usedMax": 118}},
        "mainGroup": {"index": 1, "displayLabel": "Righteous Fire", "slot": "Body Armour", "gems": []},
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_500_000, "totalEhp": 90_000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_200_000, "totalEhp": 80_000},
        "healthGaps": [{"severity": "HIGH", "category": "Resistance", "message": "Chaos Resist uncapped"}],
        "equippedItems": [{"slot": "Belt", "name": "Mageblood", "rarity": "UNIQUE", "isFlask": False}],
        "skillGroups": [{"index": 1, "isMain": True, "slot": "Body Armour", "gems": [{"name": "Righteous Fire", "level": 21, "quality": 20}]}],
        "factCards": {"gems": [{"name": "Righteous Fire", "isSupport": False}], "uniques": []},
        "advisories": [],
        "topNodes": {"stat": "CombinedDPS", "maxDist": 5, "byPerPoint": [{"id": 111, "name": "Arsonist", "pathDist": 2, "pathPower": 3.5, "power": 7.0}]},
        "topGems": {"dpsField": "CombinedDPS", "mode": "add_link", "candidates": []},
    }

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        assert action == "diagnose"
        calls.append(dict(params or {}))
        return dict(fake_res)

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon)
    runner = CliRunner()

    # Plain `pob diagnose` defaults to no_rank=True (<1s fast mode)
    r_plain = runner.invoke(cli.main, ["diagnose"])
    assert r_plain.exit_code == 0, r_plain.output
    assert calls[-1]["no_rank"] is True

    # `--rank`, explicit `--top`, or `--section upgrades` enables ranking (no_rank=False)
    assert runner.invoke(cli.main, ["diagnose", "--rank"]).exit_code == 0
    assert calls[-1]["no_rank"] is False

    assert runner.invoke(cli.main, ["diagnose", "--top", "3"]).exit_code == 0
    assert calls[-1]["no_rank"] is False and calls[-1]["top"] == 3

    r_upg = runner.invoke(cli.main, ["diagnose", "--section", "upgrades"])
    assert r_upg.exit_code == 0, r_upg.output
    assert calls[-1]["no_rank"] is False
    assert "#### 4. Top ROI Upgrades" in r_upg.output
    assert "#### 2. Hard Defensive" not in r_upg.output

    r_gaps_json = runner.invoke(cli.main, ["diagnose", "--section", "gaps", "--json"])
    assert r_gaps_json.exit_code == 0
    gaps_payload = json.loads(r_gaps_json.stdout)
    assert gaps_payload["section"] == "gaps"
    assert "healthGaps" in gaps_payload and "equippedItems" not in gaps_payload

    # Surface loopGuardWarning on stderr and Markdown header while keeping exit_code == 0
    fake_res["loopGuardWarning"] = LOOP_GUARD_WARNING
    r_loop = runner.invoke(cli.main, ["diagnose", "--section", "summary"])
    assert r_loop.exit_code == 0
    assert LOOP_GUARD_WARNING in r_loop.stderr
    assert f"> {LOOP_GUARD_WARNING}" in r_loop.stdout
