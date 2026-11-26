"""Tests for P1 Phase 1: `pathofexile.tw` realm config, manual `zh-TW` prompt, TW names & leagues."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_lang, cli_trade
from pob_cli.cli import main
from pob_cli.cli_trade.fresh_bargain import _fetch_with_fresh_and_market
from pob_cli.locale_cfg import (
    TW_REALM_CONFIRM_PROMPT,
    current_realm,
    get_active_realm,
    set_active_lang,
    set_active_realm,
    trade_base_url_for_realm,
)
from pob_cli.trade_client import (
    TradeClient,
    clear_tw_name_cache,
    localize_query_for_tw,
    seller_url,
    to_tw_trade_name,
)
from pob_cli.trade_query import StatPlan, build_query
from pob_cli.trade_query.auto_query import apply_cli_filters_to_auto_query


def _make_tw_dict_engine(root: Path) -> Path:
    (root / "PoE1" / "Data").mkdir(parents=True)
    cn = root / "Data" / "poe1" / "zh-rCN"
    tw = root / "Data" / "poe1" / "zh-rTW"
    cn.mkdir(parents=True)
    tw.mkdir(parents=True)

    (cn / "meta.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (cn / "uniques.json").write_text(
        json.dumps({"entries": {"The Ivory Tower": "象牙之塔", "Ephemeral Edge": "影殒", "Mageblood": "法师之血"}}),
        encoding="utf-8",
    )
    (tw / "meta.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (tw / "uniques.json").write_text(
        json.dumps({"entries": {"The Ivory Tower": "象牙塔", "Ephemeral Edge": "影殞", "Mageblood": "魔血"}}),
        encoding="utf-8",
    )
    (tw / "items.json").write_text(
        json.dumps({"entries": {"Saint's Hauberk": "聖者鏈甲", "Dusk Blade": "暮色之刃"}}),
        encoding="utf-8",
    )
    (tw / "gems.json").write_text(
        json.dumps({"entries": {"Enlighten Support": "啟蒙輔助"}}),
        encoding="utf-8",
    )
    return root


def test_realm_config_precedence_and_no_auto_switch_on_zh_tw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.delenv("POB_REALM", raising=False)
    monkeypatch.delenv("POB_LANG", raising=False)
    monkeypatch.setattr(cli_lang, "_installed_locales", lambda _rt: ["zh-CN", "zh-TW", "en"])

    assert get_active_realm(rt) == ("global", "default")
    assert trade_base_url_for_realm("global") == "https://www.pathofexile.com"
    assert trade_base_url_for_realm("tw") == "https://pathofexile.tw"

    # Switching lang to zh-TW must NEVER auto-switch realm to tw
    set_active_lang("zh-TW", runtime_dir=rt)
    assert get_active_realm(rt) == ("global", "default")

    # Persisted config < POB_REALM env < CLI --realm
    set_active_realm("tw", runtime_dir=rt)
    assert get_active_realm(rt) == ("tw", "config")
    cfg = json.loads((rt / "config.json").read_text(encoding="utf-8"))
    assert cfg["lang"] == "zh-TW"
    assert cfg["realm"] == "tw"

    monkeypatch.setenv("POB_REALM", "global")
    assert get_active_realm(rt) == ("global", "env")
    assert get_active_realm(rt, cli_realm="tw") == ("tw", "cli")
    assert current_realm(rt, cli_realm="tw") == "tw"


def test_pob_lang_zh_tw_prompts_without_switching_realm_and_supports_realm_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.delenv("POB_REALM", raising=False)
    monkeypatch.delenv("POB_LANG", raising=False)
    monkeypatch.setattr(cli_lang, "_installed_locales", lambda _rt: ["zh-CN", "zh-TW", "en"])

    runner = CliRunner()
    res_tw = runner.invoke(main, ["lang", "zh-TW"])
    assert res_tw.exit_code == 0, res_tw.output
    assert "你玩的是台服吗？" in res_tw.output
    assert "pob lang --realm tw" in res_tw.output
    assert "Trade realm: `global`" in res_tw.output
    assert current_realm(rt) == "global"

    # JSON also surfaces the prompt and confirms realm stays global
    res_tw_json = runner.invoke(main, ["lang", "zh-TW", "--json"])
    assert res_tw_json.exit_code == 0, res_tw_json.output
    data = json.loads(res_tw_json.output)
    assert data["realm"] == "global"
    assert data["twRealmPrompt"] == TW_REALM_CONFIRM_PROMPT

    # Explicit confirmation switches realm to tw and updates config.json
    res_realm = runner.invoke(main, ["lang", "--realm", "tw"])
    assert res_realm.exit_code == 0, res_realm.output
    assert "Trade realm: `tw`" in res_realm.output
    assert "https://pathofexile.tw" in res_realm.output
    assert current_realm(rt) == "tw"

    # Re-running `pob lang zh-TW` once realm is already tw does not re-prompt
    res_tw2 = runner.invoke(main, ["lang", "zh-TW"])
    assert res_tw2.exit_code == 0
    assert "你玩的是台服吗？" not in res_tw2.output


def test_tw_name_mapping_and_query_localization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_tw_name_cache()

    assert to_tw_trade_name("The Ivory Tower", kind="unique", pob_dir=eng) == "象牙塔"
    assert to_tw_trade_name("Ephemeral Edge", kind="unique", pob_dir=eng) == "影殞"
    # Simplified Chinese bridged -> canonical EN -> official zh-TW
    assert to_tw_trade_name("象牙之塔", kind="unique", pob_dir=eng) == "象牙塔"
    assert to_tw_trade_name("法师之血", kind="unique", pob_dir=eng) == "魔血"
    # Base type and gem normalization
    assert to_tw_trade_name("Saint's Hauberk", kind="type", pob_dir=eng) == "聖者鏈甲"
    assert to_tw_trade_name("Enlighten", kind="type", pob_dir=eng) == "啟蒙輔助"

    raw_q = {
        "query": {
            "status": {"option": "available"},
            "name": "The Ivory Tower",
            "type": "Saint's Hauberk",
            "stats": [{"type": "and", "filters": [{"id": "explicit.stat_3299347043", "value": {"min": 80}}]}],
        },
        "sort": {"price": "asc"},
    }
    tw_q = localize_query_for_tw(raw_q, pob_dir=eng)
    assert tw_q["query"]["name"] == "象牙塔"
    assert tw_q["query"]["type"] == "聖者鏈甲"
    # Official stat IDs remain identical to Global
    assert tw_q["query"]["stats"][0]["filters"][0]["id"] == "explicit.stat_3299347043"


def test_tw_trade_client_leagues_urls_and_cache_ratelimit_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    clear_tw_name_cache()

    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        seen.append((request.method, str(request.url), body))
        if request.url.path.endswith("/leagues"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": "標準模式", "realm": "pc"},
                        {"id": "專家模式", "realm": "pc"},
                        {"id": "殘暴 巫影密會", "realm": "pc"},
                        {"id": "單人自力 巫影密會", "realm": "pc"},
                        {"id": "專家 巫影密會", "realm": "pc"},
                        {"id": "巫影密會", "realm": "pc"},
                    ]
                },
            )
        if request.url.path.endswith("/stats"):
            return httpx.Response(200, json={"result": [{"label": "Explicit", "entries": []}]})
        return httpx.Response(200, json={"id": "twQ123", "result": ["id1"], "total": 1})

    global_client = TradeClient(cache_dir=tmp_path, realm="global", transport=httpx.MockTransport(handler))
    tw_client = TradeClient(cache_dir=tmp_path, realm="tw", pob_dir=eng, transport=httpx.MockTransport(handler))

    assert tw_client.default_league() == "巫影密會"
    assert tw_client.normalize_league("Standard") == "標準模式"
    assert tw_client.normalize_league("Hardcore") == "專家模式"

    # Search maps name/type to zh-TW and posts to pathofexile.tw
    res = tw_client.search("巫影密會", {"query": {"name": "The Ivory Tower", "status": {"option": "available"}}})
    assert res["id"] == "twQ123"
    post_method, post_url, post_body = seen[-1]
    assert post_method == "POST"
    assert post_url.startswith("https://pathofexile.tw/api/trade/search/")
    assert post_body["query"]["name"] == "象牙塔"

    # URLs use pathofexile.tw with percent-encoded Chinese league
    s_url = tw_client.search_url("巫影密會", "twQ123")
    assert s_url.startswith("https://pathofexile.tw/trade/search/%E5%B7%AB%E5%BD%B1%E5%AF%86%E6%9C%83/twQ123")
    sel_url = seller_url("巫影密會", {"status": {"option": "available"}}, "ExileTW", realm="tw")
    assert sel_url.startswith("https://pathofexile.tw/trade/search/%E5%B7%AB%E5%BD%B1%E5%AF%86%E6%9C%83/")

    # Cache and rate-limit state files are isolated by realm
    tw_client.stats(force_refresh=True)
    global_client.stats(force_refresh=True)
    assert (tmp_path / "trade_stats_tw.json").is_file()
    assert (tmp_path / "trade_stats.json").is_file()
    assert tw_client.rate_limiter.state_path.name == "trade_ratelimit_tw.json"
    assert global_client.rate_limiter.state_path.name == "trade_ratelimit.json"


def test_cli_trade_search_dump_query_tw_realm_and_corrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_tw_name_cache()
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: ([], "mock_local"))

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "tw",
            "--league",
            "Standard",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["realm"] == "tw"
    assert data["league"] == "標準模式"
    q = data["query"]["query"]
    assert q["name"] == "象牙塔"
    assert q["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "true"}
    assert q["filters"]["misc_filters"]["filters"]["mirrored"] == {"option": "false"}


def test_cli_realm_global_overrides_tw_config_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    set_active_realm("tw", runtime_dir=rt)
    monkeypatch.setenv("POB_REALM", "tw")
    clear_tw_name_cache()
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: ([], "mock_local"))

    seen_realms: list[str] = []

    class _TrackRealmClient(TradeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen_realms.append(self.realm)

    monkeypatch.setattr(cli_trade, "TradeClient", _TrackRealmClient)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "global",
            "--slot",
            "body_armour",
            "--name",
            "The Ivory Tower",
            "--dump-query",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["realm"] == "global"
    assert payload["league"] == "Standard"
    assert seen_realms == ["global"]
    assert payload["query"]["query"]["name"] == "The Ivory Tower"


def test_auto_query_honors_name_type_corrupted_and_tw_localization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_tw_name_cache()
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: ([], "mock_local"))

    fake_auto_json = json.dumps(
        {
            "query": {
                "status": {"option": "available"},
                "stats": [{"type": "weight", "value": {"min": 10}, "filters": []}],
                "filters": {"misc_filters": {"filters": {"corrupted": {"option": "false"}}}},
            },
            "sort": {"statgroup.0": "desc"},
        }
    )
    monkeypatch.setattr(
        cli_trade,
        "_daemon_call_safe",
        lambda action, _params=None, **_kw: (
            {"queryJson": fake_auto_json, "filters": 0, "complexity": 62}
            if action == "trade_generate_query"
            else {}
        ),
    )

    runner = CliRunner()
    res_global = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--slot",
            "body_armour",
            "--auto-query",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--dump-query",
        ],
    )
    assert res_global.exit_code == 0, res_global.output
    d_global = json.loads(res_global.output)
    assert d_global["realm"] == "global"
    assert d_global["query"]["query"]["name"] == "The Ivory Tower"
    assert d_global["query"]["query"]["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "true"}

    res_tw = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "tw",
            "--slot",
            "body_armour",
            "--auto-query",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--dump-query",
        ],
    )
    assert res_tw.exit_code == 0, res_tw.output
    d_tw = json.loads(res_tw.output)
    assert d_tw["realm"] == "tw"
    assert d_tw["query"]["query"]["name"] == "象牙塔"
    assert d_tw["query"]["query"]["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "true"}

    # --corrupted any removes pre-existing corrupted filter and allow_corrupted=False never overrides it
    body = {
        "query": {
            "status": {"option": "available"},
            "filters": {"misc_filters": {"filters": {"corrupted": {"option": "false"}}}},
        },
        "sort": {"statgroup.0": "desc"},
    }
    apply_cli_filters_to_auto_query(body, corrupted="any", allow_corrupted=False)
    assert "misc_filters" not in body["query"]["filters"]


def test_corrupted_mirrored_decoupling_unmapped_ascii_warning_and_stale_fallback_tw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_dict_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_tw_name_cache()
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: ([], "mock_local"))

    # Mirrored excluded on --corrupted yes/no/any unless allow_corrupted=True
    for flag in ("yes", "no", "any"):
        q = build_query(plan=StatPlan(), category="armour.chest", rarity="any", corrupted=flag)
        misc = q["query"]["filters"]["misc_filters"]["filters"]
        assert misc["mirrored"] == {"option": "false"}
        q_allow = build_query(
            plan=StatPlan(), category="armour.chest", rarity="any", corrupted=flag, allow_corrupted=True
        )
        misc_allow = (q_allow["query"].get("filters") or {}).get("misc_filters", {}).get("filters", {})
        assert "mirrored" not in misc_allow

    # Unmapped ASCII name on --realm tw warns on stderr without breaking JSON stdout
    runner = CliRunner()
    res_warn = runner.invoke(
        main,
        ["trade", "search", "--realm", "tw", "--name", "Nonexistent Unique XYZ", "--dump-query"],
    )
    assert res_warn.exit_code == 0
    assert "Traditional Chinese" in res_warn.output

    # Stale --max-age fallback re-localizes rebuilt query for tw realm
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/trade/search/" in request.url.path:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"id": "emptyFresh", "result": [], "total": 0})
            return httpx.Response(200, json={"id": "staleHit", "result": ["id1"], "total": 1})
        if "/api/trade/fetch/" in request.url.path:
            return httpx.Response(200, json={"result": [{"id": "id1", "item": {}, "listing": {}}]})
        return httpx.Response(200, json={"result": []})

    tw_client = TradeClient(cache_dir=tmp_path, realm="tw", pob_dir=eng, transport=httpx.MockTransport(handler))
    query_args = {
        "plan": StatPlan(),
        "category": "armour.chest",
        "rarity": "any",
        "name": "The Ivory Tower",
        "item_type": "Saint's Hauberk",
    }
    initial_q = localize_query_for_tw(build_query(**query_args, max_age_hours=1.0), pob_dir=eng)
    res_ctx = _fetch_with_fresh_and_market(
        client=tw_client,
        league_name="巫影密會",
        query=initial_q,
        query_args=query_args,
        fetch_limit=5,
        max_age=1.0,
        bargain=None,
        search_sort="asc",
        sort_by="score",
        proxy=None,
        has_auto_query=False,
    )
    assert res_ctx["stale_fallback"] is True
    assert res_ctx["query"]["query"]["name"] == "象牙塔"
    assert res_ctx["query"]["query"]["type"] == "聖者鏈甲"
