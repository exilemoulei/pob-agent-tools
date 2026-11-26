"""Text folding, Traditional Chinese normalization, and legacy Awakened gem helpers."""

from __future__ import annotations

import re
import unicodedata

from .constants import (
    _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER,
    _CJK_RE,
    _FOLD_CHAR_MAP,
    _POB_ESCAPE_RE,
)
from .tables_han import HAN_FOLD
from .tables_tw import _TRAD_TO_SIMP_MAP, _TW_ALIAS_RE, TW_TO_EN_ALIASES

# Unihan's full Traditional -> Simplified fold, with the hand-picked PoE term choices on top
# (e.g. 鍊 -> 链 as in 链结, where Unihan picks 炼).
_HAN_FOLD_ALL = {**HAN_FOLD, **_TRAD_TO_SIMP_MAP}

_ZH_REPLICA_BARE_RE = re.compile(
    r"^[\s·・.．\-:：]*(?:[【\[（(]\s*(?:仿品|赝品|贗品)\s*[】\]）)]|(?:仿品|赝品|贗品))[\s·・.．\-:：]*$"
)
_ZH_REPLICA_PREFIX_RE = re.compile(
    r"^(?:[【\[（(]\s*(?:仿品|赝品|贗品)\s*[】\]）)]|(?:仿品|赝品|贗品))[\s·・.．\-:：]*(.+)$"
)
_ZH_REPLICA_SUFFIX_RE = re.compile(
    r"^(.+?)[\s·・.．\-:：]*(?:[【\[（(]\s*(?:仿品|赝品|贗品)\s*[】\]）)]|(?<=[\s·・.．\-:：】\]）)])(?:仿品|赝品|贗品))$"
)


def extract_zh_replica_stem(text: str | None, *, fold: bool = True) -> tuple[bool, str]:
    """Parse Chinese/mixed Replica item queries (`仿品X`, `仿品·X`, `X【仿品】`, `贗品．X`, `Replica 中文`) into `(is_replica, stem)`."""
    if not text:
        return False, ""
    s = (fold_han(text) if fold else str(text)).strip()
    if not s:
        return False, ""
    if s.lower().startswith("replica ") and _CJK_RE.search(s):
        return True, s[8:].strip(" \t·・.．-:：")
    if not any(tok in s for tok in ("仿品", "赝品", "贗品")):
        return False, ""
    if _ZH_REPLICA_BARE_RE.match(s):
        return True, ""
    m_pre = _ZH_REPLICA_PREFIX_RE.match(s)
    if m_pre:
        return True, m_pre.group(1).strip(" \t·・.．-:：")
    m_suf = _ZH_REPLICA_SUFFIX_RE.match(s)
    if m_suf:
        return True, m_suf.group(1).strip(" \t·・.．-:：")
    return False, ""


def is_legacy_awakened_gem(name: str) -> bool:
    """Return True iff `name` is a legacy Awakened support removed/drop-disabled in PoE1 3.28 (Mirage).

    Only Awakened Empower, Awakened Enhance, and Awakened Enlighten remain active as Exceptional gems.
    """
    if not name:
        return False
    cleaned = _POB_ESCAPE_RE.sub("", str(name)).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+support$", "", cleaned).strip()
    return cleaned.startswith("awakened ") and cleaned not in _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER


def normalize_tw_query(text: str) -> str:
    """Normalize Traditional Chinese (TW) PoE1 gem/unique aliases and common Trad->Simp characters."""
    if not text:
        return ""
    s = str(text)
    if not _CJK_RE.search(s) and "．" not in s and "・" not in s:
        return s

    stripped = s.strip()
    if stripped:
        dot_norm = stripped.replace("．", "·").replace("・", "·")
        exact = TW_TO_EN_ALIASES.get(dot_norm)
        if exact is not None:
            return exact

    out = s.replace("．", "·").replace("・", "·")
    if _TW_ALIAS_RE.search(out):
        parts: list[str] = []
        last_end = 0
        prev_was_match_end = -1
        for m in _TW_ALIAS_RE.finditer(out):
            start, end = m.span()
            if start > last_end:
                parts.append(out[last_end:start])
            rep = TW_TO_EN_ALIASES[m.group(0)]
            need_pad_l = (start == prev_was_match_end) or (
                start > 0 and out[start - 1].isascii() and out[start - 1].isalnum()
            )
            need_pad_r = end < len(out) and out[end].isascii() and out[end].isalnum()
            parts.append((" " if need_pad_l else "") + rep + (" " if need_pad_r else ""))
            last_end = end
            prev_was_match_end = end
        if last_end < len(out):
            parts.append(out[last_end:])
        out = "".join(parts)

    return out.translate(_TRAD_TO_SIMP_MAP)


def _fold_search_text(text: str) -> str:
    """Fold diacritics (NFKD), curly apostrophes/dashes, case and Traditional Chinese for matching.

    Every index column and every query goes through this, so both sides fold alike. Folding the
    query alone would break Simplified text that keeps a foldable character (覆盖 -> 复盖).
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.translate(_FOLD_CHAR_MAP).lower().translate(_HAN_FOLD_ALL)


def fold_han(text: str | None) -> str:
    """Fold Traditional Chinese characters to Simplified so both scripts compare equal.

    Apply it to both sides of a comparison: the engine shows Traditional (zh-rTW) labels under
    `pob lang zh-TW` or the legacy pob-zh.exe, while users mostly type Simplified. Only closes character-level gaps
    (聖宗 == 圣宗); names the two translations word differently need `english_for_zh`.
    """
    return str(text).translate(_HAN_FOLD_ALL) if text else ""
