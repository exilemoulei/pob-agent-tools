"""Input checking, free-text trap scanning, and PoE2 timeless/class guards."""

from __future__ import annotations

import re
from typing import Any

from ..errors import CrossGameHallucinationError, HallucinationWarning, LegacyPatchTrapError
from .build_gate import URL_GATE_RULES
from .rules_false_friends import SAME_NAME_FALSE_FRIENDS_POE1
from .rules_legacy import LEGACY_PATCH_TRAPS_POE1
from .rules_poe2 import (
    _POE2_CLASS_ASC_EN,
    _POE2_CLASS_ASC_ZH,
    POE2_ONLY_ASCENDANCIES_EN,
    POE2_ONLY_CLASSES_EN,
    POE2_ONLY_CLASSES_ZH,
    POE2_ONLY_REVERSE_TRAPS_FOR_POB1,
    POE2_ONLY_TIMELESS_JEWELS_EN,
    POE2_ONLY_TIMELESS_JEWELS_ZH,
    TrapRule,
)
from .warnings import (
    GAME_ID,
    GuardrailScope,
    S,
    _match_rule_fields,
    _record_warning,
    _strip_scope_suffix,
    extract_scannable_item_parts,
    match_phrase_zh,
    match_word_en,
    normalize_guardrail_text,
    normalize_zh_compact,
)

_FREE_TEXT_EXCLUDED_POE2_RULE_IDS = frozenset({"POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"})
_FREE_TEXT_ASC_ZH = (
    "风暴编织者",
    "时空术士",
    "古灵使徒",
    "宝石军团",
    "血法师",
    "鲜血法师",
    "狱火师",
    "奇塔弗铁匠",
    "奇塔弗的铁匠",
    "夏乌拉侍僧",
    "夏乌拉的侍僧",
    "猎巫人",
    "祈求者",
)
_STANDALONE_FF_ABBR = {
    "FF_POE1_ELDRITCH_BATTERY": re.compile(r"(?<![A-Za-z0-9_])EB(?![A-Za-z0-9_])"),
    "FF_POE1_CHAOS_INOCULATION": re.compile(r"(?<![A-Za-z0-9_])CI(?![A-Za-z0-9_])"),
    "FF_POE1_MIND_OVER_MATTER": re.compile(r"(?<![A-Za-z0-9_])MoM(?![A-Za-z0-9_])"),
}


def _match_trap_rule(
    raw_text: str,
    norm_text: str,
    exact_cand: str,
    zh_compact_exact: str,
    rule: TrapRule,
) -> str | None:
    return _match_rule_fields(
        raw_text,
        norm_text,
        exact_cand,
        zh_compact_exact,
        en_exact=rule.en_exact,
        zh_exact=rule.zh_exact,
        en_words=rule.en_words,
        zh_phrases=rule.zh_phrases,
        regex=rule.regex,
        exclude_en_re=rule.exclude_en_re,
        exclude_zh_compounds=rule.exclude_zh_compounds,
    )


def check_input(
    text: str | None,
    *,
    scope: GuardrailScope,
    expected_game: str = GAME_ID,
    emit_stderr: bool = True,
    block_legacy_gems: bool = False,
) -> list[HallucinationWarning]:
    """Validate user input against Table D PoE2 reverse traps, PoE1 legacy patch traps, and false friends."""
    if not text or expected_game != "poe1":
        return []
    raw = str(text).strip()
    if not raw:
        return []

    norm = normalize_guardrail_text(raw)
    exact_cand = _strip_scope_suffix(norm, scope)
    zh_exact_raw = _strip_scope_suffix(raw, scope) if scope in (S.GEM_NAME, S.STAT_QUERY) else raw
    zh_compact_exact = normalize_zh_compact(zh_exact_raw)

    for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
        if scope not in rule.scopes:
            continue
        matched = _match_trap_rule(raw, norm, exact_cand, zh_compact_exact, rule)
        if matched is not None:
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=matched,
                error=rule.error,
                remediation=rule.remediation,
                equivalent_command=rule.equivalent_command,
                rule_id=rule.rule_id,
            )

    warnings: list[HallucinationWarning] = []
    seen_ids: set[str] = set()

    for prule in LEGACY_PATCH_TRAPS_POE1:
        if scope not in prule.scopes:
            continue
        hit = _match_rule_fields(
            raw,
            norm,
            exact_cand,
            zh_compact_exact,
            en_exact=prule.en_exact,
            zh_exact=prule.zh_exact,
            en_words=prule.en_words,
            zh_phrases=prule.zh_phrases,
            regex=prule.regex,
            exclude_en_re=prule.exclude_en_re,
            exclude_zh_compounds=prule.exclude_zh_compounds,
        )
        if hit is not None:
            if block_legacy_gems and prule.hard_block_on_mutate:
                raise LegacyPatchTrapError(
                    trigger=hit,
                    patch_version=prule.patch_version,
                    error=prule.summary,
                    remediation=prule.remediation,
                    equivalent_command=prule.equivalent_command,
                    rule_id=prule.rule_id,
                )
            if prule.rule_id not in seen_ids:
                seen_ids.add(prule.rule_id)
                w = HallucinationWarning(
                    rule_id=prule.rule_id,
                    game="poe1",
                    category="legacy_patch",
                    trigger=hit,
                    summary=prule.summary,
                    poe1_behavior=prule.remediation or prule.summary,
                    poe2_behavior="",
                    canonical_term=prule.canonical_term,
                    patch_version=prule.patch_version,
                    remediation=prule.remediation or None,
                )
                _record_warning(w, emit_stderr=emit_stderr)
                warnings.append(w)

    for ff_rule in SAME_NAME_FALSE_FRIENDS_POE1:
        if scope not in ff_rule.scopes:
            continue
        hit = _match_rule_fields(
            raw,
            norm,
            exact_cand,
            zh_compact_exact,
            en_exact=ff_rule.en_exact,
            zh_exact=ff_rule.zh_exact,
            en_words=ff_rule.en_words,
            zh_phrases=ff_rule.zh_phrases,
            regex=None,
            exclude_en_re=ff_rule.exclude_en_re,
            exclude_zh_compounds=ff_rule.exclude_zh_compounds,
        )
        if hit is not None and ff_rule.rule_id not in seen_ids:
            seen_ids.add(ff_rule.rule_id)
            w = HallucinationWarning(
                rule_id=ff_rule.rule_id,
                game="poe1",
                category="false_friend",
                trigger=hit,
                summary=ff_rule.summary,
                poe1_behavior=ff_rule.poe1_behavior,
                poe2_behavior=ff_rule.poe2_behavior,
                canonical_term=ff_rule.canonical_term,
            )
            _record_warning(w, emit_stderr=emit_stderr)
            warnings.append(w)

    return warnings


def check_input_item_raw(
    raw_text: str | None,
    *,
    expected_game: str = GAME_ID,
    emit_stderr: bool = True,
) -> list[HallucinationWarning]:
    """Scan raw PoE item clipboard text after stripping random Rare/Magic Line-1 titles, Unique ID, and Flavour."""
    if not raw_text or expected_game != "poe1":
        return []
    scannable, base_line = extract_scannable_item_parts(raw_text)
    warnings: list[HallucinationWarning] = []
    if base_line:
        warnings.extend(check_input(base_line, scope=S.ITEM_RAW, expected_game=expected_game, emit_stderr=emit_stderr))
    if scannable:
        warnings.extend(check_input(scannable, scope=S.ITEM_RAW, expected_game=expected_game, emit_stderr=emit_stderr))
    return warnings


def _scan_poe2_free_text_traps(raw: str, norm: str, zh_compact: str) -> list[dict[str, Any]]:
    poe2_traps: list[dict[str, Any]] = []
    seen_poe2: set[str] = set()

    for url_m in re.finditer(r"https?://[^\s<>\"']+|pob2://[^\s<>\"']+", raw, re.IGNORECASE):
        cand_url = url_m.group(0).rstrip(".,);]")
        for ur in URL_GATE_RULES:
            if ur.belongs_to == "poe2" and ur.pattern.search(cand_url) and ur.rule_id not in seen_poe2:
                seen_poe2.add(ur.rule_id)
                poe2_traps.append(
                    {
                        "ruleId": ur.rule_id,
                        "trigger": cand_url,
                        "error": f"URL matches {ur.label} (PoE2), not PoE1.",
                        "remediation": ur.counterpart_hint,
                    }
                )

    for rule in POE2_ONLY_REVERSE_TRAPS_FOR_POB1:
        if rule.rule_id in _FREE_TEXT_EXCLUDED_POE2_RULE_IDS or rule.rule_id in seen_poe2:
            continue
        hit = _match_trap_rule(raw, norm, norm, zh_compact, rule)
        if hit is None and rule.rule_id == "POE2_SPIRIT_RESERVATION":
            hit = match_phrase_zh(raw, "精魂", ("精魂之盾",))
        if hit is None and rule.rule_id == "POE2_WAYSTONE":
            hit = match_word_en(norm, "waystone", rule.exclude_en_re) or match_word_en(
                norm, "waystones", rule.exclude_en_re
            )
        if hit is not None:
            seen_poe2.add(rule.rule_id)
            poe2_traps.append(
                {
                    "ruleId": rule.rule_id,
                    "trigger": hit,
                    "error": rule.error,
                    "remediation": rule.remediation,
                    "equivalentCommand": rule.equivalent_command,
                }
            )

    if "POE2_TIMELESS_JEWEL_IN_POB1" not in seen_poe2:
        tj_hit = next((p for p in POE2_ONLY_TIMELESS_JEWELS_EN if match_word_en(norm, p) is not None), None)
        if tj_hit is None:
            tj_hit = next((z for z in POE2_ONLY_TIMELESS_JEWELS_ZH if match_phrase_zh(raw, z) is not None), None)
        if tj_hit is not None:
            seen_poe2.add("POE2_TIMELESS_JEWEL_IN_POB1")
            poe2_traps.append(
                {
                    "ruleId": "POE2_TIMELESS_JEWEL_IN_POB1",
                    "trigger": tj_hit,
                    "error": f"`{tj_hit}` is a PoE2 timeless/radius jewel and does not exist in PoE1.",
                    "remediation": "Use `pob timeless list` for valid PoE1 Historic jewels, or `pob2` for PoE2.",
                    "equivalentCommand": "pob timeless list",
                }
            )

    if "POE2_CLASS_IN_POB1" not in seen_poe2:
        asc_hit: str | None = None
        for asc in sorted(POE2_ONLY_ASCENDANCIES_EN, key=len, reverse=True):
            if asc in ("titan", "oracle", "shaman", "amazon", "lich"):
                continue
            if match_word_en(norm, asc) is not None:
                asc_hit = asc
                break
        if asc_hit is None:
            asc_hit = next((z for z in _FREE_TEXT_ASC_ZH if match_phrase_zh(raw, z) is not None), None)
        if asc_hit is not None:
            seen_poe2.add("POE2_CLASS_IN_POB1")
            poe2_traps.append(
                {
                    "ruleId": "POE2_CLASS_IN_POB1",
                    "trigger": asc_hit,
                    "error": f"`{asc_hit}` is a PoE2-only ascendancy and does not exist in PoE1.",
                    "remediation": "PoE1 classes are Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion.",
                }
            )

    return poe2_traps


def _scan_patch_and_ff_traps(
    raw: str,
    norm: str,
    zh_compact: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    patch_traps: list[dict[str, Any]] = []
    seen_patch: set[str] = set()
    for prule in LEGACY_PATCH_TRAPS_POE1:
        hit = _match_rule_fields(
            raw,
            norm,
            norm,
            zh_compact,
            en_exact=prule.en_exact,
            zh_exact=prule.zh_exact,
            en_words=prule.en_words,
            zh_phrases=prule.zh_phrases,
            regex=prule.regex,
            exclude_en_re=prule.exclude_en_re,
            exclude_zh_compounds=prule.exclude_zh_compounds,
        )
        if hit is not None and prule.rule_id not in seen_patch:
            seen_patch.add(prule.rule_id)
            patch_traps.append(
                {
                    "ruleId": prule.rule_id,
                    "patchVersion": prule.patch_version,
                    "category": "legacy_patch",
                    "trigger": hit,
                    "canonicalTerm": prule.canonical_term,
                    "warning": prule.summary,
                    "remediation": prule.remediation,
                    "equivalentCommand": prule.equivalent_command,
                }
            )

    false_friends: list[dict[str, Any]] = []
    seen_ff: set[str] = set()
    for ff_rule in SAME_NAME_FALSE_FRIENDS_POE1:
        hit = _match_rule_fields(
            raw,
            norm,
            norm,
            zh_compact,
            en_exact=ff_rule.en_exact,
            zh_exact=ff_rule.zh_exact,
            en_words=ff_rule.en_words,
            zh_phrases=ff_rule.zh_phrases,
            regex=_STANDALONE_FF_ABBR.get(ff_rule.rule_id),
            exclude_en_re=ff_rule.exclude_en_re,
            exclude_zh_compounds=ff_rule.exclude_zh_compounds,
        )
        if hit is not None and ff_rule.rule_id not in seen_ff:
            seen_ff.add(ff_rule.rule_id)
            false_friends.append(
                {
                    "ruleId": ff_rule.rule_id,
                    "category": "false_friend",
                    "trigger": hit,
                    "canonicalTerm": ff_rule.canonical_term,
                    "warning": ff_rule.summary,
                    "poe1Behavior": ff_rule.poe1_behavior,
                    "poe2Behavior": ff_rule.poe2_behavior,
                }
            )

    return patch_traps, false_friends


def scan_text_traps(text: str | None) -> dict[str, Any]:
    """Non-blocking scan of arbitrary EN/ZH prompt text for PoE2 traps, legacy 3.23-3.25 traps, and false friends."""
    raw = str(text or "").strip()
    if not raw:
        return {"poe2Traps": [], "patchTraps": [], "falseFriends": [], "warnings": []}

    norm = normalize_guardrail_text(raw)
    zh_compact = normalize_zh_compact(raw)

    poe2_traps = _scan_poe2_free_text_traps(raw, norm, zh_compact)
    patch_traps, false_friends = _scan_patch_and_ff_traps(raw, norm, zh_compact)

    combined_warnings: list[dict[str, Any]] = []
    for t in poe2_traps:
        combined_warnings.append(
            {
                "severity": "BLOCK",
                "tag": "[POE1-CROSS-GAME-BLOCK]",
                "ruleId": t["ruleId"],
                "trigger": t["trigger"],
                "message": f"{t['error']} {t.get('remediation') or ''}".strip(),
            }
        )
    for p in patch_traps:
        combined_warnings.append(
            {
                "severity": "PATCH",
                "tag": "[POE1-PATCH-WARN]",
                "ruleId": p["ruleId"],
                "trigger": p["trigger"],
                "message": p["warning"],
            }
        )
    for f in false_friends:
        combined_warnings.append(
            {
                "severity": "TRAP",
                "tag": "[POE1-TRAP-WARN]",
                "ruleId": f["ruleId"],
                "trigger": f["trigger"],
                "message": f["warning"],
            }
        )

    return {
        "poe2Traps": poe2_traps,
        "patchTraps": patch_traps,
        "falseFriends": false_friends,
        "warnings": combined_warnings,
    }


def check_poe2_timeless_jewel(jewel_name: str | None, *, expected_game: str = GAME_ID) -> None:
    """Hard-block PoE2-only timeless / radius jewels in `pob timeless search`."""
    if not jewel_name or expected_game != "poe1":
        return
    raw = str(jewel_name).strip()
    if not raw:
        return

    norm = normalize_guardrail_text(raw)
    matched: str | None = None
    for phrase in POE2_ONLY_TIMELESS_JEWELS_EN:
        if norm == phrase or match_word_en(norm, phrase) is not None:
            matched = raw
            break
    if matched is None:
        compact = normalize_zh_compact(raw)
        for zh in POE2_ONLY_TIMELESS_JEWELS_ZH:
            if compact == normalize_zh_compact(zh) or match_phrase_zh(raw, zh) is not None:
                matched = zh
                break

    if matched is not None:
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=matched,
            error=f"Jewel `{raw}` is a Path of Exile 2 timeless/radius jewel and does not exist in PoE1 `pob timeless`.",
            remediation=(
                "Use `pob2` for PoE2 timeless/radius jewels, or run `pob timeless list` "
                "for supported PoE1 Historic jewels (e.g. `Lethal Pride`, `Brutal Restraint`, `Heroic Tragedy`)."
            ),
            equivalent_command="pob timeless list",
            rule_id="POE2_TIMELESS_JEWEL_IN_POB1",
        )


def check_poe2_class_or_ascendancy(
    value: str | None,
    *,
    field: str = "class",
    expected_game: str = GAME_ID,
) -> None:
    """Hard-block PoE2-only character classes and ascendancies in PoE1 `new` / `tree set-class` / `batch`."""
    if not value or expected_game != "poe1":
        return
    raw = str(value).strip()
    if not raw:
        return

    norm = normalize_guardrail_text(raw)
    zh_compact = normalize_zh_compact(raw)
    if norm in _POE2_CLASS_ASC_EN or (zh_compact and zh_compact in _POE2_CLASS_ASC_ZH):
        is_cls = norm in POE2_ONLY_CLASSES_EN or zh_compact in {
            normalize_zh_compact(x) for x in POE2_ONLY_CLASSES_ZH
        }
        kind_label = "class" if is_cls else "ascendancy"
        equiv = f'pob2 new --class "{raw}"' if field == "class" and is_cls else f'pob2 tree set-class "{raw}"'
        raise CrossGameHallucinationError(
            game="poe1",
            detected_game="poe2",
            trigger=raw,
            error=f"`{raw}` is a Path of Exile 2 {kind_label} and does not exist in PoE1.",
            remediation=(
                "Use `pob2` for PoE2 classes/ascendancies, or pick a PoE1 class "
                "(Marauder, Duelist, Ranger, Shadow, Witch, Templar, Scion)."
            ),
            equivalent_command=equiv,
            rule_id="POE2_CLASS_IN_POB1",
        )
