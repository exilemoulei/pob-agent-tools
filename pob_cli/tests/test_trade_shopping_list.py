"""Tests for Layer 3: L0 zero-HTTP full-build shopping list (`pob trade shopping-list`)."""

from __future__ import annotations

import base64
import gzip
import json
import time
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data
from support import perf_budget

from pob_cli.cli_trade import trade_group
from pob_cli.trade_eval.shopping_list import build_shopping_list
from pob_cli.trade_query import load_local_trade_stats

_SAMPLE_STATS: list[dict[str, Any]] = [
    {"id": "enchant.stat_3086156145", "text": "Adds # Passive Skills", "type": "enchant"},
    {"id": "enchant.stat_3948993189|3", "text": "Added Small Passive Skills grant: 12% increased Lightning Damage", "type": "enchant"},
    {"id": "explicit.stat_4022743870", "text": "1 Added Passive Skill is Adrenaline", "type": "explicit"},
    {"id": "explicit.stat_2460506030|47389", "text": "Allocates Profane Bloom if you have the matching modifier on Forbidden Flame", "type": "explicit"},
    {"id": "explicit.stat_1190333629|47389", "text": "Allocates Profane Bloom if you have the matching modifier on Forbidden Flesh", "type": "explicit"},
    {"id": "explicit.stat_3642528642|4", "text": "Only affects Passives in Very Large Ring", "type": "explicit"},
    {"id": "explicit.stat_2422708892|54307", "text": "Passive Skills in Radius of Acrobatics can be Allocated without being connected to your tree Passage", "type": "explicit"},
    {"id": "explicit.stat_2901986750", "text": "+#% to all Elemental Resistances", "type": "explicit"},
    {"id": "explicit.stat_1873457881", "text": "#% additional Physical Damage Reduction while affected by Determination", "type": "explicit"},
    {"id": "explicit.stat_2690790844", "text": "#% increased Life Recovery Rate while affected by Vitality", "type": "explicit"},
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "fractured.stat_3299347043", "text": "+# to maximum Life", "type": "fractured"},
    {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
    {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
    {"id": "explicit.stat_124131830", "text": "+# to Level of all Spell Skill Gems", "type": "explicit"},
    {"id": "explicit.stat_1050105434", "text": "+# to maximum Mana", "type": "explicit"},
    {"id": "explicit.stat_3695891184", "text": "25% increased effect", "type": "explicit"},
    {"id": "explicit.stat_2866361420", "text": "#% increased Armour during Effect", "type": "explicit"},
    {"id": "implicit.stat_2923486259", "text": "+#% to Chaos Resistance", "type": "implicit"},
    {"id": "explicit.stat_2763429652", "text": "Rightmost # Magic Utility Flasks constantly apply their Flask Effects to you", "type": "explicit"},
]


def _make_full_build_payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    items_res = {
        "buildInfo": {"className": "Witch", "ascendClassName": "Occultist", "level": 98},
        "equippedSlots": [
            {
                "slotName": "Weapon 1",
                "itemId": 1,
                "raw": (
                    "Rarity: UNIQUE\nLegacy of the Rose\nJudgement Staff\nSockets: B-B-B-B-B-B\n"
                    "Implicits: 1\n+18% Chance to Block Attack Damage while wielding a Staff\n"
                    "+5 to Level of all Spell Skill Gems\n"
                ),
            },
            {
                "slotName": "Body Armour",
                "itemId": 2,
                "raw": (
                    "Rarity: RARE\nDoom Shell\nVaal Regalia\nSockets: B-B-B-B-G-R\nImplicits: 0\n"
                    "{fractured}+110 to maximum Life\n+44% to Fire Resistance\n+42% to Cold Resistance\n"
                    "Shaper Item\nFractured Item\n"
                ),
            },
            {
                "slotName": "Amulet",
                "itemId": 3,
                "raw": (
                    "Rarity: UNIQUE\nAshes of the Stars\nOnyx Amulet\nImplicits: 2\n"
                    "{enchant}Allocates Executioner\n+16 to all Attributes\n"
                    "+1 to Level of all Skill Gems\n"
                ),
            },
            {
                "slotName": "Belt",
                "itemId": 4,
                "raw": (
                    "Rarity: UNIQUE\nFoulborn Mageblood\nHeavy Belt\nImplicits: 1\n"
                    "+30 to Strength\n{mutated}Rightmost 4 Magic Utility Flasks constantly apply their Flask Effects to you\n"
                ),
            },
            {
                "slotName": "Flask 1",
                "isFlask": True,
                "itemId": 5,
                "raw": (
                    "Rarity: MAGIC\nAbecedarian's Quicksilver Flask of the Armadillo\nImplicits: 0\n"
                    "25% increased effect\n58% increased Armour during Effect\n"
                ),
            },
            {
                "slotName": "Flask 2",
                "isFlask": True,
                "itemId": 6,
                "raw": "Rarity: UNIQUE\nProgenesis\nAmethyst Flask\nImplicits: 0\n+35% to Chaos Resistance\n",
            },
            {
                "slotName": "Jewel 1",
                "nodeId": 26725,
                "socketIndex": 1,
                "allocated": True,
                "itemId": 7,
                "raw": (
                    "Rarity: RARE\nCluster Gem\nLarge Cluster Jewel\nImplicits: 2\n"
                    "{enchant}Adds 8 Passive Skills\n"
                    "{enchant}Added Small Passive Skills grant: 12% increased Lightning Damage\n"
                    "1 Added Passive Skill is Adrenaline\n"
                ),
            },
            {
                "slotName": "Jewel 2",
                "nodeId": 36634,
                "socketIndex": 2,
                "allocated": True,
                "itemId": 8,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                    "30% increased Life Recovery Rate while affected by Vitality\n"
                ),
            },
            {
                "slotName": "Jewel 3",
                "nodeId": 2491,
                "socketIndex": 3,
                "allocated": True,
                "itemId": 9,
                "raw": (
                    "Rarity: UNIQUE\nForbidden Flame\nCrimson Jewel\nImplicits: 0\n"
                    "Allocates Profane Bloom if you have the matching modifier on Forbidden Flesh\n"
                ),
            },
            {
                "slotName": "Jewel 4",
                "nodeId": 61834,
                "socketIndex": 4,
                "allocated": True,
                "itemId": 10,
                "raw": (
                    "Rarity: UNIQUE\nThread of Hope\nCrimson Jewel\nImplicits: 0\n"
                    "Only affects Passives in Very Large Ring\n-12% to all Elemental Resistances\n"
                ),
            },
            {
                "slotName": "Jewel 5",
                "nodeId": 6230,
                "socketIndex": 5,
                "allocated": True,
                "itemId": 11,
                "raw": (
                    "Rarity: UNIQUE\nImpossible Escape\nViridian Jewel\nImplicits: 0\n"
                    "Passives in Radius of Acrobatics can be Allocated without being connected to your tree\n"
                ),
            },
            {
                "slotName": "Jewel 6",
                "nodeId": 41263,
                "socketIndex": 6,
                "allocated": True,
                "itemId": 12,
                "raw": (
                    "Rarity: UNIQUE\nGlorious Vanity\nTimeless Jewel\nImplicits: 0\n"
                    "Bathed in the blood of 4512 sacrificed in the name of Mazcotl\n"
                ),
            },
            {
                # Unallocated jewel socket must be skipped!
                "slotName": "Jewel 7",
                "nodeId": 99999,
                "socketIndex": 7,
                "allocated": False,
                "itemId": 13,
                "raw": "Rarity: UNIQUE\nUnnatural Instinct\nViridian Jewel\nImplicits: 0\n",
            },
        ],
    }
    skills_res = {
        "groups": [
            {
                "index": 1,
                "slot": "Weapon 1",
                "enabled": True,
                "gems": [
                    {"nameSpec": "Lightning Tendrils of Eccentricity", "level": 21, "quality": 20, "enabled": True, "isSupport": False},
                    {"nameSpec": "Awakened Spell Echo", "level": 5, "quality": 20, "enabled": True, "isSupport": True},
                    {"nameSpec": "Enlighten", "level": 4, "quality": 0, "enabled": True, "isSupport": True},
                    {"nameSpec": "Arcane Surge", "level": 20, "quality": 0, "enabled": True, "isSupport": False},
                ],
            },
        ]
    }
    return items_res, skills_res


def _decode_b64gzip_url(url: str) -> dict[str, Any]:
    qid = url.rstrip("/").rsplit("/", 1)[-1]
    pad = "=" * (-len(qid) % 4)
    raw = gzip.decompress(base64.urlsafe_b64decode(qid + pad)).decode("utf-8")
    return json.loads(raw)


def _decode_q_param_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return json.loads(unquote(qs["q"][0]))


def test_build_shopping_list_covers_all_slot_classes_zero_http_and_fast() -> None:
    items_res, skills_res = _make_full_build_payloads()

    with (
        patch("httpx.Client", side_effect=AssertionError("L0 shopping-list must make 0 HTTP calls!")),
        patch(
            "pob_cli.trade_eval.shopping_extract.lookup_anoint_oils",
            return_value=["Amber Oil", "Verdant Oil", "Azure Oil"],
        ),
    ):
        t0 = time.perf_counter()
        res = build_shopping_list(items_res, skills_res, _SAMPLE_STATS, league="Mirage", status="securable")
        elapsed = time.perf_counter() - t0

    assert elapsed < perf_budget(0.5)
    assert res["httpRequests"] == 0
    assert res["status"] == "securable"
    assert res["anoint"] is not None
    assert res["anoint"]["notable"] == "Executioner"

    by_slot = {it["slot"]: it for it in res["items"] if it["section"] != "gem"}
    assert "Jewel #7" not in by_slot  # unallocated skipped

    # 1. Unique 6L Staff + discrete +5 spell gems
    w1 = by_slot["Weapon 1"]
    q_w1 = _decode_b64gzip_url(w1["url"])
    assert q_w1["name"] == "Legacy of the Rose"
    assert q_w1["filters"]["socket_filters"]["filters"]["links"] == {"min": 6}
    assert q_w1["status"] == {"option": "securable"}
    assert _decode_q_param_url(w1["queryParamUrl"])["query"] == q_w1

    # 2. Rare Body Armour with fractured + Shaper influence + complexity <= 160
    body = by_slot["Body Armour"]
    q_body = _decode_b64gzip_url(body["url"])
    assert q_body["filters"]["misc_filters"]["filters"]["fractured_item"] == {"option": "true"}
    assert q_body["filters"]["misc_filters"]["filters"]["shaper_item"] == {"option": "true"}
    assert body["complexity"] <= 160

    # 3. Foulborn Mageblood -> stripped name + foulborn_item=true
    belt = by_slot["Belt"]
    q_belt = _decode_b64gzip_url(belt["url"])
    assert q_belt["name"] == "Mageblood"
    assert q_belt["filters"]["misc_filters"]["filters"]["foulborn_item"] == {"option": "true"}

    # 4. Magic Utility Flask -> clean Quicksilver Flask base + 25% inc effect
    f1 = by_slot["Flask 1"]
    q_f1 = _decode_b64gzip_url(f1["url"])
    assert q_f1["type"] == "Quicksilver Flask"
    assert q_f1["filters"]["type_filters"]["filters"]["category"] == {"option": "flask"}

    # 5. Cluster Jewel -> passives 8..8 + option ID 3 + Adrenaline notable
    j1 = by_slot["Jewel #1"]
    q_j1 = _decode_b64gzip_url(j1["url"])
    j1_fids = {f["id"]: f["value"] for f in q_j1["stats"][0]["filters"]}
    assert j1_fids["enchant.stat_3086156145"] == {"min": 8, "max": 8}
    assert j1_fids["enchant.stat_3948993189"] == {"option": 3}
    assert "explicit.stat_4022743870" in j1_fids

    # 6. Watcher's Eye -> both aura mods preserved
    j2 = by_slot["Jewel #2"]
    q_j2 = _decode_b64gzip_url(j2["url"])
    assert q_j2["name"] == "Watcher's Eye"
    assert len(q_j2["stats"][0]["filters"]) == 2

    # 7. Forbidden Flame (matching modifier on Forbidden Flesh) -> explicit.stat_1190333629 option 47389
    j3 = by_slot["Jewel #3"]
    q_j3 = _decode_b64gzip_url(j3["url"])
    assert q_j3["stats"][0]["filters"][0] == {"id": "explicit.stat_1190333629", "value": {"option": 47389}, "disabled": False}

    # 8. Thread of Hope -> Very Large Ring option ID 4 + -12 all ele res
    j4 = by_slot["Jewel #4"]
    q_j4 = _decode_b64gzip_url(j4["url"])
    j4_fids = {f["id"]: f["value"] for f in q_j4["stats"][0]["filters"]}
    assert j4_fids["explicit.stat_3642528642"] == {"option": 4}
    assert j4_fids["explicit.stat_2901986750"] == {"min": -12}

    # 9. Impossible Escape -> Acrobatics option ID 54307
    j5 = by_slot["Jewel #5"]
    q_j5 = _decode_b64gzip_url(j5["url"])
    assert q_j5["stats"][0]["filters"][0] == {"id": "explicit.stat_2422708892", "value": {"option": 54307}, "disabled": False}

    # 10. Timeless Jewel -> Mazcotl mapped to explicit.pseudo_timeless_jewel_ahuana (seed 4512)
    j6 = by_slot["Jewel #6"]
    q_j6 = _decode_b64gzip_url(j6["url"])
    assert q_j6["stats"][0]["filters"][0] == {
        "id": "explicit.pseudo_timeless_jewel_ahuana",
        "value": {"min": 4512, "max": 4512},
        "disabled": False,
    }

    # 11. Key Gems -> 21/20 Transfigured, Awakened 5/20, Enlighten 4/0; vendor 20/0 skipped!
    gems = {it["name"]: it for it in res["items"] if it["section"] == "gem"}
    assert "Lightning Tendrils of Eccentricity" in gems
    assert "Awakened Spell Echo Support" in gems
    assert "Enlighten Support" in gems
    assert "Arcane Surge" not in gems
    q_gem = _decode_b64gzip_url(gems["Lightning Tendrils of Eccentricity"]["url"])
    assert q_gem["filters"]["misc_filters"]["filters"]["gem_level"] == {"min": 21}
    assert q_gem["filters"]["misc_filters"]["filters"]["quality"] == {"min": 20}


def test_cli_trade_shopping_list_markdown_and_json() -> None:
    items_res, skills_res = _make_full_build_payloads()
    runner = CliRunner()

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        return items_res if action == "items" else skills_res

    with (
        patch("pob_cli.cli_trade._daemon_call_safe", side_effect=_fake_daemon),
        patch("pob_cli.cli_trade.shopping_cmd.load_local_trade_stats", return_value=_SAMPLE_STATS),
        patch(
            "pob_cli.trade_eval.shopping_extract.lookup_anoint_oils",
            return_value=["Amber Oil", "Verdant Oil", "Azure Oil"],
        ),
    ):
        res_json = runner.invoke(trade_group, ["shopping-list", "--league", "Mirage", "--json", "--compact"])
        assert res_json.exit_code == 0, res_json.output
        payload = json.loads(res_json.output[res_json.output.index("{") :])
        assert payload["httpRequests"] == 0
        assert payload["totalItems"] == 15

        res_md = runner.invoke(trade_group, ["shopping-list", "--league", "Mirage"])
        assert res_md.exit_code == 0, res_md.output
        assert "PoE1 Build Shopping List" in res_md.output
        assert "Legacy of the Rose" in res_md.output
        assert "Executioner" in res_md.output


def test_real_engine_shopping_list_anoint_and_tw_realm() -> None:
    engine_root = find_engine_data()
    if engine_root is None:
        pytest.skip("No local PoB engine found")
    items_res, skills_res = _make_full_build_payloads()
    entries = load_local_trade_stats(pob_root=engine_root)
    assert entries
    res = build_shopping_list(
        items_res,
        skills_res,
        entries,
        league="Standard",
        realm="tw",
        pob_dir=engine_root,
    )
    assert res["anoint"]["oils"] == ["Amber Oil", "Verdant Oil", "Azure Oil"]
    assert res["realm"] == "tw"
    assert all(it["url"].startswith("https://pathofexile.tw/trade/search/") for it in res["items"])


def test_weapon_set_swap_slots_corrupted_watchers_eye_and_foulborn_mutated_tag() -> None:
    swap_items = {
        "buildInfo": {"className": "Duelist", "level": 95},
        "useSecondWeaponSet": True,
        "equippedSlots": [
            {
                "slotName": "Weapon 1",
                "weaponSet": 1,
                "raw": "Rarity: UNIQUE\nLegacy of the Rose\nJudgement Staff\nImplicits: 0\n",
            },
            {
                "slotName": "Weapon 1 Swap",
                "weaponSet": 2,
                "raw": "Rarity: UNIQUE\nVoidforge\nInfernal Sword\nSockets: R-R-R-R-R-R\nImplicits: 0\n",
            },
            {
                "slotName": "Weapon 1 Swap Abyssal Socket 1",
                "weaponSet": 2,
                "raw": "Rarity: RARE\nGhoul Eye\nMurderous Eye Jewel\nImplicits: 0\n+35 to maximum Life\n",
            },
            {
                "slotName": "Jewel 1",
                "nodeId": 12345,
                "socketIndex": 1,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 1\n"
                    "+14% to Chaos Resistance\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                    "Corrupted\n"
                ),
            },
            {
                "slotName": "Belt",
                "raw": (
                    "Rarity: UNIQUE\nFoulborn Mageblood\nHeavy Belt\nImplicits: 0\n"
                    "Leftmost 4 Magic Utility Flasks constantly apply their Flask Effects to you\n"
                    "{mutated}Rightmost 4 Magic Utility Flasks constantly apply their Flask Effects to you\n"
                ),
            },
        ],
    }
    # Force empty Foulborn map to verify `{mutated}` tag from `Mod.tags` still strips replaced mod
    with patch("pob_cli.trade_query.gem_filters.load_foulborn_map", return_value={}):
        res_swap = build_shopping_list(swap_items, None, _SAMPLE_STATS, league="Mirage")
    by_slot = {it["slot"]: it for it in res_swap["items"]}
    assert "Weapon 1" not in by_slot
    assert "Weapon 1 Swap" in by_slot and by_slot["Weapon 1 Swap"]["name"] == "Voidforge"
    assert "Weapon 1 Swap Abyssal Socket 1" in by_slot

    # Corrupted Watcher's Eye preserves `corrupted=true` AND its corrupted implicit filter
    q_we = _decode_b64gzip_url(by_slot["Jewel #1"]["url"])
    assert q_we["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "true"}
    we_ids = {f["id"] for f in q_we["stats"][0]["filters"]}
    assert "explicit.stat_1873457881" in we_ids
    assert "implicit.stat_2923486259" in we_ids

    # Foulborn belt stripped the replaced Leftmost line and kept only the mutated Rightmost line
    assert len(by_slot["Belt"]["notes"]) >= 1
    assert all("Leftmost" not in n for n in by_slot["Belt"]["notes"])

    # When useSecondWeaponSet=False, Weapon 1 is kept and Weapon 1 Swap + its Abyssal Socket are skipped
    swap_items["useSecondWeaponSet"] = False
    res_primary = build_shopping_list(swap_items, None, _SAMPLE_STATS, league="Mirage")
    primary_slots = {it["slot"] for it in res_primary["items"]}
    assert "Weapon 1" in primary_slots
    assert "Weapon 1 Swap" not in primary_slots
    assert "Weapon 1 Swap Abyssal Socket 1" not in primary_slots


def test_tw_unmapped_warnings_global_cjk_residue_and_non_english_raw_item() -> None:
    items_res = {
        "buildInfo": {"className": "Witch", "level": 90},
        "equippedSlots": [
            {
                "slotName": "Helmet",
                "raw": "稀有度: 稀有\n中文头盔\n瓦尔法衣\n",
            },
            {
                "slotName": "Ring 1",
                "raw": "Rarity: UNIQUE\n未知中文暗金\nRuby Ring\nImplicits: 0\n",
            },
            {
                "slotName": "Jewel 1",
                "nodeId": 26725,
                "socketIndex": 1,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                ),
            },
        ],
    }
    # Global realm with empty stat catalog: non-English raw skipped with warning, CJK name warned, Watcher's Eye flagged unresolved
    res_global = build_shopping_list(items_res, None, [], league="Mirage", realm="global")
    slots_global = {it["slot"]: it for it in res_global["items"]}
    assert "Helmet" not in slots_global
    assert any("Rarity:" in w for w in res_global.get("warnings") or ())
    assert any("CJK" in w for w in slots_global["Ring 1"].get("warnings") or ())
    assert slots_global["Jewel #1"].get("definingModsUnresolved") is True

    # TW realm with unmapped ASCII league emits league warning
    res_tw = build_shopping_list(items_res, None, _SAMPLE_STATS, league="FutureAsciiLeague", realm="tw")
    assert any("FutureAsciiLeague" in w for w in res_tw.get("warnings") or ())

    # Corrupted Forbidden Flame with unresolved allocation mod still flags definingModsUnresolved
    # even when its corrupted implicit resolves, and missing implicit domain never leaks to explicit.*
    corr_items = {
        "buildInfo": {"className": "Witch", "level": 90},
        "equippedSlots": [
            {
                "slotName": "Jewel 1",
                "nodeId": 2491,
                "socketIndex": 1,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nForbidden Flame\nCrimson Jewel\nImplicits: 1\n"
                    "+14% to Chaos Resistance\n"
                    "Allocates Unlisted Notable if you have the matching modifier on Forbidden Flesh\n"
                    "Corrupted\n"
                ),
            },
            {
                "slotName": "Jewel 2",
                "nodeId": 26725,
                "socketIndex": 2,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 1\n"
                    "+12% to Fire Resistance\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                    "Corrupted\n"
                ),
            },
        ],
    }
    res_corr = build_shopping_list(corr_items, None, _SAMPLE_STATS, league="Mirage", realm="global")
    by_slot_corr = {it["slot"]: it for it in res_corr["items"]}
    assert by_slot_corr["Jewel #1"].get("definingModsUnresolved") is True
    j1_q = _decode_b64gzip_url(by_slot_corr["Jewel #1"]["url"])
    assert j1_q["stats"][0]["filters"] == [{"id": "implicit.stat_2923486259", "value": {"min": 12}, "disabled": False}]
    # Fire Resistance exists ONLY as explicit.stat_3372524247 in _SAMPLE_STATS -> must NOT be emitted as an implicit filter!
    j2_q = _decode_b64gzip_url(by_slot_corr["Jewel #2"]["url"])
    j2_fids = [f["id"] for f in j2_q["stats"][0]["filters"]]
    assert j2_fids == ["explicit.stat_1873457881"]
