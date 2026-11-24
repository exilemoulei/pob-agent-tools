"""Unit & integration tests for `pob_cli.bench` dual-source crafting bench catalog."""

from __future__ import annotations

import re

import pytest

from pob_cli import bench, game_data


@pytest.fixture(autouse=True)
def _reset_bench_cache():
    bench.clear_cache()
    yield
    bench.clear_cache()


def _has_local_engine() -> bool:
    try:
        game_data.ensure_gamedata_index()
        return True
    except Exception:
        return False


class TestEmbeddedSnapshot:
    """Zero-engine embedded snapshot tests (always runnable in CI without PoB engine)."""

    def test_traditional_query_scores_like_its_simplified_form(self):
        def hits(text: str) -> list[tuple[str, float]]:
            found = bench.search("ring1", text, limit=3, allow_engine=False)
            return [(h.recipe.id, round(h.score, 4)) for h in found]

        assert hits("攻擊速度") == hits("攻击速度") != []
        assert hits("暴擊率") == hits("暴击率") != []

    def test_snapshot_loads_with_zero_unknown_affixes_and_clean_text(self):
        recipes, meta = bench.load_recipes(allow_engine=False)
        assert meta["mode"] == "embedded_snapshot"
        assert len(recipes) >= 700
        assert meta["count"] == len(recipes)
        assert meta["costMatched"] / len(recipes) >= 0.90

        range_re = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?\s*\)")
        for r in recipes:
            assert r.affix in ("prefix", "suffix"), f"Invalid affix for {r.id}: {r.affix}"
            assert r.lines, f"Empty lines for {r.id}"
            assert r.slots, f"Empty slots for {r.id}"
            for ln in r.lines:
                assert "<br" not in ln.lower()
                assert "\u2014" not in ln and "\u2013" not in ln
            crafted = r.crafted_lines(use_min=True)
            assert len(crafted) == len(r.lines)
            for cl in crafted:
                assert cl.startswith("{crafted}")
                assert not range_re.search(cl), f"Unresolved range in crafted line: {cl}"

    def test_fallback_when_engine_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        def _boom(*args, **kwargs):
            raise FileNotFoundError("no local PoB engine installed")

        monkeypatch.setattr(game_data, "ensure_gamedata_index", _boom)
        meta = bench.source_metadata(allow_engine=True)
        assert meta["mode"] == "embedded_snapshot"
        assert meta["count"] >= 700

    def test_slot_aliases_and_specific_slots(self):
        assert bench.slot_key("chest") == "body_armour"
        assert bench.slot_key("body") == "body_armour"
        assert bench.slot_key("BodyArmour") == "body_armour"
        assert bench.slot_key("Body Armour") == "body_armour"
        assert bench.slot_key("helm") == "helmet"
        assert bench.slot_key("ring") == "ring1"
        assert bench.slot_key("Ring 1") == "ring1"
        assert bench.slot_key("Ring 2") == "ring2"
        assert bench.slot_key("Ring 3") == "ring3"
        assert bench.slot_key("weapon") == "weapon1"
        assert bench.slot_key("Weapon 1") == "weapon1"
        assert bench.slot_key("Weapon 2") == "weapon2"
        assert bench.slot_key("Quiver") == "quiver"
        assert bench.slot_key("Bow") == "bow"

        chest_recipes = bench.recipes_for_slot("chest", allow_engine=False)
        body_recipes = bench.recipes_for_slot("body_armour", allow_engine=False)
        assert chest_recipes == body_recipes
        assert len(chest_recipes) > 50

        ring_recipes = bench.recipes_for_slot("ring", allow_engine=False)
        ring1_recipes = bench.recipes_for_slot("ring1", allow_engine=False)
        ring2_recipes = bench.recipes_for_slot("ring2", allow_engine=False)
        assert ring_recipes == ring1_recipes == ring2_recipes
        assert len(ring_recipes) > 50

        for slot in ("quiver", "bow", "wand", "shield", "weapon1", "weapon2"):
            assert len(bench.recipes_for_slot(slot, allow_engine=False)) > 10

    def test_affix_filtering_and_validation(self):
        prefixes = bench.list_for_slot("body_armour", affix="prefix", allow_engine=False)
        suffixes = bench.list_for_slot("body_armour", affix="suffix", allow_engine=False)
        assert prefixes and all(r.affix == "prefix" for r in prefixes)
        assert suffixes and all(r.affix == "suffix" for r in suffixes)

        with pytest.raises(ValueError, match="Invalid affix"):
            bench.list_for_slot("body_armour", affix="implicit", allow_engine=False)

    def test_english_and_chinese_search_best_tier_ranking(self):
        en_cold = bench.best("chest", "to Cold Resistance", allow_engine=False)
        zh_cold = bench.best("body_armour", "冰霜抗性", allow_engine=False)
        assert en_cold is not None and zh_cold is not None
        assert en_cold.id == zh_cold.id
        assert en_cold.affix == "suffix"
        assert en_cold.text == "+(29-35)% to Cold Resistance"
        assert en_cold.crafted_lines(use_min=True) == ["{crafted}+29% to Cold Resistance"]
        assert en_cold.crafted_lines(use_min=False) == ["{crafted}+35% to Cold Resistance"]

        # Ring caps Cold Resistance bench craft at +(21-28)% (authoritative ModMaster rule)
        ring_cold = bench.best("ring1", "to Cold Resistance", allow_engine=False)
        assert ring_cold is not None
        assert ring_cold.text == "+(21-28)% to Cold Resistance"

        # Body Armour caps Life bench craft at +(71-85), Gloves at +(56-70)
        en_life_chest = bench.best("body_armour", "maximum life", affix="prefix", allow_engine=False)
        zh_life_chest = bench.best("chest", "最大生命", affix="prefix", allow_engine=False)
        assert en_life_chest is not None and zh_life_chest is not None
        assert en_life_chest.id == zh_life_chest.id
        assert en_life_chest.text == "+(71-85) to maximum Life"

        zh_life_gloves = bench.best("gloves", "最大生命", affix="prefix", allow_engine=False)
        assert zh_life_gloves is not None
        assert zh_life_gloves.text == "+(56-70) to maximum Life"

        # Attack speed EN + ZH on weapon1
        en_as = bench.best("weapon1", "attack speed", affix="suffix", allow_engine=False)
        zh_as = bench.best("weapon1", "攻击速度", affix="suffix", allow_engine=False)
        assert en_as is not None and zh_as is not None
        assert en_as.id == zh_as.id
        assert en_as.text == "(16-20)% increased Attack Speed"

        # Negative mana cost craft ranks -(7-6) above -5 and -4, with conservative safe floor -6
        mana_cost = bench.best("ring1", "non-channelling mana cost", affix="prefix", allow_engine=False)
        assert mana_cost is not None
        assert mana_cost.text == "Non-Channelling Skills have -(7-6) to Total Mana Cost"
        assert mana_cost.crafted_lines(use_min=True) == ["{crafted}Non-Channelling Skills have -6 to Total Mana Cost"]
        assert mana_cost.crafted_lines(use_min=False) == ["{crafted}Non-Channelling Skills have -7 to Total Mana Cost"]

    def test_multiline_and_float_crafted_lines(self):
        # Float leech craft
        leech = bench.best("gloves", "Physical Attack Damage Leeched as Life", allow_engine=False)
        assert leech is not None
        assert leech.crafted_lines(use_min=True) == ["{crafted}0.3% of Physical Attack Damage Leeched as Life"]
        assert leech.crafted_lines(use_min=False) == ["{crafted}0.5% of Physical Attack Damage Leeched as Life"]
        assert leech.roll_text == "0.3-0.5"

        # Multi-line hybrid craft
        impale_hits = bench.search("weapon1", "chance to Impale", limit=3, affix="prefix", allow_engine=False)
        assert impale_hits
        top = impale_hits[0].recipe
        assert len(top.lines) == 2
        crafted_min = top.crafted_lines(use_min=True)
        assert len(crafted_min) == 2
        assert all(line.startswith("{crafted}") for line in crafted_min)

    def test_as_dict_and_format_recipe(self):
        recipe = bench.best("body_armour", "to Cold Resistance", allow_engine=False)
        assert recipe is not None
        d = recipe.as_dict()
        expected_keys = {
            "id",
            "text",
            "textZh",
            "lines",
            "linesZh",
            "template",
            "affix",
            "group",
            "level",
            "slots",
            "classes",
            "values",
            "minValue",
            "maxValue",
            "craftedLinesMin",
            "cost",
            "unlock",
        }
        assert expected_keys.issubset(d.keys())
        assert d["affix"] == "suffix"
        assert d["minValue"] == 29.0
        assert d["maxValue"] == 35.0
        assert d["craftedLinesMin"] == ["{crafted}+29% to Cold Resistance"]

        fmt_zh = bench.format_recipe(recipe, show_zh=True)
        fmt_en = bench.format_recipe(recipe, show_zh=False)
        assert "[suffix]" in fmt_zh
        assert "冰霜抗性" in fmt_zh
        assert "冰霜抗性" not in fmt_en
        assert "Cost:" in fmt_en

    def test_can_craft_on_item(self):
        cold = bench.best("body_armour", "to Cold Resistance", allow_engine=False)
        assert cold is not None

        ok, msg = bench.can_craft_on_item(cold, {"# to maximum life", "#% to fire resistance"})
        assert ok is True
        assert "Craftable" in msg

        ok_rep, msg_rep = bench.can_craft_on_item(
            cold,
            {"# to maximum life"},
            has_crafted_mod=True,
        )
        assert ok_rep is True
        assert "replaced" in msg_rep.lower()

        # Conflict via normalized template or raw PoB mod line
        ok_dup, msg_dup = bench.can_craft_on_item(cold, {"#% to cold resistance"})
        assert ok_dup is False
        assert "conflict" in msg_dup.lower()

        ok_dup_raw, _ = bench.can_craft_on_item(cold, {"{crafted}+34% to Cold Resistance"})
        assert ok_dup_raw is False

        # Conflict on a component line of a multi-line hybrid craft
        hybrid = bench.best("ring1", "Minions have to all Elemental Resistances", allow_engine=False)
        assert hybrid is not None and len(hybrid.lines) == 2
        ok_hyb, _ = bench.can_craft_on_item(hybrid, {"+10% to all Elemental Resistances"})
        assert ok_hyb is False


@pytest.mark.skipif(not _has_local_engine(), reason="Local PoB engine not installed")
class TestLocalModMasterSource:
    """Authoritative local ModMaster.lua SQLite integration tests."""

    def test_local_modmaster_loads_and_matches_snapshot_parity(self):
        engine_recipes, engine_meta = bench.load_recipes(allow_engine=True)
        assert engine_meta["mode"] == "local_modmaster"
        assert engine_meta["dbPath"] is not None
        assert len(engine_recipes) >= 700
        assert all(r.affix in ("prefix", "suffix") for r in engine_recipes)
        assert engine_meta["costMatched"] / len(engine_recipes) >= 0.90

        # Bilingual search works identically against local_modmaster
        hit_en = bench.best("chest", "to Cold Resistance", allow_engine=True)
        hit_zh = bench.best("chest", "冰霜抗性", allow_engine=True)
        assert hit_en is not None and hit_zh is not None
        assert hit_en.id == hit_zh.id
        assert hit_en.text == "+(29-35)% to Cold Resistance"

        # Disambiguated IncreasedMana tiers carry distinct accurate costs/unlocks
        mana_recipes = [r for r in engine_recipes if r.group == "IncreasedMana" and r.text == "+(35-44) to maximum Mana"]
        assert len(mana_recipes) == 2
        by_lvl = {r.level: r for r in mana_recipes}
        assert by_lvl[20].cost == "3x Orb of Augmentation"
        assert by_lvl[40].cost == "6x Orb of Augmentation"


def test_slot_key_takes_the_chinese_slot_names_other_commands_take() -> None:
    for name, key in (
        ("手套", "gloves"),
        ("頭盔", "helmet"),
        ("头盔", "helmet"),
        ("腰帶", "belt"),
        ("左戒指", "ring1"),
        ("Body Armour", "body_armour"),
        ("shield", "shield"),
        ("盾牌", "shield"),
        ("箭袋", "quiver"),
        ("no-such-slot", "no_such_slot"),
        ("肢体躯干", "肢体躯干"),
    ):
        assert bench.slot_key(name) == key
