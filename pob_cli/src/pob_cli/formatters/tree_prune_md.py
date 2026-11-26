"""Markdown formatters for `pob tree prune` and threshold gap probes."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, render_table, strip_pob_escapes


def _fmt_pct(val: Any) -> str:
    if val is None:
        return "-"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "-"
    return f"{f:+.2f}%"


def format_tree_prune_markdown(res: dict[str, Any], *, include_unsafe: bool = False) -> str:
    """Format `pob tree prune` into a multi-dimensional per-point delta & guardrail Markdown table."""
    dps_key = res.get("dpsKey") or "CombinedDPS"
    safe = res.get("safeBranches") or []
    unsafe = res.get("unsafeBranches") or []
    elapsed = res.get("elapsedMs")
    time_tag = f" | `{elapsed:.0f}ms`" if isinstance(elapsed, (int, float)) else ""

    lines: list[str] = [
        f"### Passive Tree Prune Candidates (`{dps_key}` base `{fmt_num(res.get('baseDps'))}`, EHP `{fmt_num(res.get('baseEhp'))}`, sort: `{res.get('sort', 'dps')}`, max `{res.get('maxBranchPts', 5)}` pts{time_tag})",
        f"- **Evaluated**: `{res.get('evaluatedCount', 0)}` branches (`{res.get('safeCount', 0)}` safe, `{res.get('unsafeCount', 0)}` unsafe)",
    ]

    def _rows(items: list[dict[str, Any]]) -> list[list[Any]]:
        out: list[list[Any]] = []
        for idx, b in enumerate(items, start=1):
            lbl = strip_pob_escapes(str(b.get("labelZh") or b.get("label") or "-"))
            gr = b.get("guardrails") or {}
            viols = gr.get("violations") or []
            status = "SAFE" if b.get("safe") else f"[UNSAFE: {'; '.join(viols)}]"
            out.append(
                [
                    idx,
                    b.get("points"),
                    _fmt_pct(b.get("dpsPct")),
                    _fmt_pct(b.get("dpsPctPerPt")),
                    _fmt_pct(b.get("ehpPct")),
                    _fmt_pct(b.get("ehpPctPerPt")),
                    lbl,
                    status,
                    f"`{b.get('deallocCommand') or '-'}`",
                ]
            )
        return out

    headers = ["#", "Pts Freed", "ΔDPS%", "ΔDPS%/pt", "ΔEHP%", "ΔEHP%/pt", "Nodes / Wheel", "Guardrail", "Dealloc Command"]
    if safe:
        lines.append("\n#### Safe Prunable Branches")
        lines.append(render_table(headers, _rows(safe)))
    else:
        lines.append("\n#### Safe Prunable Branches\n- _(No safe prunable branches within point limit.)_")

    if include_unsafe and unsafe:
        lines.append("\n#### Unsafe Branches (Guardrail Violations)")
        lines.append(render_table(headers, _rows(unsafe)))
    elif unsafe:
        lines.append(f"\n_({len(unsafe)} unsafe branch(es) hidden due to attribute/resist/reservation/cliff guardrails; pass `--include-unsafe` to inspect.)_")

    return "\n".join(lines)


def format_threshold_probes_markdown(probes: dict[str, Any], *, compact: bool = False) -> list[str]:
    """Render concise threshold gap probe lines for `audit`, `diagnose`, and `rank-nodes`."""
    if not isinstance(probes, dict) or not probes:
        return []
    lines: list[str] = []

    supp = probes.get("spellSuppression") or {}
    if isinstance(supp, dict) and supp.get("currentPct") is not None:
        cur = float(supp["currentPct"])
        gap = float(supp.get("gapTo100Pct") or 0.0)
        over = float(supp.get("overcapPct") or 0.0)
        if cur <= 0.0:
            s_txt = "0% (not invested)"
        elif gap > 0:
            s_txt = f"`{cur:.0f}%` (gap **`-{gap:.0f}%`** to 100%)"
        elif over > 0:
            s_txt = f"`{cur:.0f}%` (capped, **`+{over:.0f}%`** overcap surplus)"
        else:
            s_txt = "`100%` (exact cap)"
        lines.append(f"- **Spell Suppression**: {s_txt}")

    mre = probes.get("manaReservation") or {}
    if isinstance(mre, dict) and int(mre.get("maxMana") or 0) > 0:
        a25 = mre.get("extra25Aura") or {}
        a50 = mre.get("extra50Aura") or {}

        def _fmt_aura(a: dict[str, Any], label: str) -> str:
            if a.get("fitsNow"):
                return f"+{label} aura fits now (`{a.get('unreservedAfter')}` mana left)"
            need = a.get("additionalMreNeededPct")
            if need is not None:
                return f"+{label} aura needs **`+{need}%` MRE**"
            return f"+{label} aura needs >1000% MRE"

        lines.append(
            f"- **Mana Reservation Efficiency** (current `{mre.get('currentMrePct', 0)}%`, "
            f"unreserved `{mre.get('manaUnreserved', 0)}/{mre.get('maxMana', 0)}`, cost `{mre.get('manaCost', 0)}`): "
            f"{_fmt_aura(a25, '25%')} | {_fmt_aura(a50, '50%')}"
        )

    trig = probes.get("triggerBreakpoint")
    if isinstance(trig, dict) and trig.get("triggerRateCap"):
        aps = trig.get("effectiveAps", 0)
        cap = trig.get("triggerRateCap", 0)
        hr_aps = trig.get("headroomAps")
        hr_ias = trig.get("headroomIasPct")
        if trig.get("overspeedCliff"):
            lines.append(
                f"- **[OVERSPEED CLIFF] Trigger (`{trig.get('triggerName')}`, `{trig.get('ticks')}t`=`{trig.get('effectiveCdSec')}s`)**: "
                f"APS `{aps}/s` exceeds cap `{cap}/s` (reduce APS by `{-hr_aps if hr_aps is not None else 0:.2f}/s` or add CDR!)"
            )
        else:
            ias_part = f" (`+{hr_ias:.1f}%` IAS headroom)" if hr_ias is not None else ""
            lines.append(
                f"- **Trigger Breakpoint (`{trig.get('triggerName')}`, `{trig.get('ticks')}t`=`{trig.get('effectiveCdSec')}s`)**: "
                f"APS `{aps}/s` vs cap `{cap}/s` — headroom `+{hr_aps if hr_aps is not None else 0:.2f}/s`{ias_part}"
            )

    acc = probes.get("accuracy")
    attrs = probes.get("attributes") or {}
    tail_bits: list[str] = []
    if isinstance(acc, dict) and acc.get("hitChancePct") is not None:
        gap_acc = float(acc.get("gapTo100Pct") or 0.0)
        if gap_acc > 0:
            tail_bits.append(f"Accuracy `{acc['hitChancePct']:.1f}%` (**`-{gap_acc:.1f}%`** to 100%)")
        elif not compact:
            tail_bits.append("Accuracy `100%`")
    if isinstance(attrs, dict):
        attr_strs = []
        for a in ("Str", "Dex", "Int"):
            entry = attrs.get(a)
            if isinstance(entry, dict) and (entry.get("required") or entry.get("current")):
                s = int(entry.get("surplus") or 0)
                sign = f"{s:+d}"
                tag = " [DEFICIT]" if s < 0 else (" [TIGHT]" if entry.get("status") == "tight" else "")
                attr_strs.append(f"{a} `{entry.get('current')}/{entry.get('required')}` ({sign}{tag})")
        if attr_strs:
            tail_bits.append("Attributes: " + ", ".join(attr_strs))
    if tail_bits:
        lines.append("- " + " | ".join(tail_bits))

    return lines
