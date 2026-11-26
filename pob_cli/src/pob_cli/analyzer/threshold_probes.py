"""Zero-RPC (<20ms) build threshold gap probes: Suppression, MRE (25%/50% aura), CoC/Trigger cliffs, Accuracy & Attributes."""

from __future__ import annotations

import math
import time
from typing import Any

from ..ticks import POE1_SERVER_TICK_SEC, compute_server_tick_cooldown

_TRIGGER_BASE_COOLDOWNS: tuple[tuple[str, float], ...] = (
    ("cast on critical strike", 0.15),
    ("cospri's malice", 0.25),
    ("mjolner", 0.25),
    ("mjölner", 0.25),
    ("poet's pen", 0.25),
    ("cast when damage taken", 0.25),
)


def _num(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(float(val)) else None
    try:
        f = float(str(val).strip())
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _pob_round_int(val: float) -> int:
    """Match PoB Lua `round(val, 0)` (half-away-from-zero for non-negative values)."""
    return math.floor(val + 0.5) if val >= 0.0 else math.ceil(val - 0.5)


def _pob_round_2dec(val: float) -> float:
    """Match PoB Lua `round(val, 2)` (2-decimal rounding in `CalcPerform.lua`)."""
    return math.floor(val * 100.0 + 0.5) / 100.0 if val >= 0.0 else math.ceil(val * 100.0 - 0.5) / 100.0


def _reserved_pct_at_mre(base_pct: float, total_mre_pct: float, mre_more_mult: float = 1.0) -> float:
    """PoB CalcPerform.lua exact reservation percent: round(base / (1 + mre/100) / effMore, 2)."""
    denom = max(0.01, 1.0 + total_mre_pct / 100.0) * max(0.01, mre_more_mult)
    return max(0.0, _pob_round_2dec(base_pct / denom))


def compute_mre_needed_for_aura(
    *,
    max_mana: float,
    mana_unreserved: float,
    mana_reserved_pct: float | None,
    current_mre_pct: float,
    active_mana_cost: float,
    extra_aura_base_pct: float,
    mana_reservations: list[dict[str, Any]] | None = None,
    mre_more_mult: float = 1.0,
    estimated: bool = False,
) -> dict[str, Any]:
    """Compute exact minimum additional `% increased Mana Reservation Efficiency` to fit an extra aura."""
    m_int = math.floor(max_mana)
    if m_int <= 0:
        return {
            "auraBasePct": extra_aura_base_pct,
            "fitsNow": False,
            "additionalMreNeededPct": None,
            "minUnreservedRequired": max(1, math.ceil(max(0.0, active_mana_cost))),
            "estimated": bool(estimated),
        }

    min_unres = max(1, math.ceil(max(0.0, active_mana_cost)))
    cur_eff_mult = max(0.01, 1.0 + current_mre_pct / 100.0) * max(0.01, mre_more_mult)
    cur_reserved_mana = max(0, m_int - math.floor(mana_unreserved))

    if mana_reserved_pct is not None and mana_reserved_pct >= 0.0:
        cur_pct = float(mana_reserved_pct)
        cur_pct_mana = math.ceil(m_int * cur_pct / 100.0)
        flat_cur_mana = max(0, cur_reserved_mana - cur_pct_mana)
    else:
        cur_pct = (cur_reserved_mana / m_int) * 100.0
        flat_cur_mana = 0

    existing_base_pct = cur_pct * cur_eff_mult
    existing_base_flat = flat_cur_mana * cur_eff_mult
    skill_rows = [r for r in (mana_reservations or []) if isinstance(r, dict)]

    def _remaining_at(add_mre: int) -> tuple[int, float]:
        tot_mre = current_mre_pct + add_mre
        eff_new_pct = _reserved_pct_at_mre(extra_aura_base_pct, tot_mre, mre_more_mult)
        if skill_rows:
            tot_pct = eff_new_pct
            tot_flat = 0
            for sk in skill_rows:
                mult_cnt = int(sk.get("multCount") or 1)
                sk_eff = max(-100.0, float(sk.get("efficiency") or 0.0) + add_mre)
                sk_more = max(0.01, float(sk.get("efficiencyMore") or 1.0))
                denom = max(0.01, 1.0 + sk_eff / 100.0) * sk_more
                if sk.get("forcedFlat") is not None:
                    r_flat = int(sk["forcedFlat"])
                else:
                    mf = float(sk.get("modifiedFlat") or 0.0)
                    r_flat = max(0, _pob_round_int(mf / denom)) if mf != 0.0 else 0
                if sk.get("forcedPercent") is not None:
                    r_pct = float(sk["forcedPercent"])
                else:
                    mp = float(sk.get("modifiedPercent") or 0.0)
                    r_pct = max(0.0, _pob_round_int(mp / denom) / 100.0) if mp != 0.0 else 0.0
                tot_flat += r_flat * mult_cnt
                tot_pct += r_pct * mult_cnt
            total_res = tot_flat + math.ceil(m_int * tot_pct / 100.0)
            return m_int - total_res, eff_new_pct

        eff_existing_pct = (
            _reserved_pct_at_mre(existing_base_pct, tot_mre, mre_more_mult) if existing_base_pct > 0 else 0.0
        )
        existing_pct_m = math.ceil(m_int * eff_existing_pct / 100.0) if eff_existing_pct > 0 else 0
        denom_g = max(0.01, 1.0 + tot_mre / 100.0) * max(0.01, mre_more_mult)
        existing_flat_m = _pob_round_int(existing_base_flat / denom_g) if existing_base_flat > 0 else 0
        new_aura_m = math.ceil(m_int * eff_new_pct / 100.0)
        return m_int - existing_pct_m - existing_flat_m - new_aura_m, eff_new_pct

    rem0, aura_pct0 = _remaining_at(0)
    if rem0 >= min_unres:
        return {
            "auraBasePct": extra_aura_base_pct,
            "fitsNow": True,
            "additionalMreNeededPct": 0,
            "totalMreRequiredPct": round(current_mre_pct, 1),
            "auraEffectivePct": aura_pct0,
            "unreservedAfter": rem0,
            "minUnreservedRequired": min_unres,
            "estimated": bool(estimated),
        }

    # Binary search smallest integer additional MRE in [1, 1000] (monotonic in add_mre).
    lo, hi = 1, 1000
    found: int | None = None
    rem_found = rem0
    aura_pct_found = aura_pct0
    if _remaining_at(hi)[0] >= min_unres:
        while lo < hi:
            mid = (lo + hi) // 2
            if _remaining_at(mid)[0] >= min_unres:
                hi = mid
            else:
                lo = mid + 1
        found = lo
        rem_found, aura_pct_found = _remaining_at(lo)

    return {
        "auraBasePct": extra_aura_base_pct,
        "fitsNow": False,
        "additionalMreNeededPct": found,
        "totalMreRequiredPct": round(current_mre_pct + found, 1) if found is not None else None,
        "auraEffectivePct": aura_pct_found,
        "unreservedAfter": rem_found,
        "minUnreservedRequired": min_unres,
        "estimated": bool(estimated),
    }


def _probe_spell_suppression(stats: dict[str, Any]) -> dict[str, Any]:
    raw_supp = _num(stats.get("SpellSuppressionChance"))
    eff_supp = _num(stats.get("EffectiveSpellSuppressionChance"))
    cur = raw_supp if raw_supp is not None else (eff_supp if eff_supp is not None else 0.0)
    gap = max(0.0, round(100.0 - cur, 1))
    overcap = max(0.0, round(cur - 100.0, 1))
    if cur <= 0.0:
        status = "none"
    elif cur < 100.0:
        status = "gap"
    elif cur > 100.0:
        status = "overcapped"
    else:
        status = "capped"
    return {
        "currentPct": round(cur, 1),
        "effectivePct": round(eff_supp, 1) if eff_supp is not None else round(min(100.0, cur), 1),
        "targetPct": 100.0,
        "gapTo100Pct": gap,
        "overcapPct": overcap,
        "status": status,
    }


def _detect_trigger_probe(
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    agent_probe: dict[str, Any] | None,
) -> dict[str, Any] | None:
    # 1. Prefer exact Lua triggerCliffs from agent_probe when present.
    cliffs = (agent_probe or {}).get("triggerCliffs") if isinstance(agent_probe, dict) else None
    if isinstance(cliffs, list) and cliffs:
        primary = next((c for c in cliffs if isinstance(c, dict) and c.get("mainGroup")), cliffs[0])
        if isinstance(primary, dict):
            aps = _num(primary.get("sourceAps")) or _num(stats.get("EffectiveSourceRate")) or _num(stats.get("Speed")) or 0.0
            cap = _num(primary.get("maxSafeAps")) or _num(primary.get("triggerRateCap")) or 0.0
            if cap > 0:
                headroom_aps = round(cap - aps, 3)
                headroom_ias = round((cap / aps - 1.0) * 100.0, 1) if aps > 0 else None
                overspeed = bool(primary.get("overspeedCliffExceeded")) or aps > cap * 1.01
                return {
                    "skillName": primary.get("skillName"),
                    "triggerName": primary.get("triggerName"),
                    "serverTickSec": POE1_SERVER_TICK_SEC,
                    "ticks": primary.get("currentTicks"),
                    "effectiveCdSec": round(float(primary.get("tickRoundedCd") or (POE1_SERVER_TICK_SEC * (primary.get("currentTicks") or 1))), 3),
                    "effectiveAps": round(aps, 2),
                    "triggerRateCap": round(cap, 2),
                    "headroomAps": headroom_aps,
                    "headroomIasPct": headroom_ias,
                    "overspeedCliff": overspeed,
                    "status": "OVERSPEED CLIFF" if overspeed else "OK",
                    "nextCdrNeededPct": primary.get("extraIcdrPctNeeded"),
                }

    # 2. Fall back to PoB output stats (`TriggerRateCap` / `SkillTriggerRate` / `EffectiveSourceRate`).
    cap = _num(stats.get("TriggerRateCap"))
    src_aps = _num(stats.get("EffectiveSourceRate"))
    trig_rate = _num(stats.get("SkillTriggerRate"))
    speed = _num(stats.get("Speed"))
    cdr_pct = _num(stats.get("CooldownRecovery")) or 0.0

    trigger_name: str | None = None
    base_cd: float | None = None
    if isinstance(skills_res, dict):
        for g in skills_res.get("groups") or []:
            if not isinstance(g, dict) or not g.get("enabled", True):
                continue
            for gm in g.get("gems") or []:
                if not isinstance(gm, dict) or not gm.get("enabled", True):
                    continue
                nm = str(gm.get("name") or gm.get("nameSpec") or "").lower()
                if "cast while channelling" in nm:
                    trigger_name = gm.get("name") or "Cast while Channelling"
                    base_cd = 0.30 if "awakened" in nm else 0.32
                for pat, cd_val in _TRIGGER_BASE_COOLDOWNS:
                    if pat in nm:
                        trigger_name = gm.get("name") or pat
                        base_cd = cd_val
                        break
    if trigger_name is None and isinstance(items_res, dict):
        for it in items_res.get("items") or []:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("title") or it.get("name") or "").lower()
            for pat, cd_val in _TRIGGER_BASE_COOLDOWNS:
                if pat in nm:
                    trigger_name = it.get("title") or it.get("name")
                    base_cd = cd_val
                    break

    if cap is not None and cap > 0:
        eff_aps = src_aps if (src_aps is not None and src_aps > 0) else (speed or 0.0)
        eff_cd = round(1.0 / cap, 3)
        ticks = max(1, round(eff_cd / POE1_SERVER_TICK_SEC))
        headroom_aps = round(cap - eff_aps, 3)
        headroom_ias = round((cap / eff_aps - 1.0) * 100.0, 1) if eff_aps > 0 else None
        overspeed = eff_aps > cap * 1.01
        return {
            "triggerName": trigger_name or "Trigger",
            "serverTickSec": POE1_SERVER_TICK_SEC,
            "ticks": ticks,
            "effectiveCdSec": round(ticks * POE1_SERVER_TICK_SEC, 3),
            "effectiveAps": round(eff_aps, 2),
            "triggerRateCap": round(cap, 2),
            "reportedTriggerRate": round(trig_rate, 2) if trig_rate is not None else None,
            "headroomAps": headroom_aps,
            "headroomIasPct": headroom_ias,
            "overspeedCliff": overspeed,
            "status": "OVERSPEED CLIFF" if overspeed else "OK",
        }

    if trigger_name and base_cd is not None:
        eff_aps = src_aps if (src_aps is not None and src_aps > 0) else (speed or 0.0)
        tick_info = compute_server_tick_cooldown(base_cd, cdr_pct, aps=eff_aps if eff_aps > 0 else None)
        eff_cap = float(tick_info["effectiveCapPerSec"])
        headroom_aps = round(eff_cap - eff_aps, 3) if eff_aps > 0 else None
        headroom_ias = round((eff_cap / eff_aps - 1.0) * 100.0, 1) if eff_aps > 0 else None
        overspeed = bool(tick_info.get("overspeed"))
        return {
            "triggerName": trigger_name,
            "serverTickSec": POE1_SERVER_TICK_SEC,
            "ticks": tick_info["ticks"],
            "effectiveCdSec": tick_info["effectiveCdSec"],
            "effectiveAps": round(eff_aps, 2),
            "triggerRateCap": eff_cap,
            "headroomAps": headroom_aps,
            "headroomIasPct": headroom_ias,
            "overspeedCliff": overspeed,
            "status": "OVERSPEED CLIFF" if overspeed else "OK",
            "nextCdrNeededPct": (tick_info.get("nextBreakpoint") or {}).get("cdrNeededPct"),
        }

    return None


def compute_threshold_probes(
    stats: dict[str, Any],
    *,
    skills_res: dict[str, Any] | None = None,
    items_res: dict[str, Any] | None = None,
    agent_probe: dict[str, Any] | None = None,
    mana_reservations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute zero-RPC (<20ms) threshold gap probes from PoB output stats."""
    t0 = time.perf_counter()
    st = stats if isinstance(stats, dict) else {}

    supp = _probe_spell_suppression(st)

    max_mana = _num(st.get("Mana")) or 0.0
    mana_unres = _num(st.get("ManaUnreserved"))
    if mana_unres is None:
        mana_unres = max_mana
    mana_res_pct = _num(st.get("ManaReservedPercent"))
    res_slack = (agent_probe or {}).get("resourceSlack") if isinstance(agent_probe, dict) else None
    cur_mre_raw = _num((res_slack or {}).get("manaReservationEfficiency")) if isinstance(res_slack, dict) else None
    if cur_mre_raw is None:
        cur_mre_raw = _num(st.get("ManaReservationEfficiency"))
    mre_estimated = cur_mre_raw is None
    cur_mre = cur_mre_raw if cur_mre_raw is not None else 0.0
    mre_more = (
        _num((res_slack or {}).get("manaReservationEfficiencyMore"))
        if isinstance(res_slack, dict)
        else None
    ) or 1.0
    eff_mana_res = mana_reservations
    if eff_mana_res is None and isinstance(res_slack, dict) and isinstance(res_slack.get("manaReservations"), list):
        eff_mana_res = res_slack["manaReservations"]

    mana_cost = _num(st.get("ManaCost")) or 0.0
    mre_probe = {
        "maxMana": math.floor(max_mana),
        "manaUnreserved": math.floor(mana_unres),
        "manaCost": round(mana_cost, 1),
        "currentMrePct": round(cur_mre, 1),
        "estimated": mre_estimated,
        "extra25Aura": compute_mre_needed_for_aura(
            max_mana=max_mana,
            mana_unreserved=mana_unres,
            mana_reserved_pct=mana_res_pct,
            current_mre_pct=cur_mre,
            active_mana_cost=mana_cost,
            extra_aura_base_pct=25.0,
            mana_reservations=eff_mana_res,
            mre_more_mult=mre_more,
            estimated=mre_estimated,
        ),
        "extra50Aura": compute_mre_needed_for_aura(
            max_mana=max_mana,
            mana_unreserved=mana_unres,
            mana_reserved_pct=mana_res_pct,
            current_mre_pct=cur_mre,
            active_mana_cost=mana_cost,
            extra_aura_base_pct=50.0,
            mana_reservations=eff_mana_res,
            mre_more_mult=mre_more,
            estimated=mre_estimated,
        ),
    }

    trig_probe = _detect_trigger_probe(st, skills_res, items_res, agent_probe)

    hit_chance = _num(st.get("HitChance"))
    accuracy_probe: dict[str, Any] | None = None
    if hit_chance is not None:
        gap = max(0.0, round(100.0 - hit_chance, 2))
        accuracy_probe = {
            "hitChancePct": round(hit_chance, 2),
            "gapTo100Pct": gap,
            "capped": hit_chance >= 99.95,
        }

    attr_probe: dict[str, dict[str, Any]] = {}
    for attr in ("Str", "Dex", "Int"):
        cur = _num(st.get(attr)) or 0.0
        req = _num(st.get(f"Req{attr}")) or 0.0
        surplus = round(cur - req)
        attr_probe[attr] = {
            "current": round(cur),
            "required": round(req),
            "surplus": surplus,
            "status": "deficit" if surplus < 0 else ("tight" if req > 0 and surplus <= 15 else "ok"),
        }

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    out: dict[str, Any] = {
        "spellSuppression": supp,
        "manaReservation": mre_probe,
        "attributes": attr_probe,
        "elapsedMs": elapsed_ms,
    }
    if accuracy_probe is not None:
        out["accuracy"] = accuracy_probe
    if trig_probe is not None:
        out["triggerBreakpoint"] = trig_probe
    return out
