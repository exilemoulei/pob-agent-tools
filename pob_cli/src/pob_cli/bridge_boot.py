"""Boot-time build restore for `BridgeWorker`: one bounded attempt, and a code that fails is put aside.

Every call here goes through `_raw_call_unlocked(..., recover=False)`: a restore that hangs or
crashes the engine kills that process and returns, it never restarts the worker from inside its own
boot. The worker then boots empty, and the failed code is written to `slot_<slug>.restore_failed.txt`
so the user can load it again by hand (`pob load @<file>`) and the next daemon start skips it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bridge_trees import stage_tree_versions_hint
from .updater import get_runtime_dir, get_shared_cache_dir

RawCall = Callable[[str, dict[str, Any], float], dict[str, Any]]


def restore_build_on_start(raw_call: RawCall, target_code: str, build_loaded: Any) -> str | None:
    """Import a saved build code at boot; None on success, else why it failed (R4: skip redundant new_build)."""

    def _fresh_import() -> str | None:
        try:
            raw_call("new_build", {}, 10.0)
            raw_call("import_code", {"code": target_code, "mode": "replace"}, 15.0)
            return None
        except Exception as exc:
            _try_new_build(raw_call)
            return str(exc) or type(exc).__name__

    if build_loaded is False:
        return _fresh_import()
    try:
        raw_call("import_code", {"code": target_code, "mode": "new"}, 15.0)
        return None
    except Exception as exc:
        if "no build is loaded" in str(exc).lower():
            return _fresh_import()
        _try_new_build(raw_call)
        return str(exc) or type(exc).__name__


def _try_new_build(raw_call: RawCall) -> None:
    try:
        raw_call("new_build", {}, 10.0)
    except Exception:
        pass


def engine_env(worker: Any, extra_env: dict[str, str], lang: str) -> dict[str, str]:
    """The environment an engine process starts with."""
    env = os.environ.copy()
    env.update(extra_env)
    env["POB_LANG"] = lang
    try:
        env.setdefault("POB_CACHE_DIR", str(get_shared_cache_dir(worker.runtime_dir, pob_root=worker.pob_root)))
    except Exception:
        pass
    # Mirror POB_PROXY into HTTPS_PROXY/HTTP_PROXY so Windows pob-zh.exe lcurl honors it.
    pob_proxy = env.get("POB_PROXY", "").strip()
    if pob_proxy:
        env.setdefault("HTTPS_PROXY", pob_proxy)
        env.setdefault("HTTP_PROXY", pob_proxy)
    return env


def aside_path(worker: Any) -> Path:
    return get_runtime_dir(worker.runtime_dir) / f"slot_{worker.slot_name}.restore_failed.txt"


def put_code_aside(worker: Any, code: str, reason: str, *, skipped: bool = False) -> None:
    """Drop `code` as the worker's build, keep a copy on disk, and record why for `status`."""
    code_file: str | None = None
    if worker.slot_name != "test":
        path = aside_path(worker)
        try:
            if not skipped:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(code, encoding="utf-8")
                os.replace(tmp, path)
            code_file = str(path)
        except OSError:
            code_file = None
    worker.last_good_code = None
    worker.restore_failed = {
        "error": (reason or "restore failed").splitlines()[0][:300],
        "codeFile": code_file,
        "skipped": skipped,
        "ts": time.time(),
    }


def _failed_before(worker: Any, code: str) -> bool:
    """True when this exact code already failed to restore and the slot file has not changed since."""
    if worker.slot_name == "test":
        return False
    path = aside_path(worker)
    try:
        if path.read_text(encoding="utf-8") != code:
            return False
        slot_file = worker.slot_state_path
        return not slot_file.exists() or path.stat().st_mtime >= slot_file.stat().st_mtime
    except OSError:
        return False


def restore_on_boot(worker: Any, target_code: str | None, *, from_slot_file: bool) -> None:
    """Load `target_code` into a freshly launched worker, or boot empty; at most one relaunch."""

    def raw(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return worker._raw_call_unlocked(method, params, timeout=timeout, recover=False)

    if target_code and from_slot_file and _failed_before(worker, target_code):
        put_code_aside(worker, target_code, "this saved build failed to restore before", skipped=True)
        target_code = None
    if not target_code:
        raw("new_build", {}, 10.0)
        return
    stage_tree_versions_hint(target_code, worker.game_dir)  # a failed restore falls back cleanly
    error = restore_build_on_start(raw, target_code, worker.boot_build_loaded)
    if error is None:
        worker.last_good_code = target_code
        worker.restore_failed = None
        return
    put_code_aside(worker, target_code, error)
    if not worker.is_running:
        # The restore hung or crashed the engine: one clean relaunch, empty.
        worker._launch_unlocked()
        raw("new_build", {}, 10.0)
