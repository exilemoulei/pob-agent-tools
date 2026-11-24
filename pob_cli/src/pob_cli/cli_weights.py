"""`pob weights`: PoB-verified marginal value of generic stat probes (DPS/EHP per mod line) via one sweep RPC."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _merge_keep_tokens,
    _output_options,
    _target_option,
    invoke_daemon,
)
from .formatters import restore_warning

# Generic PoB custom-mod probes; each is applied alone on top of the baseline and rolled back.
DEFAULT_PROBES: tuple[str, ...] = (
    "10% increased Damage",
    "10% increased Attack Speed",
    "10% increased Cast Speed",
    "10% increased Critical Strike Chance",
    "+10% to Critical Strike Multiplier",
    "Damage Penetrates 5% Elemental Resistances",
    "+100 to Accuracy Rating",
    "+10 to all Attributes",
    "+50 to maximum Life",
    "5% increased maximum Life",
    "+50 to maximum Energy Shield",
    "+500 to Armour",
    "+500 to Evasion Rating",
    "+10% to all Elemental Resistances",
    "+1% to all maximum Elemental Resistances",
    "+5% chance to Suppress Spell Damage",
)


def build_weight_variants(mods: list[str]) -> list[dict[str, Any]]:
    """One sweep variant per mod line, each appending that line to the build's customMods."""
    return [{"name": m, "ops": [{"op": "config", "appendCustomMods": m}]} for m in mods]


def _num(val: Any) -> float | None:
    return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else None


def _delta(base: float | None, new: float | None) -> tuple[float | None, float | None]:
    if base is None or new is None:
        return None, None
    d = new - base
    return d, (d / base * 100.0 if base else None)


def compute_weights(res: dict[str, Any]) -> dict[str, Any]:
    """Turn a sweep result into per-mod DPS/EHP deltas, sorted by DPS% then EHP% (unverified rows last)."""
    base = (res.get("baseline") or {}).get("summary") or {}
    b_dps, b_ehp = _num(base.get("primaryDps")), _num(base.get("totalEhp"))
    rows: list[dict[str, Any]] = []
    for var in res.get("variants") or []:
        row: dict[str, Any] = {"mod": var.get("name"), "ok": bool(var.get("ok"))}
        if not row["ok"]:
            row["error"] = var.get("error")
            rows.append(row)
            continue
        s = var.get("summary") or {}
        row["dpsDelta"], row["dpsPct"] = _delta(b_dps, _num(s.get("primaryDps")))
        row["ehpDelta"], row["ehpPct"] = _delta(b_ehp, _num(s.get("totalEhp")))
        rows.append(row)
    rows.sort(key=lambda r: (not r["ok"], -(r.get("dpsPct") or 0.0), -(r.get("ehpPct") or 0.0)))
    return {
        "primaryDpsKey": base.get("primaryDpsKey"),
        "baselineDps": b_dps,
        "baselineEhp": b_ehp,
        "scenario": res.get("scenario"),
        "weights": rows,
        "restoreCheck": res.get("restoreCheck"),
    }


def _fmt(d: float | None, pct: float | None) -> str:
    if d is None:
        return "-"  # unverified, never shown as 0 (R13 spirit)
    return f"{d:+,.0f}" + (f" ({pct:+.2f}%)" if pct is not None else "")


def _cell(v: Any) -> str:
    # Lua tracebacks are multi-line and mod text may hold '|': either breaks the table row.
    lines = str(v or "").splitlines()
    return (lines[0] if lines else "").replace("|", "\\|")


def format_weights_markdown(w: dict[str, Any], *, target: str) -> str:
    def _base(v: float | None) -> str:
        return f"{v:,.0f}" if v is not None else "-"

    lines = [
        f"### Stat Weights (`{target}`)",
        f"- Baseline {w.get('primaryDpsKey') or 'DPS'}: {_base(w.get('baselineDps'))}"
        f" | TotalEHP: {_base(w.get('baselineEhp'))}"
        + (f" | Scenario: {w['scenario']}" if w.get("scenario") else ""),
        "",
        "| Mod | ΔDPS | ΔEHP |",
        "|---|---|---|",
    ]
    for r in w["weights"]:
        mod = _cell(r["mod"])
        if not r["ok"]:
            lines.append(f"| {mod} | error: {_cell(r.get('error'))} | - |")
            continue
        lines.append(f"| {mod} | {_fmt(r['dpsDelta'], r['dpsPct'])} | {_fmt(r['ehpDelta'], r['ehpPct'])} |")
    lines.append("")
    lines.append("> Δ = 0 may mean the mod has no effect on this build or PoB could not parse the line.")
    warn = restore_warning(w.get("restoreCheck"))
    if warn:
        lines.append(f"> [WARN] {warn}")
    return "\n".join(lines)


@click.command("weights")
@click.option("--mod", "mods", multiple=True, help="Custom mod line to probe (repeatable; replaces the default probe set).")
@click.option("--scenario", "scenario", help="Optional baseline scenario (e.g. Pinnacle, Uber, FlasksOff).")
@click.option("--keep", "keep", multiple=True, help="Keep build-guaranteed items in Pinnacle scenarios.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_weights(
    mods: tuple[str, ...],
    scenario: str | None,
    keep: tuple[str, ...],
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """PoB-verified stat weights: DPS/EHP gain of each probe mod line (0-residue rollback)."""
    probe = [m.strip() for m in mods if m.strip()] or list(DEFAULT_PROBES)
    params: dict[str, Any] = {
        "target": target,
        "variants": build_weight_variants(probe),
        "base_ops": None,
        "calc_keywords": None,
        "scenario": scenario,
    }
    eff_keep = _merge_keep_tokens(None, keep)
    if eff_keep:
        params["keep"] = eff_keep
    res = invoke_daemon("sweep", params, timeout=300.0)
    weights = compute_weights(res)
    if as_json:
        _emit_json(weights, compact=compact, out_path=out_path)
        return
    _emit_text(format_weights_markdown(weights, target=target), out_path=out_path)


def register_weights_commands(group: click.Group) -> None:
    group.add_command(cmd_weights)
