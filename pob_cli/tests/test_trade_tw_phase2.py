"""Tests for P1 Phase 2: `zh-TW -> EN` reverse item translation into PoB `trade eval` & TW pricing."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.cli_trade.fresh_bargain import DEFAULT_TW_CURRENCY_RATES, _chaos_rates
from pob_cli.trade_client import TradeClient
from pob_cli.trade_item import (
    build_english_item_text,
    clear_tw_reverse_cache,
    convert_item_text,
    decode_extended_item_text,
    parse_item_text,
    reverse_tw_item_text,
    trade_mod_tiers_english,
)


def _make_tw_reverse_engine(root: Path) -> Path:
    (root / "PoE1" / "Data").mkdir(parents=True)
    tw = root / "Data" / "poe1" / "zh-rTW"
    tw.mkdir(parents=True)
    (tw / "meta.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (tw / "uniques.json").write_text(
        json.dumps({"entries": {"The Ivory Tower": "象牙塔", "Ephemeral Edge": "影殞"}}),
        encoding="utf-8",
    )
    (tw / "items.json").write_text(
        json.dumps({"entries": {"Saint's Hauberk": "聖者鏈甲", "Dusk Blade": "暮色之刃", "Vaal Regalia": "瓦爾法衣"}}),
        encoding="utf-8",
    )
    (tw / "stats.json").write_text(
        json.dumps(
            {
                "entries": {
                    "{0} to Intelligence": "{0} 智慧",
                    "{0} to maximum Energy Shield per {1} Reserved Life": "每 {1} 生命保留 {0} 最大能量護盾",
                    "{0} to maximum Mana": "{0} 最大魔力",
                    "Regenerate {0}% of Energy Shield per second": "每秒回復 {0}% 能量護盾",
                    "Chaos Damage is taken from Mana before Life": "混沌傷害優先從魔力扣除而非生命",
                    "{0}% increased maximum Energy Shield": "最大能量護盾增加 {0}%",
                    "{0} to maximum Life": "{0} 最大生命",
                    "{0}% to Fire Resistance": "{0}% 火焰抗性",
                    "{0}% to Cold Resistance": "{0}% 冰霜抗性",
                }
            }
        ),
        encoding="utf-8",
    )
    (tw / "item_metadata.json").write_text(
        json.dumps(
            {
                "headers": [
                    {"zh": "物品種類", "en": "Item Class"},
                    {"zh": "稀有度", "en": "Rarity"},
                    {"zh": "能量護盾", "en": "Energy Shield"},
                    {"zh": "物品等級", "en": "Item Level"},
                    {"zh": "插槽", "en": "Sockets"},
                ],
                "rarity_values": [
                    {"zh": "傳奇", "en": "Unique"},
                    {"zh": "稀有", "en": "Rare"},
                ],
                "item_classes": [{"zh": "胸甲", "en": "Body Armours"}],
                "status_lines": [{"zh": "已汙染", "en": "Corrupted"}],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_reverse_tw_item_text_unique_and_extended_base64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_reverse_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    clear_tw_reverse_cache()

    tw_clipboard = (
        "物品種類: 胸甲\n"
        "稀有度: 傳奇\n"
        "象牙塔\n"
        "聖者鏈甲\n"
        "--------\n"
        "能量護盾: 412\n"
        "記憶絲縷: 42\n"
        "物品等級: 85\n"
        "--------\n"
        "+65 智慧\n"
        "每 100 生命保留 +30 最大能量護盾\n"
        "+95 最大魔力\n"
        "每秒回復 2% 能量護盾\n"
        "混沌傷害優先從魔力扣除而非生命\n"
        "已汙染\n"
    )
    en_text = reverse_tw_item_text(tw_clipboard, pob_dir=eng)
    assert "Rarity: Unique" in en_text
    assert "The Ivory Tower" in en_text
    assert "Saint's Hauberk" in en_text
    assert "Memory Strands: 42" in en_text
    assert "+65 to Intelligence" in en_text
    assert "+30 to maximum Energy Shield per 100 Reserved Life" in en_text
    assert "+95 to maximum Mana" in en_text
    assert "Chaos Damage is taken from Mana before Life" in en_text
    assert "Corrupted" in en_text

    parsed = parse_item_text(en_text)
    assert parsed.rarity == "UNIQUE"
    assert parsed.name == "The Ivory Tower"
    assert parsed.base_type == "Saint's Hauberk"
    assert parsed.corrupted is True
    assert parsed.item_level == 85

    # Base64 extended.text fallback when structured item fields are minimal
    b64 = base64.b64encode(tw_clipboard.encode("utf-8")).decode("ascii")
    decoded = decode_extended_item_text({"item": {"extended": {"text": b64}}})
    assert decoded is not None and "象牙塔" in decoded
    en_from_ext = build_english_item_text({"item": {"extended": {"text": b64}}}, realm="tw", pob_dir=eng)
    assert "The Ivory Tower" in en_from_ext
    assert "+30 to maximum Energy Shield per 100 Reserved Life" in en_from_ext


def test_reverse_tw_worker_hook_and_rare_crafted_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_reverse_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    clear_tw_reverse_cache()

    called: list[str] = []

    def fake_worker_reverse(raw: str) -> str:
        called.append(raw)
        return "Rarity: UNIQUE\nEphemeral Edge\nDusk Blade\nImplicits: 0\n50% increased maximum Energy Shield"

    out = reverse_tw_item_text("Rarity: UNIQUE\n影殞\n暮色之刃", worker_reverse=fake_worker_reverse)
    assert len(called) == 1
    assert "Ephemeral Edge" in out

    # Structured TW fetch payload with craftedMods preserves {crafted} tag during reverse translation
    tw_fetch_row = {
        "id": "tw_rare_1",
        "item": {
            "name": "末日之甲",
            "typeLine": "聖者鏈甲",
            "rarity": "Rare",
            "frameType": 2,
            "ilvl": 86,
            "explicitMods": ["+90 最大生命"],
            "craftedMods": ["+35% 火焰抗性"],
        },
    }
    en_rare = build_english_item_text(tw_fetch_row, realm="tw", pob_dir=eng)
    assert "Saint's Hauberk" in en_rare
    assert "+90 to maximum Life" in en_rare
    assert "{crafted}+35% to Fire Resistance" in en_rare
    item_obj = parse_item_text(en_rare)
    assert item_obj.base_type == "Saint's Hauberk"
    assert any(m.kind == "crafted" and "Fire Resistance" in m.raw for m in item_obj.mods)


def test_tw_trade_search_e2e_reverse_eval_and_pricing_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_reverse_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_tw_reverse_cache()

    # TW chaos rates use safe Chaos/Divine fallback without hitting Global poe.ninja
    monkeypatch.setattr(cli_trade, "_chaos_rates", _chaos_rates)
    tw_client_dummy = TradeClient(cache_dir=tmp_path, realm="tw", pob_dir=eng)
    monkeypatch.setattr(
        cli_trade,
        "fetch_currency_chaos_rates",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("poe.ninja must not be called for tw realm")),
    )
    rates = _chaos_rates(tw_client_dummy, "巫影密會", None)
    assert rates["chaos"] == 1.0
    assert rates["divine"] == DEFAULT_TW_CURRENCY_RATES["divine"]

    tw_rows = [
        {
            "id": "tw_expensive_div",
            "item": {
                "name": "象牙塔",
                "typeLine": "聖者鏈甲",
                "rarity": "Unique",
                "frameType": 3,
                "ilvl": 85,
                "corrupted": True,
                "explicitMods": [
                    "+75 智慧",
                    "每 100 生命保留 +30 最大能量護盾",
                    "+110 最大魔力",
                ],
            },
            "listing": {
                "account": {"name": "SellerDiv"},
                "price": {"amount": 2, "currency": "divine"},
            },
        },
        {
            "id": "tw_cheaper_chaos",
            "item": {
                "name": "象牙塔",
                "typeLine": "聖者鏈甲",
                "rarity": "Unique",
                "frameType": 3,
                "ilvl": 84,
                "corrupted": True,
                "explicitMods": [
                    "+55 智慧",
                    "每 100 生命保留 +30 最大能量護盾",
                    "+85 最大魔力",
                ],
            },
            "listing": {
                "account": {"name": "SellerChaos"},
                "price": {"amount": 80, "currency": "chaos"},
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leagues"):
            return httpx.Response(200, json={"result": [{"id": "巫影密會", "realm": "pc"}]})
        if "/api/trade/search/" in request.url.path:
            return httpx.Response(200, json={"id": "twQ999", "result": ["tw_expensive_div", "tw_cheaper_chaos"], "total": 2})
        if "/api/trade/fetch/" in request.url.path:
            return httpx.Response(200, json={"result": tw_rows})
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: ([], "mock_local"))
    monkeypatch.setattr(
        cli_trade,
        "TradeClient",
        lambda **kw: TradeClient(cache_dir=tmp_path, pob_dir=eng, transport=httpx.MockTransport(handler), **kw),
    )

    pob_received_texts: list[str] = []

    def fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if action == "items":
            return {"raw": "Rarity: UNIQUE\nThe Ivory Tower\nSaint's Hauberk\nImplicits: 0\n+50 to Intelligence"}
        if action == "trade_eval_items":
            items_in = (params or {}).get("items") or []
            for entry in items_in:
                pob_received_texts.append(str(entry.get("raw") or ""))
            return {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1_000_000, "totalEhp": 50_000, "life": 1200},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "after": {
                            "primaryDpsKey": "CombinedDPS",
                            "primaryDps": 1_120_000 if "+75" in entry["raw"] else 1_030_000,
                            "totalEhp": 58_000 if "+75" in entry["raw"] else 52_000,
                            "life": 1200,
                        },
                        "diff": {"newWarnings": [], "resolvedWarnings": []},
                    }
                    for entry in items_in
                ],
            }
        return {}

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", fake_daemon_call)

    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "tw",
            "--slot",
            "body_armour",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--sort",
            "price",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["realm"] == "tw"
    assert data["ratesSource"] == "tw_static_fallback"
    assert data["league"] == "巫影密會"
    assert data["searchUrl"].startswith("https://pathofexile.tw/trade/search/")

    # PoB received clean English reverse-translated item texts!
    assert len(pob_received_texts) == 2
    assert all("The Ivory Tower" in txt and "Saint's Hauberk" in txt for txt in pob_received_texts)
    assert any("+30 to maximum Energy Shield per 100 Reserved Life" in txt for txt in pob_received_texts)

    # Price sort ranks 80 chaos ahead of 2 divine (300c fallback) instead of raw 2 < 80
    cands = data["candidates"]
    assert cands[0]["price"]["currency"] == "chaos"
    assert cands[0]["price"]["chaos"] == 80.0
    assert cands[1]["price"]["currency"] == "divine"
    assert cands[1]["price"]["chaos"] == 300.0
    assert cands[1]["pob"]["dps_after"] == 1_120_000

    # Markdown output appends TW static fallback note
    res_md = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "tw",
            "--slot",
            "body_armour",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--sort",
            "price",
        ],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "Note: TW chaos-equivalent uses static fallback rates (1d=150c; override via POB_TW_DIVINE_CHAOS)" in res_md.output

    # POB_TW_DIVINE_CHAOS env override switches ratesSource and updates divine conversion
    monkeypatch.setenv("POB_TW_DIVINE_CHAOS", "200")
    res_env = runner.invoke(
        main,
        [
            "trade",
            "search",
            "--realm",
            "tw",
            "--slot",
            "body_armour",
            "--name",
            "The Ivory Tower",
            "--corrupted",
            "yes",
            "--sort",
            "price",
            "--json",
        ],
    )
    assert res_env.exit_code == 0, res_env.output
    data_env = json.loads(res_env.output)
    assert data_env["ratesSource"] == "tw_env_override"
    assert data_env["candidates"][1]["price"]["chaos"] == 400.0


def test_unnamed_tw_rare_and_harvest_tier_lookup_on_tw_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_tw_reverse_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    clear_tw_reverse_cache()

    # Unnamed TW Rare item (no separator before Implicits and with separator) translates first mod properly
    unnamed_with_sep = "Rarity: Rare\n瓦爾法衣\n--------\nImplicits: 0\n+90 最大生命"
    en_sep = reverse_tw_item_text(unnamed_with_sep, pob_dir=eng)
    assert "Vaal Regalia" in en_sep
    assert "+90 to maximum Life" in en_sep

    unnamed_no_sep = "Rarity: Rare\n瓦爾法衣\n+90 最大生命"
    en_nosep = reverse_tw_item_text(unnamed_no_sep, pob_dir=eng)
    assert "Vaal Regalia" in en_nosep
    assert "+90 to maximum Life" in en_nosep

    # Harvest conversion tier lookup retains tier & tier_range on zh-TW trade listings
    tw_item = {
        "name": "末日之甲",
        "typeLine": "瓦爾法衣",
        "rarity": "Rare",
        "frameType": 2,
        "ilvl": 86,
        "explicitMods": [
            {
                "description": "+43% 冰霜抗性",
                "mods": [
                    {
                        "tier": "S1",
                        "magnitudes": [{"hash": "explicit.stat_4220027924", "min": "42", "max": "45"}],
                    }
                ],
            }
        ],
    }
    en_text = build_english_item_text(tw_item, realm="tw", pob_dir=eng)
    tiers = trade_mod_tiers_english(tw_item, realm="tw", pob_dir=eng)
    converted = convert_item_text(en_text, "fire", tiers=tiers)
    assert len(converted.conversions) == 1
    assert converted.conversions[0].tier == "S1"
    assert converted.conversions[0].tier_range == "42-45"
