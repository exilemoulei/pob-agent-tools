"""Per-item PoB water attribution and full static + de-watered Pinnacle build audit."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none
from ..snapshot import (
    BuildTransaction,
    _call_batch,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    capture_digest,
    diff_digest,
)
from .dewater import (
    _digest_metrics,
    _extract_probe_state,
    _probe_rpc_method,
    _rpc_error_text,
)
from .dewater_auto import (
    best_effort_audit_tree_and_raw,
    infer_auto_keep_and_flask_notes,
    is_eldritch_battery_active,
)
from .dewater_steps import _same_state
from .pitfalls_core import _detect_build_mechanics_and_pitfalls
from .summary import _compute_effective_mana_spend, _has_doryanis_prototype_equipped


def _prepare_attribution_probes(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
) -> tuple[list[str | None], list[str | None], dict[str, dict[str, Any]], str | None]:
    needed_probe_rpcs: list[str] = []
    rec_probe_rpcs: list[str | None] = []
    rec_probe_errors: list[str | None] = []
    for rec in records:
        probe = rec.get("probe")
        if not probe:
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(None)
            continue
        try:
            rpc_name = _probe_rpc_method(probe)
        except Exception as exc:
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(f"cannot read state before restore: {exc}")
            continue
        rec_probe_rpcs.append(rpc_name)
        rec_probe_errors.append(None)
        if rpc_name not in needed_probe_rpcs:
            needed_probe_rpcs.append(rpc_name)

    pre_probe_results: dict[str, dict[str, Any]] = {}
    pre_probe_batch_err: str | None = None
    if needed_probe_rpcs:
        try:
            raw_pre = _call_batch(
                worker,
                [(m, {}) for m in needed_probe_rpcs],
                raise_on_error=False,
            )
            for m, r in zip(needed_probe_rpcs, raw_pre, strict=False):
                pre_probe_results[m] = r
        except Exception as exc:
            pre_probe_batch_err = str(exc)

    return rec_probe_rpcs, rec_probe_errors, pre_probe_results, pre_probe_batch_err


def _measure_single_water_record(
    worker: BridgeWorker,
    rec: dict[str, Any],
    probe: dict[str, Any] | None,
    probe_rpc: str | None,
    before: Any,
    base: dict[str, float | str | None],
    dps_key: str,
) -> tuple[dict[str, Any], str | None]:
    row: dict[str, Any] = {
        "dimension": rec["dimension"],
        "key": rec["key"],
        "label": rec["label"],
    }
    restore_calls = list(rec.get("restore") or [])
    revert_calls = list(rec.get("revert") or [])

    batch_calls: list[tuple[str, dict[str, Any]]] = []
    restore_start = len(batch_calls)
    batch_calls.extend(restore_calls)
    restore_end = len(batch_calls)

    probe_after_idx: int | None = None
    if probe and probe_rpc is not None:
        probe_after_idx = len(batch_calls)
        batch_calls.append((probe_rpc, {}))

    stats_idx = len(batch_calls)
    batch_calls.append(("get_stats", {}))

    revert_start = len(batch_calls)
    batch_calls.extend(revert_calls)
    revert_end = len(batch_calls)

    probe_back_idx: int | None = None
    if probe and probe_rpc is not None:
        probe_back_idx = len(batch_calls)
        batch_calls.append((probe_rpc, {}))

    try:
        batch_res = _call_batch(worker, batch_calls, raise_on_error=False)
    except Exception as exc:
        row["error"] = str(exc)
        return row, rec["key"]

    if len(batch_res) < len(batch_calls):
        row["error"] = f"incomplete batch response ({len(batch_res)}/{len(batch_calls)} RPCs)"
        return row, rec["key"]

    restore_err: str | None = None
    for r_i in range(restore_start, restore_end):
        err_txt = _rpc_error_text(batch_res[r_i])
        if err_txt is not None:
            restore_err = err_txt
            break

    if restore_err is None and probe and probe_after_idx is not None:
        p_err = _rpc_error_text(batch_res[probe_after_idx])
        if p_err is not None:
            restore_err = p_err
        else:
            try:
                got = _extract_probe_state(probe, batch_res[probe_after_idx])
                if not _same_state(got, rec.get("original")):
                    restore_err = (
                        f"restore not applied: expected {rec.get('original')!r}, PoB reads {got!r}"
                    )
            except Exception as exc:
                restore_err = str(exc)

    if restore_err is None:
        stats_res = batch_res[stats_idx]
        s_err = _rpc_error_text(stats_res)
        if s_err is not None:
            restore_err = s_err
        else:
            m = _digest_metrics({"stats": stats_res.get("stats", {})}, dps_key)
            for k in ("dps", "totalEhp", "physMaxHit", "eleMaxHit"):
                mv = m[k]
                bv = base[k]
                row[k] = mv
                if (
                    isinstance(mv, (int, float))
                    and isinstance(bv, (int, float))
                    and math.isfinite(mv)
                    and math.isfinite(bv)
                ):
                    delta = mv - bv
                    row[f"{k}Delta"] = delta
                    row[f"{k}Pct"] = (delta / abs(bv) * 100.0) if abs(bv) > 0.0 else None
                else:
                    row[f"{k}Delta"] = None
                    row[f"{k}Pct"] = None
    else:
        row["error"] = restore_err

    revert_err: str | None = None
    for r_i in range(revert_start, revert_end):
        err_txt = _rpc_error_text(batch_res[r_i])
        if err_txt is not None:
            revert_err = err_txt
            break

    if revert_err is None and probe and probe_back_idx is not None:
        pb_err = _rpc_error_text(batch_res[probe_back_idx])
        if pb_err is not None:
            revert_err = pb_err
        else:
            try:
                back = _extract_probe_state(probe, batch_res[probe_back_idx])
                if not _same_state(back, before):
                    revert_err = f"revert not applied: expected {before!r}, PoB reads {back!r}"
            except Exception as exc:
                revert_err = str(exc)

    broken_key: str | None = None
    if revert_err is not None:
        broken_key = rec["key"]
        prev = row.get("error")
        row["error"] = f"{prev}; {revert_err}" if prev else revert_err

    return row, broken_key


def attribute_water(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
    dewatered_digest: dict[str, Any],
    dps_key: str,
) -> list[dict[str, Any]]:
    """Measure each stripped item's real PoB contribution, one at a time (pipelined per item)."""
    if not records:
        return []

    base = _digest_metrics(dewatered_digest, dps_key)
    rec_probe_rpcs, rec_probe_errors, pre_probe_results, pre_probe_batch_err = (
        _prepare_attribution_probes(worker, records)
    )

    rows: list[dict[str, Any]] = []
    broken_after: str | None = None

    for idx, rec in enumerate(records):
        if broken_after is not None:
            rows.append(
                {
                    "dimension": rec["dimension"],
                    "key": rec["key"],
                    "label": rec["label"],
                    "error": f"skipped: baseline not restored after {broken_after}",
                }
            )
            continue

        probe = rec.get("probe")
        probe_rpc = rec_probe_rpcs[idx]
        if rec_probe_errors[idx] is not None:
            rows.append(
                {
                    "dimension": rec["dimension"],
                    "key": rec["key"],
                    "label": rec["label"],
                    "error": rec_probe_errors[idx],
                }
            )
            continue

        before: Any = None
        if probe and probe_rpc is not None:
            if pre_probe_batch_err is not None:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {pre_probe_batch_err}",
                    }
                )
                continue
            pre_res = pre_probe_results.get(probe_rpc)
            pre_err = _rpc_error_text(pre_res)
            if pre_err is not None:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {pre_err}",
                    }
                )
                continue
            try:
                before = _extract_probe_state(probe, pre_res or {})
            except Exception as exc:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {exc}",
                    }
                )
                continue
            if probe.get("kind") == "config" and _same_state(before, rec.get("original")):
                continue

        row, broken_key = _measure_single_water_record(
            worker, rec, probe, probe_rpc, before, base, dps_key
        )
        if broken_key is not None:
            broken_after = broken_key
        rows.append(row)

    return rows


def _collect_static_health_issues(
    stats: dict[str, Any],
    info: dict[str, Any],
    items_res: dict[str, Any],
    skills_res_audit: dict[str, Any] | None,
    warnings: list[str],
    tree_summary: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    health_issues: list[dict[str, str]] = []
    has_doryani = _has_doryanis_prototype_equipped(items_res)

    for elem in ("Fire", "Cold", "Lightning"):
        if elem == "Lightning" and has_doryani:
            continue
        val = _finite_float(stats.get(f"{elem}Resist"))
        over = _finite_float(stats.get(f"{elem}ResistOverCap"))
        if val < 75:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "HIGH",
                    "message": f"{elem} Resist is uncapped: {val:.0f}% (gap: {75 - val:.0f}%)",
                }
            )
        elif over < 15:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "LOW",
                    "message": f"{elem} Resist overcap is thin: +{over:.0f}% over cap (vulnerable to curses/exposure)",
                }
            )

    chaos_res = _finite_float(stats.get("ChaosResist"))
    life_val = _finite_float(stats.get("Life"))
    chaos_mh_raw = _parse_numeric_or_none(stats.get("ChaosMaximumHitTaken"))
    is_chaos_immune = (life_val <= 1.0 and chaos_mh_raw is not None and math.isinf(chaos_mh_raw)) or chaos_res >= 100
    if chaos_res < 0 and not is_chaos_immune:
        health_issues.append(
            {
                "category": "Resistance",
                "severity": "MEDIUM",
                "message": f"Chaos Resist is negative: {chaos_res:.0f}%",
            }
        )

    for attr in ("Str", "Dex", "Int"):
        cur = _finite_float(stats.get(attr))
        req = _finite_float(stats.get(f"Req{attr}"))
        if req > cur:
            health_issues.append(
                {
                    "category": "Attribute",
                    "severity": "HIGH",
                    "message": f"{attr} insufficient: have {cur:.0f}, require {req:.0f}",
                }
            )

    hit_chance = stats.get("HitChance")
    if hit_chance is not None:
        hc_val = _finite_float(hit_chance, default=100.0)
        if hc_val < 100.0:
            health_issues.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": f"Hit Chance is {hc_val:.1f}% (< 100%)",
                }
            )

    mana_cost = _finite_float(stats.get("ManaCost"))
    mana_unres = _finite_float(stats.get("ManaUnreserved"))
    es_val = max(0.0, _finite_float(stats.get("EnergyShield")))
    es_cost = _finite_float(stats.get("ESCost"))
    eb_active = is_eldritch_battery_active(stats, tree_summary)
    es_for_mana = max(0.0, es_val - es_cost)
    payable_pool = (mana_unres + es_for_mana) if eb_active else mana_unres
    eb_covers_mana = eb_active and es_for_mana >= mana_cost
    raw_mana_regen = (
        stats.get("ManaRegenRecovery")
        if stats.get("ManaRegenRecovery") is not None
        else stats.get("ManaRegen")
    )
    mana_regen = _finite_float(raw_mana_regen)
    mana_leech = _finite_float(stats.get("ManaLeechGainRate"))
    speed = _finite_float(stats.get("Speed"))
    raw_life_unres = (
        stats.get("LifeUnreserved") if stats.get("LifeUnreserved") is not None else stats.get("Life")
    )
    life_unres = _finite_float(raw_life_unres, default=1.0)
    spend_info = _compute_effective_mana_spend(stats, skills_res_audit)
    spend_rate = float(spend_info["spendRate"])
    eff_casts = float(spend_info["effectiveCastsPerSec"])
    repeats = int(spend_info["repeats"])
    rec_rate = max(0.0, mana_regen) + mana_leech
    exceeds_recovery = (spend_rate > rec_rate * 1.10) if rec_rate > 0.0 else (spend_rate > 0.0)
    if es_cost > 0 and es_cost > es_val:
        health_issues.append(
            {
                "category": "Sustain",
                "severity": "HIGH",
                "message": f"Skill ES Cost ({es_cost:.0f}) exceeds Energy Shield ({es_val:.0f})",
            }
        )
    if mana_cost > 0 and mana_cost > payable_pool and life_unres > 0:
        if eb_active and es_cost > 0:
            pool_msg = (
                f"Eldritch Battery remaining ES + Unreserved Mana "
                f"({es_for_mana:.0f} + {mana_unres:.0f} = {payable_pool:.0f} after {es_cost:.0f} ES Cost)"
            )
        elif eb_active:
            pool_msg = f"Eldritch Battery ES + Unreserved Mana ({es_val:.0f} + {mana_unres:.0f} = {payable_pool:.0f})"
        else:
            pool_msg = f"Unreserved Mana ({mana_unres:.0f})"
        health_issues.append(
            {
                "category": "Sustain",
                "severity": "HIGH",
                "message": f"Skill Mana Cost ({mana_cost:.0f}) exceeds {pool_msg}",
            }
        )
    elif mana_cost > 0 and not eb_covers_mana and mana_regen < 0:
        health_issues.append(
            {
                "category": "Sustain",
                "severity": "MEDIUM",
                "message": f"Net Mana Regen is negative ({mana_regen:.1f}/s) with Mana Cost {mana_cost:.0f}",
            }
        )
    elif mana_cost > 0 and not eb_covers_mana and eff_casts > 0 and exceeds_recovery and life_unres > 0:
        detail_bits: list[str] = []
        if repeats > 1:
            detail_bits.append(f"raw {speed:.2f}/s / {repeats}x repeats")
        if spend_info.get("cooldownCapped") and spend_info.get("effectiveCooldown"):
            detail_bits.append(f"capped by {float(spend_info['effectiveCooldown']):.2f}s CD")
        detail_suffix = f"; {', '.join(detail_bits)}" if detail_bits else ""
        health_issues.append(
            {
                "category": "Sustain",
                "severity": "LOW",
                "message": (
                    f"Mana spend rate ({spend_rate:.1f}/s = {mana_cost:.0f} * {eff_casts:.2f}/s{detail_suffix}) "
                    f"exceeds net Mana recovery + leech ({rec_rate:.1f}/s)"
                ),
            }
        )

    pts = info.get("points", {})
    used, used_max = int(pts.get("used") or 0), int(pts.get("usedMax") or 0)
    asc_used, asc_max = int(pts.get("ascUsed") or 0), int(pts.get("ascMax") or 0)
    if used > used_max:
        health_issues.append(
            {
                "category": "Points",
                "severity": "HIGH",
                "message": f"Over-allocated passive points: {used}/{used_max}",
            }
        )
    if asc_used < asc_max:
        health_issues.append(
            {
                "category": "Points",
                "severity": "LOW",
                "message": f"Unallocated Ascendancy points: {asc_used}/{asc_max}",
            }
        )

    for w in warnings:
        health_issues.append({"category": "EngineWarning", "severity": "HIGH", "message": w})

    primary_slots = {"Weapon 1", "Body Armour", "Helmet", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt"}
    for slot in items_res.get("slots", []):
        if slot.get("name") in primary_slots and not slot.get("inactive") and not slot.get("selItemId"):
            health_issues.append(
                {
                    "category": "Equipment",
                    "severity": "MEDIUM",
                    "message": f"Equipment slot '{slot['name']}' is empty",
                }
            )
    return health_issues


def _build_audit_panel(digest: dict[str, Any]) -> dict[str, Any]:
    st = digest["stats"]
    ele = _safe_min_ele_max_hit(st)
    return {
        "primaryDpsKey": digest["primaryDpsKey"],
        "primaryDps": digest["primaryDps"],
        "fullDps": _json_safe_stat(st.get("FullDPS")) if st.get("FullDPS") is not None else None,
        "combinedDps": _json_safe_stat(st.get("CombinedDPS")) if st.get("CombinedDPS") is not None else None,
        "totalDps": _json_safe_stat(st.get("TotalDPS")) if st.get("TotalDPS") is not None else None,
        "minionCombinedDps": _json_safe_stat(st.get("MinionCombinedDPS")) if st.get("MinionCombinedDPS") is not None else None,
        "totalEhp": digest["totalEhp"],
        "life": digest["life"],
        "physMaxHit": _json_safe_stat(st.get("PhysicalMaximumHitTaken")) if st.get("PhysicalMaximumHitTaken") is not None else None,
        "eleMaxHit": ele if ele is not None else 0.0,
        "chaosMaxHit": _json_safe_stat(st.get("ChaosMaximumHitTaken")) if st.get("ChaosMaximumHitTaken") is not None else None,
    }


def audit_build(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    attribution: bool = True,
    *,
    auto_keep: bool = True,
    include_envelope: bool = True,
    pre_tree_summary: dict[str, Any] | None = None,
    pre_equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit."""
    import pob_cli.analyzer as _analyzer

    with BuildTransaction(worker) as tx:
        items_res = worker.call("list_items", {})
        digest_before = capture_digest(worker)
        stats = digest_before["stats"]
        info = digest_before["buildInfo"]
        try:
            skills_res_audit, cfg_res_audit = _call_batch(
                worker,
                [("list_skills", {}), ("list_config", {})],
            )
        except Exception:
            skills_res_audit, cfg_res_audit = None, None

        eff_tree, eff_raw, alloc_st = best_effort_audit_tree_and_raw(
            worker,
            items_res,
            pre_tree_summary=pre_tree_summary,
            pre_equipped_slots_raw=pre_equipped_slots_raw,
        )
        health_issues = _collect_static_health_issues(
            stats,
            info,
            items_res,
            skills_res_audit,
            digest_before["warnings"],
            tree_summary=eff_tree,
        )

        ak_reasons, flask_notes = (
            infer_auto_keep_and_flask_notes(
                stats=stats,
                skills_res=skills_res_audit,
                items_res=items_res,
                build_info=info,
                tree_summary=eff_tree,
                equipped_slots_raw=eff_raw,
                alloc_node_stats=alloc_st,
            )
            if auto_keep
            else ({}, {})
        )
        dw_defs = getattr(_analyzer.dewater_build_with_records, "__kwdefaults__", None) or {}
        dw_kw: dict[str, Any] = {}
        if "auto_keep_reasons" in dw_defs:
            dw_kw["auto_keep_reasons"] = ak_reasons
        if "flask_notes" in dw_defs:
            dw_kw["flask_notes"] = flask_notes
        stripped, records = _analyzer.dewater_build_with_records(worker, keep=keep, **dw_kw)
        digest_dewatered = capture_digest(worker)
        dewater_diff = diff_digest(digest_before, digest_dewatered)
        water_attribution = (
            _analyzer.attribute_water(worker, records, digest_dewatered, digest_before["primaryDpsKey"])
            if attribution
            else []
        )
        restore_check = tx.rollback_verified(digest_before)

        mech_gaps, mech_audit = _detect_build_mechanics_and_pitfalls(
            stats=stats,
            skills_res=skills_res_audit,
            items_res=items_res,
            cfg_res=cfg_res_audit,
            warnings=digest_before["warnings"],
            build_info=info,
            stripped_flasks=list((stripped or {}).get("flasks") or []),
        )
        health_issues.extend(mech_gaps)
        audit_core_synergies = list(mech_audit.get("coreSynergies") or [])
        agent_probe_res: dict[str, Any] | None = None
        scenario_envelope_res: dict[str, Any] | None = None
        if (getattr(worker, "caps", None) or {}).get("agentProbes"):
            try:
                p_res = worker.call("agent_probe_build", {})
                if isinstance(p_res, dict) and not p_res.get("_error"):
                    agent_probe_res = p_res
            except Exception:
                pass
            if include_envelope:
                try:
                    env_res = worker.call("eval_scenario_envelope", {})
                    if isinstance(env_res, dict) and not env_res.get("_error"):
                        scenario_envelope_res = env_res
                except Exception:
                    pass

        out_audit: dict[str, Any] = {
            "healthIssues": health_issues,
            "core_synergies": audit_core_synergies,
            "inflationStripped": stripped,
            "waterAttribution": water_attribution,
            "restoreCheck": restore_check,
            "original": _build_audit_panel(digest_before),
            "dewateredPinnacle": _build_audit_panel(digest_dewatered),
            "dewaterDiff": dewater_diff,
        }
        if agent_probe_res is not None:
            out_audit["agentProbe"] = agent_probe_res
        if scenario_envelope_res is not None:
            out_audit["scenarioEnvelope"] = scenario_envelope_res
        return out_audit
