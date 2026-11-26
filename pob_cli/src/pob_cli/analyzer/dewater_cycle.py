"""Cycle / Burst duty-cycle envelope builder for stripped cooldown/guard skills and Tinctures (L2)."""

from __future__ import annotations

from typing import Any

from ..snapshot import _finite_float


def build_cycle_envelope(
    provenance: dict[str, Any] | None,
    stripped: dict[str, Any] | None,
    scenario_envelope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build structured Cycle / Burst duty-cycle reference for stripped CD skills & non-Bloodsoaked Tinctures."""
    stripped_skills = [str(s) for s in ((stripped or {}).get("skills") or [])]
    kept_blob = " | ".join(str(s) for s in ((stripped or {}).get("kept") or [])).lower()
    stripped_flasks = [str(s) for s in ((stripped or {}).get("flasks") or [])]
    raw_cd = (
        (provenance.get("cooldownSkills") if isinstance(provenance, dict) else None)
        or (scenario_envelope.get("cycleSkills") if isinstance(scenario_envelope, dict) else None)
        or []
    )
    skills_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if stripped_skills:
        for sk in raw_cd:
            if not isinstance(sk, dict):
                continue
            nm = str(sk.get("name") or "").strip()
            if not nm or nm.lower() in seen or (kept_blob and nm.lower() in kept_blob):
                continue
            seen.add(nm.lower())
            is_vaal = bool(sk.get("isVaal"))
            dur = _finite_float(sk.get("duration"))
            cd = _finite_float(sk.get("cooldown"))
            if is_vaal:
                duty = 0.0
            else:
                duty = _finite_float(sk.get("dutyCyclePct"))
                if duty <= 0.0 and dur > 0.0 and cd > 0.0:
                    duty = dur / (dur + cd) * 100.0
            entry: dict[str, Any] = {
                "name": nm,
                "duration": round(dur, 2),
                "cooldown": round(cd, 2),
                "dutyCyclePct": round(duty, 1),
                "isGuard": bool(sk.get("isGuard")),
                "isVaal": is_vaal,
                "isAutomated": bool(sk.get("isAutomated")),
            }
            if sk.get("manaSpend") is not None:
                entry["manaSpend"] = int(_finite_float(sk.get("manaSpend")))
            skills_out.append(entry)

    # Also record stripped non-Bloodsoaked Tinctures as cycle-only items
    flasks_prov = (provenance.get("flasks") if isinstance(provenance, dict) else None) or []
    for f in flasks_prov:
        if not isinstance(f, dict) or not f.get("isTincture"):
            continue
        sn = str(f.get("slot") or "")
        if sn and any(sn in sf for sf in stripped_flasks):
            t_name = str(f.get("title") or sn)
            if t_name.lower() not in seen:
                seen.add(t_name.lower())
                skills_out.append(
                    {
                        "name": t_name,
                        "isTincture": True,
                        "duration": 0.0,
                        "cooldown": 0.0,
                        "dutyCyclePct": 0.0,
                    }
                )

    if not skills_out:
        return None

    out: dict[str, Any] = {
        "sustainedGuardOff": True,
        "skills": skills_out,
    }
    scenarios = (scenario_envelope or {}).get("scenarios") if isinstance(scenario_envelope, dict) else None
    if isinstance(scenarios, dict):
        burst = scenarios.get("Burst")
        sust = scenarios.get("PinnacleSustained")
        if isinstance(burst, dict):
            out["burst"] = {
                "dps": burst.get("dps"),
                "totalEhp": burst.get("totalEhp"),
                "physMaxHit": burst.get("physMaxHit"),
                "eleMinMaxHit": burst.get("eleMinMaxHit"),
            }
        if isinstance(sust, dict):
            out["sustained"] = {
                "dps": sust.get("dps"),
                "totalEhp": sust.get("totalEhp"),
                "physMaxHit": sust.get("physMaxHit"),
                "eleMinMaxHit": sust.get("eleMinMaxHit"),
            }
    return out
