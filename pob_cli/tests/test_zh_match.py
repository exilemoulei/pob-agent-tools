"""Simplified queries against Traditional engine labels.

Under `pob lang zh-TW` (or the legacy pob-zh.exe) the engine shows Traditional (zh-rTW) labels,
while queries and slang aliases are mostly Simplified. These tests feed the matchers labels the
way such an engine reports them, without needing that engine.
"""

from __future__ import annotations

from typing import Any

import pytest
from engine_paths import find_engine_data

import pob_cli.game_data as game_data
from pob_cli.analyzer.calcs import _norm_calc_text
from pob_cli.game_data import english_for_zh, fold_han
from pob_cli.mutators.build_state import _match_named_option
from pob_cli.mutators.common import _fold_search_text
from pob_cli.mutators.item_db import _search_item_db_items
from pob_cli.mutators.skills_resolve import _resolve_gem_candidate

TEMPLAR_ASCENDANCIES_TW = [
    {"id": 0, "name": "None", "nameZh": "無"},
    {"id": 1, "name": "Inquisitor", "nameZh": "判官"},
    {"id": 2, "name": "Hierophant", "nameZh": "聖宗"},
    {"id": 3, "name": "Guardian", "nameZh": "守護者"},
]


@pytest.fixture
def english_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stand-in for the dictionary so matcher tests do not need engine data."""
    table: dict[str, str] = {}
    monkeypatch.setattr(game_data, "english_for_zh", lambda text, *, pob_dir=None: table.get(str(text).strip()))
    return table


class _FakeWorker:
    def __init__(self, responses: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.pob_root = None
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        key = (method, str(params.get("query", "")))
        self.calls.append(key)
        return self.responses.get(key, {"items": [], "gems": [], "total": 0})


def test_fold_han_equates_scripts() -> None:
    assert fold_han("聖宗") == fold_han("圣宗") == "圣宗"
    assert fold_han("紅玉戒指") == "红玉戒指"
    assert fold_han("魔力回復") == "魔力回复"
    # Hand-picked PoE term choices win over Unihan (鍊 -> 炼 there; 链结 here).
    assert fold_han("連結") == "连结"
    assert fold_han("鍊") == "链"
    assert fold_han("Mageblood 法师之血") == "Mageblood 法师之血"
    assert fold_han(None) == ""


def test_search_folds_apply_to_both_sides() -> None:
    assert _fold_search_text("聖宗") == _fold_search_text("圣宗")
    assert _norm_calc_text("魔力回復") == _norm_calc_text("魔力回复")
    # PoB's "Recovery" row: 恢復 in zh-rTW, 回复 in zh-rCN; the section label 魔力 precedes it.
    assert _norm_calc_text("魔力恢復") == _norm_calc_text("魔力回复")


def test_named_option_matches_traditional_label_by_fold(english_lookup: dict[str, str]) -> None:
    assert _match_named_option(TEMPLAR_ASCENDANCIES_TW, "圣宗", "ascendancy")["id"] == 2
    assert _match_named_option(TEMPLAR_ASCENDANCIES_TW, "无", "ascendancy")["id"] == 0
    assert _match_named_option(TEMPLAR_ASCENDANCIES_TW, "Hierophant", "ascendancy")["id"] == 2


def test_named_option_matches_differently_worded_name_by_english(english_lookup: dict[str, str]) -> None:
    english_lookup["守卫"] = "Guardian"  # no character overlap with the label 守護者
    assert _match_named_option(TEMPLAR_ASCENDANCIES_TW, "守卫", "ascendancy")["id"] == 3
    with pytest.raises(ValueError, match="Unknown ascendancy"):
        _match_named_option(TEMPLAR_ASCENDANCIES_TW, "不存在", "ascendancy")


def test_item_db_retries_by_english_name(english_lookup: dict[str, str]) -> None:
    english_lookup["法师之血"] = "Mageblood"
    mageblood = {"title": "Mageblood", "titleZh": "魔血", "baseName": "Heavy Belt"}
    worker = _FakeWorker({("item_db", "Mageblood"): {"items": [mageblood], "total": 1}})
    kind, items, total = _search_item_db_items(worker, query="法师之血", db_type="UNIQUE", limit=5)
    assert (kind, total) == ("unique", 1)
    assert items[0]["title"] == "Mageblood"
    assert [c for c in worker.calls if c[0] == "item_db"][:2] == [("item_db", "法师之血"), ("item_db", "Mageblood")]


def test_item_db_native_hit_skips_english_lookup(english_lookup: dict[str, str]) -> None:
    english_lookup["魔血"] = "Mageblood"
    mageblood = {"title": "Mageblood", "titleZh": "魔血"}
    worker = _FakeWorker({("item_db", "魔血"): {"items": [mageblood], "total": 1}})
    _, items, _ = _search_item_db_items(worker, query="魔血", db_type="UNIQUE", limit=5)
    assert items[0]["title"] == "Mageblood"
    assert [c for c in worker.calls if c[0] == "item_db"] == [("item_db", "魔血")]


def test_gem_resolution_retries_by_english_name(english_lookup: dict[str, str]) -> None:
    english_lookup["召唤狂暴之灵"] = "Summon Raging Spirit"
    srs = {"name": "Summon Raging Spirit", "nameZh": "召喚憤怒狂靈"}
    worker = _FakeWorker({("gem_search", "Summon Raging Spirit"): {"gems": [srs]}})
    cand = _resolve_gem_candidate(worker, 1, "召唤狂暴之灵")
    assert cand is not None and cand["name"] == "Summon Raging Spirit"


def test_gem_matches_traditional_label_by_fold(english_lookup: dict[str, str]) -> None:
    fireball = {"name": "Fireball", "nameZh": "火球術"}
    worker = _FakeWorker({("gem_search", "火球术"): {"gems": [{"name": "Firestorm", "nameZh": "烈焰風暴"}, fireball]}})
    cand = _resolve_gem_candidate(worker, 1, "火球术")
    assert cand is not None and cand["name"] == "Fireball"


@pytest.mark.skipif(find_engine_data() is None, reason="Needs engine game data with zh dictionaries")
@pytest.mark.parametrize(
    ("query", "english"),
    [
        ("法师之血", "Mageblood"),  # Simplified name
        ("魔血", "Mageblood"),  # Traditional name, worded differently
        ("圣宗", "Hierophant"),
        ("深渊血脉", "Abyssal Bloodline"),
        ("红玉戒指", "Ruby Ring"),
    ],
)
def test_english_for_zh_uses_engine_dictionary(query: str, english: str) -> None:
    got = english_for_zh(query, pob_dir=find_engine_data())
    assert got is not None and got.lower() == english.lower()


def test_english_for_zh_ignores_non_chinese() -> None:
    assert english_for_zh("Mageblood") is None
    assert english_for_zh("") is None
