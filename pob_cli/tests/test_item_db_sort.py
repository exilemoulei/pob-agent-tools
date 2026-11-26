"""Unit tests for `mutators/item_db.py` (stateless reset, targetSlot, guards, compaction, pagination)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pob_cli.bridge_protocol import BridgeRejectedError
from pob_cli.mutators import (
    DB_STAT_SORT_MAX,
    ITEM_DB_TEMPLATE_NOTE,
    list_item_db_sorts,
    search_and_try_item_db,
)

_UNIQUE_OPTS: dict[str, Any] = {
    "kind": "unique",
    "slot": [
        {"label": lbl, "labelZh": zh}
        for lbl, zh in [
            ("Any slot", "任意槽位"), ("Weapon 1", "主手"), ("Weapon 2", "副手"), ("Helmet", "头盔"),
            ("Body Armour", "胸甲"), ("Gloves", "手套"), ("Boots", "鞋子"), ("Amulet", "项链"),
            ("Ring", "戒指"), ("Belt", "腰带"), ("Jewel", "珠宝"), ("Flask", "药剂"),
        ]
    ],
    "type": [{"label": "Any type", "labelZh": "任意类型"}, {"label": "Armour", "labelZh": "护甲"}, {"label": "Bow", "labelZh": "弓箭"}],
    "league": [{"label": "Any league", "labelZh": "任意赛季"}, {"label": "No league", "labelZh": "无赛季"}],
    "requirement": [{"label": "Any requirements", "labelZh": "任意需求"}, {"label": "Current useable", "labelZh": "当前可用"}],
    "obtainable": [{"label": "Obtainable", "labelZh": "可获取"}, {"label": "Any source", "labelZh": "任意来源"}],
    "searchMode": [{"label": "Anywhere", "labelZh": "任意位置"}, {"label": "Names", "labelZh": "名称"}],
    "sort": [
        {"label": "Sort by Name", "labelZh": "按名称排序", "sortMode": "NAME", "stat": None},
        {"label": "Sort by Full DPS", "labelZh": "按完整 DPS", "sortMode": "FullDPS", "stat": "FullDPS"},
        {"label": "Sort by Effective Hit Pool", "labelZh": "按有效血池", "sortMode": "TotalEHP", "stat": "TotalEHP"},
    ],
}


class _StatefulItemDbWorker:
    """Simulates PoB's singleton `ItemDBControl` state persistence across RPC calls."""

    def __init__(
        self,
        *,
        stats: dict[str, Any] | None = None,
        support_options: bool = True,
        too_many_slots: set[int] | None = None,
        extra_items: list[dict[str, Any]] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.pob_root = None
        self.caps: dict[str, Any] = {}
        self.last_good_code = "SNAP_INIT"
        self.support_options = support_options
        self.too_many_slots = too_many_slots or set()
        self.stats = dict(stats or {"FullDPS": 0, "CombinedDPS": 1_000_000.0, "TotalEHP": 50_000.0})
        self.ctl_state: dict[str, Any] = {"slot": 1, "type": 1, "league": 1, "requirement": 1, "obtainable": 1, "searchMode": 1, "sortMode": "Name"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.equipped_slots: list[dict[str, Any]] = []
        self.items_db: list[dict[str, Any]] = [
            {"title": "Alpha's Howl", "titleZh": "极地之眼", "baseName": "Sinner Tricorne", "primarySlot": "Helmet", "slotIdx": 4, "obtainable": True, "measuredPower": 1_250_000.0, "raw": "Rarity: UNIQUE\nAlpha's Howl\nSinner Tricorne\n"},
            {"title": "Crown of Eyes", "titleZh": "邪眼之冠", "baseName": "Hubris Circlet", "primarySlot": "Helmet", "slotIdx": 4, "obtainable": True, "measuredPower": 1_400_000.0, "raw": "Rarity: UNIQUE\nCrown of Eyes\nHubris Circlet\n"},
            {"title": "Mageblood", "titleZh": "法师之血", "baseName": "Heavy Belt", "primarySlot": "Belt", "slotIdx": 10, "obtainable": True, "measuredPower": 1_800_000.0, "raw": "Rarity: UNIQUE\nMageblood\nHeavy Belt\n"},
            {"title": "Legacy Kaom", "baseName": "Glorious Plate", "primarySlot": "Body Armour", "slotIdx": 5, "obtainable": False, "measuredPower": 900_000.0, "raw": "Rarity: UNIQUE\nLegacy Kaom\nGlorious Plate\n"},
            {"title": "Nimis", "titleZh": "尼米斯", "baseName": "Topaz Ring", "primarySlot": "Ring 1", "slotIdx": 9, "obtainable": True, "measuredPower": 1_650_000.0, "raw": "Rarity: UNIQUE\nNimis\nTopaz Ring\n"},
            {"title": "Watcher's Eye", "baseName": "Prismatic Jewel", "primarySlot": "Jewel", "slotIdx": 11, "obtainable": True, "measuredPower": 1_500_000.0, "raw": "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\n"},
            {"title": "Bottled Faith", "baseName": "Sulphur Flask", "primarySlot": "Flask 1", "slotIdx": 12, "obtainable": True, "measuredPower": 1_300_000.0, "raw": "Rarity: UNIQUE\nBottled Faith\nSulphur Flask\n"},
            *(extra_items or []),
        ]

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "item_db_options":
            if not self.support_options:
                raise BridgeRejectedError("RPC 'item_db_options' failed: unknown method", code="bad_request")
            return _UNIQUE_OPTS if p.get("kind", "unique") == "unique" else {"kind": "rare", "slot": _UNIQUE_OPTS["slot"], "type": _UNIQUE_OPTS["type"], "searchMode": _UNIQUE_OPTS["searchMode"]}
        if method == "get_stats":
            return {"stats": dict(self.stats)}
        if method == "list_items":
            return {
                "slots": [
                    {"name": "Helmet", "label": "Helmet", "selItemId": 0, "shown": True},
                    {"name": "Ring 1", "label": "Ring 1", "selItemId": 1, "shown": True},
                    {"name": "Ring 2", "label": "Ring 2", "selItemId": 0, "shown": True},
                    {"name": "Belt", "label": "Belt", "selItemId": 0, "shown": True},
                    {"name": "Flask 1", "label": "Flask 1", "selItemId": 0, "shown": True},
                    {"name": "Jewel 10001", "label": "Jewel #1", "nodeId": 10001, "socketIndex": 1, "selItemId": 5, "shown": True},
                    {"name": "Jewel 26725", "label": "Jewel #2", "nodeId": 26725, "socketIndex": 2, "selItemId": 0, "shown": True},
                ]
            }
        if method == "get_tree_state":
            return {"allocatedNodes": [10001, 26725]}
        if method == "item_tooltip":
            return {"lines": [{"text": "Mock Tooltip"}]}
        if method == "export_code":
            return {"code": self.last_good_code}
        if method == "import_code":
            return {"ok": True}
        if method == "get_build_info":
            return {"outputRevision": 1, "level": 90}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "add_item":
            self.equipped_slots.append(p)
            return {"item": {"id": 99, "primarySlot": p.get("slotName") or "Ring 1"}}
        if method == "item_db":
            for k in ("slot", "type", "league", "requirement", "obtainable", "searchMode", "sortMode"):
                if k in p and p[k] is not None:
                    self.ctl_state[k] = p[k]
            slot_idx, obt_idx = int(self.ctl_state["slot"]), int(self.ctl_state["obtainable"])
            sort_mode = str(self.ctl_state["sortMode"])
            stat_sort = sort_mode.upper() != "NAME"
            if stat_sort and slot_idx in self.too_many_slots:
                return {"kind": "unique", "total": 328, "tooMany": True, "max": DB_STAT_SORT_MAX, "items": []}
            q = str(p.get("query") or "").strip().lower()
            hits = []
            for it in self.items_db:
                if slot_idx > 1 and it["slotIdx"] != slot_idx:
                    continue
                if obt_idx == 1 and not it["obtainable"]:
                    continue
                if q and q not in it["title"].lower() and q not in str(it.get("titleZh") or "").lower():
                    continue
                row = {k: v for k, v in it.items() if k not in ("slotIdx", "obtainable")}
                if not stat_sort:
                    row.pop("measuredPower", None)
                hits.append(row)
            hits.sort(key=(lambda x: float(x.get("measuredPower") or 0.0)) if stat_sort else (lambda x: str(x["title"])), reverse=stat_sort)
            page = max(1, int(p.get("page") or 1))
            size = min(200, max(1, int(p.get("size") or 50)))
            start = (page - 1) * size
            return {"kind": p.get("kind", "unique"), "total": len(hits), "page": page, "size": size, "statSort": stat_sort, "items": hits[start : start + size]}
        return {"ok": True}


def test_stateless_reset_and_descending_power_order() -> None:
    w = _StatefulItemDbWorker()
    r1 = search_and_try_item_db(w, slot_name="Helmet", sort_mode="dps", limit=10)
    assert r1["statSort"] is True and r1["sortMode"] == "FullDPS"
    assert [m["title"] for m in r1["matches"]] == ["Crown of Eyes", "Alpha's Howl"]
    assert r1["baselineValue"] == pytest.approx(1_000_000.0) and r1["templateNote"] == ITEM_DB_TEMPLATE_NOTE
    assert r1["matches"][0]["delta"] == pytest.approx(400_000.0) and r1["matches"][0]["deltaPct"] == pytest.approx(40.0)

    r2 = search_and_try_item_db(w, query="Mageblood", limit=5)
    assert r2["statSort"] is False and r2["sortMode"] == "Name"
    assert [m["title"] for m in r2["matches"]] == ["Mageblood"]
    assert (w.ctl_state["slot"], w.ctl_state["sortMode"], w.ctl_state["obtainable"]) == (1, "Name", 1)


def test_target_slot_passthrough_for_ring2_jewel_and_flask() -> None:
    w = _StatefulItemDbWorker(stats={"FullDPS": 1_000_000.0})
    r_ring = search_and_try_item_db(w, slot_name="Ring 2", sort_mode="dps", equip=True, item_index=1)
    assert r_ring["filters"]["slot"] == "Ring" and r_ring["filters"]["targetSlot"] == "Ring 2"
    assert [p for m, p in w.calls if m == "item_db"][-1]["targetSlot"] == "Ring 2"
    assert r_ring["slotName"] == "Ring 2" and w.equipped_slots[-1]["slotName"] == "Ring 2"
    assert r_ring["selectedItem"]["measuredPower"] == pytest.approx(1_650_000.0)
    assert r_ring["selectedItem"]["delta"] == pytest.approx(650_000.0)
    assert r_ring["selectedItem"]["deltaPct"] == pytest.approx(65.0)

    # Generic Jewel selects first empty allocated jewel socket (Jewel 26725) + slotNote.
    r_jwl = search_and_try_item_db(w, slot_name="Jewel", sort_mode="dps", limit=5)
    assert r_jwl["filters"]["targetSlot"] == "Jewel 26725"
    assert [p for m, p in w.calls if m == "item_db"][-1]["targetSlot"] == "Jewel 26725"
    assert "Jewel 26725" in str(r_jwl.get("slotNote") or "")

    # Generic Flask selects Flask 1 + slotNote.
    r_flk = search_and_try_item_db(w, slot_name="Flask", sort_mode="dps", limit=5)
    assert r_flk["filters"]["targetSlot"] == "Flask 1"
    assert [p for m, p in w.calls if m == "item_db"][-1]["targetSlot"] == "Flask 1"
    assert "toggleFlask" in str(r_flk.get("slotNote") or "")

    # Equipping with generic Jewel equips into the exact targetSlot evaluated; generic Ring/Flask let add_item auto-pick.
    r_jwl_eq = search_and_try_item_db(w, slot_name="Jewel", sort_mode="dps", equip=True)
    assert r_jwl_eq["slotName"] == "Jewel 26725" and w.equipped_slots[-1]["slotName"] == "Jewel 26725"
    r_ring_gen = search_and_try_item_db(w, query="Nimis", slot_name="Ring", equip=True)
    assert r_ring_gen["equipped"] is True and "slotName" not in w.equipped_slots[-1]
    r_flk_eq = search_and_try_item_db(w, slot_name="Flask", sort_mode="dps", equip=True)
    assert r_flk_eq["equipped"] is True and "slotName" not in w.equipped_slots[-1]


def test_stat_sort_guard_and_zero_hit_hint_in_both_modes() -> None:
    w = _StatefulItemDbWorker(too_many_slots={2})
    with pytest.raises(ValueError, match="300"):
        search_and_try_item_db(w, sort_mode="dps")
    with pytest.raises(ValueError, match="328 > 300"):
        search_and_try_item_db(w, slot_name="Weapon 1", obtainable="any", sort_mode="dps")

    r0_name = search_and_try_item_db(w, query="Legacy Kaom")
    assert r0_name["total"] == 0 and "--obtainable any" in str(r0_name.get("hint") or "")
    r0_stat = search_and_try_item_db(w, query="Legacy Kaom", sort_mode="dps")
    assert r0_stat["total"] == 0 and "--obtainable any" in str(r0_stat.get("hint") or "")
    r_any = search_and_try_item_db(w, query="Legacy Kaom", obtainable="any")
    assert r_any["total"] == 1 and "hint" not in r_any


def test_equip_index_above_50_pagination_and_oversized_variant_compaction() -> None:
    huge_raw = "\n".join(
        [
            "Rarity: UNIQUE", "Pearl of Tsoatha", "Prismatic Ring", "Has Alt Variant: true",
            "Selected Variant: 1", "Selected Alt Variant: 300", "Implicits: 1",
            *[f"Variant: Boots / Gem {i:03d}" for i in range(1, 551)],
            "+10% to all Elemental Resistances",
            *[f"{{variant:{i}}}Skills Socketed in your Boots are Supported by level 20 Support Number {i:03d} with padding text" for i in range(1, 551)],
        ]
    ) + "\n"
    extra = [
        {"title": "Pearl of Tsoatha", "baseName": "Prismatic Ring", "primarySlot": "Ring 1", "slotIdx": 9, "obtainable": True, "measuredPower": 1_100_000.0, "raw": huge_raw},
        *[
            {"title": f"Helm {i:03d}", "baseName": "Hubris Circlet", "primarySlot": "Helmet", "slotIdx": 4, "obtainable": True, "measuredPower": float(1_000_000 - i * 1000), "raw": f"Rarity: UNIQUE\nHelm {i:03d}\nHubris Circlet\n"}
            for i in range(1, 70)
        ],
    ]
    w = _StatefulItemDbWorker(stats={"FullDPS": 1_000_000.0}, extra_items=extra)
    res_eq = search_and_try_item_db(w, slot_name="Helmet", sort_mode="dps", item_index=60, equip=True)
    assert res_eq["selectedItem"]["index"] == 60 and res_eq["selectedItem"]["delta"] is not None
    assert [p for m, p in w.calls if m == "item_db"][-1]["size"] >= 60

    res_p2 = search_and_try_item_db(w, slot_name="Helmet", sort_mode="dps", limit=10, page=2)
    assert res_p2["page"] == 2 and len(res_p2["matches"]) == 10

    # Oversized >64KB Pearl of Tsoatha raw compacted before add_item.
    search_and_try_item_db(w, query="Pearl of Tsoatha", slot_name="Ring 2", equip=True)
    assert len(w.equipped_slots[-1]["raw"].encode("utf-8")) < 5_000


def test_legacy_worker_blocks_filters_but_allows_plain_query_and_list_sorts() -> None:
    w_legacy = _StatefulItemDbWorker(support_options=False)
    res = search_and_try_item_db(w_legacy, query="Mageblood")
    assert res["total"] == 1 and res["matches"][0]["title"] == "Mageblood"
    with pytest.raises(ValueError, match="item_db_options"):
        search_and_try_item_db(w_legacy, slot_name="Helmet")
    with pytest.raises(ValueError, match="item_db_options"):
        list_item_db_sorts(w_legacy)
    sorts_res = list_item_db_sorts(_StatefulItemDbWorker(support_options=True))
    assert sorts_res["listSorts"] is True and any(s["sortMode"] == "FullDPS" for s in sorts_res["sorts"])
