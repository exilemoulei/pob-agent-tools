"""Boot-time child environment setup and bounded build restore for PoB2 ``BridgeWorker``."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .game_profile import ENV_LANG, ENV_PROXY
from .updater import get_runtime_dir, get_shared_cache_dir

RawCall = Callable[[str, dict[str, Any], float], dict[str, Any]]


def engine_env(worker: Any, extra_env: dict[str, str], lang: str) -> dict[str, str]:
    """Build isolated environment for the headless PoB2 LuaJIT child process."""
    env = os.environ.copy()
    env.update(extra_env)
    env[ENV_LANG] = lang
    env["POB_LANG"] = lang
    try:
        env.setdefault("POB2_CACHE_DIR", str(get_shared_cache_dir(worker.runtime_dir)))
    except Exception:
        pass
    pob2_proxy = env.get(ENV_PROXY, "").strip()
    if pob2_proxy:
        env.setdefault("HTTPS_PROXY", pob2_proxy)
        env.setdefault("HTTP_PROXY", pob2_proxy)
    return env


def aside_path(worker: Any) -> Path:
    return get_runtime_dir(worker.runtime_dir) / f"slot_{worker.slot_name}.restore_failed.txt"


def put_code_aside(worker: Any, code: str, reason: str, *, skipped: bool = False) -> None:
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


def _try_new_build(raw_call: RawCall) -> None:
    try:
        raw_call("new_build", {}, 10.0)
    except Exception:
        pass


def restore_build_on_start(raw_call: RawCall, target_code: str, build_loaded: Any) -> str | None:
    def _fresh_import() -> str | None:
        try:
            raw_call("new_build", {}, 10.0)
            raw_call("import_code", {"code": target_code, "mode": "replace"}, 20.0)
            return None
        except Exception as exc:
            _try_new_build(raw_call)
            return str(exc) or type(exc).__name__

    if build_loaded is False:
        return _fresh_import()
    try:
        raw_call("import_code", {"code": target_code, "mode": "new"}, 20.0)
        return None
    except Exception as exc:
        if "no build is loaded" in str(exc).lower():
            return _fresh_import()
        _try_new_build(raw_call)
        return str(exc) or type(exc).__name__


def restore_on_boot(worker: Any, target_code: str | None) -> None:
    def raw(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return worker._raw_call_unlocked(method, params, timeout=timeout, recover=False)

    if not target_code:
        raw("new_build", {}, 10.0)
        return
    error = restore_build_on_start(raw, target_code, worker.boot_build_loaded)
    if error is None:
        worker.last_good_code = target_code
        worker.restore_failed = None
        return
    put_code_aside(worker, target_code, error)
    if not worker.is_running:
        worker._launch_unlocked()
        raw("new_build", {}, 10.0)
