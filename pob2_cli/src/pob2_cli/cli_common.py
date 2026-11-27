# SPDX-License-Identifier: GPL-3.0-only
"""Shared CLI decorators, unified JSON envelope, daemon RPC wrapper, and 0/1/5 exit-code boundary (L4)."""

from __future__ import annotations

import contextvars
import json
import re
import sys
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from .compat import ensure_utf8
from .errors import (
    EXIT_DAEMON_ERROR,
    EXIT_REQUEST_ERROR,
    BootstrapMissingError,
    CrossGameHallucinationError,
    DaemonRequestError,
    LegacyPatchTrapError,
)
from .game_profile import GAME_ID, default_slot

_SLOT_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_BRACKET_CODE_RE = re.compile(r"^(?:Error:\s*)?\[([A-Za-z0-9_.-]+)\]")
_SLOT_ALIAS_META_KEY = "pob2_build_slot_alias"
SLOT_ALIAS_PARAM = "pob2_build_slot_alias"
_RAW_ARGV_VAR: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("pob2_raw_argv", default=())


def _sanitize_err(message: str) -> str:
    from .bridge_client import sanitize_bridge_error

    return sanitize_bridge_error(message)


def _single_line(text: str) -> str:
    collapsed = " ".join(part.strip() for part in str(text or "").splitlines() if part.strip())
    if collapsed.startswith("Error:"):
        collapsed = collapsed[len("Error:") :].strip()
    return collapsed or "Request failed."


def _option_tokens_before_ddash(raw_args: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for tok in raw_args:
        if tok == "--":
            break
        out.append(tok)
    return tuple(out)


def echo_utf8(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def ctx_wants_json(ctx: click.Context | None = None) -> bool:
    """Return True if `--json` was passed on root group, leaf subcommand, or raw CLI args."""
    cur = ctx
    while cur is not None:
        if cur.params.get("as_json") or cur.meta.get("pob2_as_json"):
            return True
        if isinstance(cur.obj, dict) and cur.obj.get("as_json"):
            return True
        raw_args = cur.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in _option_tokens_before_ddash(raw_args):
            return True
        cur = cur.parent
    active_raw = _RAW_ARGV_VAR.get()
    return bool(active_raw and "--json" in _option_tokens_before_ddash(active_raw))


def _extract_raw_opt_value(raw_args: list[str] | tuple[str, ...], flag: str) -> str | None:
    opts = _option_tokens_before_ddash(raw_args)
    for idx, tok in enumerate(opts):
        if tok == flag and idx + 1 < len(opts):
            return opts[idx + 1]
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
            if "--compact" in _option_tokens_before_ddash(raw_args):
                eff_compact = True
            if not ignore_ctx_out and eff_out is None:
                raw_out = _extract_raw_opt_value(raw_args, "--out")
                if raw_out:
                    eff_out = raw_out
        cur = cur.parent
    active_raw = _RAW_ARGV_VAR.get()
    if active_raw:
        if "--compact" in _option_tokens_before_ddash(active_raw):
            eff_compact = True
        if not ignore_ctx_out and eff_out is None:
            raw_out = _extract_raw_opt_value(active_raw, "--out")
            if raw_out:
                eff_out = raw_out
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
        echo_utf8(f"Wrote {eff_out}", err=True)
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
        echo_utf8(f"Wrote {eff_out}", err=True)
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
        help="Write output to PATH instead of stdout.",
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
    from .guardrails import GuardrailScope, check_input, resolve_poe2_slot_name

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


def _classify_daemon_request_error(msg: str) -> tuple[str, str | None]:
    m_code = _BRACKET_CODE_RE.match(msg)
    code = m_code.group(1) if m_code else None
    low = msg.lower()
    if code == "POE2-INVALID-SLOT":
        return "invalid_slot", code
    if "has no build loaded" in low or "is empty in build slot" in low:
        return "empty_slot", code or "POE2-EMPTY-SLOT"
    if "file not found" in low or "no such file" in low:
        return "file_not_found", code or "POE2-FILE-NOT-FOUND"
    if "against itself" in low or ("both `--target" in low and "`--slot" in low):
        return "same_slot", code or "POE2-SAME-SLOT"
    return "request_error", code


def _emit_error_and_resolve_code(ctx: click.Context | None, exc: BaseException) -> int:
    wants_json = ctx_wants_json(ctx)
    if isinstance(exc, (CrossGameHallucinationError, LegacyPatchTrapError)):
        echo_utf8(exc.format_human(), err=True)
        if wants_json:
            payload = dict(exc.to_dict())
            payload.setdefault(
                "code",
                "POE2-CROSS-GAME-BLOCK" if isinstance(exc, CrossGameHallucinationError) else "POE2-PATCH-BLOCK",
            )
            payload["error"] = _single_line(str(payload.get("error") or exc))
            emit_output(ctx, payload, as_json=True, ok=False, ignore_ctx_out=True)
        return EXIT_REQUEST_ERROR

    if isinstance(exc, DaemonRequestError):
        exit_code = int(getattr(exc, "exit_code", EXIT_REQUEST_ERROR))
        to_dict_fn = getattr(exc, "to_dict", None)
        single = _single_line(str(exc))
        echo_utf8(f"Error: {single}", err=True)
        if wants_json:
            if callable(to_dict_fn):
                payload = dict(to_dict_fn())
                m_code = _BRACKET_CODE_RE.match(single)
                if m_code:
                    payload.setdefault("code", m_code.group(1))
                payload["error"] = _single_line(str(payload.get("error") or single))
            else:
                kind, code = _classify_daemon_request_error(single)
                payload = {"errorKind": kind, **({"code": code} if code else {}), "error": single}
            emit_output(ctx, payload, as_json=True, ok=False, ignore_ctx_out=True)
        return exit_code

    if isinstance(exc, BootstrapMissingError):
        single = _single_line(str(exc))
        echo_utf8(f"Error: {single}", err=True)
        if wants_json:
            emit_output(
                ctx,
                {"errorKind": "bootstrap_missing", "code": "POE2-BOOTSTRAP-MISSING", "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
            )
        return EXIT_REQUEST_ERROR

    if isinstance(exc, FileNotFoundError):
        single = _single_line(str(exc))
        echo_utf8(f"Error: {single}", err=True)
        if wants_json:
            emit_output(
                ctx,
                {"errorKind": "file_not_found", "code": "POE2-FILE-NOT-FOUND", "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
            )
        return EXIT_REQUEST_ERROR

    if isinstance(exc, click.ClickException):
        exc.exit_code = EXIT_REQUEST_ERROR
        if wants_json:
            single = _single_line(exc.format_message())
            echo_utf8(f"Error: {single}", err=True)
            kind = "usage_error" if isinstance(exc, click.UsageError) else "cli_error"
            code = "POE2-USAGE-ERROR" if isinstance(exc, click.UsageError) else "POE2-CLI-ERROR"
            emit_output(
                ctx,
                {"errorKind": kind, "code": code, "error": single},
                as_json=True,
                ok=False,
                ignore_ctx_out=True,
            )
        else:
            exc.show()
        return EXIT_REQUEST_ERROR

    single = _single_line(_sanitize_err(str(exc)))
    echo_utf8(f"Error: {single}", err=True)
    if wants_json:
        emit_output(
            ctx,
            {"errorKind": "daemon_error", "code": "POE2-DAEMON-ERROR", "error": single},
            as_json=True,
            ok=False,
            ignore_ctx_out=True,
        )
    return EXIT_DAEMON_ERROR


def _invoke_with_exit_contract(ctx: click.Context, fn: Any) -> Any:
    """Enforce unified `0 / 1 / 5` exit-code and `--json` error envelope contract."""
    try:
        return fn()
    except (click.exceptions.Exit, click.Abort, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        ctx.exit(_emit_error_and_resolve_code(ctx, exc))


class _Pob2CliMixin:
    """Shared Click Command/Group mixin tracking raw CLI args and unifying error envelopes."""

    def main(  # type: ignore[misc]
        self,
        args: Any = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        raw_tuple = tuple(str(a) for a in (args if args is not None else sys.argv[1:]))
        tok = _RAW_ARGV_VAR.set(raw_tuple)
        try:
            return super().main(  # type: ignore[misc]
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                **extra,
            )
        except (click.exceptions.Exit, click.Abort, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if not standalone_mode:
                raise
            code = _emit_error_and_resolve_code(click.get_current_context(silent=True), exc)
            raise SystemExit(code) from exc
        finally:
            _RAW_ARGV_VAR.reset(tok)


class Pob2Command(_Pob2CliMixin, click.Command):
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


class Pob2Group(_Pob2CliMixin, click.Group):
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
    """Invoke local PoE2 daemon (letting request errors exit 1 and daemon/transport faults exit 5)."""
    from .daemon.client import call_daemon

    return call_daemon(action, params or {}, auto_start=auto_start, timeout=timeout)


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
