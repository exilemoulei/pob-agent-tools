"""Guardrail scopes, TLS warning context, equipment slot aliases, and text matchers."""

from __future__ import annotations

import contextlib
import re
import sys
import threading
import unicodedata
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

from ..compat import ensure_utf8
from ..errors import HallucinationWarning

GAME_ID = "poe1"


class GuardrailScope(StrEnum):
    SLOT = "slot"
    CATEGORY = "category"
    ITEM_RAW = "item_raw"
    GEM_NAME = "gem_name"
    NOTABLE_OR_OIL = "notable_or_oil"
    CONFIG_VAR = "config_var"
    STAT_QUERY = "stat_query"
    CALC_QUERY = "calc_query"
    DATA_QUERY = "data_query"


ALL_QUERY_SCOPES: frozenset[GuardrailScope] = frozenset(GuardrailScope)
S = GuardrailScope

# ---------------------------------------------------------------------------
# Warning Context (stack-based thread-local, parity with pob2_cli)
# ---------------------------------------------------------------------------

_WARN_TLS = threading.local()


def _tls_stack() -> list[list[HallucinationWarning]]:
    stack = getattr(_WARN_TLS, "stack", None)
    if stack is None:
        stack = []
        _WARN_TLS.stack = stack
    return stack


def _tls_cli_warnings() -> list[HallucinationWarning]:
    buf = getattr(_WARN_TLS, "warnings", None)
    if buf is None:
        buf = []
        _WARN_TLS.warnings = buf
    return buf


def _tls_emitted_keys() -> set[tuple[str, str]]:
    keys = getattr(_WARN_TLS, "emitted_keys", None)
    if keys is None:
        keys = set()
        _WARN_TLS.emitted_keys = keys
    return keys


def _write_stderr_line(msg: str) -> None:
    ensure_utf8(sys.stderr)
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _record_warning(warning: HallucinationWarning, *, emit_stderr: bool = True) -> None:
    stack = _tls_stack()
    target_list = stack[-1] if stack else _tls_cli_warnings()
    key = (warning.rule_id, warning.trigger)
    if not any((w.rule_id, w.trigger) == key for w in target_list):
        target_list.append(warning)
    if emit_stderr:
        emitted = _tls_emitted_keys()
        if key not in emitted:
            emitted.add(key)
            _write_stderr_line(warning.format_stderr())


def reset_cli_warnings() -> None:
    _tls_cli_warnings().clear()
    _tls_emitted_keys().clear()
    stack = _tls_stack()
    if stack:
        stack[-1].clear()


@contextlib.contextmanager
def warning_context() -> Iterator[list[HallucinationWarning]]:
    stack = _tls_stack()
    bucket: list[HallucinationWarning] = []
    stack.append(bucket)
    try:
        yield bucket
    finally:
        if stack and stack[-1] is bucket:
            stack.pop()
        elif bucket in stack:
            stack.remove(bucket)


def get_active_warnings() -> list[HallucinationWarning]:
    stack = _tls_stack()
    src = stack[-1] if stack else _tls_cli_warnings()
    return list(src)


def get_active_warning_dicts() -> list[dict[str, Any]]:
    return [w.to_dict() for w in get_active_warnings()]


def emit_stderr_warnings_from_dicts(items: list[dict[str, Any]] | None) -> None:
    if not items:
        return
    for raw in items:
        if not isinstance(raw, dict):
            continue
        warning = HallucinationWarning.from_dict(raw, default_game=GAME_ID)
        _record_warning(warning, emit_stderr=True)


# ---------------------------------------------------------------------------
# Tolerant Equipment Slot Alias Normalization (P0-2)
# ---------------------------------------------------------------------------

EQUIPMENT_SLOT_ALIASES: dict[str, str] = {
    "ring1": "Ring 1",
    "ring_1": "Ring 1",
    "ring-1": "Ring 1",
    "ring 1": "Ring 1",
    "left ring": "Ring 1",
    "leftring": "Ring 1",
    "左戒指": "Ring 1",
    "左戒": "Ring 1",
    "戒指1": "Ring 1",
    "戒指 1": "Ring 1",
    "ring2": "Ring 2",
    "ring_2": "Ring 2",
    "ring-2": "Ring 2",
    "ring 2": "Ring 2",
    "right ring": "Ring 2",
    "rightring": "Ring 2",
    "右戒指": "Ring 2",
    "右戒": "Ring 2",
    "戒指2": "Ring 2",
    "戒指 2": "Ring 2",
    "ring3": "Ring 3",
    "ring_3": "Ring 3",
    "ring-3": "Ring 3",
    "ring 3": "Ring 3",
    "戒指3": "Ring 3",
    "戒指 3": "Ring 3",
    "chest": "Body Armour",
    "body": "Body Armour",
    "body_armour": "Body Armour",
    "body-armour": "Body Armour",
    "bodyarmour": "Body Armour",
    "body armour": "Body Armour",
    "body armor": "Body Armour",
    "衣服": "Body Armour",
    "胸甲": "Body Armour",
    "身甲": "Body Armour",
    "盔甲": "Body Armour",
    "helm": "Helmet",
    "helmet": "Helmet",
    "head": "Helmet",
    "头盔": "Helmet",
    "头部": "Helmet",
    "手套": "Gloves",
    "glove": "Gloves",
    "gloves": "Gloves",
    "hands": "Gloves",
    "boots": "Boots",
    "boot": "Boots",
    "feet": "Boots",
    "shoes": "Boots",
    "鞋子": "Boots",
    "靴子": "Boots",
    "amulet": "Amulet",
    "neck": "Amulet",
    "necklace": "Amulet",
    "项链": "Amulet",
    "护身符": "Amulet",
    "belt": "Belt",
    "waist": "Belt",
    "腰带": "Belt",
    "weapon1": "Weapon 1",
    "weapon_1": "Weapon 1",
    "weapon-1": "Weapon 1",
    "weapon 1": "Weapon 1",
    "mainhand": "Weapon 1",
    "main_hand": "Weapon 1",
    "main-hand": "Weapon 1",
    "main hand": "Weapon 1",
    "主手": "Weapon 1",
    "主手武器": "Weapon 1",
    "武器1": "Weapon 1",
    "武器 1": "Weapon 1",
    "weapon2": "Weapon 2",
    "weapon_2": "Weapon 2",
    "weapon-2": "Weapon 2",
    "weapon 2": "Weapon 2",
    "offhand": "Weapon 2",
    "off_hand": "Weapon 2",
    "off-hand": "Weapon 2",
    "off hand": "Weapon 2",
    "shield": "Weapon 2",
    "quiver": "Weapon 2",
    "副手": "Weapon 2",
    "副手武器": "Weapon 2",
    "盾牌": "Weapon 2",
    "箭袋": "Weapon 2",
    "武器2": "Weapon 2",
    "武器 2": "Weapon 2",
    "flask1": "Flask 1",
    "flask_1": "Flask 1",
    "flask-1": "Flask 1",
    "flask 1": "Flask 1",
    "药剂1": "Flask 1",
    "药水1": "Flask 1",
    "flask2": "Flask 2",
    "flask_2": "Flask 2",
    "flask-2": "Flask 2",
    "flask 2": "Flask 2",
    "药剂2": "Flask 2",
    "药水2": "Flask 2",
    "flask3": "Flask 3",
    "flask_3": "Flask 3",
    "flask-3": "Flask 3",
    "flask 3": "Flask 3",
    "药剂3": "Flask 3",
    "药水3": "Flask 3",
    "flask4": "Flask 4",
    "flask_4": "Flask 4",
    "flask-4": "Flask 4",
    "flask 4": "Flask 4",
    "药剂4": "Flask 4",
    "药水4": "Flask 4",
    "flask5": "Flask 5",
    "flask_5": "Flask 5",
    "flask-5": "Flask 5",
    "flask 5": "Flask 5",
    "药剂5": "Flask 5",
    "药水5": "Flask 5",
    "graft_head": "Head",
    "graft-head": "Head",
    "graft head": "Head",
    "grafthead": "Head",
    "肢体头部": "Head",
    "graft_body": "Body",
    "graft-body": "Body",
    "graft body": "Body",
    "graftbody": "Body",
    "肢体躯干": "Body",
    "graft_arm": "Arm",
    "graft-arm": "Arm",
    "graft arm": "Arm",
    "graftarm": "Arm",
    "肢体手臂": "Arm",
    "graft_leg": "Leg",
    "graft-leg": "Leg",
    "graft leg": "Leg",
    "graftleg": "Leg",
    "肢体腿部": "Leg",
}


def normalize_equipment_slot(slot: str | None) -> str | None:
    """Normalize common EN/ZH equipment slot aliases (e.g. `ring1`/`左戒指` -> `Ring 1`)."""
    if slot is None:
        return None
    raw = str(slot).strip()
    if not raw:
        return raw
    key = re.sub(r"\s+", " ", raw.lower())
    hit = EQUIPMENT_SLOT_ALIASES.get(key)
    if hit is not None:
        return hit
    compact = re.sub(r"[\s_\-]+", "", key)
    return EQUIPMENT_SLOT_ALIASES.get(compact, raw)


# ---------------------------------------------------------------------------
# Text Normalization & Token-Boundary Matchers
# ---------------------------------------------------------------------------

_POB_ESC_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_POE2_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_FOLD_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u3000": " ",
        "\u00a0": " ",
    }
)
_GEM_SPEC_TAIL_RE = re.compile(r":\d+(?::\d+)?\s*$")
_STAT_FILTER_TAIL_RE = re.compile(r"\s*(?:>=|<=|=)\s*[-+]?\d+(?:\.\d+)?\s*$")
_STAT_WEIGHT_TAIL_RE = re.compile(r"\s*\*\s*[-+]?\d+(?:\.\d+)?\s*$")


def normalize_guardrail_text(text: str) -> str:
    if not text:
        return ""
    s = _POB_ESC_RE.sub("", text)
    s = _POE2_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.translate(_FOLD_MAP).lower()
    return re.sub(r"\s+", " ", s).strip()


def normalize_zh_compact(text: str) -> str:
    s = normalize_guardrail_text(text)
    return re.sub(r"[\s·・.．:：()（）]", "", s)


_WORD_EN_CACHE: dict[str, re.Pattern[str]] = {}


def match_word_en(
    norm_text: str,
    phrase: str,
    exclude_re: re.Pattern[str] | None = None,
) -> str | None:
    key = phrase.lower()
    pat = _WORD_EN_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9_'-]){re.escape(key)}(?![a-z0-9_'-])")
        _WORD_EN_CACHE[key] = pat
    for m in pat.finditer(norm_text):
        if exclude_re is not None:
            win_start = max(0, m.start() - 24)
            win_end = min(len(norm_text), m.end() + 24)
            if exclude_re.search(norm_text[win_start:win_end]):
                continue
        return m.group(0)
    return None


def match_phrase_zh(
    raw_text: str,
    zh_phrase: str,
    exclude_compounds: tuple[str, ...] = (),
) -> str | None:
    compact = normalize_zh_compact(raw_text)
    target = normalize_zh_compact(zh_phrase)
    if not target:
        return None
    start = 0
    while True:
        idx = compact.find(target, start)
        if idx == -1:
            return None
        win = compact[max(0, idx - 5) : min(len(compact), idx + len(target) + 5)]
        if not any(normalize_zh_compact(comp) in win for comp in exclude_compounds):
            return zh_phrase
        start = idx + 1


# ---------------------------------------------------------------------------
# Item Header Sanitizer (skip random rare/magic titles, Unique ID, Flavour)
# ---------------------------------------------------------------------------

_ITEM_HEADER_KEY_RE = re.compile(
    r"^(?:Item Class|Rarity|Armour|Evasion|Energy Shield|Ward|Quality|Sockets|Item Level|"
    r"LevelReq|Requires|Unique ID|League|Crafted|Prefix|Suffix|Implicits|Variant|Version|"
    r"Selected Variant|Has Alt Variant|Radius|Limited to|Catalyst|CatalystQuality|"
    r"Flavour Text|Memory Strands|Talisman Tier|Rune Sockets|Charm Slots)\b",
    re.IGNORECASE,
)


def extract_scannable_item_parts(raw_text: str) -> tuple[str, str]:
    """Return `(scannable_text, base_line)` stripping random Rare/Magic Line-1 titles, Unique ID, and Flavour."""
    lines = (raw_text or "").splitlines()
    if not lines:
        return "", ""

    rarity = ""
    kept_lines: list[str] = []
    header_name_lines: list[str] = []
    in_header = True
    in_flavour = False
    base_line = ""

    def _flush_header_names() -> None:
        nonlocal base_line
        if not header_name_lines:
            return
        if rarity in ("RARE", "MAGIC") and len(header_name_lines) >= 2:
            base_line = header_name_lines[-1]
            kept_lines.append(base_line)
        else:
            base_line = header_name_lines[-1]
            kept_lines.extend(header_name_lines)
        header_name_lines.clear()

    for line in lines:
        stripped = line.strip()
        low = stripped.lower()

        if low.startswith("unique id:"):
            continue
        if low.startswith(("flavour text:", "{flavour}")):
            in_flavour = True
            continue
        if stripped == "--------":
            if in_header:
                _flush_header_names()
                in_header = False
            in_flavour = False
            continue
        if in_flavour:
            continue

        if in_header:
            if low.startswith("rarity:"):
                rarity = stripped.split(":", 1)[1].strip().upper()
                kept_lines.append(stripped)
                continue
            if low.startswith("implicits:"):
                _flush_header_names()
                in_header = False
                kept_lines.append(stripped)
                continue
            if _ITEM_HEADER_KEY_RE.match(stripped):
                _flush_header_names()
                kept_lines.append(stripped)
                continue
            if stripped:
                header_name_lines.append(stripped)
            continue

        if stripped:
            kept_lines.append(stripped)

    if in_header:
        _flush_header_names()

    return "\n".join(kept_lines), base_line


def _strip_scope_suffix(norm_text: str, scope: GuardrailScope) -> str:
    """Strip optional gem `:lvl:qual` or trade `=min`/`*weight` tails for EXACT matching."""
    cand = norm_text
    if scope == S.GEM_NAME:
        cand = _GEM_SPEC_TAIL_RE.sub("", cand).strip()
    elif scope == S.STAT_QUERY:
        cand = _STAT_FILTER_TAIL_RE.sub("", cand).strip()
        cand = _STAT_WEIGHT_TAIL_RE.sub("", cand).strip()
    return cand


def _match_rule_fields(
    raw_text: str,
    norm_text: str,
    exact_cand: str,
    zh_compact_exact: str,
    *,
    en_exact: tuple[str, ...],
    zh_exact: tuple[str, ...],
    en_words: tuple[str, ...],
    zh_phrases: tuple[str, ...],
    regex: re.Pattern[str] | None,
    exclude_en_re: re.Pattern[str] | None,
    exclude_zh_compounds: tuple[str, ...],
) -> str | None:
    if en_exact and exact_cand in en_exact:
        if exclude_en_re is None or not exclude_en_re.search(norm_text):
            return exact_cand

    if zh_exact and zh_compact_exact:
        for z in zh_exact:
            if zh_compact_exact == normalize_zh_compact(z):
                if not any(normalize_zh_compact(c) in zh_compact_exact for c in exclude_zh_compounds):
                    return z

    for phrase in en_words:
        hit = match_word_en(norm_text, phrase, exclude_en_re)
        if hit is not None:
            return hit

    for zh_p in zh_phrases:
        hit_zh = match_phrase_zh(raw_text, zh_p, exclude_zh_compounds)
        if hit_zh is not None:
            return hit_zh

    if regex is not None:
        raw_no_esc = _POB_ESC_RE.sub("", raw_text)
        m = regex.search(raw_no_esc)
        if m is not None:
            if exclude_en_re is None or not exclude_en_re.search(norm_text):
                return m.group(0).strip()

    return None
