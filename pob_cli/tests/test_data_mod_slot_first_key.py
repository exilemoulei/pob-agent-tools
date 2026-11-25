"""`pob data mod --slot` decides by PoB's own rule: the first weight key a base carries wins."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_engine_data

from pob_cli import item_inspect
from pob_cli.game_data import query_local_data
from pob_cli.game_data.queries_core import (
    _mod_matches_slot,
    _normalize_slot_filter,
    _slot_tag_sets,
    mod_rolls_on_base,
)

REAL_POB_DIR = find_engine_data()
needs_engine_data = pytest.mark.skipif(REAL_POB_DIR is None, reason="Real PoE1 engine data not available")

BOW = {"bow", "ranged", "two_hand_weapon", "twohand", "weapon", "default"}
SWORD = {"sword", "one_hand_weapon", "onehand", "weapon", "default"}
STR_BODY = {"armour", "body_armour", "str_armour", "default"}
INT_BODY = {"armour", "body_armour", "int_armour", "default"}
BODY_INFLUENCES = ("body_armour_elder", "body_armour_shaper")


def test_a_zero_on_the_first_carried_key_keeps_the_mod_off_the_base() -> None:
    keys, weights = ["ranged", "weapon", "default"], {"weapon": 1000}
    assert mod_rolls_on_base("Explicit", keys, weights, BOW) is False
    assert mod_rolls_on_base("Explicit", keys, weights, SWORD) is True

    # A later key with a weight does not reopen what an earlier key closed.
    two_hand_only = ["one_hand_weapon", "bow", "sword", "default"]
    assert mod_rolls_on_base("Explicit", two_hand_only, {"bow": 1400, "sword": 1200}, SWORD) is False
    assert mod_rolls_on_base("Explicit", two_hand_only, {"bow": 1400, "sword": 1200}, BOW) is True


def test_a_key_the_slot_list_never_named_still_counts() -> None:
    keys, weights = ["ring", "str_armour", "default"], {"ring": 1000, "str_armour": 1000}
    assert mod_rolls_on_base("Explicit", keys, weights, STR_BODY) is True
    assert mod_rolls_on_base("Explicit", keys, weights, INT_BODY) is False


def test_default_is_a_fallback_only_for_gear_files_on_gear() -> None:
    keys, weights = ["fishing_rod", "weapon", "default"], {"default": 1000}
    assert mod_rolls_on_base("Explicit", keys, weights, STR_BODY) is True
    assert mod_rolls_on_base("Corrupted", keys, weights, STR_BODY) is True
    assert mod_rolls_on_base("Eldritch", keys, weights, STR_BODY) is False
    assert mod_rolls_on_base("Explicit", keys, weights, SWORD) is False
    # A flask takes its mods from ModFlask, never from the gear file.
    assert mod_rolls_on_base("Explicit", keys, weights, {"flask", "utility_flask", "default"}) is False


def test_an_influence_tag_is_tried_on_top_of_the_bare_base() -> None:
    keys, weights = ["body_armour_shaper", "default"], {"body_armour_shaper": 500}
    assert mod_rolls_on_base("Explicit", keys, weights, STR_BODY) is False
    assert mod_rolls_on_base("Explicit", keys, weights, STR_BODY, BODY_INFLUENCES) is True
    assert mod_rolls_on_base("Explicit", keys, weights, STR_BODY, ("helmet_shaper",)) is False


def test_an_elevated_mod_needs_its_influence_and_nothing_else() -> None:
    keys: list[str] = ["body_armour_shaper", "default"]
    assert mod_rolls_on_base("Explicit", keys, {}, STR_BODY, BODY_INFLUENCES, elevated=True) is True
    assert mod_rolls_on_base("Explicit", keys, {}, STR_BODY, BODY_INFLUENCES) is False
    assert mod_rolls_on_base("Explicit", keys, {}, STR_BODY, ("helmet_shaper",), elevated=True) is False
    assert mod_rolls_on_base("Explicit", ["default"], {}, STR_BODY, BODY_INFLUENCES, elevated=True) is False


def _bases_db(rows: list[tuple[str, str | None, dict[str, Any]]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE bases (item_type TEXT, sub_type TEXT, data_json TEXT)")
    conn.executemany("INSERT INTO bases VALUES (?, ?, ?)", [(t, s, json.dumps(d)) for t, s, d in rows])
    return conn


def test_slot_tag_sets_come_from_the_slot_classes_and_skip_placeholders() -> None:
    conn = _bases_db(
        [
            ("One Handed Sword", None, {"tags": sorted(SWORD), "influence_tags": ["sword_shaper"]}),
            ("One Handed Sword", None, {"tags": sorted(SWORD), "influence_tags": ["sword_shaper"]}),
            ("One Handed Sword", "Thrusting", {"tags": sorted(SWORD | {"rapier"})}),
            ("One Handed Sword", None, {"tags": ["default"]}),
            ("Bow", None, {"tags": sorted(BOW)}),
        ]
    )
    try:
        one_hand = _slot_tag_sets(conn, _normalize_slot_filter("one_handed_sword")[3])
        assert {tags for tags, _ in one_hand} == {frozenset(SWORD), frozenset(SWORD | {"rapier"})}
        assert (frozenset(SWORD), ("sword_shaper",)) in one_hand

        thrusting = _slot_tag_sets(conn, _normalize_slot_filter("thrusting_one_handed_sword")[3])
        assert [tags for tags, _ in thrusting] == [frozenset(SWORD | {"rapier"})]

        assert _slot_tag_sets(conn, _normalize_slot_filter("jewel")[3]) == ()

        bow_spec = _normalize_slot_filter("bow")
        row = ("Explicit", '{"weapon":1000}', '["ranged","default"]', "[]")
        keys = '["ranged","weapon","default"]'
        bow_sets = _slot_tag_sets(conn, bow_spec[3])
        assert _mod_matches_slot(*row, bow_spec, weight_keys_json=keys, tag_sets=bow_sets) is False
        sword_sets = _slot_tag_sets(conn, _normalize_slot_filter("one_handed_sword")[3])
        assert _mod_matches_slot(*row, bow_spec, weight_keys_json=keys, tag_sets=sword_sets) is True
    finally:
        conn.close()


@pytest.fixture(scope="module")
def runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("slot-index")


def _ids(query: str, slot: str, runtime_dir: Path, **kw: Any) -> set[str]:
    res = query_local_data("mod", query, pob_dir=REAL_POB_DIR, runtime_dir=runtime_dir, slot=slot, limit=0, **kw)
    return {m["id"] for m in res["results"]}


@needs_engine_data
def test_real_mods_follow_the_first_carried_key(runtime_dir: Path) -> None:
    # `Strength1` is keyed by `str_armour`, a tag no slot list carried.
    assert "Strength1" in _ids("to Strength", "body", runtime_dir)

    # `{ranged = 0, weapon = 1000}`: the top attack speed tiers stay off bows and wands.
    bow = _ids("increased Attack Speed", "bow", runtime_dir)
    assert "LocalIncreasedAttackSpeed5" in bow and "LocalIncreasedAttackSpeed6" not in bow
    assert "LocalIncreasedAttackSpeed6" in _ids("increased Attack Speed", "one_handed_sword", runtime_dir)

    # `{one_hand_weapon = 0, ...}` marks the two-handed tier list.
    fire = "Fire Damage"
    assert "LocalAddedFireDamageTwoHand1" not in _ids(fire, "sceptre", runtime_dir)
    assert "LocalAddedFireDamageTwoHand1" in _ids(fire, "two_handed_sword", runtime_dir)
    assert "LocalAddedFireDamage1" in _ids(fire, "sceptre", runtime_dir)

    assert "LocalIncreaseSocketedGemUnsetRing1" in _ids("Level of Socketed Gems", "ring", runtime_dir)

    # Flasks and tinctures roll from their own mod files, never from the gear file's `default`.
    for own_file_slot in ("flask", "tincture"):
        assert not any(i.startswith("IncreasedLife") for i in _ids("to maximum Life", own_file_slot, runtime_dir))
    tincture = query_local_data("mod", "", pob_dir=REAL_POB_DIR, runtime_dir=runtime_dir, slot="tincture", limit=0)
    assert tincture["count"] > 0 and {m["source"] for m in tincture["results"]} == {"Tincture"}


@needs_engine_data
def test_real_elevated_mods_show_on_their_slot_only(runtime_dir: Path) -> None:
    query = "Level of Socketed Skill Gems"
    assert "LocalIncreaseSocketedActiveGemLevelUberMaven" in _ids(query, "body", runtime_dir)
    assert "LocalIncreaseSocketedActiveGemLevelUberMaven" not in _ids(query, "helmet", runtime_dir)


@needs_engine_data
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("chest", "body_armour"), ("helm", "helmet"), ("ring_1", "ring"), ("ring2", "ring"), ("weapon1", "weapon")],
)
def test_real_aliases_answer_as_their_slot(alias: str, canonical: str, runtime_dir: Path) -> None:
    for query in ("Resistance", "increased Attack Speed", "maximum Life"):
        assert _ids(query, alias, runtime_dir) == _ids(query, canonical, runtime_dir)


def _inspect(item_class: str, base: str, lines: list[str], slot: str, runtime_dir: Path) -> dict[str, Any]:
    raw = "\n".join([f"Item Class: {item_class}", "Rarity: RARE", "Doom Shell", base, "--------", "Item Level: 86", "--------", *lines])
    return item_inspect.inspect_item(
        raw, slot=slot, pob_dir=REAL_POB_DIR, runtime_dir=runtime_dir, include_suggestions=False
    )


@needs_engine_data
def test_real_item_inspect_reads_influence_mods(runtime_dir: Path) -> None:
    # Both suffixes are keyed by `body_armour_<influence>` alone, so a rule that only knows the base's own tags
    # leaves them unmatched and cannot count the open affixes.
    body = _inspect(
        "Body Armours",
        "Astral Plate",
        ["+90 to maximum Life", "11% increased Life Recovery rate", "Recover 4% of Mana on Kill"],
        "body_armour",
        runtime_dir,
    )
    assert body["unmatched"] == [] and body["certain"] is True
    assert body["counts"] == {"prefix": 1, "suffix": 2}
    assert {a["group"] for a in body["affixes"]} == {"IncreasedLife", "LifeRecoveryRate", "MaximumManaOnKillPercent"}

    # Without the influence suffix as a candidate, this line was read as a prefix of another item class.
    gloves = _inspect("Gloves", "Titan Gauntlets", ["+15% to Cold Damage over Time Multiplier"], "gloves", runtime_dir)
    assert gloves["counts"] == {"prefix": 0, "suffix": 1} and gloves["certain"] is True
