"""Bilingual EN/ZH resolution and fuzzy suggestion helpers for PoE1 Large Cluster Jewels."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from .aliases import _CJK_RE, translate_stat_query
from .cluster_data import (
    ZH_LARGE_ENCHANT_ALIASES,
    ZH_NOTABLE_OFFLINE_FALLBACK,
    ClusterData,
    ClusterEnchant,
    ClusterNotable,
)

_ZH_STRIP_RE = re.compile(r"[\s【】「」『』《》]+")


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def suggest_cluster_notables(
    query: str,
    data: ClusterData,
    *,
    enchant: ClusterEnchant | None = None,
    limit: int = 3,
) -> list[str]:
    """Return top-N closest notable suggestions across EN names and ZH aliases."""
    raw = (query or "").strip()
    if not raw or limit <= 0:
        return []
    from ..game_data import fold_han

    q_low = _norm_key(raw)
    q_zh = _ZH_STRIP_RE.sub("", fold_han(raw))
    in_pool = (
        {nm for nm, n in data.notables_by_name.items() if n.weights.get(enchant.tag, 0) > 0}
        if enchant is not None
        else set()
    )

    best_by_en: dict[str, tuple[float, str]] = {}

    def _record(en_name: str, score: float, label: str) -> None:
        if en_name not in data.notables_by_name:
            return
        total = score + (0.12 if en_name in in_pool else 0.0)
        prev = best_by_en.get(en_name)
        if prev is None or total > prev[0]:
            best_by_en[en_name] = (total, label)

    for en_name in data.notables_by_name:
        en_low = en_name.lower()
        ratio = difflib.SequenceMatcher(None, q_low, en_low).ratio()
        if q_low in en_low or en_low in q_low:
            ratio = max(ratio, 0.72)
        _record(en_name, ratio, en_name)

    if q_zh:
        for zh_alias, en_name in ZH_NOTABLE_OFFLINE_FALLBACK.items():
            if en_name not in data.notables_by_name:
                continue
            ratio = difflib.SequenceMatcher(None, q_zh, zh_alias).ratio()
            if q_zh in zh_alias or zh_alias in q_zh:
                ratio = max(ratio, 0.75)
            _record(en_name, ratio, f"{zh_alias} ({en_name})")

    ranked = sorted(best_by_en.values(), key=lambda item: (-item[0], item[1]))
    return [label for _, label in ranked[:limit]]


def resolve_large_enchant(
    query: str,
    data: ClusterData,
    *,
    pob_root: Path | None = None,
) -> ClusterEnchant:
    """Resolve an EN or ZH Large Cluster Jewel enchant query to a ``ClusterEnchant``."""
    raw = (query or "").strip()
    if not raw:
        raise ValueError("Cluster enchant cannot be empty.")

    probes: list[str] = [raw]
    if _CJK_RE.search(raw):
        from ..game_data import english_for_zh, fold_han

        folded = fold_han(raw)
        alias_tag = ZH_LARGE_ENCHANT_ALIASES.get(folded) or ZH_LARGE_ENCHANT_ALIASES.get(
            re.sub(r"[\s+#%\d]+", "", folded)
        )
        if alias_tag and alias_tag in data.large_enchants:
            return data.large_enchants[alias_tag]
        for cand_zh in dict.fromkeys((raw, folded)):
            try:
                en = english_for_zh(cand_zh, pob_dir=pob_root)
            except Exception:
                en = None
            if en and en not in probes:
                probes.append(en)
        tr = translate_stat_query(raw)
        if tr and tr != raw and tr not in probes:
            probes.append(tr)

    for probe in probes:
        p = _norm_key(probe)
        if p in data.large_enchants:
            return data.large_enchants[p]
        for ench in data.large_enchants.values():
            candidates = [ench.name, *ench.stats, *ench.enchant_lines]
            if len(ench.stats) > 1:
                candidates.append("\n".join(ench.stats))
            if len(ench.enchant_lines) > 1:
                candidates.append("\n".join(ench.enchant_lines))
            if any(_norm_key(c) == p for c in candidates):
                return ench

    sub_hits: list[ClusterEnchant] = []
    for probe in probes:
        p = _norm_key(probe).removeprefix("added small passive skills grant:").strip()
        p = re.sub(r"^\d+%\s+increased\s+", "", p).strip()
        if len(p) < 3:
            continue
        for ench in data.large_enchants.values():
            if p in " | ".join((ench.name, *ench.stats)).lower() and ench not in sub_hits:
                sub_hits.append(ench)
    if sub_hits:
        sub_hits.sort(key=lambda e: len(e.name))
        return sub_hits[0]

    avail = ", ".join(sorted(e.name for e in data.large_enchants.values()))
    raise ValueError(f"Unknown Large Cluster Jewel enchant {query!r}. Known enchants: {avail}")


def resolve_cluster_notable(
    query: str,
    data: ClusterData,
    *,
    enchant: ClusterEnchant | None = None,
    pob_root: Path | None = None,
) -> ClusterNotable:
    """Resolve an EN or ZH Cluster Jewel notable name to its ``ClusterNotable``."""
    raw = (query or "").strip()
    if not raw:
        raise ValueError("Notable name cannot be empty.")

    by_low = {_norm_key(nm): n for nm, n in data.notables_by_name.items()}
    probes: list[str] = [raw]
    if _CJK_RE.search(raw):
        from ..game_data import english_for_zh, fold_han

        folded = fold_han(raw)
        compact = _ZH_STRIP_RE.sub("", folded)
        for zh_key in dict.fromkeys((folded, compact, raw)):
            fb = ZH_NOTABLE_OFFLINE_FALLBACK.get(zh_key)
            if fb and fb not in probes:
                probes.append(fb)
        for cand_zh in dict.fromkeys((raw, folded, compact)):
            if not cand_zh:
                continue
            try:
                en = english_for_zh(cand_zh, pob_dir=pob_root)
            except Exception:
                en = None
            if en and en not in probes:
                probes.append(en)

    for probe in probes:
        hit = by_low.get(_norm_key(probe))
        if hit is not None:
            return hit

    pool_items = (
        [n for n in data.notables_by_name.values() if n.weights.get(enchant.tag, 0) > 0]
        if enchant is not None
        else list(data.notables_by_name.values())
    )
    for scope in (pool_items, list(data.notables_by_name.values())):
        for probe in probes:
            p = _norm_key(probe)
            if len(p) < 3:
                continue
            matches = [n for n in scope if p in _norm_key(n.name)]
            if matches:
                matches.sort(key=lambda n: len(n.name))
                return matches[0]

    suggestions = suggest_cluster_notables(raw, data, enchant=enchant, limit=3)
    sugg_hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    pool_hint = f" Pool for {enchant.name}: {', '.join(sorted(n.name for n in pool_items))}" if enchant else ""
    raise ValueError(f"Unknown Cluster Jewel notable {query!r}.{sugg_hint}{pool_hint}")
