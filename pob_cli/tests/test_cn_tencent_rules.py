"""Offline + optional real-dict unit tests for Tencent CN cluster, jump/timeless, tattoo, and runegraft rules."""

from __future__ import annotations

import re

import pytest
from engine_paths import find_runnable_engine, has_zh_data

from pob_cli.game_data.tables_cn_tencent import TENCENT_RUNEGRAFT_ALIASES
from pob_cli.profile_import import Zh2EnDictionary, translate_cn_bundle
from pob_cli.profile_import.cn_tencent_rules import translate_property_value
from pob_cli.profile_import.cn_tencent_tables import CLUSTER_BASE_ENCHANT_MAP


def test_tencent_property_values_and_cluster_enchants() -> None:
    assert len(set(CLUSTER_BASE_ENCHANT_MAP.values())) == 60
    assert translate_property_value("Radius", "小") == "Small"
    assert translate_property_value("Radius", "中") == "Medium"
    assert translate_property_value("Radius", "大") == "Large"
    assert translate_property_value("Radius", "可变（极小）") == "Variable"
    assert translate_property_value("Radius", "可变(巨型)") == "Variable"
    assert translate_property_value("Limited to", "1 史实") == "1 Historic"

    d = Zh2EnDictionary.from_entries(
        passives={"Renewal": "复兴", "Whispers of Doom": "灭世之语"},
        stats={"+{0} to maximum Life": "+{0} 最大生命"},
    )
    assert d.translate_mod("所有元素抗性 15%") == ("+15% to all Elemental Resistances", True)
    assert d.translate_mod("所有元素抗性 -15%") == ("-15% to all Elemental Resistances", True)
    cn_bundle = {
        "char": {"name": "CNTest", "class": "守护者", "level": 97},
        "items": {"items": []},
        "passiveSkills": {
            "hashes": [],
            "items": [
                {
                    "x": 0, "frameType": 2, "baseType": "大型星团珠宝",
                    "enchantMods": [
                        "增加 12 个天赋技能", "其中 2 个增加的天赋为珠宝槽",
                        "增加的小天赋获得：锤类或短杖攻击造成的击中和异常状态伤害提高 12%\n长杖攻击造成的击中和异常状态伤害提高 12%",
                    ],
                    "explicitMods": ["其中 1 个增加的天赋为复兴", "增加的小天赋效果提高 35%", "增加的小天赋还获得：+10 最大生命"],
                },
                {
                    "x": 1, "frameType": 3, "name": "腐败之种", "baseType": "菌丝核心",
                    "enchantMods": ["其中 1 个增加的天赋为灭世之语"],
                },
                {
                    "x": 2, "frameType": 3, "name": "妄语", "baseType": "大型星团珠宝",
                    "explicitMods": ["增加 3 个珠宝插槽天赋", "增加 1 个无特殊效果的小天赋"],
                },
                {
                    "x": 3, "frameType": 2, "baseType": "大型星团珠宝",
                    "enchantMods": [
                        "增加的小天赋获得：[stat|锤类或短杖攻击造成的击中和异常状态伤害提高 12%]\n[stat|长杖攻击造成的击中和异常状态伤害提高 12%]",
                    ],
                },
            ],
        },
    }
    en, warns = translate_cn_bundle(cn_bundle, d)
    assert warns == []
    large = en["passiveSkills"]["items"][0]
    assert large["baseType"] == "Large Cluster Jewel"
    assert large["enchantMods"] == [
        "Adds 12 Passive Skills",
        "2 Added Passive Skills are Jewel Sockets",
        "Added Small Passive Skills grant: Staff Attacks deal 12% increased Damage with Hits and Ailments",
        "Added Small Passive Skills grant: Mace or Sceptre Attacks deal 12% increased Damage with Hits and Ailments",
    ]
    assert en["passiveSkills"]["items"][3]["enchantMods"] == [
        "Added Small Passive Skills grant: Staff Attacks deal 12% increased Damage with Hits and Ailments",
        "Added Small Passive Skills grant: Mace or Sceptre Attacks deal 12% increased Damage with Hits and Ailments",
    ]
    assert large["explicitMods"] == [
        "1 Added Passive Skill is Renewal",
        "Added Small Passive Skills have 35% increased Effect",
        "Added Small Passive Skills also grant: +10 to maximum Life",
    ]
    assert en["passiveSkills"]["items"][1]["name"] == "Putrid Seed"
    assert en["passiveSkills"]["items"][1]["enchantMods"] == ["1 Added Passive Skill is Whispers of Doom"]
    assert en["passiveSkills"]["items"][2]["name"] == "Voices"


def test_tencent_jump_forbidden_timeless_tattoos_and_runegrafts() -> None:
    # Empty passives for Runegrafts proves built-in tables resolve all 32 without Z-S27~Z-S30.json!
    d = Zh2EnDictionary.from_entries(
        passives={"Magebane": "法师克星", "Harmony of Purpose": "众志成城", "Tattoo of the Arohongui Scout": "亚欧红古斥候的文身"}
    )
    assert d.translate_mod("法师克星范围内的核心天赋技能可以在\n未连结至天赋树的情况下配置") == (
        "Passives in Radius of Magebane can be Allocated without being connected to your tree",
        True,
    )
    cn_bundle = {
        "char": {"name": "JumpTest", "class": "守护者", "level": 95},
        "items": {"items": []},
        "passiveSkills": {
            "hashes": [],
            "skill_overrides": {
                "100": {"name": "巨蜥的尊贵文身"}, "101": {"name": "冈姆的忠诚纹身"},
                "102": {"name": "躯体的旅途文身"}, "103": {"name": "咆哮之符文嫁接"},
                "104": {"name": "咆哮之符文嵌板"}, "105": {"name": "咆哮符文嵌板"},
                "106": {"name": "亚欧红古斥候纹身"}, "107": {"name": "白鸽的尊贵文身"},
                "108": {"name": "不存在的未知中文覆写"},
            },
            "items": [
                {
                    "x": 0, "frameType": 3, "name": "绝望逃脱", "baseType": "翠绿珠宝",
                    "properties": [{"name": "范围", "values": [["小", 0]]}],
                    "explicitMods": ["法师克星范围内的核心天赋技能可以在\n未连结至天赋树的情况下配置\n通途"],
                },
                {
                    "x": 1, "frameType": 3, "name": "希望之线", "baseType": "赤红珠宝",
                    "properties": [{"name": "范围", "values": [["可变（巨型）", 0]]}],
                    "explicitMods": ["只影响巨型圈内的天赋", "范围内的天赋技能无需连接到你的天赋树即可配置\n所有元素抗性 -15%\n通途"],
                },
                {
                    "x": 2, "frameType": 3, "name": "禁断之火", "baseType": "赤红珠宝",
                    "explicitMods": ["禁断之肉上有匹配的词缀则配置 众志成城"],
                },
                {
                    "x": 3, "frameType": 3, "name": "英雄悲剧", "baseType": "永恒珠宝",
                    "properties": [{"name": "限制", "values": [["1 史实", 0]]}, {"name": "范围", "values": [["大", 0]]}],
                    "explicitMods": [
                        "纪念 4321 件沃拉娜世系值得传唱的事迹",
                        "在巴巴拉的阿卡拉中指派 1234 名德卡拉的服务",
                        "在娜斯玛的阿卡拉中指派 5678 名德卡拉的服务",
                        "征服 777 个受特克罗奴役的灵魂",
                        "受影响的天赋被深渊抑制",
                    ],
                },
            ],
        },
    }
    en, warns = translate_cn_bundle(cn_bundle, d)
    assert warns == ["[CN-TRANSLATE-WARN] skill_overrides[108]: 不存在的未知中文覆写"]
    ov = en["passiveSkills"]["skill_overrides"]
    assert ov["100"]["name"] == "Honoured Tattoo of the Tuatara"
    assert ov["101"]["name"] == "Loyalty Tattoo of Kaom"
    assert ov["102"]["name"] == "Journey Tattoo of the Body"
    assert ov["103"]["name"] == "Runegraft of Bellows"
    assert ov["104"]["name"] == "Runegraft of Bellows"
    assert ov["105"]["name"] == "Runegraft of Bellows"
    assert ov["106"]["name"] == "Tattoo of the Arohongui Scout"
    assert ov["107"]["name"] == "Honoured Tattoo of the Dove"
    items = en["passiveSkills"]["items"]
    assert items[0]["name"] == "Impossible Escape" and items[0]["properties"][0]["values"] == [["Small", 0]]
    assert items[0]["explicitMods"] == ["Passives in Radius of Magebane can be Allocated without being connected to your tree"]
    assert items[1]["name"] == "Thread of Hope" and items[1]["properties"][0]["values"] == [["Variable", 0]]
    assert items[1]["explicitMods"] == [
        "Only affects Passives in Massive Ring",
        "Passive Skills in Radius can be Allocated without being connected to your tree",
        "-15% to all Elemental Resistances",
    ]
    assert items[2]["explicitMods"] == ["Allocates Harmony of Purpose if you have the matching modifier on Forbidden Flesh"]
    assert items[3]["name"] == "Heroic Tragedy" and items[3]["properties"][0]["values"] == [["1 Historic", 0]]
    assert items[3]["explicitMods"] == [
        "Remembrancing 4321 songworthy deeds by the line of Vorana",
        "Denoted service of 1234 dekhara in the akhara of Balbala",
        "Denoted service of 5678 dekhara in the akhara of Nasima",
        "Subjugating 777 souls in the thrall of Tecrod",
        "Passives affected are Conquered by the Abyssal",
    ]


def test_all_32_runegrafts_resolve_offline() -> None:
    d_offline = Zh2EnDictionary.from_entries()
    for zh_name, expected_en in TENCENT_RUNEGRAFT_ALIASES.items():
        assert d_offline.translate_passive(zh_name) == expected_en
        if "之符文" in zh_name:
            assert d_offline.translate_passive(zh_name.replace("之符文", "符文")) == expected_en


@pytest.mark.skipif(
    find_runnable_engine() is None or not has_zh_data(find_runnable_engine()),
    reason="requires runnable PoB 2.69.0 engine with zh-rCN data",
)
def test_all_32_runegrafts_resolve_with_engine_dict() -> None:
    pob_root = find_runnable_engine()
    assert pob_root is not None
    tp = pob_root / "PoE1" / "Data" / "TattooPassives.lua"
    assert tp.is_file()
    en_rgs = {
        m.group(1)
        for m in re.finditer(r'\["([^"]+)"\]\s*=\s*\{(.*?)\n\t\t\},', tp.read_text(encoding="utf-8", errors="replace"), re.S)
        if '["overrideType"] = "AlternateMastery"' in m.group(2)
    }
    assert len(en_rgs) == 32
    d_eng = Zh2EnDictionary.from_engine_dir(pob_root)
    resolved = {d_eng.translate_passive(zh) for zh in TENCENT_RUNEGRAFT_ALIASES}
    assert en_rgs.issubset(resolved), en_rgs - resolved
    cj = pob_root / "PoE1" / "Data" / "ClusterJewels.lua"
    assert cj.is_file()
    lua_enchants = set(re.findall(r'"(Added Small Passive Skills grant: [^"]+)"', cj.read_text(encoding="utf-8", errors="replace")))
    assert set(CLUSTER_BASE_ENCHANT_MAP.values()) == lua_enchants
