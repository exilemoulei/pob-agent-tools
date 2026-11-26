"""Unit and real-engine tests for Layer 2 (`pob interactions`, ModStore-faithful flag matching, and diagnose red-mod warnings)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli import cli as cli_mod
from pob_cli.analyzer.diagnose import diagnose_build
from pob_cli.analyzer.diagnose_probes import _gaps_from_unsupported_mods, _probe_unsupported_mods
from pob_cli.analyzer.interactions import (
    KEYWORD_FLAG_BITS,
    KEYWORD_FLAG_MATCH_ALL,
    MOD_FLAG_BITS,
    classify_mod_entry,
    inspect_interactions,
    keyword_flags_match,
    mod_flags_match,
)
from pob_cli.bridge_client import BridgeWorker
from pob_cli.errors import DaemonRequestError
from pob_cli.formatters.interactions_md import format_interactions_markdown
from pob_cli.importers import xml_to_share_code
from pob_cli.snapshot import load_build

_SPELL_BUILD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Mismatch Ring
Coral Ring
Item Level: 84
Implicits: 1
+30 to maximum Life
25% increased Spell Damage
30% increased Attack Damage
12% increased Attack and Cast Speed
+42% to Fire Resistance
Gain 500% Astral Paradox Damage on Tuesdays
    </Item>
    <ItemSet id="1">
      <Slot name="Ring 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <CustomModifierBlock title="Default" enabled="true">
+15% to Critical Strike Multiplier
Invalid custom mod line for diagnose warning test
      </CustomModifierBlock>
    </ConfigSet>
  </Config>
</PathOfBuilding>
"""


def test_modstore_faithful_bitwise_flag_and_keyword_matching() -> None:
    spell_proj = MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Cast"] | MOD_FLAG_BITS["Projectile"]
    assert mod_flags_match(spell_proj, 0) is True
    assert mod_flags_match(spell_proj, MOD_FLAG_BITS["Spell"]) is True
    assert mod_flags_match(spell_proj, MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Projectile"]) is True
    assert mod_flags_match(spell_proj, MOD_FLAG_BITS["Attack"]) is False
    assert mod_flags_match(spell_proj, MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Area"]) is False
    # Dual-wield offhand fallback
    assert mod_flags_match(MOD_FLAG_BITS["Attack"] | MOD_FLAG_BITS["Sword"], MOD_FLAG_BITS["Axe"], offhand_flags=MOD_FLAG_BITS["Attack"] | MOD_FLAG_BITS["Axe"]) is True

    # KeywordFlag: match-ANY by default (Global.lua:190-215)
    fire_hit = KEYWORD_FLAG_BITS["Fire"] | KEYWORD_FLAG_BITS["Hit"]
    fire_or_cold = KEYWORD_FLAG_BITS["Fire"] | KEYWORD_FLAG_BITS["Cold"]
    cold_or_light = KEYWORD_FLAG_BITS["Cold"] | KEYWORD_FLAG_BITS["Lightning"]
    assert keyword_flags_match(fire_hit, 0) is True
    assert keyword_flags_match(fire_hit, fire_or_cold) is True
    assert keyword_flags_match(fire_hit, cold_or_light) is False
    # With MatchAll bit set, ALL keyword bits must be present
    assert keyword_flags_match(fire_hit, fire_or_cold | KEYWORD_FLAG_MATCH_ALL) is False
    assert keyword_flags_match(fire_hit, fire_hit | KEYWORD_FLAG_MATCH_ALL) is True


def test_classify_mod_entry_all_five_verdicts_and_multi_mod_no_false_positive() -> None:
    spell_main = {
        "skillName": "Fireball",
        "mainModFlags": MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Cast"] | MOD_FLAG_BITS["Projectile"],
        "mainModFlagNames": ["Cast", "Projectile", "Spell"],
        "mainKeywordFlags": KEYWORD_FLAG_BITS["Fire"] | KEYWORD_FLAG_BITS["Hit"],
        "mainKeywordFlagNames": ["Fire", "Hit"],
        "skillTypes": ["Spell", "Projectile", "Fire", "Damage"],
        "damageTypes": ["Fire"],
    }

    # 1. SCALES_MAIN_SKILL
    c_spell = classify_mod_entry(
        {"rawLine": "25% increased Spell Damage", "unsupported": False, "mods": [{"name": "Damage", "type": "INC", "value": 25, "flags": MOD_FLAG_BITS["Spell"], "keywordFlags": 0, "tags": []}]},
        spell_main,
    )
    assert c_spell["verdict"] == "SCALES_MAIN_SKILL"

    # 2. FLAG_MISMATCH naming exact mismatched flag
    c_atk = classify_mod_entry(
        {"rawLine": "30% increased Attack Damage", "unsupported": False, "mods": [{"name": "Damage", "type": "INC", "value": 30, "flags": MOD_FLAG_BITS["Attack"], "keywordFlags": 0, "tags": []}]},
        spell_main,
    )
    assert c_atk["verdict"] == "FLAG_MISMATCH"
    assert "ModFlag:Attack" in c_atk["mismatchedFlags"]

    # 3. Multi-mod line ("Attack and Cast Speed") on Spell build must NOT falsely condemn as FLAG_MISMATCH
    c_both = classify_mod_entry(
        {
            "rawLine": "12% increased Attack and Cast Speed",
            "unsupported": False,
            "mods": [
                {"name": "Speed", "type": "INC", "value": 12, "flags": MOD_FLAG_BITS["Attack"], "keywordFlags": 0, "tags": []},
                {"name": "Speed", "type": "INC", "value": 12, "flags": MOD_FLAG_BITS["Cast"], "keywordFlags": 0, "tags": []},
            ],
        },
        spell_main,
    )
    assert c_both["verdict"] == "SCALES_MAIN_SKILL"
    assert c_both["mismatchedFlags"] == []

    # 4. CONDITIONAL_PREMISE
    c_cond = classify_mod_entry(
        {
            "rawLine": "25% increased Damage while on Full Life",
            "unsupported": False,
            "mods": [{"name": "Damage", "type": "INC", "value": 25, "flags": 0, "keywordFlags": 0, "tags": [{"type": "Condition", "var": "FullLife"}]}],
        },
        spell_main,
    )
    assert c_cond["verdict"] == "CONDITIONAL_PREMISE"
    assert "Condition:FullLife" in c_cond["premises"]

    # 5. DEFENCE_OR_GLOBAL
    c_def = classify_mod_entry(
        {"rawLine": "+50 to maximum Life", "unsupported": False, "mods": [{"name": "Life", "type": "BASE", "value": 50, "flags": 0, "keywordFlags": 0, "tags": []}]},
        spell_main,
    )
    assert c_def["verdict"] == "DEFENCE_OR_GLOBAL"

    # 6. UNSUPPORTED_BY_POB
    c_red = classify_mod_entry({"rawLine": "Bogus red line", "unsupported": True, "extra": "Bogus red line", "mods": []}, spell_main)
    assert c_red["verdict"] == "UNSUPPORTED_BY_POB"


def test_legacy_worker_and_diagnose_probe_make_zero_extra_rpc() -> None:
    class _FakeNoCap:
        def __init__(self) -> None:
            self.caps: dict[str, Any] = {}
            self.calls: list[str] = []

        def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            self.calls.append(method)
            return {}

    w = _FakeNoCap()
    assert _probe_unsupported_mods(w) is None  # type: ignore[arg-type]
    assert w.calls == []
    with pytest.raises(DaemonRequestError, match="interactionsProbe"):
        inspect_interactions(w)  # type: ignore[arg-type]

    gaps = _gaps_from_unsupported_mods(
        {
            "customMods": [{"blockTitle": "Default", "rawLine": "Bogus custom"}],
            "items": [{"slot": "Ring 1", "itemName": "Test Ring", "rawLine": "Red mod", "active": True}],
            "totalCount": 2,
        }
    )
    assert len(gaps) == 2
    assert all(g["category"] == "UnsupportedMod" and "[UNSUPPORTED-MOD]" in g["message"] for g in gaps)


def test_cli_interactions_json_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_res = {
        "mode": "targeted",
        "unsupportedOnly": False,
        "mainSkill": {
            "skillName": "Fireball",
            "mainModFlagNames": ["Cast", "Projectile", "Spell"],
            "mainKeywordFlagNames": ["Fire", "Hit"],
            "damageTypes": ["Fire"],
        },
        "verdictCounts": {"FLAG_MISMATCH": 1},
        "lines": [
            {
                "rawLine": "30% increased Attack Damage",
                "verdict": "FLAG_MISMATCH",
                "unsupported": False,
                "mismatchedFlags": ["ModFlag:Attack"],
                "premises": [],
                "mods": [{"name": "Damage", "type": "INC", "value": 30}],
            }
        ],
    }
    monkeypatch.setattr("pob_cli.cli_interactions.invoke_daemon", lambda action, params, **_kw: fake_res)
    runner = CliRunner()
    r_json = runner.invoke(cli_mod.main, ["interactions", "--mod", "30% increased Attack Damage", "--json"])
    assert r_json.exit_code == 0, r_json.output
    parsed = json.loads(r_json.output)
    assert parsed["lines"][0]["verdict"] == "FLAG_MISMATCH"

    r_md = runner.invoke(cli_mod.main, ["interactions", "--mod", "30% increased Attack Damage"])
    assert r_md.exit_code == 0, r_md.output
    assert "FLAG_MISMATCH" in r_md.output
    assert "ModFlag:Attack" in r_md.output
    assert "Fireball" in format_interactions_markdown(fake_res)


def test_real_engine_interactions_and_diagnose_red_mods(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_interactions_l2")
    try:
        worker.start()
        assert (worker.caps or {}).get("interactionsProbe") is True
        load_build(worker, xml_code=xml_to_share_code(_SPELL_BUILD_XML))

        # 1. Targeted --slot & --mod classification on Fireball (Spell) build
        targeted = inspect_interactions(
            worker,
            slot_name="Ring 1",
            mod_lines=[
                "30% increased Attack Damage",
                "25% increased Spell Damage",
                "25% increased Damage while on Full Life",
                "Not a real PoE mod at all",
            ],
        )
        assert targeted["mainSkill"]["skillName"] == "Fireball"
        by_line = {row["rawLine"]: row for row in targeted["lines"]}
        assert by_line["30% increased Attack Damage"]["verdict"] == "FLAG_MISMATCH"
        assert "ModFlag:Attack" in by_line["30% increased Attack Damage"]["mismatchedFlags"]
        assert by_line["25% increased Spell Damage"]["verdict"] == "SCALES_MAIN_SKILL"
        assert by_line["25% increased Damage while on Full Life"]["verdict"] == "CONDITIONAL_PREMISE"
        assert by_line["Not a real PoE mod at all"]["verdict"] == "UNSUPPORTED_BY_POB"

        slot_by_line = {row["rawLine"]: row for row in targeted["slotLines"]}
        assert slot_by_line["12% increased Attack and Cast Speed"]["verdict"] == "SCALES_MAIN_SKILL"
        assert slot_by_line["30% increased Attack Damage"]["verdict"] == "FLAG_MISMATCH"
        assert any("Astral Paradox" in k and v["verdict"] == "UNSUPPORTED_BY_POB" for k, v in slot_by_line.items())
        json.dumps(targeted, allow_nan=False)

        # 2. --unsupported-only fast path catches both customMods and Ring 1 red mods
        unsup_only = inspect_interactions(worker, unsupported_only=True)
        assert unsup_only["mode"] == "unsupported_only"
        assert unsup_only["unsupportedMods"]["totalCount"] >= 2
        assert any("Invalid custom mod line" in c["rawLine"] for c in unsup_only["unsupportedMods"]["customMods"])
        assert any("Astral Paradox" in it["rawLine"] for it in unsup_only["unsupportedMods"]["items"])

        # 3. Build-wide mode catches both red mods and dead Attack Damage flag mismatch on Ring 1
        wide = inspect_interactions(worker)
        assert wide["mode"] == "build_wide"
        assert wide["unsupportedMods"]["totalCount"] >= 2
        assert any(m["slot"] == "Ring 1" and "Attack Damage" in m["rawLine"] for m in wide["flagMismatches"])
        md = format_interactions_markdown(wide)
        assert "UNSUPPORTED_BY_POB" in md and "FLAG_MISMATCH" in md

        # 4. pob diagnose automatically surfaces [UNSUPPORTED-MOD] warnings
        diag = diagnose_build(worker, no_rank=True)
        unsup_gaps = [g for g in (diag.get("healthGaps") or []) if g.get("category") == "UnsupportedMod"]
        assert len(unsup_gaps) >= 2
        assert any("[UNSUPPORTED-MOD]" in g["message"] and "Invalid custom mod line" in g["message"] for g in unsup_gaps)
        assert any("[UNSUPPORTED-MOD]" in g["message"] and "Astral Paradox" in g["message"] for g in unsup_gaps)
        assert (diag.get("buildMechanics") or {}).get("unsupportedMods", {}).get("totalCount", 0) >= 2
        json.dumps(diag, allow_nan=False)
    finally:
        worker.stop()


def test_hard_mismatch_precedence_minion_conversions_skill_tags_and_cache() -> None:
    from pob_cli.daemon.read_cache import READ_CACHE_ACTIONS, SlotReadCache
    from pob_cli.daemon.slots import _STATE_META_ACTIONS

    # 1. Hard mismatch precedence: non-offensive ManaCost with NotSkillType:Channel on a Channelling skill
    chan_skill = {
        "skillName": "Cyclone",
        "skillId": "Cyclone",
        "mainModFlags": MOD_FLAG_BITS["Attack"] | MOD_FLAG_BITS["Melee"] | MOD_FLAG_BITS["Area"],
        "mainKeywordFlags": KEYWORD_FLAG_BITS["Attack"] | KEYWORD_FLAG_BITS["Physical"],
        "skillTypes": ["Attack", "Melee", "Area", "Channel"],
    }
    mana_not_chan = classify_mod_entry(
        {
            "rawLine": "-7 to Total Mana Cost of Non-Channelling Skills",
            "unsupported": False,
            "mods": [
                {
                    "name": "ManaCost",
                    "type": "BASE",
                    "value": -7,
                    "flags": 0,
                    "keywordFlags": 0,
                    "tags": [{"type": "SkillType", "skillTypeName": "Channel", "neg": True}],
                }
            ],
        },
        chan_skill,
    )
    assert mana_not_chan["verdict"] == "FLAG_MISMATCH"
    assert "NotSkillType:Channel" in mana_not_chan["mismatchedFlags"]

    # 2. SkillName mismatch ("Fireball" on "Arc") & transfigured matching ("Flame Surge" on "Flame Surge of Combusting")
    arc_skill = {
        "skillName": "Arc",
        "skillId": "Arc",
        "mainModFlags": MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Cast"],
        "mainKeywordFlags": KEYWORD_FLAG_BITS["Spell"] | KEYWORD_FLAG_BITS["Lightning"],
        "skillTypes": ["Spell", "Lightning", "Damage"],
    }
    fb_on_arc = classify_mod_entry(
        {
            "rawLine": "Fireball deals 25% increased Damage",
            "unsupported": False,
            "mods": [
                {
                    "name": "Damage",
                    "type": "INC",
                    "value": 25,
                    "flags": 0,
                    "keywordFlags": 0,
                    "tags": [{"type": "SkillName", "skillName": "Fireball", "includeTransfigured": True}],
                }
            ],
        },
        arc_skill,
    )
    assert fb_on_arc["verdict"] == "FLAG_MISMATCH"
    assert "SkillName:Fireball" in fb_on_arc["mismatchedFlags"]

    fs_comb = {**arc_skill, "skillName": "Flame Surge of Combusting"}
    fs_match = classify_mod_entry(
        {
            "rawLine": "Flame Surge deals 25% increased Damage",
            "unsupported": False,
            "mods": [
                {
                    "name": "Damage",
                    "type": "INC",
                    "value": 25,
                    "flags": 0,
                    "keywordFlags": 0,
                    "tags": [{"type": "SkillName", "skillName": "Flame Surge", "includeTransfigured": True}],
                }
            ],
        },
        fs_comb,
    )
    assert fs_match["verdict"] != "FLAG_MISMATCH"

    # 3. Accuracy is DEFENCE_OR_GLOBAL on Spell build, SCALES_MAIN_SKILL on Attack build
    acc_entry = {
        "rawLine": "+300 to Accuracy Rating",
        "unsupported": False,
        "mods": [{"name": "Accuracy", "type": "BASE", "value": 300, "flags": 0, "keywordFlags": 0, "tags": []}],
    }
    assert classify_mod_entry(acc_entry, arc_skill)["verdict"] == "DEFENCE_OR_GLOBAL"
    assert classify_mod_entry(acc_entry, chan_skill)["verdict"] == "SCALES_MAIN_SKILL"

    # 4. Minion build rejects player Spell Damage and accepts MinionModifier; non-minion honors Spiritual Aid
    minion_main = {
        "skillName": "Summon Raging Spirit",
        "isMinion": True,
        "mainModFlags": MOD_FLAG_BITS["Spell"] | MOD_FLAG_BITS["Cast"],
        "mainKeywordFlags": KEYWORD_FLAG_BITS["Minion"] | KEYWORD_FLAG_BITS["Fire"],
        "skillTypes": ["Spell", "Minion", "Fire"],
        "minionSkill": {
            "skillName": "Melee",
            "mainModFlags": MOD_FLAG_BITS["Attack"] | MOD_FLAG_BITS["Melee"] | MOD_FLAG_BITS["Hit"],
            "mainKeywordFlags": KEYWORD_FLAG_BITS["Attack"] | KEYWORD_FLAG_BITS["Fire"],
            "skillTypes": ["Attack", "Melee"],
        },
    }
    player_spell_on_minion = classify_mod_entry(
        {
            "rawLine": "25% increased Spell Damage",
            "unsupported": False,
            "mods": [{"name": "Damage", "type": "INC", "value": 25, "flags": MOD_FLAG_BITS["Spell"], "keywordFlags": 0, "tags": []}],
        },
        minion_main,
    )
    assert player_spell_on_minion["verdict"] == "FLAG_MISMATCH"
    assert any("Minions" in r for r in player_spell_on_minion["mismatchedFlags"])

    minion_mod_entry = {
        "rawLine": "Minions deal 25% increased Damage",
        "unsupported": False,
        "mods": [
            {
                "name": "MinionModifier",
                "type": "LIST",
                "value": {"mod": {"name": "Damage", "type": "INC", "value": 25, "flags": 0, "keywordFlags": 0, "tags": []}},
                "flags": 0,
                "keywordFlags": 0,
                "tags": [],
            }
        ],
    }
    assert classify_mod_entry(minion_mod_entry, minion_main)["verdict"] == "SCALES_MAIN_SKILL"
    assert classify_mod_entry(minion_mod_entry, arc_skill)["verdict"] == "FLAG_MISMATCH"
    arc_spiritual = {**arc_skill, "conversions": {"minionDamageAppliesToPlayer": True}}
    sa_cls = classify_mod_entry(minion_mod_entry, arc_spiritual)
    assert sa_cls["verdict"] == "SCALES_MAIN_SKILL"
    assert "ConvertedVia:SpiritualAid" in sa_cls["premises"]

    # 5. Bespoke Mageblood/Enkindled flask noise folding + inactive flask exclusion + [INACTIVE] / partialMismatches MD
    gaps = _gaps_from_unsupported_mods(
        {
            "customMods": [],
            "items": [
                {"slot": "Belt", "itemName": "Mageblood", "rawLine": "Magic Utility Flasks cannot be Used", "active": True},
                {"slot": "Flask 1", "itemName": "Quicksilver Flask", "rawLine": "Gains no Charges during Effect", "active": True},
                {"slot": "Flask 2", "itemName": "Granite Flask", "rawLine": "Used when Charges reach full", "active": True},
                {"slot": "Flask 3", "itemName": "Diamond Flask", "rawLine": "Unticked bogus line xyz", "active": False},
            ],
            "totalCount": 4,
        }
    )
    assert gaps == []

    md_inactive = format_interactions_markdown(
        {
            "mode": "unsupported_only",
            "unsupportedMods": {
                "customMods": [],
                "items": [{"slot": "Flask 3", "itemName": "Diamond Flask", "rawLine": "Unticked bogus line xyz", "active": False}],
                "totalCount": 1,
            },
        }
    )
    assert "[INACTIVE]" in md_inactive

    # 6. Daemon read cache + state meta integration with alias canonicalization
    assert "interactions_inspect" in READ_CACHE_ACTIONS
    assert "interactions_inspect" in _STATE_META_ACTIONS
    cache = SlotReadCache()
    cache.store("main", 1, "zh-CN", "interactions_inspect", {"slot": "Ring 1", "mods": ["A"], "unsupported_only": False}, {"mode": "targeted"})
    hit = cache.lookup("main", 1, "zh-CN", "interactions_inspect", {"slotName": "Ring 1", "lines": ["A"], "unsupportedOnly": 0})
    assert hit is not None and hit.get("cached") is True and hit.get("mode") == "targeted"


_DOT_MINION_MAGEBLOOD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="IgniteMain">
        <Gem nameSpec="Flame Surge of Combusting" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Combustion" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="PoisonMain">
        <Gem nameSpec="Bladefall of Impaling" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Chance to Poison" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="MinionMain">
        <Gem nameSpec="Summon Raging Spirit" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="ArcMain">
        <Gem nameSpec="Arc" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: UNIQUE
Mageblood
Heavy Belt
Item Level: 84
Implicits: 1
+30 to Strength
Magic Utility Flasks cannot be Used
Leftmost 4 Magic Utility Flasks constantly apply their Flask Effects to you
Magic Utility Flask Effects cannot be Removed
    </Item>
    <Item id="2">
Rarity: MAGIC
Quicksilver Flask
Item Level: 84
Implicits: 1
{crafted}70% increased effect
Gains no Charges during Effect
    </Item>
    <Item id="3">
Rarity: MAGIC
Granite Flask
Item Level: 84
Implicits: 0
Unticked flask bogus unparsed line xyz
    </Item>
    <ItemSet id="1">
      <Slot name="Belt" itemId="1"/>
      <Slot name="Flask 1" itemId="2" active="true"/>
      <Slot name="Flask 2" itemId="3" active="false"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""


def test_real_engine_ignite_poison_minion_spiritual_aid_and_mageblood_noise(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_interactions_l2_hard")
    try:
        worker.start()
        load_build(worker, xml_code=xml_to_share_code(_DOT_MINION_MAGEBLOOD_XML))

        # 1. Ignite main skill (Flame Surge of Combusting): Burning Damage & DoT must NOT be FLAG_MISMATCH
        ig_res = inspect_interactions(
            worker,
            mod_lines=["30% increased Burning Damage", "25% increased Damage over Time"],
        )
        ig_by = {r["rawLine"]: r for r in ig_res["lines"]}
        assert ig_by["30% increased Burning Damage"]["verdict"] == "SCALES_MAIN_SKILL"
        assert ig_by["25% increased Damage over Time"]["verdict"] == "SCALES_MAIN_SKILL"

        # 2. Mageblood + Enkindled Flask + unticked Flask 2 bogus line -> 0 MEDIUM [UNSUPPORTED-MOD] noise in diagnose!
        diag = diagnose_build(worker, no_rank=True)
        med_unsup = [
            g for g in (diag.get("healthGaps") or []) if g.get("category") == "UnsupportedMod" and g.get("severity") == "MEDIUM"
        ]
        assert med_unsup == [], f"Unexpected MEDIUM UnsupportedMod noise: {med_unsup}"

        # 3. Poison main skill (Bladefall of Impaling + Chance to Poison): Damage with Poison & DoT scale!
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 2})
        po_res = inspect_interactions(
            worker,
            mod_lines=["30% increased Damage with Poison", "25% increased Damage over Time"],
        )
        po_by = {r["rawLine"]: r for r in po_res["lines"]}
        assert po_by["30% increased Damage with Poison"]["verdict"] == "SCALES_MAIN_SKILL"
        assert po_by["25% increased Damage over Time"]["verdict"] == "SCALES_MAIN_SKILL"

        # 4. Minion main skill (SRS): rejects player Spell Damage, accepts MinionModifier
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 3})
        mn_res = inspect_interactions(
            worker,
            mod_lines=["25% increased Spell Damage", "Minions deal 25% increased Damage"],
        )
        mn_by = {r["rawLine"]: r for r in mn_res["lines"]}
        assert mn_by["25% increased Spell Damage"]["verdict"] == "FLAG_MISMATCH"
        assert any("Minions" in r for r in mn_by["25% increased Spell Damage"]["mismatchedFlags"])
        assert mn_by["Minions deal 25% increased Damage"]["verdict"] == "SCALES_MAIN_SKILL"

        # 5. Arc main skill: "Fireball deals 25% increased Damage" mismatches SkillName; MinionModifier mismatches without Spiritual Aid
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 4})
        arc_res = inspect_interactions(
            worker,
            mod_lines=["Minions deal 25% increased Damage"],
        )
        assert arc_res["lines"][0]["verdict"] == "FLAG_MISMATCH"
    finally:
        worker.stop()
