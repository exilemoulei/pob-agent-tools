"""Local HTTP daemon managing persistent named headless PoB workers with per-slot locking."""

from __future__ import annotations

import subprocess
import sys

from ..bridge_client import DEFAULT_POB_DIR, DEFAULT_PROJECT_ROOT, RUNTIME_DIR, BridgeError, BridgeWorker
from ..errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from ..updater import check_and_apply_updates
from .client import (
    _ensure_daemon,
    _guard_foreign_daemon_state,
    _http_post_rpc,
    _parse_daemon_endpoint,
    _probe_daemon,
    _read_boot_error,
    _rpc_result,
    _spawn_daemon_process,
    call_daemon,
    main,
    read_daemon_state,
    stop_daemon,
)
from .runtime import (
    _CREATE_BREAKAWAY_FROM_JOB,
    DAEMON_LOCK_FILE,
    DAEMON_STATE_FILE,
    GAME_ID,
    GLOBAL_IDLE_TIMEOUT_SEC,
    REF_IDLE_TIMEOUT_SEC,
    UPDATE_RECHECK_INTERVAL_SEC,
    BootstrapMissingError,
    _build_daemon_spawn_cmd,
    _daemon_boot_error_file,
    _daemon_lock_file,
    _daemon_state_file,
    _get_kernel32,
    _get_runtime_dir,
    _is_offline_env,
    _is_pid_alive,
    _maybe_emit_degraded_alert,
    emit_stderr_update_alert,
    normalize_slot_name,
    reset_stderr_alert_flag,
)
from .server import _serve_daemon, make_handler, run_daemon_forever, run_daemon_server
from .slots import DaemonManager

__all__ = [
    "DAEMON_LOCK_FILE",
    "DAEMON_STATE_FILE",
    "DEFAULT_POB_DIR",
    "DEFAULT_PROJECT_ROOT",
    "GAME_ID",
    "GLOBAL_IDLE_TIMEOUT_SEC",
    "REF_IDLE_TIMEOUT_SEC",
    "RUNTIME_DIR",
    "UPDATE_RECHECK_INTERVAL_SEC",
    "_CREATE_BREAKAWAY_FROM_JOB",
    "BootstrapMissingError",
    "BridgeError",
    "BridgeWorker",
    "CrossGameHallucinationError",
    "DaemonManager",
    "DaemonRequestError",
    "LegacyPatchTrapError",
    "_build_daemon_spawn_cmd",
    "_daemon_boot_error_file",
    "_daemon_lock_file",
    "_daemon_state_file",
    "_ensure_daemon",
    "_get_kernel32",
    "_get_runtime_dir",
    "_guard_foreign_daemon_state",
    "_http_post_rpc",
    "_is_offline_env",
    "_is_pid_alive",
    "_maybe_emit_degraded_alert",
    "_parse_daemon_endpoint",
    "_probe_daemon",
    "_read_boot_error",
    "_rpc_result",
    "_serve_daemon",
    "_spawn_daemon_process",
    "call_daemon",
    "check_and_apply_updates",
    "emit_stderr_update_alert",
    "main",
    "make_handler",
    "normalize_slot_name",
    "read_daemon_state",
    "reset_stderr_alert_flag",
    "run_daemon_forever",
    "run_daemon_server",
    "stop_daemon",
    "subprocess",
    "sys",
]
