"""Daemon runtime paths, slot slug validation, OS PID liveness, and stderr update alerts."""

from __future__ import annotations

import inspect
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..bridge_client import RUNTIME_DIR
from ..errors import BootstrapMissingError as BootstrapMissingError
from ..updater import format_stderr_update_alert, get_runtime_dir, load_update_state

GAME_ID = "poe1"
DAEMON_STATE_FILE = RUNTIME_DIR / "daemon.json"
DAEMON_LOCK_FILE = RUNTIME_DIR / "daemon.start.lock"
REF_IDLE_TIMEOUT_SEC = 300.0        # 5 minutes (for all non-main slots)
GLOBAL_IDLE_TIMEOUT_SEC = 1800.0    # 30 minutes
UPDATE_RECHECK_INTERVAL_SEC = 1800.0  # 30 minutes

_SLOT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

_stderr_alert_emitted = False
_stderr_alert_checked_at: float | None = None
_kernel32: Any = None


def normalize_slot_name(slot: str | None) -> str:
    """Validate and normalize a named PoB worker slot slug (`^[a-zA-Z0-9._-]{1,48}$`)."""
    if slot is None:
        return "main"
    cleaned = str(slot).strip()
    if not cleaned:
        return "main"
    if cleaned in (".", "..") or not _SLOT_NAME_RE.match(cleaned):
        raise ValueError(
            f"Invalid slot name {slot!r}: must match ^[a-zA-Z0-9._-]{{1,48}}$ and not be '.' or '..'."
        )
    # Lowercase all slugs: slot_<name>.json collides on case-insensitive filesystems (Windows/macOS).
    return cleaned.lower()


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir)


def _build_daemon_spawn_cmd(runtime_dir: Path | None = None) -> list[str]:
    """Build the command line to spawn the background daemon (supports Python and frozen exe)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_daemon"]
    else:
        cmd = [sys.executable, "-m", "pob_cli.daemon"]
    if runtime_dir is not None:
        cmd.extend(["--runtime-dir", str(runtime_dir)])
    return cmd


def _daemon_state_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.json"


def _daemon_lock_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.start.lock"


def _daemon_boot_error_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.boot_error.json"


def _is_offline_env() -> bool:
    val = os.environ.get("POB_OFFLINE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _get_kernel32() -> Any:
    global _kernel32
    if _kernel32 is None:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.GetExitCodeProcess.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        _kernel32 = k32
    return _kernel32


def _is_pid_alive(pid: int) -> bool:
    """Fast (<1ms) OS-native process liveness probe to avoid 2s stale loopback TCP timeouts."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        import ctypes

        k32 = _get_kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            err = ctypes.get_last_error()
            return err == _ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_uint32(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def reset_stderr_alert_flag() -> None:
    """Reset the per-process stderr update alert deduplication flag (used by tests)."""
    global _stderr_alert_emitted, _stderr_alert_checked_at
    _stderr_alert_emitted = False
    _stderr_alert_checked_at = None


def emit_stderr_update_alert(state: dict[str, Any] | None, *, force: bool = False) -> bool:
    """Print the prominent single-block Chinese update alert to stderr (deduplicated per checkedAt/process)."""
    global _stderr_alert_emitted, _stderr_alert_checked_at
    checked_at_raw = state.get("checkedAt") if isinstance(state, dict) else None
    checked_at = float(checked_at_raw) if isinstance(checked_at_raw, (int, float)) else None
    if _stderr_alert_emitted and not force:
        if checked_at is None or checked_at == _stderr_alert_checked_at:
            return False
    alert = format_stderr_update_alert(state)
    if not alert:
        return False
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.stderr.write(alert + "\n")
    sys.stderr.flush()
    _stderr_alert_emitted = True
    if checked_at is not None:
        _stderr_alert_checked_at = checked_at
    return True


def _maybe_emit_degraded_alert(result: Any, rt_dir: Path) -> None:
    """Emit Chinese stderr alert when a daemon RPC result indicates degraded update state."""
    import pob_cli.daemon as _daemon_pkg

    if not isinstance(result, dict):
        return
    upd_status = result.get("updateStatus")
    if isinstance(upd_status, dict) and str(upd_status.get("state") or "").startswith("degraded_"):
        _daemon_pkg.emit_stderr_update_alert(upd_status)
        return
    if str(result.get("state") or "").startswith("degraded_"):
        _daemon_pkg.emit_stderr_update_alert(result)
        return
    warn = result.get("updateWarning")
    if isinstance(warn, str) and warn.strip():
        disk_state = load_update_state(rt_dir)
        if isinstance(disk_state, dict) and str(disk_state.get("state") or "").startswith("degraded_"):
            _daemon_pkg.emit_stderr_update_alert(disk_state)
        else:
            _daemon_pkg.emit_stderr_update_alert({"state": "degraded_network_error", "userHint": warn})


def _call_compat(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call `fn` forwarding only keyword arguments accepted by its signature."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)
