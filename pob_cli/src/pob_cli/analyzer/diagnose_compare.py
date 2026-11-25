"""Dual-build A/B diagnosis (`diagnose_compare_builds`) and diagnosis enrichment helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data.fact_cards import normalize_stat_lines
from ..snapshot import BuildTransaction, history_of
from .constants import _ITEM_SEL_VARIANT_RE, _MOD_VARIANT_RE, _PANTHEON_HINTS

_POB_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_POB_TRAILING_STATE_RE = re.compile(
    r"^(?:Corrupted|Mirrored|Unidentified|Split|Synthesised Item|Fractured Item|Searing Exarch Item|"
    r"Eater of Worlds Item|Elder Item|Shaper Item|Crusader Item|Redeemer Item|Hunter Item|Warlord Item|"
    r"Relic Unique|Foil Unique\b.*|Note:\s*.*)$",
    re.IGNORECASE,
)


def _filter_variant_lines(lines: list[str], sel_var: int | None) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or _POB_TRAILING_STATE_RE.match(s):
            continue
        if sel_var is not None:
            mv = _MOD_VARIANT_RE.search(s)
            if mv:
                allowed = {int(x) for x in mv.group(1).split(",") if x.isdigit()}
                if allowed and sel_var not in allowed:
                    continue
        out.append(s)
    return normalize_stat_lines(out)


def extract_equipped_unique_overrides(items_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract real parsed `implicits` & `explicits` from equipped unique items in `inspect_items(all_raw=True)`."""
    from ..trade_item.parser import parse_item_text

    overrides: list[dict[str, Any]] = []
    for s in items_info.get("equippedSlots") or []:
        if not isinstance(s, dict) or "Swap" in str(s.get("slotName") or ""):
            continue
        it = s.get("item")
        if not isinstance(it, dict):
            continue
        rarity = str(it.get("rarity") or "").upper()
        uname = strip_pob_escapes(str(it.get("title") or it.get("name") or "")).strip()
        if rarity not in ("UNIQUE", "RELIC") or not uname:
            continue
        uname_zh = strip_pob_escapes(str(it.get("titleZh") or it.get("nameZh") or "")).strip() or None
        base_nm = strip_pob_escapes(str(it.get("baseName") or "")).strip() or None
        implicits: list[str] = []
        explicits: list[str] = []

        if isinstance(it.get("implicits"), list) or isinstance(it.get("explicits"), list):
            implicits = normalize_stat_lines(it.get("implicits") or [])
            explicits = normalize_stat_lines(it.get("explicits") or [])
        else:
            raw_text = str(s.get("raw") or it.get("raw") or "").strip()
            if raw_text:
                m_sel = _ITEM_SEL_VARIANT_RE.search(raw_text)
                sel_var = int(m_sel.group(1)) if m_sel else None
                raw_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
                imp_idx = next(
                    (idx for idx, ln in enumerate(raw_lines) if _POB_IMPLICITS_HDR_RE.match(ln)),
                    None,
                )
                if imp_idx is not None:
                    m_imp = _POB_IMPLICITS_HDR_RE.match(raw_lines[imp_idx])
                    n_imp = int(m_imp.group(1)) if m_imp else 0
                    implicits = _filter_variant_lines(raw_lines[imp_idx + 1 : imp_idx + 1 + n_imp], sel_var)
                    explicits = _filter_variant_lines(raw_lines[imp_idx + 1 + n_imp :], sel_var)
                elif any(ln.startswith("--------") for ln in raw_lines):
                    try:
                        parsed = parse_item_text(raw_text)
                        implicits = normalize_stat_lines(m.text for m in parsed.implicits)
                        explicits = normalize_stat_lines(m.text for m in parsed.explicits)
                    except Exception:
                        pass

        if implicits or explicits:
            overrides.append(
                {
                    "name": uname,
                    "zhName": uname_zh,
                    "baseType": base_nm,
                    "implicits": implicits,
                    "explicits": explicits,
                }
            )
    return overrides


def deduplicate_diagnose_advisories(
    advisories: list[dict[str, Any]],
    *,
    core_synergies: list[str],
    build_mechanics: dict[str, Any] | None = None,
    health_gaps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Suppress generic patch trap advisories already explained in Section 2/2b/2c."""
    syn_blob = " ".join(str(x) for x in (core_synergies or [])).lower()
    gap_blob = " ".join(
        str(g.get("message") or "") for g in (health_gaps or []) if isinstance(g, dict)
    ).lower()
    bm = build_mechanics or {}
    has_soa = isinstance(bm.get("swordOfAges"), dict)
    has_cwc_bm = any(
        isinstance(t, dict) and t.get("kind") == "fixed_interval_channelling"
        for t in (bm.get("triggers") or [])
    )

    out: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for adv in advisories or []:
        if not isinstance(adv, dict):
            continue
        rid = str(adv.get("ruleId") or "").strip().upper()
        trig = str(adv.get("trigger") or "").strip().lower()
        dedup_key = rid or f"{adv.get('tag')}:{trig}"
        if dedup_key in seen_rules:
            continue
        if rid == "PATCH_330_AGNOSTIC_CWS_SYNERGY" and ("[do not unspec]" in syn_blob or "the agnostic" in syn_blob):
            continue
        if rid == "PATCH_330_RAMAKO_FIXED_DOT_RES" and "ramako" in syn_blob:
            continue
        if rid == "PATCH_330_DORYANI_PROTOTYPE_NEG_RES" and (
            "doryani's prototype" in syn_blob or "doryani's prototype" in gap_blob
        ):
            continue
        if rid == "PATCH_330_SWORD_OF_AGES_TEMPORAL_MIRAGE" and has_soa:
            continue
        if rid == "PATCH_330_CWC_NOT_COOLDOWN" and has_cwc_bm:
            continue
        if rid == "PATCH_330_LOW_LIFE_ESH_VISAGE" and "esh's visage" in gap_blob:
            continue
        if ("frost bomb" in trig or "寒霜爆" in trig or "FROST_BOMB" in rid) and "frost bomb" in syn_blob:
            continue
        seen_rules.add(dedup_key)
        out.append(adv)
    return out


def _merge_traps_and_ask_grounding(
    *,
    scan_corpus: str,
    ask: str | None,
    fact_cards: dict[str, Any],
    pob_dir: Any,
    runtime_dir: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    from ..game_data import ground_text_entities
    from ..guardrails import scan_text_traps

    trap_scan = scan_text_traps(scan_corpus)
    advisories_merged: list[dict[str, Any]] = []
    seen_adv_rules: set[str] = set()
    for w in trap_scan.get("warnings") or []:
        rid = str(w.get("ruleId") or w.get("trigger") or "")
        if rid and rid in seen_adv_rules:
            continue
        if rid:
            seen_adv_rules.add(rid)
        advisories_merged.append(w)

    ask_grounding: dict[str, Any] | None = None
    ask_clean = str(ask or "").strip()
    if ask_clean:
        try:
            g_ask = ground_text_entities(ask_clean, limit=8, pob_dir=pob_dir, runtime_dir=runtime_dir)
            for w in g_ask.get("warnings") or []:
                rid = str(w.get("ruleId") or w.get("trigger") or "")
                if rid and rid in seen_adv_rules:
                    continue
                if rid:
                    seen_adv_rules.add(rid)
                advisories_merged.append(w)
            gem_cards = list(fact_cards.get("gems") or [])
            uniq_cards = list(fact_cards.get("uniques") or [])
            seen_gem_names = {str(c.get("name") or "").lower() for c in gem_cards}
            seen_uniq_names = {str(c.get("name") or "").lower() for c in uniq_cards}
            added_entities: list[str] = []
            for ent in g_ask.get("entities") or []:
                enm = str(ent.get("name") or "")
                elow = enm.lower()
                if ent.get("kind") == "gem" and elow and elow not in seen_gem_names:
                    seen_gem_names.add(elow)
                    gem_cards.append(ent)
                    added_entities.append(enm)
                elif ent.get("kind") == "unique" and elow and elow not in seen_uniq_names:
                    seen_uniq_names.add(elow)
                    uniq_cards.append(ent)
                    added_entities.append(enm)
            fact_cards["gems"] = gem_cards
            fact_cards["uniques"] = uniq_cards
            ask_grounding = {
                "query": ask_clean,
                "resolvedSlang": g_ask.get("resolvedSlang") or [],
                "hasAlerts": bool(g_ask.get("hasAlerts")),
                "addedEntities": added_entities,
                "elapsedMs": g_ask.get("elapsedMs"),
            }
        except Exception:
            ask_grounding = {"query": ask_clean, "resolvedSlang": [], "hasAlerts": False, "addedEntities": []}
    return advisories_merged, ask_grounding


def _extract_pantheon_info(cfg_res_diag: dict[str, Any]) -> dict[str, str]:
    pantheon_info: dict[str, str] = {}
    cfg_var_entries: list[dict[str, Any]] = []
    if isinstance(cfg_res_diag, dict):
        for sec in cfg_res_diag.get("sections") or []:
            if isinstance(sec, dict):
                cfg_var_entries.extend(v for v in (sec.get("items") or sec.get("vars") or []) if isinstance(v, dict))
        cfg_var_entries.extend(v for v in (cfg_res_diag.get("config") or []) if isinstance(v, dict))
    for ci in cfg_var_entries:
        vr = str(ci.get("var") or "")
        vl = str(ci.get("value") or "").strip()
        if vr == "pantheonMajorGod" and vl and vl.lower() != "none":
            pantheon_info["major"] = vl
            if vl in _PANTHEON_HINTS:
                pantheon_info["majorHint"] = _PANTHEON_HINTS[vl]
        elif vr == "pantheonMinorGod" and vl and vl.lower() != "none":
            pantheon_info["minor"] = vl
            if vl in _PANTHEON_HINTS:
                pantheon_info["minorHint"] = _PANTHEON_HINTS[vl]
    return pantheon_info


def _run_diagnose_rankings(
    worker: BridgeWorker,
    audit: dict[str, Any],
    mg: dict[str, Any],
    *,
    top: int,
    keep: Iterable[str] | str | None = None,
    auto_keep_reasons: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    import pob_cli.analyzer as _analyzer

    from ..comparator import rank_gems, rank_nodes

    top_nodes: dict[str, Any] | None = None
    top_gems: dict[str, Any] | None = None
    rank_errors: list[str] = []
    primary_key = str((audit.get("original") or {}).get("primaryDpsKey") or "CombinedDPS")
    node_stat = "CombinedDPS" if primary_key in ("CombinedDPS", "TotalDPS", "TotalDotDPS", "FullDPS") else primary_key
    gem_field = "FullDPS" if primary_key == "FullDPS" else "CombinedDPS"

    def _exec_rankings(label: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        rn: dict[str, Any] | None = None
        rg: dict[str, Any] | None = None
        try:
            rn_kw = (
                {"baseline_label": label}
                if "baseline_label" in (getattr(rank_nodes, "__kwdefaults__", None) or {})
                else {}
            )
            rn = rank_nodes(worker, stat=node_stat, max_dist=5, top=top, **rn_kw)
            if isinstance(rn, dict) and rn.get("error"):
                rank_errors.append(f"rank_nodes: {rn['error']}")
                rn = None
            elif isinstance(rn, dict) and not rn.get("baselineLabel"):
                rn["baselineLabel"] = label
        except Exception as exc:
            rank_errors.append(f"rank_nodes: {exc}")
            rn = None
        try:
            if mg and (mg.get("gems") or []):
                rg_kw = (
                    {"baseline_label": label}
                    if "baseline_label" in (getattr(rank_gems, "__kwdefaults__", None) or {})
                    else {}
                )
                rg = rank_gems(
                    worker,
                    group_selector="main",
                    slot_idx=None,
                    add_link=True,
                    field=gem_field,
                    top=top,
                    exclude_archetype_swaps=True,
                    **rg_kw,
                )
                if isinstance(rg, dict) and not rg.get("baselineLabel"):
                    rg["baselineLabel"] = label
        except Exception as exc:
            rank_errors.append(f"rank_gems: {exc}")
            rg = None
        return rn, rg

    if audit.get("dewateredPinnacle") is not None:
        try:
            with BuildTransaction(worker):
                dw_defs = getattr(_analyzer.dewater_build_with_records, "__kwdefaults__", None) or {}
                dw_kw = {"auto_keep_reasons": auto_keep_reasons} if "auto_keep_reasons" in dw_defs else {}
                _analyzer.dewater_build_with_records(worker, keep=keep, **dw_kw)
                top_nodes, top_gems = _exec_rankings("Dewatered Pinnacle")
                return top_nodes, top_gems, rank_errors
        except Exception:
            pass

    top_nodes, top_gems = _exec_rankings("Original Panel")
    return top_nodes, top_gems, rank_errors


def _build_diagnose_env(
    worker: BridgeWorker,
    info: dict[str, Any],
    fact_cards: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    hello = getattr(worker, "hello_info", None) or {}
    pob_ver = fact_cards.get("pobVersion")
    if not pob_ver or pob_ver == "unknown":
        pob_ver = hello.get("pobVersion") or "unknown"
    tree_ver = info.get("treeVersion") or "3_30"
    patch_str = str(tree_ver).replace("_", ".") if str(tree_ver).startswith("3_") else "3.30"
    worker_rev = history_of(worker).rev
    env: dict[str, Any] = {
        "game": "poe1",
        "patch": patch_str,
        "pobVersion": pob_ver,
        "dataVersion": fact_cards.get("dataVersion", "unknown"),
        "treeVersion": tree_ver,
        "slot": target,
    }
    if isinstance(worker_rev, int) and not isinstance(worker_rev, bool):
        env["rev"] = worker_rev
    return env


def diagnose_compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    compare_source: str | None = None,
    compare_xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
    keep: Iterable[str] | str | None = None,
    top: int = 5,
    no_rank: bool = True,
    ask: str | None = None,
    pob_dir: Any = None,
    runtime_dir: Any = None,
    target: str = "main",
    ref_target: str = "ref",
) -> dict[str, Any]:
    """One-shot dual-build 3-baseline A/B comparison + concurrent Pinnacle diagnosis of both builds."""
    import pob_cli.analyzer as _analyzer

    from ..comparator import _run_worker_pair, compare_builds
    from ..snapshot import load_build

    def _load_into(
        worker: Any,
        src: str | None,
        code: str | None,
        *,
        c_name: str | None = None,
        c_league: str | None = None,
        c_realm: str | None = None,
    ) -> None:
        if src or code:
            load_build(
                worker,
                source=src,
                xml_code=code,
                char_name=c_name,
                league=c_league,
                realm=c_realm,
            )

    _run_worker_pair(
        main_worker,
        ref_worker,
        lambda: _load_into(
            main_worker,
            source,
            xml_code,
            c_name=char_name,
            c_league=league,
            c_realm=realm,
        ),
        lambda: _load_into(ref_worker, compare_source, compare_xml_code),
    )
    comparison = compare_builds(main_worker, ref_worker)

    def _diag_a() -> dict[str, Any]:
        return _analyzer.diagnose_build(
            main_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=ask,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=target,
        )

    def _diag_b() -> dict[str, Any]:
        return _analyzer.diagnose_build(
            ref_worker,
            keep=keep,
            top=top,
            no_rank=no_rank,
            ask=None,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            target=ref_target,
        )

    diag_a, diag_b = _run_worker_pair(main_worker, ref_worker, _diag_a, _diag_b)
    return {
        "mode": "compare",
        "target": target,
        "refTarget": ref_target,
        "env": diag_a.get("env"),
        "comparison": comparison,
        "buildA": diag_a,
        "buildB": diag_b,
        "askGrounding": diag_a.get("askGrounding"),
    }
