"""Markdown helpers for `--for-build` passive-tree tattoo slot annotations and multi-query data output."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..locale_cfg import current_lang


def format_build_tattoo_slots_callout(build_slots: dict[str, Any]) -> str:
    """Format a compact 1-line Markdown callout summarizing current tree tattoo/runegraft base slots."""
    from ..analyzer.tattoo_slots import (
        BUCKET_MASTERY,
        BUCKET_NOTABLE_DEX,
        BUCKET_NOTABLE_INT,
        BUCKET_NOTABLE_STR,
        BUCKET_SMALL_DEX,
        BUCKET_SMALL_INT,
        BUCKET_SMALL_STR,
    )

    show_zh = current_lang() != "en"
    slot_lbl = str(build_slots.get("targetSlot") or "main")
    if not build_slots.get("buildLoaded", True):
        msg = (
            f"> [WARN] No build (or 0 allocated passive nodes) loaded in slot `{slot_lbl}` — "
            f"run `pob load <source> --target {slot_lbl}` first."
        )
        return f"{msg}（槽位 `{slot_lbl}` 未加载构筑或未分配天赋点，请先运行 `pob load`）" if show_zh else msg

    buckets = build_slots.get("buckets") if isinstance(build_slots.get("buckets"), dict) else {}

    def _fmt_small(label: str, key: str) -> str:
        b = buckets.get(key) if isinstance(buckets.get(key), dict) else {}
        elig, free, tat, conq = (int(b.get(k) or 0) for k in ("eligible", "free", "tattooed", "conquered"))
        if elig == 0 and conq == 0:
            return f"{label} 0"
        bits = [f"{free} free", f"{tat} tattooed"] + ([f"{conq} conquered"] if conq > 0 else [])
        return f"{label} {elig} eligible ({', '.join(bits)})"

    parts = [
        _fmt_small("Small Str", BUCKET_SMALL_STR),
        _fmt_small("Small Dex", BUCKET_SMALL_DEX),
        _fmt_small("Small Int", BUCKET_SMALL_INT),
    ]
    n_counts = [
        int((buckets.get(bk) or {}).get("eligible") or 0)
        for bk in (BUCKET_NOTABLE_STR, BUCKET_NOTABLE_DEX, BUCKET_NOTABLE_INT)
    ]
    if any(n_counts):
        parts.append(f"+30 Notable: Str {n_counts[0]} / Dex {n_counts[1]} / Int {n_counts[2]}")
    m_b = buckets.get(BUCKET_MASTERY) if isinstance(buckets.get(BUCKET_MASTERY), dict) else {}
    if int(m_b.get("eligible") or 0) > 0:
        m_tat, m_free = int(m_b.get("tattooed") or 0), int(m_b.get("free") or 0)
        parts.append(
            f"Mastery {m_b['eligible']} ({m_free} free, {m_tat} runegraft)" if m_tat else f"Mastery {m_b['eligible']}"
        )
    parts.append(f"Tattoos Used: {int(build_slots.get('tattoosUsed') or 0)}/{int(build_slots.get('tattooLimit') or 50)}")
    return f"> Current Tree (`{slot_lbl}`): " + " | ".join(parts)


def format_tattoo_tree_cell(r: dict[str, Any], *, show_zh: bool = False) -> str:
    """Format the per-row `Current Tree` availability cell for a tattoo/runegraft record."""
    elig, free, tat, conq = (int(r.get(k) or 0) for k in ("eligibleNodes", "freeNodes", "tattooedNodes", "conqueredNodes"))
    conq_s = f" ({conq} conquered)" if conq > 0 else ""
    if elig == 0:
        zh_s = " (当前树 0 可用)" if show_zh else ""
        return f"[0 available on current tree]{zh_s}{conq_s}"
    if free == 0:
        zh_s = " (仅可重涂)" if show_zh else ""
        return f"0 free / {tat} tattooed [re-tattoo only]{zh_s}{conq_s}"
    return f"{free} free / {elig} eligible" + (f", {tat} tattooed" if tat > 0 else "") + conq_s


def render_multi_game_data(
    multi_res: dict[str, Any],
    kind: str,
    q_list: list[str],
    format_single: Callable[..., str],
) -> str:
    """Format a multi-query `pob data` response into Markdown sections (with one optional tree-slots header)."""
    sections: list[str] = []
    if isinstance(multi_res.get("buildTattooSlots"), dict):
        sections.append(format_build_tattoo_slots_callout(multi_res["buildTattooSlots"]))
    res_map = multi_res.get("results")
    resp_queries = [str(x) for x in multi_res["queries"]] if isinstance(multi_res.get("queries"), list) else q_list
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else res_map.get(q_key, {})
            sections.append(format_single(sub or {}, kind=kind, query=q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(format_single(sub_dict, kind=kind, query=lbl))
    return "\n\n".join(sections)
