"""Query resolution, pre-flight legality validation, and `[REQ-DEFICIT]` guards for PoE1 Tattoos."""

from __future__ import annotations

from typing import Any

from ..errors import DaemonRequestError
from ..formatters import strip_pob_escapes
from ..game_data import fold_han
from ..game_data.tattoos import _zh_tattoo_variants
from ..snapshot import _finite_float

_BUCKET_READABLE: dict[str, str] = {
    "Small Strength": "Small Strength (+10 Str)",
    "Small Dexterity": "Small Dexterity (+10 Dex)",
    "Small Intelligence": "Small Intelligence (+10 Int)",
    "Small Attribute": "Small Attribute (+10 Str/Dex/Int)",
    "Notable: +30 to Strength": "Notable (+30 to Strength)",
    "Notable: +30 to Dexterity": "Notable (+30 to Dexterity)",
    "Notable: +30 to Intelligence": "Notable (+30 to Intelligence)",
    "Mastery": "Mastery",
    "Keystone": "Keystone",
}


def _format_slot_inventory_hint(slots: dict[str, Any]) -> str:
    buckets = slots.get("buckets") if isinstance(slots.get("buckets"), dict) else {}

    def _part(label: str, key: str) -> str:
        b = buckets.get(key) if isinstance(buckets.get(key), dict) else {}
        f_n, t_n = int(b.get("free") or 0), int(b.get("tattooed") or 0)
        return f"{label} {f_n} free ({t_n} tattooed)" if t_n else f"{label} {f_n} free"

    parts = [
        _part("Small Dex", "Small Dexterity"),
        _part("Small Int", "Small Intelligence"),
        _part("Small Str", "Small Strength"),
    ]
    not_free = sum(
        int((buckets.get(k) or {}).get("free") or 0)
        for k in ("Notable: +30 to Strength", "Notable: +30 to Dexterity", "Notable: +30 to Intelligence")
    )
    not_tat = sum(
        int((buckets.get(k) or {}).get("tattooed") or 0)
        for k in ("Notable: +30 to Strength", "Notable: +30 to Dexterity", "Notable: +30 to Intelligence")
    )
    if not_free or not_tat:
        parts.append(f"+30 Notable {not_free} free ({not_tat} tattooed)" if not_tat else f"+30 Notable {not_free} free")
    return f"Current tree slots: {', '.join(parts)}."


def _format_target_desc(target_type: str, target_value: str) -> str:
    if target_type == "Notable" and target_value:
        return _BUCKET_READABLE.get(f"Notable: {target_value}", f"Notable ({target_value})")
    return _BUCKET_READABLE.get(target_type, target_type or "Unknown")


def _resolve_tattoo_query(
    query: str,
    catalog: dict[str, dict[str, Any]],
    *,
    live_options: list[dict[str, Any]] | None = None,
    show_legacy: bool = False,
) -> dict[str, Any]:
    q_clean = strip_pob_escapes(str(query or "")).strip()
    if not q_clean:
        raise DaemonRequestError("Tattoo query cannot be empty.")
    q_low = q_clean.lower()
    q_variants = {fold_han(v.lower()) for v in _zh_tattoo_variants(q_clean) if v}

    unique_recs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for rec in catalog.values():
        nm = str(rec.get("name") or "")
        if nm and nm not in seen_names:
            seen_names.add(nm)
            unique_recs.append(rec)

    def _match_list(recs: list[dict[str, Any]], *, allow_legacy: bool) -> list[tuple[int, dict[str, Any]]]:
        hits: list[tuple[int, dict[str, Any]]] = []
        for rec in recs:
            if not allow_legacy and rec.get("legacy"):
                continue
            nm = str(rec.get("name") or "")
            rid = str(rec.get("id") or "")
            zh = str(rec.get("zh_name") or "")
            nm_l, rid_l = nm.lower(), rid.lower()
            zh_vars = {fold_han(v.lower()) for v in _zh_tattoo_variants(zh) if v} if zh else set()
            if q_low in (nm_l, rid_l) or (q_variants & zh_vars):
                hits.append((0, rec))
            elif nm_l.startswith(q_low) or any(any(zv.startswith(qv) for zv in zh_vars) for qv in q_variants):
                hits.append((1, rec))
            elif q_low in nm_l or q_low in rid_l or any(any(qv in zv for zv in zh_vars) for qv in q_variants):
                hits.append((2, rec))
        hits.sort(key=lambda pair: (pair[0], len(str(pair[1].get("name") or "")), str(pair[1].get("name") or "")))
        return hits

    hits = _match_list(unique_recs, allow_legacy=show_legacy)
    if not hits and not show_legacy:
        hits = _match_list(unique_recs, allow_legacy=True)
    if hits:
        best_tier = hits[0][0]
        top_hits = [r for t, r in hits if t == best_tier]
        if best_tier == 0 or len(top_hits) == 1:
            return top_hits[0]
        choices = ", ".join(str(r.get("name")) for r in top_hits[:5])
        raise DaemonRequestError(f"Ambiguous tattoo '{q_clean}' matched multiple entries: {choices}. Pass exact name.")

    live_hits: list[tuple[int, dict[str, Any]]] = []
    for opt in live_options or ():
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id") or "")
        onm = str(opt.get("name") or oid)
        ozh = str(opt.get("nameZh") or "")
        oid_l, onm_l = oid.lower(), onm.lower()
        ozh_vars = {fold_han(v.lower()) for v in _zh_tattoo_variants(ozh) if v} if ozh else set()
        tier: int | None = None
        if q_low in (oid_l, onm_l) or (q_variants & ozh_vars):
            tier = 0
        elif onm_l.startswith(q_low) or oid_l.startswith(q_low) or any(
            any(zv.startswith(qv) for zv in ozh_vars) for qv in q_variants
        ):
            tier = 1
        elif q_low in onm_l or q_low in oid_l or any(any(qv in zv for zv in ozh_vars) for qv in q_variants):
            tier = 2
        if tier is not None:
            live_hits.append(
                (
                    tier,
                    {
                        "id": oid,
                        "name": oid or onm,
                        "zh_name": ozh or None,
                        "kind": "runegraft" if (onm_l.startswith("runegraft ") or "符文" in ozh) else "tattoo",
                        "target_type": "",
                        "target_value": "",
                        "min_connected": 0,
                        "max_connected": 100,
                        "limit_text": None,
                        "full_stats": list(opt.get("lines") or ()),
                        "legacy": False,
                    },
                )
            )
    if live_hits:
        live_hits.sort(key=lambda pair: (pair[0], len(str(pair[1].get("name") or "")), str(pair[1].get("name") or "")))
        best_tier = live_hits[0][0]
        top_hits = [r for t, r in live_hits if t == best_tier]
        if best_tier == 0 or len(top_hits) == 1:
            return top_hits[0]
        choices = ", ".join(str(r.get("name")) for r in top_hits[:5])
        raise DaemonRequestError(f"Ambiguous tattoo '{q_clean}' matched multiple entries: {choices}. Pass exact name.")

    raise DaemonRequestError(
        f"Unknown tattoo/runegraft '{q_clean}'. Search available entries with `pob data tattoo <query> --for-build`."
    )


def _validate_tattoo_preflight(
    nid: int,
    raw_base: dict[str, Any] | None,
    dyn: dict[str, Any] | None,
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
    tattoo_rec: dict[str, Any],
    *,
    tattoo_slots: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> list[str]:
    from ..analyzer.pitfalls_tattoos import _lookup_catalog, _parse_limit_rule, count_allocated_adjacent
    from ..analyzer.tattoo_slots import (
        TATTOO_MAX_LIMIT,
        _classify_raw_base_bucket,
        _matching_bucket_keys,
        _override_tattoo_kind,
    )
    from ..analyzer.tree_helpers import classify_base_target_type

    inv_hint = _format_slot_inventory_hint(tattoo_slots)
    t_name = str(tattoo_rec.get("name") or tattoo_rec.get("id") or "Tattoo")
    allocated_set = {int(x) for x in (tree_state.get("allocatedNodes") or ()) if str(x).lstrip("-").isdigit()}
    if nid not in allocated_set:
        raise DaemonRequestError(
            f"Node {nid} is not allocated on the current passive tree; allocate it first before applying '{t_name}'. {inv_hint}"
        )

    is_cluster = bool(
        (isinstance(dyn, dict) and (dyn.get("expansion") or dyn.get("expansionSkill")))
        or (isinstance(raw_base, dict) and (raw_base.get("expansion") or raw_base.get("expansionSkill")))
    )
    if is_cluster:
        raise DaemonRequestError(
            f"Node {nid} is a cluster jewel (expansionSkill) node and cannot take '{t_name}'. {inv_hint}"
        )
    if not isinstance(raw_base, dict) or not raw_base:
        raise DaemonRequestError(f"Node {nid} has no static passive tree definition and cannot take '{t_name}'. {inv_hint}")

    if raw_base.get("asc") or raw_base.get("ascendancyName"):
        raise DaemonRequestError(f"Node {nid} is an Ascendancy node and cannot take '{t_name}'. {inv_hint}")
    if raw_base.get("proxy") or raw_base.get("blighted") or raw_base.get("isBlighted"):
        raise DaemonRequestError(f"Node {nid} is a proxy/anoint-only node and cannot take '{t_name}'. {inv_hint}")

    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    cur_ov = overrides.get(str(nid)) if isinstance(overrides.get(str(nid)), dict) else None
    if cur_ov and str(cur_ov.get("why") or "").lower() == "conquered":
        raise DaemonRequestError(
            f"Node {nid} is conquered by a Timeless Jewel and cannot take '{t_name}'. {inv_hint}"
        )

    raw_bucket = _classify_raw_base_bucket(raw_base)
    base_type = classify_base_target_type(raw_base) or str(raw_base.get("type") or "Unknown")
    base_desc = _BUCKET_READABLE.get(raw_bucket or "", base_type)
    if raw_bucket is None and base_type == "Small Attribute":
        base_desc = "Small Attribute (hybrid attribute node; ineligible for tattoos)"

    t_type = str(tattoo_rec.get("target_type") or "")
    t_val = str(tattoo_rec.get("target_value") or "")
    allowed = _matching_bucket_keys(t_type, t_val)
    if allowed and raw_bucket not in allowed:
        req_desc = _format_target_desc(t_type, t_val)
        raise DaemonRequestError(
            f"Node {nid} base is {base_desc}, but '{t_name}' requires {req_desc}. {inv_hint}"
        )
    if not allowed and raw_bucket is None:
        raise DaemonRequestError(f"Node {nid} base is {base_desc} and cannot take '{t_name}'. {inv_hint}")

    warnings_out: list[str] = []
    min_c = int(tattoo_rec.get("min_connected") or 0)
    max_c = int(tattoo_rec.get("max_connected") if tattoo_rec.get("max_connected") is not None else 100)
    if min_c > 0 or max_c < 100:
        alloc_adj = count_allocated_adjacent(raw_base, allocated_set, nodes_db)
        if alloc_adj is None:
            warnings_out.append(
                f"[UNVERIFIED-CONNECTIVITY] Node {nid}: passive graph incomplete; "
                f"could not verify MinimumConnected/MaximumConnected for '{t_name}'."
            )
        else:
            if min_c > 0 and alloc_adj < min_c:
                raise DaemonRequestError(
                    f"Node {nid} has {alloc_adj} allocated adjacent passive(s), but '{t_name}' requires MinimumConnected >= {min_c}. {inv_hint}"
                )
            if max_c < 100 and alloc_adj > max_c:
                raise DaemonRequestError(
                    f"Node {nid} has {alloc_adj} allocated adjacent passive(s), but '{t_name}' requires MaximumConnected <= {max_c}. {inv_hint}"
                )

    target_limit = _parse_limit_rule(tattoo_rec, {})
    target_kind = str(tattoo_rec.get("kind") or "tattoo")
    other_tattoos = 0
    same_limit_nodes: list[int] = []
    for sid, ov in overrides.items():
        if not isinstance(ov, dict) or not sid.isdigit():
            continue
        oid = int(sid)
        if oid == nid or oid not in allocated_set:
            continue
        ob = _classify_raw_base_bucket(nodes_db.get(sid)) if isinstance(nodes_db.get(sid), dict) else None
        okind = _override_tattoo_kind(ov, ob or "")
        if okind not in ("tattoo", "runegraft"):
            continue
        if okind == "tattoo":
            other_tattoos += 1
        if target_limit is not None:
            orec = _lookup_catalog(catalog, ov)
            olim = _parse_limit_rule(orec, ov)
            if olim is not None and olim[1] == target_limit[1]:
                same_limit_nodes.append(oid)

    if target_limit is not None and len(same_limit_nodes) >= target_limit[0]:
        raise DaemonRequestError(
            f"Cannot apply '{t_name}' on node {nid}: exceeds 'Limited to {target_limit[0]} {target_limit[1]}' "
            f"(already active on node(s) {sorted(same_limit_nodes)}). {inv_hint}"
        )
    if target_kind != "runegraft" and other_tattoos >= TATTOO_MAX_LIMIT:
        raise DaemonRequestError(
            f"Cannot apply '{t_name}' on node {nid}: build already uses {other_tattoos}/{TATTOO_MAX_LIMIT} "
            f"non-runegraft tattoos. Clear or overwrite an existing tattoo first. {inv_hint}"
        )
    return warnings_out


def _collect_req_deficits(before_stats: dict[str, Any], after_stats: dict[str, Any]) -> list[dict[str, Any]]:
    deficits: list[dict[str, Any]] = []
    for attr in ("Str", "Dex", "Int", "Omni"):
        b_cur = _finite_float(before_stats.get(attr), default=0.0)
        b_req = _finite_float(before_stats.get(f"Req{attr}"), default=0.0)
        a_cur = _finite_float(after_stats.get(attr), default=0.0)
        a_req = _finite_float(after_stats.get(f"Req{attr}"), default=0.0)
        if a_req > a_cur:
            b_def = max(0.0, b_req - b_cur)
            a_def = a_req - a_cur
            deficits.append(
                {
                    "attribute": attr,
                    "before": round(b_cur),
                    "after": round(a_cur),
                    "required": round(a_req),
                    "deficit": round(a_def),
                    "newlyBricked": b_def <= 0.0,
                    "worsened": a_def > b_def,
                }
            )
    return deficits


def _attach_req_deficits(
    detail: dict[str, Any],
    before_stats: dict[str, Any],
    after_stats: dict[str, Any],
    *,
    strict_req: bool,
    hint_flag: str = "`--strict-req`",
) -> None:
    req_deficits = _collect_req_deficits(before_stats, after_stats)
    req_warnings = [
        f"[REQ-DEFICIT] {d['attribute']} dropped {d['before']} -> {d['after']} "
        f"(requires {d['required']}, short by {d['deficit']})."
        for d in req_deficits
        if d["newlyBricked"] or d["worsened"]
    ]
    if req_deficits:
        detail["reqDeficits"] = req_deficits
    if req_warnings:
        detail["reqWarnings"] = req_warnings
        warns = list(detail.get("warnings") or ())
        for w_txt in req_warnings:
            if w_txt not in warns:
                warns.append(w_txt)
        detail["warnings"] = warns
        if strict_req:
            raise DaemonRequestError(
                " ".join(req_warnings) + f" Re-run without {hint_flag} to preview or restore attributes first."
            )
