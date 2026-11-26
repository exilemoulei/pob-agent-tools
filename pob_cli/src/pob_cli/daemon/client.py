"""Loopback HTTP client helpers, auto-start locking, stop_daemon, and CLI entrypoint."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from ..errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from ..json_io import read_json_dict
from ..os_util import spawn_detached_process
from ..updater import STATE_BOOTSTRAP_MISSING, load_update_state
from .runtime import (
    GAME_ID,
    BootstrapMissingError,
    _build_daemon_spawn_cmd,
    _daemon_boot_error_file,
    _daemon_lock_file,
    _daemon_state_file,
    _get_runtime_dir,
    _is_offline_env,
    _is_pid_alive,
    _maybe_emit_degraded_alert,
)


def read_daemon_state(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    return read_json_dict(_daemon_state_file(runtime_dir))


def _read_boot_error(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    return read_json_dict(_daemon_boot_error_file(runtime_dir))


def _http_post_rpc(
    port: int,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float = 150.0,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request(
            "POST",
            "/rpc",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "X-PoB-Token": token,
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
    finally:
        conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}: {raw[:200]}"}
        raise RuntimeError(f"Invalid JSON response from PoB daemon (HTTP {status}): {raw[:200]}") from exc
    if not isinstance(data, dict):
        if status in (403, 404):
            return status, {"ok": False, "error": f"HTTP {status}"}
        raise RuntimeError(f"Unexpected response payload from PoB daemon (HTTP {status}).")
    return status, data


def _parse_daemon_endpoint(state: dict[str, Any]) -> tuple[int, str]:
    """Validate and extract `(port, token)` from a `daemon.json` state dict."""
    raw_port = state["port"]
    if isinstance(raw_port, bool):
        raise TypeError("Invalid boolean port in daemon.json")
    port = int(raw_port)
    if not (1 <= port <= 65535):
        raise ValueError(f"Out-of-range port in daemon.json: {port}")
    raw_token = state["token"]
    if raw_token is None:
        raise TypeError("Missing token in daemon.json")
    return port, str(raw_token)


def _guard_foreign_daemon_state(state: dict[str, Any], state_file: Path, rt_dir: Path) -> None:
    """Non-destructively block when `daemon.json` explicitly belongs to `poe2`."""
    recorded_game = state.get("game")
    if recorded_game is None:
        return
    if str(recorded_game).strip().lower() == "poe2":
        raise CrossGameHallucinationError(
            game=GAME_ID,
            detected_game="poe2",
            trigger=f"{state_file} (game={recorded_game!r})",
            error=f"Runtime directory `{rt_dir}` belongs to a `poe2` daemon, not `{GAME_ID}`.",
            remediation="Use separate runtime directories (`POB_RUNTIME_DIR` vs `POB2_RUNTIME_DIR`).",
            equivalent_command="pob2 status",
            rule_id="DAEMON_STATE_GAME_MISMATCH",
        )


def _probe_daemon(state: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Return daemon status result if alive and reachable, else None."""
    import pob_cli.daemon as _daemon_pkg

    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        return None
    try:
        port, token = _parse_daemon_endpoint(state)
        status_code, data = _daemon_pkg._http_post_rpc(
            port,
            token,
            {"action": "status", "params": {"skip_periodic_check": True}},
            timeout=timeout,
        )
        if status_code == 400 and data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if status_code == 200 and data.get("ok"):
            res = data.get("result")
            if isinstance(res, dict) and str(res.get("game") or "").strip().lower() == "poe2":
                raise CrossGameHallucinationError(
                    game=GAME_ID,
                    detected_game="poe2",
                    trigger=f"status.game={res.get('game')!r}",
                    error="Connected daemon reports `game='poe2'`, not `poe1`.",
                    remediation="Use separate runtime directories (`POB_RUNTIME_DIR` vs `POB2_RUNTIME_DIR`).",
                    equivalent_command="pob2 status",
                    rule_id="DAEMON_STATUS_GAME_MISMATCH",
                )
            return res
    except (OSError, http.client.HTTPException, TimeoutError, KeyError, TypeError, ValueError):
        return None
    return None


def stop_daemon(runtime_dir: Path | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    """Stop the running local PoB daemon cleanly if alive; unlink stale daemon.json if dead."""
    import pob_cli.daemon as _daemon_pkg

    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)
    state = _daemon_pkg.read_daemon_state(rt_dir)
    if state is None:
        return {"stopped": False, "alreadyStopped": True}

    _guard_foreign_daemon_state(state, state_file, rt_dir)

    pid = state.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"stopped": False, "staleCleaned": True}

    try:
        port, token = _parse_daemon_endpoint(state)
        status_code, data = _daemon_pkg._http_post_rpc(
            port, token, {"action": "stop", "params": {}}, timeout=timeout
        )
        if status_code == 400 and data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if status_code in (403, 404):
            try:
                state_file.unlink()
            except OSError:
                pass
            return {"stopped": False, "staleCleaned": True}
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Daemon stop error")
        return data.get("result") or {"stopped": True}
    except (OSError, http.client.HTTPException, TimeoutError, KeyError, TypeError, ValueError):
        try:
            state_file.unlink()
        except OSError:
            pass
        return {"stopped": False, "staleCleaned": True}


def _spawn_daemon_process(cmd: list[str], rt_dir: Path, log_fh: Any) -> subprocess.Popen[Any]:
    """Spawn background daemon detached from current session/job without flashing a console window."""
    import pob_cli.daemon as _daemon_pkg

    sub = _daemon_pkg.subprocess
    return spawn_detached_process(
        cmd,
        sub=sub,
        cwd=str(rt_dir),
        stdin=sub.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
    )


def _ensure_daemon(
    rt_dir: Path,
    *,
    runtime_dir_arg: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Spawn (serialized by lockfile) and wait until a fresh local daemon responds to `status`."""
    import pob_cli.daemon as _daemon_pkg

    lock_file = _daemon_lock_file(rt_dir)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    log_path = rt_dir / "daemon.log"

    acquired_lock = False
    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            stale_lock = lock_age > 70.0
            if not stale_lock:
                try:
                    raw_pid = lock_file.read_text(encoding="ascii", errors="ignore").strip()
                    if raw_pid.isdigit() and not _is_pid_alive(int(raw_pid)):
                        stale_lock = True
                except OSError:
                    pass
            if stale_lock:
                lock_file.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        acquired_lock = True
    except OSError:
        acquired_lock = False

    spawned_proc: subprocess.Popen[Any] | None = None
    ping_status_result: dict[str, Any] | None = None
    final_state: dict[str, Any] | None = None

    try:
        if acquired_lock:
            try:
                if boot_err_file.exists():
                    boot_err_file.unlink()
            except OSError:
                pass

            cmd = _build_daemon_spawn_cmd(rt_dir if runtime_dir_arg is not None else None)
            with open(log_path, "a", encoding="utf-8") as log_fh:
                spawned_proc = _spawn_daemon_process(cmd, rt_dir, log_fh)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            boot_err = _read_boot_error(rt_dir)
            if boot_err is not None:
                err_msg = str(boot_err.get("error") or "PoB daemon failed to start.")
                if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                    raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                raise RuntimeError(err_msg)

            st = _daemon_pkg.read_daemon_state(rt_dir)
            if st is not None:
                probed = _probe_daemon(st, timeout=5.0)
                if probed is not None:
                    ping_status_result = probed
                    final_state = st
                    break

            if spawned_proc is not None and spawned_proc.poll() is not None:
                boot_err = _read_boot_error(rt_dir)
                if boot_err is not None:
                    err_msg = str(boot_err.get("error") or "PoB daemon exited during startup.")
                    if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                        raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                    raise RuntimeError(err_msg)
                raise RuntimeError(
                    f"PoB daemon exited immediately with code {spawned_proc.returncode}. Check {log_path}."
                )

            time.sleep(0.05)
        else:
            tail = ""
            try:
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if lines:
                        tail = " | log tail: " + " ; ".join(lines[-8:])
            except OSError:
                pass
            raise RuntimeError(f"Timed out waiting for PoB daemon to start. Check {log_path}.{tail}")
    finally:
        if acquired_lock:
            try:
                lock_file.unlink()
            except OSError:
                pass

    if final_state is None:
        final_state = _daemon_pkg.read_daemon_state(rt_dir)
    if not final_state:
        raise RuntimeError("PoB daemon state file disappeared after startup.")
    return final_state, ping_status_result


def _rpc_result(data: dict[str, Any]) -> dict[str, Any]:
    """Preserve the daemon's request/failure classification in the CLI process."""
    if not data.get("ok"):
        if data.get("errorKind") == "cross_game_hallucination":
            raise CrossGameHallucinationError.from_dict(data, default_game=GAME_ID)
        if data.get("errorKind") == "legacy_patch_trap":
            raise LegacyPatchTrapError.from_dict(data)
        message = data.get("error") or "Daemon error"
        if data.get("errorKind") == "request":
            raise DaemonRequestError(message)
        raise RuntimeError(message)
    if "result" not in data:
        raise RuntimeError("Malformed daemon RPC response: missing 'result'.")
    return data["result"]


def call_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
    runtime_dir: Path | None = None,
    emit_cold_start_alert: bool = True,
) -> dict[str, Any]:
    """Send an authenticated RPC to the local PoB daemon, starting it automatically if not running."""
    import pob_cli.daemon as _daemon_pkg

    if action in ("status", "load") and _is_offline_env():
        # A daemon reads POB_OFFLINE once, when it starts. One started without it would still run its
        # periodic engine update check on these two actions, whatever `--offline` this command has.
        params = {**(params or {}), "skip_periodic_check": True}

    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)

    state = _daemon_pkg.read_daemon_state(rt_dir)
    if state is not None:
        _guard_foreign_daemon_state(state, state_file, rt_dir)
        pid = state.get("pid")
        if isinstance(pid, int) and not _is_pid_alive(pid):
            try:
                state_file.unlink()
            except OSError:
                pass
            state = None
        else:
            pid_alive = isinstance(pid, int) and not isinstance(pid, bool) and _is_pid_alive(pid)
            try:
                port, token = _parse_daemon_endpoint(state)
                status_code, data = _daemon_pkg._http_post_rpc(
                    port, token, {"action": action, "params": params or {}}, timeout=timeout
                )
            except TimeoutError:
                if pid_alive:
                    raise
                try:
                    state_file.unlink()
                except OSError:
                    pass
                state = None
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                # Refused means nothing listens on the port, so no daemon got the action. Any other
                # failure from a live daemon may come after it ran the action: sending it again to a
                # second daemon could apply an edit twice, with two daemons sharing one runtime dir.
                if pid_alive and not isinstance(exc, ConnectionRefusedError):
                    raise RuntimeError(
                        f"The PoB daemon (pid {pid}) did not answer '{action}' cleanly ({exc!r}); it may "
                        "still be running it. Check with `pob status`, or run `pob stop` if this repeats."
                    ) from exc
                try:
                    state_file.unlink()
                except OSError:
                    pass
                state = None
            except (KeyError, TypeError, ValueError):
                # A malformed daemon.json: nothing was sent.
                try:
                    state_file.unlink()
                except OSError:
                    pass
                state = None
            else:
                if status_code in (403, 404):
                    try:
                        state_file.unlink()
                    except OSError:
                        pass
                    state = None
                else:
                    result = _rpc_result(data)
                    if emit_cold_start_alert:
                        _maybe_emit_degraded_alert(result, rt_dir)
                    return result

    if not auto_start:
        raise RuntimeError("PoB daemon is not running.")

    st, ping_status_result = _daemon_pkg._ensure_daemon(rt_dir, runtime_dir_arg=runtime_dir)

    if emit_cold_start_alert:
        upd_state = (ping_status_result or {}).get("updateStatus") or load_update_state(rt_dir)
        if str((upd_state or {}).get("state") or "").startswith("degraded_"):
            _daemon_pkg.emit_stderr_update_alert(upd_state)

    if action == "status" and ping_status_result is not None:
        return ping_status_result

    try:
        port, token = _parse_daemon_endpoint(st)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Corrupt PoB daemon state after startup: {exc}") from exc
    _, data = _daemon_pkg._http_post_rpc(
        port, token, {"action": action, "params": params or {}}, timeout=timeout
    )
    result = _rpc_result(data)
    if emit_cold_start_alert:
        _maybe_emit_degraded_alert(result, rt_dir)
    return result


def main() -> None:
    import argparse

    import pob_cli.daemon as _daemon_pkg

    parser = argparse.ArgumentParser(description="Headless PoB local HTTP daemon")
    parser.add_argument("--pob-dir", type=Path, default=None)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--mirror", type=str, default=None)
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args()
    runner = _daemon_pkg.run_daemon_forever if args.foreground else _daemon_pkg.run_daemon_server
    runner(
        pob_dir=args.pob_dir,
        port=args.port,
        runtime_dir=args.runtime_dir,
        offline=True if args.offline else None,
        proxy=args.proxy,
        mirror=args.mirror,
    )
