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
from .scoring import (
    Floor,
    floor_failures,
    parse_floors,
    parse_score,
    pct_change,
    stat_keys,
    stat_value,
    weighted_score,
)

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


# PoB ConfigTab values. Most Pantheon powers are avoidance PoB does not score, so their delta is often 0.
CONFIG_PRESETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bandit": ("bandit", ("None", "Alira", "Kraityn", "Oak")),
    "pantheon-major": ("pantheonMajorGod", ("None", "TheBrineKing", "Lunaris", "Solaris", "Arakaali")),
    "pantheon-minor": (
        "pantheonMinorGod",
        ("None", "Gruthkul", "Yugul", "Abberath", "Tukohama", "Garukhan", "Ralakesh", "Ryslatha", "Shakari"),
    ),
}


def build_preset_variants(preset: str) -> list[dict[str, Any]]:
    """One sweep variant per option of a build-intrinsic config choice (bandit, pantheon)."""
    var, options = CONFIG_PRESETS[preset]
    return [{"name": f"{var}={opt}", "ops": [{"op": "config", "set": {var: opt}}]} for opt in options]


def build_jewel_variants(items: dict[str, Any]) -> list[dict[str, Any]]:
    """One sweep variant per equipped tree or abyssal jewel: unequip it, so its delta is what the jewel is worth."""
    out: list[dict[str, Any]] = []
    for e in items.get("equippedSlots") or []:
        slot = str(e.get("slotName") or "")
        if not e.get("itemId") or not (e.get("nodeId") is not None or "abyssal" in slot.lower()):
            continue
        name = (e.get("item") or {}).get("name") or "Jewel"
        out.append({"name": f"without {name} @ {slot}", "ops": [{"op": "unequip", "slot": slot}]})
    return out


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


def _score_row(row: dict[str, Any], s: dict[str, Any], base: dict[str, Any], score: dict[str, float], floors: list[Floor]) -> None:
    after: dict[str, float | None] = {}
    pcts: dict[str, float | None] = {}
    for k in stat_keys(score, floors):
        b, v = stat_value(base, k), stat_value(s, k)
        after[k] = v
        pcts[k] = pct_change(b, None if b is None or v is None else v - b)
    if score:
        row["score"] = weighted_score(pcts, score)
    if floors:
        row["floorFail"] = floor_failures(after, floors)


def compute_weights(
    res: dict[str, Any], *, score: dict[str, float] | None = None, floors: list[Floor] | None = None
) -> dict[str, Any]:
    """Turn a sweep result into per-mod DPS/EHP deltas, sorted by DPS% then EHP% (unverified rows last).

    With `score`, rows sort by the weighted percent score instead; rows breaking a floor sort last.
    """
    score, floors = score or {}, floors or []
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
        if score or floors:
            _score_row(row, s, base, score, floors)
        rows.append(row)
    if score:
        rows.sort(key=lambda r: (not r["ok"], bool(r.get("floorFail")), r.get("score") is None, -(r.get("score") or 0.0)))
    else:
        rows.sort(key=lambda r: (not r["ok"], bool(r.get("floorFail")), -(r.get("dpsPct") or 0.0), -(r.get("ehpPct") or 0.0)))
    out: dict[str, Any] = {
        "primaryDpsKey": base.get("primaryDpsKey"),
        "baselineDps": b_dps,
        "baselineEhp": b_ehp,
        "scenario": res.get("scenario"),
        "weights": rows,
        "restoreCheck": res.get("restoreCheck"),
    }
    if score:
        out["score"] = score
    if floors:
        out["floors"] = [f"{k}{op}{lim:g}" for k, op, lim in floors]
    return out


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
    ]
    scored = bool(w.get("score") or w.get("floors"))
    if scored:
        terms = " + ".join(f"{v:g}×{k}%" for k, v in (w.get("score") or {}).items())
        lines.append(f"- Score: {terms or '-'}" + (f" | Floors: {', '.join(w['floors'])}" if w.get("floors") else ""))
    lines += ["", "| Mod | ΔDPS | ΔEHP |" + (" Score |" if scored else ""), "|---|---|---|" + ("---|" if scored else "")]
    for r in w["weights"]:
        mod = _cell(r["mod"])
        if not r["ok"]:
            lines.append(f"| {mod} | error: {_cell(r.get('error'))} | - |" + (" - |" if scored else ""))
            continue
        row = f"| {mod} | {_fmt(r['dpsDelta'], r['dpsPct'])} | {_fmt(r['ehpDelta'], r['ehpPct'])} |"
        if scored:
            sc = r.get("score")
            cell = f"{sc:+.2f}" if sc is not None else "-"
            if r.get("floorFail"):
                cell += f" (breaks {', '.join(r['floorFail'])})"
            row += f" {cell} |"
        lines.append(row)
    lines.append("")
    lines.append("> Δ = 0 may mean the mod has no effect on this build or PoB could not parse the line.")
    warn = restore_warning(w.get("restoreCheck"))
    if warn:
        lines.append(f"> [WARN] {warn}")
    return "\n".join(lines)


def _parse_score_options(score_terms: tuple[str, ...], floor_terms: tuple[str, ...]) -> tuple[dict[str, float], list[Floor]]:
    """Parse --score/--floor and reject stats the sweep summary does not carry, before the slow sweep runs."""
    from .mutators.batch_sweep import _compact_sweep_summary

    try:
        score, floors = parse_score(score_terms), parse_floors(floor_terms)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    known = {k.lower(): k for k in _compact_sweep_summary({}) if k not in {"primaryDpsKey", "points"}}
    unknown = [k for k in stat_keys(score, floors) if k.lower() not in known]
    if unknown:
        raise click.ClickException(f"Unknown --score/--floor stat {', '.join(unknown)}; use one of: {', '.join(sorted(known.values()))}")
    return score, floors


@click.command("weights")
@click.option("--mod", "mods", multiple=True, help="Custom mod line to probe (repeatable; replaces the default probe set).")
@click.option("--scenario", "scenario", help="Optional baseline scenario (e.g. Pinnacle, Uber, FlasksOff).")
@click.option("--keep", "keep", multiple=True, help="Keep build-guaranteed items in Pinnacle scenarios.")
@click.option(
    "--preset",
    type=click.Choice(sorted([*CONFIG_PRESETS, "jewels"])),
    help="Compare every option of a build choice instead of mod lines (bandit 'None' does not allocate its 2 points); "
    "'jewels' removes each equipped jewel in turn, so Δ is what it is worth.",
)
@click.option("--score", "score_terms", multiple=True, help="Rank by weighted % gain, STAT=W (repeatable or comma-joined), e.g. CombinedDPS=1,TotalEHP=0.5.")
@click.option("--floor", "floor_terms", multiple=True, help="Hard constraint on the value after the mod, STAT>=N or STAT<=N (repeatable).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_weights(
    mods: tuple[str, ...],
    scenario: str | None,
    keep: tuple[str, ...],
    preset: str | None,
    score_terms: tuple[str, ...],
    floor_terms: tuple[str, ...],
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """PoB-verified stat weights: DPS/EHP gain of each probe mod line (0-residue rollback)."""
    score, floors = _parse_score_options(score_terms, floor_terms)
    probe = [m.strip() for m in mods if m.strip()] or list(DEFAULT_PROBES)
    if preset and mods:
        raise click.ClickException("--preset and --mod cannot be combined.")
    if preset == "jewels":
        variants = build_jewel_variants(invoke_daemon("items", {"target": target}))
        if not variants:
            raise click.ClickException("No equipped tree or abyssal jewels to compare.")
    else:
        variants = build_preset_variants(preset) if preset else build_weight_variants(probe)
    params: dict[str, Any] = {
        "target": target,
        "variants": variants,
        "base_ops": None,
        "calc_keywords": None,
        "scenario": scenario,
    }
    eff_keep = _merge_keep_tokens(None, keep)
    if eff_keep:
        params["keep"] = eff_keep
    res = invoke_daemon("sweep", params, timeout=300.0)
    weights = compute_weights(res, score=score, floors=floors)
    if as_json:
        _emit_json(weights, compact=compact, out_path=out_path)
        return
    _emit_text(format_weights_markdown(weights, target=target), out_path=out_path)


def register_weights_commands(group: click.Group) -> None:
    group.add_command(cmd_weights)
