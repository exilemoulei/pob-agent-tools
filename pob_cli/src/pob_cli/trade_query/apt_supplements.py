"""APT trade stat polarity (`better`) and Chinese stat template lookup supplements."""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "apt_trade_supplements.json.gz"

_CJK_BRACKET_RE = re.compile(r"[【】「」『』《》]")
_WATCHER_CN_RE = re.compile(r"受到\s*([^\s，,。]{1,10}?)\s*影响时")
_NUM_TOKEN_RE = re.compile(r"[+-]?(?:\{\d+\}|\d+(?:\.\d+)?(?:\s*[-–—~]\s*\d+(?:\.\d+)?)?)")
_COMPACT_STRIP_RE = re.compile(r"[\s+#%，,。．·・:：;；()（）\[\]\-–—]+")
_SPLIT_TOKEN_RE = re.compile(r"[\s，,。．·・:：;；()（）\[\]]+")


@dataclass(frozen=True)
class _AptTradeSupplements:
    loaded: bool
    polarity: dict[str, int]
    zh_exact: dict[str, str]
    zh_compact: dict[str, str]
    zh_placeholder_compact: dict[str, str]
    zh_items: tuple[tuple[str, str], ...]
    zh_placeholder_items: tuple[tuple[str, str], ...]


def _compact_zh_stat(text: str) -> str:
    return _COMPACT_STRIP_RE.sub("", text).lower()


def _normalize_zh_stat_literal(raw_folded: str) -> str:
    s = _CJK_BRACKET_RE.sub("", raw_folded).strip()
    s = _WATCHER_CN_RE.sub(r"被\1影响时", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_zh_stat_placeholder(literal_norm: str) -> str:
    return re.sub(r"\s+", " ", _NUM_TOKEN_RE.sub("#", literal_norm)).strip()


@lru_cache(maxsize=1)
def _load_apt_trade_supplements() -> _AptTradeSupplements:
    if not _DATA_PATH.is_file():
        return _AptTradeSupplements(
            loaded=False,
            polarity={},
            zh_exact={},
            zh_compact={},
            zh_placeholder_compact={},
            zh_items=(),
            zh_placeholder_items=(),
        )
    payload = json.loads(gzip.decompress(_DATA_PATH.read_bytes()).decode("utf-8"))
    raw_pol = payload.get("polarity") if isinstance(payload, dict) else None
    raw_zh = payload.get("zh") if isinstance(payload, dict) else None

    polarity: dict[str, int] = {}
    if isinstance(raw_pol, dict):
        for sid, val in raw_pol.items():
            if isinstance(sid, str) and val in (-1, 0, 1):
                polarity[sid] = int(val)

    zh_exact: dict[str, str] = {}
    zh_compact: dict[str, str] = {}
    zh_placeholder_compact: dict[str, str] = {}
    if isinstance(raw_zh, dict):
        for zh_k, en_ref in raw_zh.items():
            if not isinstance(zh_k, str) or not isinstance(en_ref, str):
                continue
            norm_k = re.sub(r"\s+", " ", _CJK_BRACKET_RE.sub("", zh_k).strip())
            if norm_k:
                zh_exact.setdefault(norm_k, en_ref)
                comp = _compact_zh_stat(norm_k)
                if comp:
                    zh_compact.setdefault(comp, en_ref)
                    if "#" in norm_k:
                        zh_placeholder_compact.setdefault(comp, en_ref)

    zh_items = tuple(sorted(zh_compact.items(), key=lambda kv: (len(kv[0]), kv[0])))
    zh_placeholder_items = tuple(
        sorted(zh_placeholder_compact.items(), key=lambda kv: (len(kv[0]), kv[0]))
    )
    return _AptTradeSupplements(
        loaded=True,
        polarity=polarity,
        zh_exact=zh_exact,
        zh_compact=zh_compact,
        zh_placeholder_compact=zh_placeholder_compact,
        zh_items=zh_items,
        zh_placeholder_items=zh_placeholder_items,
    )


def get_stat_polarity_info(stat_id: str | None) -> tuple[int, str | None]:
    """Return `(better, source)` where `better` is `1|-1|0` and `source` is `'apt'` when loaded."""
    sup = _load_apt_trade_supplements()
    if not sup.loaded:
        return 1, None
    if not stat_id:
        return 1, "apt"
    return sup.polarity.get(str(stat_id).strip(), 1), "apt"


def get_stat_polarity(stat_id: str | None) -> int:
    """Return trade stat roll polarity: `1` (higher is better, default), `-1` (lower is better), or `0` (neutral)."""
    better, _ = get_stat_polarity_info(stat_id)
    return better


def _match_compact_or_tokens(
    norm_text: str,
    exact_map: dict[str, str],
    compact_map: dict[str, str],
    items: tuple[tuple[str, str], ...],
    *,
    require_placeholder_exact: bool = False,
) -> str | None:
    hit = exact_map.get(norm_text)
    if hit and (not require_placeholder_exact or "#" in norm_text):
        return hit
    comp = _compact_zh_stat(norm_text)
    if not comp:
        return None
    hit_comp = compact_map.get(comp)
    if hit_comp:
        return hit_comp
    if len(comp) >= 4:
        for k, en_ref in items:
            if k.startswith(comp) or comp in k:
                return en_ref
    tokens = [t for raw_t in _SPLIT_TOKEN_RE.split(norm_text) if (t := _compact_zh_stat(raw_t))]
    if len(tokens) >= 2:
        for k, en_ref in items:
            if all(tok in k for tok in tokens):
                return en_ref
    return None


def lookup_apt_zh_stat(folded_text: str) -> str | None:
    """Look up a folded Chinese stat string in the bundled APT trade stat supplement.

    Preserves literal digits on both sides first so fixed-number templates (e.g.
    `1 个附加天赋为保镳`) match verbatim without collapsing distinct numbers; falls
    back to placeholder `#` normalization only against stored placeholder templates.
    """
    sup = _load_apt_trade_supplements()
    if not sup.zh_exact:
        return None
    literal = _normalize_zh_stat_literal(folded_text)
    if not literal:
        return None

    hit_lit = _match_compact_or_tokens(literal, sup.zh_exact, sup.zh_compact, sup.zh_items)
    if hit_lit:
        return hit_lit

    placeholder = _normalize_zh_stat_placeholder(literal)
    if placeholder and placeholder != literal:
        return _match_compact_or_tokens(
            placeholder,
            sup.zh_exact,
            sup.zh_placeholder_compact,
            sup.zh_placeholder_items,
            require_placeholder_exact=True,
        )
    return None
