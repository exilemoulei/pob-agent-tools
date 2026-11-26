"""<=250-char PoE1 negation regex compiler with mathematical 0-false-positive verification."""

from __future__ import annotations

import re
from typing import Any

from .corpus import load_corpus
from .rules import classify_map_mods

_SAMPLE_NUMS = ("18", "60", "100")
_FORBIDDEN_CHARS = frozenset('"!\\^$[](){}*+?')
_SEV_ORDER = {"brick": 0, "danger": 1, "avoid": 2}


def _instantiate_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        if "#" in ln:
            for num in _SAMPLE_NUMS:
                out.append(ln.replace("#", num))
            out.append(ln.replace("#", "-12"))
        else:
            out.append(ln)
    return out


def _build_safe_texts(corpus: dict[str, Any], excluded_ids: set[str]) -> list[str]:
    raw: list[str] = []
    for m in corpus["mods"]:
        if m["id"] not in excluded_ids:
            raw.extend(m["lines"])
    raw.extend(corpus.get("otherMods") or ())
    raw.extend(corpus.get("mapNames") or ())
    raw.extend(corpus.get("headers") or ())
    return _instantiate_lines(raw)


def _is_cjk_lang(client_lang: str) -> bool:
    return client_lang.startswith("zh")


def _clean_segment(text: str) -> str:
    return re.sub(r"[#%+0-9\-]+", " ", text).strip()


def _candidate_substrings(line: str, *, is_cjk: bool) -> list[str]:
    parts = [p.strip() for p in re.split(r"[#%\d,+]+", line) if p.strip()]
    min_len = 2 if is_cjk else 4
    seen: set[str] = set()
    cands: list[str] = []
    for part in parts:
        low = part.lower()
        n = len(low)
        for length in range(min_len, min(n, 28) + 1):
            for start in range(n - length + 1):
                sub = low[start : start + length]
                if sub[0].isspace() or sub[-1].isspace():
                    continue
                if any(ch in _FORBIDDEN_CHARS for ch in sub):
                    continue
                if not is_cjk and not any(ch.isalpha() for ch in sub):
                    continue
                if sub not in seen:
                    seen.add(sub)
                    cands.append(sub)
    cands.sort(key=lambda s: (len(s), s))
    return cands


def _candidate_pairs(line: str, *, is_cjk: bool) -> list[str]:
    cleaned = _clean_segment(line).lower()
    if is_cjk:
        chunks = [c for c in re.split(r"[\s，、。：:]+", cleaned) if len(c) >= 2]
        anchors: list[str] = []
        for ch in chunks:
            for length in (2, 3, 4):
                for i in range(len(ch) - length + 1):
                    anchors.append(ch[i : i + length])
    else:
        words = [w for w in re.findall(r"[a-z']+", cleaned) if len(w) >= 3]
        anchors = []
        for w in words:
            anchors.append(w[:4] if len(w) >= 5 else w)
            if len(w) >= 4:
                anchors.append(w)
    uniq: list[str] = []
    seen_a: set[str] = set()
    for a in anchors:
        if a not in seen_a:
            seen_a.add(a)
            uniq.append(a)
    pairs: list[str] = []
    seen_p: set[str] = set()
    pool = uniq[:18]
    for a in pool:
        pos_a = cleaned.find(a)
        if pos_a < 0:
            continue
        for b in pool:
            if a == b:
                continue
            pos_b = cleaned.rfind(b)
            if pos_b >= pos_a + len(a):
                pat = f"{a}.*{b}"
                if pat not in seen_p:
                    seen_p.add(pat)
                    pairs.append(pat)
    pairs.sort(key=lambda s: (len(s), s))
    return pairs


def _matches_all(rx: re.Pattern[str], targets: list[str]) -> bool:
    return all(rx.search(t) is not None for t in targets)


def _matches_none(rx: re.Pattern[str], safe_texts: list[str]) -> bool:
    return all(rx.search(s) is None for s in safe_texts)


def _synthesize_single_token(
    targets: list[str],
    raw_lines: list[str],
    safe_texts: list[str],
    *,
    is_cjk: bool,
) -> str | None:
    best: str | None = None
    for cand in _candidate_substrings(raw_lines[0], is_cjk=is_cjk):
        if best is not None and len(cand) >= len(best):
            break
        rx = re.compile(re.escape(cand), re.IGNORECASE)
        if _matches_all(rx, targets) and _matches_none(rx, safe_texts):
            best = cand
            break

    max_pair_len = len(best) if best is not None else 24
    for pat in _candidate_pairs(raw_lines[0], is_cjk=is_cjk):
        if len(pat) >= max_pair_len:
            break
        rx = re.compile(pat, re.IGNORECASE)
        if _matches_all(rx, targets) and _matches_none(rx, safe_texts):
            best = pat
            break

    if best is not None:
        return best

    # Guaranteed fallback: full normalized template line with `#` -> `.*`
    if len(raw_lines) == 1:
        full_pat = ".*".join(re.escape(seg) for seg in raw_lines[0].lower().split("#"))
        rx = re.compile(full_pat, re.IGNORECASE)
        if _matches_all(rx, targets) and _matches_none(rx, safe_texts):
            return full_pat
    return None


def _synthesize_mod_token(
    raw_lines: list[str],
    safe_texts: list[str],
    *,
    is_cjk: bool,
) -> str:
    targets = _instantiate_lines(raw_lines)
    shared = _synthesize_single_token(targets, raw_lines, safe_texts, is_cjk=is_cjk)
    if shared is not None:
        return shared
    parts: list[str] = []
    for ln in raw_lines:
        sub_tok = _synthesize_single_token(_instantiate_lines([ln]), [ln], safe_texts, is_cjk=is_cjk)
        if sub_tok is None:
            raise RuntimeError(f"Unable to synthesize 0-collision token for map mod line: {ln!r}")
        if sub_tok not in parts:
            parts.append(sub_tok)
    return "|".join(parts)


def _digit_range(lo: int) -> str:
    if lo >= 9:
        return "9"
    if lo == 8:
        return "[89]"
    return f"[{lo}-9]"


def min_value_regex(threshold: int) -> str:
    """Minimal PoE1 regex matching integers `>= threshold` (supports 1..999)."""
    n = max(1, int(threshold))
    if n >= 100:
        h, rem = divmod(n, 100)
        branches: list[str] = []
        if rem == 0:
            branches.append(f"{_digit_range(h)}\\d\\d")
        else:
            t, u = divmod(rem, 10)
            if u == 0:
                branches.append(f"{h}{_digit_range(t)}\\d")
            else:
                branches.append(f"{h}{t}{_digit_range(u)}")
                if t < 9:
                    branches.append(f"{h}{_digit_range(t + 1)}\\d")
            if h < 9:
                branches.append(f"{_digit_range(h + 1)}\\d\\d")
        return branches[0] if len(branches) == 1 else f"({'|'.join(branches)})"

    if n < 10:
        return f"({_digit_range(n)}|\\d\\d|\\d{{3}})"
    t, u = divmod(n, 10)
    branches = []
    if u == 0:
        branches.append(f"{_digit_range(t)}\\d")
    else:
        branches.append(f"{t}{_digit_range(u)}")
        if t < 9:
            branches.append(f"{_digit_range(t + 1)}\\d")
    branches.append("\\d{3}")
    return f"({'|'.join(branches)})"


def _positive_filters(client_lang: str, min_quant: int | None, min_pack: int | None) -> list[str]:
    tokens: list[str] = []
    if min_quant is not None and min_quant > 0:
        nr = min_value_regex(min_quant)
        if client_lang == "en":
            tokens.append(f'"m q.*\\+{nr}%"')
        elif client_lang == "zh-TW":
            tokens.append(f'"物品數量.*\\+{nr}%"')
        else:
            tokens.append(f'"物品数量.*\\+{nr}%"')
    if min_pack is not None and min_pack > 0:
        nr = min_value_regex(min_pack)
        if client_lang == "en":
            tokens.append(f'"pack.*\\+{nr}%"')
        elif client_lang == "zh-TW":
            tokens.append(f'"怪物群大小.*\\+{nr}%"')
        else:
            tokens.append(f'"怪物群规模.*\\+{nr}%"')
    return tokens


def _wrap_neg(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return f'"!{"|".join(tokens)}"'


def compile_map_regex(
    excluded_mods: list[dict[str, Any]],
    *,
    corpus: dict[str, Any],
    client_lang: str | None = None,
    max_len: int = 250,
    min_quant: int | None = None,
    min_pack: int | None = None,
) -> dict[str, Any]:
    """Compile a <=max_len PoE1 map regex with verified 0 false positives."""
    lang = client_lang or corpus["clientLang"]
    is_cjk = _is_cjk_lang(lang)
    mods_by_id = corpus["modsById"]
    ordered = sorted(
        excluded_mods,
        key=lambda m: (_SEV_ORDER.get(str(m.get("severity", "avoid")), 2), str(m["id"])),
    )
    excluded_ids = {str(m["id"]) for m in ordered}
    safe_texts = _build_safe_texts(corpus, excluded_ids)

    pos_tokens = _positive_filters(lang, min_quant, min_pack)
    pos_str = " ".join(pos_tokens)
    pos_overhead = (len(pos_str) + 1) if pos_str else 0

    compiled_entries: list[dict[str, Any]] = []
    for item in ordered:
        mid = str(item["id"])
        mod_rec = mods_by_id[mid]
        tok = _synthesize_mod_token(mod_rec["lines"], safe_texts, is_cjk=is_cjk)
        compiled_entries.append({**item, "token": tok, "lines": list(mod_rec["lines"])})

    # Verify every token mathematically against the entire safe corpus
    all_neg_branches: list[str] = []
    seen_all_branches: set[str] = set()
    for e in compiled_entries:
        for br in str(e["token"]).split("|"):
            if br and br not in seen_all_branches:
                seen_all_branches.add(br)
                all_neg_branches.append(br)
    collisions = 0
    if all_neg_branches:
        combined_rx = re.compile("|".join(all_neg_branches), re.IGNORECASE)
        collisions = sum(1 for s in safe_texts if combined_rx.search(s) is not None)
        if collisions != 0:
            raise RuntimeError(f"Map regex compiler invariant violated: {collisions} false positive(s) detected.")

    # Pack within max_len (brick first, then danger, then avoid), deduplicating shared tokens (0 extra cost).
    primary_tokens: list[str] = []
    seen_primary: set[str] = set()
    overflow_chunks: list[str] = []
    cur_overflow: list[str] = []
    seen_overflow: set[str] = set()

    for entry in compiled_entries:
        branches = [b for b in str(entry["token"]).split("|") if b]
        new_for_primary = [b for b in branches if b not in seen_primary]
        if not new_for_primary:
            entry["inPrimary"] = True
            continue
        if not cur_overflow:
            cand_primary = [*primary_tokens, *new_for_primary]
            cand_neg = _wrap_neg(cand_primary)
            total_len = len(cand_neg) + pos_overhead
            if total_len <= max_len:
                primary_tokens = cand_primary
                seen_primary.update(new_for_primary)
                entry["inPrimary"] = True
                continue
        entry["inPrimary"] = False
        new_for_ov = [b for b in new_for_primary if b not in seen_overflow]
        if not new_for_ov:
            continue
        cand_ov = [*cur_overflow, *new_for_ov]
        if cur_overflow and len(_wrap_neg(cand_ov)) > max_len:
            overflow_chunks.append(_wrap_neg(cur_overflow))
            cur_overflow = list(new_for_ov)
        else:
            cur_overflow = cand_ov
        seen_overflow.update(new_for_ov)

    if cur_overflow:
        overflow_chunks.append(_wrap_neg(cur_overflow))

    parts = [p for p in (_wrap_neg(primary_tokens), pos_str) if p]
    regex = " ".join(parts)

    return {
        "regex": regex,
        "length": len(regex),
        "maxLength": max_len,
        "overflowRegex": overflow_chunks,
        "clientLang": lang,
        "falsePositiveCollisions": collisions,
        "safeCorpusSize": len(safe_texts),
        "excludedMods": compiled_entries,
    }


def build_map_regex_result(
    probe: dict[str, Any] | None,
    *,
    tier: str = "both",
    mode: str = "safe",
    avoid_ids: list[str] | None = None,
    allow_ids: list[str] | None = None,
    preset_only: bool = False,
    client_lang: str = "en",
    max_len: int = 250,
    min_quant: int | None = None,
    min_pack: int | None = None,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """End-to-end classification + override application + 0-collision regex compilation."""
    corp = corpus or load_corpus(client_lang)
    avoid_set = set(avoid_ids or ())
    allow_set = set(allow_ids or ())

    excluded: list[dict[str, Any]] = []
    exempted: list[dict[str, Any]] = []
    reflect_exempt: dict[str, dict[str, Any]] = {}
    is_preset_only = bool(preset_only or probe is None)

    if is_preset_only:
        for mid in avoid_ids or ():
            if mid not in allow_set:
                excluded.append({"id": mid, "severity": "avoid", "reason": "Manually excluded via --avoid"})
    else:
        cls = classify_map_mods(probe, tier=tier, mode=mode, corpus=corp)
        seen_ids: set[str] = set()
        for row in cls["classifications"]:
            mid = row["id"]
            seen_ids.add(mid)
            if mid in allow_set:
                exempted.append({"id": mid, "severity": "safe", "reason": "Manually allowed via --allow"})
            elif mid in avoid_set:
                sev = row["severity"] if row["severity"] in ("brick", "danger") else "avoid"
                excluded.append({"id": mid, "severity": sev, "reason": f"{row['reason']} (forced via --avoid)"})
            elif row["excluded"]:
                excluded.append({"id": mid, "severity": row["severity"], "reason": row["reason"]})
            elif row["reason"].startswith("SAFE:"):
                exempted.append({"id": mid, "severity": "safe", "reason": row["reason"]})
                if isinstance(row.get("exemptMeta"), dict):
                    reflect_exempt[mid] = dict(row["exemptMeta"])
        for mid in avoid_ids or ():
            if mid not in seen_ids and mid not in allow_set:
                excluded.append({"id": mid, "severity": "avoid", "reason": "Manually excluded via --avoid"})

    compiled = compile_map_regex(
        excluded,
        corpus=corp,
        client_lang=corp["clientLang"],
        max_len=max_len,
        min_quant=min_quant,
        min_pack=min_pack,
    )
    return {
        **compiled,
        "tier": tier,
        "mode": mode,
        "presetOnly": is_preset_only,
        "corpusVersion": corp.get("corpusVersion", "3.30"),
        "langOverlay": corp.get("langOverlay", "none"),
        "cnOverlayActive": corp["cnOverlayActive"],
        "reflectExempt": reflect_exempt,
        "exemptedMods": exempted,
        "availablePresets": sorted(corp["modsById"]),
    }
