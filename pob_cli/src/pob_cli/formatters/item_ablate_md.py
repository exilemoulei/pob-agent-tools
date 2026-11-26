"""Markdown formatter for single-mod item ablation (`pob item inspect <slot> --ablate`)."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, fmt_signed_pct, render_table, restore_warning, strip_pob_escapes


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

    ranked = sorted(deltas, key=_row_sort_key)
    show_pool = any(_fmt_pool_supp_deltas(d) != "-" for d in ranked)
    show_ms = any(abs(float(d.get("deltaMovementSpeed") or 0.0)) > 1e-4 for d in ranked)
    rows: list[list[Any]] = []
    for d in ranked:
        mod_en = strip_pob_escapes(str(d.get("modText") or "-"))
        mod_zh = strip_pob_escapes(str(d.get("modTextZh") or ""))
        mod_disp = f"{mod_zh} ({mod_en})" if (mod_zh and mod_zh != mod_en) else mod_en
        flags: list[str] = []
        if d.get("bricksRequirements"):
            flags.append("[REQ-LOAD-BEARING]")
        if d.get("isDeadMod"):
            flags.append("[DEAD-MOD]")
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
        row.append(" ".join(flags) or "-")
        rows.append(row)

    headers = ["#", "Category", "Mod (toggled OFF)", f"Δ{dps_key}", "ΔTotalEHP"]
    if show_pool:
        headers.append("ΔPool/Supp")
    headers.extend(["ΔRes", "ΔAttr"])
    if show_ms:
        headers.append("ΔMS")
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
            "_Note: [DEAD-MOD] means 0 direct PoB DPS/EHP/res/attr/speed change under current Config; "
            "verify mana-cost, flask, or on-kill utility before annulling._"
        )
    return "\n".join(lines)
