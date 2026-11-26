"""Markdown formatter for single-mod item ablation (`pob item inspect <slot> --ablate`)."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, fmt_signed_pct, render_table, restore_warning, strip_pob_escapes

_OTHER_STATS_SHOWN = 3


def _fmt_res_deltas(d_res: dict[str, Any], d_ov: dict[str, Any] | None = None) -> str:
    parts: list[str] = []
    ov = d_ov if isinstance(d_ov, dict) else {}
    for key, tag in (("fire", "F"), ("cold", "C"), ("lightning", "L"), ("chaos", "Ch")):
        v = float(d_res.get(key) or 0.0)
        v_ov = float(ov.get(key) or 0.0)
        if abs(v) > 1e-4:
            parts.append(f"{tag}{v:+.0f}%")
        elif abs(v_ov) > 1e-4:
            parts.append(f"{tag}{v_ov:+.0f}%(ov)")
    return " ".join(parts) if parts else "-"


def _fmt_attr_deltas(d_attr: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, tag in (("str", "Str"), ("dex", "Dex"), ("int", "Int"), ("omni", "Omni")):
        v = float(d_attr.get(key) or 0.0)
        if abs(v) > 1e-4:
            parts.append(f"{tag}{v:+.0f}")
    return " ".join(parts) if parts else "-"


def _fmt_pool_supp_deltas(d: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, tag, unit in (
        ("deltaLife", "Life", ""),
        ("deltaMana", "Mana", ""),
        ("deltaEnergyShield", "ES", ""),
        ("deltaSuppression", "Supp", "%"),
    ):
        v = float(d.get(key) or 0.0)
        if abs(v) > 1e-4:
            parts.append(f"{tag}{v:+.0f}{unit}")
    util = d.get("deltaUtility") if isinstance(d.get("deltaUtility"), dict) else {}
    for key, tag, unit in (
        ("ward", "Ward", ""),
        ("block", "Block", "%"),
        ("spellBlock", "SBlock", "%"),
        ("lifeRegen", "LReg", "/s"),
        ("manaRegen", "MReg", "/s"),
        ("esRegen", "ESReg", "/s"),
        ("manaCost", "MCost", ""),
        ("lifeOnHit", "LoH", ""),
        ("lifeLeech", "LLeech", "/s"),
    ):
        v = float(util.get(key) or 0.0)
        if abs(v) > 1e-4:
            parts.append(f"{tag}{v:+.0f}{unit}")
    return " ".join(parts) if parts else "-"


def _fmt_ehp_cell(d: dict[str, Any]) -> str:
    trans = d.get("ehpTransition")
    if isinstance(trans, str) and "->" in trans:
        lhs, rhs = trans.split("->", 1)
        def _fmt_side(tok: str) -> str:
            t = tok.strip()
            if t.lower() in ("inf", "-inf", "nan"):
                return t
            try:
                return fmt_num(float(t))
            except ValueError:
                return t

        return f"{_fmt_side(lhs)} -> {_fmt_side(rhs)}"
    return fmt_signed_pct(d.get("deltaEhp"), d.get("deltaEhpPct"))


def _fmt_other_stats(d: dict[str, Any]) -> str:
    """Names of what else PoB recalculated, for a row whose own columns are all empty."""
    others = [str(k) for k in (d.get("otherChangedStats") or [])]
    if not others:
        return "-"
    more = int(d.get("changedStatCount") or 0) - _OTHER_STATS_SHOWN
    return ", ".join(others[:_OTHER_STATS_SHOWN]) + (f" (+{more})" if more > 0 else "")


def _row_sort_key(d: dict[str, Any]) -> tuple[Any, ...]:
    is_dead = 1 if d.get("isDeadMod") else 0
    bricks = 0 if d.get("bricksRequirements") else 1
    trans = str(d.get("ehpTransition") or "")
    inf_break = 0 if ("->" in trans and trans.split("->", 1)[0] != trans.split("->", 1)[1]) else 1
    impact = abs(float(d.get("deltaDpsPct") or 0.0)) + abs(float(d.get("deltaEhpPct") or 0.0))
    return (is_dead, bricks, inf_break, -round(impact, 4), int(d.get("lineIndex") or 0))


def format_item_ablation_markdown(ablation: dict[str, Any]) -> str:
    """Render concise bilingual-friendly single-mod ablation table highlighting dead & load-bearing mods."""
    if not isinstance(ablation, dict):
        return ""
    slot = ablation.get("slotName") or "-"
    name = strip_pob_escapes(str(ablation.get("itemName") or "-"))
    name_zh = strip_pob_escapes(str(ablation.get("itemNameZh") or ""))
    disp = f"{name_zh} ({name})" if (name_zh and name_zh != name) else name
    base = ablation.get("baseline") or {}
    dps_key = ablation.get("primaryDpsKey") or base.get("primaryDpsKey") or "CombinedDPS"

    lines: list[str] = [
        f"#### Single-Mod Ablation (单词缀消融): `{slot}` — {disp}",
        f"- **Baseline**: `{dps_key}` `{fmt_num(base.get('primaryDps'))}` | `TotalEHP` `{fmt_num(base.get('totalEhp'))}`",
    ]
    r_warn = restore_warning(ablation.get("restoreCheck"))
    if r_warn:
        lines.append(r_warn)

    deltas = list(ablation.get("ablation_deltas") or [])
    if not deltas:
        lines.append("- _(No toggleable mod lines found on this item.)_")
        return "\n".join(lines)
    if ablation.get("slotActive", True) is False:
        lines.append(
            "- **Inactive slot**: this item is outside the current calculation (swapped-out weapon set, "
            "unticked flask or unallocated socket), so every line reads 0 and none is flagged."
        )

    ranked = sorted(deltas, key=_row_sort_key)
    show_pool = any(_fmt_pool_supp_deltas(d) != "-" for d in ranked)
    show_ms = any(abs(float(d.get("deltaMovementSpeed") or 0.0)) > 1e-4 for d in ranked)
    rows: list[list[Any]] = []
    other_cells: list[str] = []
    for d in ranked:
        mod_en = strip_pob_escapes(str(d.get("modText") or "-"))
        mod_zh = strip_pob_escapes(str(d.get("modTextZh") or ""))
        mod_disp = f"{mod_zh} ({mod_en})" if (mod_zh and mod_zh != mod_en) else mod_en
        flags: list[str] = []
        if d.get("bricksRequirements"):
            flags.append("[REQ-LOAD-BEARING]")
        if d.get("isDeadMod"):
            flags.append("[DEAD-MOD]")
        if d.get("unsupported"):
            flags.append("[UNSUPPORTED]")
        if d.get("unmeasuredReason"):
            flags.append("[NOT-MEASURED]")
        row: list[Any] = [
            d.get("lineIndex"),
            d.get("slotCategory") or "-",
            mod_disp,
            fmt_signed_pct(d.get("deltaDps"), d.get("deltaDpsPct")),
            _fmt_ehp_cell(d),
        ]
        if show_pool:
            row.append(_fmt_pool_supp_deltas(d))
        row.extend(
            [
                _fmt_res_deltas(d.get("deltaResists") or {}, d.get("deltaResistOverCaps")),
                _fmt_attr_deltas(d.get("deltaAttributes") or {}),
            ]
        )
        if show_ms:
            ms_v = float(d.get("deltaMovementSpeed") or 0.0)
            row.append(f"{ms_v:+.0f}%" if abs(ms_v) > 1e-4 else "-")
        # A line can be live with every column empty (reservation, ailment avoidance, recovery):
        # name what moved, so the missing [DEAD-MOD] flag is explained.
        ehp_side = [t.strip() for t in str(d.get("ehpTransition") or "").split("->")]
        ehp_still = len(ehp_side) == 2 and ehp_side[0] == ehp_side[1]
        empty = all(c in ("0", "-") or (i == 1 and ehp_still) for i, c in enumerate(row[3:]))
        other_cells.append(_fmt_other_stats(d) if empty else "-")
        row.append(" ".join(flags) or "-")
        rows.append(row)

    headers = ["#", "Category", "Mod (toggled OFF)", f"Δ{dps_key}", "ΔTotalEHP"]
    if show_pool:
        headers.append("ΔPool/Supp")
    headers.extend(["ΔRes", "ΔAttr"])
    if show_ms:
        headers.append("ΔMS")
    if any(c != "-" for c in other_cells):
        headers.append("Other")
        for row, cell in zip(rows, other_cells, strict=True):
            row.insert(len(row) - 1, cell)
    headers.append("Flags")
    lines.append(render_table(headers, rows))

    dead_cnt = int(ablation.get("deadModCount") or 0)
    craftable = bool(ablation.get("craftable", True))
    annullable_cnt = (
        int(ablation.get("annullableDeadModCount") or 0)
        if "annullableDeadModCount" in ablation
        else (
            sum(
                1
                for d in deltas
                if craftable and d.get("isDeadMod") and d.get("slotCategory") in ("explicit", "crafted")
            )
        )
    )
    lb_cnt = int(ablation.get("loadBearingCount") or 0)
    summary_bits: list[str] = []
    if craftable and annullable_cnt > 0:
        summary_bits.append(
            f"`{annullable_cnt}` 0-impact explicit/crafted mod(s) under current Config "
            "(annulment is random; verify recovery/block/utility first)"
        )
    if lb_cnt:
        summary_bits.append(f"`{lb_cnt}` `[REQ-LOAD-BEARING]` mod(s) — removing bricks attribute/item requirements")
    if summary_bits:
        lines.append("- **Crafting Notes**: " + "; ".join(summary_bits) + ".")
    if dead_cnt > 0 or any(d.get("isDeadMod") for d in deltas):
        lines.append(
            "_Note: [DEAD-MOD] means no stat PoB calculates for the main skill changed under the current Config; "
            "a line for another skill of the build, or utility PoB does not model, can still matter before annulling._"
        )
    if any(d.get("unmeasuredReason") for d in deltas):
        lines.append(
            "_Note: [NOT-MEASURED] lines cannot be isolated by switching them off (a flask's base buff, passives a "
            "jewel lets the build allocate, a cluster jewel's layout, a granted skill that moves the main skill): "
            "compare with `pob item unequip`, `pob item try` or `pob item flask` instead._"
        )
    disabled_cnt = int(ablation.get("disabledLineCount") or 0)
    if disabled_cnt:
        lines.append(f"_Note: `{disabled_cnt}` line(s) already disabled on the item were left out._")
    if any(d.get("unsupported") for d in deltas):
        lines.append("_Note: [UNSUPPORTED] lines are ones PoB cannot parse, so their effect was not measured._")

    pairs = ablation.get("pairwiseSynergies")
    if isinstance(pairs, list):
        top_k = ablation.get("synergyTop") or 5
        lines.append(f"\n#### Pairwise Synergy (Top-{top_k} 两两协同消融)")
        if not pairs:
            lines.append("- _(Fewer than 2 eligible active mod lines for pairwise synergy.)_")
        else:
            p_rows: list[list[Any]] = []
            for p in pairs:
                flags: list[str] = []
                if p.get("jointReqCollapse"):
                    reasons = "; ".join(str(r) for r in (p.get("jointCollapseReasons") or []))
                    flags.append(f"[JOINT-REQ-COLLAPSE: {reasons}]" if reasons else "[JOINT-REQ-COLLAPSE]")
                if p.get("nonTrivialSynergy"):
                    flags.append("[SYNERGY]")
                ehp_syn = (
                    f"{float(p['synergyEhpPct']):+.2f}%"
                    if p.get("synergyEhpPct") is not None
                    else "-"
                )
                p_rows.append(
                    [
                        f"#{p.get('lineIndexA')} + #{p.get('lineIndexB')}",
                        f"{strip_pob_escapes(str(p.get('modTextA') or ''))} + {strip_pob_escapes(str(p.get('modTextB') or ''))}",
                        f"{float(p.get('jointDpsDeltaPct') or 0.0):+.2f}%",
                        f"{float(p.get('additiveDpsDeltaPct') or 0.0):+.2f}%",
                        f"{float(p.get('synergyDpsPct') or 0.0):+.2f}%",
                        ehp_syn,
                        " ".join(flags) or "-",
                    ]
                )
            lines.append(
                render_table(
                    ["Pair", "Mods (both OFF)", "Joint ΔDPS%", "Add ΔDPS%", "Synergy ΔDPS%", "Synergy ΔEHP%", "Flags"],
                    p_rows,
                )
            )
    return "\n".join(lines)
