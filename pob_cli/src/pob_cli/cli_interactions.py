"""Click CLI subcommand `pob interactions` (ModParser flag compatibility & red uncomputed mod scanner)."""

from __future__ import annotations

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _prepend_warning,
    _target_option,
    invoke_daemon,
)


@click.command("interactions")
@click.option("--slot", "slot_name", default=None, help="Inspect an equipped slot's mod lines against the active main skill.")
@click.option(
    "--mod",
    "mod_lines",
    multiple=True,
    help="Parse and classify an arbitrary candidate mod line against the active main skill (repeatable).",
)
@click.option(
    "--unsupported-only",
    "unsupported_only",
    is_flag=True,
    default=False,
    help="Show only PoB unparsed red mods (^xF05050, 0 calculation effect).",
)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
@_output_options
def cmd_interactions(
    slot_name: str | None,
    mod_lines: tuple[str, ...],
    unsupported_only: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Inspect ModParser interactions, main-skill flag mismatches, and uncomputed red mods."""
    from .formatters.interactions_md import format_interactions_markdown
    from .guardrails import GuardrailScope, check_input

    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    for ln in mod_lines:
        check_input(ln, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")

    res = invoke_daemon(
        "interactions_inspect",
        {
            "target": target,
            "slot": slot_name,
            "mods": list(mod_lines),
            "unsupported_only": unsupported_only,
        },
        timeout=30.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    md = _prepend_warning(format_interactions_markdown(res), res)
    _emit_text(md, out_path=out_path)
