# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 S4/T3.4 Deep Audit: 5 intuition-trap audits, WS1/WS2 swap audit, 4-dim dehydration,
item-101 Cap+Overcap resistance audit, and [PoB2未模拟] honest red-card shield (L2).
"""

from __future__ import annotations

from typing import Any

from ..game_profile import GAME_ID
from .version_stamp import attach_version_stamp

DEFAULT_OVERCAP_TARGET = 15
IDEAL_OVERCAP_UPPER = 30


def evaluate_resistances_cap_overcap(
    resists: dict[str, dict[str, Any]],
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
) -> dict[str, Any]:
    """Audit PoE2 resistances strictly per item-101 rule:
      1. Hard floor: each Elemental Resist >= its own MaxResist cap (Chaos >= chaos_floor).
      2. Overcap buffer: report +15%..+30% headroom for Waystone curses / Exposure.
      3. NEVER penalize unequal healthy overcaps (e.g. 105%/78%/82% is healthy, NOT an issue!).
    """
    issues: list[dict[str, Any]] = []
    entries: dict[str, dict[str, Any]] = {}

    for elem in ("Fire", "Cold", "Lightning", "Chaos"):
        rec = resists.get(elem) or {}
        final_v = float(rec.get("final", 0))
        total_v = float(rec.get("total", final_v))
        cap_v = float(rec.get("cap", 75))
        overcap = total_v - cap_v
        is_ele = elem != "Chaos"

        if is_ele:
            capped = final_v >= cap_v
            if not capped:
                status = "uncapped"
                issues.append(
                    {
                        "code": "resistance_uncapped",
                        "severity": "critical",
                        "element": elem,
                        "final": final_v,
                        "cap": cap_v,
                        "deficit": cap_v - final_v,
                        "message": f"{elem} Resistance is uncapped ({final_v:.0f}% / {cap_v:.0f}% cap; missing {cap_v - final_v:.0f}%).",
                    }
                )
            elif overcap < overcap_target:
                status = "capped_low_overcap"
                issues.append(
                    {
                        "code": "resistance_low_overcap_buffer",
                        "severity": "advisory",
                        "element": elem,
                        "final": final_v,
                        "total": total_v,
                        "cap": cap_v,
                        "overcapBuffer": overcap,
                        "recommendedBuffer": overcap_target,
                        "message": (
                            f"{elem} Resistance is capped ({final_v:.0f}%) with +{overcap:.0f}% overcap buffer "
                            f"(recommend +{overcap_target}%..+{IDEAL_OVERCAP_UPPER}% buffer for Waystone curses/Exposure)."
                        ),
                    }
                )
            else:
                status = "capped_with_buffer"
        else:
            capped = final_v >= chaos_floor
            if final_v < chaos_floor:
                status = "below_floor"
                issues.append(
                    {
                        "code": "chaos_resistance_low",
                        "severity": "warning",
                        "element": elem,
                        "final": final_v,
                        "floor": chaos_floor,
                        "message": f"Chaos Resistance is {final_v:.0f}% (below target floor {chaos_floor}%).",
                    }
                )
            else:
                status = "healthy"

        entries[elem] = {
            "final": final_v,
            "total": total_v,
            "cap": cap_v,
            "overcapBuffer": overcap,
            "capped": capped,
            "status": status,
        }

    return {
        "policy": "cap_plus_overcap_buffer_no_equalization",
        "overcapTargetPct": overcap_target,
        "elements": entries,
        "allElementalCapped": all(entries[e]["capped"] for e in ("Fire", "Cold", "Lightning")),
        "issues": issues,
    }


def _build_findings_from_probe(
    probe: dict[str, Any],
    dehydration: dict[str, Any],
    res_audit: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # 1. Two-step terminal conversion 0% dead mods
    for dm in (probe.get("conversion") or {}).get("deadMods") or []:
        findings.append(
            {
                "category": "conversion_dead_mod",
                "severity": "warning",
                "sourceDamageType": dm.get("sourceDamageType"),
                "wastedIncPct": dm.get("wastedIncPct", 0),
                "wastedMorePct": dm.get("wastedMorePct", 0),
                "message": (
                    f"100%-converted `{dm.get('sourceDamageType')}` has 0% dead source-type modifiers "
                    f"(+{dm.get('wastedIncPct', 0):.0f}% inc / +{dm.get('wastedMorePct', 0):.0f}% more) "
                    "due to PoE2 two-step terminal conversion."
                ),
            }
        )

    # 2. Armour ArmourRatio=10 pre-resistance drop-off on big hits
    arm = probe.get("armour") or {}
    ladder = arm.get("physicalLadder") or []
    if float(arm.get("armour") or 0) > 0 and len(ladder) >= 4:
        m_1k = ladder[0].get("mitigationPct", 0)
        m_20k = ladder[-1].get("mitigationPct", 0)
        if m_1k - m_20k >= 25.0:
            findings.append(
                {
                    "category": "armour_big_hit_dropoff",
                    "severity": "warning",
                    "armour": arm.get("armour"),
                    "mitigation1kPct": m_1k,
                    "mitigation20kPct": m_20k,
                    "hasElementalArmour": bool(arm.get("hasElementalArmour")),
                    "tooltipErrata": arm.get("tooltipErrata"),
                    "message": (
                        f"Armour ({arm.get('armour'):.0f}, ArmourRatio=10) mitigates {m_1k:.1f}% of a 1k hit "
                        f"but drops to {m_20k:.1f}% against a 20k slam."
                    ),
                }
            )

    # 3. Deflection 95% cap illusion vs raw un-deflected MaxHit
    defl = probe.get("deflection") or {}
    if defl.get("isIllusionRisk"):
        findings.append(
            {
                "category": "deflection_ehp_illusion",
                "severity": "warning",
                "deflectChance": defl.get("chance"),
                "deflectCap": defl.get("chanceCap", 95),
                "totalEhp": defl.get("totalEhp"),
                "rawPhysMaxHit": defl.get("rawPhysMaxHit"),
                "rawEleMinMaxHit": defl.get("rawEleMinMaxHit"),
                "message": (
                    f"Deflection ({defl.get('chance'):.0f}% / {defl.get('chanceCap', 95)}% cap) inflates expected TotalEHP "
                    f"({defl.get('totalEhp', 0):.0f}), but raw un-deflected MaxHit is {defl.get('rawPhysMaxHit', 0):.0f} Phys / "
                    f"{defl.get('rawEleMinMaxHit', 0):.0f} Ele (0% deflection mitigation on lethal bypass hits)."
                ),
            }
        )

    # 3b. 50% Block cap overcap waste
    blk = probe.get("block") or {}
    if float(blk.get("overcap") or 0) > 0:
        findings.append(
            {
                "category": "block_cap_overcap",
                "severity": "advisory",
                "blockChance": blk.get("chance"),
                "blockCap": blk.get("maxCap", 50),
                "overcap": blk.get("overcap"),
                "message": f"Block Chance exceeds PoE2 {blk.get('maxCap', 50):.0f}% cap by +{blk.get('overcap'):.1f}%.",
            }
        )

    # 3c. Crossbow EffectiveReloadTime cycle loss
    xbow = probe.get("crossbow")
    if isinstance(xbow, dict) and float(xbow.get("reloadDpsLossPct") or 0) >= 10.0:
        findings.append(
            {
                "category": "crossbow_reload_cycle",
                "severity": "advisory",
                "reloadTime": xbow.get("reloadTime"),
                "effectiveReloadTime": xbow.get("effectiveReloadTime"),
                "reloadDpsLossPct": xbow.get("reloadDpsLossPct"),
                "message": (
                    f"Crossbow reload cycle (EffectiveReloadTime={xbow.get('effectiveReloadTime'):.2f}s) "
                    f"reduces sustained DPS rate by {xbow.get('reloadDpsLossPct'):.1f}% vs burst firing rate."
                ),
            }
        )

    # 4. Chaos 2x ES, Poison bypass, CI 1-Life stun/freeze perma-lock
    es_ci = probe.get("esChaosCi") or {}
    if es_ci.get("ciStunFreezeLockRisk"):
        findings.append(
            {
                "category": "ci_1life_stun_freeze_lock",
                "severity": "critical",
                "message": (
                    "Chaos Inoculation (1 Life) without ES-based Stun/Freeze threshold or immunity "
                    "exposes the character to permanent stun/freeze locks."
                ),
            }
        )
    elif es_ci.get("chaosDoubleEsDamage"):
        findings.append(
            {
                "category": "chaos_2x_es_and_poison_bypass",
                "severity": "warning",
                "life": es_ci.get("life"),
                "energyShield": es_ci.get("energyShield"),
                "message": (
                    f"Non-CI Energy Shield ({es_ci.get('energyShield'):.0f} ES / {es_ci.get('life'):.0f} Life) "
                    "takes 2x Chaos damage to ES and Poison bypasses ES directly to Life."
                ),
            }
        )

    # 4b. Spirit overload & WS1/WS2 swap audit
    sp = probe.get("spirit") or {}
    if sp.get("overloaded"):
        findings.append(
            {
                "category": "spirit_overloaded",
                "severity": "critical",
                "spiritTotal": sp.get("total"),
                "spiritUnreserved": sp.get("unreserved"),
                "message": f"Spirit is overloaded ({sp.get('unreserved'):.0f} unreserved / {sp.get('total'):.0f} total).",
            }
        )
    ws_swap = probe.get("weaponSetSwapAudit") or {}
    if ws_swap.get("hasSwapSpiritDrop") or ws_swap.get("hasSwapAttrRedText"):
        findings.append(
            {
                "category": "weapon_set_swap_gap",
                "severity": "critical",
                "hasSwapSpiritDrop": bool(ws_swap.get("hasSwapSpiritDrop")),
                "hasSwapAttrRedText": bool(ws_swap.get("hasSwapAttrRedText")),
                "ws1": ws_swap.get("ws1"),
                "ws2": ws_swap.get("ws2"),
                "message": "Swapping between Weapon Set 1 and Weapon Set 2 causes Spirit drop or attribute red-text deficit.",
            }
        )

    # 4c. Charm / For Utopia bloat & dehydration inflation
    cb = probe.get("charmBloat") or {}
    if cb.get("hasForUtopia") or cb.get("conditionUsingCharm"):
        findings.append(
            {
                "category": "charm_for_utopia_bloat",
                "severity": "warning",
                "hasForUtopia": bool(cb.get("hasForUtopia")),
                "conditionUsingCharm": bool(cb.get("conditionUsingCharm")),
                "message": "Detected `For Utopia` / conditional Charm active config inflating EHP; stripped in PinnacleSustained.",
            }
        )
    if float(dehydration.get("dpsInflationRatio") or 0) >= 1.5 or float(dehydration.get("ehpInflationRatio") or 0) >= 1.5:
        findings.append(
            {
                "category": "panel_inflation",
                "severity": "warning",
                "dpsInflationRatio": dehydration.get("dpsInflationRatio"),
                "ehpInflationRatio": dehydration.get("ehpInflationRatio"),
                "message": (
                    f"Raw panel is inflated vs PinnacleSustained "
                    f"(DPS {dehydration.get('dpsInflationRatio')}x, EHP {dehydration.get('ehpInflationRatio')}x)."
                ),
            }
        )

    # 5. [PoB2未模拟] honest red cards
    for rc in probe.get("unsimulatedRedCards") or []:
        findings.append(
            {
                "category": "pob2_unsimulated_red_card",
                "severity": "red_card",
                "tag": "[PoB2未模拟]",
                "issue": rc.get("issue"),
                "skillName": rc.get("skillName"),
                "message": f"[PoB2未模拟] {rc.get('skillName')} ({rc.get('issue')}): {rc.get('reason')}",
            }
        )

    # 6. Item-101 Resistance cap + overcap buffer issues (NEVER equalization!)
    for r_iss in res_audit.get("issues") or []:
        findings.append({"category": "resistance_audit", **r_iss})

    return findings


def run_deep_audit(
    worker: Any,
    *,
    overcap_target: int = DEFAULT_OVERCAP_TARGET,
    chaos_floor: int = 0,
) -> dict[str, Any]:
    """Execute full PoE2 S4/T3.4 deep audit + 4-dim honest dehydration on live BridgeWorker."""
    with worker.lock:
        probe = worker.call("deep_audit_probe", {}, timeout=45.0)
        dehydration = worker.call("eval_honest_dehydration", {}, timeout=45.0)
        diag = worker.get_engine_diagnostics()

    res_audit = evaluate_resistances_cap_overcap(
        probe.get("resists") or {},
        overcap_target=overcap_target,
        chaos_floor=chaos_floor,
    )
    findings = _build_findings_from_probe(probe, dehydration, res_audit)
    payload: dict[str, Any] = {
        "game": GAME_ID,
        "dehydration": dehydration,
        "conversion": probe.get("conversion"),
        "tabulateAttribution": probe.get("tabulateAttribution") or [],
        "armour": probe.get("armour"),
        "deflection": probe.get("deflection"),
        "block": probe.get("block"),
        "esChaosCi": probe.get("esChaosCi"),
        "pinnacleBuildup": probe.get("pinnacleBuildup"),
        "crossbow": probe.get("crossbow"),
        "spirit": probe.get("spirit"),
        "charmBloat": probe.get("charmBloat"),
        "weaponSetSwapAudit": probe.get("weaponSetSwapAudit"),
        "resistances": res_audit,
        "unsimulatedRedCards": probe.get("unsimulatedRedCards") or [],
        "engineDiagnostics": diag,
        "findings": findings,
    }
    return attach_version_stamp(
        payload,
        pob2_version=probe.get("pob2Version") or getattr(worker, "pob_version", None),
        tree_version=probe.get("treeVersion"),
    )


__all__ = [
    "DEFAULT_OVERCAP_TARGET",
    "evaluate_resistances_cap_overcap",
    "run_deep_audit",
]
