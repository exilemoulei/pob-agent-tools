"""Reflect map-modifier classification (`reflect_ele` / `reflect_phys`) with residual-chip thresholds."""

from __future__ import annotations

from typing import Any


def _num(d: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = d.get(key)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _skill_total_hit(sk: dict[str, Any]) -> float:
    return (
        _num(sk, "avgPhys")
        + _num(sk, "avgFire")
        + _num(sk, "avgCold")
        + _num(sk, "avgLight")
        + _num(sk, "avgChaos")
    )


def _assess_reflect_hit(
    sk: dict[str, Any],
    *,
    role: str,
    label: str,
    is_ele: bool,
    avg_hit: float,
    taken: float,
    unres_pool: float,
    res_mit: float,
    recovery_rate: float,
    default_speed: float,
) -> tuple[str, str, dict[str, Any] | None]:
    total_hit = _skill_total_hit(sk)
    speed = max(_num(sk, "speed", default_speed), 1.0)
    refl_hit = avg_hit * 0.20 * (taken / 100.0) * (res_mit if is_ele else 1.0)
    share = (avg_hit / total_hit) if total_hit > 0 else 1.0
    share_pct = share * 100.0
    pool_pct = (refl_hit / unres_pool) * 100.0
    sk_name = sk.get("name") or role

    by_share = total_hit > 0 and share < 0.05
    by_recovery = recovery_rate > 0 and refl_hit * speed < 0.25 * recovery_rate
    if refl_hit < 0.03 * unres_pool and (by_share or by_recovery):
        if by_share:
            detail = (
                f"{share_pct:.1f}% of hit < 5%, ~{refl_hit:,.0f} reflect/hit = "
                f"{pool_pct:.1f}% of {unres_pool:,.0f} pool < 3%"
            )
        else:
            detail = (
                f"{share_pct:.1f}% of hit, ~{refl_hit:,.0f} reflect/hit = "
                f"{pool_pct:.1f}% of {unres_pool:,.0f} pool < 3%, "
                f"{refl_hit * speed:,.0f}/s < 25% of {recovery_rate:,.0f}/s recovery"
            )
        meta = {
            "skill": sk_name,
            "avgHit": round(avg_hit, 1),
            "totalHit": round(total_hit, 1),
            "sharePct": round(share_pct, 2),
            "reflectPerHit": round(refl_hit, 1),
            "poolPct": round(pool_pct, 2),
            "unreservedPool": round(unres_pool, 1),
            "thresholdPoolPct": 3.0,
            "thresholdSharePct": 5.0,
        }
        return "safe", f"SAFE: negligible residual {label} hit avg {avg_hit:,.0f} ({detail})", meta

    if share < 0.25 and refl_hit < 0.08 * unres_pool and refl_hit * speed < 0.8 * unres_pool:
        return (
            "danger",
            f"{role} {sk_name} moderate {label} chip avg {avg_hit:,.0f} "
            f"({share_pct:.1f}% of hit, ~{refl_hit:,.0f} reflect/hit = {pool_pct:.1f}% of {unres_pool:,.0f} pool)",
            None,
        )

    return (
        "brick",
        f"{role} {sk_name} {label} hit avg {avg_hit:,.0f} with "
        f"{label}TakenReflect={taken:.0f}% (no reflect immunity)",
        None,
    )


def classify_reflect_with_meta(probe: dict[str, Any], kind: str) -> tuple[str, str, dict[str, Any] | None]:
    """Classify `reflect_ele` (`kind='ele'`) or `reflect_phys` (`kind='phys'`) with optional residual metadata."""
    ms = probe.get("mainSkill") or {}
    dfn = probe.get("defence") or {}
    unres_pool = max(_num(dfn, "lifeUnreserved") + _num(dfn, "es"), 1.0)
    recovery_rate = (
        max(_num(dfn, "lifeRegen"), 0.0)
        + max(_num(dfn, "lifeLeechRate"), 0.0)
        + max(_num(dfn, "esRegen"), 0.0)
        + max(_num(dfn, "esLeechRate"), 0.0)
    )
    min_ele_max = min(
        _num(dfn, "fireMaxRes", 75.0),
        _num(dfn, "coldMaxRes", 75.0),
        _num(dfn, "lightMaxRes", 75.0),
    )
    res_mit = 1.0 - min(max(min_ele_max, 0.0), 90.0) / 100.0
    main_speed = max(_num(ms, "speed", 1.0), 1.0)

    is_ele = kind == "ele"
    if is_ele:
        taken = max(
            _num(dfn, "fireTakenReflect", 100.0),
            _num(dfn, "coldTakenReflect", 100.0),
            _num(dfn, "lightTakenReflect", 100.0),
        )
        avg_hit = _num(ms, "avgFire") + _num(ms, "avgCold") + _num(ms, "avgLight")
        label = "Elemental"
    else:
        taken = _num(dfn, "physTakenReflect", 100.0)
        avg_hit = _num(ms, "avgPhys")
        label = "Physical"

    if taken <= 0:
        return "safe", f"SAFE: {label}TakenReflect={taken:.0f}% (reflect immune)", None

    best_sev = "safe"
    best_reason = f"SAFE: main skill deals 0 {label} hit damage"
    best_meta: dict[str, Any] | None = None

    is_proxy = bool(ms.get("isMinion") or ms.get("isTotem") or ms.get("isTrap") or ms.get("isMine"))
    if ms.get("cannotBeReflected"):
        best_reason = f"SAFE: main skill {ms.get('name') or 'skill'} DamageCannotBeReflected"
    elif is_proxy:
        best_reason = "SAFE: proxy skill (minion/totem/trap/mine) does not reflect to player"
    elif avg_hit > 0:
        sev, reason, meta = _assess_reflect_hit(
            ms,
            role="Main skill",
            label=label,
            is_ele=is_ele,
            avg_hit=avg_hit,
            taken=taken,
            unres_pool=unres_pool,
            res_mit=res_mit,
            recovery_rate=recovery_rate,
            default_speed=main_speed,
        )
        if sev == "brick":
            return sev, reason, None
        best_sev, best_reason, best_meta = sev, reason, meta

    for trig in probe.get("triggeredSkills") or ():
        if trig.get("cannotBeReflected") or trig.get("isProxy"):
            continue
        t_avg = (
            _num(trig, "avgFire") + _num(trig, "avgCold") + _num(trig, "avgLight")
            if is_ele
            else _num(trig, "avgPhys")
        )
        if t_avg > 0:
            sev, reason, meta = _assess_reflect_hit(
                trig,
                role="Triggered skill",
                label=label,
                is_ele=is_ele,
                avg_hit=t_avg,
                taken=taken,
                unres_pool=unres_pool,
                res_mit=res_mit,
                recovery_rate=recovery_rate,
                default_speed=main_speed,
            )
            if sev == "brick":
                return sev, reason, None
            if sev == "danger" and best_sev == "safe":
                best_sev, best_reason, best_meta = sev, reason, None
            elif sev == "safe" and meta is not None and best_meta is None:
                best_reason, best_meta = reason, meta

    if best_sev == "danger":
        return best_sev, best_reason, None

    for mv in probe.get("movementSkills") or ():
        if mv.get("cannotBeReflected"):
            continue
        m_avg = (
            _num(mv, "avgFire") + _num(mv, "avgCold") + _num(mv, "avgLight")
            if is_ele
            else _num(mv, "avgPhys")
        )
        if m_avg * 0.18 * (taken / 100.0) >= 0.25 * unres_pool or m_avg >= 0.25 * unres_pool:
            return (
                "danger",
                f"Movement skill {mv.get('name') or 'Movement'} {label} hit avg {m_avg:,.0f} "
                f">=25% of unreserved pool ({unres_pool:,.0f})",
                None,
            )

    return best_sev, best_reason, best_meta


def classify_reflect(probe: dict[str, Any], kind: str) -> tuple[str, str]:
    """Classify `reflect_ele` (`kind='ele'`) or `reflect_phys` (`kind='phys'`)."""
    sev, reason, _ = classify_reflect_with_meta(probe, kind)
    return sev, reason
