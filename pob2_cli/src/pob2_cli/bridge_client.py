"""Headless PoB2 Slim LuaJIT bridge subprocess wrapper (thread-safe, zero-write, auto-healing)."""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .bridge_boot import engine_env, put_code_aside, restore_on_boot
from .bridge_protocol import (
    FORBIDDEN_METHODS,
    MAX_UNDO_STEPS,
    BridgeError,
    BridgeRejectedError,
    BridgeTimeoutError,
    await_startup_handshake,
    collect_batch_responses,
    decode_share_code_xml,
    format_rpc_error,
    rpc_rejection,
    stderr_drain_loop,
    stdout_reader_loop,
    sync_worker_state,
    validate_poe2_xml_root,
    validate_rpc_method,
)
from .errors import DaemonRequestError
from .game_profile import POE2_CLASS_IDS
from .locale_cfg import current_lang, normalize_lang
from .os_util import win_hidden_subprocess_kwargs
from .updater import (
    build_engine_command,
    get_runtime_dir,
    resolve_engine_root,
    resolve_game_dir,
)

_RECOVERY_TRIES = 2
_VALID_POE2_CLASS_ID_SET: frozenset[int] = frozenset(POE2_CLASS_IDS)
_POE2_CLASS_LABELS = "1=Witch, 2=Ranger, 6=Warrior, 7=Sorceress, 8=Huntress, 9=Mercenary, 10=Monk, 11=Druid"
_SPEC_CLASS_ID_RE = re.compile(r"<\s*Spec\b[^>]*\bclassId\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_LUA_LOC_PREFIX_RE = re.compile(r"(?:[A-Za-z]:)?[^\s:]+?\.lua:\d+:\s*")
_REQUEST_ERROR_MARKERS: tuple[str, ...] = (
    "no node ",
    "not an attribute node",
    "attribute must be",
    "mode must be",
    "no slot ",
    "no flask slot",
    "item does not fit slot",
    "item base not recognised",
    "item text too large",
    "params.",
    "no socket group",
    "no item ",
    "no item set",
    "no skill",
    "no config",
    "unknown power stat",
    "unknown method",
    "path:",
    "not a mastery",
    "no mastery effect",
    "cannot delete",
    "empty code",
    "code too large",
    "not a path of building code",
    "code did not decode",
    "code is not a build file",
    "no build is loaded",
)
_REMEDIATION_HINTS: tuple[tuple[str, str], ...] = (
    ("no node ", "Run `pob2 tree` to inspect valid passive node IDs for this tree version."),
    ("not an attribute node", "Only +5 Attribute travel nodes support `--attribute-node` / `--attribute`."),
    ("no slot ", 'Use a valid PoE2 slot name such as "Weapon 1", "Body Armour", "Helmet", "Ring 1", "Amulet", "Flask 1", or "Charm 1".'),
    ("item does not fit slot", "Ensure the item base type matches the target equipment slot."),
    ("item base not recognised", "Verify the item text uses a valid PoE2 0.5.5 base type."),
    ("no socket group", "Run `pob2 skills` to inspect valid 1-based socket group indices."),
    ("unknown power stat", "Omit `--stat` or choose a valid PoB2 power stat such as `CombinedDPS`, `FullDPS`, or `TotalEHP`."),
    ("unknown method", "Check supported PoB2 bridge RPC methods via `pob2 status`."),
)


def is_debug_enabled() -> bool:
    return os.environ.get("POB2_DEBUG", "").strip().lower() in ("1", "true", "yes")


def sanitize_bridge_error(message: str, *, debug: bool | None = None) -> str:
    """Collapse multi-line Lua stack tracebacks into a clean single-line message unless POB2_DEBUG=1."""
    raw = str(message or "").strip()
    if not raw:
        return "PoB2 engine request failed."
    if is_debug_enabled() if debug is None else debug:
        return raw
    tb_idx = raw.lower().find("stack traceback:")
    head = raw[:tb_idx].strip() if tb_idx >= 0 else raw
    first_line = " ".join(part.strip() for part in head.splitlines() if part.strip()) or "PoB2 engine request failed."
    cleaned = _LUA_LOC_PREFIX_RE.sub("", first_line).strip()
    low = cleaned.lower()
    for marker, hint in _REMEDIATION_HINTS:
        if marker in low and hint not in cleaned:
            cleaned = f"{cleaned} ({hint})"
            break
    return cleaned


def _wrap_rpc_rejection(method: str, err_obj: Any) -> BridgeRejectedError:
    exc = rpc_rejection(method, err_obj)
    raw_msg = str(exc)
    low = raw_msg.lower()
    is_req = exc.kind == "request" or any(m in low for m in _REQUEST_ERROR_MARKERS)
    clean_msg = sanitize_bridge_error(raw_msg)
    return BridgeRejectedError(clean_msg, code=exc.code, kind="request" if is_req else exc.kind)


def validate_poe2_spec_class_ids(code_or_xml: str) -> None:
    """Validate `<Spec classId="...">` against PoE2's 8 valid class IDs `{1,2,6,7,8,9,10,11}` before engine import."""
    xml_text = decode_share_code_xml(code_or_xml)
    if not xml_text or "<Spec" not in xml_text:
        return
    found_ids: list[str] = []
    try:
        root = ET.fromstring(xml_text)
        for spec_el in root.iter("Spec"):
            cid_raw = spec_el.attrib.get("classId")
            if cid_raw is not None and str(cid_raw).strip() != "":
                found_ids.append(str(cid_raw).strip())
    except ET.ParseError:
        found_ids.extend(m.group(1).strip() for m in _SPEC_CLASS_ID_RE.finditer(xml_text) if m.group(1).strip())

    for cid_str in found_ids:
        try:
            cid_val = int(cid_str)
        except ValueError:
            raise DaemonRequestError(
                f"[POE2-INVALID-CLASS-ID] Invalid PoE2 passive tree classId={cid_str!r}; "
                f"valid PoE2 classIds are {_POE2_CLASS_LABELS}."
            ) from None
        if cid_val not in _VALID_POE2_CLASS_ID_SET:
            raise DaemonRequestError(
                f"[POE2-INVALID-CLASS-ID] Invalid PoE2 passive tree classId={cid_val}; "
                f"valid PoE2 classIds are {_POE2_CLASS_LABELS}."
            )


__all__ = [
    "FORBIDDEN_METHODS",
    "MAX_UNDO_STEPS",
    "BridgeError",
    "BridgeRejectedError",
    "BridgeTimeoutError",
    "BridgeWorker",
    "is_debug_enabled",
    "sanitize_bridge_error",
    "validate_poe2_spec_class_ids",
]


class BridgeWorker:
    """Manages one headless PoB2 Slim LuaJIT subprocess with lock, timeout, and #2529/#2582 shields."""

    def __init__(
        self,
        slot_name: str = "main",
        pob_root: Path | None = None,
        pob_dir: Path | None = None,
        *,
        data_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.slot_name = slot_name
        self.runtime_dir: Path | None = Path(runtime_dir) if runtime_dir is not None else None
        self.data_dir: Path | None = Path(data_dir) if data_dir is not None else None
        resolved = Path(pob_dir) if pob_dir is not None else (
            Path(pob_root) if pob_root is not None else resolve_engine_root(runtime_dir=self.runtime_dir)
        )
        self.pob_root = resolved
        self.game_dir = resolve_game_dir(resolved)

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
        self.last_good_code: str | None = None
        self.has_build: bool = False
        self.generation: int = 0
        self.boot_build_loaded: Any = None
        self.restore_failed: dict[str, Any] | None = None
        self._recovering = False
        self._tree_data_cache: dict[str, dict[str, Any]] = {}

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_alive(self) -> bool:
        return self.is_running

    @property
    def slot_state_path(self) -> Path:
        return get_runtime_dir(self.runtime_dir) / f"slot_{self.slot_name}.json"

    def ensure_alive(self) -> None:
        with self._lock:
            if not self.is_running:
                self.start()

    def start(self, *, restore_code: str | None = None) -> None:
        with self._lock:
            if self.is_running:
                return
            self._closing = False
            target_code = (restore_code or None) if restore_code is not None else self.last_good_code
            if restore_code is not None and target_code is None:
                self.last_good_code = None
            self._launch_unlocked()
            try:
                restore_on_boot(self, target_code)
                self.has_build = bool(self.last_good_code)
                self.last_used = time.time()
            except BaseException:
                self._kill_unlocked()
                raise

    def _launch_unlocked(self) -> None:
        self._kill_unlocked()
        try:
            cmd, extra_env = build_engine_command(
                self.pob_root, data_dir=self.data_dir, runtime_dir=self.runtime_dir
            )
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
            raise BridgeError(f"Failed to launch PoB2 engine: {exc}") from exc
        self.generation += 1

        try:
            proc = self._proc
            threading.Thread(target=stdout_reader_loop, args=(proc, out_queue), name=f"pob2-{self.slot_name}-out", daemon=True).start()
            threading.Thread(target=stderr_drain_loop, args=(proc, self._stderr_tail), name=f"pob2-{self.slot_name}-err", daemon=True).start()

            self.hello_info, self.pob_version, self.bridge_version = await_startup_handshake(
                self._stdout_queue, self._stderr_tail, self._kill_unlocked
            )
            ver_info = self._raw_call_unlocked("version", {}, timeout=10.0, recover=False)
            self.caps = ver_info.get("caps") or {}
            self.pob_version = ver_info.get("pobVersion") or self.pob_version
            self.boot_build_loaded = ver_info.get("buildLoaded", self.hello_info.get("buildLoaded"))

            if self.caps.get("localeSwitch"):
                self._raw_call_unlocked("set_locale", {"lang": active_lang}, timeout=5.0, recover=False)
        except BaseException:
            self._kill_unlocked()
            raise

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
        self._closing = True
        if self._proc is not None:
            try:
                self._proc.kill()
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

    def _after_transport_failure_unlocked(self, recover: bool) -> None:
        if recover:
            self._recover_after_crash_unlocked()
        else:
            self._kill_unlocked()

    def _recover_after_crash_unlocked(self) -> None:
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

    def _raw_call_unlocked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
        *,
        recover: bool = True,
    ) -> dict[str, Any]:
        validate_rpc_method(method, self.caps)
        if not self.is_running:
            if not recover:
                raise BridgeError(f"Headless PoB2 engine is not running for '{method}'.")
            self.start()

        assert self._proc is not None and self._proc.stdin is not None
        self._req_counter += 1
        req_id = self._req_counter
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}, ensure_ascii=False)
        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._after_transport_failure_unlocked(recover)
            raise BridgeError(f"Failed writing to PoB2 stdin on '{method}': {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            rem = deadline - time.monotonic()
            try:
                if rem <= 0:
                    raise queue.Empty
                msg = self._stdout_queue.get(timeout=rem)
            except queue.Empty as exc:
                self._after_transport_failure_unlocked(recover)
                raise BridgeTimeoutError(f"PoB2 RPC '{method}' timed out after {timeout:.1f}s") from exc
            if msg is None or isinstance(msg, Exception):
                self._after_transport_failure_unlocked(recover)
                raise BridgeError(f"PoB2 engine crashed during '{method}': {msg}")
            if msg.get("id") != req_id:
                continue
            self.last_used = time.time()
            if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
                raise _wrap_rpc_rejection(method, msg.get("error"))
            result = msg.get("result")
            res_dict = result if isinstance(result, dict) else {"value": result}
            sync_worker_state(self, method, params, res_dict)
            return res_dict

    def _restore_after_failed_import_unlocked(self) -> None:
        try:
            self._raw_call_unlocked("new_build", {}, timeout=10.0, recover=False)
            if self.last_good_code:
                try:
                    self._raw_call_unlocked(
                        "import_code", {"code": self.last_good_code, "mode": "replace"}, timeout=20.0, recover=False
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
        eff_timeout = timeout or (35.0 if method in {"node_power", "gem_search", "import_code"} else 20.0)
        with self._lock:
            if method == "import_code":
                code = str((params or {}).get("code") or "")
                validate_poe2_xml_root(code)
                validate_poe2_spec_class_ids(code)
                self.ensure_alive()
                gen = self.generation
                try:
                    return self._raw_call_unlocked(method, params, timeout=eff_timeout)
                except BridgeError:
                    if self.generation == gen:
                        self._restore_after_failed_import_unlocked()
                    raise
            if method == "load_xml":
                xml_raw = str((params or {}).get("xml") or "")
                validate_poe2_xml_root(xml_raw)
                validate_poe2_spec_class_ids(xml_raw)
            return self._raw_call_unlocked(method, params, timeout=eff_timeout)

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        timeout: float = 35.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        if not calls:
            return []
        with self._lock:
            for method, _ in calls:
                validate_rpc_method(method, self.caps)
            if not self.is_running:
                self.start()
            assert self._proc is not None and self._proc.stdin is not None
            send_entries: list[tuple[int, int, str, dict[str, Any]]] = []
            lines: list[str] = []
            id_to_index: dict[int, int] = {}
            for idx, (method, raw_params) in enumerate(calls):
                params = raw_params or {}
                if method == "import_code":
                    c_str = str(params.get("code") or "")
                    validate_poe2_xml_root(c_str)
                    validate_poe2_spec_class_ids(c_str)
                elif method == "load_xml":
                    x_str = str(params.get("xml") or "")
                    validate_poe2_xml_root(x_str)
                    validate_poe2_spec_class_ids(x_str)
                self._req_counter += 1
                req_id = self._req_counter
                id_to_index[req_id] = idx
                send_entries.append((idx, req_id, method, params))
                lines.append(json.dumps({"id": req_id, "method": method, "params": params}, ensure_ascii=False))
            try:
                self._proc.stdin.write("\n".join(lines) + "\n")
                self._proc.stdin.flush()
            except Exception as exc:
                self._recover_after_crash_unlocked()
                raise BridgeError(f"Failed writing PoB2 batch RPCs: {exc}") from exc
            raw_res = collect_batch_responses(
                self._stdout_queue, id_to_index, len(send_entries), timeout, self._recover_after_crash_unlocked
            )
            self.last_used = time.time()
            results: list[dict[str, Any]] = [{} for _ in range(len(calls))]
            first_error: BridgeError | None = None
            had_import_err = False
            for idx, _, method, params in send_entries:
                msg = raw_res[idx]
                if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
                    exc = _wrap_rpc_rejection(method, msg.get("error"))
                    err_txt = sanitize_bridge_error(format_rpc_error(msg.get("error")))
                    results[idx] = {"ok": False, "error": err_txt, "_error": err_txt, "method": method}
                    if method == "import_code":
                        had_import_err = True
                    if first_error is None:
                        first_error = exc
                else:
                    res = msg.get("result")
                    res_dict = res if isinstance(res, dict) else {"value": res}
                    sync_worker_state(self, method, params, res_dict)
                    results[idx] = res_dict
            if had_import_err:
                self._restore_after_failed_import_unlocked()
            if raise_on_error and first_error is not None:
                raise first_error
            return results

    def set_locale(self, lang: str) -> dict[str, Any]:
        norm = normalize_lang(lang)
        with self._lock:
            if not self.is_running:
                self.locale = norm
                return {"ok": True, "lang": norm, "translate": norm != "en", "started": False}
            res = self._raw_call_unlocked("set_locale", {"lang": norm}, timeout=5.0)
            self.locale = norm
            return res

    def get_engine_diagnostics(self) -> dict[str, Any]:
        """Return #2529 mainOutput shield status and #2582 dropped-item warnings from the Lua engine."""
        return self.call("get_engine_diagnostics", {})
