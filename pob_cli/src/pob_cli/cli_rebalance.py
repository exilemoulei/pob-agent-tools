"""Click CLI command `pob rebalance` for CoW-verified zero-buy stat rebalancing (L4)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from .cli_common import _emit_json, _emit_text, _output_options, _target_option, invoke_daemon
from .formatters.rebalance_md import format_rebalance_markdown


def _read_try_item_source(raw_arg: str) -> str:
    s = raw_arg.strip()
    if s == "-":
        return sys.stdin.read().lstrip("\ufeff")
    if s.startswith("@"):
        return Path(s[1:]).read_text(encoding="utf-8-sig").lstrip("\ufeff")
    p = Path(s)
    if "\n" not in s and p.is_file():
        return p.read_text(encoding="utf-8-sig").lstrip("\ufeff")
    return raw_arg.lstrip("\ufeff")


@click.command("rebalance")
@click.option(
    "--try-item",
    "try_item",
    type=str,
    default=None,
    help="Preview equipping a candidate item (`-` for stdin, file path, `@file`, or raw text) before solving.",
)
@click.option(
    "--slot",
    "slot_name",
    type=str,
    default=None,
    help="Target equipment slot for `--try-item` (e.g. 'Belt', 'Ring 1', 'Body Armour').",
)
@click.option(
    "--max-steps",
    "max_steps",
    type=click.IntRange(1, 3),
    default=3,
    show_default=True,
    help="Maximum orthogonal zero-buy steps per candidate plan (1..3).",
)
@click.option(
    "--max-evals",
    "max_evals",
    type=click.IntRange(1, 60),
    default=24,
    show_default=True,
    help="Maximum CoW PoB combo evaluations per solve (1..60).",
)
@click.option("--include-chaos", "include_chaos", is_flag=True, default=False, help="Also target negative Chaos Resistance -> 0%.")
@click.option(
    "--include-suppression",
    "include_suppression",
    is_flag=True,
    default=False,
    help="Also target Spell Suppression gap to 100%.",
)
@click.option(
    "--apply-plan",
    "apply_plan",
    type=click.IntRange(min=1),
    default=None,
    help="Persist the 1-based verified plan index (and `--try-item` if given) into the build.",
)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output lossless JSON.")
@_output_options
def cmd_rebalance(
    try_item: str | None,
    slot_name: str | None,
    max_steps: int,
    max_evals: int,
    include_chaos: bool,
    include_suppression: bool,
    apply_plan: int | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Solve attribute & resistance deficits with zero-buy Bench/Harvest/Catalyst/Tattoo steps verified in PoB CoW."""
    if slot_name and not try_item:
        raise click.UsageError("`--slot` requires `--try-item`.")
    raw_text = _read_try_item_source(try_item) if try_item is not None else None
    if try_item is not None and (not raw_text or not raw_text.strip()):
        raise click.UsageError("`--try-item` text is empty.")

    res: dict[str, Any] = invoke_daemon(
        "rebalance",
        {
            "target": target,
            "try_item_raw": raw_text,
            "slot": slot_name,
            "max_steps": max_steps,
            "max_evals": max_evals,
            "include_chaos": include_chaos,
            "include_suppression": include_suppression,
            "apply_plan": apply_plan,
        },
        timeout=150.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_rebalance_markdown(res), out_path=out_path)
