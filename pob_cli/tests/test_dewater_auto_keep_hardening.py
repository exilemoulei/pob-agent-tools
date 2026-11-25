"""Auto-keep must not credit charges a build is not guaranteed: real localized node names, minimum below maximum."""

from __future__ import annotations

from typing import Any

import pytest

from pob_cli.analyzer.dewater_auto import infer_auto_keep_and_flask_notes

CWS_GROUP = {
    "index": 1,
    "enabled": True,
    "gems": [{"name": "Cast when Stunned Support", "skillId": "SupportCastWhenStunned", "enabled": True}],
}


def _auto_keep(
    *,
    stats: dict[str, Any] | None = None,
    notables: list[dict[str, Any]] | None = None,
    cws: bool = False,
    item: dict[str, Any] | None = None,
) -> dict[str, str]:
    kept, _notes = infer_auto_keep_and_flask_notes(
        stats=stats or {},
        skills_res={"groups": [CWS_GROUP] if cws else []},
        items_res={
            "items": [{"id": 1, **item}] if item else [],
            "slots": [{"name": "Body Armour", "selItemId": 1}] if item else [],
        },
        tree_summary={"notables": notables or [], "keystones": [], "ascendancyNodes": []},
    )
    return kept


# Names as PoB's zh-rCN and zh-rTW passives.json give them.
@pytest.mark.parametrize(
    ("name", "name_zh", "cws"),
    [
        ("Unstoppable", "势不可挡", False),  # zh-CN shares this name with Inexorable
        ("Unstoppable", "勢不可擋", False),
        ("Inexorable", "势不可挡", False),  # Inexorable alone needs a hit-tank setup
        ("Unbending", "不屈之心", True),
        ("Indomitable Resolve", "不屈坚毅", True),
        ("Disciple of the Unyielding", "不屈门徒", True),
        ("Defensive Arts", "坚定不移", True),
        ("Tenacity", "不屈不撓", True),
    ],
)
def test_passives_that_share_a_chinese_name_do_not_keep_endurance_charges(name: str, name_zh: str, cws: bool) -> None:
    kept = _auto_keep(notables=[{"id": 1, "name": name, "nameZh": name_zh}], cws=cws)
    assert "useendurancecharges" not in kept


@pytest.mark.parametrize(
    ("name", "name_zh", "cws"),
    [("Unflinching", "永不动摇", False), ("Unflinching", "永不動搖", False), ("Inexorable", "势不可挡", True), ("Inexorable", "無可避免", True)],
)
def test_the_real_endurance_sustain_passives_still_keep_them(name: str, name_zh: str, cws: bool) -> None:
    kept = _auto_keep(notables=[{"id": 1, "name": name, "nameZh": name_zh}], cws=cws)
    assert "useendurancecharges" in kept


@pytest.mark.parametrize("kind", ["Endurance", "Frenzy", "Power"])
def test_minimum_charges_keep_the_setting_only_when_they_equal_the_maximum(kind: str) -> None:
    key = f"use{kind.lower()}charges"
    # PoB counts the minimum by itself; the setting would count all 4.
    assert key not in _auto_keep(stats={f"{kind}ChargesMin": 1.0, f"{kind}ChargesMax": 4.0})
    assert key not in _auto_keep(stats={f"{kind}ChargesMin": 1.0})
    assert key in _auto_keep(stats={f"{kind}ChargesMin": 4.0, f"{kind}ChargesMax": 4.0})


INEXORABLE = [{"id": 1, "name": "Inexorable", "nameZh": "势不可挡"}]


def test_a_sanguine_raiment_is_not_bloodnotch() -> None:
    # zh-CN: Bloodnotch is "血痕", the body armour base Sanguine Raiment is "血痕之衣".
    rare = {"title": "Dread Shell", "name": "Dread Shell, Sanguine Raiment", "baseName": "Sanguine Raiment", "nameZh": "血痕之衣"}
    assert "useendurancecharges" not in _auto_keep(notables=INEXORABLE, item=rare)

    for name_zh in ("血痕", "缺血症"):
        jewel = {"title": "Bloodnotch", "name": "Bloodnotch, Crimson Jewel", "baseName": "Crimson Jewel", "titleZh": name_zh}
        assert "useendurancecharges" in _auto_keep(notables=INEXORABLE, item=jewel)


@pytest.mark.parametrize("label", ["Tattoo of the Tukohama Brawler", "图克哈玛斗士的文身", "圖克哈瑪鬥士的紋身"])
def test_a_tukohama_tattoo_is_not_the_ascendancy_node(label: str) -> None:
    kept, _notes = infer_auto_keep_and_flask_notes(
        stats={}, skills_res={"groups": []}, items_res={"items": [], "slots": []}, alloc_node_stats=[(label, [])]
    )
    assert "conditionstationary" not in kept


def test_the_tukohama_ascendancy_node_keeps_the_stationary_setting() -> None:
    node = {"id": 2, "name": "Tukohama, War's Herald", "nameZh": "战争使者.图克哈玛"}
    assert "conditionstationary" in _auto_keep(notables=[node])
