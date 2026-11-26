"""Build-mechanism-aware smart dewatering auto-keep and flask notes (Flagellant, Eldritch Battery flask)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch, _finite_float
from .dewater import dewater_build_in_place
from .eb_sustain import is_eldritch_battery_active as is_eldritch_battery_active
from .eb_sustain import is_replica_sorrow_eb_item
from .summary import _active_item_lines
from .tree_helpers import apply_node_override

_TATTOO_LABEL_TOKENS = ("tattoo", "文身", "纹身", "紋身")  # zh-CN writes 文身, zh-TW 紋身


def _is_tattoo_label(label: str) -> bool:
    low = label.lower()
    return any(tok in low for tok in _TATTOO_LABEL_TOKENS)



def _min_charges_fill_max(stats: dict[str, Any], kind: str) -> bool:
    """True when the build's minimum `kind` charges already equal its maximum.

    PoB counts minimum charges whether or not `use<Kind>Charges` is set, and the setting counts the
    maximum. Keeping it for a build with 1 minimum of 4 would credit 4 guaranteed charges, so a
    minimum only justifies keeping it when there is nothing above it to add.
    """
    lowest = max(_finite_float(stats.get(f"{kind}ChargesMin")), _finite_float(stats.get(f"Minimum{kind}Charges")))
    highest = _finite_float(stats.get(f"{kind}ChargesMax"))
    return 0 < highest <= lowest


_PASSIVE_ENDURANCE_RE = re.compile(
    r"gain\s+an?\s+endurance\s+charge\s+every\b|gain\s+\d+\s+endurance\s+charges?\s+when\s+(?:you\s+are\s+)?hit\b"
    r"|\d+%\s+chance\s+to\s+gain\s+an?\s+endurance\s+charge\s+when\s+you\s+are\s+hit",
    re.IGNORECASE,
)
_FLAGELLANT_RE = re.compile(
    r"flagellant|gain\s+\d+\s+charges?\s+when\s+you\s+are\s+hit|受击时获得.*充能|被击中时获得.*充能",
    re.IGNORECASE,
)


def _collect_build_tokens(
    *,
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    tree_summary: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
    alloc_node_stats: list[tuple[str, list[str]]] | None,
) -> dict[str, Any]:
    enabled_gems_low: list[str] = []
    enabled_skill_ids: set[str] = set()
    for g in (skills_res or {}).get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if not isinstance(gm, dict) or not gm.get("enabled", True):
                continue
            sid = str(gm.get("skillId") or "").strip()
            if sid:
                enabled_skill_ids.add(sid)
            blob = " ".join(strip_pob_escapes(str(gm.get(k) or "")) for k in ("name", "nameSpec", "nameZh")).lower()
            if blob.strip():
                enabled_gems_low.append(blob)

    items_by_id = {
        it["id"]: it for it in ((items_res or {}).get("items") or []) if isinstance(it, dict) and "id" in it
    }
    active_item_names: list[str] = []
    for s in (items_res or {}).get("slots") or []:
        if not isinstance(s, dict) or s.get("inactive") or "Swap" in str(s.get("name") or ""):
            continue
        iid = s.get("selItemId")
        if not iid:
            continue
        it = items_by_id.get(iid) or {}
        nm = " ".join(
            strip_pob_escapes(str(it.get(k) or "")) for k in ("title", "name", "titleZh", "nameZh", "baseName")
        ).lower()
        if nm.strip():
            active_item_names.append(nm)

    raw_by_slot: dict[str, str] = {}
    for entry in equipped_slots_raw or []:
        if isinstance(entry, dict) and entry.get("slotName") and entry.get("raw"):
            raw_by_slot[str(entry["slotName"])] = str(entry["raw"])

    active_mod_lines: list[str] = []
    for sn, rtxt in raw_by_slot.items():
        if "Swap" not in sn:
            active_mod_lines.extend(strip_pob_escapes(ln) for ln in _active_item_lines(rtxt))

    node_names_low: set[str] = set()
    node_ids: set[int] = set()
    node_stat_lines: list[str] = []
    if isinstance(tree_summary, dict):
        for bucket in ("keystones", "ascendancyNodes", "notables"):
            for nd in tree_summary.get(bucket) or []:
                if isinstance(nd, dict):
                    lbls = [
                        strip_pob_escapes(str(nd.get(k) or "")).strip().lower()
                        for k in ("name", "nameZh")
                    ]
                    is_tattoo = str(nd.get("overrideWhy") or "").lower() == "tattoo" or any(
                        _is_tattoo_label(v) for v in lbls if v
                    )
                    if not is_tattoo:
                        for v in lbls:
                            if v:
                                node_names_low.add(v)
                        if nd.get("id") is not None:
                            try:
                                node_ids.add(int(nd["id"]))
                            except (TypeError, ValueError):
                                pass
                    for st_ln in nd.get("stats") or []:
                        node_stat_lines.append(strip_pob_escapes(str(st_ln)))
                elif isinstance(nd, str) and nd.strip():
                    v = strip_pob_escapes(nd).strip().lower()
                    if v and not _is_tattoo_label(v):
                        node_names_low.add(v)
    for n_lbl, st_lines in alloc_node_stats or []:
        nl = strip_pob_escapes(str(n_lbl or "")).strip().lower()
        if nl and not _is_tattoo_label(nl):
            node_names_low.add(nl)
        node_stat_lines.extend(strip_pob_escapes(str(x)) for x in st_lines)

    return {
        "enabled_gems_low": enabled_gems_low,
        "enabled_skill_ids": enabled_skill_ids,
        "items_by_id": items_by_id,
        "active_item_names": active_item_names,
        "raw_by_slot": raw_by_slot,
        "all_mod_lines": active_mod_lines + node_stat_lines,
        "node_names_low": node_names_low,
        "node_ids": node_ids,
    }


def infer_auto_keep_and_flask_notes(
    *,
    stats: dict[str, Any] | None,
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    build_info: dict[str, Any] | None = None,
    tree_summary: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    alloc_node_stats: list[tuple[str, list[str]]] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Infer build-guaranteed config auto-keep reasons and the notes on flasks whose stripping needs a word."""
    st = stats or {}
    tok = _collect_build_tokens(
        skills_res=skills_res,
        items_res=items_res,
        tree_summary=tree_summary,
        equipped_slots_raw=equipped_slots_raw,
        alloc_node_stats=alloc_node_stats,
    )
    node_names = tok["node_names_low"]
    node_ids = tok["node_ids"]
    item_blob = " | ".join(tok["active_item_names"])
    gems_blob = " | ".join(tok["enabled_gems_low"])
    mods_blob = "\n".join(tok["all_mod_lines"])
    mods_low = mods_blob.lower()

    auto_keep: dict[str, str] = {}

    ig_chance = max(
        _finite_float(st.get("IgniteChance")),
        _finite_float(st.get("EnemyIgniteChance")),
    )
    # Every node and item carries its English name, so that is what these checks read. A Chinese
    # fragment is kept only where it is the real zh-CN or zh-TW name and matches nothing else.
    has_shaper_of_flames = any("shaper of flames" in nm for nm in node_names)
    if has_shaper_of_flames or "hits always ignite" in mods_low or "always ignite" in mods_low:
        auto_keep["conditionenemyignited"] = "Build has guaranteed Ignite (Shaper of Flames / always Ignite)"
    elif ig_chance >= 100.0:
        auto_keep["conditionenemyignited"] = f"Main skill IgniteChance {ig_chance:.0f}% >= 100%"

    has_cws = (
        "SupportCastWhenStunned" in tok["enabled_skill_ids"]
        or any(t in gems_blob for t in ("cast when stunned", "晕眩时施放", "暈眩時施放"))
    )
    has_ramako = (61355 in node_ids) or any(
        not _is_tattoo_label(nm)
        and any(t in nm for t in ("ramako, sun's light", "ramako, suns light", "烈陽之光", "太阳之光"))
        for nm in node_names
    )
    has_tukohama = any(
        not _is_tattoo_label(nm) and any(t in nm for t in ("tukohama", "图克哈玛", "圖克哈瑪"))
        for nm in node_names
    )
    has_natures_patience = any("nature's patience" in nm for nm in node_names)
    if has_cws and has_ramako:
        auto_keep["conditionstationary"] = "CwS/Ramako stationary build"
    elif has_cws:
        auto_keep["conditionstationary"] = "Cast when Stunned stationary build"
    elif has_ramako:
        auto_keep["conditionstationary"] = "Chieftain Ramako stationary build"
    elif has_tukohama:
        auto_keep["conditionstationary"] = "Chieftain Tukohama stationary build"
    elif has_natures_patience:
        auto_keep["conditionstationary"] = "Nature's Patience stationary keystone"

    # Bloodnotch is "血痕" in zh-CN, which also opens the base Sanguine Raiment ("血痕之衣").
    has_hittank = has_cws or any(
        t in item_blob for t in ("bloodnotch", "缺血症", "valyrium", "女武神", "戰日", "immutable force")
    )
    has_ralakesh = "ralakesh's impatience" in item_blob
    has_farrul = "farrul's fur" in item_blob
    is_replica_farrul = "replica farrul's fur" in item_blob
    has_conviction = any("conviction of power" in nm for nm in node_names)
    # Chinese names are shared here: zh-CN calls both Inexorable and Juggernaut's Unstoppable
    # "势不可挡", and "不屈" opens Unbending, Indomitable Resolve and two more.
    has_inexorable = any("inexorable" in nm for nm in node_names)
    has_enduring_comp = any("enduring composure" in nm for nm in node_names)
    has_unflinching = any("unflinching" in nm for nm in node_names)

    if _min_charges_fill_max(st, "Endurance"):
        auto_keep["useendurancecharges"] = "Minimum Endurance Charges = maximum"
    elif has_enduring_comp:
        auto_keep["useendurancecharges"] = "Enduring Composure on-hit Endurance sustain"
    elif has_inexorable and has_hittank:
        auto_keep["useendurancecharges"] = "Inexorable + CwS/hit-tank Endurance sustain"
    elif has_conviction:
        auto_keep["useendurancecharges"] = "Hierophant Conviction of Power (+4 Min Endurance Charges)"
    elif has_unflinching:
        auto_keep["useendurancecharges"] = "Juggernaut Unflinching Endurance sustain"
    elif has_ralakesh and ("maximum endurance charges" in mods_low or ("maximum frenzy charges" not in mods_low and "maximum power charges" not in mods_low)):
        auto_keep["useendurancecharges"] = "Ralakesh's Impatience"
    elif is_replica_farrul:
        auto_keep["useendurancecharges"] = "Replica Farrul's Fur Endurance sustain"
    elif _PASSIVE_ENDURANCE_RE.search(mods_blob) and has_hittank:
        auto_keep["useendurancecharges"] = "Passive/on-hit Endurance Charge generation"

    if _min_charges_fill_max(st, "Frenzy"):
        auto_keep["usefrenzycharges"] = "Minimum Frenzy Charges = maximum"
    elif has_farrul:
        auto_keep["usefrenzycharges"] = "Farrul's Fur Aspect of the Cat sustain"
    elif has_ralakesh and ("maximum frenzy charges" in mods_low or ("maximum endurance charges" not in mods_low and "maximum power charges" not in mods_low)):
        auto_keep["usefrenzycharges"] = "Ralakesh's Impatience"

    if _min_charges_fill_max(st, "Power"):
        auto_keep["usepowercharges"] = "Minimum Power Charges = maximum"
    elif has_conviction:
        auto_keep["usepowercharges"] = "Hierophant Conviction of Power (+4 Min Power Charges)"
    elif has_farrul and not is_replica_farrul:
        auto_keep["usepowercharges"] = "Farrul's Fur Aspect of the Cat sustain"
    elif has_ralakesh and ("maximum power charges" in mods_low or ("maximum endurance charges" not in mods_low and "maximum frenzy charges" not in mods_low)):
        auto_keep["usepowercharges"] = "Ralakesh's Impatience"

    flask_notes: dict[str, str] = {}
    if isinstance(items_res, dict):
        items_by_id = tok["items_by_id"]
        raw_by_slot = tok["raw_by_slot"]
        for slot in items_res.get("slots") or []:
            if not isinstance(slot, dict) or not slot.get("isFlask") or not slot.get("active"):
                continue
            sname = str(slot.get("name") or "")
            if not sname:
                continue
            it = items_by_id.get(slot.get("selItemId") or 0) or {}
            raw_slot_txt = raw_by_slot.get(sname, "")
            if is_replica_sorrow_eb_item(it, raw_slot_txt):
                flask_notes[sname] = (
                    f"Eldritch Battery during Effect: stripping disables ES-backed Mana casting; test --keep '{sname}'"
                )
                continue
            if has_hittank:
                f_txt = " ".join(
                    [
                        strip_pob_escapes(str(it.get("title") or "")),
                        strip_pob_escapes(str(it.get("name") or "")),
                        strip_pob_escapes(str(it.get("titleZh") or "")),
                        strip_pob_escapes(str(it.get("raw") or "")),
                        raw_slot_txt,
                    ]
                )
                if _FLAGELLANT_RE.search(f_txt):
                    flask_notes[sname] = f"Flagellant: recharges on Boss hits; test --keep '{sname}'"

    return auto_keep, flask_notes


def best_effort_audit_tree_and_raw(
    worker: BridgeWorker,
    items_res: dict[str, Any],
    *,
    pre_tree_summary: dict[str, Any] | None = None,
    pre_equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, list[tuple[str, list[str]]]]:
    """Read tree node stats and active item raw texts when supported by worker (no-op on bare stubs)."""
    alloc_node_stats: list[tuple[str, list[str]]] = []
    tree_summary = pre_tree_summary
    equipped_slots_raw = pre_equipped_slots_raw
    if not callable(getattr(worker, "get_cached_tree_data", None)):
        return tree_summary, equipped_slots_raw, alloc_node_stats

    if tree_summary is None:
        try:
            t_state = worker.call("get_tree_state", {})
            if isinstance(t_state, dict) and not t_state.get("_error"):
                nodes_db = worker.get_cached_tree_data(t_state.get("treeVersion")).get("nodes", {})
                ov = t_state.get("overrides") or {}
                synth_tree: dict[str, list[dict[str, Any]]] = {
                    "keystones": [],
                    "ascendancyNodes": [],
                    "notables": [],
                }
                for raw_nid in t_state.get("allocatedNodes") or []:
                    nid = int(raw_nid)
                    sid = str(nid)
                    raw_nd = nodes_db.get(sid) or {}
                    nd = dict(raw_nd)
                    if sid in ov and isinstance(ov[sid], dict):
                        # A conquered node with no stats must not keep the lines it had before the jewel.
                        apply_node_override(nd, ov[sid], raw_nd)
                    nd["id"] = nid
                    nm = str(nd.get("name") or sid)
                    st_lines = [str(x) for x in (nd.get("stats") or []) if str(x).strip()]
                    alloc_node_stats.append((nm, st_lines))
                    ntype = nd.get("type")
                    if nd.get("asc") or nd.get("ascendancyName"):
                        if ntype not in ("AscendClassStart", "ClassStart"):
                            synth_tree["ascendancyNodes"].append(nd)
                    elif ntype == "Keystone" or nd.get("isKeystone"):
                        synth_tree["keystones"].append(nd)
                    elif ntype == "Notable":
                        synth_tree["notables"].append(nd)
                from .tree_sockets import enrich_tree_sockets

                dyn_map = {
                    str(n["id"]): n
                    for n in (t_state.get("dynamicNodes") or [])
                    if isinstance(n, dict) and n.get("id") is not None
                }
                alloc_set = {int(x) for x in (t_state.get("allocatedNodes") or [])}
                j_socks, s_sum, near_socks = enrich_tree_sockets(t_state, items_res, nodes_db, dyn_map, alloc_set)
                synth_tree_out: dict[str, Any] = dict(synth_tree)
                synth_tree_out["jewelSockets"] = j_socks
                synth_tree_out["socketSummary"] = s_sum
                synth_tree_out["nearbySockets"] = near_socks
                tree_summary = synth_tree_out
        except Exception:
            pass

    if equipped_slots_raw is None and isinstance(items_res, dict):
        active_slots = [
            (str(s.get("name") or ""), int(s["selItemId"]))
            for s in (items_res.get("slots") or [])
            if isinstance(s, dict)
            and s.get("selItemId")
            and not s.get("inactive")
            and "Swap" not in str(s.get("name") or "")
        ]
        if active_slots:
            try:
                raw_res = _call_batch(
                    worker,
                    [("item_raw", {"id": iid}) for _, iid in active_slots],
                    raise_on_error=False,
                )
                equipped_slots_raw = [
                    {"slotName": sname, "raw": r.get("raw")}
                    for (sname, _), r in zip(active_slots, raw_res, strict=False)
                    if isinstance(r, dict) and not r.get("_error") and r.get("raw")
                ]
            except Exception:
                equipped_slots_raw = None
    return tree_summary, equipped_slots_raw, alloc_node_stats


def dewater_smart_in_place(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    *,
    auto_keep: bool = True,
    stats: dict[str, Any] | None = None,
    skills_res: dict[str, Any] | None = None,
    items_res: dict[str, Any] | None = None,
    build_info: dict[str, Any] | None = None,
    tree_summary: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Run build-mechanism-aware smart dewatering in place (or bare dewatering when `auto_keep=False`)."""
    if not auto_keep:
        return dewater_build_in_place(worker, keep=keep)

    missing_keys: list[str] = []
    missing_calls: list[tuple[str, dict[str, Any]]] = []
    if stats is None:
        missing_keys.append("stats")
        missing_calls.append(("get_stats", {}))
    if skills_res is None:
        missing_keys.append("skills")
        missing_calls.append(("list_skills", {}))
    if items_res is None:
        missing_keys.append("items")
        missing_calls.append(("list_items", {}))
    if build_info is None:
        missing_keys.append("info")
        missing_calls.append(("get_build_info", {}))

    if missing_calls:
        try:
            fetched = _call_batch(worker, missing_calls, raise_on_error=False)
            for mk, mr in zip(missing_keys, fetched, strict=False):
                if not isinstance(mr, dict) or mr.get("_error"):
                    continue
                if mk == "stats":
                    stats = mr.get("stats") if isinstance(mr.get("stats"), dict) else mr
                elif mk == "skills":
                    skills_res = mr
                elif mk == "items":
                    items_res = mr
                elif mk == "info":
                    build_info = mr
        except Exception:
            pass

    eff_tree, eff_raw, alloc_st = best_effort_audit_tree_and_raw(
        worker,
        items_res or {},
        pre_tree_summary=tree_summary,
    )
    ak_reasons, flask_notes = infer_auto_keep_and_flask_notes(
        stats=stats,
        skills_res=skills_res,
        items_res=items_res,
        build_info=build_info,
        tree_summary=eff_tree,
        equipped_slots_raw=eff_raw,
        alloc_node_stats=alloc_st,
    )
    return dewater_build_in_place(
        worker,
        keep=keep,
        auto_keep_reasons=ak_reasons,
        flask_notes=flask_notes,
    )
