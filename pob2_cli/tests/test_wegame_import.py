# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic unit & CLI tests for T2.2c CN WeGame PoE2 cookie-free share importer.

All fixtures are 100% synthetic — zero real player share codes, openids, account names, or exports.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob2_cli.cli import main as pob2_main
from pob2_cli.errors import CrossGameHallucinationError, DaemonRequestError
from pob2_cli.guardrails import validate_build_xml
from pob2_cli.importers import (
    DEFAULT_QUEST_BOOLEAN_KEYS,
    DEFAULT_QUEST_OPTION_KEYS,
    PASSIVE_JEWEL_SLOTS,
    WEGAME_METHODS,
    GemCandidate,
    WeGameTranslator,
    clean_wegame_text,
    convert_wegame_bundle_to_pob2_xml,
    fetch_wegame_share,
    parse_leading_int,
    parse_wegame_share_url,
    sanitize_wegame_responses,
)

SYNTHETIC_SHARE_CODE = "SynthTestCode_0123456789ABCDEF"
SYNTHETIC_SHARE_URL = f"https://www.wegame.com.cn/helper/poe2/#/share/{SYNTHETIC_SHARE_CODE}"


def _make_raw_five_responses(*, class_name: str = "猎巫人", double_escape_jewels: bool = True) -> dict[str, dict[str, Any]]:
    """Build 100% synthetic 5-endpoint WeGame responses covering all 5 real-device pitfalls."""
    jewel_entries = [
        {
            "socket_id": "PassiveJewel_4",
            "jewel": {
                "id": "Metadata/Items/Jewels/JewelStr",
                "name": "红宝石",
                "display_name": "合成红玉",
                "rarity": 2,
                "mod_descriptions": [
                    {"values": ["最大生命提高 6 (5-7)%", "攻击伤害提高 14 (10-15)%"]}
                ],
            },
        },
        {
            "socket_id": "PassiveJewel_12",
            "jewel": {
                "id": "Metadata/Items/Jewels/JewelDex",
                "name": "翠绿珠宝",
                "display_name": "合成翠玉",
                "rarity": 2,
                "mod_descriptions": [
                    {"values": ["速度提高 4 (4-8)% (local)"]}
                ],
            },
        },
        {
            "socket_id": "PassiveJewel_16",
            "jewel": {
                "id": "Metadata/Items/Jewels/JewelInt",
                "name": "蓝宝石",
                "display_name": "合成蓝玉",
                "rarity": 2,
                "mod_descriptions": [{"values": ["能量护盾提高 15 (10-20)%"]}],
            },
        },
        {
            "socket_id": "PassiveJewel_17",
            "jewel": {
                "id": "Metadata/Items/Jewels/JewelDiamond",
                "name": "钻石",
                "display_name": "合成钻石",
                "rarity": 3,
                "mod_descriptions": [{"values": ["+10% 所有元素抗性"]}],
            },
        },
    ]
    jewel_json = json.dumps(jewel_entries, ensure_ascii=False)
    if double_escape_jewels:
        jewel_json = jewel_json.replace('"', '\\"')

    equipments: list[dict[str, Any]] = [
        # Main weapon with rune sockets, mixed dict/str mods, special flags, Shaman Bonded line, quality markup
        {
            "inventoryId": "Weapon",
            "baseType": "双子十字弓",
            "name": "合成弩炮",
            "frameType": 2,
            "ilvl": 82,
            "corrupted": True,
            "properties": [
                {"type": 6, "name": "[Quality|品质]", "values": [["+20%", 1]]},
            ],
            "requirements": [
                {"name": "等级", "values": [["78", 0]]},
                {"name": "力量", "values": [["104", 0]]},
                {"name": "敏捷", "values": [["104", 0]]},
            ],
            "sockets": [{"type": "rune"}, {"type": "rune"}, {"type": "jewel"}],
            "socketedItems": [
                {"socket": 0, "baseType": "高级冰川符文"},
                {"socket": 1, "baseType": "法瑞尔的狩猎符文"},
            ],
            "enchantMods": ["攻击速度提高 8%"],
            "runeMods": [
                "[Attack|攻击]附加 12 - 20 [Physical|物理]伤害",
                "[ShamanOnlyMods|羁绊]：所有[Attack|攻击]技能等级 +1",
            ],
            "implicitMods": [{"description": "物理伤害提高 20%", "flags": {}}],
            "explicitMods": [
                {"description": "+120 最大生命", "flags": {"fractured": True}},
                {"description": "+35% 火焰抗性", "flags": {}},
                {"description": "+30% 闪电抗性", "flags": {}},
                {"description": "+25 力量", "flags": {}},
            ],
            "fracturedMods": ["+120 最大生命"],
            "craftedMods": ["+35% 火焰抗性"],
            "desecratedMods": ["+30% 闪电抗性"],
            "mutatedMods": ["+25 力量"],
            # Dummy PII field on item to verify item sanitizer strips unknown fields
            "account_name": "SYNTH_ITEM_PII",
        },
        # Offhand + Weapon2 + Offhand2 (dual weapon set)
        {
            "inventoryId": "Offhand",
            "baseType": "箭袋",
            "name": "合成箭袋",
            "frameType": 2,
            "ilvl": 80,
            "explicitMods": [{"description": "+80 最大生命", "flags": {}}],
        },
        {
            "inventoryId": "Weapon2",
            "baseType": "精绝法杖",
            "name": "副手法杖",
            "frameType": 2,
            "ilvl": 81,
            "sockets": [{"type": "rune"}],
            "socketedItems": [{"socket": 0, "baseType": "萨奇沃的苍空符文"}],
            "explicitMods": [{"description": "法术伤害提高 85%", "flags": {}}],
        },
        {
            "inventoryId": "Offhand2",
            "baseType": "塔盾",
            "name": "副手盾牌",
            "frameType": 2,
            "ilvl": 80,
            "properties": [{"type": 16, "name": "护甲", "values": [["1450", 1]]}],
            "explicitMods": [{"description": "+40% 冰霜抗性", "flags": {}}],
        },
        # Ring with full-width catalyst quality
        {
            "inventoryId": "Ring",
            "baseType": "裂隙戒指",
            "name": "合成火戒",
            "frameType": 2,
            "ilvl": 82,
            "properties": [{"type": 6, "name": "品质（火焰词缀）", "values": [["+40%", 1]]}],
            "explicitMods": [{"description": "+42% 火焰抗性", "flags": {}}],
        },
        # Amulet with half-width catalyst quality
        {
            "inventoryId": "Amulet",
            "baseType": "太阳项链",
            "name": "合成项链",
            "frameType": 2,
            "ilvl": 81,
            "properties": [{"type": 6, "name": "品质(生命词缀)", "values": [["+20%", 1]]}],
            "explicitMods": [{"description": "+110 最大生命", "flags": {}}],
        },
        # Flasks (x=0..1) and Charms (x=2..4)
        {"inventoryId": "Flask", "x": 0, "baseType": "终极生命药剂", "frameType": 1, "ilvl": 75, "explicitMods": []},
        {"inventoryId": "Flask", "x": 1, "baseType": "终极魔力药剂", "frameType": 1, "ilvl": 75, "explicitMods": []},
        {
            "inventoryId": "Flask", "x": 2, "baseType": "接地咒符", "frameType": 1, "ilvl": 70,
            "implicitMods": ["当你被感电时使用"], "explicitMods": [],
        },
        {
            "inventoryId": "Flask", "x": 3, "baseType": "解毒咒符", "frameType": 1, "ilvl": 70,
            "implicitMods": ["当你中毒时使用"], "explicitMods": [],
        },
        {"inventoryId": "Flask", "x": 4, "baseType": "黄金咒符", "frameType": 1, "ilvl": 70, "explicitMods": []},
        # Incursion limbs
        {"inventoryId": "IncursionArmRight", "baseType": "合成右臂", "frameType": 2, "ilvl": 79, "explicitMods": []},
        {"inventoryId": "IncursionArmLeft", "baseType": "合成左臂", "frameType": 2, "ilvl": 79, "explicitMods": []},
        {"inventoryId": "IncursionLegRight", "baseType": "合成右腿", "frameType": 2, "ilvl": 79, "explicitMods": []},
        {"inventoryId": "IncursionLegLeft", "baseType": "合成左腿", "frameType": 2, "ilvl": 79, "explicitMods": []},
    ]

    return {
        "GetRoleInfo": {
            "result": {"error_code": 0, "error_message": ""},
            "nick_name": "SYNTH_NICK_DO_NOT_LEAK",
            "share_code": SYNTHETIC_SHARE_CODE,
            "role": {
                "level": 94,
                "class_name": class_name,
                "openid": "SYNTH_OPENID_99999",
                "account_name": "SYNTH_ACCOUNT_99999",
                "role_id": "SYNTH_ROLE_ID_99999",
                "name": "SYNTH_CHAR_NAME_99999",
            },
        },
        "GetEquipments": {
            "result": {"error_code": 0, "error_message": ""},
            "equipments": equipments,
        },
        "GetTalentTree": {
            "result": {"error_code": 0, "error_message": ""},
            "talent_tree": {
                "hashes": [1001, 1002, 1003, 60735, 46882, 54127, 32763],
                "specialisations": {"set1": [2001, 2002], "set2": [3001, 3002]},
                "skill_overrides": {
                    "1001": {"grantedStrength": 5},
                    "1002": {"grantedDexterity": 5},
                    "1003": {"grantedIntelligence": 5},
                },
                "quest_stats": [
                    "+100 [Spirit|精魂]",
                    "+10% 火焰抗性",
                    "+10% 冰霜抗性",
                    "+10% 闪电抗性",
                    "[Charm|咒符]栏 +1",
                    "48 个[BrokenFace|毁面首领]",
                ],
                # Radius metadata dict on talent_tree must NEVER be confused with GetJewels.jewel_data string
                "jewel_data": {"0": {"radius": 1500, "type": "JewelStr"}},
            },
        },
        "GetJewels": {
            "result": {"error_code": 0, "error_message": ""},
            "jewel_data": jewel_json,
        },
        "GetSkills": {
            "result": {"error_code": 0, "error_message": ""},
            "skills": [
                {
                    "baseType": "冰霜新星",
                    "support": False,
                    "corrupted": True,
                    "properties": [
                        {"type": 5, "name": "等级", "values": [["20(最高等级)", 0]]},
                        {"type": 6, "name": "品质", "values": [["+20%", 1]]},
                    ],
                    "socketedItems": [
                        {
                            "baseType": "快速攻击 I",
                            "support": True,
                            "properties": [{"type": 5, "name": "等级", "values": [["1", 0]]}],
                        },
                    ],
                },
                {
                    # Collides in Gems.lua with UniqueBreachLightningBoltPlayer -> must pick regular gem!
                    "baseType": "闪电",
                    "support": False,
                    "properties": [{"type": 5, "name": "等级", "values": [["19", 0]]}],
                    "socketedItems": [],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1. URL validation & cross-game PoE1 blocking
# ---------------------------------------------------------------------------


def test_parse_wegame_url_valid_and_rejects_malformed() -> None:
    assert parse_wegame_share_url(SYNTHETIC_SHARE_URL) == SYNTHETIC_SHARE_CODE
    assert parse_wegame_share_url(SYNTHETIC_SHARE_URL + "/") == SYNTHETIC_SHARE_CODE

    bad_urls = [
        f"http://www.wegame.com.cn/helper/poe2/#/share/{SYNTHETIC_SHARE_CODE}",
        f"https://www.wegame.com.cn:443/helper/poe2/#/share/{SYNTHETIC_SHARE_CODE}",
        f"https://user:pass@www.wegame.com.cn/helper/poe2/#/share/{SYNTHETIC_SHARE_CODE}",
        f"https://evil.example.com/helper/poe2/#/share/{SYNTHETIC_SHARE_CODE}",
        f"https://www.wegame.com.cn/helper/poe1/#/share/{SYNTHETIC_SHARE_CODE}",
        "https://www.wegame.com.cn/helper/poe2/#/share/too_short",
        "https://www.wegame.com.cn/helper/poe2/#/share/bad!char!1234567890",
    ]
    for bad in bad_urls:
        with pytest.raises(DaemonRequestError):
            parse_wegame_share_url(bad)


def test_poe1_cn_profile_url_blocked_with_exit_code_1() -> None:
    with pytest.raises(CrossGameHallucinationError) as exc:
        parse_wegame_share_url("https://poe.game.qq.com/character-window/get-items?character=demo")
    assert exc.value.exit_code == 1
    assert exc.value.detected_game == "poe1"


# ---------------------------------------------------------------------------
# 2. Concurrent cookie-free fetch, redirect rejection, atomicity, PII strip
# ---------------------------------------------------------------------------


def test_fetch_wegame_share_cookie_free_concurrent_and_strips_pii() -> None:
    raw = _make_raw_five_responses()
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "cookie" not in {k.lower() for k in request.headers}
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"share_code": SYNTHETIC_SHARE_CODE, "area": 0, "from_src": "poe2_helper"}
        method = request.url.path.rsplit("/", 1)[-1]
        seen_methods.append(method)
        return httpx.Response(200, json=raw[method])

    bundle = fetch_wegame_share(SYNTHETIC_SHARE_URL, transport=httpx.MockTransport(handler))
    assert set(seen_methods) == set(WEGAME_METHODS)
    assert bundle["format"] == "wegame"
    assert bundle["version"] == 1
    assert bundle["role"] == {"level": 94, "class_name": "猎巫人"}

    # Verify ZERO PII tokens survive anywhere in sanitized bundle
    serialized = json.dumps(bundle, ensure_ascii=False)
    for forbidden in (
        "SYNTH_OPENID_99999",
        "SYNTH_ACCOUNT_99999",
        "SYNTH_ROLE_ID_99999",
        "SYNTH_CHAR_NAME_99999",
        "SYNTH_NICK_DO_NOT_LEAK",
        "SYNTH_ITEM_PII",
        SYNTHETIC_SHARE_CODE,
        "openid",
        "account_name",
        "role_id",
        "nick_name",
        "share_code",
    ):
        assert forbidden not in serialized

    # Verify talent_tree.jewel_data radius dict was separated from GetJewels.jewel_data string
    assert isinstance(bundle["jewel_data"], str)
    assert "jewel_data" not in bundle["talent_tree"]
    assert isinstance(bundle["talent_tree"].get("jewel_radius_meta"), dict)


def test_fetch_wegame_share_rejects_redirect_and_partial_error_atomically() -> None:
    raw = _make_raw_five_responses()

    # Case A: 302 redirect on GetSkills must fail immediately (never follow redirect)
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "GetSkills":
            return httpx.Response(302, headers={"Location": "https://example.com/login"})
        return httpx.Response(200, json=raw[method])

    with pytest.raises(DaemonRequestError, match="HTTP 302"):
        fetch_wegame_share(SYNTHETIC_SHARE_URL, transport=httpx.MockTransport(redirect_handler))

    # Case B: error_code != 0 on GetJewels aborts entire import without partial build
    raw_err = _make_raw_five_responses()
    raw_err["GetJewels"] = {"result": {"error_code": 8000001, "error_message": "Invalid"}, "jewel_data": ""}
    with pytest.raises(DaemonRequestError, match="8000001"):
        sanitize_wegame_responses(raw_err)


# ---------------------------------------------------------------------------
# 3. Rich text cleaning, ModRunes.lua CN reverse lookup, gem disambiguation
# ---------------------------------------------------------------------------


def test_clean_wegame_text_and_leading_int() -> None:
    assert clean_wegame_text("[Attack|攻击]附加 4 - 9 [Physical|物理]伤害") == "攻击附加 4 - 9 物理伤害"
    assert clean_wegame_text("速度提高 4 (4-8)% (local)") == "速度提高 4% (local)"
    assert parse_leading_int("20(最高等级)") == 20
    assert parse_leading_int("+20%") == 20
    assert parse_leading_int("not_a_num") is None


def test_modrunes_cn_reverse_lookup_covers_real_cn_names() -> None:
    tr = WeGameTranslator()
    assert tr.translate_rune("高级冰川符文") == ("Greater Glacial Rune", True)
    assert tr.translate_rune("法瑞尔的狩猎符文") == ("Farrul's Rune of the Hunt", True)
    assert tr.translate_rune("萨奇沃的苍空符文") == ("Saqawal's Rune of the Sky", True)
    assert tr.translate_rune("高階鍛鐵符文") == ("Greater Iron Rune", True)
    assert tr.translate_rune("埃曼纳姆的凝视") == ("Amanamu's Gaze", True)
    # Unknown rune preserved verbatim with matched=False (never silently dropped)
    assert tr.translate_rune("未知测试符文") == ("未知测试符文", False)


def test_gem_disambiguation_prefers_regular_gem_over_item_granted_variant() -> None:
    tr = WeGameTranslator(
        extra_gem_zh={
            ("冰霜之捷", False): [
                GemCandidate(
                    name="Herald of Ice", is_support=False,
                    gem_id="Metadata/Items/Gems/SkillGemHeraldOfIce",
                    skill_id="HeraldOfIcePlayer", variant_id="HeraldOfIce", is_item_granted=False,
                ),
                GemCandidate(
                    name="Herald of Ice", is_support=False,
                    gem_id="Metadata/Items/Gems/SkillGemUniqueComingCalamityHeraldOfIce",
                    skill_id="UniqueComingCalamityHeraldOfIce", variant_id="UniqueHeraldOfIce", is_item_granted=True,
                ),
            ]
        }
    )
    # Real Gems.lua collision: Lightning Bolt vs UniqueBreachLightningBoltPlayer
    res_lb = tr.translate_gem("闪电", is_support=False)
    assert res_lb.candidate is not None
    assert res_lb.candidate.skill_id == "LightningBoltPlayer"

    # Synthetic Coming Calamity collision: must choose regular HeraldOfIcePlayer
    res_hoi = tr.translate_gem("冰霜之捷", is_support=False)
    assert res_hoi.candidate is not None
    assert res_hoi.candidate.skill_id == "HeraldOfIcePlayer"


# ---------------------------------------------------------------------------
# 4. Full conversion: 5 pitfalls, PoE2 slots, WS1/WS2, PassiveJewel, quests, Shaman
# ---------------------------------------------------------------------------


def test_full_wegame_conversion_to_valid_pob2_xml() -> None:
    bundle = sanitize_wegame_responses(_make_raw_five_responses(class_name="猎巫人", double_escape_jewels=True))
    result = convert_wegame_bundle_to_pob2_xml(bundle)

    assert result.class_name == "Mercenary"
    assert result.ascendancy_name == "Witchhunter"
    assert result.level == 94

    # Must pass PoE2 XML guardrail & fail PoE1 XML guardrail
    validate_build_xml(result.xml, expected_game="poe2")
    with pytest.raises(CrossGameHallucinationError):
        validate_build_xml(result.xml, expected_game="poe1")

    root = ET.fromstring(result.xml)
    assert root.tag == "PathOfBuilding2"

    # Tree: WS1/WS2 + AttributeOverride + PassiveJewel_<4,12,16,17> exact table mapping
    spec = root.find("./Tree/Spec")
    assert spec is not None
    assert spec.attrib["treeVersion"] == "0_5"
    assert spec.attrib["classId"] == "9"
    assert spec.attrib["ascendancyInternalId"] == "Mercenary2"
    ws1 = spec.find("./WeaponSet1")
    ws2 = spec.find("./WeaponSet2")
    assert ws1 is not None and ws1.attrib["nodes"] == "2001,2002"
    assert ws2 is not None and ws2.attrib["nodes"] == "3001,3002"

    attr = spec.find("./Overrides/AttributeOverride")
    assert attr is not None
    assert attr.attrib["strNodes"] == "1001"
    assert attr.attrib["dexNodes"] == "1002"
    assert attr.attrib["intNodes"] == "1003"

    socket_node_ids = [int(s.attrib["nodeId"]) for s in spec.findall("./Sockets/Socket")]
    assert socket_node_ids == [
        PASSIVE_JEWEL_SLOTS[4],
        PASSIVE_JEWEL_SLOTS[12],
        PASSIVE_JEWEL_SLOTS[16],
        PASSIVE_JEWEL_SLOTS[17],
    ]
    assert socket_node_ids == [60735, 46882, 54127, 32763]

    # Equipment slots: dual weapon sets, Flask 1..2, Charm 1..3, Incursion limbs
    items_el = root.find("./Items")
    assert items_el is not None and items_el.attrib["useSecondWeaponSet"] == "true"
    slot_names = {s.attrib["name"] for s in items_el.findall("./ItemSet/Slot")}
    for expected_slot in (
        "Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap",
        "Ring 1", "Amulet",
        "Flask 1", "Flask 2", "Charm 1", "Charm 2", "Charm 3",
        "Arm 1", "Arm 2", "Leg 1", "Leg 2",
    ):
        assert expected_slot in slot_names

    # Item 1 (Weapon): runes, 8 mod tags with dedup, quality, non-Shaman strips Bonded line
    weapon_text = (items_el.find("./Item[@id='1']").text or "")
    assert "Quality: 20" in weapon_text
    assert "Rune: Greater Glacial Rune" in weapon_text
    assert "Rune: Farrul's Rune of the Hunt" in weapon_text
    assert "{enchant}8% increased Attack Speed" in weapon_text
    assert "{enchant}{rune}Adds 12 to 20 Physical Damage to Attacks" in weapon_text
    assert "{fractured}+120 to maximum Life" in weapon_text
    assert "{crafted}+35% to Fire Resistance" in weapon_text
    assert "{desecrated}+30% to Lightning Resistance" in weapon_text
    assert "{mutated}+25 to Strength" in weapon_text
    assert "Bonded:" not in weapon_text
    assert "羁绊" not in weapon_text
    assert "Corrupted" in weapon_text
    # Ensure explicit mod wasn't duplicated when also listed in fracturedMods/craftedMods
    assert weapon_text.count("+120 to maximum Life") == 1
    assert weapon_text.count("+35% to Fire Resistance") == 1

    # Catalyst quality full-width and half-width
    all_item_texts = "\n".join((el.text or "") for el in items_el.findall("./Item"))
    assert "Quality (Fire Modifiers): +40%" in all_item_texts
    assert "Quality (Life Modifiers): +20%" in all_item_texts
    assert "Rune: Saqawal's Rune of the Sky" in all_item_texts

    # Skills: leading-int level & quality, regular Lightning Bolt chosen over Unique variant
    gems = root.findall("./Skills/SkillSet/Skill/Gem")
    gem_by_skill = {g.attrib["skillId"]: g for g in gems}
    assert "IceNovaPlayer" in gem_by_skill
    assert gem_by_skill["IceNovaPlayer"].attrib["level"] == "20"
    assert gem_by_skill["IceNovaPlayer"].attrib["quality"] == "20"
    assert gem_by_skill["IceNovaPlayer"].attrib["corrupted"] == "true"
    assert "LightningBoltPlayer" in gem_by_skill
    assert "UniqueBreachLightningBoltPlayer" not in gem_by_skill

    # Quest config: all 17 defaults disabled; actual quest_stats in customMods; BrokenFace filtered
    cfg_inputs = {el.attrib["name"]: el.attrib for el in root.findall("./Config/ConfigSet/Input")}
    for k in DEFAULT_QUEST_BOOLEAN_KEYS:
        assert cfg_inputs[k].get("boolean") == "false"
    for k in DEFAULT_QUEST_OPTION_KEYS:
        assert cfg_inputs[k].get("string") == ""
    custom_mods = cfg_inputs["customMods"]["string"]
    assert "+100 to Spirit" in custom_mods
    assert "+10% to Fire Resistance" in custom_mods
    assert "+1 Charm Slot" in custom_mods
    assert "毁面首领" not in custom_mods
    assert "BrokenFace" not in custom_mods

    # Notes contain mandatory [import] warnings & checklist
    notes_text = (root.find("./Notes").text or "")
    assert "[import] Shaman-only Bonded rune modifier stripped" in notes_text
    assert "[import] Equipment-socketed jewels require manual review." in notes_text
    assert "[import] WeGame does not export skill weapon-set bindings" in notes_text
    assert "overcap buffer" in notes_text


def test_shaman_preserves_bonded_rune_modifier() -> None:
    bundle = sanitize_wegame_responses(_make_raw_five_responses(class_name="萨满", double_escape_jewels=False))
    result = convert_wegame_bundle_to_pob2_xml(bundle)
    assert result.class_name == "Druid"
    assert result.ascendancy_name == "Shaman"
    assert "{enchant}{rune}Bonded: +1 to Level of all Attack Skills" in result.xml
    assert not any("Shaman-only Bonded" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 5. CLI integration: pob2 --help mounts import, JSON/XML out, PoE1 exit 1
# ---------------------------------------------------------------------------


def test_cli_import_mounted_and_end_to_end(tmp_path: Path) -> None:
    runner = CliRunner()

    help_res = runner.invoke(pob2_main, ["--help"])
    assert help_res.exit_code == 0
    for cmd_name in ("craft", "item", "ground", "import"):
        assert cmd_name in help_res.output

    bundle = sanitize_wegame_responses(_make_raw_five_responses())
    bundle_file = tmp_path / "synth_wegame.json"
    bundle_file.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    out_xml = tmp_path / "imported.xml"

    # JSON output + XML file write
    res = runner.invoke(pob2_main, ["import", str(bundle_file), "--out", str(out_xml), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["game"] == "poe2"
    assert data["ok"] is True
    assert data["className"] == "Mercenary"
    assert data["ascendancyName"] == "Witchhunter"
    assert out_xml.is_file()
    validate_build_xml(out_xml.read_text(encoding="utf-8"), expected_game="poe2")

    # PoE1 URL -> exit 1
    bad_url = runner.invoke(pob2_main, ["import", "https://poe.game.qq.com/character-window/get-items"])
    assert bad_url.exit_code == 1

    # PoE1 XML file -> exit 1
    poe1_xml = tmp_path / "poe1_bad.xml"
    poe1_xml.write_text('<PathOfBuilding><Build className="Templar"/></PathOfBuilding>', encoding="utf-8")
    bad_xml = runner.invoke(pob2_main, ["import", str(poe1_xml)])
    assert bad_xml.exit_code == 1
