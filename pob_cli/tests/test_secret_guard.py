"""Offline tests for L0 NO_SECRET_ENV_DUMP file guard, CLI wiring, Tencent tables, and `data translate`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob_cli.cli import main
from pob_cli.game_data.apt_non_gear import translate_offline_fallback
from pob_cli.game_data.queries_translate import _query_translate
from pob_cli.game_data.tables_cn_tencent import (
    TENCENT_BASE_ALIASES,
    TENCENT_RUNEGRAFT_ALIASES,
    TENCENT_TATTOO_ALIASES,
    TENCENT_UNIQUE_ALIASES,
)
from pob_cli.guardrails import NO_SECRET_ENV_DUMP_RULE_ID, check_secret_file_path


@pytest.fixture(autouse=True)
def _clear_secret_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POB_ALLOW_SECRET_PATH", raising=False)


def test_tencent_static_tables_cover_32_tattoos_and_32_runegrafts() -> None:
    assert TENCENT_BASE_ALIASES["大型星团珠宝"] == "Large Cluster Jewel"
    assert TENCENT_BASE_ALIASES["菌丝核心"] == "Mycelium Core"
    assert TENCENT_UNIQUE_ALIASES["绝望逃脱"] == "Impossible Escape"
    assert TENCENT_UNIQUE_ALIASES["妄语"] == "Voices"
    assert TENCENT_UNIQUE_ALIASES["英雄悲剧"] == "Heroic Tragedy"
    assert TENCENT_UNIQUE_ALIASES["腐败之种"] == "Putrid Seed"
    assert len(set(TENCENT_TATTOO_ALIASES.values())) == 32
    assert TENCENT_TATTOO_ALIASES["白鸽的尊贵文身"] == "Honoured Tattoo of the Dove"
    assert TENCENT_TATTOO_ALIASES["巨蜥的尊贵文身"] == "Honoured Tattoo of the Tuatara"
    assert TENCENT_TATTOO_ALIASES["冈姆的忠诚文身"] == "Loyalty Tattoo of Kaom"
    assert TENCENT_TATTOO_ALIASES["躯体的旅途文身"] == "Journey Tattoo of the Body"
    assert len(set(TENCENT_RUNEGRAFT_ALIASES.values())) == 32
    assert TENCENT_RUNEGRAFT_ALIASES["咆哮之符文嫁接"] == "Runegraft of Bellows"
    assert TENCENT_RUNEGRAFT_ALIASES["咆哮之符文嵌板"] == "Runegraft of Bellows"


@pytest.mark.parametrize(
    "path",
    [
        ".secrets/cn_cookie.txt", "D:/POE/.secrets/poe.env", "_secrets/my_build.xml",
        "cn_cookie.txt", "poe_cookie.txt", "cookies.txt", "poesessid.txt", "my_sessid.ini",
        ".cookie", "poe.cn.cookie", "oauth_token.json", "oauth_token.json.old",
        "daemon.json", "daemon.json.bak", "session_secret.key", ".env", ".env.local", ".envrc", ".envrc.local",
    ],
)
def test_secret_file_guard_blocks_credential_paths_and_respects_escape_hatch(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = check_secret_file_path(path)
    assert not res.allowed and res.rule_id == NO_SECRET_ENV_DUMP_RULE_ID
    assert res.reason and "POE_CN_COOKIE" in res.reason and "pob diagnose" in res.reason
    monkeypatch.setenv("POB_ALLOW_SECRET_PATH", "1")
    assert check_secret_file_path(path).allowed


@pytest.mark.parametrize(
    "path",
    [
        "build.xml", "builds/cookie_monster.xml", "D:/pob/builds/my_sessid_build.xml",
        "pob_cli/tests/fixtures/cookie_cutter_build.xml", "cn/POESESSID_notes.md",
        "Settings.xml", "cookie_cutter.txt", "data/bench_costs.json",
        "pob_cli/src/pob_cli/profile_import/cn_zh2en.py",
    ],
)
def test_secret_file_guard_zero_false_positives_on_build_and_doc_paths(path: str) -> None:
    assert check_secret_file_path(path).allowed


def test_cli_file_entrypoints_enforce_secret_file_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.cli_common import _prepare_load_source

    runner = CliRunner()
    secret_file = tmp_path / "cn_cookie.txt"
    secret_file.write_text("POESESSID=deadbeef", encoding="utf-8")
    normal_item = tmp_path / "normal_item.txt"
    normal_item.write_text("Rarity: RARE\nGlyph Coil\nTwo-Stone Ring\n--------\n+75 to maximum Life\n", encoding="utf-8")
    normal_xml = tmp_path / "cookie_monster.xml"
    normal_xml.write_text('<PathOfBuilding><Build level="90" className="Witch"/></PathOfBuilding>', encoding="utf-8")
    for args in (
        ["load", "@.secrets/cn_cookie.txt"],
        ["item", "inspect", "--file", str(secret_file)],
        ["item", "try", "--file", str(secret_file)],
        ["trade", "convert", "--file", str(secret_file), "--to", "cold"],
        ["trade", "search", "--slot", "ring", "--current-item-file", str(secret_file), "--dump-query"],
    ):
        r = runner.invoke(main, args)
        assert r.exit_code == 1 and NO_SECRET_ENV_DUMP_RULE_ID in r.output, (args, r.exit_code, r.output)
    assert _prepare_load_source(f"@{normal_xml}")[1]
    monkeypatch.setattr("pob_cli.item_inspect.inspect_item", lambda raw, **_kw: {"item": {"name": "Glyph Coil"}})
    assert runner.invoke(main, ["item", "inspect", "--file", str(normal_item), "--json"]).exit_code == 0


def test_secret_file_guard_blocks_symlink_to_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_symlink", lambda _self: True)
    monkeypatch.setattr(Path, "resolve", lambda _self: Path(".secrets/cn_cookie.txt"))
    res = check_secret_file_path("harmless_link.xml")
    assert not res.allowed and res.rule_id == NO_SECRET_ENV_DUMP_RULE_ID


def test_data_translate_resolves_tencent_cn_unique_tattoo_and_runegraft() -> None:
    expected = (
        ("绝望逃脱", "Impossible Escape"),
        ("白鸽的尊贵文身", "Honoured Tattoo of the Dove"),
        ("白鸽尊贵文身", "Honoured Tattoo of the Dove"),
        ("咆哮之符文嫁接", "Runegraft of Bellows"),
        ("咆哮符文嵌板", "Runegraft of Bellows"),
    )
    for zh, en in expected:
        rows, _ = translate_offline_fallback(zh, zh, limit=5)
        assert rows and rows[0]["en"] == en, (zh, rows)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE translations (id INTEGER PRIMARY KEY, en TEXT, en_lower TEXT, zh TEXT, zh_lower TEXT, category TEXT, cat_rank INTEGER)")
    conn.execute("CREATE TABLE uniques (name TEXT, name_lower TEXT, zh_name TEXT, zh_lower TEXT)")
    conn.execute("CREATE TABLE gems (name TEXT, name_lower TEXT, zh_name TEXT, zh_lower TEXT)")
    conn.execute("CREATE TABLE bases (name TEXT, name_lower TEXT, zh_name TEXT, zh_lower TEXT)")
    conn.execute("CREATE TABLE mods (data_json TEXT, stats_en_lower TEXT, stats_zh_lower TEXT)")
    for zh, en in expected:
        rows = _query_translate(conn, zh, 5, zh_available=False, raw_query=zh)
        assert rows and rows[0]["en"] == en, (zh, rows)
