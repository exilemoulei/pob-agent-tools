"""ROI node and gem ranking (`rank_nodes`, `rank_gems`)."""

from __future__ import annotations

import math
import re
from typing import Any

from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, strip_pob_escapes
from ..snapshot import _call_batch
from .gem_caveats import (
    _check_support_hard_conflict,
    _gem_archetype_bucket,
    _gem_caveat,
    _group_has_trigger_gem,
    _is_legacy_awakened_gem_safe,
    compute_sadism_per_ailment_pct,
    compute_sadism_stats,
)


def _safe_dist(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val or "").strip()
    if s.isdigit():
        return int(s)
    return 9999


def _safe_float(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val or "0").strip())
    except ValueError:
        return 0.0


_WEAPON_MASTERY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("staff mastery", "staff"),
    ("bow mastery", "bow"),
    ("wand mastery", "wand"),
    ("claw mastery", "claw"),
    ("dagger mastery", "dagger"),
    ("axe mastery", "axe"),
    ("sword mastery", "sword"),
    ("mace mastery", "mace"),
)

_WEAPON_MASTERY_ZH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("长杖专精", "staff"),
    ("長杖專精", "staff"),
    ("弓箭专精", "bow"),
    ("弓类专精", "bow"),
    ("弓類專精", "bow"),
    ("弓专精", "bow"),
    ("弓專精", "bow"),
    ("法杖专精", "wand"),
    ("法杖專精", "wand"),
    ("爪类专精", "claw"),
    ("爪類專精", "claw"),
    ("爪专精", "claw"),
    ("爪專精", "claw"),
    ("匕首专精", "dagger"),
    ("匕首專精", "dagger"),
    ("斧类专精", "axe"),
    ("斧類專精", "axe"),
    ("斧专精", "axe"),
    ("斧專精", "axe"),
    ("剑类专精", "sword"),
    ("劍類專精", "sword"),
    ("剑专精", "sword"),
    ("劍專精", "sword"),
    ("锤类专精", "mace"),
    ("錘類專精", "mace"),
    ("锤专精", "mace"),
    ("錘專精", "mace"),
)


def _node_weapon_mastery_kind(node: dict[str, Any]) -> str | None:
    """Return weapon mastery category (`staff`/`bow`/`wand`/`claw`/`dagger`/`axe`/`sword`/`mace`) or None."""
    name_en = strip_pob_escapes(str(node.get("name") or "")).strip().lower()
    if name_en:
        for prefix, wtype in _WEAPON_MASTERY_PREFIXES:
            if name_en == prefix or name_en.startswith((prefix + ":", prefix + " ")):
                return wtype
    name_zh = strip_pob_escapes(str(node.get("nameZh") or "")).strip()
    if name_zh:
        for prefix, wtype in _WEAPON_MASTERY_ZH_PREFIXES:
            if name_zh.startswith(prefix):
                return wtype
    return None


def _classify_item_weapon_type(item: dict[str, Any]) -> str | None:
    """Classify an equipped Weapon 1 / Weapon 2 item into its PoE1 weapon mastery type."""
    if not isinstance(item, dict):
        return None
    type_fields = " ".join(
        strip_pob_escapes(str(item.get(k) or ""))
        for k in ("type", "subType", "itemClass", "baseType", "baseName")
    ).lower()
    raw_txt = strip_pob_escapes(str(item.get("raw") or "")).lower()
    name_fields = strip_pob_escapes(str(item.get("name") or item.get("title") or "")).lower()
    hay = f"{type_fields} {name_fields}".strip()

    def _match_text(txt: str) -> str | None:
        if not txt:
            return None
        if "shield" in txt or "quiver" in txt or "buckler" in txt:
            return "non_weapon"
        if "sceptre" in txt or "scepter" in txt:
            return "mace"
        if "warstaff" in txt or "quarterstaff" in txt or re.search(r"\bstaff\b|\bstaves\b", txt):
            return "staff"
        if re.search(r"\bbow\b", txt):
            return "bow"
        if re.search(r"\bwand\b", txt):
            return "wand"
        if re.search(r"\bclaw\b", txt):
            return "claw"
        if "dagger" in txt:
            return "dagger"
        if re.search(r"\baxe\b|\bhatchet\b|\bcleaver\b|\bchopper\b|\bhalberd\b|\blabrys\b", txt):
            return "axe"
        if re.search(
            r"\bsword\b|\brapier\b|\bfoil\b|\bblade\b|\bsabre\b|\bsaber\b|\bcutlass\b|\bestoc\b|\bgreatsword\b",
            txt,
        ):
            return "sword"
        if re.search(
            r"\bmace\b|\bmaul\b|\bclub\b|\bhammer\b|\bmallet\b|\bbreaker\b|\bgavel\b|\btenderizer\b|\bmorning star\b",
            txt,
        ):
            return "mace"
        return None

    for candidate in (type_fields, hay):
        res = _match_text(candidate)
        if res is not None:
            return res
    if raw_txt:
        return _match_text(" ".join(raw_txt.splitlines()[:4]))
    return None


def _detect_equipped_weapon_types(worker: BridgeWorker) -> set[str] | None:
    """Best-effort inspect `Weapon 1` / `Weapon 2` equipped weapon categories."""
    try:
        items_res = worker.call("list_items", {})
    except Exception:
        return None
    if not isinstance(items_res, dict):
        return None
    slots = items_res.get("slots")
    if not isinstance(slots, list) or not slots:
        return None

    slots_by_name = {str(s.get("name") or ""): s for s in slots if isinstance(s, dict)}
    if "Weapon 1" not in slots_by_name and "Weapon 2" not in slots_by_name:
        return None

    items_by_id = {
        it.get("id"): it
        for it in (items_res.get("items") or [])
        if isinstance(it, dict) and it.get("id") is not None
    }

    weapon_types: set[str] = set()
    for slot_name in ("Weapon 1", "Weapon 2"):
        slot_obj = slots_by_name.get(slot_name)
        if not slot_obj:
            continue
        item_id = slot_obj.get("selItemId")
        if not item_id:
            continue
        item_obj = items_by_id.get(item_id)
        wtype = _classify_item_weapon_type(item_obj) if item_obj is not None else None
        if wtype is None:
            try:
                fetched = worker.call("get_item", {"id": item_id})
                if isinstance(fetched, dict):
                    item_data = fetched.get("item") if isinstance(fetched.get("item"), dict) else fetched
                    wtype = _classify_item_weapon_type(item_data)
            except Exception:
                wtype = None
        if wtype is None:
            return None
        if wtype != "non_weapon":
            weapon_types.add(wtype)

    return weapon_types


def rank_nodes(
    worker: BridgeWorker,
    *,
    stat: str = "CombinedDPS",
    max_dist: int = 5,
    top: int = 15,
    baseline_label: str | None = None,
) -> dict[str, Any]:
    """Rank unallocated passive tree nodes by ROI (`pathPower` per point and raw `power`)."""
    with worker.lock:
        eff_stat = stat
        stat_fallback: str | None = None
        try:
            res = worker.call("node_power", {"stat": eff_stat, "maxDepth": max_dist}, timeout=120.0)
            if isinstance(res, dict) and (res.get("error") or res.get("ok") is False):
                err_txt = str(res.get("error") or "")
                if eff_stat != "CombinedDPS" and (
                    "unknown power stat" in err_txt.lower() or eff_stat.startswith("Minion")
                ):
                    raise RuntimeError(err_txt or "unknown power stat")
        except Exception as exc:
            err_low = str(exc).lower()
            if eff_stat != "CombinedDPS" and ("unknown power stat" in err_low or eff_stat.startswith("Minion")):
                stat_fallback = "CombinedDPS"
                eff_stat = "CombinedDPS"
                res = worker.call("node_power", {"stat": eff_stat, "maxDepth": max_dist}, timeout=120.0)
            else:
                raise

        report = res.get("report", [])
        equipped_weapons = _detect_equipped_weapon_types(worker)

        raw_candidates: list[dict[str, Any]] = []
        cluster_candidates: list[dict[str, Any]] = []
        filtered_masteries: list[str] = []
        for r in report:
            if r.get("allocated"):
                continue
            raw_dist = r.get("pathDist")
            if isinstance(raw_dist, str) and raw_dist.strip().lower() == "cluster":
                if _safe_float(r.get("power")) > 0:
                    cluster_candidates.append(r)
                continue
            if _safe_dist(raw_dist) > max_dist:
                continue
            if not (_safe_float(r.get("pathPower")) > 0 or _safe_float(r.get("power")) > 0):
                continue
            if equipped_weapons is not None:
                wm_kind = _node_weapon_mastery_kind(r)
                if wm_kind is not None and wm_kind not in equipped_weapons:
                    filtered_masteries.append(strip_pob_escapes(str(r.get("name") or r.get("nameZh") or wm_kind)))
                    continue
            if not r.get("allocSpec") and r.get("id") is not None:
                eff = r.get("effectId")
                r = {**r, "allocSpec": f"{r['id']}:{eff}" if eff is not None else str(r["id"])}
            raw_candidates.append(r)

        best_by_effect: dict[Any, dict[str, Any]] = {}
        for r in raw_candidates:
            eff_id = r.get("effectId")
            if eff_id is None:
                continue
            prev = best_by_effect.get(eff_id)
            if prev is None or (
                _safe_float(r.get("pathPower")),
                _safe_float(r.get("power")),
                -_safe_dist(r.get("pathDist")),
            ) > (
                _safe_float(prev.get("pathPower")),
                _safe_float(prev.get("power")),
                -_safe_dist(prev.get("pathDist")),
            ):
                best_by_effect[eff_id] = r
        candidates = [
            r for r in raw_candidates if r.get("effectId") is None or best_by_effect.get(r.get("effectId")) is r
        ]

        by_per_point = sorted(
            candidates,
            key=lambda r: (_safe_float(r.get("pathPower")), _safe_float(r.get("power"))),
            reverse=True,
        )[:top]
        by_single_node = sorted(
            candidates,
            key=lambda r: (_safe_float(r.get("power")), _safe_float(r.get("pathPower"))),
            reverse=True,
        )[:top]
        by_cluster = sorted(
            cluster_candidates,
            key=lambda r: _safe_float(r.get("power")),
            reverse=True,
        )[:top]

        out: dict[str, Any] = {
            "stat": res.get("stat") or eff_stat,
            "availableStats": res.get("stats", []),
            "maxDist": max_dist,
            "totalCandidates": len(candidates),
            "byPerPoint": by_per_point,
            "byTotalGain": by_single_node,
        }
        if baseline_label:
            out["baselineLabel"] = baseline_label
        if by_cluster:
            out["topCluster"] = by_cluster
        if stat_fallback is not None:
            out["stat_fallback"] = stat_fallback
        if filtered_masteries:
            out["filteredIncompatibleMasteries"] = filtered_masteries
        if isinstance(res.get("socketSummary"), dict):
            out["socketSummary"] = res["socketSummary"]
        if isinstance(res.get("nearbySockets"), list):
            out["nearbySockets"] = [
                s
                for s in res["nearbySockets"]
                if isinstance(s, dict) and (bool(s.get("allocated")) or _safe_dist(s.get("cost")) <= max_dist)
            ]
        return out


def rank_gems(
    worker: BridgeWorker,
    *,
    group_selector: str = "main",
    slot_idx: int | None = None,
    add_link: bool = False,
    field: str = "CombinedDPS",
    top: int = 15,
    exclude_archetype_swaps: bool = False,
    include_legacy: bool = False,
    baseline_label: str | None = None,
) -> dict[str, Any]:
    """Rank candidate gems for a skill group slot (or a new link) by DPS gain."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g.get("index") == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found.")
        gems = group.get("gems", [])
        group_has_trigger = _group_has_trigger_gem(gems)
        # `get_stats` describes the main skill. Its damage split and mana cost say nothing about a
        # support socketed in another group, so the conflict and mana checks only run for the main one.
        ranks_main_group = bool(group.get("isMain")) or skills_res.get("mainSocketGroup") == g_idx

        main_mana_cost: float | None = None
        stats_map: dict[str, Any] = {}
        try:
            st_res = worker.call("get_stats", {}) if ranks_main_group else None
            if isinstance(st_res, dict) and isinstance(st_res.get("stats"), dict):
                stats_map = st_res["stats"]
            raw_mc = stats_map.get("ManaCost")
            if raw_mc is not None:
                parsed_mc = _parse_numeric_or_none(raw_mc)
                if parsed_mc is not None and math.isfinite(parsed_mc):
                    main_mana_cost = float(parsed_mc)
        except Exception:
            main_mana_cost = None

        if add_link or slot_idx is None:
            target_idx = len(gems) + 1
            mode = f"add_link (slot #{target_idx})"
            supportable_only = True
        else:
            target_idx = int(slot_idx)
            mode = f"replace_slot (slot #{target_idx})"
            existing_gem = next((gm for gm in gems if gm.get("index") == target_idx), None)
            supportable_only = bool(existing_gem.get("support", True)) if existing_gem else True

        existing_archetypes: set[str] = set()
        if exclude_archetype_swaps:
            for gm in gems:
                if not gm.get("enabled", True) or gm.get("fromItem") or gm.get("fromNode"):
                    continue
                nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
                b = _gem_archetype_bucket(nm)
                if b:
                    existing_archetypes.add(b)
                for tag in ("totem", "trap", "mine"):
                    if tag in nm:
                        existing_archetypes.add(tag)

        extra_fetch = (20 if not include_legacy else 0) + (15 if exclude_archetype_swaps else 0) + 10
        fetch_limit = top + extra_fetch

        prev_opts = worker.call("get_gem_options", {})
        try:
            worker.call(
                "set_gem_options",
                {"sortGemsByDPS": True, "sortGemsByDPSField": field or "CombinedDPS"},
            )
            search_res = worker.call(
                "gem_search",
                {
                    "group": g_idx,
                    "index": target_idx,
                    "byDps": True,
                    "supportableOnly": supportable_only,
                    "limit": fetch_limit,
                },
            )
            raw_ranked = search_res.get("gems", [])
            cfg_res_lazy: dict[str, Any] | None = None
            cfg_fetched = False
            filtered_swaps: list[str] = []
            filtered_legacy: list[str] = []
            excluded_supports: list[dict[str, Any]] = []
            ranked: list[dict[str, Any]] = []
            for cand in raw_ranked:
                cname = str(cand.get("name") or "")
                if not include_legacy and _is_legacy_awakened_gem_safe(cname):
                    filtered_legacy.append(cname)
                    continue
                if supportable_only:
                    if "hypothermia" in cname.lower() and not cfg_fetched:
                        cfg_fetched = True
                        try:
                            c_res = worker.call("list_config", {})
                            cfg_res_lazy = c_res if isinstance(c_res, dict) else None
                        except Exception:
                            cfg_res_lazy = None
                    conflict_reason = _check_support_hard_conflict(
                        cname, stats_map, skills_res=skills_res, cfg_res=cfg_res_lazy
                    )
                    if conflict_reason:
                        excluded_supports.append(
                            {
                                "name": cname,
                                "nameZh": cand.get("nameZh"),
                                "delta": cand.get("delta"),
                                "pct": cand.get("pct"),
                                "reason": conflict_reason,
                            }
                        )
                        continue
                if exclude_archetype_swaps:
                    cbucket = _gem_archetype_bucket(cname)
                    if cbucket is not None and cbucket not in existing_archetypes:
                        filtered_swaps.append(f"{cname} ({cbucket})")
                        continue
                    caveat = _gem_caveat(
                        cname, main_mana_cost=main_mana_cost, group_has_trigger=group_has_trigger
                    )
                    if caveat == "0 charges on 0-mana skill!":
                        filtered_swaps.append(f"{cname} (0-mana skill: 0 charges)")
                        continue
                else:
                    caveat = _gem_caveat(
                        cname, main_mana_cost=main_mana_cost, group_has_trigger=group_has_trigger
                    )
                cand_copy = dict(cand)
                if caveat:
                    cand_copy["caveat"] = caveat
                ranked.append(cand_copy)
                if len(ranked) >= top:
                    break
            top_cands = [c for c in ranked[:5] if c.get("gemId")]
            if top_cands:
                tip_calls = [
                    ("gem_candidate_tooltip", {"group": g_idx, "index": target_idx, "gemId": c["gemId"]})
                    for c in top_cands
                ]
                tips = _call_batch(worker, tip_calls, raise_on_error=False)
                for cand, tip in zip(top_cands, tips, strict=False):
                    if isinstance(tip, dict) and not tip.get("_error") and tip.get("ok") is not False and "dps" in tip:
                        cand["tooltipDps"] = tip.get("dps")
        finally:
            try:
                restore_payload: dict[str, Any] = {}
                if "sortGemsByDPS" in prev_opts:
                    restore_payload["sortGemsByDPS"] = bool(prev_opts["sortGemsByDPS"])
                if prev_opts.get("sortGemsByDPSField"):
                    restore_payload["sortGemsByDPSField"] = prev_opts["sortGemsByDPSField"]
                if restore_payload:
                    worker.call("set_gem_options", restore_payload)
            except Exception:
                pass

        out_res: dict[str, Any] = {
            "groupIndex": g_idx,
            "groupLabel": group.get("displayLabelZh") or group.get("displayLabel") or group.get("label"),
            "targetIndex": target_idx,
            "mode": mode,
            "dpsField": search_res.get("dpsField") or field,
            "baseDps": search_res.get("baseDps"),
            "candidates": ranked,
            "filtered_legacy": filtered_legacy,
        }
        if baseline_label:
            out_res["baselineLabel"] = baseline_label
        if excluded_supports:
            out_res["excludedSupports"] = excluded_supports
        if filtered_swaps:
            out_res["filteredArchetypeSwaps"] = filtered_swaps
        return out_res

__all__ = [
    "_classify_item_weapon_type",
    "_detect_equipped_weapon_types",
    "_gem_archetype_bucket",
    "_gem_caveat",
    "_is_legacy_awakened_gem_safe",
    "_node_weapon_mastery_kind",
    "_safe_dist",
    "_safe_float",
    "compute_sadism_per_ailment_pct",
    "compute_sadism_stats",
    "rank_gems",
    "rank_nodes",
]
