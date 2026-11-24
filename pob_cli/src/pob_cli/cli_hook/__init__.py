"""Click `pob hook` subcommand group exposing deterministic pre-prompt, pre-tool, and verify-stop gates."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import click

from .gates import handle_pre_prompt, handle_pre_tool, handle_verify_stop
from .protocols import decode_input, encode_output, read_stdin_json

_PROTOCOLS = click.Choice(["raw", "agy", "claude"], case_sensitive=False)


def _emit_compact_json(obj: dict[str, Any]) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _run_gate(
    gate: str,
    protocol: str,
    handler: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    try:
        payload = read_stdin_json()
        canonical_in, meta = decode_input(gate, protocol, payload)
        canonical_out = handler(canonical_in)
        encoded = encode_output(gate, protocol, canonical_out, meta=meta)
    except Exception:
        encoded = {"decision": "allow"} if gate == "pre-tool" and protocol.lower() in ("raw", "agy") else {}
    _emit_compact_json(encoded)


@click.group("hook")
def hook_group() -> None:
    """Deterministic lifecycle hooks (pre-prompt, pre-tool, verify-stop) for agent harnesses."""


@hook_group.command("pre-prompt")
@click.option(
    "--protocol",
    type=_PROTOCOLS,
    default="raw",
    show_default=True,
    help="Host stdin/stdout protocol adapter.",
)
def cmd_hook_pre_prompt(protocol: str) -> None:
    """Gate 1: anchor turn cursor and inject offline 3.30 grounding context."""
    _run_gate("pre-prompt", protocol, handle_pre_prompt)


@hook_group.command("pre-tool")
@click.option(
    "--protocol",
    type=_PROTOCOLS,
    default="raw",
    show_default=True,
    help="Host stdin/stdout protocol adapter.",
)
def cmd_hook_pre_tool(protocol: str) -> None:
    """Gate 2: block forbidden shell patterns or rewrite bare --json invocations."""
    _run_gate("pre-tool", protocol, handle_pre_tool)


@hook_group.command("verify-stop")
@click.option(
    "--protocol",
    type=_PROTOCOLS,
    default="raw",
    show_default=True,
    help="Host stdin/stdout protocol adapter.",
)
def cmd_hook_verify_stop(protocol: str) -> None:
    """Gate 3: verify assistant reply against V1..V4 receipts/rules and auto-remediate once."""
    _run_gate("verify-stop", protocol, handle_verify_stop)


__all__ = [
    "cmd_hook_pre_prompt",
    "cmd_hook_pre_tool",
    "cmd_hook_verify_stop",
    "hook_group",
]
