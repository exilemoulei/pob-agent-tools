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

from .bridge_boot import engine_env, put_code_aside, restore_on_boot
from .bridge_protocol import (
    FORBIDDEN_METHODS,
    MAX_UNDO_STEPS,
    BridgeError,
    BridgeRejectedError,
    BridgeTimeoutError,
    apply_worker_locale,
    await_startup_handshake,
    check_tree_version_mismatch,
    collect_batch_responses,
    decode_batch_responses,
    rpc_rejection,
    stderr_drain_loop,
    stdout_reader_loop,
    sync_worker_state,
    validate_import_xml_guardrail,
    validate_rpc_method,
)
from .bridge_trees import rpc_tree_stage_error, stage_tree_versions_hint
from .locale_cfg import current_lang, normalize_lang
from .os_util import win_hidden_subprocess_kwargs
from .updater import (
    build_engine_command,
    detect_source_checkout_root,
    get_runtime_dir,
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
# Fresh starts one hang or crash may cost before the worker is left stopped.
_RECOVERY_TRIES = 2

__all__ = [
    "DEFAULT_POB_DIR",
    "DEFAULT_POB_ROOT",
    "DEFAULT_PROJECT_ROOT",
    "FORBIDDEN_METHODS",
    "MAX_UNDO_STEPS",
    "RUNTIME_DIR",
    "BridgeError",
    "BridgeRejectedError",
    "BridgeTimeoutError",
    "BridgeWorker",
]


class BridgeWorker:
    """Manages a single pob-zh.exe --engine-headless subprocess with lock, timeout, and stderr drain."""

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
        # The build a (re)start restores: the slot's committed build, kept equal by its SlotHistory.
        self.last_good_code: str | None = None
        # Bumped on every engine launch, so a heal step can tell that a restart already restored the build.
        self.generation: int = 0
        self.boot_build_loaded: Any = None
        # Set when the saved build could not be restored at boot (see bridge_boot.put_code_aside).
        self.restore_failed: dict[str, Any] | None = None
        self._recovering = False

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
        """Launch the engine, run the handshake (R4, R8) and restore the slot's build.

        Never restarts itself: a restore that hangs or crashes the engine boots it empty instead
        (`bridge_boot.restore_on_boot`), so one `start()` launches the engine at most twice.
        """
        with self._lock:
            if self.is_running:
                return
            self._closing = False
            if restore_code is not None:
                target_code = restore_code or None
                if target_code is None:
                    self.last_good_code = None
            else:
                target_code = self.last_good_code
            self._launch_unlocked()
            try:
                restore_on_boot(self, target_code, from_slot_file=restore_code is None)
                self.last_used = time.time()
            except BaseException:
                self._kill_unlocked()
                raise

    def _launch_unlocked(self) -> None:
        """Start the engine process, handshake and apply the locale; every call here skips recovery."""
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

        active_lang = self.locale or current_lang(self.runtime_dir)
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.game_dir),
                env=engine_env(self, extra_env, active_lang),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **win_hidden_subprocess_kwargs(subprocess),
            )
        except OSError as exc:
            raise BridgeError(f"Failed to launch PoB engine: {exc}") from exc
        self.generation += 1

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

            ver_info = self._raw_call_unlocked("version", {}, timeout=10.0, recover=False)
            self.caps = ver_info.get("caps") or {}
            self.pob_version = ver_info.get("pobVersion") or self.pob_version
            self.boot_build_loaded = (
                ver_info["buildLoaded"] if "buildLoaded" in ver_info else self.hello_info.get("buildLoaded")
            )

            want_translate = active_lang != "en"
            if self.caps.get("localeSwitch") or self.caps.get("zhTranslate") or self.caps.get("setLocale"):
                self._raw_call_unlocked("set_locale", {"lang": active_lang}, timeout=5.0, recover=False)
            elif self.caps.get("translateToggle", True) and self.translate_enabled != want_translate:
                self._raw_call_unlocked("set_translate", {"enabled": want_translate}, timeout=5.0, recover=False)
                if self.translate_enabled is None:
                    self.translate_enabled = want_translate
        except BaseException:
            self._kill_unlocked()
            raise

    @property
    def slot_state_path(self) -> Path:
        return get_runtime_dir(self.runtime_dir) / f"slot_{self.slot_name}.json"

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
        sync_worker_state(self, method, params, result)

    def _raw_call_unlocked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
        *,
        recover: bool = True,
    ) -> dict[str, Any]:
        """One RPC. A hang, crash or failed write restarts the worker unless `recover` is False.

        Boot and heal steps pass `recover=False`: they run inside a (re)start already, and letting
        them restart the worker again is what used to nest boots until the recursion limit.
        """
        if method in FORBIDDEN_METHODS:
            raise BridgeError(FORBIDDEN_METHODS[method])

        if not self.is_running:
            if not recover:
                raise BridgeError(f"Headless engine is not running for '{method}'.")
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
            self._after_transport_failure_unlocked(recover)
            raise BridgeError(f"Failed writing to pob-zh stdin on method '{method}': {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                msg = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                self._after_transport_failure_unlocked(recover)
                raise BridgeTimeoutError(
                    f"RPC '{method}' timed out after {timeout:.1f}s; "
                    + ("worker restarted and healed." if recover else "engine stopped.")
                ) from exc

            if msg is None or isinstance(msg, Exception):
                self._after_transport_failure_unlocked(recover)
                raise BridgeError(f"Headless engine crashed during '{method}': {msg}")

            # Ignore asynchronous events or stale response IDs (R8)
            if msg.get("id") != req_id:
                continue

            self.last_used = time.time()
            if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
                raise rpc_rejection(method, msg.get("error"))

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

            pre_errors: dict[int, BridgeError] = {}
            send_entries: list[tuple[int, int, str, dict[str, Any]]] = []
            lines: list[str] = []
            id_to_index: dict[int, int] = {}

            for idx, (method, raw_params) in enumerate(calls):
                params = raw_params or {}
                pre_error = self._rpc_precheck(method, params)
                if pre_error is not None:
                    if raise_on_error:
                        raise pre_error
                    pre_errors[idx] = pre_error
                    continue

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
                raise first_error

            return results

    def _after_transport_failure_unlocked(self, recover: bool) -> None:
        if recover:
            self._recover_after_crash_unlocked()
        else:
            self._kill_unlocked()

    def _recover_after_crash_unlocked(self) -> None:
        """The one place that restarts a hung or crashed worker: a bounded number of fresh starts.

        `start()` never recovers on its own and restores `last_good_code` (or boots empty when that
        restore fails), so each try launches at most twice and nothing nests. After the last try
        the worker stays stopped; the next call starts it again.
        """
        self._kill_unlocked()
        if self._closing or self._recovering:
            return
        self._recovering = True
        try:
            for _ in range(_RECOVERY_TRIES):
                if self._closing:
                    return
                try:
                    self.start()
                    return
                except Exception:
                    self._kill_unlocked()
        finally:
            self._recovering = False

    def _check_tree_version_mismatch(self, code: str) -> str | None:
        return check_tree_version_mismatch(code, self.game_dir, self.pob_version)

    def _import_precheck(self, code: str) -> BridgeError | None:
        """Why `code` cannot be imported before the engine sees it, or None."""
        validate_import_xml_guardrail(code)
        mismatch = self._check_tree_version_mismatch(code)
        if mismatch:  # the user's build needs a newer tree than this engine has
            return BridgeRejectedError(f"RPC 'import_code' failed: {mismatch}", kind="request")
        staging = stage_tree_versions_hint(code, self.game_dir)  # offline or a failed download: a fault
        return BridgeError(f"RPC 'import_code' failed: {staging}") if staging else None

    def _rpc_precheck(self, method: str, params: dict[str, Any] | None) -> BridgeError | None:
        if method == "import_code":
            return self._import_precheck(str((params or {}).get("code") or ""))
        return rpc_tree_stage_error(method, params, self.game_dir)

    def _restore_after_failed_import_unlocked(self) -> None:
        """Heal the slot after a failed import_code (which may leave b:Shutdown() active / no build loaded)."""
        try:
            # Must call new_build{} first because b:Shutdown() breaks ensure_build() for import_code
            self._raw_call_unlocked("new_build", {}, timeout=10.0, recover=False)
            if self.last_good_code:
                try:
                    self._raw_call_unlocked(
                        "import_code",
                        {"code": self.last_good_code, "mode": "replace"},
                        timeout=15.0,
                        recover=False,
                    )
                except BridgeRejectedError as exc:
                    put_code_aside(self, self.last_good_code, str(exc))
                    self._raw_call_unlocked("new_build", {}, timeout=10.0, recover=False)
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
                pre_error = self._import_precheck(code)
                if pre_error is not None:
                    raise pre_error
                self.ensure_alive()  # so only a restart the import itself causes changes generation
                generation = self.generation
                try:
                    return self._raw_call_unlocked(method, params, timeout=timeout)
                except BridgeError as exc:
                    if self.generation == generation:  # else a restart already restored last_good_code
                        self._restore_after_failed_import_unlocked()
                    tv_hint = self._check_tree_version_mismatch(code)
                    if tv_hint and tv_hint not in str(exc):
                        exc.args = (f"{exc} | {tv_hint}",)
                    raise
            if (pre_error := self._rpc_precheck(method, params)) is not None:
                raise pre_error
            return self._raw_call_unlocked(method, params, timeout=timeout)

    def set_locale(self, lang: str) -> dict[str, Any]:
        """Switch active display locale (`'zh-CN'`, `'zh-TW'`, or `'en'`) on this worker."""
        norm = normalize_lang(lang)
        with self._lock:
            return apply_worker_locale(self, norm)

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        """Fetch and cache static tree_data for the active tree version."""
        with self._lock:
            if not tree_version:
                info = self._raw_call_unlocked("get_build_info", {}, timeout=10.0)
                tree_version = info.get("treeVersion") or "latest"
            if tree_version not in self._tree_data_cache:
                params = {"version": tree_version} if tree_version != "latest" else {}
                if (pre_error := self._rpc_precheck("tree_data", params)) is not None:
                    raise pre_error
                self._tree_data_cache[tree_version] = self._raw_call_unlocked("tree_data", params, timeout=20.0)
            return self._tree_data_cache[tree_version]
