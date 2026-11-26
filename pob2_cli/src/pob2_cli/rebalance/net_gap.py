# SPDX-License-Identifier: GPL-3.0-only
"""Step 0 bare net-gap analyzer for PoE2 5-stage zero-buy rebalance solver (L2).

Obeys the core PoE2 resistance contract:
  - Hard Floor: each elemental resistance >= its own MaxRes (`Res >= MaxRes`), Chaos >= target,
    and Str/Dex/Int >= ReqStr/ReqDex/ReqInt across BOTH Weapon Set 1 and Weapon Set 2.
  - Overcap Buffer: soft bonus goal (`+overcap_target`, default +20%) against curses/exposure/minus-res maps.
  - NEVER equalizes Fire/Cold/Lightning or penalizes healthy overcap surplus.
  - NEVER counts hit-triggered `Charm 1..3` modifiers as permanent resistance or overcap buffer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..craft.item_inspector import inspect_item_text
from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from ..rune.socket_rules import (
    SocketParityResult,
    resolve_item_augment_types,
    strip_replaceable_runes_from_item_raw,
)
from ..snapshot import BuildScope, BuildTransaction

_CHARM_SLOTS: frozenset[str] = frozenset({"Charm 1", "Charm 2", "Charm 3"})
_FLASK_SLOTS: frozenset[str] = frozenset({"Flask 1", "Flask 2"})
_JEWELLERY_BELT_SLOTS: frozenset[str] = frozenset({"Amulet", "Ring 1", "Ring 2", "Ring 3", "Belt"})
_GIANTS_BLOOD_RE = re.compile(r"giant'?s\s+blood", re.IGNORECASE)
_RADIUS_JEWEL_RE = re.compile(
    r"(?:from nothing|controlled metamorphosis|against the darkness|time-lost|undying hate|heroic tragedy|radius:)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class StatSnapshot:
    """Character attribute, resistance, and Spirit state across WS1 and WS2."""

    level: int
    ascendancy: str
    str_val: float
    req_str: float
    dex_val: float
    req_dex: float
    int_val: float
    req_int: float
    fire_total: float
    fire_max: float
    cold_total: float
    cold_max: float
    lightning_total: float
    lightning_max: float
    chaos_total: float
    chaos_max: float
    spirit_total: float
    spirit_reserved: float
    spirit_unreserved: float
    combined_dps: float = 0.0
    total_ehp: float = 0.0
    giants_blood: bool = False
    ws2_req_str: float = 0.0
    ws2_req_dex: float = 0.0
    ws2_req_int: float = 0.0
    ws2_str_val: float | None = None
    ws2_dex_val: float | None = None
    ws2_int_val: float | None = None

    def effective_attr_deficits(self) -> dict[str, int]:
        """Return worst-case Str/Dex/Int hard deficit across BOTH WS1 and WS2."""
        d_str = max(0, round(self.req_str - self.str_val))
        d_dex = max(0, round(self.req_dex - self.dex_val))
        d_int = max(0, round(self.req_int - self.int_val))
        if self.ws2_str_val is not None:
            d_str = max(d_str, round(self.ws2_req_str - self.ws2_str_val))
        if self.ws2_dex_val is not None:
            d_dex = max(d_dex, round(self.ws2_req_dex - self.ws2_dex_val))
        if self.ws2_int_val is not None:
            d_int = max(d_int, round(self.ws2_req_int - self.ws2_int_val))
        return {"str": d_str, "dex": d_dex, "int": d_int}

    def effective_attr_surpluses(self) -> dict[str, int]:
        """Return safe Str/Dex/Int surplus above requirements across BOTH WS1 and WS2."""
        s_str = round(self.str_val - self.req_str)
        s_dex = round(self.dex_val - self.req_dex)
        s_int = round(self.int_val - self.req_int)
        if self.ws2_str_val is not None:
            s_str = min(s_str, round(self.ws2_str_val - self.ws2_req_str))
        if self.ws2_dex_val is not None:
            s_dex = min(s_dex, round(self.ws2_dex_val - self.ws2_req_dex))
        if self.ws2_int_val is not None:
            s_int = min(s_int, round(self.ws2_int_val - self.ws2_req_int))
        return {"str": max(0, s_str), "dex": max(0, s_dex), "int": max(0, s_int)}

    def resistance_hard_deficits(self, *, chaos_target: float = 0.0) -> dict[str, int]:
        """Hard Floor deficits: each element MUST meet its own MaxRes (`Res >= MaxRes`)."""
        return {
            "fire": max(0, round(self.fire_max - self.fire_total)),
            "cold": max(0, round(self.cold_max - self.cold_total)),
            "lightning": max(0, round(self.lightning_max - self.lightning_total)),
            "chaos": max(0, round(chaos_target - self.chaos_total)),
        }

    def resistance_overcap_needs(self, *, overcap_target: float = 20.0) -> dict[str, int]:
        """Soft Overcap Buffer gaps (never penalizes elements already exceeding `MaxRes + overcap`)."""
        if overcap_target <= 0:
            return {"fire": 0, "cold": 0, "lightning": 0}
        return {
            "fire": max(0, round((self.fire_max + overcap_target) - self.fire_total)),
            "cold": max(0, round((self.cold_max + overcap_target) - self.cold_total)),
            "lightning": max(0, round((self.lightning_max + overcap_target) - self.lightning_total)),
        }

    def spirit_deficit(self) -> int:
        """True Spirit deficit (`>0` when reserved exceeds total Spirit)."""
        unres = min(self.spirit_unreserved, self.spirit_total - self.spirit_reserved)
        return max(0, round(-unres))

    def to_dict(self, *, chaos_target: float = 0.0, overcap_target: float = 20.0) -> dict[str, Any]:
        return {
            "level": self.level,
            "ascendancy": self.ascendancy,
            "attributes": {
                "str": round(self.str_val, 1), "reqStr": round(self.req_str, 1),
                "dex": round(self.dex_val, 1), "reqDex": round(self.req_dex, 1),
                "int": round(self.int_val, 1), "reqInt": round(self.req_int, 1),
                "hardDeficits": self.effective_attr_deficits(),
                "giantsBlood": self.giants_blood,
            },
            "resistances": {
                "fire": round(self.fire_total, 1), "fireMax": round(self.fire_max, 1),
                "fireOvercap": round(self.fire_total - self.fire_max, 1),
                "cold": round(self.cold_total, 1), "coldMax": round(self.cold_max, 1),
                "coldOvercap": round(self.cold_total - self.cold_max, 1),
                "lightning": round(self.lightning_total, 1), "lightningMax": round(self.lightning_max, 1),
                "lightningOvercap": round(self.lightning_total - self.lightning_max, 1),
                "chaos": round(self.chaos_total, 1), "chaosMax": round(self.chaos_max, 1),
                "hardDeficits": self.resistance_hard_deficits(chaos_target=chaos_target),
                "overcapNeeds": self.resistance_overcap_needs(overcap_target=overcap_target),
            },
            "spirit": {
                "total": round(self.spirit_total, 1),
                "reserved": round(self.spirit_reserved, 1),
                "unreserved": round(self.spirit_unreserved, 1),
                "deficit": self.spirit_deficit(),
            },
            "combinedDps": round(self.combined_dps, 2),
            "totalEhp": round(self.total_ehp, 2),
        }


@dataclass(frozen=True)
class EquippedSlotAudit:
    """Single equipped slot's socket editability, Crafted slot status, and catalyst eligibility."""

    slot_name: str
    item_id: int
    name: str
    base_name: str
    rarity: str
    item_type: str
    raw_text: str
    stripped_raw_text: str
    parity: SocketParityResult
    editable_socket_count: int
    stripped_runes: tuple[str, ...]
    preserved_bound_runes: tuple[str, ...]
    has_open_crafted_slot: bool
    open_prefix_count: int
    open_suffix_count: int
    catalyst_eligible: bool
    is_weapon_slot: bool


@dataclass(frozen=True)
class NetGapAudit:
    """Complete Step 0 bare-build net gap analysis before Stages 1-5 run.

    Snapshot semantics (never strips permanent campaign quest rewards or equipped gear):
      - `current_in_game`: live in-game stats as loaded (including active hit-triggered Charms).
      - `current` (`withoutConditionalAndCharm`): permanent stats with Charms deactivated, keeping all
        equipped gear, socketed runes, passive tree, and permanent campaign quest rewards.
      - `bare_net`: permanent stats with Charms deactivated AND replaceable socketed runes temporarily
        stripped (keeping all equipped gear, permanent quest rewards, Mirrored items, and isSocketBound
        augments) to measure exact free socket budget — NOT unequipped resistances.
    """

    current: StatSnapshot
    bare_net: StatSnapshot
    slots: tuple[EquippedSlotAudit, ...]
    protected_radius_jewel_nodes: tuple[int, ...] = ()
    charm_resists_excluded: bool = True
    warnings: tuple[str, ...] = ()
    crafted_slot_hints: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    current_in_game: StatSnapshot | None = None


def _resolve_res_max(stats: dict[str, Any], elem: str, capped_res: float, overcap: float) -> float:
    """Compute real PoE2 MaxRes from PoB2 stats (which omit `*ResistMax` and expose `Missing*Resist` / `*ResistOver75`)."""
    explicit = stats.get(f"{elem}ResistMax")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    missing = stats.get(f"Missing{elem}Resist")
    if isinstance(missing, (int, float)) and isinstance(stats.get(f"{elem}Resist"), (int, float)):
        return float(capped_res) + float(missing)
    if isinstance(missing, (int, float)) and float(missing) > 0.0:
        return float(capped_res) + float(missing)
    if overcap > 0.0 and isinstance(stats.get(f"{elem}Resist"), (int, float)):
        return float(capped_res)
    over75 = stats.get(f"{elem}ResistOver75")
    over75_f = float(over75) if isinstance(over75, (int, float)) else 0.0
    return 75.0 + over75_f


def stat_snapshot_from_pob_stats(
    stats: dict[str, Any],
    *,
    level: int = 90,
    ascendancy: str = "",
    giants_blood: bool = False,
    ws2_stats: dict[str, Any] | None = None,
) -> StatSnapshot:
    """Build a `StatSnapshot` from PoB2 `get_stats().stats` dictionary."""
    def _f(d: dict[str, Any], key: str, default: float = 0.0) -> float:
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) else default

    fire_res = _f(stats, "FireResist")
    fire_over = _f(stats, "FireResistOverCap")
    fire_max = _resolve_res_max(stats, "Fire", fire_res, fire_over)
    fire_tot = _f(stats, "FireResistTotal", fire_res + fire_over)
    cold_res = _f(stats, "ColdResist")
    cold_over = _f(stats, "ColdResistOverCap")
    cold_max = _resolve_res_max(stats, "Cold", cold_res, cold_over)
    cold_tot = _f(stats, "ColdResistTotal", cold_res + cold_over)
    light_res = _f(stats, "LightningResist")
    light_over = _f(stats, "LightningResistOverCap")
    light_max = _resolve_res_max(stats, "Lightning", light_res, light_over)
    light_tot = _f(stats, "LightningResistTotal", light_res + light_over)
    chaos_res = _f(stats, "ChaosResist")
    chaos_over = _f(stats, "ChaosResistOverCap")
    chaos_max = _resolve_res_max(stats, "Chaos", chaos_res, chaos_over)
    chaos_tot = _f(stats, "ChaosResistTotal", chaos_res + chaos_over)

    spirit_total = _f(stats, "Spirit")
    spirit_unres = _f(stats, "SpiritUnreserved", spirit_total)
    spirit_res = _f(stats, "SpiritReserved", max(0.0, spirit_total - spirit_unres))
    # Repair #2401/clamping: if SpiritReserved > Spirit, expose true negative unreserved Spirit.
    true_unres = min(spirit_unres, spirit_total - spirit_res)

    return StatSnapshot(
        level=level,
        ascendancy=ascendancy,
        str_val=_f(stats, "Str"),
        req_str=_f(stats, "ReqStr"),
        dex_val=_f(stats, "Dex"),
        req_dex=_f(stats, "ReqDex"),
        int_val=_f(stats, "Int"),
        req_int=_f(stats, "ReqInt"),
        fire_total=fire_tot,
        fire_max=fire_max,
        cold_total=cold_tot,
        cold_max=cold_max,
        lightning_total=light_tot,
        lightning_max=light_max,
        chaos_total=chaos_tot,
        chaos_max=chaos_max,
        spirit_total=spirit_total,
        spirit_reserved=spirit_res,
        spirit_unreserved=true_unres,
        combined_dps=_f(stats, "CombinedDPS") or _f(stats, "TotalDPS"),
        total_ehp=_f(stats, "TotalEHP"),
        giants_blood=giants_blood,
        ws2_req_str=_f(ws2_stats, "ReqStr") if ws2_stats else 0.0,
        ws2_req_dex=_f(ws2_stats, "ReqDex") if ws2_stats else 0.0,
        ws2_req_int=_f(ws2_stats, "ReqInt") if ws2_stats else 0.0,
        ws2_str_val=_f(ws2_stats, "Str") if ws2_stats else None,
        ws2_dex_val=_f(ws2_stats, "Dex") if ws2_stats else None,
        ws2_int_val=_f(ws2_stats, "Int") if ws2_stats else None,
    )


def audit_equipped_slot(
    *,
    slot_name: str,
    item_id: int,
    name: str,
    base_name: str,
    rarity: str,
    item_type: str,
    raw_text: str,
    is_shaman: bool = False,
    catalog: RuneInstillCatalog | None = None,
) -> EquippedSlotAudit:
    """Audit a single equipped slot for editable rune sockets, Crafted slot availability, and catalysts."""
    cat = catalog or load_rune_instill_catalog()
    parity = resolve_item_augment_types(item_type, raw_text=raw_text, base_name=base_name, catalog=cat)
    stripped_raw, stripped, kept = strip_replaceable_runes_from_item_raw(
        raw_text, item_type=item_type, base_name=base_name, is_shaman=is_shaman, catalog=cat
    )
    editable_sockets = 0 if parity.is_mirrored else max(0, parity.rune_socket_count - len(kept))

    has_open_crafted = False
    open_pre = open_suf = 0
    if rarity.upper() == "RARE" and not (parity.is_corrupted or parity.is_sanctified or parity.is_mirrored):
        try:
            insp = inspect_item_text(raw_text)
            open_pre = insp.open_prefixes
            open_suf = insp.open_suffixes
            has_open_crafted = (not insp.has_crafted_mod) and (open_pre + open_suf > 0)
        except Exception:
            has_open_crafted = False

    catalyst_eligible = (
        slot_name in _JEWELLERY_BELT_SLOTS
        and not parity.is_corrupted
        and not parity.is_sanctified
        and not parity.is_mirrored
    )
    is_weapon_slot = slot_name in ("Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap")
    return EquippedSlotAudit(
        slot_name=slot_name,
        item_id=item_id,
        name=name,
        base_name=base_name,
        rarity=rarity.upper(),
        item_type=item_type,
        raw_text=raw_text,
        stripped_raw_text=stripped_raw,
        parity=parity,
        editable_socket_count=editable_sockets,
        stripped_runes=tuple(stripped),
        preserved_bound_runes=tuple(kept),
        has_open_crafted_slot=has_open_crafted,
        open_prefix_count=open_pre,
        open_suffix_count=open_suf,
        catalyst_eligible=catalyst_eligible,
        is_weapon_slot=is_weapon_slot,
    )


def analyze_live_net_gap(
    worker: Any,
    *,
    catalog: RuneInstillCatalog | None = None,
) -> NetGapAudit:
    """Run Step 0 on a live `BridgeWorker`: deactivate Charms, inspect WS1/WS2, strip replaceable runes in CoW."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []
    info = worker.call("get_build_info", {})
    level = int(info.get("level") or 90)
    asc = str(info.get("ascendClassName") or info.get("ascendancyName") or "")
    is_shaman = asc.lower() == "shaman"

    listing = worker.call("list_items", {})
    items_by_id = {int(it["id"]): it for it in (listing.get("items") or ()) if isinstance(it, dict) and "id" in it}
    raw_slots = listing.get("slots") or ()

    # Detect Giant's Blood & radius jewels on tree
    tstate = worker.call("get_tree_state", {})
    tdata = worker.call("tree_data", {})
    nodes_map = tdata.get("nodes") or {}
    giants_blood = False
    for nid in tstate.get("allocatedNodes") or ():
        nd = nodes_map.get(str(nid)) or {}
        if _GIANTS_BLOOD_RE.search(str(nd.get("name") or "")):
            giants_blood = True

    radius_jewel_nodes: list[int] = []
    audited_slots: list[EquippedSlotAudit] = []
    has_active_charm = False
    has_ws2_equip = False

    for sl in raw_slots:
        if not isinstance(sl, dict):
            continue
        sname = str(sl.get("name") or "")
        sel_id = int(sl.get("selItemId") or sl.get("selectedId") or 0)
        if sel_id <= 0 or sel_id not in items_by_id:
            continue
        meta = items_by_id[sel_id]
        raw_txt = str(worker.call("item_raw", {"id": sel_id}).get("raw") or "")
        if sname in _CHARM_SLOTS:
            if sl.get("active"):
                has_active_charm = True
            continue
        if sname in _FLASK_SLOTS:
            continue
        if sl.get("nodeId"):
            if _RADIUS_JEWEL_RE.search(raw_txt):
                radius_jewel_nodes.append(int(sl["nodeId"]))
            continue
        if sname in ("Weapon 1 Swap", "Weapon 2 Swap"):
            has_ws2_equip = True
        audited_slots.append(
            audit_equipped_slot(
                slot_name=sname,
                item_id=sel_id,
                name=str(meta.get("name") or ""),
                base_name=str(meta.get("baseName") or ""),
                rarity=str(meta.get("rarity") or "RARE"),
                item_type=str(meta.get("type") or ""),
                raw_text=raw_txt,
                is_shaman=is_shaman,
                catalog=cat,
            )
        )

    if has_active_charm:
        warnings.append("Hit-triggered Charm(s) deactivated for permanent resistance & overcap calculation.")

    # Capture live `currentInGame` baseline BEFORE deactivating hit-triggered Charms.
    # Permanent campaign quest rewards (`quest*`) are NEVER stripped in any snapshot!
    ingame_ws1 = worker.call("get_stats", {}).get("stats") or {}
    ingame_snap = stat_snapshot_from_pob_stats(
        ingame_ws1, level=level, ascendancy=asc, giants_blood=giants_blood
    )

    # Measure permanent baseline (with Charms explicitly turned off) and bare net gap (replaceable runes stripped)
    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
        if has_active_charm:
            for cname in sorted(_CHARM_SLOTS):
                try:
                    worker.call("set_slot_active", {"slotName": cname, "active": False})
                except Exception:
                    pass
        ws1_stats = worker.call("get_stats", {}).get("stats") or {}
        ws2_stats: dict[str, Any] | None = None
        if has_ws2_equip:
            worker.call("set_weapon_swap", {"on": True})
            ws2_stats = worker.call("get_stats", {}).get("stats") or {}
            worker.call("set_weapon_swap", {"on": False})
        current_snap = stat_snapshot_from_pob_stats(
            ws1_stats, level=level, ascendancy=asc, giants_blood=giants_blood, ws2_stats=ws2_stats
        )

        # Strip ONLY replaceable socket runes (preserving all gear, permanent campaign quests, Mirrored & isSocketBound)
        any_stripped = False
        for sl_audit in audited_slots:
            if sl_audit.stripped_runes:
                worker.call("add_item", {"raw": sl_audit.stripped_raw_text, "slotName": sl_audit.slot_name})
                any_stripped = True
        if any_stripped:
            bare_ws1 = worker.call("get_stats", {}).get("stats") or {}
            bare_ws2: dict[str, Any] | None = None
            if has_ws2_equip:
                worker.call("set_weapon_swap", {"on": True})
                bare_ws2 = worker.call("get_stats", {}).get("stats") or {}
                worker.call("set_weapon_swap", {"on": False})
            bare_snap = stat_snapshot_from_pob_stats(
                bare_ws1, level=level, ascendancy=asc, giants_blood=giants_blood, ws2_stats=bare_ws2
            )
        else:
            bare_snap = current_snap

    crafted_hints = [
        {
            "slot": s.slot_name,
            "item": s.name,
            "openPrefixes": s.open_prefix_count,
            "openSuffixes": s.open_suffix_count,
            "note": "0.5.0+ single Crafted mod slot is unused (Essence/Alloy/Distill legal without overwriting).",
        }
        for s in audited_slots
        if s.has_open_crafted_slot
    ]

    return NetGapAudit(
        current=current_snap,
        bare_net=bare_snap,
        slots=tuple(audited_slots),
        protected_radius_jewel_nodes=tuple(sorted(set(radius_jewel_nodes))),
        charm_resists_excluded=True,
        warnings=tuple(warnings),
        crafted_slot_hints=tuple(crafted_hints),
        current_in_game=ingame_snap,
    )


__all__ = [
    "EquippedSlotAudit",
    "NetGapAudit",
    "StatSnapshot",
    "analyze_live_net_gap",
    "audit_equipped_slot",
    "stat_snapshot_from_pob_stats",
]
