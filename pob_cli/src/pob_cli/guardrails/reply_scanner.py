"""L0 deterministic assistant-reply scanner for PoE1 3.30 anti-hallucination hooks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .build_gate import URL_GATE_RULES
from .reply_patterns import (
    _ALT_QUALITY_REPLY_RE,
    _AMBIGUOUS_ASC_EN,
    _AWAKE_3_RE,
    _BUILD_BASE64_RE,
    _BUILD_SOURCE_PATTERNS,
    _CHARM_CTX_RE,
    _DISTILLED_EN_CTX_RE,
    _DISTILLED_EN_EXCL_RE,
    _EXC_OVER_LV5_RE,
    _EXCLUDED_POE2_IDS,
    _EXTRA_SLOT_ALIASES,
    _FALLACY_SPECS,
    _FREE_TEXT_ASC_ZH,
    _HARD_REMOVED_LEGACY_IDS,
    _HESITATION_CITED_EXEMPT_RE,
    _HESITATION_EN_RE,
    _HESITATION_V3_EXEMPT_RE,
    _HESITATION_ZH_RE,
    _INTERROGATIVE_HEADING_RE,
    _MECH_INTENT_SPECS,
    _NEGATION_EXEMPT_RE,
    _OPEN_AFFIX_CLAIM_RE,
    _OPEN_AFFIX_EXEMPT_RE,
    _PANEL_PATTERNS,
    _PAREN_EN_ENTITY_RE,
    _POE2_EB_FALLACY_RE,
    _POE2_EB_TERM_RE,
    _POE2_SINGLE_SUPPORT_RE,
    _QUERY_STOPWORDS,
    _QUOTED_TERM_RE,
    _SENTENCE_SPLIT_RE,
    _SINGLE_SUPPORT_DEBUNK_RE,
    _SPIRIT_EN_CTX_RE,
    _SPIRIT_EN_EXCL_RE,
    _TITLE_EN_ENTITY_RE,
    _TRIGGER_CTX_RE,
    _TRIGGER_NUM_RE,
    _URL_RE,
    _ZH_QUERY_SPLIT_RE,
    _ZH_QUERY_STOPWORDS,
)
from .rules_legacy import LEGACY_PATCH_TRAPS_POE1
from .rules_poe2 import (
    POE2_ONLY_ASCENDANCIES_EN,
    POE2_ONLY_REVERSE_TRAPS_FOR_POB1,
    POE2_ONLY_TIMELESS_JEWELS_EN,
    POE2_ONLY_TIMELESS_JEWELS_ZH,
)
from .warnings import (
    EQUIPMENT_SLOT_ALIASES,
    _match_rule_fields,
    match_phrase_zh,
    match_word_en,
    normalize_equipment_slot,
    normalize_guardrail_text,
    normalize_zh_compact,
)


@dataclass(frozen=True)
class ReplyIssue:
    rule_id: str
    category: str
    trigger: str
    sentence: str
    reason: str
    remediation: str
    ground_terms: tuple[str, ...] = ()


def _strip_blockquotes(text: str) -> str:
    return "\n".join(
        ln
        for ln in (text or "").splitlines()
        if not ln.lstrip().startswith(">") and not _INTERROGATIVE_HEADING_RE.match(ln)
    )


def _split_sentences(text: str) -> list[str]:
    return [seg.strip() for seg in _SENTENCE_SPLIT_RE.split(_strip_blockquotes(text)) if seg.strip()]


def _scan_sentence_poe2(
    sentence: str, norm: str, zh_compact: str, seen: set[str], issues: list[ReplyIssue]
) -> None:
    def _add(rule_id: str, trigger: str, reason: str, remediation: str, terms: tuple[str, ...] = ()) -> None:
        if rule_id not in seen:
            seen.add(rule_id)
            issues.append(ReplyIssue(rule_id, "poe2", trigger, sentence, reason, remediation, terms or (trigger,)))

    if not _NEGATION_EXEMPT_RE.search(sentence):
        for url_m in _URL_RE.finditer(sentence):
            cand = url_m.group(0).rstrip(".,);]")
            for ur in URL_GATE_RULES:
                if ur.belongs_to == "poe2" and ur.pattern.search(cand):
                    _add(ur.rule_id, cand, f"URL matches {ur.label} (PoE2), not PoE1.", ur.counterpart_hint, (cand,))

        for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
            if rule.rule_id in _EXCLUDED_POE2_IDS or rule.rule_id in seen:
                continue
            hit = _match_rule_fields(
                sentence, norm, norm, zh_compact,
                en_exact=rule.en_exact, zh_exact=rule.zh_exact, en_words=rule.en_words,
                zh_phrases=rule.zh_phrases, regex=rule.regex,
                exclude_en_re=rule.exclude_en_re, exclude_zh_compounds=rule.exclude_zh_compounds,
            )
            if hit is None and rule.rule_id == "POE2_SPIRIT_RESERVATION":
                hit = match_phrase_zh(sentence, "精魂", ("精魂之盾",))
                if hit is None and _SPIRIT_EN_CTX_RE.search(norm) and not _SPIRIT_EN_EXCL_RE.search(norm):
                    hit = "spirit"
            elif hit is None and rule.rule_id == "POE2_WAYSTONE":
                hit = match_word_en(norm, "waystone", rule.exclude_en_re) or match_word_en(
                    norm, "waystones", rule.exclude_en_re
                )
            elif hit is None and rule.rule_id == "POE2_DISTILLED_EMOTIONS":
                if _DISTILLED_EN_CTX_RE.search(norm) and not _DISTILLED_EN_EXCL_RE.search(norm):
                    hit = "distilled"
            elif hit is None and rule.rule_id == "POE2_CHARM_BELT_SLOTS":
                cm = _CHARM_CTX_RE.search(sentence)
                hit = cm.group(0) if cm else None
            if hit is not None:
                _add(rule.rule_id, hit, rule.error, rule.remediation, (hit,))

        if "POE2_TIMELESS_JEWEL_IN_POB1" not in seen:
            tj = next((p for p in POE2_ONLY_TIMELESS_JEWELS_EN if match_word_en(norm, p) is not None), None)
            if tj is None:
                tj = next((z for z in POE2_ONLY_TIMELESS_JEWELS_ZH if match_phrase_zh(sentence, z) is not None), None)
            if tj is not None:
                _add(
                    "POE2_TIMELESS_JEWEL_IN_POB1", tj,
                    f"`{tj}` is a PoE2 timeless/radius jewel and does not exist in PoE1.",
                    "Use `pob timeless list` for valid PoE1 Historic jewels.", (tj,),
                )

        if "POE2_CLASS_IN_POB1" not in seen:
            asc = next(
                (a for a in sorted(POE2_ONLY_ASCENDANCIES_EN, key=len, reverse=True)
                 if a not in _AMBIGUOUS_ASC_EN and match_word_en(norm, a) is not None),
                None,
            ) or next((z for z in _FREE_TEXT_ASC_ZH if match_phrase_zh(sentence, z) is not None), None)
            if asc is not None:
                _add(
                    "POE2_CLASS_IN_POB1", asc,
                    f"`{asc}` is a PoE2-only ascendancy and does not exist in PoE1.",
                    "PoE1 classes: Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion.", (asc,),
                )

        if "POE2_EB_MANA_CONVERSION" not in seen and _POE2_EB_TERM_RE.search(sentence):
            eb_m = _POE2_EB_FALLACY_RE.search(sentence)
            if eb_m is not None:
                _add(
                    "POE2_EB_MANA_CONVERSION", eb_m.group(0),
                    "PoE1 Eldritch Battery protects Mana with ES; it does NOT convert ES<->Mana (PoE2).",
                    "State PoE1 EB: ES protects Mana and pays skill Mana costs (50% less ES Recharge Rate).",
                    ("Eldritch Battery",),
                )

    if "POE2_SINGLE_SUPPORT_LIMIT" not in seen and not _SINGLE_SUPPORT_DEBUNK_RE.search(sentence):
        ss_m = _POE2_SINGLE_SUPPORT_RE.search(sentence)
        if ss_m is not None:
            _add(
                "POE2_SINGLE_SUPPORT_LIMIT", ss_m.group(0),
                "PoE1 allows duplicate support gems across different skill groups (single-support limit is PoE2 only).",
                "Remove the PoE2 single-support restriction.", ("support gem",),
            )


def _scan_sentence_legacy_and_fallacies(
    sentence: str, norm: str, zh_compact: str, seen: set[str], issues: list[ReplyIssue]
) -> None:
    if _NEGATION_EXEMPT_RE.search(sentence):
        return

    for prule in LEGACY_PATCH_TRAPS_POE1:
        if prule.rule_id not in _HARD_REMOVED_LEGACY_IDS or prule.rule_id in seen:
            continue
        if prule.rule_id == "PATCH_323_ALT_QUALITY_REMOVED":
            alt_m = _ALT_QUALITY_REPLY_RE.search(sentence)
            hit = alt_m.group(0).strip() if alt_m is not None else None
        else:
            hit = _match_rule_fields(
                sentence, norm, norm, zh_compact,
                en_exact=prule.en_exact, zh_exact=prule.zh_exact, en_words=prule.en_words,
                zh_phrases=prule.zh_phrases, regex=prule.regex,
                exclude_en_re=prule.exclude_en_re, exclude_zh_compounds=prule.exclude_zh_compounds,
            )
        if hit is not None:
            seen.add(prule.rule_id)
            issues.append(
                ReplyIssue(prule.rule_id, "legacy_removed", hit, sentence, prule.summary, prule.remediation, (hit,))
            )

    for rule_id, cat, term_re, trig_re, reason, remed, terms in _FALLACY_SPECS:
        if rule_id in seen:
            continue
        if term_re is not None and not term_re.search(sentence):
            continue
        m = trig_re.search(sentence)
        if m is not None:
            if (
                rule_id == "EXCEPTIONAL_SUPPORT_LEVEL_CAP"
                and _AWAKE_3_RE.search(sentence)
                and not _EXC_OVER_LV5_RE.search(sentence)
            ):
                continue
            seen.add(rule_id)
            issues.append(ReplyIssue(rule_id, cat, m.group(0), sentence, reason, remed, terms))


def scan_assistant_reply(text: str) -> list[ReplyIssue]:
    """Scan assistant final reply text for PoE2 leaks, 3.23-3.28 removals, and hard mechanic fallacies."""
    if not text or not text.strip():
        return []
    issues: list[ReplyIssue] = []
    seen: set[str] = set()
    for sentence in _split_sentences(text):
        norm = normalize_guardrail_text(sentence)
        zh_compact = normalize_zh_compact(sentence)
        _scan_sentence_poe2(sentence, norm, zh_compact, seen, issues)
        _scan_sentence_legacy_and_fallacies(sentence, norm, zh_compact, seen, issues)
    return issues


def _build_slot_alias_pattern() -> tuple[re.Pattern[str], dict[str, str]]:
    merged = {k.lower(): v for k, v in EQUIPMENT_SLOT_ALIASES.items() if len(k) >= 2}
    merged.update({k.lower(): v for k, v in _EXTRA_SLOT_ALIASES.items()})
    parts = [
        rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])" if k.isascii() else re.escape(k)
        for k in sorted(merged, key=len, reverse=True)
    ]
    return re.compile("|".join(parts), re.IGNORECASE), merged


_SLOT_ALIAS_RE, _SLOT_ALIAS_MAP = _build_slot_alias_pattern()


def extract_claimed_open_affix_slots(text: str) -> list[tuple[str, str]]:
    """Extract `[(normalized_slot, sentence), ...]` for positive open-affix / craftable slot claims."""
    if not text or not text.strip():
        return []
    results: list[tuple[str, str]] = []
    seen_slots: set[str] = set()
    for sentence in _split_sentences(text):
        if _OPEN_AFFIX_EXEMPT_RE.search(sentence):
            continue
        claims = list(_OPEN_AFFIX_CLAIM_RE.finditer(sentence))
        if not claims:
            continue
        for slot_m in _SLOT_ALIAS_RE.finditer(sentence):
            norm_slot = _SLOT_ALIAS_MAP.get(slot_m.group(0).lower()) or normalize_equipment_slot(slot_m.group(0))
            if not norm_slot or norm_slot in seen_slots:
                continue
            for cm in claims:
                dist = cm.start() - slot_m.end() if cm.start() >= slot_m.end() else slot_m.start() - cm.end()
                if 0 <= dist <= 25:
                    seen_slots.add(norm_slot)
                    results.append((norm_slot, sentence))
                    break
    return results


def detect_panel_or_delta_claims(text: str) -> list[str]:
    """Return concrete DPS/EHP panel or simulated delta claim snippets in assistant text."""
    cleaned = _strip_blockquotes(text)
    if not cleaned.strip():
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for pat in _PANEL_PATTERNS:
        for m in pat.finditer(cleaned):
            snippet = m.group(0).strip()
            key = snippet.lower()
            if snippet and key not in seen:
                seen.add(key)
                hits.append(snippet)
    return hits


def detect_trigger_breakpoint_claims(text: str) -> list[str]:
    """Return sentences asserting concrete trigger APS caps or CDR breakpoints."""
    if not text or not text.strip():
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for sentence in _split_sentences(text):
        if _TRIGGER_CTX_RE.search(sentence) and _TRIGGER_NUM_RE.search(sentence):
            snippet = sentence[:180]
            if snippet not in seen:
                seen.add(snippet)
                hits.append(snippet)
    return hits


def detect_build_source_in_text(text: str) -> tuple[bool, str | None]:
    """Detect whether text references a PoE1 build source and return `(has_source, source_hint)`."""
    if not text or not text.strip():
        return False, None
    for pat in _BUILD_SOURCE_PATTERNS:
        m = pat.search(text)
        if m is not None:
            return True, m.group(0).rstrip(".,);]")
    if _BUILD_BASE64_RE.search(text):
        return True, None
    return False, None


def detect_hesitation_claims(text: str) -> list[str]:
    """Return non-blockquote sentences containing unverified hedging/guessing fingerprints (V5)."""
    if not text or not text.strip():
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for sentence in _split_sentences(text):
        if _HESITATION_V3_EXEMPT_RE.search(sentence) or _HESITATION_CITED_EXEMPT_RE.search(sentence):
            continue
        if _HESITATION_ZH_RE.search(sentence) or _HESITATION_EN_RE.search(sentence):
            snippet = sentence[:180]
            if snippet not in seen:
                seen.add(snippet)
                hits.append(snippet)
    return hits


def extract_research_entities(text: str, limit: int = 3) -> list[str]:
    """Extract clean short PoE1 entity names (EN/CN) from a user prompt or long sentence."""
    if not text or not text.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _push(raw: str) -> None:
        if len(out) >= limit:
            return
        cand = re.sub(r"\s+", " ", raw.strip(" \t\r\n.,;:!?()（）「」『』《》\"'"))
        if len(cand) < 2 or len(cand) > 40:
            return
        low = cand.lower()
        if low in _QUERY_STOPWORDS or low in seen or low.startswith(("http://", "https://", "@")):
            return
        seen.add(low)
        out.append(cand)

    for m in _PAREN_EN_ENTITY_RE.finditer(text):
        _push(m.group(1))
    for m in _TITLE_EN_ENTITY_RE.finditer(text):
        _push(m.group(1))
    for m in _QUOTED_TERM_RE.finditer(text):
        inner = m.group(1)
        inner_en = _PAREN_EN_ENTITY_RE.search(inner)
        if inner_en:
            _push(inner_en.group(1))
        else:
            for part in re.split(r"[/、,，]", inner):
                cleaned = re.sub(r"[（(][^()（）]{1,40}[)）]", "", part).strip()
                _push(cleaned)
    if not out:
        for seg in _ZH_QUERY_SPLIT_RE.split(text):
            cand = seg.strip()
            if 2 <= len(cand) <= 12 and cand not in _ZH_QUERY_STOPWORDS:
                if not any(cand in existing for existing in out):
                    _push(cand)
    return out


def detect_mechanics_research_need(user_text: str, assistant_text: str = "") -> dict[str, Any]:
    """Detect whether user prompt asks for mechanics/interaction/PoB-discrepancy verification."""
    u = (user_text or "").strip()
    if not u:
        return {"needsResearch": False, "reasons": [], "suggestedQueries": []}

    reasons: list[str] = []
    for reason_id, pat in _MECH_INTENT_SPECS:
        if pat.search(u):
            reasons.append(reason_id)

    if not reasons:
        return {"needsResearch": False, "reasons": [], "suggestedQueries": []}

    queries = extract_research_entities(u, limit=3)
    if not queries and assistant_text:
        queries = extract_research_entities(assistant_text, limit=3)
    return {
        "needsResearch": True,
        "reasons": reasons,
        "suggestedQueries": queries,
    }
