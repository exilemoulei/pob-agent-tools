# SPDX-License-Identifier: GPL-3.0-only
"""Shared CLI helpers for JSON/Markdown output, daemon invocation, and exit-code handling (L4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from .compat import ensure_utf8
from .daemon import call_daemon
from .errors import (
    EXIT_DAEMON_ERROR,
    EXIT_REQUEST_ERROR,
    BootstrapMissingError,
    CrossGameHallucinationError,
    DaemonRequestError,
    LegacyPatchTrapError,
)
from .game_profile import default_slot


def echo_utf8(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def emit_output(
    ctx: click.Context | None,
    payload: dict[str, Any],
    *,
    as_json: bool = False,
    markdown_text: str | None = None,
    out_path: str | None = None,
) -> None:
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    compact = bool(root_obj.get("compact"))
    eff_out = out_path or root_obj.get("out")

    if as_json or markdown_text is None:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
    else:
        text = markdown_text

    if eff_out:
        p = Path(eff_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8", newline="\n")
        return
    echo_utf8(text)


def resolve_target_slot(target: str | None) -> str:
    return (target or "").strip().lower() or default_slot()


def daemon_call_cli(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Invoke local PoE2 daemon and translate transport/fault errors to exit code 5."""
    try:
        return call_daemon(action, params or {}, auto_start=auto_start, timeout=timeout)
    except (CrossGameHallucinationError, LegacyPatchTrapError, DaemonRequestError):
        raise
    except BootstrapMissingError as exc:
        echo_utf8(f"Error: {exc}", err=True)
        raise SystemExit(EXIT_REQUEST_ERROR) from exc
    except Exception as exc:
        echo_utf8(f"Daemon Error: {exc}", err=True)
        raise SystemExit(EXIT_DAEMON_ERROR) from exc


__all__ = [
    "daemon_call_cli",
    "echo_utf8",
    "emit_output",
    "resolve_target_slot",
]
