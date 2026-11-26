"""Click CLI entry point for ``pob2`` (lazy-loaded with optional extension mounting)."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

if len(sys.argv) > 1 and sys.argv[1] == "_lua-host":
    from .updater.win_lua_host import main as _lua_host_main

    raise SystemExit(_lua_host_main(sys.argv[2:]))

import click

from . import __version__
from .compat import ensure_utf8
from .errors import (
    CrossGameHallucinationError,
    DaemonRequestError,
    LegacyPatchTrapError,
)
from .game_profile import (
    CLI_NAME,
    ENV_OFFLINE,
    GAME_ID,
    GAME_NAME,
    LICENSE_SPDX,
    PACKAGE_NAME,
    POE2BRAIN_ATTRIBUTION,
    TOKEN_HEADER,
    resolve_runtime_dir,
    stamp_game,
)
from .locale_cfg import SUPPORTED_LANGS, get_active_lang, set_active_lang

# Core engine/lifecycle subcommands registered lazily (must exist when listed here).
_LAZY_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {}

# Optional extension subcommands discovered dynamically when their module is present
# in the package tree (enables conflict-free parallel feature branch merges).
_OPTIONAL_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "craft": (".cli_craft", "craft_group", "PoE2 crafting odds, MDP solver, and step-by-step plans."),
    "item": (".cli_craft", "item_group", "Inspect PoE2 item modifiers, open slots, and 0.5.5 legality."),
    "ground": (".cli_ground", "cmd_ground", "Fast (<15ms, daemon-free) PoE2 entity & mechanic grounding."),
    "import": (".cli_import", "cmd_import", "Import PoE2 builds (PoB2 code, share URL, CN WeGame link)."),
}

_HIDDEN_LAZY_SUBCOMMANDS: dict[str, tuple[str, str]] = {}


def _echo_utf8(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _module_exists(mod_path: str) -> bool:
    full_name = importlib.util.resolve_name(mod_path, __package__)
    try:
        return importlib.util.find_spec(full_name) is not None
    except (ImportError, ValueError):
        return False


def _ctx_wants_json(ctx: click.Context | None) -> bool:
    if ctx is not None:
        if ctx.params.get("as_json"):
            return True
        root = ctx.find_root()
        raw_args = root.meta.get("pob2_raw_args")
        if isinstance(raw_args, (list, tuple)) and "--json" in raw_args:
            return True
    return "--json" in sys.argv[1:]


class ExtensibleLazyGroup(click.Group):
    """Click Group with lazy core subcommands, optional extension auto-mount, and guardrail exit codes."""

    def __init__(
        self,
        *args: Any,
        lazy_subcommands: dict[str, tuple[str, str, str]] | None = None,
        optional_subcommands: dict[str, tuple[str, str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_subcommands: dict[str, tuple[str, str, str]] = dict(lazy_subcommands or {})
        self._optional_subcommands: dict[str, tuple[str, str, str]] = dict(optional_subcommands or {})

    def register_lazy(self, name: str, module_path: str, attr_name: str, help_text: str) -> None:
        """Dynamically mount a lazy subcommand onto the CLI root group."""
        self._lazy_subcommands[name] = (module_path, attr_name, help_text)

    def _active_optional(self) -> dict[str, tuple[str, str, str]]:
        return {
            name: spec
            for name, spec in self._optional_subcommands.items()
            if name not in self.commands and name not in self._lazy_subcommands and _module_exists(spec[0])
        }

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx))
        names.update(self._lazy_subcommands.keys())
        names.update(self._active_optional().keys())
        return sorted(names)

    def _load_spec(self, cmd_name: str, mod_path: str, attr_name: str) -> click.Command | None:
        if not _module_exists(mod_path):
            return None
        mod = importlib.import_module(mod_path, __package__)
        loaded = getattr(mod, attr_name, None)
        if not isinstance(loaded, click.Command):
            return None
        self.commands[cmd_name] = loaded
        return loaded

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        spec = self._lazy_subcommands.get(cmd_name) or self._optional_subcommands.get(cmd_name)
        if spec is not None:
            return self._load_spec(cmd_name, spec[0], spec[1])
        hidden = _HIDDEN_LAZY_SUBCOMMANDS.get(cmd_name)
        if hidden is not None:
            return self._load_spec(cmd_name, hidden[0], hidden[1])
        return None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        opt = self._active_optional()
        commands: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            if subcommand in self._lazy_subcommands and subcommand not in self.commands:
                commands.append((subcommand, self._lazy_subcommands[subcommand][2]))
                continue
            if subcommand in opt and subcommand not in self.commands:
                commands.append((subcommand, opt[subcommand][2]))
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

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        raw_args_copy = list(args)
        ctx = super().make_context(info_name, args, parent=parent, **extra)
        ctx.meta["pob2_raw_args"] = raw_args_copy
        return ctx

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (CrossGameHallucinationError, LegacyPatchTrapError) as exc:
            _echo_utf8(exc.format_human(), err=True)
            if _ctx_wants_json(ctx):
                _echo_utf8(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
            ctx.exit(1)
        except DaemonRequestError as exc:
            _echo_utf8(f"Error: {exc}", err=True)
            ctx.exit(getattr(exc, "exit_code", 1))


@click.group(
    cls=ExtensibleLazyGroup,
    lazy_subcommands=_LAZY_SUBCOMMANDS,
    optional_subcommands=_OPTIONAL_SUBCOMMANDS,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name=CLI_NAME)
@click.option("--offline", is_flag=True, help=f"Skip network update checks (sets {ENV_OFFLINE}=1).")
@click.option("--compact", is_flag=True, default=False, help="Emit compact single-line JSON for --json.")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write output to a UTF-8 (no BOM) file.",
)
@click.pass_context
def main(ctx: click.Context, offline: bool, compact: bool, out_path: str | None) -> None:
    """Headless Path of Building (PoE2) CLI for build analysis, what-if testing, and crafting."""
    if offline:
        os.environ[ENV_OFFLINE] = "1"
    ctx.ensure_object(dict)
    ctx.obj["compact"] = compact
    ctx.obj["out"] = out_path


def _emit_payload(payload: dict[str, Any], *, compact: bool = False, out_path: str | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":") if compact else None, indent=None if compact else 2)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8", newline="\n")
        return
    _echo_utf8(text)


@main.command("about")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_about(ctx: click.Context, as_json: bool) -> None:
    """Show version, license, runtime isolation profile, and GPLv3 attribution notices."""
    lang, lang_src = get_active_lang()
    info = stamp_game(
        {
            "package": PACKAGE_NAME,
            "cli": CLI_NAME,
            "version": __version__,
            "gameName": GAME_NAME,
            "license": LICENSE_SPDX,
            "tokenHeader": TOKEN_HEADER,
            "runtimeDir": str(resolve_runtime_dir()),
            "lang": lang,
            "langSource": lang_src,
            "attribution": [
                POE2BRAIN_ATTRIBUTION,
                "PathOfBuildingCommunity/PathOfBuilding-PoE2 (MIT)",
                "ackness/pobr (MIT)",
                "Hsiung-Shao/PobTools-zh",
                "exilemoulei/pob4agents contributors",
            ],
        }
    )
    if as_json:
        obj = ctx.find_root().obj or {}
        _emit_payload(info, compact=bool(obj.get("compact")), out_path=obj.get("out"))
        return
    lines = [
        f"{PACKAGE_NAME} ({CLI_NAME}) v{__version__} — {GAME_NAME} [{GAME_ID}]",
        f"License: {LICENSE_SPDX}",
        f"Runtime: {info['runtimeDir']} (lang={lang}, source={lang_src}, header={TOKEN_HEADER})",
        f"Attribution: {POE2BRAIN_ATTRIBUTION}",
        "Upstream & references: PathOfBuilding-PoE2 (MIT), ackness/pobr (MIT), PobTools-zh, pob4agents contributors",
    ]
    _echo_utf8("\n".join(lines))


@main.command("lang")
@click.argument("value", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_lang(ctx: click.Context, value: str | None, as_json: bool) -> None:
    """Get or set display language (`zh-CN`, `zh-TW`, `en`)."""
    if value is None:
        lang, source = get_active_lang()
        payload = stamp_game({"lang": lang, "source": source, "supported": list(SUPPORTED_LANGS)})
    else:
        try:
            saved = set_active_lang(value)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        payload = stamp_game({"lang": saved["lang"], "source": "config", "configPath": saved["configPath"]})

    if as_json:
        obj = ctx.find_root().obj or {}
        _emit_payload(payload, compact=bool(obj.get("compact")), out_path=obj.get("out"))
        return
    _echo_utf8(f"lang={payload['lang']} (source={payload['source']})")


def register_lazy_subcommand(name: str, module_path: str, attr_name: str, help_text: str) -> None:
    """Public hook for optional extension modules to mount a lazy subcommand on ``main``."""
    main.register_lazy(name, module_path, attr_name, help_text)


def register_command(command: click.Command, name: str | None = None) -> None:
    """Public hook to attach an already-constructed Click command/group to ``main``."""
    main.add_command(command, name=name)


__all__ = [
    "_HIDDEN_LAZY_SUBCOMMANDS",
    "_LAZY_SUBCOMMANDS",
    "_OPTIONAL_SUBCOMMANDS",
    "ExtensibleLazyGroup",
    "cmd_about",
    "cmd_lang",
    "main",
    "register_command",
    "register_lazy_subcommand",
]

if __name__ == "__main__":
    main()
