"""Gem quality stat humanization, enrichment, and 20%->40%+ breakpoint analysis."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..guardrails import GuardrailScope, check_input
from .constants import _REMOVED_LEGACY_GEM_NAMES_LOWER
from .paths import _sqlite_ro_uri
from .tw_norm import _fold_search_text

SKILL_GEM_QUALITY_SCOPE_RULE = (
    '"+X% to Quality of all Skill Gems" (所有主动技能石品质，如 Ashes of the Stars / 3.30 柳条人肢体) '
    "applies ONLY to Active Skill Gems (`grants_active_skill`), NOT Support Gems (e.g. Archmage / Spell Echo)! "
    'Only "+X% to Quality of Socketed [Support] Gems" (e.g. Foulborn Skin of the Loyal/Lords, Crest of Desire, Enhance Support) '
    "applies to Support Gems."
)

_QUALITY_STAT_ZH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "projectile": ("投射物", "箭矢", "飞弹", "弹幕"),
    "chain": ("连锁", "弹射", "链"),
    "split": ("分裂",),
    "pierce": ("穿透",),
    "fork": ("分叉",),
    "final": ("更多", "独立乘区", "more"),
    "more": ("更多", "独立乘区", "final"),
    "cooldown": ("冷却", "cdr"),
    "minion": ("召唤", "召唤物", "魔像", "僵尸", "骷髅", "灵体"),
    "totem": ("图腾",),
    "stage": ("阶段", "层数", "叠层", "脉冲", "爆发"),
    "pulse": ("脉冲", "冲击", "波"),
    "beam": ("光束", "射线"),
    "wall": ("墙", "冰墙"),
    "repeat": ("重复", "回响", "次数"),
    "additional": ("额外", "数量", "上限"),
    "maximum": ("上限", "最大"),
    "double_damage": ("双倍", "双倍伤害"),
    "critical": ("暴击", "暴伤"),
    "penetration": ("元素穿透", "抗性穿透"),
    "area": ("范围", "aoe"),
    "speed": ("攻速", "施法速度", "速度"),
    "brand": ("烙印",),
    "mine": ("地雷",),
    "trap": ("陷阱",),
    "corpse": ("灵柩", "尸体"),
    "wither": ("凋零",),
    "bleeding": ("流血",),
    "poison": ("中毒",),
    "ignite": ("点燃",),
    "shock": ("感电",),
    "reservation": ("保留",),
    "aura": ("光环",),
    "curse": ("诅咒",),
    "quality": ("品质",),
}

_INTEGER_STEP_STAT_TOKENS: tuple[str, ...] = (
    "projectile",
    "number_of_chains",
    "chain_count",
    "chains_+",
    "split",
    "pierce",
    "fork",
    "additional_",
    "maximum_number",
    "max_number",
    "maximum_stages",
    "max_stages",
    "stage_count",
    "stages_+",
    "repeat",
    "pulse",
    "burst",
    "beam",
    "target",
    "wall",
    "totem",
    "minion",
    "golem",
    "zombie",
    "skeleton",
    "spectre",
    "sentinel",
    "spider",
    "storm",
    "geyser",
    "spire",
    "brand",
    "seal",
    "orb",
    "arrow",
    "bolt",
    "blade",
    "wave",
    "shard",
    "strike",
    "corpse",
    "limit",
    "count",
    "clone",
    "mirror",
    "trap",
    "mine",
    "charge",
    "link",
    "bounce",
    "explosion",
    "impact",
    "arc",
    "radius",
    "metres",
)

_INTEGER_STEP_EXCLUDE_TOKENS: tuple[str, ...] = (
    "chance_%",
    "%_chance",
    "damage_+%",
    "speed_+%",
    "duration_+%",
    "radius_+%",
    "area_of_effect_+%",
    "effect_+%",
    "cost_+%",
    "life_+%",
    "multiplier",
    "effectiveness",
    "accuracy",
    "critical_strike",
    "threshold",
    "distance",
    "range",
    "velocity",
    "delay",
)

_HIGH_VALUE_QUALITY_TOKENS: tuple[str, ...] = (
    "cooldown_speed",
    "cooldown_recovery",
    "double_damage",
    "triple_damage",
    "penetration",
    "base_critical_strike_chance",
    "wither",
    "impale",
    "aura_effect",
    "curse_effect",
    "mana_reservation",
    "action_speed",
    "elusive",
    "fortify",
    "exposure",
    "culling",
    "trigger",
)


def _humanize_quality_stat(stat_key: str, per_q: float) -> str:
    """Turn PoB internal gem quality stat keys into readable EN/ZH descriptions."""
    s = str(stat_key or "").strip()
    low = s.lower()
    is_final = "_+%_final" in low or low.endswith("_final")
    is_pct = "+%" in low or "_%_" in low or low.endswith("_%")
    cleaned = (
        low.replace("_+%_final", "")
        .replace("_final", "")
        .replace("_+%", "")
        .replace("_+", "")
        .replace("active_skill_", "")
        .replace("support_", "")
        .replace("base_", "")
        .replace("_", " ")
        .strip()
    )
    val20 = round(per_q * 20, 3)
    val40 = round(per_q * 40, 3)
    sign = "+" if per_q >= 0 else ""

    def _fmt_v(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    if is_final:
        return f"{sign}{_fmt_v(val20)}% -> {sign}{_fmt_v(val40)}% more {cleaned} (独立乘区)"
    if is_pct:
        return f"{sign}{_fmt_v(val20)}% -> {sign}{_fmt_v(val40)}% {cleaned}"
    return f"{sign}{_fmt_v(val20)} -> {sign}{_fmt_v(val40)} {cleaned}"


def _enrich_gem_quality_stats(results: list[dict[str, Any]], kind_norm: str) -> None:
    """Attach `at_40_quality` and human-readable `label` to every gem's `quality_stats` without schema migration."""
    if kind_norm != "gem":
        return
    for r in results:
        if not isinstance(r, dict):
            continue
        q_list = r.get("quality_stats")
        if not isinstance(q_list, list):
            continue
        cleaned_q: list[dict[str, Any]] = []
        for q in q_list:
            if not isinstance(q, dict):
                continue
            st_low = str(q.get("stat") or "").strip().lower()
            if st_low.startswith("quality_display_"):
                continue
            per_q = q.get("per_quality")
            if isinstance(per_q, (int, float)):
                q["at_40_quality"] = round(float(per_q) * 40, 4)
                if not q.get("label"):
                    q["label"] = _humanize_quality_stat(str(q.get("stat") or ""), float(per_q))
            cleaned_q.append(q)
        r["quality_stats"] = cleaned_q


def _classify_quality_stat(stat_key: str, per_q: float) -> tuple[str, int, str]:
    """Classify a quality stat into `(category, priority_score, breakpoint_note)` for 20% -> 40%+ analysis."""
    low = str(stat_key or "").lower()
    if low.startswith("quality_display_"):
        return ("other", 0, "")
    abs_pq = abs(float(per_q))
    if abs_pq <= 0:
        return ("other", 0, "")

    at20_raw = abs_pq * 20.0
    at40_raw = abs_pq * 40.0

    is_step_candidate = (
        abs_pq <= 0.5
        and "+%" not in low
        and "_%_" not in low
        and not low.endswith("_%")
        and "_final" not in low
        and not any(ex in low for ex in _INTEGER_STEP_EXCLUDE_TOKENS)
        and any(tok in low for tok in _INTEGER_STEP_STAT_TOKENS)
    )
    if is_step_candidate:
        int20 = math.floor(at20_raw + 1e-9)
        int40 = math.floor(at40_raw + 1e-9)
        if int40 > int20:
            step_q = round(1.0 / abs_pq, 1)
            step_str = str(int(step_q)) if float(step_q).is_integer() else f"{step_q:g}"
            core_bonus = (
                30
                if any(
                    k in low
                    for k in ("number_of_projectiles", "number_of_chains", "projectile_number_to_split", "maximum_number_of_stages")
                )
                else (
                    20
                    if any(
                        k in low
                        for k in (
                            "projectile", "chain", "split", "additional_", "maximum_number", "repeat",
                            "stage", "pulse", "beam", "totem", "minion", "brand", "seal", "bounce", "wall",
                        )
                    )
                    else 10
                )
            )
            return (
                "integer_step",
                100 + core_bonus + min(20, (int40 - int20) * 5),
                f"+1 per {step_str}% qual (@20%={int20}, @40%={int40})",
            )

    if "_+%_final" in low or low.endswith("_final"):
        delta = round(at40_raw - at20_raw, 2)
        if delta >= 4.0:
            bonus = 20 if "damage" in low or "speed" in low or "explosion" in low else 8
            return (
                "more_multiplier",
                85 + bonus + min(25, int(delta // 5)),
                f"more multiplier: {at20_raw:g}% (@20%) -> {at40_raw:g}% (@40%) [+{delta:g}%]",
            )

    if any(tok in low for tok in _HIGH_VALUE_QUALITY_TOKENS):
        delta = round(at40_raw - at20_raw, 2)
        return (
            "high_value",
            60 + min(20, int(delta // 2)),
            f"@20%={at20_raw:g} -> @40%={at40_raw:g}",
        )

    return ("standard", 10, f"@20%={at20_raw:g} -> @40%={at40_raw:g}")


def query_gem_quality_breakpoints(
    search_quality: str | None = None,
    *,
    active_only: bool = False,
    limit: int = 30,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Scan local 3.30 SQLite gems (<30ms) for 20% -> 40%+ quality breakpoints or quality-stat search."""
    import pob_cli.game_data as _gd

    t0 = time.perf_counter()
    q_clean = str(search_quality or "").strip()
    warnings = check_input(q_clean, scope=GuardrailScope.DATA_QUERY, expected_game="poe1") if q_clean else []
    q_low = _fold_search_text(q_clean)
    limit = max(1, int(limit))

    search_terms: list[str] = []
    if q_low:
        search_terms.append(q_low)
        for en_tok, zh_list in _QUALITY_STAT_ZH_KEYWORDS.items():
            if q_low == en_tok or en_tok in q_low or any(z in q_low or q_low in z for z in zh_list):
                if en_tok not in search_terms:
                    search_terms.append(en_tok)
                for z in zh_list:
                    if z not in search_terms:
                        search_terms.append(z)

    db_path = _gd.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        where_clauses = ["is_item_skill = 0"]
        if active_only:
            where_clauses.append("is_support = 0")
        sql = f"SELECT name, name_lower, zh_name, is_support, is_vaal, is_transfigured, is_imbued, color, tag_string, data_json FROM gems WHERE {' AND '.join(where_clauses)}"
        rows = conn.execute(sql).fetchall()

        matches: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
        for name, name_l, zh_name, is_sup, is_vaal, is_trans, _is_imbued, color, tag_str, dj in rows:
            if name_l in _REMOVED_LEGACY_GEM_NAMES_LOWER:
                continue
            obj = json.loads(dj)
            q_stats = obj.get("quality_stats") or []
            if not q_stats:
                continue

            best_score = -1
            matched_q: list[dict[str, Any]] = []
            for q in q_stats:
                st = str(q.get("stat") or "")
                pq = q.get("per_quality")
                if not st or not isinstance(pq, (int, float)) or float(pq) == 0.0:
                    continue
                pq_f = float(pq)
                at20 = round(pq_f * 20, 4)
                at40 = round(pq_f * 40, 4)
                label = _humanize_quality_stat(st, pq_f)
                cat, score, bp_note = _classify_quality_stat(st, pq_f)

                if search_terms:
                    hay = _fold_search_text(f"{name} {zh_name or ''} {st} {label} {bp_note}")
                    if not any(t in hay for t in search_terms):
                        continue
                elif cat not in ("integer_step", "more_multiplier", "high_value"):
                    continue

                if score > best_score:
                    best_score = score
                matched_q.append(
                    {
                        "stat": st,
                        "label": label,
                        "category": cat,
                        "perQuality": pq_f,
                        "at20": at20,
                        "at40": at40,
                        "breakpointNote": bp_note,
                    }
                )

            if not matched_q:
                continue

            dup_penalty = 1 if is_vaal else 0
            matched_q.sort(key=lambda item: (-_classify_quality_stat(item["stat"], item["perQuality"])[1], item["stat"]))
            top_cat = matched_q[0]["category"]
            sort_key = (-best_score, dup_penalty, int(is_sup), name_l)
            matches.append(
                (
                    sort_key,
                    {
                        "name": obj.get("display_name") or name,
                        "zhName": zh_name,
                        "isSupport": bool(is_sup),
                        "isTransfigured": bool(is_trans),
                        "isVaal": bool(is_vaal),
                        "color": color,
                        "tags": tag_str,
                        "topCategory": top_cat,
                        "score": best_score,
                        "qualityStats": matched_q,
                    },
                )
            )

        matches.sort(key=lambda item: item[0])
        if not search_terms and limit >= 6:
            by_cat: dict[str, list[dict[str, Any]]] = {
                "integer_step": [],
                "more_multiplier": [],
                "high_value": [],
            }
            for _, item in matches:
                cat_k = item.get("topCategory")
                if cat_k in by_cat:
                    by_cat[cat_k].append(item)
            q_int = max(2, round(limit * 0.45))
            q_more = max(2, round(limit * 0.40))
            q_hv = max(1, limit - q_int - q_more)
            chosen: list[dict[str, Any]] = []
            picked_names: set[str] = set()
            for cat_k, quota in (("integer_step", q_int), ("more_multiplier", q_more), ("high_value", q_hv)):
                for it in by_cat[cat_k][:quota]:
                    if it["name"] not in picked_names:
                        chosen.append(it)
                        picked_names.add(it["name"])
            if len(chosen) < limit:
                for _, it in matches:
                    if it["name"] not in picked_names:
                        chosen.append(it)
                        picked_names.add(it["name"])
                        if len(chosen) >= limit:
                            break
            top_results = chosen[:limit]
        else:
            top_results = [item[1] for item in matches[:limit]]
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    out: dict[str, Any] = {
        "kind": "gem_quality",
        "mode": "search_quality" if q_clean else "quality_breakpoints",
        "query": q_clean or None,
        "activeOnly": bool(active_only),
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "qualityRule": SKILL_GEM_QUALITY_SCOPE_RULE,
        "totalMatched": len(matches),
        "count": len(top_results),
        "elapsedMs": elapsed_ms,
        "results": top_results,
    }
    if warnings:
        out["warnings"] = [w.to_dict() for w in warnings]
    return out
