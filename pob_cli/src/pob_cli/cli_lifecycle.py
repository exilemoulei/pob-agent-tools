"""Daemon lifecycle and engine bootstrap/update Click commands for `pob` CLI."""

from __future__ import annotations

import os
import sys
from typing import Any

import click

from .cli_build_io import register_build_io_commands
from .cli_common import (
    DAEMON_FAILURE_EXIT_CODE,
    _call_with_supported_kwargs,
    _echo_stderr_utf8,
    _emit_json,
    _emit_text,
    _output_options,
    _prepend_warning,
    invoke_daemon,
)
from .cli_lang import (
    _installed_locales as _installed_locales,
)
from .cli_lang import (
    cmd_lang as cmd_lang,
)


@click.command("_daemon", hidden=True)
@click.option("--pob-dir", type=click.Path(), default=None)
@click.option("--runtime-dir", type=click.Path(), default=None)
@click.option("--port", type=int, default=0)
@click.option("--offline", is_flag=True, default=False)
@click.option("--proxy", type=str, default=None)
@click.option("--mirror", type=str, default=None)
def cmd_internal_daemon(
    pob_dir: str | None,
    runtime_dir: str | None,
    port: int,
    offline: bool,
    proxy: str | None,
    mirror: str | None,
) -> None:
    """Internal entry point for spawning the background daemon (used by standalone binaries)."""
    from pathlib import Path

    from .daemon import run_daemon_server

    run_daemon_server(
        pob_dir=Path(pob_dir) if pob_dir else None,
        port=port,
        runtime_dir=Path(runtime_dir) if runtime_dir else None,
        offline=True if offline else None,
        proxy=proxy,
        mirror=mirror,
    )


@click.command("serve")
@click.option("--offline", is_flag=True, default=False, help="Skip GitHub engine update checks.")
@click.option("--proxy", type=str, default=None, help="HTTP/SOCKS proxy URL.")
@click.option("--mirror", type=str, default=None, help="Optional GitHub mirror prefix URL.")
@click.option("--port", type=int, default=0, help="Loopback TCP port (0 = auto-assign).")
@click.option("--runtime-dir", type=click.Path(), default=None, help="Override runtime state directory.")
def cmd_serve(
    offline: bool,
    proxy: str | None,
    mirror: str | None,
    port: int,
    runtime_dir: str | None,
) -> None:
    """Run the PoB daemon persistently in the foreground (ideal for IsDaemon:true agent shells)."""
    from pathlib import Path

    from . import daemon as daemon_mod
    from .errors import CrossGameHallucinationError
    from .updater import format_stderr_update_alert

    runner = getattr(daemon_mod, "run_daemon_forever", None) or daemon_mod.run_daemon_server
    try:
        _call_with_supported_kwargs(
            runner,
            port=port,
            runtime_dir=Path(runtime_dir) if runtime_dir else None,
            offline=True if offline else None,
            proxy=proxy,
            mirror=mirror,
        )
    except CrossGameHallucinationError:
        raise
    except daemon_mod.BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        _echo_stderr_utf8(alert)
        raise click.exceptions.Exit(1) from exc
    except (OSError, RuntimeError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB daemon failed to start: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc


@click.command("bootstrap")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--no-zh", is_flag=True, help="Skip PobTools-zh bilingual Chinese DataV2 pack.")
@click.option(
    "--from-dir",
    "from_dir",
    type=click.Path(exists=True, file_okay=False),
    help="Offline import from an existing local PobTools directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bootstrap(
    proxy: str | None,
    mirror: str | None,
    no_zh: bool,
    from_dir: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Bootstrap or offline-import the managed headless PoB engine."""
    import time
    from pathlib import Path

    from .daemon import call_daemon, emit_stderr_update_alert, read_daemon_state
    from .errors import CrossGameHallucinationError
    from .formatters import render_table
    from .updater import (
        STATE_BOOTSTRAP_MISSING,
        EngineGateError,
        EngineLockError,
        UpdateError,
        bootstrap_engine,
        get_update_warning,
    )

    if read_daemon_state() is not None:
        try:
            call_daemon("stop", auto_start=False)
            deadline = time.monotonic() + 2.0
            while read_daemon_state() is not None and time.monotonic() < deadline:
                time.sleep(0.1)
        except CrossGameHallucinationError:
            raise
        except Exception:
            pass

    try:
        res = bootstrap_engine(
            proxy=proxy,
            mirror=mirror,
            include_zh=not no_zh,
            from_dir=Path(from_dir) if from_dir else None,
        )
    except EngineGateError as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB engine bootstrap gate check failed: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc
    except EngineLockError as exc:  # busy or read-only engine folder: nothing wrong with the request
        err = click.ClickException(str(exc).strip() or type(exc).__name__)
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc
    except UpdateError as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise click.ClickException(detail) from exc
    except OSError as exc:
        detail = str(exc).strip() or type(exc).__name__
        err = click.ClickException(f"PoB engine bootstrap failed: {detail}")
        err.exit_code = DAEMON_FAILURE_EXIT_CODE
        raise err from exc
    warn = get_update_warning(res)
    if warn:
        res["updateWarning"] = warn
    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
    else:
        rows = [
            ["State", state_name or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["PoB Version", res.get("localPobVersion") or "-"],
            ["PobTools-zh", res.get("pobToolsVersion") or "-"],
            ["Data Pack", res.get("dataVersion") or "-"],
            ["Updated Files", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
            ["Source", res.get("source") or "github"],
        ]
        body = "### PoB Engine Bootstrap\n" + render_table(["Field", "Value"], rows)
        _emit_text(_prepend_warning(body, res), out_path=out_path)

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@click.command("update")
@click.option("--check", "check_only", is_flag=True, help="Check remote versions without applying updates.")
@click.option("--force", is_flag=True, help="Force full check/apply ignoring cached ETag.")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--offline", is_flag=True, help="Skip network check and report local versions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_update(
    check_only: bool,
    force: bool,
    proxy: str | None,
    mirror: str | None,
    offline: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Check or incrementally update managed PoB & PobTools-zh engine (preserving loaded builds if daemon is alive)."""
    import http.client

    from .daemon import DaemonRequestError, call_daemon, emit_stderr_update_alert, read_daemon_state
    from .errors import CrossGameHallucinationError
    from .formatters import render_table
    from .updater import STATE_BOOTSTRAP_MISSING, check_and_apply_updates, get_update_warning

    eff_offline = bool(offline or os.environ.get("POB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"))
    res: dict[str, Any] | None = None
    if read_daemon_state() is not None:
        try:
            res = call_daemon(
                "update",
                {
                    "check_only": check_only,
                    "force": force,
                    "proxy": proxy,
                    "mirror": mirror,
                    "offline": eff_offline,
                },
                auto_start=False,
                timeout=180.0,
            )
        except CrossGameHallucinationError:
            raise
        except DaemonRequestError as exc:
            msg = str(exc).strip() or "Daemon rejected 'update'."
            raise click.ClickException(msg) from exc
        except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
            if "not running" not in str(exc).lower():
                detail = str(exc).strip() or type(exc).__name__
                err = click.ClickException(
                    f"PoB daemon failure during 'update': {detail}\n"
                    "Run `pob stop` and retry; check daemon logs in the runtime directory if this persists."
                )
                err.exit_code = DAEMON_FAILURE_EXIT_CODE
                raise err from exc

    if res is None:
        res = check_and_apply_updates(
            check_only=check_only,
            force=force,
            proxy=proxy,
            mirror=mirror,
            offline=eff_offline,
        )
        warn = get_update_warning(res)
        if warn:
            res["updateWarning"] = warn

    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
    else:
        rows = [
            ["State", state_name or "-"],
            ["Local PoB Version", res.get("localPobVersion") or "-"],
            ["Remote PoB Version", res.get("remotePobVersion") or "-"],
            ["PobTools-zh Version", res.get("pobToolsVersion") or "-"],
            ["Data Pack Version", res.get("dataVersion") or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["Update Available", res.get("updateAvailable") if res.get("updateAvailable") is not None else "-"],
            ["Files Updated", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
        ]
        if res.get("networkError"):
            rows.append(["Network Error", res["networkError"]])
        if res.get("updateSkipped"):
            rows.append(["Update Skipped", res["updateSkipped"]])
        body = "### PoB Engine Update Status\n" + render_table(["Field", "Value"], rows)
        _emit_text(_prepend_warning(body, res), out_path=out_path)

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_status(as_json: bool, compact: bool, out_path: str | None) -> None:
    """Show daemon status, engine versions, update state, and loaded builds across slots."""
    from .daemon import emit_stderr_update_alert
    from .formatters import render_table
    from .guardrails import get_auth_env_status

    res = invoke_daemon("status")
    upd = res.get("updateStatus") or {}
    if str(upd.get("state") or "").startswith("degraded_") or res.get("updateWarning"):
        emit_stderr_update_alert(upd)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    m = res.get("main", {})
    m_hello = m.get("hello") or {}

    pob_ver = upd.get("localPobVersion") or m_hello.get("pobVersion") or "-"
    tools_ver = upd.get("pobToolsVersion") or "-"
    data_ver = upd.get("dataVersion") or "-"
    upd_state = upd.get("state") or "-"

    slots_map = res.get("slots") if isinstance(res.get("slots"), dict) else {}
    slot_entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for default_slot in ("main", "ref"):
        if default_slot in slots_map:
            slot_entries.append((default_slot, slots_map[default_slot]))
            seen.add(default_slot)
        elif default_slot in res and isinstance(res[default_slot], dict):
            slot_entries.append((default_slot, res[default_slot]))
            seen.add(default_slot)
    for k, v in sorted(slots_map.items()):
        if k not in seen and isinstance(v, dict):
            slot_entries.append((k, v))

    rows = []
    for slot_name, slot_data in slot_entries:
        s_info = slot_data.get("buildInfo") or {}
        state_lbl = "Running" if slot_data.get("alive") else ("Stopped" if slot_name == "main" else "Idle (Stopped)")
        undo_str = (
            f"{slot_data.get('undoDepth', 0)} / {slot_data.get('redoDepth', 0)}"
            if "undoDepth" in slot_data
            else "-"
        )
        rows.append(
            [
                slot_name,
                state_lbl,
                s_info.get("classNameZh") or s_info.get("className") or "-",
                s_info.get("ascendClassNameZh") or s_info.get("ascendClassName") or "-",
                s_info.get("level") or "-",
                undo_str,
            ]
        )

    raw_auth = res.get("auth")
    auth = raw_auth if isinstance(raw_auth, dict) else get_auth_env_status()
    auth_line = (
        f"Auth: POESESSID={'set' if auth.get('poesessid') else 'unset'}, "
        f"POE_COOKIE={'set' if auth.get('poeCookie') else 'unset'}, "
        f"POE_CN_SESSID={'set' if auth.get('poeCnSessid') else 'unset'}, "
        f"POE_CN_COOKIE={'set' if auth.get('poeCnCookie') else 'unset'}\n"
    )
    lang_lbl = res.get("lang") or "-"
    header = (
        f"Daemon PID: `{res.get('pid')}` | Uptime: `{res.get('uptimeSec')}s` | "
        f"PoB: `{pob_ver}` | PobTools: `{tools_ver}` | Data: `{data_ver}` | Lang: `{lang_lbl}` | Update: `{upd_state}`\n"
    )
    body = header + auth_line + render_table(["Slot", "State", "Class", "Ascendancy", "Level", "Undo/Redo"], rows)
    _emit_text(_prepend_warning(body, res), out_path=out_path)


@click.command("stop")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_stop(as_json: bool, compact: bool, out_path: str | None) -> None:
    """Stop the background PoB daemon and release headless processes."""
    import http.client

    from .daemon import DaemonRequestError, call_daemon
    from .errors import CrossGameHallucinationError

    try:
        res = call_daemon("stop", auto_start=False)
    except CrossGameHallucinationError:
        raise
    except DaemonRequestError as exc:
        msg = str(exc).strip() or "Daemon rejected 'stop'."
        raise click.ClickException(msg) from exc
    except (RuntimeError, OSError, http.client.HTTPException, ValueError) as exc:
        if "not running" in str(exc).lower():
            res = {"stopped": True, "note": "Daemon was not running"}
        else:
            detail = str(exc).strip() or type(exc).__name__
            err = click.ClickException(
                f"PoB daemon failure during 'stop': {detail}\n"
                "Check daemon logs in the runtime directory or terminate the stale process manually if this persists."
            )
            err.exit_code = DAEMON_FAILURE_EXIT_CODE
            raise err from exc
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text("PoB daemon stopped.", out_path=out_path)


def register_lifecycle_commands(group: click.Group) -> None:
    for cmd in (cmd_internal_daemon, cmd_serve, cmd_bootstrap, cmd_update, cmd_status, cmd_lang, cmd_stop):
        group.add_command(cmd)
    register_build_io_commands(group)
