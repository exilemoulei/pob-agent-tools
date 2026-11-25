"""Drop-only / zero-weight slot blind-spot guidance and official trade stat filter builder."""

from __future__ import annotations

import re
from typing import Any

from .constants import _CJK_RE


def _pick_matching_stat_line(lines_en: list[str], lines_zh: list[str], q: str) -> str:
    clean_en = [str(s).strip() for s in lines_en if str(s).strip()]
    if not clean_en:
        return ""
    if not q or len(clean_en) == 1:
        return clean_en[0]
    if _CJK_RE.search(q):
        q_chars = [ch for ch in q if _CJK_RE.match(ch)]
        best_idx, best_score = 0, 0
        for idx, zh in enumerate(lines_zh[: len(clean_en)]):
            zh_s = str(zh or "")
            sc = (100 if q in zh_s else 0) + sum(1 for ch in q_chars if ch in zh_s)
            if sc > best_score:
                best_idx, best_score = idx, sc
        return clean_en[best_idx]

    q_toks = [t for t in re.findall(r"[a-z]+", q.lower()) if len(t) >= 3]
    if not q_toks:
        return clean_en[0]
    best_line, best_hits = clean_en[0], 0
    for ln in clean_en:
        ln_low = ln.lower()
        hits = sum(1 for t in q_toks if t in ln_low)
        if hits > best_hits:
            best_line, best_hits = ln, hits
    return best_line


def _quote_slot_arg(slot: str) -> str:
    s = slot.replace('"', "").strip()
    return f'"{s}"' if any(ch.isspace() for ch in s) else s


def _build_trade_stat_arg(
    query: str,
    results: list[dict[str, Any]],
    cands: list[dict[str, Any]],
) -> str:
    q = (query or "").replace('"', "").strip()
    if "=" in q:
        return q
    ref_stat = ""
    if cands:
        c0 = cands[0]
        lines_en = list(c0.get("stats") or str(c0.get("stat") or "").split("; "))
        ref_stat = _pick_matching_stat_line(lines_en, list(c0.get("statsZh") or ()), q)
    elif results:
        drop_row = next(
            (r for r in results if isinstance(r, dict) and not r.get("weights") and not r.get("bench_info")),
            results[0],
        )
        if isinstance(drop_row, dict):
            ref_stat = _pick_matching_stat_line(
                list(drop_row.get("stats") or ()),
                list(drop_row.get("stats_zh") or ()),
                q,
            )

    use_stat = ref_stat or q or "<mod>"
    if "(" in use_stat:
        return re.sub(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)", "#", use_stat)
    nums = list(re.finditer(r"(?<![a-zA-Z])([+-]?)(\d+(?:\.\d+)?)", use_stat))
    if len(nums) == 1:
        sign, num_s = nums[0].group(1), nums[0].group(2)
        if float(num_s) > 1:
            templated = use_stat[: nums[0].start()] + f"{sign}#" + use_stat[nums[0].end() :]
            return f"{templated}>={num_s}"
    return use_stat


def _attach_mod_drop_only_guidance(
    out: dict[str, Any],
    results: list[dict[str, Any]],
    drop_only_excluded: list[dict[str, Any]],
    query: str,
    slot: str | None,
) -> None:
    """Attach drop-only blind-spot notes & `pob trade search` Ground-Truth guidance for `pob data mod`."""
    q_low = (query or "").strip().lower()
    seen_cands: set[tuple[str, str]] = set()
    cands: list[dict[str, Any]] = []
    ordered_excluded = sorted(
        drop_only_excluded,
        key=lambda m: (
            0 if q_low and q_low in (str(m.get("affix") or "").lower(), str(m.get("id") or "").lower()) else 1,
            0 if m.get("drop_origin") in ("Delve", "Incursion", "Essence", "Veiled") else 1,
            -int(m.get("level") or 0),
        ),
    )
    for m in ordered_excluded:
        aff = str(m.get("affix") or m.get("id") or "-")
        stats_list = [str(s) for s in (m.get("stats") or []) if str(s).strip()]
        st0 = "; ".join(stats_list)
        key = (aff, st0)
        if key in seen_cands:
            continue
        seen_cands.add(key)
        cands.append(
            {
                "id": m.get("id"),
                "source": m.get("source"),
                "type": m.get("generation_type"),
                "affix": m.get("affix") or None,
                "origin": m.get("drop_origin") or m.get("source"),
                "knownSlots": m.get("applicable_types") or [],
                "stat": st0,
                "stats": stats_list,
                "statsZh": [str(z) for z in (m.get("stats_zh") or []) if z],
                "reason": "excluded-by-slot-table",
            }
        )
        if len(cands) >= 4:
            break

    has_drop_only_rows = any(
        isinstance(r, dict) and not r.get("weights") and not r.get("bench_info") for r in results
    )
    if cands:
        out["dropOnlyCandidates"] = cands
        out["dropOnlyReason"] = "excluded-by-slot-table"
    elif has_drop_only_rows:
        out["dropOnlyReason"] = "hit"

    stat_q = _build_trade_stat_arg(query, results, cands)

    if slot:
        slot_arg = _quote_slot_arg(slot)
        cmd = f'pob trade search --slot {slot_arg} --stat "{stat_q}" --fetch-limit 5 --pob off'
        preview = "; ".join(
            f"{c['affix'] or c['id']} [{c['origin']} {c['type']}]: {c['stat']}" for c in cands[:4]
        )
        if not results:
            if cands:
                out["hint"] = (
                    f"0 roll-weighted/bench mod matches for '{query}' on slot '{slot}', but drop-only/zero-weight "
                    f"explicit mod(s) match globally ({preview}). PoB Mods.dat stores 0 spawn weight (and incomplete "
                    f"static drop-slot tags) for Delve/Incursion/league-chest explicits, which may still legally exist "
                    f"on '{slot}' — verify live official trade Ground Truth via: `{cmd}` "
                    f"(if offline, mark `unverified (offline)`)."
                )
            else:
                prev_hint = str(out.get("hint") or "").strip()
                extra = (
                    f"Drop-only / Delve / Incursion / league explicits have 0 spawn weight in PoB Mods.dat and may "
                    f"still exist on '{slot}'; verify live official trade Ground Truth via: `{cmd}` "
                    f"(if offline, mark `unverified (offline)`)."
                )
                out["hint"] = f"{prev_hint} {extra}".strip() if prev_hint else extra
        elif cands or has_drop_only_rows:
            cand_note = f" (global zero-weight matches: {preview})" if preview else ""
            out["dropOnlyNote"] = (
                f"**[DROP-ONLY / SLOT-BLIND-SPOT]** PoB `Mods.dat` stores `0` spawn weight and incomplete per-slot tags "
                f"for Delve / Incursion / Essence / league-drop explicits{cand_note}. Empty weights or `--slot` filtering "
                f"NEVER proves an explicit affix cannot exist on `{slot}` — verify live official trade Ground Truth via: "
                f"`{cmd}` (if offline, mark `unverified (offline)`)."
            )
    elif has_drop_only_rows:
        cmd = f'pob trade search --slot <slot> --stat "{stat_q}" --fetch-limit 5 --pob off'
        out["dropOnlyNote"] = (
            f"**[DROP-ONLY WEIGHTS NOTE]** Rows marked `drop-only` have `0` currency spawn weight in PoB `Mods.dat` "
            f"(and PoB's static drop-slot tags omit many Delve/Incursion/league chest drops). Never conclude an affix "
            f"cannot exist on a gear slot from empty roll weights — verify live slot legality via: `{cmd}` "
            f"(if offline, mark `unverified (offline)`)."
        )
