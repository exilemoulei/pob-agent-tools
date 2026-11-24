"""Offline mocked tests for Layer 1 web research upgrades (`pob wiki` ZH->EN & mechanics, `pob research`)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli, external_data, poedb_client
from pob_cli.cli_data_render import (
    _render_research_markdown,
    _render_single_wiki_markdown,
    run_dual_research,
)
from pob_cli.external_data.meta_wiki import (
    _extract_wiki_sections_and_summary,
    _resolve_wiki_english_query,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    monkeypatch.delenv("POB_ROOT", raising=False)
    return tmp_path


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data: Any = None) -> None:
        self.status_code = status_code
        self._text = text
        self._json = json_data

    @property
    def text(self) -> str:
        if self._text:
            return self._text
        if self._json is not None:
            return json.dumps(self._json)
        return ""

    def json(self) -> Any:
        if self._json is not None:
            return self._json
        return json.loads(self._text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://example.invalid")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=req, response=resp)


# ---------------------------------------------------------------------------
# 1. query_wiki: Auto ZH->EN fallback
# ---------------------------------------------------------------------------


def test_query_wiki_zh_to_en_slang_fallback_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_searches: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _FakeResponse:
            params = params or {}
            action = params.get("action")
            if action == "opensearch":
                s = str(params.get("search") or "")
                seen_searches.append(s)
                if s == "Inspiration":
                    return _FakeResponse(
                        json_data=[s, ["Inspiration Support"], [""], ["https://www.poewiki.net/wiki/Inspiration_Support"]]
                    )
                return _FakeResponse(json_data=[s, [], [], []])
            if action == "parse":
                return _FakeResponse(
                    json_data={
                        "parse": {
                            "title": "Inspiration Support",
                            "text": {
                                "*": (
                                    "<p>Inspiration Support reduces mana cost and grants Inspiration Charges.</p>"
                                    "<h2><span class='mw-headline'>Skill functions and interactions</span>"
                                    "<span class='mw-editsection'>[edit | edit source]</span></h2>"
                                    "<p>Totems and traps spending mana do not grant Inspiration Charges to the player.</p>"
                                )
                            },
                        }
                    }
                )
            raise AssertionError(f"Unexpected params: {params}")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    res = external_data.query_wiki("启迪")
    assert res["query"] == "启迪"
    assert res["resolvedQuery"] == "Inspiration"
    assert res["title"] == "Inspiration Support"
    assert seen_searches == ["Inspiration"]
    assert res["mechanicsSections"][0]["heading"] == "Skill functions and interactions"
    assert "Totems and traps" in res["mechanicsSections"][0]["text"]

    # Cache file is keyed by original normalized query
    slug_hash = hashlib.sha1("启迪".encode()).hexdigest()[:12]
    assert (tmp_path / "cache" / f"poewiki_{slug_hash}.json").exists()

    # Second call hits disk cache without HTTP
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should use disk cache")),
    )
    cached_res = external_data.query_wiki("启迪")
    assert cached_res["resolvedQuery"] == "Inspiration"
    assert cached_res["title"] == "Inspiration Support"


def test_query_wiki_zh_translate_fallback_and_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pob_cli.game_data.queries as gd_queries

    # 0. Priority 1: local cached PoEDB autocomplete index exact CN->US match (zero translation, zero hallucination)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "poedb_autocomplete_index.json").write_text(
        json.dumps(
            {
                "_ts": 9999999999,
                "entries": [
                    {"slug": "Returning_Projectiles_Support", "cnLabel": "投射物归返（辅）", "usLabel": "Returning Projectiles Support"},
                    {"slug": "Titanium_Spirit_Shield", "cnLabel": "巨人魔盾", "usLabel": "Titanium Spirit Shield"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert _resolve_wiki_english_query("投射物归返") == "Returning Projectiles Support"
    assert _resolve_wiki_english_query("投射物归返（辅）") == "Returning Projectiles Support"
    assert _resolve_wiki_english_query("巨人魔盾") == "Titanium Spirit Shield"

    # 1. Not in PoEDB cache or slang map -> resolves via exact-only query_local_data("translate", ...)
    def fake_local_data(kind: str, query: str = "", **_: Any) -> dict[str, Any]:
        if kind == "translate" and query == "寒霜爆":
            return {"results": [{"en": "Frost Bomb", "zh": "寒霜爆", "match_type": "exact"}]}
        return {"results": []}

    monkeypatch.setattr(gd_queries, "query_local_data", fake_local_data)
    assert _resolve_wiki_english_query("寒霜爆") == "Frost Bomb"
    assert _resolve_wiki_english_query("Frost Bomb") is None  # non-CJK skipped

    # 2. Fail-open when game_data SQLite isn't bootstrapped
    def broken_local_data(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise FileNotFoundError("gamedata.sqlite missing")

    monkeypatch.setattr(gd_queries, "query_local_data", broken_local_data)
    assert _resolve_wiki_english_query("未知技能名测试") is None

    # 3. Translated EN yields no titles -> falls back to original topic without false resolvedQuery
    monkeypatch.setattr(
        "pob_cli.external_data.meta_wiki._resolve_wiki_english_query",
        lambda topic: "NonExistentEnglishTitle",
    )
    seen: list[str] = []

    class FallbackClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FallbackClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _FakeResponse:
            params = params or {}
            action = params.get("action")
            if action == "opensearch":
                s = str(params.get("search") or "")
                seen.append(s)
                if s == "特殊词条":
                    return _FakeResponse(json_data=[s, ["Special Page"], [""], ["https://www.poewiki.net/wiki/Special_Page"]])
                return _FakeResponse(json_data=[s, [], [], []])
            if action == "query":
                return _FakeResponse(json_data={"query": {"search": []}})
            if action == "parse":
                return _FakeResponse(json_data={"parse": {"title": "Special Page", "text": {"*": "<p>Fallback text</p>"}}})
            raise AssertionError(f"Unexpected params: {params}")

    monkeypatch.setattr(httpx, "Client", FallbackClient)
    res = external_data.query_wiki("特殊词条")
    assert seen == ["NonExistentEnglishTitle", "特殊词条"]
    assert res["title"] == "Special Page"
    assert "resolvedQuery" not in res


# ---------------------------------------------------------------------------
# 2. Mechanics & Interactions section extraction (anti-table crowding)
# ---------------------------------------------------------------------------


def test_extract_wiki_sections_prioritizes_mechanics_over_gem_table() -> None:
    huge_table = "<table>" + "".join(f"<tr><td>Level {i}</td><td>{i * 100} damage</td></tr>" for i in range(1, 250)) + "</table>"
    raw_html = (
        f"<p>Returning Projectiles Support makes projectiles return to the user after reaching the end of their flight. {'A' * 1200}</p>"
        f"<h2><span class='mw-headline'>Gem level progression</span></h2>{huge_table}"
        "<h2><span class='mw-headline'>Version history</span></h2><p>3.22.0: Introduced Returning Projectiles Support.</p>"
        "<h2><span class='mw-headline'>Skill functions and interactions</span>"
        "<span class='mw-editsection'>[edit | edit source]</span></h2>"
        "<p>Pierce, Fork, and Chain occur in strict order before Return. Returning projectiles can hit enemies again only for skills that allow it.</p>"
        "<h3><span class='mw-headline'>Trigger</span></h3><p>Triggered spells still obey return order.</p>"
    )

    summary, sections = _extract_wiki_sections_and_summary(raw_html)
    assert len(summary) <= 3500
    headings = [s["heading"] for s in sections]
    assert headings[0] == "Skill functions and interactions"
    assert "Trigger" in headings
    assert "Version history" in headings
    # Tier ordering: Skill functions (tier 0) -> Trigger (tier 1) -> Version history (tier 2)
    assert headings.index("Skill functions and interactions") < headings.index("Trigger") < headings.index("Version history")
    # Lead intro is capped <= 900 chars and Mechanics text is NOT crowded out by the huge Lv1-20 table
    assert "Pierce, Fork, and Chain occur in strict order before Return." in summary
    assert "Level 240" not in summary


def test_extract_wiki_sections_plain_page_has_no_mechanics_sections() -> None:
    summary, sections = _extract_wiki_sections_and_summary("<p>Simple wiki stub about Chaos Damage.</p>")
    assert summary == "Simple wiki stub about Chaos Damage."
    assert sections == []


def test_render_single_wiki_markdown_shows_resolved_query_and_mechanics() -> None:
    md = _render_single_wiki_markdown(
        {
            "query": "冰矛",
            "resolvedQuery": "Ice Spear",
            "title": "Ice Spear",
            "url": "https://www.poewiki.net/wiki/Ice_Spear",
            "summary": "Ice Spear fires icy projectiles.\n\n## Skill functions and interactions\nSecond form gains +300% crit chance.",
            "mechanicsSections": [
                {"heading": "Skill functions and interactions", "text": "Second form gains +300% crit chance."}
            ],
        },
        "冰矛",
    )
    assert "- **Resolved Query**: `冰矛` -> `Ice Spear`" in md
    assert "#### Mechanics & Interactions" in md
    assert "##### Skill functions and interactions" in md
    # Lead appears once and section text is not duplicated
    assert md.count("Second form gains +300% crit chance.") == 1


# ---------------------------------------------------------------------------
# 3. run_dual_research + partial-success resilience + CLI `pob research`
# ---------------------------------------------------------------------------


def test_run_dual_research_concurrent_and_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    poedb_calls: list[dict[str, Any]] = []
    wiki_calls: list[dict[str, Any]] = []

    def fake_poedb(q: str, *, include_mods: bool = False, lang: str = "cn", proxy: str | None = None, limit_mods: int = 60) -> dict[str, Any]:
        poedb_calls.append({"q": q, "include_mods": include_mods, "lang": lang, "proxy": proxy, "limit_mods": limit_mods})
        if q == "BrokenPoedb":
            raise RuntimeError("PoEDB timed out")
        return {
            "query": q,
            "slug": q.replace(" ", "_"),
            "title": f"PoEDB {q}",
            "url": f"https://poedb.tw/{lang}/{q.replace(' ', '_')}",
            "properties": ["Armour: 100"],
        }

    def fake_wiki(q: str, *, proxy: str | None = None, **_: Any) -> dict[str, Any]:
        wiki_calls.append({"q": q, "proxy": proxy})
        if q == "BrokenWiki":
            raise ValueError("No poewiki.net page found")
        return {
            "query": q,
            "title": f"Wiki {q}",
            "url": f"https://www.poewiki.net/wiki/{q.replace(' ', '_')}",
            "summary": f"Mechanics of {q}.",
        }

    monkeypatch.setattr(poedb_client, "query_poedb", fake_poedb)
    monkeypatch.setattr(external_data, "query_wiki", fake_wiki)

    res = run_dual_research(
        ["BrokenPoedb", "BrokenWiki"],
        include_mods=True,
        lang="us",
        proxy="http://127.0.0.1:7891",
        limit_mods=25,
    )
    assert res["queries"] == ["BrokenPoedb", "BrokenWiki"]
    assert res["count"] == 2
    assert res["includeMods"] is True
    assert res["lang"] == "us"

    # Order preserved + partial-success resilience on both sides
    r0, r1 = res["results"]
    assert r0["query"] == "BrokenPoedb"
    assert r0["okSources"] == ["wiki"]
    assert "timed out" in r0["errors"]["poedb"]
    assert r0["wiki"]["title"] == "Wiki BrokenPoedb"

    assert r1["query"] == "BrokenWiki"
    assert r1["okSources"] == ["poedb"]
    assert "No poewiki.net" in r1["errors"]["wiki"]
    assert r1["poedb"]["title"] == "PoEDB BrokenWiki"

    assert all(c["include_mods"] is True and c["lang"] == "us" and c["limit_mods"] == 25 and c["proxy"] == "http://127.0.0.1:7891" for c in poedb_calls)
    assert all(c["proxy"] == "http://127.0.0.1:7891" for c in wiki_calls)

    md = _render_research_markdown(res)
    assert "## Dual-Source Research: `BrokenPoedb`" in md
    assert "> [PARTIAL] Succeeded: wiki | Errors: poedb: PoEDB timed out" in md
    assert "Mechanics of BrokenPoedb." in md
    assert "> [PARTIAL] Succeeded: poedb | Errors: wiki: No poewiki.net page found" in md
    assert "PoEDB BrokenWiki" in md

    with pytest.raises(ValueError, match="cannot be empty"):
        run_dual_research(["   "])


def test_cli_research_json_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        poedb_client,
        "query_poedb",
        lambda q, **kw: {
            "query": q,
            "slug": "Twilight_Regalia",
            "title": "暮光法衣",
            "url": "https://poedb.tw/cn/Twilight_Regalia",
            "properties": ["Energy Shield: 320"],
        },
    )
    monkeypatch.setattr(
        external_data,
        "query_wiki",
        lambda q, **kw: {
            "query": q,
            "resolvedQuery": "Twilight Regalia",
            "title": "Twilight Regalia",
            "url": "https://www.poewiki.net/wiki/Twilight_Regalia",
            "summary": "High ES body armour.",
        },
    )

    runner = CliRunner()
    res_json = runner.invoke(cli.main, ["research", "暮光法衣", "--mods", "--limit", "30", "--json"])
    assert res_json.exit_code == 0, res_json.output
    payload = json.loads(res_json.output)
    assert payload["count"] == 1
    assert payload["includeMods"] is True
    assert payload["results"][0]["okSources"] == ["poedb", "wiki"]
    assert payload["results"][0]["wiki"]["resolvedQuery"] == "Twilight Regalia"

    res_md = runner.invoke(cli.main, ["research", "暮光法衣"])
    assert res_md.exit_code == 0, res_md.output
    assert "## Dual-Source Research: `暮光法衣`" in res_md.output
    assert "### PoEDB: 暮光法衣" in res_md.output
    assert "- **Resolved Query**: `暮光法衣` -> `Twilight Regalia`" in res_md.output

    res_empty = runner.invoke(cli.main, ["research", "   "])
    assert res_empty.exit_code != 0


def test_run_dual_research_reraises_guardrail_hard_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.errors import CrossGameHallucinationError, LegacyPatchTrapError

    def raise_cross_game(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise CrossGameHallucinationError(
            trigger="Spirit",
            error="PoE2 term blocked",
            remediation="Use pob2",
        )

    def raise_legacy(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise LegacyPatchTrapError(
            trigger="Awakened Elemental Focus",
            patch_version="3.28",
            error="Legacy patch trap",
            remediation="Use Elemental Focus",
        )

    monkeypatch.setattr(poedb_client, "query_poedb", raise_cross_game)
    monkeypatch.setattr(external_data, "query_wiki", lambda q, **kw: {"query": q, "title": q, "url": "-", "summary": "ok"})
    with pytest.raises(CrossGameHallucinationError, match="PoE2 term blocked"):
        run_dual_research(["Spirit"])

    monkeypatch.setattr(poedb_client, "query_poedb", lambda q, **kw: {"query": q, "slug": q, "title": q, "url": "-"})
    monkeypatch.setattr(external_data, "query_wiki", raise_legacy)
    with pytest.raises(LegacyPatchTrapError, match="Legacy patch trap"):
        run_dual_research(["Awakened Elemental Focus"])


def test_long_nl_research_query_auto_splits_without_breaking_short_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))
    (cache_dir / "poedb_autocomplete_index.json").write_text(
        json.dumps(
            {
                "_ts": 9_999_999_999,
                "entries": [
                    {"slug": "Twilight_Regalia", "cnLabel": "暮光法衣", "usLabel": "Twilight Regalia", "usDesc": "Body Armours"},
                    {"slug": "Titanium_Spirit_Shield", "cnLabel": "巨人魔盾", "usLabel": "Titanium Spirit Shield", "usDesc": "Shields"},
                    {"slug": "Returning_Projectiles_Support", "cnLabel": "投射物归返（辅）", "usLabel": "Returning Projectiles Support", "usDesc": "Support Gems"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    seen_poedb: list[str] = []
    monkeypatch.setattr(
        poedb_client,
        "query_poedb",
        lambda q, **kw: (seen_poedb.append(q) or {"query": q, "slug": q.replace(" ", "_"), "title": q, "url": "-"}),
    )
    monkeypatch.setattr(
        external_data,
        "query_wiki",
        lambda q, **kw: {"query": q, "title": q, "url": "-", "summary": "ok"},
    )

    res = run_dual_research(
        ["Twilight Regalia Titanium Spirit Shield base energy shield values patch 3.25 3.26 3.30 changes"]
    )
    assert res["queries"] == ["Twilight Regalia", "Titanium Spirit Shield"]
    assert res["autoSplits"]
    assert seen_poedb == ["Twilight Regalia", "Titanium Spirit Shield"]
    md = _render_research_markdown(res)
    assert "[AUTO-SPLIT]" in md

    # Long official 6-word skill gem name must NOT be split
    seen_poedb.clear()
    res_exact = run_dual_research(["Cast On Critical Strike Support"])
    assert res_exact["queries"] == ["Cast On Critical Strike Support"]
    assert "autoSplits" not in res_exact


def test_poedb_modsview_prioritizes_normal_prefix_suffix_and_supports_filter() -> None:
    from pob_cli.poedb_client.html_parser import _extract_mods_from_modsview

    # Construct 30 special/influence buckets + 8 core normal prefix/suffix families
    mv: dict[str, Any] = {
        "normal": [
            {"ModGenerationTypeID": "1", "ModFamilyList": ["IncreasedEnergyShield"], "Level": "86", "Name": "Unassailable", "str": "(101-110)% increased Energy Shield", "DropChance": "1000"},
            {"ModGenerationTypeID": "1", "ModFamilyList": ["FlatEnergyShield"], "Level": "75", "Name": "Resplendent", "str": "+(101-110) to maximum Energy Shield", "DropChance": "1000"},
            {"ModGenerationTypeID": "1", "ModFamilyList": ["HybridES"], "Level": "78", "Name": "Seraphim's", "str": "(39-42)% increased Energy Shield / +(16-17)% Stun Recovery", "DropChance": "1000"},
            {"ModGenerationTypeID": "2", "ModFamilyList": ["Intelligence"], "Level": "82", "Name": "of the Genius", "str": "+(51-55) to Intelligence", "DropChance": "1000"},
        ],
    }
    for bkt in ("corrupted", "searing", "eater", "delve", "shaper", "elder", "crusader", "redeemer", "hunter", "warlord", "veiled", "essence", "synthesis"):
        mv[bkt] = [
            {"ModGenerationTypeID": "1", "ModFamilyList": [f"{bkt}_fam_{i}"], "Level": "85", "Name": f"{bkt}_{i}", "str": f"+{i} special {bkt} stat", "DropChance": "500"}
            for i in range(4)
        ]

    # Default limit=16 must place ALL normal prefix/suffix families FIRST
    default_mods, _, _ = _extract_mods_from_modsview(mv, limit_mods=16)
    top4_sides = [m["side"] for m in default_mods[:4]]
    assert top4_sides.count("prefix") == 3 and top4_sides.count("suffix") == 1
    assert any("101-110" in m["text"] and m["level"] == 86 for m in default_mods[:4])

    # --filter supports bilingual CN alias ("能量护盾" -> matches English "Energy Shield")
    filtered, total_m, _ = _extract_mods_from_modsview(mv, limit_mods=10, mod_filter="能量护盾")
    assert total_m == 3
    assert all("energy shield" in m["text"].lower() for m in filtered)
    assert filtered[0]["level"] == 86
