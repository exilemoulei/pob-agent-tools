"""Allocated passive-tree base-slot inventory for PoE1 3.30 Tattoos & Runegrafts."""

from __future__ import annotations

from typing import Any

TATTOO_MAX_LIMIT = 50

BUCKET_SMALL_STR = "Small Strength"
BUCKET_SMALL_DEX = "Small Dexterity"
BUCKET_SMALL_INT = "Small Intelligence"
BUCKET_NOTABLE_STR = "Notable: +30 to Strength"
BUCKET_NOTABLE_DEX = "Notable: +30 to Dexterity"
BUCKET_NOTABLE_INT = "Notable: +30 to Intelligence"
BUCKET_MASTERY = "Mastery"
BUCKET_KEYSTONE = "Keystone"

_SMALL_NAME_TO_BUCKET: dict[str, str] = {
    "Strength": BUCKET_SMALL_STR,
    "Dexterity": BUCKET_SMALL_DEX,
    "Intelligence": BUCKET_SMALL_INT,
}
_NOTABLE_STAT_TO_BUCKET: tuple[tuple[str, str], ...] = (
    ("+30 to Strength", BUCKET_NOTABLE_STR),
    ("+30 to Dexterity", BUCKET_NOTABLE_DEX),
    ("+30 to Intelligence", BUCKET_NOTABLE_INT),
)
_ALL_BUCKETS: tuple[str, ...] = (
    BUCKET_SMALL_STR,
    BUCKET_SMALL_DEX,
    BUCKET_SMALL_INT,
    BUCKET_NOTABLE_STR,
    BUCKET_NOTABLE_DEX,
    BUCKET_NOTABLE_INT,
    BUCKET_MASTERY,
    BUCKET_KEYSTONE,
)
_EXCLUDED_NODE_TYPES = frozenset({"ClassStart", "AscendClassStart", "Socket"})


def _empty_bucket() -> dict[str, Any]:
    return {
        "total": 0,
        "eligible": 0,
        "free": 0,
        "tattooed": 0,
        "conquered": 0,
        "freeLinkedCounts": [],
        "tattooedLinkedCounts": [],
    }


def _classify_raw_base_bucket(raw_base: dict[str, Any]) -> str | None:
    """Map an un-overridden `nodes_db` entry to its PoE1 tattoo/runegraft base slot bucket."""
    if raw_base.get("expansion") or raw_base.get("expansionSkill") or raw_base.get("asc") or raw_base.get("ascendancyName"):
        return None
    if raw_base.get("proxy") or raw_base.get("blighted") or raw_base.get("isBlighted"):
        return None
    ntype = str(raw_base.get("type") or "Normal")
    if ntype in _EXCLUDED_NODE_TYPES:
        return None
    if ntype in (BUCKET_KEYSTONE, BUCKET_MASTERY):
        return ntype
    if ntype == "Normal":
        return _SMALL_NAME_TO_BUCKET.get(str(raw_base.get("name") or "").strip())
    if ntype == "Notable":
        stats = [str(s) for s in (raw_base.get("stats") or ()) if s]
        for marker, bucket in _NOTABLE_STAT_TO_BUCKET:
            if any(marker in s for s in stats):
                return bucket
    return None


def _override_tattoo_kind(ov: dict[str, Any] | None, raw_bucket: str) -> str:
    """Classify a `bridge.lua:get_tree_state` override as `'conquered'`, `'runegraft'`, `'tattoo'`, or `'free'`."""
    if not isinstance(ov, dict):
        return "free"
    why = str(ov.get("why") or "").strip().lower()
    if why == "conquered":
        return "conquered"
    ov_name_l = str(ov.get("name") or "").strip().lower()
    ov_name_zh = str(ov.get("nameZh") or "")
    # PassiveSpec.lua:1450 leaves masterySelections[id] set when a Mastery is tattooed with a Runegraft.
    if ov_name_l.startswith("runegraft ") or "符文" in ov_name_zh:
        return "runegraft"
    if why == "tattoo":
        return "runegraft" if raw_bucket == BUCKET_MASTERY else "tattoo"
    return "free"


def analyze_tattoo_slots(
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
    *,
    target_slot: str = "main",
    build_loaded: bool | None = None,
) -> dict[str, Any]:
    """Inventory allocated un-overridden passive tree base slots eligible for Tattoos and Runegrafts."""
    buckets: dict[str, dict[str, Any]] = {k: _empty_bucket() for k in _ALL_BUCKETS}
    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    dynamic_map = {str(n["id"]): n for n in (tree_state.get("dynamicNodes") or ()) if isinstance(n, dict) and "id" in n}

    tattoos_used = runegrafts_used = 0
    seen_ids: set[int] = set()
    has_non_start_alloc = False
    for raw_nid in tree_state.get("allocatedNodes") or ():
        try:
            nid = int(raw_nid)
        except (TypeError, ValueError):
            continue
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        sid = str(nid)
        dyn = dynamic_map.get(sid)
        raw_base = nodes_db.get(sid)
        ntype = str((raw_base if isinstance(raw_base, dict) else (dyn if isinstance(dyn, dict) else {})).get("type") or "")
        if ntype not in ("ClassStart", "AscendClassStart"):
            has_non_start_alloc = True
        if isinstance(dyn, dict) and (dyn.get("expansion") or dyn.get("expansionSkill")):
            continue
        if not isinstance(raw_base, dict):
            continue
        bucket_key = _classify_raw_base_bucket(raw_base)
        if bucket_key is None:
            continue

        b = buckets[bucket_key]
        b["total"] += 1
        status = _override_tattoo_kind(overrides.get(sid), bucket_key)
        if status == "conquered":
            b["conquered"] += 1
            continue
        linked = raw_base.get("linked")
        link_cnt = len(linked) if isinstance(linked, (list, tuple)) else 0
        b["eligible"] += 1
        if status in ("tattoo", "runegraft"):
            b["tattooed"] += 1
            b["tattooedLinkedCounts"].append(link_cnt)
            if status == "runegraft":
                runegrafts_used += 1
            else:
                tattoos_used += 1
        else:
            b["free"] += 1
            b["freeLinkedCounts"].append(link_cnt)

    effective_loaded = has_non_start_alloc if build_loaded is None else (bool(build_loaded) and has_non_start_alloc)
    return {
        "targetSlot": target_slot,
        "buildLoaded": effective_loaded,
        "treeVersion": tree_state.get("treeVersion"),
        "className": tree_state.get("className"),
        "ascendClassName": tree_state.get("ascendClassName"),
        "tattoosUsed": tattoos_used,
        "tattooLimit": TATTOO_MAX_LIMIT,
        "runegraftsUsed": runegrafts_used,
        "buckets": buckets,
    }


def inspect_build_tattoo_slots(worker: Any, *, target_slot: str = "main") -> dict[str, Any]:
    """Fetch `get_tree_state` + raw static `nodes_db` from `worker` and inventory tattoo base slots."""
    lock = getattr(worker, "lock", None)
    if lock is not None:
        with lock:
            tree_state = worker.call("get_tree_state", {})
            nodes_db = (worker.get_cached_tree_data(tree_state.get("treeVersion")) or {}).get("nodes", {})
    else:
        tree_state = worker.call("get_tree_state", {})
        nodes_db = (worker.get_cached_tree_data(tree_state.get("treeVersion")) or {}).get("nodes", {})
    raw_loaded = getattr(worker, "build_loaded", None)
    if raw_loaded is None and hasattr(worker, "last_good_code") and getattr(worker, "last_good_code", None):
        raw_loaded = True
    return analyze_tattoo_slots(
        tree_state,
        nodes_db if isinstance(nodes_db, dict) else {},
        target_slot=target_slot,
        build_loaded=raw_loaded,
    )


def _matching_bucket_keys(target_type: str, target_value: str) -> tuple[str, ...]:
    tt_l, tv_l = str(target_type or "").strip().lower(), str(target_value or "").strip().lower()
    direct = {
        "small strength": (BUCKET_SMALL_STR,),
        "small dexterity": (BUCKET_SMALL_DEX,),
        "small intelligence": (BUCKET_SMALL_INT,),
        "small attribute": (BUCKET_SMALL_STR, BUCKET_SMALL_DEX, BUCKET_SMALL_INT),
        "mastery": (BUCKET_MASTERY,),
        "keystone": (BUCKET_KEYSTONE,),
    }
    if tt_l in direct:
        return direct[tt_l]
    if tt_l == "notable":
        for marker, bucket in _NOTABLE_STAT_TO_BUCKET:
            if marker.lower() in tv_l:
                return (bucket,)
        return tuple(b for _, b in _NOTABLE_STAT_TO_BUCKET)
    return ()


def annotate_tattoo_results_for_build(records: list[dict[str, Any]], build_slots: dict[str, Any]) -> None:
    """Attach `availableOnTree`, `eligibleNodes`, `freeNodes`, `tattooedNodes`, `conqueredNodes` to records."""
    if not isinstance(build_slots, dict) or not build_slots.get("buildLoaded", True):
        return
    buckets = build_slots.get("buckets") if isinstance(build_slots.get("buckets"), dict) else {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        keys = _matching_bucket_keys(str(rec.get("target_type") or ""), str(rec.get("target_value") or ""))
        min_c = int(rec.get("min_connected") or 0)
        max_c = int(rec.get("max_connected") if rec.get("max_connected") is not None else 100)
        free_n = tat_n = conq_n = 0
        for bk in keys:
            b = buckets.get(bk)
            if not isinstance(b, dict):
                continue
            fl, tl = b.get("freeLinkedCounts"), b.get("tattooedLinkedCounts")
            free_n += sum(1 for lc in fl if min_c <= int(lc) <= max_c) if isinstance(fl, list) else int(b.get("free") or 0)
            tat_n += sum(1 for lc in tl if min_c <= int(lc) <= max_c) if isinstance(tl, list) else int(b.get("tattooed") or 0)
            conq_n += int(b.get("conquered") or 0)
        elig_n = free_n + tat_n
        rec.update(
            {
                "availableOnTree": elig_n > 0,
                "eligibleNodes": elig_n,
                "freeNodes": free_n,
                "tattooedNodes": tat_n,
                "conqueredNodes": conq_n,
            }
        )


def annotate_data_response_for_build(res: dict[str, Any], build_slots: dict[str, Any]) -> dict[str, Any]:
    """Annotate single- or multi-query `pob data tattoo|runegraft` response dict in place."""
    res["buildTattooSlots"] = build_slots
    raw_results = res.get("results")
    if isinstance(raw_results, list):
        annotate_tattoo_results_for_build(raw_results, build_slots)
    elif isinstance(raw_results, dict):
        for sub in raw_results.values():
            if isinstance(sub, dict) and isinstance(sub.get("results"), list):
                annotate_tattoo_results_for_build(sub["results"], build_slots)
    return res
