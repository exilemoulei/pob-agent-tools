"""Inverted stat index and slot-aware fuzzy resolver for PoE1 trade stats."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ..trade_item import normalize_template
from .aliases import (
    _ARMOUR_SLOTS,
    _SHIELD_SLOTS,
    _WEAPON_SLOTS,
    STAT_TYPE_ORDER,
    _normalize_slot,
)

_SCOPE_SUFFIX_RE = re.compile(r"\s*\((local|shields|global)\)\s*$", re.IGNORECASE)
_LEADING_PLACEHOLDER_RE = re.compile(r"^(?:#%\s+|#\s+)+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WEAPON_FLAT_DMG_RE = re.compile(r"^adds # to # (?:fire|cold|lightning|physical|chaos) damage$")
_SLOW_NORMALIZE_CHARS_RE = re.compile(r"[<\[{(0-9\u2013\u2014\u2212]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_CJK_BRACKET_STRIP_RE = re.compile(r"[【】「」『』《》]")
_AFFECTED_BY_AURA_RE = re.compile(r"\bwhile affected by ([a-z][a-z\s'-]{1,30}?)(?:\s*$|\s*\()")
_BARE_AFFECTED_BY_RE = re.compile(r"^while affected by ([a-z][a-z\s'-]{1,30}?)$")
_AURA_DISAMBIG_SCORE_GAP = 0.04
_UNREQUESTED_MULTIPLIER_TOKENS: frozenset[str] = frozenset({"more", "less", "reduced", "total"})

_STOPWORDS: frozenset[str] = frozenset(
    {
        "to",
        "of",
        "the",
        "a",
        "an",
        "in",
        "on",
        "and",
        "or",
        "for",
        "with",
        "per",
        "you",
        "your",
        "have",
        "has",
        "are",
        "is",
        "by",
        "at",
        "from",
        "as",
        "if",
    }
)

_ENTRY_INDEX_CACHE: dict[tuple[int, int, str, str], _EntryIndex] = {}
_ENTRY_INDEX_LOCK = threading.Lock()


@dataclass
class StatMatch:
    id: str
    text: str
    score: float
    template: str
    disambiguation_candidates: tuple[dict[str, Any], ...] = ()
    disambiguation_aura: str | None = None
    is_ambiguous_bare_aura: bool = False


def _extract_affected_aura(template: str) -> str | None:
    m = _AFFECTED_BY_AURA_RE.search(template or "")
    return m.group(1).strip() if m else None


def _stat_priority(stat_id: str) -> int:
    prefix = stat_id.split(".", 1)[0]
    return STAT_TYPE_ORDER.get(prefix, 5)


def _split_scope_tag(text: str) -> tuple[str, str]:
    """Split trailing ``(Local)`` / ``(Shields)`` / ``(Global)`` from raw stat text."""
    raw = (text or "").strip()
    m = _SCOPE_SUFFIX_RE.search(raw)
    if not m:
        return raw, ""
    return raw[: m.start()].strip(), m.group(1).lower()


def _strip_leading_placeholder(template: str) -> str:
    return _LEADING_PLACEHOLDER_RE.sub("", template).strip()


def _extract_tokens(text: str) -> frozenset[str]:
    return frozenset(tok for tok in _TOKEN_RE.findall(text) if len(tok) >= 2)


def _meaningful_tokens(tokens: frozenset[str]) -> frozenset[str]:
    filtered = tokens - _STOPWORDS
    return filtered if filtered else tokens


def _is_weapon_local_template(base_template: str) -> bool:
    if "attack speed" in base_template:
        return True
    if "critical strike chance" in base_template and "global" not in base_template:
        return True
    if (
        "physical damage" in base_template
        and "global" not in base_template
        and "over time" not in base_template
    ):
        return True
    if _WEAPON_FLAT_DMG_RE.match(base_template):
        return True
    if "accuracy rating" in base_template:
        return True
    if "chance to poison on hit" in base_template:
        return True
    if "physical attack damage leeched as" in base_template:
        return True
    return False


def _is_armour_local_template(base_template: str) -> bool:
    return any(
        kw in base_template
        for kw in ("armour", "evasion rating", "evasion", "energy shield", "ward")
    )


def _is_shield_block_template(base_template: str) -> bool:
    return "chance to block" in base_template


def _scope_adjustment(
    base_template: str,
    entry_scope: str,
    slot_norm: str,
    requested_scope: str,
) -> float:
    """Compute slot-aware preference adjustment for ``(Local)`` / ``(Shields)`` / global entries."""
    if requested_scope:
        if entry_scope == requested_scope:
            return 0.08
        if requested_scope == "global" and entry_scope == "":
            return 0.06
        return -0.08

    if not slot_norm:
        if entry_scope == "":
            return 0.01
        return -0.01

    if _is_shield_block_template(base_template):
        if slot_norm in _SHIELD_SLOTS:
            if entry_scope == "shields":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        if entry_scope == "shields":
            return -0.14

    if _is_weapon_local_template(base_template):
        if slot_norm in _WEAPON_SLOTS:
            if entry_scope == "local":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        # Non-weapon slot (gloves, ring, amulet, quiver, armour, etc.)
        if entry_scope == "local":
            return -0.14
        if entry_scope in ("", "global"):
            return 0.02

    if _is_armour_local_template(base_template):
        if slot_norm in _ARMOUR_SLOTS:
            if entry_scope == "local":
                return 0.08
            if entry_scope == "":
                return 0.0
            return -0.06
        # Non-armour slot (rings, amulet, belt, weapon, jewel, etc.)
        if entry_scope == "local":
            return -0.14
        if entry_scope in ("", "global"):
            return 0.02

    # Fallback for any other scoped entry when slot is known
    if entry_scope == "shields" and slot_norm not in _SHIELD_SLOTS:
        return -0.12
    if entry_scope == "":
        return 0.01
    return 0.0


@dataclass(frozen=True)
class _PreparedEntry:
    entry: dict[str, Any]
    stat_id: str
    raw_text: str
    base_template: str
    core_template: str
    scope_tag: str
    tokens: frozenset[str]
    priority: int


def _fast_normalize_stat_text(base_raw: str) -> str:
    """Fast path equivalent to ``normalize_template`` for clean TradeSiteStats lines."""
    if _SLOW_NORMALIZE_CHARS_RE.search(base_raw):
        return normalize_template(base_raw)
    value = base_raw.lower().replace("+", "")
    if "  " in value or "\t" in value or "\n" in value:
        value = _MULTI_SPACE_RE.sub(" ", value)
    return value.strip(" .,")


class _EntryIndex:
    """In-memory inverted index over trade stat entries for <15ms lookups."""

    __slots__ = ("by_base", "by_core", "by_token", "prepared")

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        prepared: list[_PreparedEntry] = []
        by_base: dict[str, list[_PreparedEntry]] = {}
        by_core: dict[str, list[_PreparedEntry]] = {}
        by_token: dict[str, list[int]] = {}

        for entry in entries:
            raw_text = str(entry.get("text") or "")
            stat_id = str(entry.get("id") or "")
            if not raw_text or not stat_id:
                continue
            base_raw, scope_tag = _split_scope_tag(raw_text)
            base_template = _fast_normalize_stat_text(base_raw)
            if not base_template:
                continue
            core_template = _strip_leading_placeholder(base_template)
            tokens = _extract_tokens(core_template)
            idx = len(prepared)
            item = _PreparedEntry(
                entry=entry,
                stat_id=stat_id,
                raw_text=raw_text,
                base_template=base_template,
                core_template=core_template,
                scope_tag=scope_tag,
                tokens=tokens,
                priority=_stat_priority(stat_id),
            )
            prepared.append(item)
            by_base.setdefault(base_template, []).append(item)
            if core_template:
                by_core.setdefault(core_template, []).append(item)
            for tok in tokens:
                by_token.setdefault(tok, []).append(idx)

        self.prepared = prepared
        self.by_base = by_base
        self.by_core = by_core
        self.by_token = by_token


def _get_entry_index(entries: list[dict[str, Any]]) -> _EntryIndex:
    if not entries:
        return _EntryIndex([])
    first_id = str(entries[0].get("id") or "")
    last_id = str(entries[-1].get("id") or "")
    key = (id(entries), len(entries), first_id, last_id)
    with _ENTRY_INDEX_LOCK:
        cached = _ENTRY_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
    index = _EntryIndex(entries)
    with _ENTRY_INDEX_LOCK:
        if len(_ENTRY_INDEX_CACHE) >= 8:
            _ENTRY_INDEX_CACHE.clear()
        _ENTRY_INDEX_CACHE[key] = index
    return index


def _verb_penalty(target_tokens: frozenset[str], item_tokens: frozenset[str]) -> float:
    """Avoid letting rare ``more`` / ``less`` / ``reduced`` mods hijack plain queries."""
    for tok in _UNREQUESTED_MULTIPLIER_TOKENS:
        if tok in item_tokens and tok not in target_tokens:
            return -0.04
    return 0.0


def _base_similarity(
    target: str,
    target_core: str,
    target_tokens: frozenset[str],
    item: _PreparedEntry,
) -> float:
    if target == item.base_template:
        return 1.0
    if target_core and target_core == item.core_template:
        return 0.98

    # Bare aura shorthand ("while affected by vitality") should score all sibling
    # stats for that aura closely above threshold so disambiguation catches them.
    if _BARE_AFFECTED_BY_RE.match(target_core) and target_core in item.core_template:
        ratio = len(target_core) / max(1, len(item.core_template))
        return 0.86 + 0.02 * ratio

    # Safe one-way containment: short query inside longer template ONLY when
    # the query is substantial (>=6 chars) and covers at least 45% of the entry.
    if len(target_core) >= 6 and target_core in item.core_template:
        ratio = len(target_core) / max(1, len(item.core_template))
        if ratio >= 0.45:
            return 0.90 + 0.05 * ratio

    union_len = len(target_tokens | item.tokens)
    jaccard = (len(target_tokens & item.tokens) / union_len) if union_len else 0.0
    seq_core = SequenceMatcher(None, target_core, item.core_template).ratio()
    blended = 0.45 * jaccard + 0.55 * seq_core
    if target != target_core and blended >= 0.55:
        seq_full = SequenceMatcher(None, target, item.base_template).ratio()
        return max(blended, seq_full * 0.95)
    return blended


def _select_candidates(
    index: _EntryIndex,
    target: str,
    target_core: str,
    target_tokens: frozenset[str],
    *,
    max_candidates: int = 120,
) -> list[_PreparedEntry]:
    """Fast candidate pre-filter using exact maps + inverted token index."""
    if len(index.prepared) <= max_candidates:
        return index.prepared

    seen_ids: set[int] = set()
    candidates: list[_PreparedEntry] = []

    def _add_prepared(items: list[_PreparedEntry]) -> None:
        for it in items:
            obj_id = id(it)
            if obj_id not in seen_ids:
                seen_ids.add(obj_id)
                candidates.append(it)

    exact_base = index.by_base.get(target)
    if exact_base:
        _add_prepared(exact_base)
    exact_core = index.by_core.get(target_core)
    if exact_core:
        _add_prepared(exact_core)

    # If we already have exact base/core matches, no need to scan 18k entries.
    if candidates:
        return candidates

    # Rank entries by overlapping meaningful tokens via inverted index.
    query_toks = _meaningful_tokens(target_tokens)
    overlap_counts: dict[int, int] = {}
    for tok in query_toks:
        for entry_idx in index.by_token.get(tok, ()):
            overlap_counts[entry_idx] = overlap_counts.get(entry_idx, 0) + 1

    if not overlap_counts and target_tokens != query_toks:
        for tok in target_tokens:
            for entry_idx in index.by_token.get(tok, ()):
                overlap_counts[entry_idx] = overlap_counts.get(entry_idx, 0) + 1

    if overlap_counts:
        # Sort by descending token overlap, breaking ties by shorter template length.
        ranked_indices = sorted(
            overlap_counts.keys(),
            key=lambda i: (-overlap_counts[i], len(index.prepared[i].core_template)),
        )[:max_candidates]
        return [index.prepared[i] for i in ranked_indices]

    # Last-resort prefix/character filter if no exact token matched (e.g. typo).
    prefix = target_core[:3]
    if prefix:
        prefix_hits = [it for it in index.prepared if prefix in it.core_template]
        if prefix_hits:
            return prefix_hits[:max_candidates]
    return index.prepared[:max_candidates]


def _collect_aura_disambiguation(
    scored: list[tuple[float, int, int, float, _PreparedEntry]],
    best_item: _PreparedEntry,
    best_effective: float,
    target_core: str,
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    aura = _extract_affected_aura(best_item.core_template)
    if not aura:
        return (), None
    is_bare_aura = bool(_BARE_AFFECTED_BY_RE.match(target_core))
    siblings: list[dict[str, Any]] = []
    seen_templates: set[str] = set()
    for effective, _neg_prio, _neg_len, base_score, item in scored:
        if item.core_template in seen_templates:
            continue
        if _extract_affected_aura(item.core_template) != aura:
            continue
        if not is_bare_aura and (best_effective - effective) >= _AURA_DISAMBIG_SCORE_GAP:
            continue
        seen_templates.add(item.core_template)
        siblings.append(
            {
                "id": item.stat_id,
                "text": item.raw_text,
                "score": round(min(1.0, max(0.0, base_score)), 3),
            }
        )
        if len(siblings) >= 8:
            break
    if len(siblings) < 2:
        return (), None
    return tuple(siblings), aura


def aura_disambiguation_for_query(
    text: str,
    entries: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    """Return ``(aura, candidates)`` for any ``while affected by <Aura>`` / ``受.*影响时`` query."""
    import pob_cli.trade_query as _tq

    cleaned_in = _CJK_BRACKET_STRIP_RE.sub("", text or "").strip()
    translated = _tq.translate_stat_query(cleaned_in)
    query_base, _ = _split_scope_tag(translated)
    target = normalize_template(query_base)
    if not target:
        return None, ()
    aura = _extract_affected_aura(_strip_leading_placeholder(target))
    if not aura:
        return None, ()
    index = _get_entry_index(entries)
    out: list[dict[str, Any]] = []
    seen_templates: set[str] = set()
    for item in index.prepared:
        if item.core_template in seen_templates:
            continue
        if _extract_affected_aura(item.core_template) == aura:
            seen_templates.add(item.core_template)
            out.append({"id": item.stat_id, "text": item.raw_text, "score": 0.0})
            if len(out) >= limit:
                break
    return (aura, tuple(out)) if out else (None, ())


def resolve_stat(
    text: str,
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
    threshold: float = 0.80,
) -> StatMatch | None:
    """Resolve an EN or ZH modifier query to its PoE1 trade stat ID.

    Slot-aware: prefers ``(Local)`` for local weapon stats on weapon slots,
    ``(Local)`` for local defence stats on armour slots, ``(Shields)`` for
    block chance on shields, and global/unscoped stats otherwise.
    """
    import pob_cli.trade_query as _tq

    cleaned_in = _CJK_BRACKET_STRIP_RE.sub("", text or "").strip()
    translated = _tq.translate_stat_query(cleaned_in)
    query_base, requested_scope = _split_scope_tag(translated)
    target = normalize_template(query_base)
    if not target:
        return None
    target_core = _strip_leading_placeholder(target)
    target_tokens = _extract_tokens(target_core)
    slot_norm = _normalize_slot(slot)

    index = _get_entry_index(entries)
    if not index.prepared:
        return None

    candidates = _select_candidates(index, target, target_core, target_tokens)

    scored: list[tuple[float, int, int, float, _PreparedEntry]] = []
    for item in candidates:
        base_score = _base_similarity(target, target_core, target_tokens, item)
        if base_score < threshold:
            continue
        scope_adj = _scope_adjustment(item.base_template, item.scope_tag, slot_norm, requested_scope)
        verb_adj = _verb_penalty(target_tokens, item.tokens)
        effective = round(base_score + scope_adj + verb_adj, 6)
        scored.append((effective, -item.priority, -len(item.base_template), base_score, item))

    if not scored:
        return None
    scored.sort(key=lambda row: row[:3], reverse=True)
    best_effective, _, _, base_score, item = scored[0]
    disambig, disambig_aura = _collect_aura_disambiguation(scored, item, best_effective, target_core)
    is_bare_aura = bool(_BARE_AFFECTED_BY_RE.match(target_core))
    return StatMatch(
        id=item.stat_id,
        text=item.raw_text,
        score=round(min(1.0, max(0.0, base_score)), 3),
        template=item.base_template,
        disambiguation_candidates=disambig,
        disambiguation_aura=disambig_aura,
        is_ambiguous_bare_aura=bool(is_bare_aura and len(disambig) > 1),
    )


def suggest_stats(
    text: str,
    entries: list[dict[str, Any]],
    limit: int = 5,
    *,
    slot: str | None = None,
) -> list[StatMatch]:
    """Return top candidate trade stat matches for error diagnostics or discovery."""
    import pob_cli.trade_query as _tq

    cleaned_in = _CJK_BRACKET_STRIP_RE.sub("", text or "").strip()
    translated = _tq.translate_stat_query(cleaned_in)
    query_base, requested_scope = _split_scope_tag(translated)
    target = normalize_template(query_base)
    if not target:
        return []
    target_core = _strip_leading_placeholder(target)
    target_tokens = _extract_tokens(target_core)
    slot_norm = _normalize_slot(slot)

    index = _get_entry_index(entries)
    if not index.prepared:
        return []

    candidates = _select_candidates(index, target, target_core, target_tokens, max_candidates=200)
    scored: list[tuple[float, int, int, float, _PreparedEntry]] = []
    for item in candidates:
        base_score = _base_similarity(target, target_core, target_tokens, item)
        scope_adj = _scope_adjustment(item.base_template, item.scope_tag, slot_norm, requested_scope)
        verb_adj = _verb_penalty(target_tokens, item.tokens)
        effective = round(base_score + scope_adj + verb_adj, 6)
        scored.append((effective, -item.priority, -len(item.base_template), base_score, item))

    scored.sort(key=lambda row: row[:3], reverse=True)
    return [
        StatMatch(
            id=item.stat_id,
            text=item.raw_text,
            score=round(min(1.0, max(0.0, base_score)), 3),
            template=item.base_template,
        )
        for _, _, _, base_score, item in scored[:limit]
    ]
