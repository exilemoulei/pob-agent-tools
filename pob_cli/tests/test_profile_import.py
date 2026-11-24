"""Offline unit tests for international & CN official PoE1 profile import (L1/L4)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli as cli_mod
from pob_cli import importers
from pob_cli.errors import CrossGameHallucinationError, DaemonRequestError
from pob_cli.guardrails import validate_build_xml
from pob_cli.profile_import import (
    Zh2EnDictionary,
    character_bundle_to_pob_xml,
    fetch_cn_character_bundle,
    fetch_cn_characters,
    fetch_intl_characters,
    is_official_profile_url,
    parse_profile_source,
    resolve_target_character,
    sort_profile_characters,
    translate_cn_bundle,
)


class _DummyLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: Any) -> None:
        return None


class _FakeWorker:
    def __init__(self, *, caps: dict[str, Any] | None = None) -> None:
        self.lock = _DummyLock()
        self.caps = caps or {}
        self.pob_root = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_good_code: str | None = None
        self.source_label: str | None = None
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self._import_count = 0

    def save_slot_state(self) -> None:
        pass

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "import_status":
            return {"mode": "SELECTCHAR", "importedCount": self._import_count}
        if method == "import_account_character":
            self._import_count += 1
            return {"started": True, "what": p.get("what")}
        if method == "export_code":
            return {"code": "FAKE_EXPORT_CODE"}
        if method == "get_build_info":
            return {"className": "Shadow", "ascendClassName": "Assassin", "level": 89, "treeVersion": "3_30"}
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1, "list": []}, "mainSkill": {"index": 1, "list": []}}
        return {"ok": True}


def test_parse_official_profile_urls_and_reject_poe2() -> None:
    assert is_official_profile_url("https://www.pathofexile.com/account/view-profile/alice-1234")
    assert is_official_profile_url("https://poe.game.qq.com/account/view-profile/tester-9999/characters")
    assert not is_official_profile_url("https://pobb.in/abc123")

    t1 = parse_profile_source("https://www.pathofexile.com/account/view-profile/alice-1234/characters?characterName=ArcWitch")
    assert t1.server == "intl"
    assert t1.account_name == "alice#1234"
    assert t1.char_name == "ArcWitch"
    assert t1.realm == "pc"

    t2 = parse_profile_source("https://poe.game.qq.com/account/view-profile/%E6%B5%8B%E8%AF%95-4321")
    assert t2.server == "cn"
    assert t2.account_name == "测试#4321"
    assert t2.realm == "cn"

    t3 = parse_profile_source(None, realm="cn")
    assert t3.server == "cn"
    assert t3.account_name == ""

    with pytest.raises(CrossGameHallucinationError):
        parse_profile_source("https://pathofexile2.com/account/view-profile/alice-1234")


def test_sort_and_disambiguate_characters() -> None:
    chars = [
        {"name": "StdMule", "league": "Standard", "class": "Witch", "level": 100},
        {"name": "LeagueLow", "league": "Covens", "class": "Shadow", "level": 72},
        {"name": "LeagueMain", "league": "Covens", "class": "Assassin", "level": 95},
    ]
    ordered = sort_profile_characters(chars)
    assert [c["name"] for c in ordered] == ["LeagueMain", "LeagueLow", "StdMule"]

    target = parse_profile_source("https://www.pathofexile.com/account/view-profile/alice-1234")
    # Multi-character without --char must fail cleanly with candidate table (never silent guess)
    with pytest.raises(DaemonRequestError) as exc_info:
        resolve_target_character(chars, target=target)
    msg = str(exc_info.value)
    assert "LeagueMain" in msg and "--char" in msg

    # Explicit --char selects cleanly
    t_explicit = parse_profile_source("https://www.pathofexile.com/account/view-profile/alice-1234", char_name="LeagueMain")
    assert resolve_target_character(chars, target=t_explicit)["name"] == "LeagueMain"

    # Single character auto-selects without --char
    assert resolve_target_character([chars[2]], target=target)["name"] == "LeagueMain"


def test_fetch_intl_characters_public_and_private_cookie_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POESESSID", raising=False)
    monkeypatch.delenv("POE_COOKIE", raising=False)

    # 1. Public profile succeeds with zero Cookie header
    seen_headers: list[dict[str, str]] = []

    def public_handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(req.headers))
        return httpx.Response(200, json=[{"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 90}])

    with httpx.Client(transport=httpx.MockTransport(public_handler)) as client:
        chars, used_cookie = fetch_intl_characters("alice#1234", client=client)
    assert used_cookie is False
    assert len(chars) == 1
    assert "cookie" not in {k.lower() for k in seen_headers[0]}

    # 2. Private profile (403) without cookie fails with clear guidance
    def private_handler(req: httpx.Request) -> httpx.Response:
        if "POESESSID=secret1234567890" in req.headers.get("cookie", ""):
            return httpx.Response(200, json=[{"name": "SecretChar", "league": "Covens", "class": "Witch", "level": 98}])
        return httpx.Response(403, text="Forbidden")

    with httpx.Client(transport=httpx.MockTransport(private_handler)) as client:
        with pytest.raises(DaemonRequestError, match="403"):
            fetch_intl_characters("alice#1234", client=client)

        # 3. Setting POESESSID in env retries cleanly with Cookie
        monkeypatch.setenv("POESESSID", "secret1234567890")
        chars2, used_cookie2 = fetch_intl_characters("alice#1234", client=client)
    assert used_cookie2 is True
    assert chars2[0]["name"] == "SecretChar"


def test_fetch_cn_server_post_auto_profile_and_inventory_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POE_CN_SESSID", raising=False)
    monkeypatch.delenv("POE_CN_COOKIE", raising=False)
    with pytest.raises(DaemonRequestError, match="POE_CN_SESSID"):
        fetch_cn_characters("tester#1234")

    monkeypatch.setenv("POE_CN_SESSID", "cn_sess_abc123456")
    calls: list[tuple[str, str]] = []

    def cn_handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/profile":
            return httpx.Response(200, json={"name": "国服玩家#8888"})
        if req.url.path == "/character-window/get-characters":
            assert req.method == "POST"
            return httpx.Response(200, json=[{"name": "大法师角色", "league": "永火之咒", "class": "圣宗", "level": 92}])
        if req.url.path == "/character-window/get-items":
            return httpx.Response(
                200,
                json={
                    "character": {"name": "大法师角色", "level": 92, "class": "圣宗"},
                    "items": [
                        {"inventoryId": "Helm", "baseType": "灵主之环", "frameType": 2},
                        {"inventoryId": "MainInventory", "baseType": "传送卷轴", "frameType": 0},
                        {"inventoryId": "ExpandedMainInventory", "baseType": "回城卷轴", "frameType": 0},
                        {"inventoryId": "Trinket", "baseType": "赏金猎人饰品", "frameType": 2},
                    ],
                },
            )
        if req.url.path == "/character-window/get-passive-skills":
            return httpx.Response(200, json={"character": 5, "ascendancy": 2, "hashes": [100, 200], "items": []})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(cn_handler)) as client:
        acc, chars = fetch_cn_characters(None, client=client)
        assert acc == "国服玩家#8888"
        assert chars[0]["name"] == "大法师角色"
        bundle = fetch_cn_character_bundle(acc, chars[0], client=client)

    assert ("GET", "/api/profile") in calls
    assert ("POST", "/character-window/get-characters") in calls
    assert len(bundle["items"]["items"]) == 1
    assert bundle["items"]["items"][0]["inventoryId"] == "Helm"


def test_cn_zh2en_translation_and_pob_xml_roundtrip() -> None:
    dictionary = Zh2EnDictionary.from_entries(
        bases={"Hubris Circlet": "灵主之环", "Crimson Jewel": "赤红珠宝"},
        uniques={"Ashes of the Stars": "群星之灰"},
        gems={"Arc": "电弧", "Archmage Support": "大法师辅助", "Vaal Arc": "瓦尔·电弧"},
        passives={"Impact": "影响", "Honoured Tattoo of the Dove": "鸽子荣誉纹身"},
        stats={
            "+{0} to maximum Life": "+{0} 最大生命",
            "{0}% less Effect of your Curses": "你的诅咒效果总降 {0}%",
            "Adds {0} to {1} Lightning Damage to Spells": "法术附加 {0} - {1} 基础闪电伤害",
        },
    )
    cn_bundle: dict[str, Any] = {
        "char": {"name": "国服大法师", "class": "圣宗", "level": 92, "league": "永火之咒"},
        "items": {
            "character": {"name": "国服大法师", "class": "圣宗", "level": 92},
            "items": [
                {
                    "inventoryId": "Helm",
                    "frameType": 2,
                    "name": "随机黄装名",
                    "baseType": "灵主之环",
                    "typeLine": "灵主之环",
                    "ilvl": 86,
                    "properties": [{"name": "能量护盾", "values": [["350", 1]]}, {"name": "品质", "values": [["+20%", 1]]}],
                    "explicitMods": [
                        "+85 最大生命",
                        "法术附加 12 - 48 基础闪电伤害",
                        "一条完全未收录的新赛季未知中文词缀",
                    ],
                    "craftedMods": ["你的诅咒效果总降 15%"],
                    "sockets": [{"group": 0, "sColour": "B"}, {"group": 0, "sColour": "B"}],
                    "socketedItems": [
                        {
                            "socket": 0,
                            "baseType": "电弧",
                            "typeLine": "电弧",
                            "properties": [{"name": "等级", "values": [["21", 0]]}, {"name": "品质", "values": [["+20%", 0]]}],
                        },
                        {
                            "socket": 1,
                            "baseType": "大法师辅助",
                            "typeLine": "大法师辅助",
                            "properties": [{"name": "等级", "values": [["20", 0]]}],
                        },
                    ],
                },
                {
                    "inventoryId": "Flask",
                    "x": 0,
                    "frameType": 1,
                    "name": "",
                    "baseType": "Quicksilver Flask",
                    "typeLine": "Quicksilver Flask",
                },
            ],
        },
        "passiveSkills": {
            "character": 5,
            "ascendancy": 2,
            "hashes": [111, 222, 333],
            "mastery_effects": {"111": 999},
            "skill_overrides": {"222": {"name": "鸽子荣誉纹身"}},
            "items": [
                {
                    "x": 0,
                    "frameType": 3,
                    "name": "群星之灰",
                    "baseType": "赤红珠宝",
                    "explicitMods": ["若禁断之肉上有符合的词缀，配置 影响"],
                }
            ],
        },
    }

    en_bundle, warns = translate_cn_bundle(cn_bundle, dictionary)
    assert en_bundle["char"]["class"] == "Hierophant"
    helm = en_bundle["items"]["items"][0]
    assert helm["baseType"] == "Hubris Circlet"
    assert "+85 to maximum Life" in helm["explicitMods"]
    assert "Adds 12 to 48 Lightning Damage to Spells" in helm["explicitMods"]
    assert "15% less Effect of your Curses" in helm["craftedMods"]
    assert len(warns) == 1 and "[CN-TRANSLATE-WARN]" in warns[0]
    assert "未知中文词缀" in warns[0]
    assert en_bundle["passiveSkills"]["skill_overrides"]["222"]["name"] == "Honoured Tattoo of the Dove"
    assert "Allocates Impact if you have the matching modifier on Forbidden Flesh" in en_bundle["passiveSkills"]["items"][0]["explicitMods"]

    xml_text = character_bundle_to_pob_xml(en_bundle, tree_version="3_30")
    validate_build_xml(xml_text, expected_game="poe1")
    root = ET.fromstring(xml_text)
    build_el = root.find("Build")
    assert build_el is not None
    assert build_el.attrib["className"] == "Templar"
    assert build_el.attrib["ascendClassName"] == "Hierophant"
    assert build_el.attrib["level"] == "92"
    gems = [g.attrib["nameSpec"] for g in root.findall(".//Gem")]
    assert "Arc" in gems and "Archmage" in gems
    spec_el = root.find(".//Spec")
    assert spec_el is not None
    assert spec_el.attrib["nodes"] == "111,222,333"
    assert spec_el.attrib["masteryEffects"] == "{111,999}"
    flask_slot = root.find(".//Slot[@name='Flask 1']")
    assert flask_slot is not None and flask_slot.attrib.get("active") == "true"


def test_load_build_into_worker_routes_official_profile_and_classifies_http_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. POSIX worker (caps.importCharacterJson=True) routes international profile to import_character_json
    posix_w = _FakeWorker(caps={"importCharacterJson": True})
    monkeypatch.setattr(
        "pob_cli.profile_import.fetch_intl_characters",
        lambda *_a, **_k: ([{"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89}], False),
    )
    monkeypatch.setattr(
        "pob_cli.profile_import.fetch_intl_character_bundle",
        lambda *_a, **_k: {
            "char": {"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89},
            "passiveSkills": {"hashes": [1, 2], "items": []},
            "items": {"items": []},
        },
    )
    res = importers.load_build_into_worker(
        posix_w,
        source="https://www.pathofexile.com/account/view-profile/alice-1234",
        char_name="HeroA",
    )
    assert res["importMethod"] == "official_profile_intl_import_character_json"
    assert any(m == "import_character_json" for m, _ in posix_w.calls)
    assert res.get("importNotes")

    # 2. HTTP 403 on generic share URL fallback raises DaemonRequestError (exit 1), NOT httpx.HTTPStatusError (exit 5)
    req = httpx.Request("GET", "https://example.com/blocked")
    resp = httpx.Response(403, request=req, text="cf challenge")
    monkeypatch.setattr(
        importers,
        "_make_httpx_client",
        lambda **_k: httpx.Client(transport=httpx.MockTransport(lambda _r: resp)),
    )
    with pytest.raises(DaemonRequestError, match="403"):
        importers.fetch_share_url_code_fallback("https://example.com/blocked")


def test_cli_profile_command_markdown_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_listing(source: str | None = None, **_k: Any) -> dict[str, Any]:
        return {
            "server": "intl",
            "accountName": "alice#1234",
            "realm": "pc",
            "leagueFilter": None,
            "usedCookie": False,
            "count": 1,
            "characters": [{"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89}],
            "markdown": "### PoE1 Official Profile: `alice#1234`\n| 1 | `HeroA` | Covens | Assassin | 89 |",
        }

    monkeypatch.setattr("pob_cli.profile_import.fetch_profile_listing", fake_listing)
    runner = CliRunner()
    r_md = runner.invoke(cli_mod.main, ["profile", "https://www.pathofexile.com/account/view-profile/alice-1234"])
    assert r_md.exit_code == 0, r_md.output
    assert "HeroA" in r_md.output

    r_json = runner.invoke(cli_mod.main, ["profile", "alice#1234", "--json"])
    assert r_json.exit_code == 0, r_json.output
    parsed = json.loads(r_json.output)
    assert parsed["accountName"] == "alice#1234"
    assert parsed["characters"][0]["name"] == "HeroA"
