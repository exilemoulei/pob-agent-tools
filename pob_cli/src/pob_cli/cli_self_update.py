"""`pob self-update`, the daily newer-release notice, and the hidden `_release-check` that feeds it."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .cli_common import _echo_stderr_utf8, _emit_json, _emit_text, _output_options

# Commands whose output an agent harness parses, or that run in the background: no notice there.
_QUIET_COMMANDS = frozenset({"_daemon", "_release-check", "serve", "hook", "self-update"})


def _stderr_is_terminal() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def maybe_show_update_notice(ctx: click.Context) -> None:
    """On an interactive terminal, mention a newer release on stderr after the command finishes.

    Reads a cache only; when the cache is a day old, a detached `pob _release-check` refreshes it
    for next time. Off with --offline, POB_OFFLINE=1 or POB_NO_UPDATE_NOTICE=1.
    """
    if ctx.invoked_subcommand in _QUIET_COMMANDS or ctx.resilient_parsing or not _stderr_is_terminal():
        return
    from .updater.paths import get_runtime_dir
    from .updater.self_update import notice_enabled, notice_text, pending_notice, spawn_background_check

    if not notice_enabled():
        return
    try:
        runtime_dir = get_runtime_dir()
        newer, due = pending_notice(runtime_dir)
    except OSError:
        return
    if due:
        spawn_background_check(runtime_dir)
    if newer:
        ctx.call_on_close(lambda: _echo_stderr_utf8(notice_text(newer)))


@click.command("_release-check", hidden=True)
@click.option("--runtime-dir", type=click.Path(), default=None)
def cmd_release_check(runtime_dir: str | None) -> None:
    """Refresh the cached latest-release lookup behind the update notice."""
    from .updater.paths import get_runtime_dir
    from .updater.self_update import refresh_check

    try:
        refresh_check(get_runtime_dir(Path(runtime_dir) if runtime_dir else None))
    except Exception:
        sys.exit(0)  # a failed background check just waits for the next day


def _stop_daemon_quietly() -> None:
    """The daemon runs from the build being replaced; it restarts from the new one on demand."""
    try:
        from .daemon import call_daemon

        call_daemon("stop", auto_start=False)
    except Exception:
        pass


@click.command("self-update")
@click.option("--check", "check_only", is_flag=True, help="Only report the installed and latest versions.")
@click.option("--version", "target", default=None, help="Install this release (e.g. 0.9.0) instead of the latest.")
@click.option("--proxy", default=None, help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", default=None, help="Optional GitHub mirror prefix URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_self_update(
    check_only: bool,
    target: str | None,
    proxy: str | None,
    mirror: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Update pob itself to the latest release (standalone builds; others get their update command)."""
    from .updater import UpdateError
    from .updater.releases import is_newer, latest_release_tag, parse_version
    from .updater.self_update import app_dir, download_release, install_method, update_command

    method = install_method()
    report: dict[str, Any] = {"installed": __version__, "method": method, "command": update_command(method)}

    def emit(text: str) -> None:
        if as_json:
            _emit_json(report, compact=compact, out_path=out_path)
        else:
            _emit_text(text, out_path=out_path)

    if target is not None and parse_version(target) is None:
        raise click.BadParameter(f"{target!r} is not a release version like 0.9.0", param_hint="--version")

    if check_only:
        from .updater import _make_http_client
        from .updater.constants import _CHECK_TIMEOUT_SPEC, _to_httpx_timeout

        with _make_http_client(timeout=_to_httpx_timeout(_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=False) as c:
            latest = latest_release_tag(c, mirror)
        if latest is None:
            raise click.ClickException("Could not find the latest pob4agents release (offline? try --proxy or --mirror).")
        report["latest"] = latest.lstrip("v")
        report["updateAvailable"] = is_newer(latest, __version__)
        if report["updateAvailable"]:
            emit(f"pob {report['latest']} is available (installed: {__version__}). Update: {report['command']}")
        else:
            emit(f"pob {__version__} is up to date (latest: {report['latest']}).")
        return

    if method != "standalone":
        raise click.ClickException(f"This pob was installed via {method}; update it with: {report['command']}")

    current = app_dir()
    staging = current.with_name(current.name + ".update")
    try:
        result = download_release(staging, tag=target, proxy=proxy, mirror=mirror)
    except UpdateError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise click.ClickException(str(exc)) from exc
    if result.get("upToDate"):
        shutil.rmtree(staging, ignore_errors=True)
        report["latest"] = result["tag"].lstrip("v")
        report["updated"] = False
        emit(f"pob {__version__} is up to date.")
        return

    _stop_daemon_quietly()
    report["latest"] = result["version"]
    if os.name == "nt":
        from .updater.self_update import schedule_windows_swap

        schedule_windows_swap(current, result["dir"])
        report["updated"] = "pending"
        emit(f"pob {result['version']} is downloaded and verified; it replaces {__version__} as soon as this command exits.")
        return

    from .updater.self_update import swap_posix

    try:
        swap_posix(current, result["dir"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    report["updated"] = True
    emit(f"Updated pob {__version__} -> {result['version']}.")


def register_self_update_commands(group: click.Group) -> None:
    group.add_command(cmd_self_update)
    group.add_command(cmd_release_check)
