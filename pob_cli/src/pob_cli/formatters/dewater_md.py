"""Markdown rendering for dewater Auto-Kept / Auto-Clamped tags and 1-line Cycle / Burst reference (L2)."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, keep_rejected_warning


def format_cycle_burst_line(
    cycle_envelope: dict[str, Any] | None,
    *,
    boss_tier: str | None = None,
) -> str | None:
    """Render a compact 1-line Cycle / Burst duty-cycle reference for stripped CD/guard skills."""
    if not isinstance(cycle_envelope, dict):
        return None
    skills = [s for s in (cycle_envelope.get("skills") or []) if isinstance(s, dict) and s.get("name")]
    if not skills:
        return None
    parts: list[str] = []
    for sk in skills[:5]:
        name = str(sk["name"])
        tags: list[str] = []
        if sk.get("isAutomated"):
            tags.append("Auto")
        if sk.get("isTincture"):
            parts.append(f"{name} (non-sustained Tincture)")
            continue
        dur = float(sk.get("duration") or 0.0)
        cd = float(sk.get("cooldown") or 0.0)
        tag_s = f"[{'/'.join(tags)}] " if tags else ""
        if sk.get("isVaal"):
            timing = f"Vaal burst, soul-limited, {dur:.1f}s dur" if dur > 0.0 else "Vaal burst, soul-limited"
        else:
            duty = float(sk.get("dutyCyclePct") or 0.0)
            if duty <= 0.0 and dur > 0.0 and cd > 0.0:
                duty = dur / (dur + cd) * 100.0
            timing = f"{duty:.1f}% uptime, {dur:.1f}s/{cd:.1f}s CD" if (dur > 0.0 and cd > 0.0) else "CD burst"
        mana_spend = sk.get("manaSpend")
        mana_s = f", ~{fmt_num(mana_spend)} Mana spend" if mana_spend else ""
        parts.append(f"{tag_s}{name} ({timing}{mana_s})")

    ref_bits: list[str] = []
    burst = cycle_envelope.get("burst")
    is_uber = str(boss_tier or "").strip().lower() == "uber"
    burst_pfx = "Original/Pinnacle Burst" if is_uber else "Burst"
    if isinstance(burst, dict):
        if burst.get("dps") is not None:
            ref_bits.append(f"{burst_pfx} DPS `{fmt_num(burst['dps'])}`")
        if burst.get("physMaxHit") is not None or burst.get("eleMinMaxHit") is not None:
            ref_bits.append(
                f"{burst_pfx} MaxHit P/E `{fmt_num(burst.get('physMaxHit'))}/{fmt_num(burst.get('eleMinMaxHit'))}`"
            )
    ref_suffix = f" | {' / '.join(ref_bits)}" if ref_bits else ""
    return (
        "**Cycle / Burst Ref** _(Sustained main panel keeps guard/CD skills OFF)_: "
        + "; ".join(parts)
        + ref_suffix
    )


def format_dewater_summary_lines(
    stripped: dict[str, Any] | None,
    *,
    cycle_envelope: dict[str, Any] | None = None,
    show_flask_notes: bool = False,
    leading_newline: bool = True,
    boss_tier: str | None = None,
) -> list[str]:
    """Render Auto-Kept, Auto-Clamped, Kept, optional Flask Notes, Cycle/Burst ref, Pass 2 warn, and keepRejected."""
    lines: list[str] = []
    pfx = "\n" if leading_newline else ""
    if isinstance(stripped, dict):
        auto_kept = [str(x) for x in (stripped.get("autoKept") or []) if x]
        if auto_kept:
            lines.append(f"{pfx}**Auto-Kept (Build-Guaranteed)**: " + "; ".join(auto_kept))
        auto_clamped = [str(x) for x in (stripped.get("autoClamped") or []) if x]
        if auto_clamped:
            lines.append(f"{pfx}**Auto-Clamped (Boss Physical Cap)**: " + "; ".join(auto_clamped))
        kept = [str(x) for x in (stripped.get("kept") or []) if x]
        if kept:
            lines.append(f"{pfx}**Kept (--keep)**: " + "; ".join(kept))
        if show_flask_notes:
            noted_flasks = [str(f) for f in (stripped.get("flasks") or []) if "[" in str(f) and "]" in str(f)]
            if noted_flasks:
                lines.append(f"{pfx}**Flask Dewater Notes**: " + "; ".join(noted_flasks))
        if stripped.get("pass2Applied") is False:
            p2_warns = [str(w) for w in (stripped.get("pass2Warnings") or []) if w]
            detail = f" ({'; '.join(p2_warns[:2])})" if p2_warns else ""
            lines.append(f"{pfx}> [WARN] Pass 2 Boss clamping degraded; fell back to basic mechanics{detail}.")
    cycle_ln = format_cycle_burst_line(cycle_envelope, boss_tier=boss_tier)
    if cycle_ln:
        lines.append(f"{pfx}{cycle_ln}")
    if isinstance(stripped, dict):
        rej = keep_rejected_warning(stripped)
        if rej:
            lines.append(f"{pfx}" + "\n".join(rej))
    return lines


def format_compare_dewater_lines(
    pin_dew: dict[str, Any],
    target: str,
    ref_target: str,
) -> list[str]:
    """Render Auto-Clamped lines for dual-build compare baseline without bloating `build_md.py`."""
    clamped_parts: list[str] = []
    for slot_lbl, st_key in ((target, "mainStripped"), (ref_target, "refStripped")):
        st_d = pin_dew.get(st_key)
        if not isinstance(st_d, dict):
            continue
        cl = [str(x) for x in (st_d.get("autoClamped") or []) if x]
        if cl:
            clamped_parts.append(f"`{slot_lbl}`: " + "; ".join(cl))
    if not clamped_parts:
        return []
    return ["\n**Auto-Clamped (Boss Physical Cap)**: " + " | ".join(clamped_parts)]
