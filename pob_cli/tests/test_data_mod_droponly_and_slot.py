"""Tests for `pob data mod` ModDelve/ModVeiled indexing, drop-only slot tables,
parent-tag slot inheritance, and `pob item inspect` drop-only fallback isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pob_cli import game_data, item_inspect
from pob_cli.game_data.constants import SLOT_ALIASES
from pob_cli.game_data.queries_core import _mod_matches_slot, _query_mods


def _write_synthetic_poe1_mod_data(poe1_dir: Path) -> None:
    data = poe1_dir / "Data"
    for sub in ("Bases", "Uniques", "Skills"):
        (data / sub).mkdir(parents=True)
    (data / "Gems.lua").write_text("return {}\n", encoding="utf-8")
    (data / "Bases" / "glove.lua").write_text(
        'local itemBases = ...\nitemBases["Sorcerer Gloves"] = { type = "Gloves", subType = "Energy Shield", '
        "tags = { gloves = true, int_armour = true, armour = true, default = true }, req = { level = 69, int = 97 } }\n",
        encoding="utf-8",
    )
    (data / "Bases" / "wand.lua").write_text(
        'local itemBases = ...\nitemBases["Imbued Wand"] = { type = "Wand", '
        "tags = { wand = true, one_hand_weapon = true, onehand = true, weapon = true, ranged = true, default = true }, "
        "req = { level = 59, int = 188 } }\n",
        encoding="utf-8",
    )
    (data / "ModExplicit.lua").write_text(
        """return {
    ["LocalIncreaseSocketedMinionGemLevelEnhancedMod"] = {
        type = "Prefix", affix = "Citaqualotl's", "+2 to Level of Socketed Minion Gems",
        level = 1, group = "LocalIncreaseSocketedMinionGemLevel", weightKey = { "default" }, weightVal = { 0 },
    },
    ["MinionDamageSiblingTempleSuffix"] = {
        type = "Suffix", affix = "of Citaqualotl", "Minions deal (20-30)% increased Damage",
        level = 1, group = "MinionDamage", weightKey = { "default" }, weightVal = { 0 },
    },
    ["LocalIncreaseSocketedMinionGemLevel2"] = {
        type = "Prefix", affix = "Summoner's", "+2 to Level of Socketed Minion Gems",
        level = 65, group = "LocalIncreaseSocketedMinionGemLevel", weightKey = { "default" }, weightVal = { 0 },
    },
    ["AddedFireDamageEnhancedLevel50Mod"] = {
        type = "Prefix", affix = "Puhuarte's", "25% of Physical Damage Converted to Fire Damage",
        level = 50, group = "AddedFireDamage", weightKey = { "default" }, weightVal = { 0 },
    },
    ["SpellDamageRuneDaggerIncursion"] = {
        type = "Prefix", affix = "Tacati's", "(50-60)% increased Spell Damage",
        level = 50, group = "SpellDamage", weightKey = { "default" }, weightVal = { 0 },
    },
    ["LocalIncreasedEnergyShield1"] = {
        type = "Prefix", affix = "Shining", "+(10-20) to maximum Energy Shield",
        level = 1, group = "BaseLocalDefences", weightKey = { "gloves", "default" }, weightVal = { 1000, 0 },
    },
    ["LocalIncreasedEnergyShieldEssenceBootsGloves7"] = {
        type = "Prefix", affix = "Essences", "+(56-65) to maximum Energy Shield",
        level = 75, group = "BaseLocalDefences", weightKey = { "default" }, weightVal = { 0 },
    },
    ["LocalIncreaseSocketedBowGemLevel2"] = {
        type = "Prefix", affix = "Sharpshooter's", "+2 to Level of Socketed Bow Gems",
        level = 64, group = "LocalIncreaseSocketedBowGemLevel", weightKey = { "bow", "default" }, weightVal = { 1000, 0 },
    },
    ["Strength1"] = {
        type = "Suffix", affix = "of the Brute", "+(8-12) to Strength",
        level = 11, group = "Strength", weightKey = { "gloves", "ring", "default" }, weightVal = { 1000, 1000, 0 },
    },
    ["OffSlotZeroWeightStrengthPrefix"] = {
        type = "Prefix", affix = "Legacy", "+(1-50) to Strength",
        level = 1, group = "OffSlotStrength", weightKey = { "default" }, weightVal = { 0 },
    },
}
""",
        encoding="utf-8",
    )
    (data / "ModDelve.lua").write_text(
        """return {
    ["LocalIncreaseSocketedMinionGemLevelDelve"] = {
        type = "Prefix", affix = "Subterranean", "+2 to Level of Socketed Minion Gems",
        level = 60, group = "LocalIncreaseSocketedMinionGemLevel", weightKey = { "default" }, weightVal = { 0 },
    },
    ["DelveCurseFrostbiteOnHit"] = {
        type = "Suffix", affix = "of the Underground", "Curse Enemies with Frostbite on Hit",
        level = 60, group = "CurseOnHitLevel", weightKey = { "default" }, weightVal = { 0 },
    },
}
""",
        encoding="utf-8",
    )
    (data / "ModVeiled.lua").write_text(
        """return {
    ["JunMasterVeiledDoubleDamageChance1h"] = {
        type = "Suffix", affix = "of the Order", "(6-7)% chance to deal Double Damage",
        level = 60, group = "DoubleDamageChance",
        weightKey = { "abyss_jewel", "jewel", "shield", "one_hand_weapon", "default" },
        weightVal = { 0, 0, 1000, 1000, 0 }, modTags = { "unveiled_mod", "damage" },
    },
}
""",
        encoding="utf-8",
    )
    (data / "ModCorrupted.lua").write_text(
        """return {
    ["V2SocketedMinionGemCorrupted"] = {
        type = "Corrupted", affix = "", "+2 to Level of Socketed Minion Gems",
        level = 20, group = "LocalIncreaseSocketedMinionGemLevel", weightKey = { "armour", "default" }, weightVal = { 1000, 0 },
    },
}
""",
        encoding="utf-8",
    )
    (data / "DelveDropOnly.lua").write_text(
        'return { ["LocalIncreaseSocketedMinionGemLevelDelve"] = { ["categories"] = { "Boots" } } }\n',
        encoding="utf-8",
    )
    (data / "IncursionDropOnly.lua").write_text(
        'return {\n'
        '    ["AddedFireDamageEnhancedLevel50Mod"] = { ["categories"] = { "Gloves" } },\n'
        '    ["SpellDamageRuneDaggerIncursion"] = { ["categories"] = { "Dagger: Rune Dagger" } },\n'
        '}\n',
        encoding="utf-8",
    )
    (data / "Essence.lua").write_text(
        'return { ["Woe7"] = { mods = { ["Boots"] = "LocalIncreasedEnergyShieldEssenceBootsGloves7", '
        '["Gloves"] = "LocalIncreasedEnergyShieldEssenceBootsGloves7" } } }\n',
        encoding="utf-8",
    )


def test_mod_delve_veiled_indexing_and_dynamic_incursion_origins(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")
    res = game_data.query_local_data("mod", "", pob_dir=pob_root, runtime_dir=rt, limit=50)
    by_id = {m["id"]: m for m in res["results"]}

    assert by_id["LocalIncreaseSocketedMinionGemLevelDelve"]["source"] == "Delve"
    assert by_id["LocalIncreaseSocketedMinionGemLevelDelve"]["drop_origin"] == "Delve"
    assert by_id["LocalIncreaseSocketedMinionGemLevelDelve"]["applicable_types"] == ["Boots"]
    assert by_id["JunMasterVeiledDoubleDamageChance1h"]["source"] == "Veiled"
    assert by_id["AddedFireDamageEnhancedLevel50Mod"]["drop_origin"] == "Incursion"
    assert by_id["MinionDamageSiblingTempleSuffix"]["drop_origin"] == "Incursion"
    assert by_id["LocalIncreasedEnergyShieldEssenceBootsGloves7"]["drop_origin"] == "Essence"
    assert by_id["LocalIncreasedEnergyShieldEssenceBootsGloves7"]["applicable_types"] == ["Boots", "Gloves"]


def test_parent_tags_ordered_inheritance_and_adversarial_order() -> None:
    for slot_key, (_, ptags, _) in SLOT_ALIASES.items():
        if "default" in ptags:
            assert ptags[-1] == "default", f"{slot_key} must put default last"
        if "armour" in ptags and len(ptags) > 2:
            assert ptags.index("armour") > 0, f"{slot_key} must list specific child tag before armour"
        if "weapon" in ptags and ptags[0] != "weapon":
            assert ptags.index("weapon") > 0, f"{slot_key} must list specific weapon tag before weapon"

    # Specific zero on `gloves` blocks fallback to `armour: 1000` when ordered child -> parent -> default
    ordered_spec = ("gloves", ("gloves",), ("gloves", "armour", "default"), ("Gloves",))
    reversed_spec = ("gloves", ("gloves",), ("armour", "gloves", "default"), ("Gloves",))
    assert _mod_matches_slot("Corrupted", '{"armour": 1000}', '["gloves", "default"]', "[]", ordered_spec) is False
    assert _mod_matches_slot("Corrupted", '{"armour": 1000}', '["gloves", "default"]', "[]", reversed_spec) is True


def test_drop_only_subtype_and_five_slot_matrix_regression(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    assert game_data.query_local_data("mod", "Spell Damage", pob_dir=pob_root, runtime_dir=rt, slot="dagger")["count"] == 1
    assert game_data.query_local_data("mod", "Spell Damage", pob_dir=pob_root, runtime_dir=rt, slot="wand")["count"] == 0

    expected_matrix = {
        "gloves": {
            "AddedFireDamageEnhancedLevel50Mod",
            "LocalIncreasedEnergyShield1",
            "LocalIncreasedEnergyShieldEssenceBootsGloves7",
            "Strength1",
            "V2SocketedMinionGemCorrupted",
        },
        "boots": {
            "LocalIncreaseSocketedMinionGemLevelDelve",
            "LocalIncreasedEnergyShieldEssenceBootsGloves7",
            "V2SocketedMinionGemCorrupted",
        },
        "wand": {"JunMasterVeiledDoubleDamageChance1h"},
        "body_armour": {"V2SocketedMinionGemCorrupted"},
        "ring": {"Strength1"},
    }
    for slot_name, expected_ids in expected_matrix.items():
        got = {
            m["id"]
            for m in game_data.query_local_data("mod", "", pob_dir=pob_root, runtime_dir=rt, slot=slot_name, limit=50)["results"]
        }
        assert got == expected_ids, f"slot={slot_name}: got {got}, expected {expected_ids}"


def test_drop_only_sink_collects_excluded_zero_weight_candidates(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")
    db_path = game_data.ensure_gamedata_index(pob_dir=pob_root, runtime_dir=rt)
    sink: list[dict] = []
    conn = sqlite3.connect(str(db_path))
    try:
        hits = _query_mods(conn, "socketed", "gloves", None, 25, drop_only_sink=sink)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["V2SocketedMinionGemCorrupted"]
    sink_ids = {m["id"] for m in sink}
    assert {
        "LocalIncreaseSocketedMinionGemLevelEnhancedMod",
        "LocalIncreaseSocketedMinionGemLevelDelve",
        "LocalIncreaseSocketedMinionGemLevel2",
    } <= sink_ids
    assert "LocalIncreaseSocketedBowGemLevel2" not in sink_ids


def test_item_inspect_delve_veiled_and_drop_only_fallback_isolation(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    # 1. Drop-only Minion Gem Prefix + weighted Strength Suffix (unaffected by OffSlotZeroWeightStrengthPrefix)
    gloves_minion = "\n".join(
        ["Item Class: Gloves", "Rarity: RARE", "Blight Grip", "Sorcerer Gloves", "--------", "Item Level: 85", "--------",
         "+2 to Level of Socketed Minion Gems", "+10 to Strength"]
    )
    rep_minion = item_inspect.inspect_item(gloves_minion, slot="gloves", pob_dir=pob_root, runtime_dir=rt, include_suggestions=False)
    assert rep_minion["counts"] == {"prefix": 1, "suffix": 1}
    assert rep_minion["certain"] is True
    str_affix = next(a for a in rep_minion["affixes"] if a["group"] == "Strength")
    assert str_affix["ambiguous"] is False
    assert str_affix["tierLevels"] == [11]

    # 2. Delve-only mod (`source == "Delve"`)
    gloves_delve = "\n".join(
        ["Item Class: Gloves", "Rarity: RARE", "Sub Grip", "Sorcerer Gloves", "--------", "Item Level: 85", "--------",
         "Curse Enemies with Frostbite on Hit"]
    )
    rep_delve = item_inspect.inspect_item(gloves_delve, slot="gloves", pob_dir=pob_root, runtime_dir=rt, include_suggestions=False)
    assert rep_delve["affixes"][0]["source"] == "Delve"
    assert rep_delve["certain"] is True

    # 3. Veiled-only mod (`source == "Veiled"`) & Essence exact tier (`+60 ES` above natural `+(10-20)`)
    wand_veiled = "\n".join(
        ["Item Class: Wands", "Rarity: RARE", "Order Horn", "Imbued Wand", "--------", "Item Level: 85", "--------",
         "7% chance to deal Double Damage"]
    )
    rep_veiled = item_inspect.inspect_item(wand_veiled, slot="wand", pob_dir=pob_root, runtime_dir=rt, include_suggestions=False)
    assert rep_veiled["affixes"][0]["source"] == "Veiled"
    assert rep_veiled["certain"] is True

    gloves_ess = "\n".join(
        ["Item Class: Gloves", "Rarity: RARE", "Woe Grip", "Sorcerer Gloves", "--------", "Item Level: 85", "--------",
         "+60 to maximum Energy Shield"]
    )
    rep_ess = item_inspect.inspect_item(gloves_ess, slot="gloves", pob_dir=pob_root, runtime_dir=rt, include_suggestions=False)
    assert rep_ess["affixes"][0]["affix"] == "Essences"
    assert rep_ess["affixes"][0]["matchedTier"] is True
    assert rep_ess["certain"] is True
