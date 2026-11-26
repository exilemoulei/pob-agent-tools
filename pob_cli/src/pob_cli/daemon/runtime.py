"""Daemon runtime paths, slot slug validation, OS PID liveness, and stderr update alerts."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from ..bridge_client import RUNTIME_DIR
from ..compat import ensure_utf8
from ..errors import BootstrapMissingError as BootstrapMissingError
from ..os_util import _CREATE_BREAKAWAY_FROM_JOB as _CREATE_BREAKAWAY_FROM_JOB
from ..os_util import _ERROR_ACCESS_DENIED as _ERROR_ACCESS_DENIED
from ..os_util import _get_kernel32 as _get_kernel32
from ..os_util import _is_pid_alive as _is_pid_alive
from ..updater import format_stderr_update_alert, get_runtime_dir, load_update_state

GAME_ID = "poe1"
DAEMON_STATE_FILE = RUNTIME_DIR / "daemon.json"
DAEMON_LOCK_FILE = RUNTIME_DIR / "daemon.start.lock"
REF_IDLE_TIMEOUT_SEC = 300.0        # 5 minutes (for all non-main slots)
GLOBAL_IDLE_TIMEOUT_SEC = 1800.0    # 30 minutes
UPDATE_RECHECK_INTERVAL_SEC = 1800.0  # 30 minutes

_SLOT_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,48}$")

_stderr_alert_emitted = False
_stderr_alert_checked_at: float | None = None


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
    ensure_utf8(sys.stderr)
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


