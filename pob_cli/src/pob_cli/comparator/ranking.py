"""ROI node and gem ranking (`rank_nodes`, `rank_gems`)."""

from __future__ import annotations

import math
import re
from typing import Any

from .. import game_data as _game_data
from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, strip_pob_escapes
from ..snapshot import _call_batch


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

        candidates: list[dict[str, Any]] = []
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
            candidates.append(r)

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
        if by_cluster:
            out["topCluster"] = by_cluster
        if stat_fallback is not None:
            out["stat_fallback"] = stat_fallback
        if filtered_masteries:
            out["filteredIncompatibleMasteries"] = filtered_masteries
        return out


_EXCEPTIONAL_AWAKENED_KEEP: frozenset[str] = frozenset(
    {
        "awakened empower",
        "awakened enhance",
        "awakened enlighten",
    }
)


def _is_legacy_awakened_gem_safe(name: str) -> bool:
    fn = getattr(_game_data, "is_legacy_awakened_gem", None)
    if callable(fn):
        try:
            return bool(fn(name))
        except Exception:
            pass
    low = strip_pob_escapes(str(name or "")).strip().lower()
    if low.endswith(" support"):
        low = low[: -len(" support")].strip()
    return low.startswith("awakened ") and low not in _EXCEPTIONAL_AWAKENED_KEEP


_ARCHETYPE_SWAP_SUPPORT_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "trigger",
        (
            "cast when stunned",
            "cast on critical strike",
            "cast when damage taken",
            "cast on death",
            "cast while channelling",
            "cast on melee kill",
            "cast on ward break",
            "spellslinger",
            "arcanist brand",
            "manaforged arrows",
            "mark on hit",
            "hextouch",
            "battlemage's cry",
            "automation",
            "autoexertion",
        ),
    ),
    ("totem", ("spell totem", "ballista totem", "earthbreaker", "multiple totems")),
    (
        "trap",
        ("trap support", "cluster traps", "multiple traps", "charged traps", "advanced traps", "trap and mine damage"),
    ),
    (
        "mine",
        ("high-impact mine", "blastchain mine", "minefield", "swift assembly", "charged mines", "locus mine"),
    ),
)


def _gem_archetype_bucket(name_en: str) -> str | None:
    low = strip_pob_escapes(name_en or "").strip().lower()
    if low.startswith("awakened "):
        low = low[len("awakened ") :].strip()
    if low == "trap":
        return "trap"
    for bucket, patterns in _ARCHETYPE_SWAP_SUPPORT_BUCKETS:
        for pat in patterns:
            if low == pat or low.startswith(pat + " ") or low == pat + " support":
                return bucket
    return None


_SUPPORT_GEM_CAVEATS: tuple[tuple[str, str], ...] = (
    ("immolate", "Requires Enemy Burning/Ignited"),
    ("hypothermia", "Requires Enemy Chilled"),
    ("trinity", "Requires 2+ alternating highest hit elements"),
    ("elemental focus", "Cannot inflict Elemental Ailments (breaks Shock/Chill/Freeze/Ignite)"),
    ("controlled destruction", "80% less Crit Chance"),
    ("concentrated effect", "30% less Area of Effect (check overlap/clear)"),
    ("brutality", "Deals NO Elemental or Chaos Damage"),
    ("void manipulation", "Deals NO Elemental Damage"),
    ("sacrifice", "Sacrifices 20% Current Life per cast (bricks CI / deadly on Low-Life)"),
    (
        "sadism",
        "70% LESS Ailment Duration! Inflates PoB DPS rate while slashing total per-ignite damage (~-74%) and breaking uptime",
    ),
    ("awakened empower", "Exceptional (Max Lv4 / corrupt Lv5; mirror-tier endgame budget)"),
    ("awakened enhance", "Exceptional (Max Lv4 / corrupt Lv5; endgame budget)"),
    ("awakened enlighten", "Exceptional (Max Lv4 / corrupt Lv5; endgame budget)"),
)


def _gem_caveat(name_en: str, *, main_mana_cost: float | None = None) -> str | None:
    raw_low = strip_pob_escapes(name_en or "").strip().lower()
    if raw_low.endswith(" support"):
        raw_low = raw_low[: -len(" support")].strip()
    for pat, note in _SUPPORT_GEM_CAVEATS:
        if raw_low == pat or raw_low.startswith(pat + " "):
            return note
    low = raw_low[len("awakened ") :].strip() if raw_low.startswith("awakened ") else raw_low
    if low == "inspiration" and main_mana_cost is not None:
        if main_mana_cost <= 0.0:
            return "0 charges on 0-mana skill!"
        if main_mana_cost >= 400.0:
            return (
                f"High ManaCost ({round(main_mana_cost)}) vs Lv20 800-mana reset threshold "
                "— resets every 1-2 casts (real DPS far below PoB's assumed 5 charges!)"
            )
    if low != raw_low:
        for pat, note in _SUPPORT_GEM_CAVEATS:
            if low == pat or low.startswith(pat + " "):
                return note
    return None


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
) -> dict[str, Any]:
    """Rank candidate gems for a skill group slot (or a new link) by DPS gain."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g.get("index") == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found.")
        gems = group.get("gems", [])

        main_mana_cost: float | None = None
        try:
            st_res = worker.call("get_stats", {})
            raw_mc = (st_res.get("stats") or {}).get("ManaCost") if isinstance(st_res, dict) else None
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

        extra_fetch = (20 if not include_legacy else 0) + (15 if exclude_archetype_swaps else 0)
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
            filtered_swaps: list[str] = []
            filtered_legacy: list[str] = []
            ranked: list[dict[str, Any]] = []
            for cand in raw_ranked:
                cname = str(cand.get("name") or "")
                if not include_legacy and _is_legacy_awakened_gem_safe(cname):
                    filtered_legacy.append(cname)
                    continue
                if exclude_archetype_swaps:
                    cbucket = _gem_archetype_bucket(cname)
                    if cbucket is not None and cbucket not in existing_archetypes:
                        filtered_swaps.append(f"{cname} ({cbucket})")
                        continue
                    caveat = _gem_caveat(cname, main_mana_cost=main_mana_cost)
                    if caveat == "0 charges on 0-mana skill!":
                        filtered_swaps.append(f"{cname} (0-mana skill: 0 charges)")
                        continue
                else:
                    caveat = _gem_caveat(cname, main_mana_cost=main_mana_cost)
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
        if filtered_swaps:
            out_res["filteredArchetypeSwaps"] = filtered_swaps
        return out_res
