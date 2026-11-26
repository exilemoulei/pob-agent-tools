"""Shared Click infrastructure, output helpers, guardrail group, and daemon RPC bridge for `pob` CLI."""

from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING, Any

import click

from .compat import call_compat as _call_with_supported_kwargs  # noqa: F401  (re-exported for CLI cmds)
from .compat import ensure_utf8

if TYPE_CHECKING:
    from pathlib import Path

_SLOT_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_GEM_SPEC_RE = re.compile(r"^(.*?):(\d+)(?::(\d+))?$")
DAEMON_FAILURE_EXIT_CODE = 5  # same code as upstream/API failures in the exit-code contract


class _SlotSlugType(click.ParamType):
    name = "slot"

    def convert(self, value: Any, param: Any, ctx: Any) -> str:
        s = str(value or "").strip()
        if s in (".", "..") or not _SLOT_SLUG_RE.match(s):
            self.fail(
                f"Invalid slot slug {value!r}; expected 1-48 chars matching [a-zA-Z0-9._-] (not '.' or '..').",
                param,
                ctx,
            )
        return s.lower()


_SLOT_SLUG_TYPE = _SlotSlugType()


def _default_target() -> str:
    val = (os.environ.get("POB_SLOT") or "main").strip()
    return (val or "main").lower()


class LazyGroup(click.Group):
    """Click Group that defers importing heavy subcommand groups until invoked."""

    def __init__(
        self,
        *args: Any,
        lazy_subcommands: dict[str, tuple[str, str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_subcommands: dict[str, tuple[str, str, str]] = dict(lazy_subcommands or {})

    def list_commands(self, ctx: click.Context) -> list[str]:
        base = set(super().list_commands(ctx))
        base.update(self._lazy_subcommands.keys())
        return sorted(base)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        spec = self._lazy_subcommands.get(cmd_name)
        if spec is None:
            return None
        mod_path, attr_name, _ = spec
        import importlib

        mod = importlib.import_module(mod_path, __package__)
        loaded = getattr(mod, attr_name)
        self.commands[cmd_name] = loaded
        return loaded

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        commands: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            if subcommand in self._lazy_subcommands and subcommand not in self.commands:
                commands.append((subcommand, self._lazy_subcommands[subcommand][2]))
                continue
            cmd = super().get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            commands.append((subcommand, cmd.get_short_help_str(limit=formatter.width)))

        if commands:
            limit = formatter.width - 6 - max(len(c[0]) for c in commands)
            rows = []
            for subcommand, help_text in commands:
                if len(help_text) > limit > 3:
                    help_text = help_text[: limit - 3] + "..."
                rows.append((subcommand, help_text))
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _ctx_wants_json(ctx: click.Context | None) -> bool:
    """Return True if the active CLI invocation requested `--json`."""
    if ctx is not None:
        if ctx.params.get("as_json"):
            return True
        root = ctx.find_root()
        raw_args = root.meta.get("pob_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in raw_args:
            return True
    return "--json" in sys.argv[1:]


def _augment_click_usage_error(exc: click.UsageError, fallback_ctx: click.Context | None = None) -> None:
    """Append actionable `Did you mean` / `Suggested command` hints onto Click UsageError/NoSuchOption."""
    import difflib

    msg = str(exc.message or "")
    if "Did you mean" in msg or "Suggested command" in msg:
        return
    ctx = exc.ctx or fallback_ctx
    cmd = ctx.command if ctx is not None else None
    cmd_path = ctx.command_path if ctx is not None else "pob"

    if isinstance(exc, click.NoSuchOption):
        bad_opt = str(exc.option_name or "")
        all_opts: list[str] = []
        if cmd is not None:
            for p in getattr(cmd, "params", ()):
                all_opts.extend(getattr(p, "opts", ()))
                all_opts.extend(getattr(p, "secondary_opts", ()))
        all_opts = sorted({o for o in all_opts if o.startswith("-")})
        matches = difflib.get_close_matches(bad_opt, all_opts, n=3, cutoff=0.45)
        # Click appends its own "Did you mean" from `possibilities`; fold those in so only one line prints.
        for p in exc.possibilities or ():
            if p not in matches:
                matches.append(p)
        exc.possibilities = None
        if matches:
            exc.message = f"{msg}\nDid you mean: {', '.join(matches)}? (Run `{cmd_path} --help` for valid options.)"
        else:
            exc.message = f"{msg}\nSuggested command: `{cmd_path} --help`"
        return

    if "No such command" in msg and isinstance(cmd, click.Group) and ctx is not None:
        m = re.search(r"No such command ['\"]([^'\"]+)['\"]", msg)
        if m:
            bad_cmd = m.group(1)
            known_cmds = cmd.list_commands(ctx)
            matches = difflib.get_close_matches(bad_cmd, known_cmds, n=3, cutoff=0.45)
            if matches:
                exc.message = (
                    f"{msg}\nDid you mean: {', '.join(matches)}? "
                    f"Suggested command: `{cmd_path} {matches[0]} --help`"
                )
                return
    if cmd_path:
        exc.message = f"{msg}\nSuggested command: `{cmd_path} --help`"


class GuardrailGroup(LazyGroup):
    """LazyGroup catching CrossGameHallucinationError / LegacyPatchTrapError (-> exit 1) & enriching Click errors."""

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            _augment_click_usage_error(exc, fallback_ctx=ctx)
            raise

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_args_copy = list(args)
        try:
            ctx = super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            _augment_click_usage_error(exc, fallback_ctx=parent)
            raise
        ctx.meta["pob_raw_args"] = raw_args_copy
        return ctx

    def invoke(self, ctx: click.Context) -> Any:
        from .errors import CrossGameHallucinationError, LegacyPatchTrapError
        from .guardrails import reset_cli_warnings, warning_context

        reset_cli_warnings()
        with warning_context():
            try:
                return super().invoke(ctx)
            except (CrossGameHallucinationError, LegacyPatchTrapError) as exc:
                as_json = _ctx_wants_json(ctx)
                _echo_stderr_utf8(exc.format_human())
                if as_json:
                    raw_args = ctx.find_root().meta.get("pob_raw_args") or ()
                    eff_compact, eff_out = _resolve_output_flags(compact="--compact" in raw_args)
                    from .formatters import emit_json

                    emit_json(exc.to_dict(), compact=eff_compact, out=eff_out, sanitize=False)
                ctx.exit(1)
            except click.UsageError as exc:
                _augment_click_usage_error(exc, fallback_ctx=ctx)
                raise


_SLOT_ALIAS_ATTR = "_pob_build_slot_alias"
SLOT_ALIAS_PARAM = "pob_build_slot_alias"


def _looks_like_equipment_slot(value: str) -> bool:
    """True when `value` names an equipment slot (`gloves`, `ring1`, `Weapon 1`, `#1`, `頭盔`) rather than a build slot."""
    from .game_data import fold_han
    from .guardrails import EQUIPMENT_SLOT_ALIASES, normalize_equipment_slot

    raw = str(value or "").strip()
    if not raw:
        return False
    if raw.startswith("#"):
        return True
    canonical = {v.lower() for v in EQUIPMENT_SLOT_ALIASES.values()}
    for cand in dict.fromkeys((raw, fold_han(raw))):
        hit = normalize_equipment_slot(cand)
        if (hit is not None and hit != cand) or cand.lower() in canonical:
            return True
    return False


def _slot_alias_callback(ctx: click.Context, param: click.Parameter, value: Any) -> None:
    """Validate the `--slot` build-slot alias; refuse an equipment slot name instead of silently using it."""
    if value is None:
        return
    if _looks_like_equipment_slot(str(value)):
        raise click.ClickException(
            f"`--slot {value}` here selects a build slot, not an equipment slot; use --target for build slots, "
            "or the command's equipment option (e.g. `pob items --slot`, `pob item try --slot`)."
        )
    setattr(ctx, _SLOT_ALIAS_ATTR, _SLOT_SLUG_TYPE.convert(value, param, ctx))


def _target_callback(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """Fold the `--slot` alias into `--target` (the alias is eager, so it is already parsed)."""
    from click.core import ParameterSource

    alias = getattr(ctx, _SLOT_ALIAS_ATTR, None)
    if alias is None:
        return value
    name = param.name or "target"
    if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE and value != alias:
        raise click.ClickException(f"Both --target {value} and --slot {alias} were given; pass only --target.")
    # The alias was typed on the command line: group -> subcommand target inheritance must see that.
    ctx.set_parameter_source(name, ParameterSource.COMMANDLINE)
    return alias


def _target_default_or_alias() -> str:
    """`--target`'s default on a command with the alias: the alias wins, so a bad `POB_SLOT` cannot fail `--slot ref`.

    Click converts a default before the option's callback runs, which is too late to swap it there.
    """
    ctx = click.get_current_context(silent=True)
    alias = getattr(ctx, _SLOT_ALIAS_ATTR, None) if ctx is not None else None
    return alias or _default_target()


def _target_option(*, allow_slot_alias: bool = True) -> Any:
    """Attach named multi-slot `--target` (plus a `--slot` build-slot alias on commands without an equipment `--slot`).

    The alias refuses equipment slot names (`--slot gloves` exits 1) so it cannot be mistaken for the
    equipment `--slot` of `items` / `item` / `trade` / `bench` / `data`.
    """
    target_opt = click.option(
        "--target",
        "target",
        type=_SLOT_SLUG_TYPE,
        default=_target_default_or_alias if allow_slot_alias else _default_target,
        callback=_target_callback if allow_slot_alias else None,
        help="Target build slot slug (defaults to POB_SLOT or 'main').",
    )
    if not allow_slot_alias:
        return target_opt
    alias_opt = click.option(
        "--slot",
        SLOT_ALIAS_PARAM,
        default=None,
        expose_value=False,
        is_eager=True,
        metavar="SLUG",
        callback=_slot_alias_callback,
        help="Deprecated alias of --target (a build slot; equipment slot names are refused).",
    )

    def _decorate(fn: Any) -> Any:
        return target_opt(alias_opt(fn))

    return _decorate


def _output_options(fn: Any) -> Any:
    """Attach `--compact` and `--out` output options to a Click command."""
    fn = click.option(
        "--out",
        "out_path",
        type=click.Path(dir_okay=False),
        default=None,
        help="Write output directly to UTF-8 (no BOM) file.",
    )(fn)
    fn = click.option(
        "--compact",
        is_flag=True,
        default=False,
        help="Emit compact single-line JSON (when --json is used).",
    )(fn)
    return fn


def _resolve_output_flags(compact: bool = False, out_path: str | Path | None = None) -> tuple[bool, Path | None]:
    ctx = click.get_current_context(silent=True)
    root_obj = (ctx.find_root().obj if ctx is not None else None) or {}
    eff_compact = bool(compact or root_obj.get("compact"))
    eff_out_raw = out_path if out_path is not None else root_obj.get("out")
    if eff_out_raw is None:
        return eff_compact, None
    from pathlib import Path

    return eff_compact, Path(eff_out_raw)


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    ensure_utf8(sys.stdout)
    click.echo(text)


def _echo_stderr_utf8(text: str) -> None:
    """Write UTF-8 safely to stderr on Windows consoles."""
    ensure_utf8(sys.stderr)
    click.echo(text, err=True)


def _emit_json(
    data: Any,
    *,
    compact: bool = False,
    out_path: str | Path | None = None,
    sanitize: bool = True,
) -> None:
    from .formatters import emit_json

    eff_compact, eff_out = _resolve_output_flags(compact=compact, out_path=out_path)
    text = emit_json(data, compact=eff_compact, out=eff_out, sanitize=sanitize)
    if eff_out is not None:
        _echo_utf8(f"Saved JSON to `{eff_out}` ({len(text.encode('utf-8')):,} bytes).")


def _emit_text(text: str, *, out_path: str | Path | None = None) -> None:
    _, eff_out = _resolve_output_flags(out_path=out_path)
    if eff_out is not None:
        eff_out.parent.mkdir(parents=True, exist_ok=True)
        eff_out.write_text(text + "\n", encoding="utf-8", newline="\n")
        _echo_utf8(f"Saved output to `{eff_out}` ({len(text.encode('utf-8')):,} bytes).")
        return
    _echo_utf8(text)


def _read_json_input(file_path: str | None, inline_json: str | None, *, label: str) -> Any:
    import json
    from pathlib import Path

    if file_path:
        if file_path == "-":
            raw = sys.stdin.read().lstrip("\ufeff")
        else:
            p = Path(file_path)
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {file_path}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    elif inline_json:
        raw = inline_json.lstrip("\ufeff")
        if raw.startswith("@"):
            p = Path(raw[1:].strip())
            if not p.is_file():
                raise click.UsageError(f"{label} file not found: {p}")
            raw = p.read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    else:
        raise click.UsageError(f"Provide {label} via --file <path|-> or inline JSON.")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"Invalid JSON for {label}: {exc}") from exc


def _parse_gem_spec(spec: str) -> dict[str, Any]:
    """Parse 'Gem Name[:level[:quality]]' into a gem dict."""
    s = spec.strip()
    m = _GEM_SPEC_RE.match(s)
    if m and m.group(1).strip():
        out: dict[str, Any] = {"name": m.group(1).strip(), "level": int(m.group(2))}
        if m.group(3) is not None:
            out["quality"] = int(m.group(3))
        return out
    return {"name": s}


def _daemon_call(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Call the daemon; report known failures as clean messages instead of tracebacks."""
    import http.client

    from .daemon import BootstrapMissingError, DaemonRequestError, call_daemon
    from .errors import CrossGameHallucinationError, LegacyPatchTrapError
    from .guardrails import redact_secrets
    from .updater import format_stderr_update_alert

    try:
        return call_daemon(action, params, auto_start=auto_start, timeout=timeout)
    except (CrossGameHallucinationError, LegacyPatchTrapError):
        raise
    except BootstrapMissingError as exc:
        alert = redact_secrets(format_stderr_update_alert(exc.update_status) or str(exc))
        if "pob bootstrap" not in alert:
            alert = (
                f"{alert}\nError: no PoB engine is installed; run `pob bootstrap` "
                "(or `pob bootstrap --from-dir <path>`) and retry."
            )
        _echo_stderr_utf8(alert)
        raise click.exceptions.Exit(1) from exc
    except DaemonRequestError as exc:
        msg = redact_secrets(str(exc).strip()) or f"Daemon rejected '{action}'."
        raise click.ClickException(msg) from exc
    except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
        detail = redact_secrets(str(exc).strip()) or type(exc).__name__
        err = click.ClickException(
            f"PoB daemon failure during '{action}': {detail}\n"
            "Run `pob stop` and retry; check daemon logs in the runtime directory if this persists."
        )
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc


def invoke_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Dispatch daemon call respecting test monkeypatches on `pob_cli.cli._daemon_call`."""
    cli_mod = sys.modules.get("pob_cli.cli")
    fn = getattr(cli_mod, "_daemon_call", _daemon_call) if cli_mod is not None else _daemon_call
    return fn(action, params, **kwargs)


def _merge_keep_tokens(*sources: Any) -> list[str]:
    merged: list[str] = []
    for src in sources:
        if src is None:
            continue
        items = [src] if isinstance(src, str) else (list(src) if isinstance(src, (list, tuple)) else [str(src)])
        for item in items:
            token = str(item).strip()
            if token and token not in merged:
                merged.append(token)
    return merged


def _prepend_warning(body: str, res: dict[str, Any]) -> str:
    from .formatters import render_update_warning

    parts: list[str] = []
    banner = render_update_warning(res.get("updateWarning"))
    if banner:
        parts.append(banner)
    drift = str(res.get("slotDriftWarning") or "").strip()
    if drift:
        parts.append(f"[WARN] {drift}\n\n")
    # Top level on a load or an undo/redo, per slot on `pob status`.
    slots = res.get("slots") if isinstance(res.get("slots"), dict) else {}
    for unsaved in dict.fromkeys(
        str(d["persistWarning"]) for d in (res, *slots.values()) if isinstance(d, dict) and d.get("persistWarning")
    ):
        parts.append(f"[WARN] {unsaved}\n\n")
    parts.append(body)
    return "".join(parts)


def _prepend_active_guardrail_warnings(body: str) -> str:
    from .guardrails import get_active_warnings

    warns = get_active_warnings()
    if not warns:
        return body
    lines = [w.format_markdown() for w in warns]
    return "\n".join(lines) + "\n\n" + body


def _ref_stage_options(fn: Any) -> Any:
    """Attach `--ref-stage`, `--ref-spec`, `--ref-item-set`, `--ref-skill-set` options for compare commands."""
    fn = click.option("--ref-skill-set", "ref_skill_set", default=None, help="SkillSet ID/index/title for reference build.")(fn)
    fn = click.option("--ref-item-set", "ref_item_set", default=None, help="ItemSet ID/index/title for reference build.")(fn)
    fn = click.option("--ref-spec", "ref_spec", default=None, help="Tree Spec index/title for reference build.")(fn)
    fn = click.option("--ref-stage", "ref_stage", default=None, help="Unified stage index or title substring for reference build.")(fn)
    return fn


def _prepare_load_source_with_stage(source: str) -> tuple[str | None, str | None, dict[str, str | None]]:
    """Resolve a user build source string plus any `#stage=` / `#spec=` / `#item-set=` / `#skill-set=` fragment."""
    from pathlib import Path

    from .guardrails import validate_build_xml, validate_url
    from .importers_stages import split_source_stage_fragment

    raw_full = source.lstrip("\ufeff").strip()
    raw, stage_hints = split_source_stage_fragment(raw_full)
    candidate_path = raw[1:].lstrip("\ufeff").strip() if raw.startswith("@") else raw
    is_local_file = False
    if len(candidate_path) <= 260 and not candidate_path.startswith(("http://", "https://")):
        try:
            p = Path(candidate_path)
            is_local_file = p.exists() and p.is_file()
        except OSError:
            is_local_file = False

    if is_local_file:
        text = Path(candidate_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff").strip()
        if text.lstrip().startswith("<"):
            validate_build_xml(text, expected_game="poe1", source_hint=candidate_path)
            from .importers import xml_to_share_code

            return None, xml_to_share_code(text), stage_hints
        validate_url(text, expected_game="poe1", cli_subcommand="load")
        from .bridge_protocol import _decode_share_code_xml

        decoded_file_xml = _decode_share_code_xml(text)
        if decoded_file_xml:
            validate_build_xml(decoded_file_xml, expected_game="poe1", source_hint=candidate_path)
        return text, None, stage_hints

    if raw.startswith("@"):
        raise click.ClickException(f"Build file not found: {candidate_path or raw}")

    if raw.lstrip().startswith("<"):
        validate_build_xml(raw, expected_game="poe1")
        from .importers import xml_to_share_code

        return None, xml_to_share_code(raw), stage_hints

    validate_url(raw, expected_game="poe1", cli_subcommand="load")
    if not raw.startswith(("http://", "https://")):
        from .bridge_protocol import _decode_share_code_xml

        decoded_xml = _decode_share_code_xml(raw)
        if decoded_xml:
            validate_build_xml(decoded_xml, expected_game="poe1")

    return raw, None, stage_hints


def _prepare_load_source(source: str) -> tuple[str | None, str | None]:
    """Resolve a user build source string (`@path`, `.xml` file, share-code `.txt` file, URL, or Base64 code)."""
    resolved_src, resolved_xml, _ = _prepare_load_source_with_stage(source)
    return resolved_src, resolved_xml
