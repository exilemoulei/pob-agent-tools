"""Enumerated trade stat option index for Cluster Jewels, Forbidden Flesh/Flame, Thread of Hope, and Impossible Escape."""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any

from .aliases import _CJK_RE, translate_stat_query

_PASSAGE_TAIL_RE = re.compile(r"\s+passage\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PIPE_TEMPLATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("enchant.stat_3948993189", re.compile(r"^added small passive skills grant:\s*(.+)$", re.IGNORECASE)),
    (
        "explicit.stat_2460506030",
        re.compile(r"^allocates\s+(.+?)\s+if you have the matching modifier on forbidden flame$", re.IGNORECASE),
    ),
    (
        "explicit.stat_1190333629",
        re.compile(r"^allocates\s+(.+?)\s+if you have the matching modifier on forbidden flesh$", re.IGNORECASE),
    ),
    ("explicit.stat_3642528642", re.compile(r"^only affects passives in\s+(.+?)\s+ring$", re.IGNORECASE)),
    (
        "explicit.stat_2422708892",
        re.compile(
            r"^passive skills in radius of\s+(.+?)\s+can be allocated without being connected to your tree$",
            re.IGNORECASE,
        ),
    ),
)

_TEMPLATE_BY_BASE_ID: dict[str, str] = {
    "enchant.stat_3948993189": "added small passive skills grant: #",
    "explicit.stat_2460506030": "allocates # if you have the matching modifier on forbidden flame",
    "explicit.stat_1190333629": "allocates # if you have the matching modifier on forbidden flesh",
    "explicit.stat_3642528642": "only affects passives in # ring",
    "explicit.stat_2422708892": "passive skills in radius of # can be allocated without being connected to your tree",
}

_RING_ZH_ALIASES: dict[str, str] = {
    "小": "small",
    "小环": "small",
    "小范围": "small",
    "极小": "small",
    "中": "medium",
    "中环": "medium",
    "中等": "medium",
    "大": "large",
    "大环": "large",
    "极大": "very large",
    "很大": "very large",
    "超大": "very large",
    "非常大": "very large",
    "巨大": "massive",
    "巨环": "massive",
    "海量": "massive",
}

_ZH_TEMPLATE_HINTS: tuple[tuple[str, str], ...] = (
    ("forbidden flame", "explicit.stat_2460506030"),
    ("禁断之炎", "explicit.stat_2460506030"),
    ("禁断火焰", "explicit.stat_2460506030"),
    ("forbidden flesh", "explicit.stat_1190333629"),
    ("禁断之肉", "explicit.stat_1190333629"),
    ("禁断肉体", "explicit.stat_1190333629"),
    ("added small passive skills grant", "enchant.stat_3948993189"),
    ("附加的小型天赋给予", "enchant.stat_3948993189"),
    ("小型天赋给予", "enchant.stat_3948993189"),
    ("only affects passives in", "explicit.stat_3642528642"),
    ("仅影响", "explicit.stat_3642528642"),
    ("圆环内", "explicit.stat_3642528642"),
    ("without being connected to your tree", "explicit.stat_2422708892"),
    ("passives in radius of", "explicit.stat_2422708892"),
    ("范围内的天赋无需连接", "explicit.stat_2422708892"),
)

_OPTION_INDEX_CACHE: dict[bytes, _OptionIndex] = {}
_OPTION_INDEX_LOCK = threading.Lock()


@dataclass(frozen=True)
class OptionStatMatch:
    id: str
    option_id: int | str
    text: str
    option_text: str
    score: float = 1.0


def split_pipe_stat_id(stat_id: str) -> tuple[str, int | str | None]:
    """Split a TradeSiteStats pipe-expanded stat ID ``base|opt`` into ``(base, option_id)``."""
    raw = str(stat_id or "").strip()
    if "|" not in raw:
        return raw, None
    base, opt_raw = raw.split("|", 1)
    opt_s = opt_raw.strip()
    if opt_s.isdigit():
        return base.strip(), int(opt_s)
    return base.strip(), opt_s


def _norm_opt_text(text: str) -> str:
    s = _PASSAGE_TAIL_RE.sub("", str(text or "").replace("\n", " ")).strip().lower()
    return _WS_RE.sub(" ", s)


def _extract_pipe_option_label(base_id: str, text: str) -> str:
    cleaned = _PASSAGE_TAIL_RE.sub("", str(text or "").replace("\n", " ")).strip()
    for target_base, pat in _PIPE_TEMPLATES:
        if base_id == target_base:
            m = pat.match(cleaned)
            if m:
                return m.group(1).strip()
    return cleaned


class _OptionIndex:
    __slots__ = ("by_base_opt", "by_exact_text", "by_template")

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        by_exact_text: dict[tuple[str, str], OptionStatMatch] = {}
        by_base_opt: dict[str, dict[str, OptionStatMatch]] = {}
        by_template: dict[str, list[str]] = {}

        for base_id, tmpl in _TEMPLATE_BY_BASE_ID.items():
            by_template.setdefault(tmpl, []).append(base_id)

        for entry in entries:
            raw_id = str(entry.get("id") or "").strip()
            raw_text = str(entry.get("text") or "").strip()
            if not raw_id or not raw_text:
                continue
            domain = str(entry.get("type") or raw_id.split(".", 1)[0]).strip().lower()

            if "|" in raw_id:
                base_id, opt_id = split_pipe_stat_id(raw_id)
                if opt_id is None:
                    continue
                opt_label = _extract_pipe_option_label(base_id, raw_text)
                match = OptionStatMatch(
                    id=base_id,
                    option_id=opt_id,
                    text=_PASSAGE_TAIL_RE.sub("", raw_text).strip(),
                    option_text=opt_label,
                    score=1.0,
                )
                norm_full = _norm_opt_text(raw_text)
                by_exact_text.setdefault((domain, norm_full), match)
                by_exact_text.setdefault(("", norm_full), match)
                opt_map = by_base_opt.setdefault(base_id, {})
                opt_map.setdefault(_norm_opt_text(opt_label), match)
                opt_map.setdefault(str(opt_id).lower(), match)
                continue

            opt_block = entry.get("option")
            options = opt_block.get("options") if isinstance(opt_block, dict) else None
            if not isinstance(options, list) or not options:
                continue
            tmpl_norm = _norm_opt_text(raw_text)
            by_template.setdefault(tmpl_norm, [])
            if raw_id not in by_template[tmpl_norm]:
                by_template[tmpl_norm].append(raw_id)
            opt_map = by_base_opt.setdefault(raw_id, {})
            for opt in options:
                if not isinstance(opt, dict) or opt.get("id") is None:
                    continue
                opt_id = opt["id"]
                opt_text = str(opt.get("text") or "").strip()
                if not opt_text:
                    continue
                full_text = raw_text.replace("#", opt_text, 1) if "#" in raw_text else f"{raw_text}: {opt_text}"
                match = OptionStatMatch(
                    id=raw_id,
                    option_id=opt_id,
                    text=full_text,
                    option_text=opt_text,
                    score=1.0,
                )
                opt_map.setdefault(_norm_opt_text(opt_text), match)
                opt_map.setdefault(str(opt_id).lower(), match)
                norm_full = _norm_opt_text(full_text)
                by_exact_text.setdefault((domain, norm_full), match)
                by_exact_text.setdefault(("", norm_full), match)

        self.by_exact_text = by_exact_text
        self.by_base_opt = by_base_opt
        self.by_template = by_template


def _option_entries_digest(entries: list[dict[str, Any]]) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    h.update(f"n={len(entries)}\n".encode())
    for entry in entries:
        raw_id = str(entry.get("id") or "")
        opt_block = entry.get("option")
        options = opt_block.get("options") if isinstance(opt_block, dict) else None
        if "|" in raw_id:
            h.update(f"{raw_id}\t{entry.get('type') or ''}\t{entry.get('text') or ''}\n".encode())
        elif isinstance(options, list) and options:
            h.update(f"{raw_id}\t{entry.get('type') or ''}\t{entry.get('text') or ''}".encode())
            for opt in options:
                if isinstance(opt, dict):
                    h.update(f"|{opt.get('id')}:{opt.get('text') or ''}".encode())
            h.update(b"\n")
    return h.digest()


def build_option_index(entries: list[dict[str, Any]]) -> _OptionIndex:
    if not entries:
        return _OptionIndex([])
    key = _option_entries_digest(entries)
    with _OPTION_INDEX_LOCK:
        cached = _OPTION_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
    idx = _OptionIndex(entries)
    with _OPTION_INDEX_LOCK:
        if len(_OPTION_INDEX_CACHE) >= 8:
            _OPTION_INDEX_CACHE.clear()
        _OPTION_INDEX_CACHE[key] = idx
    return idx


def _translate_option_value(raw_val: str, base_id: str) -> list[str]:
    cleaned = str(raw_val or "").strip()
    if not cleaned:
        return []
    probes: list[str] = [_norm_opt_text(cleaned)]
    if base_id == "explicit.stat_3642528642":
        norm_ring = cleaned.lower().removesuffix(" ring").strip()
        if norm_ring and norm_ring not in probes:
            probes.append(norm_ring)
    if _CJK_RE.search(cleaned):
        from ..game_data import english_for_zh, fold_han, resolve_player_slang

        folded = fold_han(cleaned)
        if base_id == "explicit.stat_3642528642":
            ring_en = _RING_ZH_ALIASES.get(folded.removesuffix("环").strip()) or _RING_ZH_ALIASES.get(folded)
            if ring_en and ring_en not in probes:
                probes.append(ring_en)
        slang = resolve_player_slang(folded) or resolve_player_slang(cleaned)
        if slang and _norm_opt_text(slang[0]) not in probes:
            probes.append(_norm_opt_text(slang[0]))
        try:
            en_ent = english_for_zh(cleaned) or (english_for_zh(folded) if folded != cleaned else None)
        except Exception:
            en_ent = None
        if en_ent and _norm_opt_text(en_ent) not in probes:
            probes.append(_norm_opt_text(en_ent))
        stat_en = translate_stat_query(cleaned)
        if stat_en and stat_en != cleaned and _norm_opt_text(stat_en) not in probes:
            probes.append(_norm_opt_text(stat_en))
    return probes


def _candidate_base_ids(text: str, idx: _OptionIndex, domain: str | None) -> list[str]:
    norm_t = _norm_opt_text(text)
    if norm_t in idx.by_base_opt:
        return [norm_t]
    candidates: list[str] = []
    # Exact template match
    for bid in idx.by_template.get(norm_t, ()):
        if not domain or bid.startswith(f"{domain}."):
            candidates.append(bid)
    if candidates:
        return candidates
    # Normalized hint match (supports slight wording differences like "Passives in Radius of #..." or ZH hints)
    probe_text = norm_t
    if _CJK_RE.search(text):
        from ..game_data import fold_han

        probe_text = fold_han(norm_t)
    for hint, bid in _ZH_TEMPLATE_HINTS:
        if hint in probe_text and bid in idx.by_base_opt:
            if not domain or bid.startswith(f"{domain}."):
                if bid not in candidates:
                    candidates.append(bid)
    if candidates:
        return candidates
    # Substring fallback over registered templates
    for tmpl, bids in idx.by_template.items():
        if norm_t in tmpl or tmpl in norm_t:
            for bid in bids:
                if (not domain or bid.startswith(f"{domain}.")) and bid not in candidates:
                    candidates.append(bid)
    return candidates


def resolve_option_stat(
    text: str,
    entries: list[dict[str, Any]],
    *,
    option_value: str | None = None,
    domain: str | None = None,
) -> OptionStatMatch | None:
    """Resolve an enumerated option stat (template + option value, or full expanded line)."""
    idx = build_option_index(entries)
    dom = (domain or "").strip().lower()
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    # 1. Explicit template + option_value (e.g. `Allocates # ... Forbidden Flame=Profane Bloom`)
    if option_value is not None and option_value.strip():
        base_ids = _candidate_base_ids(raw_text, idx, dom or None)
        for bid in base_ids:
            opt_map = idx.by_base_opt.get(bid) or {}
            probes = _translate_option_value(option_value, bid)
            for probe in probes:
                hit = opt_map.get(probe)
                if hit is not None:
                    return hit
            # Safe partial match for cluster jewel small passive descriptions (e.g. "Lightning Damage")
            for probe in probes:
                if len(probe) >= 4:
                    sub_hits = [m for k, m in opt_map.items() if probe in k]
                    if sub_hits:
                        sub_hits.sort(key=lambda m: len(m.option_text))
                        return OptionStatMatch(
                            id=sub_hits[0].id,
                            option_id=sub_hits[0].option_id,
                            text=sub_hits[0].text,
                            option_text=sub_hits[0].option_text,
                            score=0.92,
                        )
        return None

    # 2. Direct full expanded line (e.g. "Only Affects Passives in Very Large Ring")
    norm_full = _norm_opt_text(raw_text)
    hit = idx.by_exact_text.get((dom, norm_full))
    if hit is not None:
        return hit

    # Handle slight wording variant: "Passives in Radius of X can be Allocated..." -> "Passive Skills in Radius of X..."
    if norm_full.startswith("passives in radius of "):
        alt = "passive skills in radius of " + norm_full[len("passives in radius of ") :]
        hit = idx.by_exact_text.get((dom, alt))
        if hit is not None:
            return hit

    # Handle double-line cluster enchant ("Added Small Passive Skills grant: +12% to Chaos Resistance")
    if "added small passive skills grant:" in norm_full:
        after = norm_full.split("added small passive skills grant:", 1)[1].strip()
        if after:
            return resolve_option_stat(
                "enchant.stat_3948993189",
                entries,
                option_value=after,
                domain=dom or "enchant",
            )
    return None
