"""`pob self-update`, the daily newer-release notice, and the hidden `_release-check` that feeds it."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import click

from . import __version__
from .cli_common import DAEMON_FAILURE_EXIT_CODE, _echo_stderr_utf8, _emit_json, _emit_text, _output_options

if TYPE_CHECKING:
    from pathlib import Path

# Commands whose output an agent harness parses, or that run in the background: no notice there.
_QUIET_COMMANDS = frozenset({"_daemon", "_lua-host", "_release-check", "serve", "hook", "self-update"})
# Background commands, which nobody reads: a failed Windows swap is reported by the next other one.
_BACKGROUND_COMMANDS = frozenset({"_daemon", "_lua-host", "_release-check"})


def _stderr_is_terminal() -> bool:
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _report_windows_swap(ctx: click.Context) -> None:
    """Say so when the swap a Windows `pob self-update` left to a helper did not finish."""
    if not getattr(sys, "frozen", False) or ctx.invoked_subcommand in _BACKGROUND_COMMANDS:
        return
    from .updater.self_update import app_dir, take_swap_result

    result = take_swap_result(app_dir())
    if result is None or result.get("ok") is True:
        return
    target = str(result.get("version") or "the new release")
    _echo_stderr_utf8(
        f"pob self-update to {target} did not finish: {result.get('error') or 'unknown error'}. "
        f"pob {__version__} is still installed; close other pob windows and run `pob self-update` again."
    )


def _show_update_notice(ctx: click.Context) -> None:
    if ctx.invoked_subcommand in _QUIET_COMMANDS or ctx.resilient_parsing or not _stderr_is_terminal():
        return
    from .updater.paths import get_runtime_dir
    from .updater.self_update import notice_enabled, notice_text, pending_notice, spawn_background_check

    if not notice_enabled():
        return
    runtime_dir = get_runtime_dir()
    newer, due = pending_notice(runtime_dir)
    if due:
        spawn_background_check(runtime_dir)
    if newer:
        text = notice_text(newer)
        ctx.call_on_close(lambda: _echo_stderr_utf8(text))


def maybe_show_update_notice(ctx: click.Context) -> None:
    """On an interactive terminal, mention a newer release on stderr after the command finishes.

    Reads a cache only; when the cache is a day old, a detached `pob _release-check` refreshes it
    for next time. Off with --offline, POB_OFFLINE=1 or POB_NO_UPDATE_NOTICE=1. Also reports a
    Windows self-update whose swap failed. Neither may ever break the command the user ran.
    """
    for step in (_report_windows_swap, _show_update_notice):
        try:
            step(ctx)
        except Exception:
            pass


@click.command("_release-check", hidden=True)
@click.option("--runtime-dir", type=click.Path(), default=None)
def cmd_release_check(runtime_dir: str | None) -> None:
    """Refresh the cached latest-release lookup behind the update notice."""
    from pathlib import Path

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


def _fault(message: str) -> click.ClickException:
    """A network, disk or swap failure: exit 5 with a clean `Error: ...` line (the exit-code contract)."""
    err = click.ClickException(message)
    err.exit_code = DAEMON_FAILURE_EXIT_CODE
    return err


def _install(current: Path, target: str | None, proxy: str | None, mirror: str | None) -> dict[str, Any]:
    """Download, verify and swap in a release under the update lock; return what happened."""
    import shutil

    from .updater import UpdateError
    from .updater import self_update as su

    with su.update_lock(current):
        staging = su.new_staging(current)
        keep_staging = False
        try:
            result = su.download_release(staging, tag=target, proxy=proxy, mirror=mirror)
            if result.get("upToDate"):
                return result
            _stop_daemon_quietly()
            if su.is_windows():
                su.schedule_windows_swap(current, result["dir"], result["version"])
                keep_staging = True  # the helper moves staging/new into place, then removes staging
                return {**result, "pending": True}
            # Keep this the last step before output: PyInstaller re-reads the running binary by path
            # for a first-time import, which after the swap would come from the new build.
            try:
                su.swap_posix(current, result["dir"])
            except OSError as exc:
                raise UpdateError(f"Could not replace {current}: {exc}") from exc
            return result
        finally:
            if not keep_staging:
                shutil.rmtree(staging, ignore_errors=True)


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
    from .updater import self_update as su
    from .updater.releases import is_newer, latest_release_tag, parse_version

    method = su.install_method()
    report: dict[str, Any] = {"installed": __version__, "method": method, "command": su.update_command(method)}

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

        try:
            timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC)
            with _make_http_client(timeout=timeout, proxy=proxy, follow_redirects=False) as c:
                latest = latest_release_tag(c, mirror)
        except (OSError, ValueError) as exc:
            raise _fault(f"Could not look up the latest pob4agents release: {exc}") from exc
        if latest is None:
            raise _fault("Could not find the latest pob4agents release (offline? try --proxy or --mirror).")
        report["latest"] = latest.lstrip("v")
        report["updateAvailable"] = is_newer(latest, __version__)
        if report["updateAvailable"]:
            emit(f"pob {report['latest']} is available (installed: {__version__}). Update: {report['command']}")
        else:
            emit(f"pob {__version__} is up to date (latest: {report['latest']}).")
        return

    if method != "standalone":
        raise click.ClickException(f"This pob was installed via {method}; update it with: {report['command']}")

    current = su.app_dir()
    if su.is_windows() and su.cwd_is_inside(current):
        raise click.ClickException(
            f"The current directory is inside {current}, which the update replaces; cd elsewhere and run it again."
        )
    try:
        result = _install(current, target, proxy, mirror)
    except UpdateError as exc:
        raise _fault(str(exc)) from exc

    if result.get("upToDate"):
        report["latest"] = result["tag"].lstrip("v")
        report["updated"] = False
        emit(f"pob {__version__} is up to date.")
    elif result.get("pending"):
        report["latest"] = result["version"]
        report["updated"] = "pending"
        emit(f"pob {result['version']} is downloaded and verified; it replaces {__version__} as soon as this command exits.")
    else:
        report["latest"] = result["version"]
        report["updated"] = True
        emit(f"Updated pob {__version__} -> {result['version']}.")


def register_self_update_commands(group: click.Group) -> None:
    group.add_command(cmd_self_update)
    group.add_command(cmd_release_check)
