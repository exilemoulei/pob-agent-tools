# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Provenance-enforced PoE2 0.5.5 mechanics fact cards and <15ms `pob2 ground` query engine (L1)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..guardrails import (
    GuardrailScope,
    check_input,
    resolve_poe1_zh_alias,
    warning_context,
)

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "mechanics_facts.json"

# Every fact card MUST cite a verifiable source (PoB2 .lua / RePoE / PoE2DB / Patch Notes / EE2).
_PROVENANCE_PATTERN = re.compile(
    r"(?:\.lua\b|poe2db\.tw|poe2wiki\.net|pathofexile\.com|Patch\s+0\.\d|RePoE\s+[a-z0-9_.\[\]]+|EE2\s+[a-z0-9_.]+|PoB2\s+)",
    re.IGNORECASE,
)

_ZH_GROUND_ALIASES: dict[str, str] = {
    "护甲": "armour",
    "元素抗性": "resistance overcap",
    "抗性": "resistance overcap",
    "闪避": "evasion",
    "偏转": "deflection",
    "偏斜": "deflection",
    "格挡": "block",
    "能量护盾": "energy shield",
    "护盾": "energy shield",
    "异灵之体": "chaos inoculation",
    "异能魔力": "eldritch battery",
    "心灵升华": "mind over matter",
    "精魂": "spirit",
    "辅助宝石": "support gems lineage",
    "血脉辅助": "lineage support",
    "武器套装": "weapon set",
    "伤害转化": "damage conversion",
    "品质": "quality",
    "暴击": "critical",
    "点燃": "ignite flammability",
    "感电": "shock",
    "冰冻": "freeze",
    "眩晕": "stun",
    "护符": "charms",
    "咒符": "charms",
    "引路石": "waystone",
    "碑牌": "tablet",
    "精华": "essence crafted",
    "合金": "verisium alloys",
    "亵渎": "desecrated",
    "削切预兆": "whittling",
    "破裂石": "fracturing orb",
    "神圣化": "sanctification",
    "符文": "runes",
    "灵核": "soul cores",
    "涂灌": "liquid emotions",
    "终结": "culling strike",
    "偷取": "leech",
    "德鲁伊": "druid talisman",
    "金币": "gold respec",
    "冷酷盛宴": "grim feast",
}


class MissingProvenanceError(ValueError):
    """Raised when a mechanics fact card lacks a verifiable PoB2/Patch/PoE2DB source reference."""


@dataclass(frozen=True, slots=True)
class FactCard:
    slug: str
    title: str
    category: str
    tags: tuple[str, ...]
    poe1_or_stale_trap: str
    poe2_live_rule: str
    formula_expr: str
    source_ref: str
    patch_verified: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "category": self.category,
            "tags": list(self.tags),
            "poe1OrStaleTrap": self.poe1_or_stale_trap,
            "poe2LiveRule": self.poe2_live_rule,
            "formulaExpr": self.formula_expr,
            "sourceRef": self.source_ref,
            "patchVerified": self.patch_verified,
        }


@dataclass(frozen=True, slots=True)
class GroundResult:
    query: str
    normalized_query: str
    elapsed_ms: float
    cards: tuple[FactCard, ...]
    warnings: tuple[dict[str, Any], ...]
    patch: str = "0.5.5"

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "patch": self.patch,
            "query": self.query,
            "normalizedQuery": self.normalized_query,
            "elapsedMs": round(self.elapsed_ms, 3),
            "count": len(self.cards),
            "cards": [c.to_dict() for c in self.cards],
            "warnings": list(self.warnings),
        }


def validate_fact_provenance(card: FactCard) -> None:
    """Enforce mandatory verifiable provenance; unsourced rules belong in docs/unverified-mechanics.md."""
    src = (card.source_ref or "").strip()
    if not src or not _PROVENANCE_PATTERN.search(src):
        raise MissingProvenanceError(
            f"FactCard {card.slug!r} lacks a verifiable PoB2 Lua / PoE2DB / Patch Notes source_ref: {src!r}. "
            "Quarantine unverified rules in pob2_cli/docs/unverified-mechanics.md."
        )
    if not (card.patch_verified or "").strip():
        raise MissingProvenanceError(f"FactCard {card.slug!r} is missing patch_verified.")


@dataclass(slots=True)
class _CardIndex:
    cards: tuple[FactCard, ...]
    search_blobs: tuple[tuple[str, frozenset[str], frozenset[str]], ...]


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9\u3400-\u9fff]+", text.lower()) if t]


@lru_cache(maxsize=2)
def _load_index(path: Path = _DATA_PATH) -> _CardIndex:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards: list[FactCard] = []
    blobs: list[tuple[str, frozenset[str], frozenset[str]]] = []
    for item in raw:
        card = FactCard(
            slug=str(item["slug"]),
            title=str(item["title"]),
            category=str(item.get("category") or "general"),
            tags=tuple(str(t) for t in item.get("tags", ())),
            poe1_or_stale_trap=str(item.get("poe1_or_stale_trap") or ""),
            poe2_live_rule=str(item.get("poe2_live_rule") or ""),
            formula_expr=str(item.get("formula_expr") or ""),
            source_ref=str(item.get("source_ref") or ""),
            patch_verified=str(item.get("patch_verified") or ""),
        )
        validate_fact_provenance(card)
        cards.append(card)
        tag_set = frozenset(t.lower() for t in card.tags)
        full_blob = f"{card.slug} {card.title} {' '.join(card.tags)} {card.poe2_live_rule} {card.formula_expr}".lower()
        tok_set = frozenset(_tokenize(full_blob))
        blobs.append((full_blob, tag_set, tok_set))
    return _CardIndex(tuple(cards), tuple(blobs))


def load_fact_cards(path: Path | None = None) -> tuple[FactCard, ...]:
    """Return all provenance-verified PoE2 0.5.5 mechanics fact cards."""
    return _load_index(path or _DATA_PATH).cards


def _expand_zh_ground_query(query: str) -> str:
    out = query.strip()
    alias = resolve_poe1_zh_alias(out, emit_warning=False)
    if alias is not None:
        out = f"{out} {alias[0]}"
    for zh, en in sorted(_ZH_GROUND_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if zh in out:
            out = f"{out} {en}"
    return out


def ground_query(
    query: str,
    *,
    category: str | None = None,
    limit: int = 5,
    emit_stderr: bool = False,
    path: Path | None = None,
) -> GroundResult:
    """Fast (<15ms) guardrail-checked PoE2 0.5.5 mechanic grounding query."""
    t0 = time.perf_counter()
    with warning_context() as collected:
        check_input(query, GuardrailScope.DATA_QUERY, expected_game="poe2", emit_stderr=emit_stderr)
        expanded = _expand_zh_ground_query(query)
        idx = _load_index(path or _DATA_PATH)
        q_low = expanded.lower().strip()
        q_tokens = _tokenize(q_low)

        scored: list[tuple[int, int, FactCard]] = []
        cat_filter = category.lower().strip() if category else None
        for i, card in enumerate(idx.cards):
            if cat_filter and card.category.lower() != cat_filter:
                continue
            blob, tag_set, tok_set = idx.search_blobs[i]
            score = 0
            if q_low and q_low == card.slug.lower():
                score += 200
            if q_low and q_low in tag_set:
                score += 120
            if q_low and q_low in card.title.lower():
                score += 80
            for tok in q_tokens:
                if tok in tag_set:
                    score += 45
                elif tok in tok_set:
                    score += 20
                elif len(tok) >= 3 and tok in blob:
                    score += 10
            if score > 0:
                scored.append((score, -i, card))

        scored.sort(reverse=True)
        matched = tuple(c for _, _, c in scored[: max(1, limit)])
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return GroundResult(
            query=query,
            normalized_query=expanded,
            elapsed_ms=elapsed_ms,
            cards=matched,
            warnings=tuple(w.to_dict() for w in collected),
        )
