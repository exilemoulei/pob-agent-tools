"""Headless PoB bridge subprocess wrapper (lossless transport, thread-safe, auto-healing)."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .bridge_protocol import (
    FORBIDDEN_METHODS,
    MAX_UNDO_STEPS,
    BridgeError,
    BridgeTimeoutError,
    apply_worker_locale,
    await_startup_handshake,
    check_tree_version_mismatch,
    collect_batch_responses,
    compact_source_label,
    decode_batch_responses,
    format_rpc_error,
    read_slot_state,
    restore_build_on_start,
    stage_missing_tree_versions,
    stderr_drain_loop,
    stdout_reader_loop,
    validate_import_xml_guardrail,
    validate_rpc_method,
    write_slot_state,
)
from .locale_cfg import current_lang, normalize_lang
from .updater import (
    build_engine_command,
    detect_source_checkout_root,
    get_runtime_dir,
    get_shared_cache_dir,
    resolve_engine_root,
)

DEFAULT_PROJECT_ROOT = (
    Path(os.environ["POB_HOME"].strip())
    if os.environ.get("POB_HOME", "").strip()
    else (detect_source_checkout_root() or get_runtime_dir())
)
DEFAULT_POB_ROOT = resolve_engine_root()
DEFAULT_POB_DIR = DEFAULT_POB_ROOT
RUNTIME_DIR = get_runtime_dir()

__all__ = [
    "DEFAULT_POB_DIR",
    "DEFAULT_POB_ROOT",
    "DEFAULT_PROJECT_ROOT",
    "FORBIDDEN_METHODS",
    "MAX_UNDO_STEPS",
    "RUNTIME_DIR",
    "BridgeError",
    "BridgeTimeoutError",
    "BridgeWorker",
]


class BridgeWorker:
    """Manages a single pob-zh.exe --engine-headless subprocess with lock, timeout, and stderr drain."""

    _compact_source_label = staticmethod(compact_source_label)

    def __init__(
        self,
        slot_name: str = "main",
        pob_root: Path | None = None,
        pob_dir: Path | None = None,
        *,
        runtime_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.runtime_dir: Path | None = Path(runtime_dir) if runtime_dir is not None else None
        if pob_dir is not None:
            resolved_root = Path(pob_dir)
        elif pob_root is not None:
            resolved_root = Path(pob_root)
        else:
            resolved_root = resolve_engine_root(runtime_dir=self.runtime_dir)
        self.pob_root = resolved_root
        self.exe_path = self.pob_root / "pob-zh.exe"
        self.game_dir = self.pob_root / "PoE1"

        self._lock = threading.RLock()
        self.lock = self._lock
        self._proc: subprocess.Popen[str] | None = None
        self._closing = False
        self._stdout_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=100)
        self._req_counter = 0

        self.pob_version: str | None = None
        self.bridge_version: str | None = None
        self.hello_info: dict[str, Any] = {}
        self.caps: dict[str, Any] = {}
        self.locale: str | None = None
        self.translate_enabled: bool | None = None
        self.last_used: float = time.time()
        self.source_label: str | None = None
        self.last_good_code: str | None = None
        self.rev: int = 0

        # Undo / Redo stacks holding lossless export_code strings (max 30 steps)
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []

        # Cached static tree data per treeVersion
        self._tree_data_cache: dict[str, dict[str, Any]] = {}

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_alive(self) -> bool:
        return self.is_running

    def ensure_alive(self) -> None:
        with self._lock:
            if not self.is_running:
                self.start()

    def start(self, *, restore_code: str | None = None) -> None:
        """Start pob-zh.exe --engine-headless and execute the mandatory handshake (R4, R8)."""
        with self._lock:
            if self.is_running:
                return
            self._closing = False
            self._kill_unlocked()

            if not self.game_dir.is_dir():
                raise BridgeError(f"PoE1 directory not found at {self.game_dir}")

            try:
                cmd, extra_env = build_engine_command(self.pob_root)
            except Exception as exc:
                raise BridgeError(str(exc)) from exc

            out_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
            self._stdout_queue = out_queue
            self._stderr_tail.clear()

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            active_lang = self.locale or current_lang(self.runtime_dir)
            proc_env = os.environ.copy()
            proc_env.update(extra_env)
            proc_env["POB_LANG"] = active_lang
            try:
                proc_env.setdefault(
                    "POB_CACHE_DIR",
                    str(get_shared_cache_dir(self.runtime_dir, pob_root=self.pob_root)),
                )
            except Exception:
                pass
            # Mirror POB_PROXY into HTTPS_PROXY/HTTP_PROXY so Windows pob-zh.exe lcurl honors it.
            pob_proxy = proc_env.get("POB_PROXY", "").strip()
            if pob_proxy:
                proc_env.setdefault("HTTPS_PROXY", pob_proxy)
                proc_env.setdefault("HTTP_PROXY", pob_proxy)
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.game_dir),
                    env=proc_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise BridgeError(f"Failed to launch PoB engine: {exc}") from exc

            try:
                proc = self._proc
                threading.Thread(
                    target=stdout_reader_loop,
                    args=(proc, out_queue),
                    name=f"pob-{self.slot_name}-stdout",
                    daemon=True,
                ).start()
                threading.Thread(
                    target=stderr_drain_loop,
                    args=(proc, self._stderr_tail),
                    name=f"pob-{self.slot_name}-stderr",
                    daemon=True,
                ).start()

                self.hello_info, self.pob_version, self.bridge_version = await_startup_handshake(
                    self._stdout_queue, self._stderr_tail, self._kill_unlocked
                )

                ver_info = self._raw_call_unlocked("version", {}, timeout=10.0)
                self.caps = ver_info.get("caps") or {}
                self.pob_version = ver_info.get("pobVersion") or self.pob_version

                want_translate = active_lang != "en"
                if self.caps.get("localeSwitch") or self.caps.get("zhTranslate") or self.caps.get("setLocale"):
                    self._raw_call_unlocked("set_locale", {"lang": active_lang}, timeout=5.0)
                elif self.caps.get("translateToggle", True) and self.translate_enabled != want_translate:
                    self._raw_call_unlocked("set_translate", {"enabled": want_translate}, timeout=5.0)
                    if self.translate_enabled is None:
                        self.translate_enabled = want_translate

                if restore_code is not None:
                    target_code = restore_code or None
                    if target_code is None:
                        self.last_good_code = None
                else:
                    if not self.last_good_code:
                        self.load_slot_state()
                    target_code = self.last_good_code

                if target_code:
                    stage_missing_tree_versions(target_code, self.game_dir)
                    build_loaded = (
                        ver_info["buildLoaded"]
                        if "buildLoaded" in ver_info
                        else self.hello_info.get("buildLoaded")
                    )
                    restored = restore_build_on_start(
                        self._raw_call_unlocked, target_code, build_loaded
                    )
                    self.last_good_code = target_code if restored else None
                else:
                    self._raw_call_unlocked("new_build", {}, timeout=10.0)

                self.last_used = time.time()
            except BaseException:
                self._kill_unlocked()
                raise

    @property
    def slot_state_path(self) -> Path:
        return get_runtime_dir(self.runtime_dir) / f"slot_{self.slot_name}.json"

    def bump_rev(self) -> int:
        """Increment and return the monotonic build-state revision counter for this slot."""
        with self._lock:
            self.rev = int(self.rev) + 1
            return self.rev

    def save_slot_state(self) -> None:
        """Persist slot build code and undo/redo stacks so daemon restarts never lose state."""
        if self.slot_name == "test":
            return
        self.source_label = compact_source_label(self.source_label)
        write_slot_state(
            self.slot_state_path,
            slot_name=self.slot_name,
            last_good_code=self.last_good_code,
            undo_stack=self.undo_stack,
            redo_stack=self.redo_stack,
            source_label=self.source_label,
            rev=self.rev,
        )

    def load_slot_state(self) -> None:
        """Load persisted slot build code and undo/redo stacks if present."""
        if self.slot_name == "test":
            return
        data = read_slot_state(self.slot_state_path)
        if not data:
            return
        self.last_good_code = data.get("last_good_code")
        self.undo_stack = list(data.get("undo_stack") or [])
        self.redo_stack = list(data.get("redo_stack") or [])
        self.source_label = compact_source_label(data.get("source_label"))
        raw_rev = data.get("rev")
        if isinstance(raw_rev, int) and raw_rev >= 0:
            self.rev = raw_rev

    def _kill_unlocked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3.0)
            except Exception:
                pass
            self._proc = None
        self.translate_enabled = None

    def abort(self) -> None:
        """Non-blocking subprocess termination to immediately unblock any in-flight RPC during shutdown."""
        self._closing = True
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            self._stdout_queue.put_nowait(None)
        except Exception:
            pass

    def stop(self) -> None:
        self._closing = True
        with self._lock:
            self._kill_unlocked()

    def _sync_state_from_rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> None:
        if method == "set_translate":
            if isinstance(result.get("enabled"), bool):
                self.translate_enabled = result["enabled"]
            elif isinstance(result.get("translate"), bool):
                self.translate_enabled = result["translate"]
            elif params is not None and "enabled" in params:
                self.translate_enabled = bool(params["enabled"])
        elif method == "set_locale":
            if isinstance(result.get("lang"), str):
                self.locale = result["lang"]
            if isinstance(result.get("translate"), bool):
                self.translate_enabled = result["translate"]
            elif isinstance(result.get("enabled"), bool):
                self.translate_enabled = result["enabled"]
        elif method == "version":
            if isinstance(result.get("translate"), bool):
                self.translate_enabled = result["translate"]

    def _raw_call_unlocked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        if method in FORBIDDEN_METHODS:
            raise BridgeError(FORBIDDEN_METHODS[method])

        if not self.is_running:
            self.start()

        validate_rpc_method(method, self.caps)

        assert self._proc is not None and self._proc.stdin is not None
        self._req_counter += 1
        req_id = self._req_counter
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}, ensure_ascii=False)

        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._recover_after_crash_unlocked()
            raise BridgeError(f"Failed writing to pob-zh stdin on method '{method}': {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._recover_after_crash_unlocked()
                raise BridgeTimeoutError(f"RPC '{method}' timed out after {timeout:.1f}s; worker restarted and healed.")
            try:
                msg = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                self._recover_after_crash_unlocked()
                raise BridgeTimeoutError(
                    f"RPC '{method}' timed out after {timeout:.1f}s; worker restarted and healed."
                ) from exc

            if msg is None or isinstance(msg, Exception):
                self._recover_after_crash_unlocked()
                raise BridgeError(f"Headless engine crashed during '{method}': {msg}")

            # Ignore asynchronous events or stale response IDs (R8)
            if msg.get("id") != req_id:
                continue

            self.last_used = time.time()
            if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
                raise BridgeError(f"RPC '{method}' failed: {format_rpc_error(msg.get('error'))}")

            result = msg.get("result")
            res_dict = result if isinstance(result, dict) else {"value": result}
            self._sync_state_from_rpc(method, params, res_dict)
            return res_dict

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """Pipeline multiple JSON-RPC calls in a single stdin write+flush under `self._lock`."""
        if not calls:
            return []

        with self._lock:
            for method, _ in calls:
                if method in FORBIDDEN_METHODS:
                    raise BridgeError(FORBIDDEN_METHODS[method])

            if not self.is_running:
                self.start()

            for method, _ in calls:
                validate_rpc_method(method, self.caps)

            assert self._proc is not None and self._proc.stdin is not None

            pre_errors: dict[int, str] = {}
            send_entries: list[tuple[int, int, str, dict[str, Any]]] = []
            lines: list[str] = []
            id_to_index: dict[int, int] = {}

            for idx, (method, raw_params) in enumerate(calls):
                params = raw_params or {}
                if method == "import_code":
                    code_str = str(params.get("code") or "")
                    validate_import_xml_guardrail(code_str)
                    tv_mismatch = self._check_tree_version_mismatch(code_str)
                    if tv_mismatch:
                        err_msg = f"RPC 'import_code' failed: {tv_mismatch}"
                        if raise_on_error:
                            raise BridgeError(err_msg)
                        pre_errors[idx] = err_msg
                        continue
                    stage_missing_tree_versions(code_str, self.game_dir)

                self._req_counter += 1
                req_id = self._req_counter
                id_to_index[req_id] = idx
                send_entries.append((idx, req_id, method, params))
                lines.append(json.dumps({"id": req_id, "method": method, "params": params}, ensure_ascii=False))

            raw_responses: dict[int, dict[str, Any]] = {}
            if lines:
                batch_payload = "\n".join(lines) + "\n"
                try:
                    self._proc.stdin.write(batch_payload)
                    self._proc.stdin.flush()
                except Exception as exc:
                    self._recover_after_crash_unlocked()
                    raise BridgeError(
                        f"Failed writing batch ({len(send_entries)} RPCs) to pob-zh stdin: {exc}"
                    ) from exc

                raw_responses = collect_batch_responses(
                    self._stdout_queue,
                    id_to_index,
                    len(send_entries),
                    timeout,
                    self._recover_after_crash_unlocked,
                )
                self.last_used = time.time()

            results, first_error, had_import_error = decode_batch_responses(
                calls,
                pre_errors,
                send_entries,
                raw_responses,
                check_tv_mismatch=self._check_tree_version_mismatch,
                sync_state=self._sync_state_from_rpc,
            )

            if had_import_error:
                self._restore_after_failed_import_unlocked()

            if raise_on_error and first_error is not None:
                raise BridgeError(first_error)

            return results

    def _recover_after_crash_unlocked(self) -> None:
        """Kill hung/crashed subprocess, restart cleanly, and restore last_good_code if available."""
        self._kill_unlocked()
        if self._closing:
            return
        try:
            self.start()
        except Exception:
            pass

    def _check_tree_version_mismatch(self, code: str) -> str | None:
        return check_tree_version_mismatch(code, self.game_dir, self.pob_version)

    def _restore_after_failed_import_unlocked(self) -> None:
        """Heal the slot after a failed import_code (which may leave b:Shutdown() active / no build loaded)."""
        try:
            # Must call new_build{} first because b:Shutdown() breaks ensure_build() for import_code
            self._raw_call_unlocked("new_build", {}, timeout=10.0)
            if self.last_good_code:
                try:
                    self._raw_call_unlocked(
                        "import_code",
                        {"code": self.last_good_code, "mode": "replace"},
                        timeout=15.0,
                    )
                except Exception:
                    self.last_good_code = None
                    self._raw_call_unlocked("new_build", {}, timeout=10.0)
        except Exception:
            self._recover_after_crash_unlocked()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Thread-safe RPC call with automatic timeout selection, dynamic R2, and import_code deadlock healing."""
        if timeout is None:
            timeout = (
                30.0
                if method
                in {
                    "import_from_url",
                    "fetch_characters",
                    "import_account_character",
                    "import_character_json",
                    "node_power",
                    "gem_search",
                }
                else 15.0
            )
        with self._lock:
            if method == "import_code":
                code = str((params or {}).get("code") or "")
                validate_import_xml_guardrail(code)
                tv_mismatch = self._check_tree_version_mismatch(code)
                if tv_mismatch:
                    raise BridgeError(f"RPC 'import_code' failed: {tv_mismatch}")
                stage_missing_tree_versions(code, self.game_dir)
                try:
                    return self._raw_call_unlocked(method, params, timeout=timeout)
                except BridgeError as exc:
                    self._restore_after_failed_import_unlocked()
                    tv_hint = self._check_tree_version_mismatch(code)
                    if tv_hint and tv_hint not in str(exc):
                        raise BridgeError(f"{exc} | {tv_hint}") from exc
                    raise
            return self._raw_call_unlocked(method, params, timeout=timeout)

    def set_locale(self, lang: str) -> dict[str, Any]:
        """Switch active display locale (`'zh-CN'`, `'zh-TW'`, or `'en'`) on this worker."""
        norm = normalize_lang(lang)
        with self._lock:
            return apply_worker_locale(self, norm)

    def update_checkpoint(self) -> str:
        """Export current build code into last_good_code for crash recovery."""
        with self._lock:
            res = self._raw_call_unlocked("export_code", {}, timeout=10.0)
            code = res.get("code", "")
            if code:
                self.last_good_code = code
            return code

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        """Fetch and cache static tree_data for the active tree version."""
        with self._lock:
            if not tree_version:
                info = self._raw_call_unlocked("get_build_info", {}, timeout=10.0)
                tree_version = info.get("treeVersion") or "latest"
            if tree_version not in self._tree_data_cache:
                params = {"version": tree_version} if tree_version != "latest" else {}
                self._tree_data_cache[tree_version] = self._raw_call_unlocked("tree_data", params, timeout=20.0)
            return self._tree_data_cache[tree_version]
