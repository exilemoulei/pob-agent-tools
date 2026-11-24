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


def test_330_structured_mod_dicts_markup_and_main_socket_group_selection() -> None:
    # P0: 3.30 character-window/get-items returns dict mods `{description, flags}` + `[id|Display]` markup
    # P2: smart mainSocketGroup chooses 6L active damage skill over 4L Helmet Shield Charge / 4L Boots Raise Spectre
    bundle = {
        "char": {"name": "ArcMaster", "league": "Covens", "class": "Assassin", "level": 89},
        "passiveSkills": {"hashes": [100, 200], "hashes_ex": [65536, 65537], "mastery_effects": {}, "items": []},
        "items": {
            "character": {"name": "ArcMaster", "class": "Assassin", "level": 89},
            "items": [
                {
                    "inventoryId": "Helm",
                    "frameType": 2,
                    "name": "Fast Helm",
                    "baseType": "Hubris Circlet",
                    "typeLine": "Hubris Circlet",
                    "ilvl": 85,
                    "sockets": [{"group": 0, "sColour": "R"}, {"group": 0, "sColour": "G"}],
                    "explicitMods": [
                        {"description": "+80 to maximum Life"},
                        {"description": "12% increased Cooldown Recovery Rate", "flags": {"crafted": True}},
                    ],
                    "socketedItems": [
                        {"baseType": "Shield Charge", "typeLine": "Shield Charge", "socket": 0, "support": False, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                        {"baseType": "Faster Attacks Support", "typeLine": "Faster Attacks Support", "socket": 1, "support": True, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                    ],
                },
                {
                    "inventoryId": "Boots",
                    "frameType": 2,
                    "name": "Minion Boots",
                    "baseType": "Warlock Boots",
                    "typeLine": "Warlock Boots",
                    "ilvl": 86,
                    "sockets": [{"group": 0, "sColour": "B"}, {"group": 0, "sColour": "R"}, {"group": 0, "sColour": "B"}, {"group": 0, "sColour": "B"}],
                    "implicitMods": [{"description": "7% increased Cooldown Recovery Rate"}],
                    "explicitMods": [{"description": "+40% to Fire Resistance", "flags": {"fractured": True}}],
                    "socketedItems": [
                        {"baseType": "Raise Spectre", "typeLine": "Raise Spectre", "socket": 0, "support": False, "properties": [{"name": "Level", "values": [["21", 0]]}]},
                        {"baseType": "Empower Support", "typeLine": "Empower Support", "socket": 1, "support": True, "properties": [{"name": "Level", "values": [["3", 0]]}]},
                        {"baseType": "Minion Life Support", "typeLine": "Minion Life Support", "socket": 2, "support": True, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                        {"baseType": "Elemental Army Support", "typeLine": "Elemental Army Support", "socket": 3, "support": True, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                    ],
                },
                {
                    "inventoryId": "Weapon",
                    "frameType": 3,
                    "name": "The Sword of Ages",
                    "baseType": "Exquisite Blade",
                    "typeLine": "Exquisite Blade",
                    "ilvl": 86,
                    "sockets": [{"group": 0, "sColour": "B"} for _ in range(6)],
                    "enchantMods": ["Allocates [mana_rmc_regen_notable1878|Righteous Decree]"],
                    "implicitMods": [{"description": "+50% to Global Critical Strike Multiplier"}],
                    "explicitMods": [
                        {"description": "Trigger level 20 [TemporalMirage::20|Temporal Mirage] on using a Movement Attack"},
                        {"description": "+2 to Level of all Movement Skill Gems"},
                    ],
                    "socketedItems": [
                        {"baseType": "Firestorm of Meteors", "typeLine": "Firestorm of Meteors", "socket": 0, "support": False, "properties": [{"name": "Level", "values": [["21", 0]]}]},
                        {"baseType": "Archmage Support", "typeLine": "Archmage Support", "socket": 1, "support": True, "properties": [{"name": "Level", "values": [["21", 0]]}]},
                        {"baseType": "Increased Critical Strikes Support", "typeLine": "Increased Critical Strikes Support", "socket": 2, "support": True, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                        {"baseType": "Increased Critical Damage Support", "typeLine": "Increased Critical Damage Support", "socket": 3, "support": True, "properties": [{"name": "Level", "values": [["20", 0]]}]},
                        {"baseType": "Invert the Rules Support", "typeLine": "Invert the Rules Support", "socket": 4, "support": True, "properties": [{"name": "Level", "values": [["3", 0]]}]},
                        {"baseType": "Greater Spell Cascade Support", "typeLine": "Greater Spell Cascade Support", "socket": 5, "support": True, "properties": [{"name": "Level", "values": [["3", 0]]}]},
                    ],
                },
            ],
        },
    }
    xml_text = character_bundle_to_pob_xml(bundle)
    validate_build_xml(xml_text, expected_game="poe1")
    root = ET.fromstring(xml_text)
    build_el = root.find("Build")
    assert build_el is not None
    # Group #3 is the 6L Weapon Firestorm of Meteors (beats Helmet Shield Charge #1 & Boots 4L Spectre #2)
    assert build_el.attrib["mainSocketGroup"] == "3"
    spec_el = root.find(".//Spec")
    assert spec_el is not None
    assert spec_el.attrib.get("clusterHashFormatVersion") == "2"
    assert "65536" in spec_el.attrib["nodes"]
    all_item_text = "\n".join(el.text or "" for el in root.findall(".//Item"))
    assert "+80 to maximum Life" in all_item_text
    assert "{crafted}12% increased Cooldown Recovery Rate" in all_item_text
    assert "{fractured}+40% to Fire Resistance" in all_item_text
    assert "{crafted}Allocates Righteous Decree" in all_item_text
    assert "Trigger level 20 Temporal Mirage on using a Movement Attack" in all_item_text
    assert "Implicits: 2" in all_item_text


def test_cn_330_dict_mods_gem_suffix_variants_and_permanent_league_sort() -> None:
    # P2: CN permanent leagues (`永久`, `永久（独狼）`, `标准`) sort AFTER `S31赛季`, dirty `/characters1` parses cleanly
    t_dirty = parse_profile_source(
        "https://poe.game.qq.com/account/view-profile/%E6%B5%8B%E8%AF%95%E8%B4%A6%E5%8F%B7-3499/characters1",
        realm="cn",
    )
    assert t_dirty.server == "cn"
    assert t_dirty.account_name == "测试账号#3499"
    # Spoofed lookalike host must NOT be treated as poe.game.qq.com
    t_spoof = parse_profile_source("https://poe.game.qq.com.evil.example/account/view-profile/foo-1234")
    assert t_spoof.server != "cn"
    ordered = sort_profile_characters(
        [
            {"name": "老号满级", "league": "永久", "level": 100},
            {"name": "独狼永久", "league": "永久（独狼）", "level": 99},
            {"name": "标准旧号", "league": "标准", "level": 98},
            {"name": "新赛季号", "league": "S31赛季", "level": 96},
        ]
    )
    assert ordered[0]["name"] == "新赛季号"

    # P0 + P1: CN 3.30 dict mods + Empower `(辅)` / `辅助` / `(强辅)` gem translation + unknown gem warning
    d = Zh2EnDictionary.from_entries(
        bases={"Heavy Belt": "重革腰带"},
        gems={
            "Empower": "赋予辅助",
            "Awakened Empower": "赋予（强辅）",
            "Raise Spectre": "召唤灵体",
        },
        stats={"+{0} to Strength": "+{0} 力量", "+{0} to maximum Life": "+{0} 最大生命"},
    )
    assert d.translate_gem("赋予（辅）", is_support=True) == "Empower"
    assert d.translate_gem("赋予（强辅）", is_support=True) == "Awakened Empower"

    cn_bundle = {
        "char": {"name": "火天帝", "league": "S31赛季", "class": "追猎者", "level": 96},
        "passiveSkills": {"hashes": [10], "items": []},
        "items": {
            "items": [
                {
                    "inventoryId": "Belt",
                    "frameType": 2,
                    "name": "测试腰带",
                    "baseType": "重革腰带",
                    "typeLine": "重革腰带",
                    "ilvl": 85,
                    "sockets": [{"group": 0, "sColour": "B"}, {"group": 0, "sColour": "R"}, {"group": 0, "sColour": "B"}],
                    "implicitMods": [{"description": "+35 力量"}],
                    "explicitMods": [{"description": "+95 最大生命", "flags": {"crafted": True}}],
                    "socketedItems": [
                        {"baseType": "召唤灵体", "typeLine": "召唤灵体", "socket": 0, "support": False, "properties": [{"name": "等级", "values": [["21", 0]]}]},
                        {"baseType": "赋予（辅）", "typeLine": "赋予（辅）", "socket": 1, "support": True, "properties": [{"name": "等级", "values": [["3", 0]]}]},
                        {"baseType": "未知新辅助（辅）", "typeLine": "未知新辅助（辅）", "socket": 2, "support": True, "properties": [{"name": "等级", "values": [["3", 0]]}]},
                    ],
                }
            ]
        },
    }
    en_bundle, warns = translate_cn_bundle(cn_bundle, d)
    xml_text = character_bundle_to_pob_xml(en_bundle)
    assert "+35 to Strength" in xml_text
    assert "{crafted}+95 to maximum Life" in xml_text
    assert 'nameSpec="Empower"' in xml_text
    assert any("[CN-TRANSLATE-WARN] Gem: 未知新辅助（辅）" in w for w in warns)


def test_coderabbit_major_fixes_league_filter_shell_safe_proxy_cn_hash_viscera_and_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.profile_import.site import format_profile_markdown, make_site_client

    # 1. --league with zero matches returns [] instead of falling back to unfiltered Standard character
    std_only = [{"name": "StdOnly", "league": "Standard", "class": "Witch", "level": 90}]
    assert sort_profile_characters(std_only, league_filter="Covens") == []
    target_miss = parse_profile_source("https://www.pathofexile.com/account/view-profile/alice-1234", league="Covens")
    with pytest.raises(DaemonRequestError, match="未找到"):
        resolve_target_character(std_only, target=target_miss)

    # 2. Case-insensitive HTTPS:// scheme parses query selectors, and format_profile_markdown strips raw query injection
    t_inj = parse_profile_source("HTTPS://www.pathofexile.com/account/view-profile/alice-1234?realm=xbox&junk=$(id)")
    assert t_inj.realm == "xbox"
    md = format_profile_markdown(t_inj, [{"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89}])
    assert "$(id)" not in md
    assert "'https://www.pathofexile.com/account/view-profile/alice-1234'" in md

    # 3. make_site_client with proxy constructs cleanly
    c = make_site_client(proxy="http://127.0.0.1:7890")
    c.close()

    # 4. cn_zh2en: `#` template matches concrete numbers, and unmatched Awakened (`强辅`) is NOT downgraded to normal
    d = Zh2EnDictionary.from_entries(
        gems={"Empower": "赋予辅助"},
        stats={"+#% to Fire Resistance": "+#% 火焰抗性", "+{0}% to Cold Resistance": "+{0}% 冰霜抗性"},
    )
    assert d.translate_mod("+30% 火焰抗性") == ("+30% to Fire Resistance", True)
    assert d.translate_mod("+30% 冰霜抗性") == ("+30% to Cold Resistance", True)
    assert d.translate_gem("赋予（强辅）", is_support=True) == "赋予（强辅）"
    assert d.translate_gem("赋予（辅）", is_support=True) == "Empower"

    # 5. Viscera equipment slots survive XML conversion
    visc_xml = character_bundle_to_pob_xml(
        {
            "char": {"name": "V", "class": "Witch", "level": 90},
            "passiveSkills": {"hashes": [], "items": []},
            "items": {
                "items": [
                    {
                        "inventoryId": "VisceraHead",
                        "frameType": 2,
                        "name": "Graft Skull",
                        "baseType": "Hubris Circlet",
                        "typeLine": "Hubris Circlet",
                        "explicitMods": [{"description": "+50 to maximum Life"}],
                    }
                ]
            },
        }
    )
    assert 'name="Head"' in visc_xml and "+50 to maximum Life" in visc_xml

    # 6. POSIX import_character_json failure restores previous live build via import_code
    class _FailingPosixWorker(_FakeWorker):
        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
            self.calls.append((method, dict(params or {})))
            if method == "import_character_json":
                raise RuntimeError("boom during posix import")
            return {"ok": True}

    fw = _FailingPosixWorker(caps={"importCharacterJson": True})
    fw.last_good_code = "PREV_BUILD_CODE"
    monkeypatch.setattr(
        "pob_cli.profile_import.fetch_intl_characters",
        lambda *_a, **_k: ([{"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89}], False),
    )
    monkeypatch.setattr(
        "pob_cli.profile_import.fetch_intl_character_bundle",
        lambda *_a, **_k: {
            "char": {"name": "HeroA", "league": "Covens", "class": "Assassin", "level": 89},
            "passiveSkills": {"hashes": [1], "items": []},
            "items": {"items": []},
        },
    )
    with pytest.raises(RuntimeError, match="boom"):
        importers.load_build_into_worker(
            fw,
            source="https://www.pathofexile.com/account/view-profile/alice-1234",
            char_name="HeroA",
        )
    assert any(m == "import_code" and p.get("code") == "PREV_BUILD_CODE" for m, p in fw.calls)
