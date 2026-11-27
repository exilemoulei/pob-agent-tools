"""Markdown formatter for `pob rebalance` CoW-verified zero-buy plans and Tier 5 trade fallback (L2)."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_signed_pct, render_table, restore_warning


def _fmt_needs_line(deficits: dict[str, Any]) -> str:
    needs = deficits.get("needs") or {}
    if not needs:
        return "None (all attribute requirements and elemental resistance caps satisfied)."
    parts = [f"`{k}`: short by **{v:g}**" for k, v in needs.items()]
    return ", ".join(parts)


def format_rebalance_markdown(res: dict[str, Any]) -> str:
    """Format `pob rebalance` result into concise human/agent-readable Markdown."""
    status = str(res.get("status") or "unknown")
    applied = bool(res.get("applied"))
    try_info = res.get("tryItem")
    hdr_suffix = ""
    if isinstance(try_info, dict) and (try_info.get("slot") or try_info.get("name")):
        hdr_suffix = f" (after equipping `{try_info.get('name') or 'item'}` in `{try_info.get('slot') or '-'}`)"

    mode_tag = "APPLIED" if applied else "WHAT-IF (0-residue)"
    lines: list[str] = [
        f"### Zero-Buy Auto-Rebalance [{mode_tag}]{hdr_suffix}",
        "_Tier 1-4 = 0-buy Bench/Harvest/Catalyst/Tattoo; Tier 5 = single-slot trade replacement_",
    ]

    r_warn = restore_warning(res.get("restoreCheck"))
    if r_warn:
        lines.append(r_warn)

    deficits = res.get("deficits") or {}
    lines.append(f"- **Detected Deficits**: {_fmt_needs_line(deficits)}")

    counts = res.get("candidateCounts") or {}
    eval_combos = int(res.get("evaluatedCombos") or 0)
    eval_errors = int(res.get("evalErrors") or 0)
    elapsed_ms = res.get("elapsedMs")
    cap_applied = bool(res.get("candidateCapApplied"))
    budget_reached = bool(res.get("evalBudgetReached"))
    if any(counts.values()) or eval_combos or eval_errors:
        meta_bits: list[str] = []
        if eval_combos:
            if elapsed_ms is not None:
                meta_bits.append(f"`{eval_combos}` combos verified in `{int(elapsed_ms)}ms`")
            else:
                meta_bits.append(f"`{eval_combos}` combos verified")
        if budget_reached:
            meta_bits.append("eval budget reached")
        elif cap_applied:
            meta_bits.append("candidate cap applied")
        if eval_errors:
            meta_bits.append(f"`{eval_errors}` eval error(s)")
        meta_suffix = f" ({'; '.join(meta_bits)})" if meta_bits else ""
        lines.append(
            f"- **Candidates Evaluated**: Bench `{counts.get('bench', 0)}` | "
            f"Harvest `{counts.get('harvest', 0)}` | Catalyst `{counts.get('catalyst', 0)}` | Tattoo `{counts.get('tattoo', 0)}`{meta_suffix}"
        )

    if status == "already_balanced":
        msg = res.get("message") or "Build already satisfies all attribute requirements and elemental resistance caps — no rebalance needed."
        lines.append(f"\n{msg}")
        return "\n".join(lines)

    verified = res.get("verifiedPlans") or []
    partial = res.get("partialPlans") or []

    if verified:
        lines.append(f"\n#### Verified Zero-Buy Plans ({len(verified)} found — PoB CoW verified)")
        rows = []
        for p in verified:
            p_idx = p.get("planIndex") or p.get("index")
            step_str = " + ".join(str(st.get("title") or "-") for st in (p.get("steps") or ()))
            rows.append(
                [
                    f"#{p_idx}",
                    f"~{p.get('estCostChaos', 0):g}c",
                    fmt_signed_pct(p.get("deltaDps"), p.get("deltaDpsPct")),
                    fmt_signed_pct(p.get("deltaEhp"), p.get("deltaEhpPct")),
                    step_str,
                ]
            )
        lines.append(render_table(["Plan", "Est. Cost", "ΔDPS", "ΔEHP", "Steps"], rows))
        top_warns = verified[0].get("warnings") or []
        for w in top_warns[:3]:
            lines.append(f"> **Note**: {w}")
        if not applied:
            top_idx = verified[0].get("planIndex") or verified[0].get("index") or 1
            lines.append(f"\n> Pass `--apply-plan {top_idx}` to persist the top verified plan into the build.")
    if partial:
        lines.append(f"\n#### Partial Zero-Buy Fixes ({len(partial)} — closes part of the gap)")
        rows = []
        for p in partial:
            p_idx = p.get("planIndex") or p.get("index")
            step_str = " + ".join(str(st.get("title") or "-") for st in (p.get("steps") or ()))
            rem = ", ".join(f"{k}:{v:g}" for k, v in (p.get("remainingDeficits") or {}).items()) or "-"
            rows.append(
                [
                    f"#{p_idx}",
                    f"~{p.get('estCostChaos', 0):g}c",
                    rem,
                    fmt_signed_pct(p.get("deltaDps"), p.get("deltaDpsPct")),
                    step_str,
                ]
            )
        lines.append(render_table(["Plan", "Est. Cost", "Remaining Gap", "ΔDPS", "Steps"], rows))
        if not verified and not applied:
            p_top_idx = partial[0].get("planIndex") or partial[0].get("index") or 1
            lines.append(f"\n> Pass `--apply-plan {p_top_idx}` to apply the top partial fix.")
    if not verified and not partial:
        lines.append("\nNo 0-buy Bench/Harvest/Catalyst/Tattoo combination closed the deficit.")

    fb = res.get("tradeFallback")
    if isinstance(fb, dict) and fb.get("command"):
        lines.append("\n#### Tier 5 Trade Fallback (Lowest-Burden Rare Slot)")
        lines.append(
            f"- **Target Slot**: `{fb.get('slot')}` (`{fb.get('currentItem') or '-'}`)"
        )
        lines.append(f"- **Ready-to-run search**: `{fb['command']}`")

    return "\n".join(lines)
