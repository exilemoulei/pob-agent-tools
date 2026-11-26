"""Offline unit tests for APT item drops, non-gear bilingual dictionary, and Chinese `pob price` resolution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import external_data as ext
from pob_cli import game_data as gd
from pob_cli.cli import main
from pob_cli.formatters import format_game_data

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "pob_cli" / "src" / "pob_cli"
DATA_DIR = PKG_ROOT / "data"
SCRIPTS_DIR = REPO_ROOT / "pob_cli" / "scripts"


def test_third_party_notices_and_packaging_configuration() -> None:
    notices = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    assert notices.is_file()
    txt = notices.read_text(encoding="utf-8")
    assert "SnosMe/awakened-poe-trade" in txt
    assert "MIT License" in txt
    assert "Alexander Drozdov" in txt

    pyproject = (REPO_ROOT / "pob_cli" / "pyproject.toml").read_text(encoding="utf-8")
    assert "data/*.json.gz" in pyproject

    spec_text = (REPO_ROOT / "pob_cli" / "packaging" / "pob.spec").read_text(encoding="utf-8")
    assert ".json.gz" in spec_text

    drops_gz = DATA_DIR / "apt_item_drops.json.gz"
    non_gear_gz = DATA_DIR / "apt_non_gear_items.json.gz"
    assert drops_gz.is_file() and 0 < drops_gz.stat().st_size < 15_000
    assert non_gear_gz.is_file() and 0 < non_gear_gz.stat().st_size <= 50_000


def test_drop_forward_and_reverse_queries_bilingual() -> None:
    # Forward lookup: boss / fragment / map -> dropped loot
    res_echo = gd.query_drop_sources("Echo of Loneliness")
    assert res_echo["count"] >= 1
    assert any(
        any(it["name"] == "Legacy of the Rose" for it in grp["items"])
        for grp in res_echo["forward"]
    )

    res_syn = gd.query_drop_sources("Syndicate Medallion")
    assert any(
        any(it["name"] == "Veiled Exalted Orb" for it in grp["items"])
        for grp in res_syn["forward"]
    )

    res_nm = gd.query_drop_sources("Nightmare Map")
    assert any(
        any(it["name"] == "Yoke of Suffering" for it in grp["items"])
        for grp in res_nm["forward"]
    )

    # Fragment of the Minotaur appears in reverse (dropped by Shaper Guardian Map) and/or forward
    res_mino = gd.query_drop_sources("Fragment of the Minotaur")
    assert res_mino["count"] >= 1
    assert res_mino["forward"] or res_mino["reverse"]

    # Reverse lookup: unique EN, CN, TW, and slang
    for query in ("Legacy of the Rose", "玫瑰遗产", "玫瑰杖", "玫瑰的傳承"):
        rev = gd.query_drop_sources(query)
        assert any(
            entry["item"]["name"] == "Legacy of the Rose"
            and any(s["name"] == "Echo of Loneliness" for s in entry["dropSources"])
            for entry in rev["reverse"]
        ), f"failed reverse drop lookup for {query!r}"

    rev_yoke = gd.query_drop_sources("Yoke of Suffering")
    assert any(
        entry["item"]["name"] == "Yoke of Suffering"
        and any(s["name"] == "Nightmare Map" for s in entry["dropSources"])
        for entry in rev_yoke["reverse"]
    )

    # Fast helper for `pob data unique` enrichment + Markdown rendering
    assert "Echo of Loneliness" in gd.lookup_item_drop_sources("Legacy of the Rose")
    md = format_game_data(
        {
            "kind": "unique",
            "query": "Legacy of the Rose",
            "pobVersion": "test",
            "dataVersion": "test",
            "count": 1,
            "results": [
                {
                    "name": "Legacy of the Rose",
                    "zh_name": "玫瑰遗产",
                    "base_type": "Judgement Staff",
                    "item_class": "Staff",
                    "dropSources": gd.lookup_item_drop_sources("Legacy of the Rose"),
                    "implicits": [],
                    "explicits": [],
                }
            ],
        }
    )
    assert "Drop Sources" in md
    assert "Echo of Loneliness" in md


def test_cli_data_drop_works_offline_without_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.setenv("POB_ROOT", str(tmp_path / "missing_engine"))
    runner = CliRunner()

    r_json = runner.invoke(main, ["data", "drop", "玫瑰杖", "--json"])
    assert r_json.exit_code == 0, r_json.output
    data = json.loads(r_json.output)
    assert data["kind"] == "drop"
    assert data["resolvedAlias"]["to"] == "Legacy of the Rose"
    assert any(e["item"]["name"] == "Legacy of the Rose" for e in data["reverse"])

    r_md = runner.invoke(main, ["data", "drop", "Echo of Loneliness", "--limit", "5"])
    assert r_md.exit_code == 0, r_md.output
    assert "Legacy of the Rose" in r_md.output


def test_translate_non_gear_works_100pct_offline_without_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.setenv("POB_ROOT", str(tmp_path / "no_engine"))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("POB_LEGACY_DIR", str(tmp_path / "no_legacy"))

    cases = [
        ("剥离石", "Orb of Annulment"),
        ("無效石", "Orb of Annulment"),
        ("神圣石", "Divine Orb"),
        ("神聖石", "Divine Orb"),
        ("医者", "The Doctor"),
        ("瘋醫", "The Doctor"),
        ("药剂师", "The Apothecary"),
        ("藥劑師", "The Apothecary"),
        ("明镜", "House of Mirrors"),
        ("觉醒者之石", "Awakener's Orb"),
    ]
    for zh_query, expected_en in cases:
        assert gd.resolve_non_gear_en(zh_query) == expected_en
        res = gd.query_local_data("translate", zh_query, pob_dir=tmp_path / "no_engine", runtime_dir=tmp_path / "rt")
        assert res["count"] >= 1
        assert res["results"][0]["en"] == expected_en

    # Reverse EN -> ZH offline without engine
    en_res = gd.query_local_data("translate", "The Apothecary", pob_dir=tmp_path / "no_engine", runtime_dir=tmp_path / "rt")
    assert en_res["results"][0]["en"] == "The Apothecary"
    assert en_res["results"][0]["zh"] == "药剂师"


def _make_minimal_pob_engine(base_dir: Path, zh_items: dict[str, str]) -> Path:
    data = base_dir / "PoE1" / "Data"
    for sub in ("Skills", "Bases", "Uniques"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (base_dir / "PoE1" / "manifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><PoBVersion><Version number="2.99.0" /></PoBVersion>',
        encoding="utf-8",
    )
    (data / "Gems.lua").write_text("return {}", encoding="utf-8")
    (data / "Skills" / "act_int.lua").write_text("local skills = ...", encoding="utf-8")
    (data / "Bases" / "amulet.lua").write_text("local itemBases = ...", encoding="utf-8")
    (data / "Uniques" / "amulet.lua").write_text("return {}", encoding="utf-8")
    (data / "ModExplicit.lua").write_text("return {}", encoding="utf-8")
    zh_dir = base_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir.mkdir(parents=True, exist_ok=True)
    (zh_dir / "meta.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    (zh_dir / "items.json").write_text(json.dumps({"entries": zh_items}, ensure_ascii=False), encoding="utf-8")
    return base_dir


def test_translate_engine_first_priority_and_cross_path_parity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Engine SQLite wins when present, falls back to APT on misses, and matches APT canon EN without drift."""
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    monkeypatch.setenv("POB_LEGACY_DIR", str(tmp_path / "no_legacy"))

    rt_engine = tmp_path / "rt_engine"
    rt_offline = tmp_path / "rt_offline"
    missing_dir = tmp_path / "no_engine"
    engine_dir = _make_minimal_pob_engine(
        tmp_path / "syn_engine",
        {
            "Engine Only Relic": "引擎专属圣物",
            "Nightmare Map": "梦魇地图",
            "Strand": "致命岩滩",
        },
    )

    # (a) Engine-first priority: engine entries come from SQLite (not APT); misses fall back to APT.
    eng_only = gd.query_local_data("translate", "引擎专属圣物", pob_dir=engine_dir, runtime_dir=rt_engine)
    assert eng_only["pobVersion"] == "2.99.0"
    assert eng_only["results"][0]["en"] == "Engine Only Relic"
    assert eng_only["results"][0].get("source") != "apt"

    eng_miss = gd.query_local_data("translate", "剥离石", pob_dir=engine_dir, runtime_dir=rt_engine)
    assert eng_miss["pobVersion"] == "2.99.0"
    assert eng_miss["results"][0]["en"] == "Orb of Annulment"
    assert eng_miss["results"][0].get("source") == "apt"

    # (b) Cross-path parity: overlapping entries resolve to the exact same canonical EN on both paths.
    for query, expected_en in (
        ("Nightmare Map", "Nightmare Map"),
        ("梦魇地图", "Nightmare Map"),
        ("Strand", "Strand"),
        ("致命岩滩", "Strand"),
    ):
        res_engine = gd.query_local_data("translate", query, pob_dir=engine_dir, runtime_dir=rt_engine)
        res_offline = gd.query_local_data("translate", query, pob_dir=missing_dir, runtime_dir=rt_offline)
        assert res_engine["results"][0]["en"] == expected_en
        assert res_engine["results"][0].get("source") != "apt"
        assert res_offline["pobVersion"] == "offline"
        assert res_offline["results"][0]["en"] == res_engine["results"][0]["en"]
        assert res_offline["results"][0].get("source") == "apt"

    # Optional live-engine parity check when a real PoB checkout is available on the host.
    from engine_paths import find_engine_data

    real_engine = find_engine_data()
    if real_engine is not None:
        live_eng = gd.query_local_data("translate", "Nightmare Map", pob_dir=real_engine, runtime_dir=tmp_path / "rt_live")
        live_off = gd.query_local_data("translate", "Nightmare Map", pob_dir=missing_dir, runtime_dir=rt_offline)
        assert live_eng["results"][0]["en"] == live_off["results"][0]["en"] == "Nightmare Map"


def test_pob_price_resolves_chinese_queries_before_ninja_search(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_overviews: dict[str, list[dict[str, Any]]] = {
        "Currency": [
            {"name": "Divine Orb", "chaos": 150.0, "count": 500},
            {"name": "Orb of Annulment", "chaos": 18.5, "count": 420},
        ],
        "DivinationCard": [
            {"name": "The Doctor", "chaos": 1800.0, "count": 40},
            {"name": "The Apothecary", "chaos": 5200.0, "count": 25},
        ],
        "UniqueWeapon": [
            {"name": "Legacy of the Rose", "chaos": 600.0, "count": 15},
        ],
    }
    monkeypatch.setattr(ext, "resolve_current_trade_league", lambda proxy=None: "TestLeague")
    monkeypatch.setattr(ext, "_fetch_dense_overviews", lambda league, proxy=None: fake_overviews)

    for zh_q, expected_en in (
        ("神圣石", "Divine Orb"),
        ("剥离石", "Orb of Annulment"),
        ("医者", "The Doctor"),
        ("药剂师", "The Apothecary"),
        ("玫瑰杖", "Legacy of the Rose"),
    ):
        res = ext.query_price(zh_q)
        assert res["query"] == zh_q
        assert res["resolvedQuery"] == expected_en
        assert res["matches"], f"no price match for {zh_q!r}"
        assert res["matches"][0]["name"] == expected_en

    multi = ext.query_price_multi(["神圣石", "医者"])
    assert [r["matches"][0]["name"] for r in multi["results"]] == ["Divine Orb", "The Doctor"]


def test_update_apt_data_script_is_deterministic(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("update_apt_data", SCRIPTS_DIR / "update_apt_data.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    data_root = tmp_path / "apt" / "renderer" / "public" / "data"
    (data_root / "en").mkdir(parents=True)
    (data_root / "cmn-Hant").mkdir(parents=True)
    (data_root / "item-drop.json").write_text(
        json.dumps([{"query": ["ITEM::Echo of Loneliness"], "items": ["UNIQUE::Legacy of the Rose // Staff"]}]),
        encoding="utf-8",
    )
    (data_root / "en" / "items.ndjson").write_text(
        "\n".join(
            [
                json.dumps({"name": "Orb of Annulment", "refName": "Orb of Annulment", "namespace": "ITEM"}),
                json.dumps({"name": "The Doctor", "refName": "The Doctor", "namespace": "DIVINATION_CARD"}),
                json.dumps({"name": "Echo of Loneliness", "refName": "Echo of Loneliness", "namespace": "ITEM"}),
                json.dumps({"name": "Legacy of the Rose", "refName": "Legacy of the Rose", "namespace": "UNIQUE"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (data_root / "cmn-Hant" / "items.ndjson").write_text(
        "\n".join(
            [
                json.dumps({"name": "無效石", "refName": "Orb of Annulment", "namespace": "ITEM"}),
                json.dumps({"name": "瘋醫", "refName": "The Doctor", "namespace": "DIVINATION_CARD"}),
                json.dumps({"name": "孤獨之迴響", "refName": "Echo of Loneliness", "namespace": "ITEM"}),
                json.dumps({"name": "玫瑰的傳承", "refName": "Legacy of the Rose", "namespace": "UNIQUE"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    mod.generate_apt_data(tmp_path / "apt", out_dir=out1)
    mod.generate_apt_data(tmp_path / "apt", out_dir=out2)
    for fn in ("apt_item_drops.json.gz", "apt_non_gear_items.json.gz"):
        h1 = hashlib.sha256((out1 / fn).read_bytes()).hexdigest()
        h2 = hashlib.sha256((out2 / fn).read_bytes()).hexdigest()
        assert h1 == h2
