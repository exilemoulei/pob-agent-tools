"""Offline tests for Chinese Replica (`仿品` / `赝品` / `贗品`) unique search, translation, and grounding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pob_cli.game_data import (
    ensure_gamedata_index,
    ground_text_entities,
    query_local_data,
    query_multi,
)
from pob_cli.game_data.tw_norm import extract_zh_replica_stem
from pob_cli.game_data.zh_match import _english_for_zh_cached, english_for_zh


def _build_replica_fixture_pob(tmp_path: Path) -> tuple[Path, Path]:
    pob_root = tmp_path / "engine"
    rt_dir = tmp_path / "rt"
    poe1 = pob_root / "PoE1"
    data = poe1 / "Data"
    (data / "Bases").mkdir(parents=True)
    (data / "Uniques").mkdir(parents=True)

    (poe1 / "manifest.xml").write_text('<PoBVersion><Version number="2.68.1"/></PoBVersion>', encoding="utf-8")
    (data / "Gems.lua").write_text("return {}\n", encoding="utf-8")
    (data / "Skills").mkdir(parents=True)
    (data / "ModItem.lua").write_text("return {}\n", encoding="utf-8")
    (data / "Bases" / "flask.lua").write_text(
        'itemBases["Sulphur Flask"] = { type = "Flask", subType = "Utility", tags = { flask = true, utility_flask = true }, req = { level = 35 }, implicit = "Creates Consecrated Ground on Use", flask = {} }\n',
        encoding="utf-8",
    )
    (data / "Bases" / "body.lua").write_text(
        'itemBases["Triumphant Lamellar"] = { type = "Body Armour", tags = { body_armour = true, armour = true }, req = { level = 69 }, armour = {} }\n',
        encoding="utf-8",
    )
    (data / "Bases" / "amulet.lua").write_text(
        'itemBases["Paua Amulet"] = { type = "Amulet", tags = { amulet = true }, req = { level = 1 } }\n'
        'itemBases["Onyx Amulet"] = { type = "Amulet", tags = { amulet = true }, req = { level = 20 } }\n',
        encoding="utf-8",
    )
    (data / "Uniques" / "flask.lua").write_text(
        """return {
[[
Sorrow of the Divine
Sulphur Flask
Implicits: 1
Creates Consecrated Ground on Use
Life Recovery from Flasks also applies to Energy Shield during Effect
Zealot's Oath during Effect
]],[[
Replica Sorrow of the Divine
Sulphur Flask
League: Heist
Implicits: 1
Creates Consecrated Ground on Use
Life Recovery from Flasks also applies to Energy Shield during Effect
Eldritch Battery during Effect
]],
}
""",
        encoding="utf-8",
    )
    (data / "Uniques" / "body.lua").write_text(
        """return {
[[
Farrul's Fur
Triumphant Lamellar
Implicits: 0
+2 to Level of Socketed Aura Gems
]],[[
Replica Farrul's Fur
Triumphant Lamellar
League: Heist
Implicits: 0
Aspect of the Cat has no Reservation
]],[[
Replica Atziri's Foible
Paua Amulet
League: Heist
Implicits: 1
(20-30)% increased Mana Regeneration Rate
+100 to maximum Mana
]],[[
Replica Astramentis
Onyx Amulet
League: Heist
Implicits: 1
+(10-16) to all Attributes
+(80-100) to all Attributes
]],[[
Mageblood
Heavy Belt
Implicits: 0
Magic Utility Flasks cannot be Used
]],
}
""",
        encoding="utf-8",
    )

    zh_cn = pob_root / "Data" / "poe1" / "zh-rCN"
    zh_tw = pob_root / "Data" / "poe1" / "zh-rTW"
    zh_cn.mkdir(parents=True)
    zh_tw.mkdir(parents=True)
    (zh_cn / "meta.json").write_text('{"version": "test-cn"}', encoding="utf-8")
    (zh_tw / "meta.json").write_text('{"version": "test-tw"}', encoding="utf-8")
    (zh_cn / "items.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Sulphur Flask": "硫磺药剂",
                    "Triumphant Lamellar": "凯旋鳞甲",
                    "Paua Amulet": "海灵护身符",
                    "Onyx Amulet": "黑曜护身符",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (zh_cn / "uniques.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Sorrow of the Divine": "神圣哀悼",
                    "Replica Sorrow of the Divine": "神圣哀悼【仿品】",
                    "Farrul's Fur": "大地之护",
                    "Replica Farrul's Fur": "大地之护【仿品】",
                    "Replica Atziri's Foible": "阿兹里圣徽【仿品】",
                    "Mageblood": "法师之血",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (zh_cn / "stats.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Eldritch Battery during Effect": "生效期间获得异能魔力",
                    "Heist Targets are always Replica Unique Items": "夺宝目标必定为传奇物品仿品",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (zh_tw / "uniques.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Sorrow of the Divine": "神聖哀悼",
                    "Replica Sorrow of the Divine": "贗品．神聖哀悼",
                    "Replica Farrul's Fur": "贗品．法瑞爾的獸甲",
                    "Replica Atziri's Foible": "贗品．阿茲里聖徽",
                    "Replica Astramentis": "贗品．群星",
                    "Mageblood": "魔血",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ensure_gamedata_index(pob_dir=pob_root, runtime_dir=rt_dir, force_rebuild=True)
    return pob_root, rt_dir


def test_extract_zh_replica_stem_forms_and_non_replica_guard() -> None:
    for q in (
        "仿品神圣哀悼",
        "仿品·神圣哀悼",
        "仿品・神圣哀悼",
        "仿品 神圣哀悼",
        "【仿品】神圣哀悼",
        "(仿品)神圣哀悼",
        "神圣哀悼【仿品】",
        "神圣哀悼（仿品）",
        "神圣哀悼 仿品",
        "贗品．神聖哀悼",
        "赝品·神圣哀悼",
        "Replica 神圣哀悼",
    ):
        is_rep, stem = extract_zh_replica_stem(q)
        assert (is_rep, stem) == (True, "神圣哀悼"), f"Failed for {q!r}: {(is_rep, stem)}"

    assert extract_zh_replica_stem("仿品") == (True, "")
    assert extract_zh_replica_stem("仿品·") == (True, "")
    assert extract_zh_replica_stem("神圣哀悼") == (False, "")
    # Mod prose ending in unseparated `仿品` must NOT be misparsed as an item suffix
    assert extract_zh_replica_stem("夺宝目标必定为传奇物品仿品") == (False, "")


@pytest.mark.parametrize(
    ("query", "expected_en"),
    [
        ("仿品神圣哀悼", "Replica Sorrow of the Divine"),
        ("仿品·神圣哀悼", "Replica Sorrow of the Divine"),
        ("仿品 神圣哀悼", "Replica Sorrow of the Divine"),
        ("神圣哀悼【仿品】", "Replica Sorrow of the Divine"),
        ("神圣哀悼(仿品)", "Replica Sorrow of the Divine"),
        ("贗品．神聖哀悼", "Replica Sorrow of the Divine"),
        ("赝品神圣哀悼", "Replica Sorrow of the Divine"),
        ("仿品阿茲里聖徽", "Replica Atziri's Foible"),
        ("仿品大地之护", "Replica Farrul's Fur"),
        ("仿品法瑞尔的兽甲", "Replica Farrul's Fur"),
        ("仿品猫衣", "Replica Farrul's Fur"),
    ],
)
def test_data_unique_and_english_for_zh_match_replica_prefixes(
    tmp_path: Path, query: str, expected_en: str
) -> None:
    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)

    res = query_local_data("unique", query, pob_dir=pob_root, runtime_dir=rt_dir)
    assert res["count"] >= 1, f"Expected hit for {query!r}, got: {res}"
    assert res["results"][0]["name"] == expected_en

    if query not in ("仿品法瑞尔的兽甲", "仿品猫衣"):
        assert english_for_zh(query, pob_dir=pob_root, runtime_dir=rt_dir) == expected_en


def test_data_unique_partial_prefix_multi_translate_grounding_and_nonexistent_guard(tmp_path: Path) -> None:
    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)

    # Partial prefix stem ("仿品大地" -> "大地之护【仿品】")
    r_part = query_local_data("unique", "仿品大地", pob_dir=pob_root, runtime_dir=rt_dir)
    assert r_part["count"] == 1
    assert r_part["results"][0]["name"] == "Replica Farrul's Fur"

    # Bare "仿品" lists all Replica uniques (and excludes non-Replica uniques)
    r_bare = query_local_data("unique", "仿品", pob_dir=pob_root, runtime_dir=rt_dir)
    names_bare = {u["name"] for u in r_bare["results"]}
    assert names_bare == {
        "Replica Sorrow of the Divine",
        "Replica Farrul's Fur",
        "Replica Atziri's Foible",
        "Replica Astramentis",
    }

    # Variadic query_multi parity (including cross-locale TW name "魔血" + "仿品·神圣哀悼")
    r_multi = query_multi(
        kind="unique",
        queries=["仿品·神圣哀悼", "魔血"],
        pob_dir=pob_root,
        runtime_dir=rt_dir,
    )
    assert r_multi["results"]["仿品·神圣哀悼"]["results"][0]["name"] == "Replica Sorrow of the Divine"
    assert r_multi["results"]["魔血"]["results"][0]["name"] == "Mageblood"

    # pob data translate resolves prefix form cleanly
    r_tr = query_local_data("translate", "仿品·神圣哀悼", pob_dir=pob_root, runtime_dir=rt_dir)
    assert r_tr["count"] >= 1
    assert r_tr["results"][0]["en"] == "Replica Sorrow of the Divine"

    # pob ground prefers Replica Sorrow of the Divine over base Sorrow of the Divine on "仿品神圣哀悼"
    g_res = ground_text_entities("我带了仿品·神圣哀悼药剂为什么缺蓝", pob_dir=pob_root, runtime_dir=rt_dir)
    ent_names = [e["name"] for e in g_res["entities"]]
    assert "Replica Sorrow of the Divine" in ent_names
    assert "Sorrow of the Divine" not in ent_names

    # Non-existent Replica unique ("仿品法师之血" — Mageblood has no Replica version) must NOT hallucinate a hit
    assert english_for_zh("仿品法师之血", pob_dir=pob_root, runtime_dir=rt_dir) is None
    r_non = query_local_data("unique", "仿品法师之血", pob_dir=pob_root, runtime_dir=rt_dir)
    assert r_non["count"] == 0


def test_a_replica_query_still_matches_on_item_type(tmp_path: Path) -> None:
    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)

    def names(q: str, **kw: Any) -> set[str]:
        return {u["name"] for u in query_local_data("unique", q, pob_dir=pob_root, runtime_dir=rt_dir, **kw)["results"]}

    # The words after the Replica mark may name a base type or a class, with or without --ilvl.
    assert names("仿品 sulphur flask") == {"Replica Sorrow of the Divine"}
    assert names("仿品 sulphur flask", ilvl=100) == {"Replica Sorrow of the Divine"}
    assert names("仿品 硫磺药剂") == names("仿品 药剂") == {"Replica Sorrow of the Divine"}
    assert names("仿品 amulet") == {"Replica Atziri's Foible", "Replica Astramentis"}
    assert names("replica 神圣哀悼") == {"Replica Sorrow of the Divine"}
    assert names("仿品 no such thing") == set()
    # A piece from the middle of a base type, or of a mod line, is a name no Replica has.
    assert names("仿品灵护") == names("仿品 灵护", ilvl=100) == set()  # 海灵护身符
    assert names("仿品 consecrated") == names("仿品 圣徽之外") == set()


def test_grounding_names_the_replica_when_slang_carries_a_replica_mark(tmp_path: Path) -> None:
    import sqlite3

    from pob_cli.game_data.grounding import _resolve_slang, _settle_replica_slang

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE uniques (name_lower TEXT)")
    conn.executemany("INSERT INTO uniques VALUES (?)", [("replica headhunter",), ("replica farrul's fur",)])

    def canon(text: str, db: sqlite3.Connection | None = conn) -> list[str]:
        return [r["canonical"] for r in _settle_replica_slang(_resolve_slang(text), db)]

    assert canon("猎首") == ["Headhunter"]
    for marked in ("仿品·猎首", "赝品猎首", "仿品 猎首", "猎首【仿品】", "【仿品】猎首", "猎首仿品", "我带了仿品·猎首"):
        assert canon(marked) == ["Replica Headhunter"], marked
    # A slang word inside a longer one is not a hit of its own; both items named means both.
    assert canon("仿品猎首") == ["Replica Headhunter"]
    assert canon("猎首和仿品猎首") == ["Replica Headhunter", "Headhunter"]
    assert canon("仿品清单里没有猎首") == ["Headhunter"]
    # A mark that belongs to the neighbouring name is not this name's mark.
    assert canon("猎首 仿品猫衣") == ["Replica Farrul's Fur", "Headhunter"]
    assert canon("神圣哀悼【仿品】 猎首") == ["Headhunter"]
    assert canon("猎首 赝品神圣哀悼") == ["Headhunter"]
    # No such Replica in the data, or no data: the item itself, never an invented name.
    assert canon("仿品魔血") == ["Mageblood"]
    assert canon("仿品·猎首", None) == ["Headhunter"]
    conn.close()

    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)

    def grounded(text: str) -> list[str]:
        return [e["name"] for e in ground_text_entities(text, pob_dir=pob_root, runtime_dir=rt_dir)["entities"]]

    for text in ("仿品·猫衣", "猫衣【仿品】", "仿品猫衣"):
        assert grounded(text) == ["Replica Farrul's Fur"], text
    assert grounded("猫衣") == ["Farrul's Fur"]
    assert grounded("仿品魔血") == ["Mageblood"]
    # A shorter name inside a longer one is not named, unless it also stands on its own.
    assert grounded("神圣哀悼【仿品】") == ["Replica Sorrow of the Divine"]
    assert grounded("神圣哀悼【仿品】和神圣哀悼") == ["Replica Sorrow of the Divine", "Sorrow of the Divine"]


def test_grounding_reads_the_bracketed_replica_form_in_a_traditional_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POB_LANG", "zh-TW")
    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)
    for text in ("神圣哀悼【仿品】", "贗品．神聖哀悼"):
        ents = ground_text_entities(text, pob_dir=pob_root, runtime_dir=rt_dir)["entities"]
        assert [e["name"] for e in ents] == ["Replica Sorrow of the Divine"], text


def test_data_unique_zh_tw_locale_matches_simplified_and_traditional_replica_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POB_LANG", "zh-TW")
    _english_for_zh_cached.cache_clear()
    pob_root, rt_dir = _build_replica_fixture_pob(tmp_path)

    for q in ("仿品神圣哀悼", "仿品·神圣哀悼", "神圣哀悼【仿品】", "贗品．神聖哀悼", "赝品神圣哀悼"):
        res = query_local_data("unique", q, pob_dir=pob_root, runtime_dir=rt_dir)
        assert res["count"] >= 1, f"zh-TW failed for {q!r}: {res}"
        assert res["results"][0]["name"] == "Replica Sorrow of the Divine"

