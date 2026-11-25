"""Compact sweep readouts: JSON-safe headline summaries and keyword-matched calcs rows."""

from __future__ import annotations

import math
import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, fmt_num, strip_pob_escapes

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _json_safe_num(val: Any) -> float | str | None:
    if val is None:
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None:
        return str(val)
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


def _safe_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    """Compute elemental max hit (`min(Fire, Cold, Lightning)`) safely across `inf`/`NaN`/None."""
    vals: list[float] = []
    for k in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw_v = stats.get(k)
        if raw_v is None:
            continue
        parsed = _parse_numeric_or_none(raw_v)
        if parsed is not None and not math.isnan(parsed):
            vals.append(parsed)
    if not vals:
        return None
    m = min(vals)
    return m if math.isfinite(m) else fmt_num(m)


def _compact_sweep_summary(digest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, RFC-8259-safe offence/defence summary from a `capture_digest` dict."""
    stats = digest.get("stats") or {}
    return {
        "primaryDpsKey": digest.get("primaryDpsKey"),
        "primaryDps": digest.get("primaryDps"),
        "fullDps": _json_safe_num(stats.get("FullDPS")),
        "combinedDps": _json_safe_num(stats.get("CombinedDPS")),
        "totalDps": _json_safe_num(stats.get("TotalDPS")),
        "totalDotDps": _json_safe_num(stats.get("TotalDotDPS")),
        "minionCombinedDps": _json_safe_num(stats.get("MinionCombinedDPS")),
        "averageDamage": _json_safe_num(stats.get("AverageDamage")),
        "speed": _json_safe_num(stats.get("Speed")),
        "critChance": _json_safe_num(stats.get("CritChance")),
        "critMultiplier": _json_safe_num(stats.get("CritMultiplier")),
        "hitChance": _json_safe_num(stats.get("HitChance")),
        "manaCost": _json_safe_num(stats.get("ManaCost")),
        "totalEhp": digest.get("totalEhp"),
        "life": digest.get("life"),
        "energyShield": _json_safe_num(stats.get("EnergyShield")),
        "mana": _json_safe_num(stats.get("Mana")),
        "manaUnreserved": _json_safe_num(stats.get("ManaUnreserved")),
        "armour": _json_safe_num(stats.get("Armour")),
        "evasion": _json_safe_num(stats.get("Evasion")),
        "physMaxHit": _json_safe_num(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": _safe_ele_max_hit(stats),
        "fireMaxHit": _json_safe_num(stats.get("FireMaximumHitTaken")),
        "coldMaxHit": _json_safe_num(stats.get("ColdMaximumHitTaken")),
        "lightningMaxHit": _json_safe_num(stats.get("LightningMaximumHitTaken")),
        "chaosMaxHit": _json_safe_num(stats.get("ChaosMaximumHitTaken")),
        "fireResist": _json_safe_num(stats.get("FireResist")),
        "coldResist": _json_safe_num(stats.get("ColdResist")),
        "lightningResist": _json_safe_num(stats.get("LightningResist")),
        "chaosResist": _json_safe_num(stats.get("ChaosResist")),
        "spellSuppression": _json_safe_num(stats.get("EffectiveSpellSuppressionChance")),
        "blockChance": _json_safe_num(stats.get("EffectiveBlockChance")),
        "spellBlockChance": _json_safe_num(stats.get("EffectiveSpellBlockChance")),
        "points": (digest.get("buildInfo") or {}).get("points", {}),
    }


def _extract_compact_calcs(
    worker: BridgeWorker,
    calc_keywords: list[str],
    *,
    limit_per_keyword: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch `get_calcs` ONCE and extract compact matching row values (no breakdown dumps)."""
    has_cjk = any(_CJK_RE.search(str(kw)) for kw in calc_keywords)
    toggled_translate = False
    if has_cjk:
        caps = getattr(worker, "caps", None) or {}
        if caps.get("translateToggle", not caps):
            tracked = getattr(worker, "translate_enabled", None)
            if tracked is None:
                info_dict = getattr(worker, "hello_info", None) or getattr(worker, "info", None) or {}
                if isinstance(info_dict, dict) and "translate" in info_dict:
                    tracked = info_dict.get("translate")
            if tracked is False:
                try:
                    worker.call("set_translate", {"enabled": True})
                    toggled_translate = True
                except Exception:
                    toggled_translate = False
    try:
        calcs = worker.call("get_calcs", {})
    finally:
        if toggled_translate:
            try:
                worker.call("set_translate", {"enabled": False})
            except Exception:
                pass

    rows_index: list[dict[str, Any]] = []
    for sec in calcs.get("sections", []):
        sec_id = str(sec.get("id") or "")
        for sub in sec.get("subsections", []):
            sub_en = strip_pob_escapes(str(sub.get("label") or ""))
            sub_zh = strip_pob_escapes(str(sub.get("labelZh") or sub_en))
            for row in sub.get("rows", []):
                r_en = strip_pob_escapes(str(row.get("label") or ""))
                r_zh = strip_pob_escapes(str(row.get("labelZh") or r_en))
                vals: list[dict[str, Any]] = []
                cell_texts: list[str] = []
                for cell in row.get("cells", []):
                    raw_v = strip_pob_escapes(str(cell.get("raw") or ""))
                    txt_v = strip_pob_escapes(str(cell.get("text") or raw_v))
                    if raw_v or txt_v:
                        vals.append({"raw": raw_v or txt_v, "text": txt_v or raw_v})
                        cell_texts.append(f"{raw_v} {txt_v}")
                hay_row = f"{r_en} {r_zh}".lower()
                hay_sub = f"{sec_id} {sub_en} {sub_zh}".lower()
                hay_full = f"{hay_sub} {hay_row} {' '.join(cell_texts).lower()}"
                rows_index.append(
                    {
                        "sectionId": sec_id,
                        "subsection": sub_en,
                        "subsectionZh": sub_zh,
                        "rowLabel": r_en,
                        "rowLabelZh": r_zh,
                        "values": vals,
                        "_hay_row": hay_row,
                        "_hay_row_compact": re.sub(r"[\s_\-]+", "", hay_row),
                        "_hay_full": hay_full,
                        "_hay_full_compact": re.sub(r"[\s_\-]+", "", hay_full),
                    }
                )

    out: dict[str, list[dict[str, Any]]] = {}
    for kw in calc_keywords:
        q = kw.strip().lower()
        q_compact = re.sub(r"[\s_\-]+", "", q)
        if not q:
            out[kw] = []
            continue
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for pos, r in enumerate(rows_index):
            if q in r["_hay_row"] or (q_compact and q_compact in r["_hay_row_compact"]):
                score = 3
            elif q in r["_hay_full"] or (q_compact and q_compact in r["_hay_full_compact"]):
                score = 1
            else:
                continue
            scored.append((score, pos, r))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out[kw] = [
            {
                "sectionId": r["sectionId"],
                "subsection": r["subsection"],
                "rowLabel": r["rowLabel"],
                "rowLabelZh": r["rowLabelZh"],
                "values": r["values"],
            }
            for _, _, r in scored[:limit_per_keyword]
        ]
    return out
