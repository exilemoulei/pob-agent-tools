"""Bilingual (EN/ZH) search across PoB CalcsTab (`get_calcs`) and pipelined `calcs_breakdown`."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data import fold_han
from ..snapshot import _call_batch
from .tree_inspect import _normalize_query_list

_CALC_CELL_NUM_RE = re.compile(
    r"^\s*[xX×]?\s*([+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)\s*(?:%|/s|s|sec|ms|x|X|×|m|pts?|秒|次/秒|米)?\s*$"
)
_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_CALC_PUNCT_RE = re.compile(r"[\s_\-\"'`.,:/\\()\[\]{}%+]+")

_CALC_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "regen": ("regeneration", "recovery", "回复"),
    "regeneration": ("regen", "recovery", "回复"),
    "recovery": ("regen", "regeneration", "回复"),
    "es": ("energy shield", "能量护盾"),
    "ehp": ("effective hit pool", "effective health pool", "有效生命"),
    "effective": ("eff", "有效"),
    "eff": ("effective", "有效"),
    "dps": ("damage", "totaldps", "秒伤", "伤害"),
    "dmg": ("damage", "伤害"),
    "avg": ("average", "平均"),
    "average": ("avg", "平均"),
    "crit": ("critical", "crits", "暴击"),
    "critical": ("crit", "crits", "暴击"),
    "mult": ("multiplier", "multi", "加成", "倍率"),
    "multi": ("multiplier", "mult", "加成", "倍率"),
    "multiplier": ("multi", "mult", "加成", "倍率"),
    "res": ("resist", "resistance", "抗性"),
    "resist": ("res", "resistance", "抗性"),
    "resistance": ("resist", "res", "抗性"),
    "phys": ("physical", "物理"),
    "ele": ("elemental", "元素"),
    "pen": ("penetration", "穿透"),
    "supp": ("suppression", "压制"),
    "suppression": ("supp", "suppress", "压制"),
    "inc": ("increased", "提高"),
    "max": ("maximum", "最大"),
    "min": ("minimum", "最小"),
    "mana": ("魔力",),
    "life": ("生命",),
    "hit": ("击中",),
    "dot": ("持续伤", "damage over time"),
    "speed": ("rate", "速度"),
    "armour": ("armor", "护甲"),
    "armor": ("armour", "护甲"),
    "evasion": ("evade", "闪避"),
    "block": ("格挡",),
    "leech": ("偷取",),
    "cost": ("消耗",),
}


def _parse_calc_cell_value(raw_text: str, disp_text: str) -> float | None:
    """Parse a clean numeric value from English `raw` (preferred) or localized `text`, returning finite float or None."""
    for candidate in (raw_text, disp_text):
        s = (candidate or "").strip()
        if not s:
            continue
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "-inf", "-infinity", "nan"):
            return None
        m = _CALC_CELL_NUM_RE.match(s)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if math.isfinite(val):
                    return val
            except ValueError:
                continue
    return None


def _norm_calc_text(text: str | None) -> str:
    """Normalize calc label/query: strip PoB escapes, split camelCase, lowercase, collapse punctuation.

    Also folds Traditional -> Simplified Chinese, since pob-zh.exe labels default to Traditional.
    """
    if not text:
        return ""
    cleaned = strip_pob_escapes(str(text))
    cleaned = _CAMEL_SPLIT_RE.sub(r"\1 \2", cleaned)
    cleaned = _CALC_PUNCT_RE.sub(" ", cleaned).strip().lower()
    return fold_han(cleaned)


def _compact_calc_text(text: str | None) -> str:
    """Strip spaces/underscores/punctuation and lowercase for compact key matching ('Average Hit' == 'averagehit')."""
    return _norm_calc_text(text).replace(" ", "")


def _token_matches_haystack(token: str, hay_norm: str, hay_compact: str) -> bool:
    if token in hay_norm or token in hay_compact:
        return True
    for syn in _CALC_TOKEN_SYNONYMS.get(token, ()):
        syn_norm = _norm_calc_text(syn)
        syn_compact = syn_norm.replace(" ", "")
        if syn_norm in hay_norm or (syn_compact and syn_compact in hay_compact):
            return True
    return False


def _score_calc_row(
    q_norm: str,
    q_compact: str,
    q_tokens: list[str],
    sec_norm: str,
    sub_norm: str,
    row_norm: str,
    cells_norm: str,
    sub_row_compact: str,
) -> float:
    """Score how well a calcs row matches the bilingual query (0.0 means no match)."""
    if not q_compact:
        return 1.0

    row_compact = row_norm.replace(" ", "")
    sub_compact = sub_norm.replace(" ", "")
    full_norm = f"{sec_norm} {sub_norm} {row_norm} {cells_norm}".strip()
    full_compact = full_norm.replace(" ", "")

    if q_norm in row_norm or q_compact in row_compact:
        return 100.0 + (20.0 if row_compact == q_compact else 0.0)
    if q_compact in sub_row_compact:
        return 90.0
    if q_norm in sub_norm or q_compact in sub_compact:
        return 75.0
    if q_norm in cells_norm or q_compact in cells_norm.replace(" ", ""):
        return 70.0
    if q_norm in sec_norm or q_compact in sec_norm.replace(" ", ""):
        return 60.0

    if len(q_tokens) >= 2:
        if all(_token_matches_haystack(tok, full_norm, full_compact) for tok in q_tokens):
            row_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, row_norm, row_compact))
            sub_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, sub_norm, sub_compact))
            if row_hits > 0:
                return 80.0 + row_hits * 5.0 + sub_hits * 3.0
            return 55.0 + sub_hits * 3.0
    elif len(q_tokens) == 1:
        tok = q_tokens[0]
        if _token_matches_haystack(tok, row_norm, row_compact):
            return 65.0
        if _token_matches_haystack(tok, sub_norm, sub_compact):
            return 50.0
        if _token_matches_haystack(tok, full_norm, full_compact):
            return 40.0

    return 0.0


def _ensure_calcs_translate_enabled(worker: BridgeWorker) -> tuple[bool | None, bool]:
    orig_translate: bool | None = None
    toggled_translate = False
    if worker.caps.get("translateToggle", False):
        tracked = getattr(worker, "translate_enabled", None)
        if tracked is None:
            info_dict = getattr(worker, "hello_info", None) or getattr(worker, "info", None) or {}
            if isinstance(info_dict, dict) and "translate" in info_dict:
                tracked = info_dict.get("translate")
        if tracked is None:
            try:
                ver = worker.call("version", {})
                tracked = ver.get("translate")
            except Exception:
                tracked = None
        if tracked is False:
            orig_translate = False
            try:
                worker.call("set_translate", {"enabled": True})
                toggled_translate = True
            except Exception:
                toggled_translate = False
    return orig_translate, toggled_translate


def _index_calc_rows(calcs: dict[str, Any]) -> list[dict[str, Any]]:
    indexed_rows: list[dict[str, Any]] = []
    seq = 0
    for sec in calcs.get("sections", []):
        si = sec.get("si")
        sec_id = str(sec.get("id") or "")
        sec_group = str(sec.get("group") or "")
        sec_norm = _norm_calc_text(f"{sec_id} {sec_group}")
        for sub in sec.get("subsections", []):
            ui = sub.get("ui")
            sub_en = str(sub.get("label") or "")
            sub_zh = str(sub.get("labelZh") or "")
            sub_extra = str(sub.get("extra") or "")
            sub_norm = _norm_calc_text(f"{sub_en} {sub_zh} {sub_extra}")
            for row in sub.get("rows", []):
                ri = row.get("ri")
                r_en = str(row.get("label") or "")
                r_zh = str(row.get("labelZh") or "")
                row_norm = _norm_calc_text(f"{r_en} {r_zh}")

                cell_parts: list[str] = []
                for cell in row.get("cells", []):
                    if cell.get("text"):
                        cell_parts.append(str(cell["text"]))
                    if cell.get("raw") and cell.get("raw") != cell.get("text"):
                        cell_parts.append(str(cell["raw"]))
                    widget = cell.get("widget")
                    if isinstance(widget, dict):
                        if widget.get("value"):
                            cell_parts.append(str(widget["value"]))
                        sel_idx = widget.get("selIndex")
                        w_list = widget.get("list")
                        if isinstance(w_list, list) and isinstance(sel_idx, int) and 1 <= sel_idx <= len(w_list):
                            sel_entry = w_list[sel_idx - 1]
                            if isinstance(sel_entry, dict):
                                cell_parts.append(str(sel_entry.get("label") or ""))
                                cell_parts.append(str(sel_entry.get("labelZh") or ""))
                            else:
                                cell_parts.append(str(sel_entry))
                cells_norm = _norm_calc_text(" ".join(cell_parts))
                sub_row_compact = (
                    _compact_calc_text(f"{sub_en} {r_en}")
                    + " "
                    + _compact_calc_text(f"{sub_zh} {r_zh}")
                )
                indexed_rows.append(
                    {
                        "seq": seq,
                        "si": si,
                        "ui": ui,
                        "ri": ri,
                        "sec_id": sec_id,
                        "sec_group": sec_group,
                        "sec_norm": sec_norm,
                        "sub_en": sub_en,
                        "sub_zh": sub_zh or sub_en,
                        "sub_norm": sub_norm,
                        "row_en": r_en,
                        "row_zh": r_zh,
                        "row_norm": row_norm,
                        "cells_norm": cells_norm,
                        "sub_row_compact": sub_row_compact,
                        "cells": row.get("cells", []),
                    }
                )
                seq += 1
    return indexed_rows


def _build_calc_matches(
    scored: list[tuple[float, int, dict[str, Any]]],
    *,
    limit: int,
    max_breakdowns: int,
    no_breakdown: bool,
    bd_map: dict[tuple[Any, Any, Any, Any], list[Any]],
) -> list[dict[str, Any]]:
    out_matches: list[dict[str, Any]] = []
    kw_bd_count = 0
    for score, _, item in scored[:limit]:
        cells_out: list[dict[str, Any]] = []
        for cell in item["cells"]:
            ci = cell.get("ci")
            raw_clean = strip_pob_escapes(cell.get("raw") or cell.get("text") or "")
            text_clean = strip_pob_escapes(cell.get("text") or cell.get("raw") or "")
            c_entry: dict[str, Any] = {
                "ci": ci,
                "text": text_clean,
                "raw": raw_clean,
                "value": _parse_calc_cell_value(raw_clean, text_clean),
                "hasBreakdown": bool(cell.get("hasBreakdown")),
            }
            if not no_breakdown and cell.get("hasBreakdown") and kw_bd_count < max_breakdowns:
                coord = (item["si"], item["ui"], item["ri"], ci)
                if coord in bd_map:
                    c_entry["breakdown"] = bd_map[coord]
                kw_bd_count += 1
            cells_out.append(c_entry)

        out_matches.append(
            {
                "sectionId": item["sec_id"],
                "sectionGroup": item["sec_group"],
                "subsection": item["sub_zh"],
                "subsectionEn": item["sub_en"],
                "rowLabel": item["row_en"],
                "rowLabelZh": item["row_zh"],
                "score": score,
                "coords": {"si": item["si"], "ui": item["ui"], "ri": item["ri"]},
                "cells": cells_out,
            }
        )
    return out_matches


def search_calcs_breakdown(
    worker: BridgeWorker,
    keyword: str | None = None,
    *,
    keywords: Iterable[str] | str | None = None,
    limit: int = 20,
    max_breakdowns: int = 8,
    no_breakdown: bool = False,
) -> dict[str, Any]:
    """Bilingual (EN + ZH) search across `get_calcs` rows with variadic keywords and pipelined `calcs_breakdown`."""
    kw_list = _normalize_query_list(keyword, keywords)
    if not kw_list:
        kw_list = [""]
    else:
        from ..guardrails import GuardrailScope, check_input

        for kw in kw_list:
            if kw.strip():
                check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")

    with worker.lock:
        orig_translate, toggled_translate = _ensure_calcs_translate_enabled(worker)
        try:
            calcs = worker.call("get_calcs", {})
            indexed_rows = _index_calc_rows(calcs)

            per_kw_scored: list[tuple[str, list[tuple[float, int, dict[str, Any]]]]] = []
            bd_coord_order: list[tuple[Any, Any, Any, Any]] = []
            bd_coord_seen: set[tuple[Any, Any, Any, Any]] = set()

            for kw in kw_list:
                q_norm = _norm_calc_text(kw)
                q_compact = q_norm.replace(" ", "")
                q_tokens = [t for t in q_norm.split(" ") if t]
                scored: list[tuple[float, int, dict[str, Any]]] = []
                for item in indexed_rows:
                    score = _score_calc_row(
                        q_norm,
                        q_compact,
                        q_tokens,
                        item["sec_norm"],
                        item["sub_norm"],
                        item["row_norm"],
                        item["cells_norm"],
                        item["sub_row_compact"],
                    )
                    if score > 0:
                        scored.append((score, item["seq"], item))
                scored.sort(key=lambda pair: (-pair[0], pair[1]))
                per_kw_scored.append((kw, scored))

                if not no_breakdown and max_breakdowns > 0:
                    kw_bd_count = 0
                    for _, _, item in scored[:limit]:
                        for cell in item["cells"]:
                            if cell.get("hasBreakdown") and kw_bd_count < max_breakdowns:
                                coord = (item["si"], item["ui"], item["ri"], cell.get("ci"))
                                if coord not in bd_coord_seen:
                                    bd_coord_seen.add(coord)
                                    bd_coord_order.append(coord)
                                kw_bd_count += 1

            bd_map: dict[tuple[Any, Any, Any, Any], list[Any]] = {}
            if not no_breakdown and bd_coord_order:
                bd_calls = [
                    ("calcs_breakdown", {"si": si, "ui": ui, "ri": ri, "ci": ci})
                    for (si, ui, ri, ci) in bd_coord_order
                ]
                bd_responses = _call_batch(worker, bd_calls, raise_on_error=False)
                for coord, bd in zip(bd_coord_order, bd_responses, strict=False):
                    if isinstance(bd, dict) and not bd.get("_error") and bd.get("ok") is not False and "sections" in bd:
                        bd_map[coord] = bd.get("sections", [])

            if len(per_kw_scored) == 1:
                kw, scored = per_kw_scored[0]
                return {
                    "keyword": kw,
                    "totalMatches": len(scored),
                    "matches": _build_calc_matches(
                        scored,
                        limit=limit,
                        max_breakdowns=max_breakdowns,
                        no_breakdown=no_breakdown,
                        bd_map=bd_map,
                    ),
                }

            per_kw_results: list[dict[str, Any]] = []
            combined_matches: list[dict[str, Any]] = []
            seen_coords: set[tuple[Any, Any, Any]] = set()
            for kw, scored in per_kw_scored:
                kw_matches = _build_calc_matches(
                    scored,
                    limit=limit,
                    max_breakdowns=max_breakdowns,
                    no_breakdown=no_breakdown,
                    bd_map=bd_map,
                )
                per_kw_results.append(
                    {
                        "keyword": kw,
                        "totalMatches": len(scored),
                        "matches": kw_matches,
                    }
                )
                for m in kw_matches:
                    c = m["coords"]
                    key = (c["si"], c["ui"], c["ri"])
                    if key not in seen_coords:
                        seen_coords.add(key)
                        combined_matches.append({**m, "matchedKeyword": kw})

            return {
                "keyword": kw_list[0],
                "keywords": kw_list,
                "results": per_kw_results,
                "totalMatches": sum(r["totalMatches"] for r in per_kw_results),
                "matches": combined_matches,
            }
        finally:
            if toggled_translate and orig_translate is not None:
                try:
                    worker.call("set_translate", {"enabled": orig_translate})
                except Exception:
                    pass


inspect_calcs = search_calcs_breakdown
