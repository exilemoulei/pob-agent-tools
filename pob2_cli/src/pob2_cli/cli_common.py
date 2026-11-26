# SPDX-License-Identifier: GPL-3.0-only
"""Shared CLI decorators, unified JSON envelope, daemon RPC wrapper, and 0/1/5 exit-code boundary (L4)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from .bridge_client import sanitize_bridge_error
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
from .game_profile import GAME_ID, default_slot
from .guardrails import GuardrailScope, check_input, resolve_poe2_slot_name

_SLOT_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_SLOT_ALIAS_META_KEY = "pob2_build_slot_alias"
SLOT_ALIAS_PARAM = "pob2_build_slot_alias"


def echo_utf8(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def ctx_wants_json(ctx: click.Context | None) -> bool:
    """Return True if `--json` was passed on either the root group or leaf subcommand."""
    cur = ctx
    while cur is not None:
        if cur.params.get("as_json") or cur.meta.get("pob2_as_json"):
            return True
        if isinstance(cur.obj, dict) and cur.obj.get("as_json"):
            return True
        raw_args = cur.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in raw_args:
            return True
        cur = cur.parent
    return False


def _extract_raw_opt_value(raw_args: list[str] | tuple[str, ...], flag: str) -> str | None:
    for idx, tok in enumerate(raw_args):
        if tok == flag and idx + 1 < len(raw_args):
            return raw_args[idx + 1]
        if tok.startswith(f"{flag}="):
            return tok.split("=", 1)[1]
    return None


def resolve_output_flags(
    ctx: click.Context | None,
    *,
    compact: bool = False,
    out_path: str | Path | None = None,
    ignore_ctx_out: bool = False,
) -> tuple[bool, Path | None]:
    """Resolve effective `(compact, out_path)` across leaf options, eager raw args, and root `ctx.obj`."""
    eff_compact = bool(compact)
    eff_out: str | Path | None = out_path
    cur = ctx
    while cur is not None:
        if cur.params.get("compact") or cur.meta.get("pob2_compact"):
            eff_compact = True
        if not ignore_ctx_out and eff_out is None:
            cand = cur.params.get("out_path") or cur.meta.get("pob2_out")
            if cand is not None:
                eff_out = cand
        if isinstance(cur.obj, dict):
            if cur.obj.get("compact"):
                eff_compact = True
            if not ignore_ctx_out and eff_out is None and cur.obj.get("out") is not None:
                eff_out = cur.obj.get("out")
        raw_args = cur.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)):
            if "--compact" in raw_args:
                eff_compact = True
            if not ignore_ctx_out and eff_out is None:
                raw_out = _extract_raw_opt_value(raw_args, "--out")
                if raw_out:
                    eff_out = raw_out
        cur = cur.parent
    return eff_compact, (Path(eff_out) if eff_out is not None else None)


def normalize_json_envelope(payload: dict[str, Any], *, ok: bool | None = None) -> dict[str, Any]:
    """Ensure every CLI JSON payload carries top-level `ok` (bool) and `game: 'poe2'`."""
    existing_ok = payload.get("ok")
    resolved_ok = existing_ok if isinstance(existing_ok, bool) else (True if ok is None else ok)
    out: dict[str, Any] = {
        "ok": resolved_ok,
        "game": str(payload.get("game") or GAME_ID),
    }
    for key, val in payload.items():
        if key not in ("ok", "game"):
            out[key] = val
    return out


def emit_output(
    ctx: click.Context | None,
    payload: dict[str, Any],
    *,
    as_json: bool = False,
    compact: bool = False,
    markdown_text: str | None = None,
    out_path: str | Path | None = None,
    ok: bool | None = None,
    ignore_ctx_out: bool = False,
) -> None:
    """Unified CLI output sink for both JSON envelopes and human/Markdown text."""
    eff_json = bool(as_json or ctx_wants_json(ctx))
    eff_compact, eff_out = resolve_output_flags(
        ctx, compact=compact, out_path=out_path, ignore_ctx_out=ignore_ctx_out
    )
    if eff_json or markdown_text is None:
        normalized = normalize_json_envelope(payload, ok=ok)
        text = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":") if eff_compact else None,
            indent=None if eff_compact else 2,
        )
    else:
        text = markdown_text

    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        return
    echo_utf8(text)


def emit_text(
    ctx: click.Context | None,
    text: str,
    *,
    out_path: str | Path | None = None,
) -> None:
    """Write plain/Markdown text to `--out` if configured, otherwise stdout."""
    _eff_compact, eff_out = resolve_output_flags(ctx, out_path=out_path)
    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        return
    echo_utf8(text)


def _json_opt_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    eff = bool(value or root_obj.get("as_json"))
    if eff and not ctx.resilient_parsing:
        ctx.meta["pob2_as_json"] = True
    return eff


def _compact_opt_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> bool:
    if value and not ctx.resilient_parsing:
        ctx.meta["pob2_compact"] = True
    return value


def _out_opt_callback(ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is not None and not ctx.resilient_parsing:
        ctx.meta["pob2_out"] = value
    return value


def output_options(fn: Any) -> Any:
    """Attach trailing `--json`, `--compact`, and `--out <path>` options to a leaf Click command."""
    fn = click.option(
        "--out",
        "out_path",
        type=click.Path(dir_okay=False),
        default=None,
        expose_value=False,
        callback=_out_opt_callback,
        help="Write output to a UTF-8 (no BOM) file.",
    )(fn)
    fn = click.option(
        "--compact",
        is_flag=True,
        default=False,
        expose_value=False,
        callback=_compact_opt_callback,
        help="Emit compact single-line JSON when `--json` is set.",
    )(fn)
    fn = click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        callback=_json_opt_callback,
        help="Emit machine-readable JSON.",
    )(fn)
    return fn


def _validate_build_slot_slug(raw: str, *, ctx: click.Context | None = None, param: click.Parameter | None = None) -> str:
    s = (raw or "").strip()
    if s in (".", "..") or not _SLOT_SLUG_RE.match(s):
        raise click.BadParameter(
            f"Invalid daemon build slot slug {raw!r}; expected 1-48 chars matching [a-zA-Z0-9._-].",
            ctx=ctx,
            param=param,
        )
    return s.lower()


def _slot_alias_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> None:
    if value is None or ctx.resilient_parsing:
        return
    raw = str(value).strip()
    check_input(raw, GuardrailScope.SLOT, expected_game="poe2", emit_stderr=False)
    if resolve_poe2_slot_name(raw) is not None:
        raise DaemonRequestError(
            f"`--slot {raw}` on `{ctx.command_path}` selects a daemon build slot, not an equipment slot; "
            "use `--target <build-slot>` for build slots."
        )
    ctx.meta[_SLOT_ALIAS_META_KEY] = _validate_build_slot_slug(raw, ctx=ctx, param=param)


def _target_opt_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> str:
    if ctx.resilient_parsing:
        return resolve_target_slot(value)
    alias = ctx.meta.get(_SLOT_ALIAS_META_KEY)
    norm_val = _validate_build_slot_slug(value, ctx=ctx, param=param) if value else None
    if alias is not None:
        if (
            norm_val is not None
            and ctx.get_parameter_source(param.name or "target") == ParameterSource.COMMANDLINE
            and norm_val != alias
        ):
            raise DaemonRequestError(
                f"Both `--target {norm_val}` and `--slot {alias}` were given; pass only `--target`."
            )
        return str(alias)
    return norm_val or default_slot()


def target_option(*, allow_slot_alias: bool = True, help_text: str | None = None) -> Any:
    """Attach `--target` build-slot option (plus `--slot` alias ONLY when `allow_slot_alias=True`)."""
    desc = help_text or "Target daemon build slot slug (defaults to POB2_SLOT or 'main')."
    target_dec = click.option(
        "--target",
        "target",
        type=str,
        default=None,
        callback=_target_opt_callback,
        help=desc,
    )
    if not allow_slot_alias:
        return target_dec
    alias_dec = click.option(
        "--slot",
        SLOT_ALIAS_PARAM,
        type=str,
        default=None,
        expose_value=False,
        is_eager=True,
        metavar="SLUG",
        callback=_slot_alias_callback,
        help="Alias of `--target` for daemon build slot (equipment slot names are rejected).",
    )

    def _decorate(fn: Any) -> Any:
        return target_dec(alias_dec(fn))

    return _decorate


def resolve_target_slot(target: str | None) -> str:
    return (target or "").strip().lower() or default_slot()


def _invoke_with_exit_contract(ctx: click.Context, fn: Any) -> Any:
    """Enforce unified `0 / 1 / 5` exit-code contract across all `pob2` commands and groups."""
    try:
        return fn()
    except (click.exceptions.Exit, click.Abort, KeyboardInterrupt, SystemExit):
        raise
    except click.UsageError as exc:
        exc.exit_code = EXIT_REQUEST_ERROR
        raise
    except click.ClickException:
        raise
    except (CrossGameHallucinationError, LegacyPatchTrapError) as exc:
        echo_utf8(exc.format_human(), err=True)
        if ctx_wants_json(ctx):
            emit_output(ctx, exc.to_dict(), as_json=True, ok=False)
        ctx.exit(EXIT_REQUEST_ERROR)
    except DaemonRequestError as exc:
        to_dict_fn = getattr(exc, "to_dict", None)
        if callable(to_dict_fn):
            echo_utf8(str(exc), err=True)
            if ctx_wants_json(ctx):
                emit_output(ctx, to_dict_fn(), as_json=True, ok=False)
            ctx.exit(getattr(exc, "exit_code", EXIT_REQUEST_ERROR))
        msg = str(exc)
        echo_utf8(msg if msg.startswith("Error:") else f"Error: {msg}", err=True)
        ctx.exit(getattr(exc, "exit_code", EXIT_REQUEST_ERROR))
    except BootstrapMissingError as exc:
        echo_utf8(f"Error: {exc}", err=True)
        ctx.exit(EXIT_REQUEST_ERROR)
    except FileNotFoundError as exc:
        echo_utf8(f"Error: {exc}", err=True)
        ctx.exit(EXIT_REQUEST_ERROR)
    except Exception as exc:
        echo_utf8(f"Error: {sanitize_bridge_error(str(exc))}", err=True)
        ctx.exit(EXIT_DAEMON_ERROR)


class Pob2Command(click.Command):
    """Click Command enforcing `0 / 1 / 5` exit codes and recording raw CLI args."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise
        ctx.meta["pob2_raw_args"] = raw_copy
        return ctx

    def invoke(self, ctx: click.Context) -> Any:
        return _invoke_with_exit_contract(ctx, lambda: super(Pob2Command, self).invoke(ctx))


class Pob2Group(click.Group):
    """Click Group defaulting child commands to `Pob2Command` and enforcing `0 / 1 / 5` exit codes."""

    command_class = Pob2Command

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise
        ctx.meta["pob2_raw_args"] = raw_copy
        return ctx

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            exc.exit_code = EXIT_REQUEST_ERROR
            raise

    def invoke(self, ctx: click.Context) -> Any:
        return _invoke_with_exit_contract(ctx, lambda: super(Pob2Group, self).invoke(ctx))


def pob2_command(name: str | None = None, **attrs: Any) -> Any:
    attrs.setdefault("cls", Pob2Command)
    return click.command(name, **attrs)


def pob2_group(name: str | None = None, **attrs: Any) -> Any:
    attrs.setdefault("cls", Pob2Group)
    return click.group(name, **attrs)


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
    except (CrossGameHallucinationError, LegacyPatchTrapError, DaemonRequestError, BootstrapMissingError):
        raise
    except Exception as exc:
        echo_utf8(f"Daemon Error: {sanitize_bridge_error(str(exc))}", err=True)
        raise SystemExit(EXIT_DAEMON_ERROR) from exc


__all__ = [
    "SLOT_ALIAS_PARAM",
    "Pob2Command",
    "Pob2Group",
    "ctx_wants_json",
    "daemon_call_cli",
    "echo_utf8",
    "emit_output",
    "emit_text",
    "normalize_json_envelope",
    "output_options",
    "pob2_command",
    "pob2_group",
    "resolve_output_flags",
    "resolve_target_slot",
    "target_option",
]
