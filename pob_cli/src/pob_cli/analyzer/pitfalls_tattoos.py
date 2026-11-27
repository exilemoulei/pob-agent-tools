"""Anti-bypass (`customMods`) and static XML `<Override>` legality audit for PoE1 Tattoos & Runegrafts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..formatters import strip_pob_escapes
from ..game_data import _parse_tattoos, _ZhDictionary, fold_han, query_local_data, resolve_pob_dir
from .tattoo_slots import (
    BUCKET_MASTERY,
    TATTOO_MAX_LIMIT,
    _classify_raw_base_bucket,
    _matching_bucket_keys,
    _override_tattoo_kind,
)
from .tree_helpers import classify_base_target_type

_BYPASS_TOKEN_RE = re.compile(
    r"(?:\btattoos?\b|\brunegrafts?\b|文身|纹身|符文之结|符文嫁接|符文嵌板)",
    re.IGNORECASE,
)
_LIMIT_TO_RE = re.compile(r"^limited\s+to\s+(\d+)\s+(.+?)\s*$", re.IGNORECASE)
_CATALOG_CACHE: dict[str, tuple[tuple[int, int], dict[str, dict[str, Any]]]] = {}


def _catalog_file_fingerprint(pob_dir: Path | str | None) -> tuple[int, int] | None:
    if not pob_dir:
        return None
    lua = Path(pob_dir) / "PoE1" / "Data" / "TattooPassives.lua"
    try:
        st = lua.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _index_tattoo_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key_raw in (rec.get("name"), rec.get("id")):
            if key_raw:
                idx[str(key_raw).strip().lower()] = rec
        zh_nm = rec.get("zh_name")
        if zh_nm:
            idx[fold_han(str(zh_nm).strip().lower())] = rec
    return idx


def load_tattoo_catalog(worker: Any = None) -> dict[str, dict[str, Any]]:
    """Load cached Tattoo/Runegraft metadata keyed by lowered EN name, id, and folded ZH name."""
    pob_dir = getattr(worker, "pob_dir", None) or getattr(worker, "pob_root", None)
    runtime_dir = getattr(worker, "runtime_dir", None)
    cache_key = str(pob_dir or "")
    fp = _catalog_file_fingerprint(pob_dir)
    if cache_key and fp is not None:
        cached = _CATALOG_CACHE.get(cache_key)
        if cached is not None and cached[0] == fp:
            return cached[1]

    records: list[dict[str, Any]] = []
    try:
        res = query_local_data(
            "tattoo",
            "",
            include_legacy=True,
            limit=0,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
        )
        raw_recs = res.get("results") if isinstance(res, dict) else None
        if isinstance(raw_recs, list) and raw_recs:
            records = raw_recs
    except Exception:
        records = []

    if not records:
        try:
            eff_dir = Path(pob_dir) if pob_dir else resolve_pob_dir()
            if eff_dir is not None and (eff_dir / "PoE1" / "Data" / "TattooPassives.lua").is_file():
                records = _parse_tattoos(eff_dir / "PoE1", _ZhDictionary())
        except Exception:
            records = []

    indexed = _index_tattoo_records(records)
    if indexed and cache_key and fp is not None:
        _CATALOG_CACHE[cache_key] = (fp, indexed)
    return indexed


def _lookup_catalog(catalog: dict[str, dict[str, Any]], ov: dict[str, Any]) -> dict[str, Any] | None:
    for cand in (ov.get("name"), ov.get("id")):
        if cand:
            hit = catalog.get(strip_pob_escapes(str(cand)).strip().lower())
            if hit is not None:
                return hit
    zh = ov.get("nameZh")
    if zh:
        hit = catalog.get(fold_han(strip_pob_escapes(str(zh)).strip().lower()))
        if hit is not None:
            return hit
    return None


def _parse_limit_rule(rec: dict[str, Any] | None, ov: dict[str, Any]) -> tuple[int, str] | None:
    lines: list[str] = []
    if isinstance(rec, dict):
        if rec.get("limit_text"):
            lines.append(str(rec["limit_text"]))
        lines.extend(str(s) for s in (rec.get("full_stats") or ()) if s)
    lines.extend(str(s) for s in (ov.get("stats") or ()) if s)
    for ln in lines:
        m = _LIMIT_TO_RE.match(strip_pob_escapes(ln).strip())
        if m:
            return int(m.group(1)), " ".join(m.group(2).lower().split())
    return None


def check_custom_mods_tattoo_bypass(cfg_res: dict[str, Any] | None) -> list[dict[str, str]]:
    """Detect ConfigTab `customMods` lines attempting to simulate Tattoos or Runegrafts."""
    if not isinstance(cfg_res, dict):
        return []
    hits: list[str] = []
    for blk in cfg_res.get("customMods") or ():
        if not isinstance(blk, dict) or blk.get("enabled", True) is False:
            continue
        title = strip_pob_escapes(str(blk.get("title") or "")).strip()
        if title and _BYPASS_TOKEN_RE.search(fold_han(title)):
            hits.append(f"block '{title}'")
        for raw_ln in str(blk.get("text") or "").splitlines():
            clean = strip_pob_escapes(raw_ln).strip()
            if clean and _BYPASS_TOKEN_RE.search(fold_han(clean)):
                hits.append(f"'{clean[:80]}'")
    if not hits:
        return []
    sample = "; ".join(dict.fromkeys(hits[:4]))
    return [
        {
            "category": "TattooBypass",
            "severity": "HIGH",
            "message": (
                f"[CUSTOM-MOD-TATTOO-BYPASS] ConfigTab customMods contains tattoo/runegraft text ({sample}). "
                "Do not fake passive tree tattoos in customMods — apply real tree overrides via "
                "`pob tree tattoo --node <id> --tattoo <name>`."
            ),
        }
    ]


def _extract_linked_ids(raw_base: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for key in ("linked", "links"):
        vals = raw_base.get(key)
        if isinstance(vals, (list, tuple)):
            for x in vals:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
            if out:
                return out
    return out


def count_allocated_adjacent(
    raw_base: dict[str, Any],
    allocated_set: set[int],
    nodes_db: dict[str, Any],
) -> int | None:
    """Return allocated adjacent passive count, or None when allocation or graph data is incomplete."""
    linked_ids = _extract_linked_ids(raw_base)
    if not allocated_set or not linked_ids or not all(str(x) in nodes_db for x in linked_ids):
        return None
    return sum(1 for x in linked_ids if x in allocated_set)


def _audit_single_override(
    nid: int,
    ov: dict[str, Any],
    *,
    raw_base: dict[str, Any] | None,
    dyn: dict[str, Any] | None,
    catalog: dict[str, dict[str, Any]],
    allocated_set: set[int],
    nodes_db: dict[str, Any],
    also_conquered: bool,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Validate one tattoo/runegraft override; returns `(kind, catalog_rec, violation_records)`."""
    ov_name = strip_pob_escapes(str(ov.get("name") or ov.get("nameZh") or ov.get("id") or "Tattoo")).strip()
    rec = _lookup_catalog(catalog, ov)
    raw_bucket = _classify_raw_base_bucket(raw_base) if isinstance(raw_base, dict) else None
    kind = _override_tattoo_kind(ov, raw_bucket or "")
    if isinstance(rec, dict) and rec.get("kind") in ("tattoo", "runegraft"):
        kind = str(rec["kind"])
    elif ov_name.lower().startswith("runegraft ") or "符文" in str(ov.get("nameZh") or ""):
        kind = "runegraft"
    else:
        kind = "tattoo"

    violations: list[dict[str, Any]] = []
    if also_conquered:
        violations.append({"nodeId": nid, "tattoo": ov_name, "reason": "conquered by a Timeless Jewel"})

    is_cluster = bool(
        (isinstance(dyn, dict) and (dyn.get("expansion") or dyn.get("expansionSkill")))
        or (isinstance(raw_base, dict) and (raw_base.get("expansion") or raw_base.get("expansionSkill")))
    )
    if is_cluster:
        violations.append({"nodeId": nid, "tattoo": ov_name, "reason": "cluster jewel expansionSkill node cannot take tattoos"})
        return kind, rec, violations

    if not isinstance(raw_base, dict) or not raw_base:
        return kind, rec, violations

    if raw_base.get("asc") or raw_base.get("ascendancyName"):
        violations.append({"nodeId": nid, "tattoo": ov_name, "reason": "Ascendancy node cannot take tattoos"})
        return kind, rec, violations
    if raw_base.get("proxy") or raw_base.get("blighted") or raw_base.get("isBlighted"):
        violations.append({"nodeId": nid, "tattoo": ov_name, "reason": "proxy/anoint-only node cannot take tattoos"})
        return kind, rec, violations

    base_disp = classify_base_target_type(raw_base) or str(raw_base.get("type") or "Unknown")
    if isinstance(rec, dict):
        allowed = _matching_bucket_keys(str(rec.get("target_type") or ""), str(rec.get("target_value") or ""))
        if allowed and raw_bucket not in allowed:
            req_lbl = str(rec.get("target_type") or "")
            if rec.get("target_value"):
                req_lbl = f"{req_lbl}: {rec['target_value']}"
            violations.append(
                {
                    "nodeId": nid,
                    "tattoo": ov_name,
                    "reason": f"base node is {base_disp} ({raw_base.get('name') or nid}), but '{ov_name}' requires {req_lbl}",
                }
            )
        min_c = int(rec.get("min_connected") or 0)
        max_c = int(rec.get("max_connected") if rec.get("max_connected") is not None else 100)
        if min_c > 0 or max_c < 100:
            alloc_adj = count_allocated_adjacent(raw_base, allocated_set, nodes_db)
            if alloc_adj is not None:
                if min_c > 0 and alloc_adj < min_c:
                    violations.append(
                        {
                            "nodeId": nid,
                            "tattoo": ov_name,
                            "reason": (
                                f"requires MinimumConnected >= {min_c} allocated adjacent passives, "
                                f"but node {nid} has only {alloc_adj} allocated adjacent passive(s)"
                            ),
                        }
                    )
                if max_c < 100 and alloc_adj > max_c:
                    violations.append(
                        {
                            "nodeId": nid,
                            "tattoo": ov_name,
                            "reason": (
                                f"requires MaximumConnected <= {max_c} allocated adjacent passive(s), "
                                f"but node {nid} has {alloc_adj} allocated adjacent passive(s)"
                            ),
                        }
                    )
    else:
        if kind == "runegraft" and raw_bucket != BUCKET_MASTERY:
            violations.append(
                {
                    "nodeId": nid,
                    "tattoo": ov_name,
                    "reason": f"base node is {base_disp}, but Runegraft '{ov_name}' requires Mastery",
                }
            )
        elif kind == "tattoo" and (raw_bucket is None or raw_bucket == BUCKET_MASTERY):
            violations.append(
                {
                    "nodeId": nid,
                    "tattoo": ov_name,
                    "reason": f"base node is {base_disp}, which cannot take tattoo '{ov_name}'",
                }
            )
    return kind, rec, violations


def audit_illegal_tattoo_overrides(
    tree_state: dict[str, Any] | None,
    nodes_db: dict[str, Any] | None,
    *,
    tattoo_catalog: dict[str, dict[str, Any]] | None = None,
    worker: Any = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Statically audit active passive tree `<Override>` / `hashOverrides` tattoo legality."""
    if not isinstance(tree_state, dict) or tree_state.get("_error"):
        return [], []
    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    hash_overrides = tree_state.get("hashOverrides") if isinstance(tree_state.get("hashOverrides"), dict) else {}
    if not overrides and not hash_overrides:
        return [], []

    ndb = nodes_db if isinstance(nodes_db, dict) else {}
    dyn_map = {
        str(n["id"]): n
        for n in (tree_state.get("dynamicNodes") or ())
        if isinstance(n, dict) and "id" in n
    }
    allocated_set: set[int] = set()
    for raw_id in tree_state.get("allocatedNodes") or ():
        try:
            allocated_set.add(int(raw_id))
        except (TypeError, ValueError):
            continue

    catalog = tattoo_catalog if tattoo_catalog is not None else load_tattoo_catalog(worker)
    all_sids = sorted({str(k) for k in overrides} | {str(k) for k in hash_overrides}, key=lambda s: int(s) if s.isdigit() else 0)

    violations: list[dict[str, Any]] = []
    tattoo_count = 0
    limit_counts: dict[str, tuple[int, list[int], str]] = {}

    for sid in all_sids:
        try:
            nid = int(sid)
        except ValueError:
            continue
        ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else None
        hov = hash_overrides.get(sid) if isinstance(hash_overrides.get(sid), dict) else None
        also_conquered = bool(ov and ov.get("why") == "conquered" and hov is not None)
        eff_ov = hov if (also_conquered and hov is not None) else (ov or hov)
        if not isinstance(eff_ov, dict):
            continue
        why = str(eff_ov.get("why") or "").strip().lower()
        name_l = strip_pob_escapes(str(eff_ov.get("name") or "")).strip().lower()
        name_zh = str(eff_ov.get("nameZh") or "")
        is_rg_name = name_l.startswith("runegraft ") or "符文" in name_zh
        is_tat_name = "tattoo" in name_l or "文身" in name_zh or "纹身" in name_zh
        if why == "conquered" and not also_conquered:
            continue
        if why == "mastery" and not is_rg_name and hov is None:
            continue
        if hov is None and why not in ("tattoo", "mastery") and not (is_rg_name or is_tat_name):
            continue
        if allocated_set and nid not in allocated_set and hov is None:
            continue

        raw_base = ndb.get(sid) if isinstance(ndb.get(sid), dict) else None
        kind, rec, node_viols = _audit_single_override(
            nid,
            eff_ov,
            raw_base=raw_base,
            dyn=dyn_map.get(sid),
            catalog=catalog,
            allocated_set=allocated_set,
            nodes_db=ndb,
            also_conquered=also_conquered,
        )
        violations.extend(node_viols)
        if kind != "runegraft":
            tattoo_count += 1
        lim = _parse_limit_rule(rec, eff_ov)
        if lim is not None:
            cap, grp = lim
            entry = limit_counts.get(grp)
            if entry is None:
                limit_counts[grp] = (cap, [nid], str(eff_ov.get("name") or grp))
            else:
                entry[1].append(nid)
                limit_counts[grp] = (min(entry[0], cap), entry[1], entry[2])

    for grp, (cap, nids, lbl) in sorted(limit_counts.items()):
        if len(nids) > cap:
            violations.append(
                {
                    "nodeId": nids[0],
                    "nodeIds": nids,
                    "tattoo": lbl,
                    "reason": f"exceeds 'Limited to {cap} {grp}' ({len(nids)} active on nodes {nids})",
                }
            )
    if tattoo_count > TATTOO_MAX_LIMIT:
        violations.append(
            {
                "nodeId": None,
                "tattoo": "Total Tattoos",
                "reason": f"total non-runegraft tattoos ({tattoo_count}) exceeds PoE1 cap of {TATTOO_MAX_LIMIT}",
            }
        )

    findings = [
        {
            "category": "IllegalTattoo",
            "severity": "HIGH",
            "message": (
                f"[ILLEGAL-TATTOO-NODE] Node {v['nodeId']} ({v['tattoo']}): {v['reason']}."
                if v.get("nodeId") is not None
                else f"[ILLEGAL-TATTOO-NODE] {v['tattoo']}: {v['reason']}."
            ),
        }
        for v in violations
    ]
    return findings, violations


def audit_tattoo_pitfalls(
    *,
    cfg_res: dict[str, Any] | None = None,
    tree_state: dict[str, Any] | None = None,
    nodes_db: dict[str, Any] | None = None,
    worker: Any = None,
    tattoo_catalog: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Run both customMods tattoo-bypass detection and static XML `<Override>` legality checks."""
    gaps: list[dict[str, str]] = check_custom_mods_tattoo_bypass(cfg_res)

    eff_ts = tree_state
    eff_ndb = nodes_db
    if eff_ts is None and worker is not None:
        try:
            ts = worker.call("get_tree_state", {})
            if isinstance(ts, dict) and not ts.get("_error"):
                eff_ts = ts
        except Exception:
            eff_ts = None
    if eff_ndb is None and isinstance(eff_ts, dict) and worker is not None and hasattr(worker, "get_cached_tree_data"):
        try:
            td = worker.get_cached_tree_data(eff_ts.get("treeVersion"))
            if isinstance(td, dict) and isinstance(td.get("nodes"), dict):
                eff_ndb = td["nodes"]
        except Exception:
            eff_ndb = None

    override_gaps, illegal_records = audit_illegal_tattoo_overrides(
        eff_ts,
        eff_ndb,
        tattoo_catalog=tattoo_catalog,
        worker=worker,
    )
    gaps.extend(override_gaps)
    info = {"illegalTattoos": illegal_records} if illegal_records else None
    return gaps, info
