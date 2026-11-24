"""Headless PoB bridge protocol constants, blocklist (R1/R3/R4/R7/R11), and transport/state helpers."""

from __future__ import annotations

import base64
import collections
import json
import os
import queue
import re
import subprocess
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .json_io import read_json_dict as read_slot_state  # noqa: F401  (re-exported for bridge_client)

MAX_UNDO_STEPS = 30

# Hardcoded red-line RPC blocklist (R1, R3, R4, R7, R11; R2 sidebar_breakdown is checked dynamically via caps)
FORBIDDEN_METHODS: dict[str, str] = {
    "compare_load": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_state": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_select": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_remove": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_set": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_tree": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_items": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_skills": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_config": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "compare_use": "R1: Build.compareTab is unavailable on 2.67.2 master; use dual-slot comparator.",
    "config_mod_search": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "config_mod_add": "R3: configModBrowser is false on 2.67.2; use list_config/set_config/set_custom_mods.",
    "set_pob_options": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "oauth_logout": "R4: Writing Settings.xml at runtime is forbidden across workers.",
    "import_from_uri": "R7: import_from_uri rejects non-ASCII/parentheses; use poe.ninja raw/overview/code + import_code.",
    "save_build_as": "R11: Direct disk writes in engine are restricted to buildPath; use save_xml and write locally in CLI.",
}

_TREE_VERSION_ATTR_RE = re.compile(r'treeVersion=["\']([^"\']+)["\']')
_TREE_VERSION_NUM_RE = re.compile(r"^(\d+)_(\d+)")


class BridgeError(RuntimeError):
    """Raised when the headless PoB bridge returns an error or violates a contract."""


class BridgeTimeoutError(BridgeError):
    """Raised when an RPC call exceeds its deadline and the worker is recycled."""


def _decode_share_code_xml(code: str) -> str | None:
    """Best-effort decode of a PoB share code (or raw XML string) to XML text."""
    text = (code or "").strip()
    if not text:
        return None
    if text.startswith("<"):
        return text
    try:
        cleaned = text.replace("-", "+").replace("_", "/")
        pad = (-len(cleaned)) % 4
        if pad:
            cleaned += "=" * pad
        raw = base64.b64decode(cleaned)
        try:
            xml_bytes = zlib.decompress(raw)
        except zlib.error:
            xml_bytes = zlib.decompress(raw, -15)
        return xml_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_xml_tree_versions(xml_text: str) -> list[str]:
    return _TREE_VERSION_ATTR_RE.findall(xml_text)


def _parse_tree_version_tuple(tv: str) -> tuple[int, int] | None:
    m = _TREE_VERSION_NUM_RE.match(tv.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _get_local_tree_versions(game_dir: Path) -> tuple[set[str], tuple[int, int] | None, str | None]:
    tree_data_dir = Path(game_dir) / "TreeData"
    if not tree_data_dir.is_dir():
        return set(), None, None
    names: set[str] = set()
    max_tuple: tuple[int, int] | None = None
    max_str: str | None = None
    for entry in tree_data_dir.iterdir():
        if not entry.is_dir():
            continue
        names.add(entry.name)
        parsed = _parse_tree_version_tuple(entry.name)
        if parsed is not None:
            if max_tuple is None or parsed > max_tuple:
                max_tuple = parsed
                max_str = f"{parsed[0]}_{parsed[1]}"
    return names, max_tuple, max_str


def check_tree_version_mismatch(code: str, game_dir: Path, pob_version: str | None) -> str | None:
    """Return an actionable mismatch message if code declares a treeVersion newer than local TreeData."""
    xml_text = _decode_share_code_xml(code)
    if not xml_text:
        return None
    requested_versions = _extract_xml_tree_versions(xml_text)
    if not requested_versions:
        return None
    local_names, max_tuple, max_str = _get_local_tree_versions(game_dir)
    if max_tuple is None or not max_str:
        return None
    for tv in requested_versions:
        if tv in local_names:
            continue
        parsed = _parse_tree_version_tuple(tv)
        if parsed is not None and parsed > max_tuple:
            pob_ver = pob_version or "unknown"
            return (
                f"Build requires treeVersion='{tv}', which is newer than local engine max "
                f"TreeData='{max_str}' (PoB {pob_ver}). "
                "Run `pob update` (or set HTTPS_PROXY / POB_PROXY and run "
                "`pob update --proxy http://127.0.0.1:<port>`) to update the local engine."
            )
    return None


def validate_import_xml_guardrail(code: str) -> None:
    """Run reverse PoE2 -> PoE1 XML guardrail on a decoded share code before sending to the engine."""
    xml_text = _decode_share_code_xml(code)
    if xml_text:
        from .guardrails import validate_build_xml

        validate_build_xml(xml_text, expected_game="poe1")


def parse_version_caps(ver_info: Any) -> dict[str, Any]:
    """Extract capability flags from a `version` RPC result (always returns a dict)."""
    if not isinstance(ver_info, dict):
        return {}
    raw = ver_info.get("caps")
    return dict(raw) if isinstance(raw, dict) else {}


def get_worker_caps(worker: Any) -> dict[str, Any]:
    """Return a worker's parsed `version` RPC capability dict (defaulting to {} for FakeWorker)."""
    raw = getattr(worker, "caps", None)
    return dict(raw) if isinstance(raw, dict) else {}


def validate_rpc_method(method: str, caps: dict[str, Any]) -> None:
    """Enforce static R1/R3/R4/R7/R11 blocklist and dynamic R2 sidebarBreakdown capability."""
    if method in FORBIDDEN_METHODS:
        raise BridgeError(FORBIDDEN_METHODS[method])
    if method == "sidebar_breakdown" and not bool(caps.get("sidebarBreakdown")):
        raise BridgeError(
            "R2: sidebarBreakdown is false on this engine version; use get_calcs + calcs_breakdown."
        )


def format_rpc_error(err_obj: Any) -> str:
    if isinstance(err_obj, dict):
        return err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
    return str(err_obj or "unknown bridge error")


def compact_source_label(label: Any) -> str | None:
    if not label:
        return None
    s = str(label).strip()
    if (
        len(s) > 120
        and not s.startswith(("http://", "https://", "<", "@"))
        and "://" not in s
        and "/" not in s
        and "\\" not in s
        and not any(ch.isspace() for ch in s)
    ):
        return f"share_code({len(s)} chars)"
    return s


def write_slot_state(
    state_path: Path,
    *,
    slot_name: str,
    last_good_code: str | None,
    undo_stack: list[str],
    redo_stack: list[str],
    source_label: str | None,
    rev: int,
) -> None:
    """Persist slot build code and undo/redo stacks to disk (best-effort)."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "slot": slot_name,
            "last_good_code": last_good_code,
            "undo_stack": undo_stack[-MAX_UNDO_STEPS:],
            "redo_stack": redo_stack[-MAX_UNDO_STEPS:],
            "source_label": compact_source_label(source_label),
            "rev": int(rev),
            "updated_at": time.time(),
        }
        # Temp file, then rename: a daemon killed mid-write would otherwise leave half a JSON, and
        # the slot would come back empty with its undo history gone.
        tmp_path = state_path.with_suffix(f".{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, state_path)
    except Exception:
        pass


def stdout_reader_loop(
    proc: subprocess.Popen[str],
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
) -> None:
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out_queue.put(obj)
    except Exception as exc:
        out_queue.put(exc)
    finally:
        out_queue.put(None)


def stderr_drain_loop(
    proc: subprocess.Popen[str],
    stderr_tail: collections.deque[str],
) -> None:
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            stripped = line.rstrip("\r\n")
            if stripped:
                stderr_tail.append(stripped)
    except Exception:
        pass


def await_startup_handshake(
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
    stderr_tail: collections.deque[str],
    on_abort: Callable[[], None],
    *,
    timeout: float = 20.0,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Consume headless startup events until both gate_result(ok) and hello are received."""
    gate_seen = False
    hello_seen = False
    hello_info: dict[str, Any] = {}
    pob_version: str | None = None
    bridge_version: str | None = None
    deadline = time.monotonic() + timeout
    while not (gate_seen and hello_seen):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            on_abort()
            raise BridgeTimeoutError(
                f"Timed out waiting for headless startup events (stderr: {list(stderr_tail)[-5:]})"
            )
        try:
            msg = out_queue.get(timeout=remaining)
        except queue.Empty as exc:
            on_abort()
            raise BridgeTimeoutError("Timed out waiting for startup events") from exc

        if msg is None or isinstance(msg, Exception):
            on_abort()
            raise BridgeError(
                f"Subprocess exited during boot: {msg} (stderr: {list(stderr_tail)[-5:]})"
            )

        event = msg.get("event")
        data = msg.get("data") or {}
        if event == "gate_result":
            if not data.get("ok"):
                on_abort()
                raise BridgeError(f"PoB bridge gate_result failed: {data.get('failed')}")
            gate_seen = True
        elif event == "hello":
            hello_info = dict(data)
            pob_version = data.get("pobVersion")
            bridge_version = data.get("bridge")
            hello_seen = True

    return hello_info, pob_version, bridge_version


def restore_build_on_start(
    raw_call: Callable[[str, dict[str, Any], float], dict[str, Any]],
    target_code: str,
    build_loaded: Any,
) -> bool:
    """Restore a saved build code during startup, skipping redundant new_build{} when possible (R4)."""
    if build_loaded is False:
        raw_call("new_build", {}, 10.0)
        try:
            raw_call("import_code", {"code": target_code, "mode": "replace"}, 15.0)
            return True
        except Exception:
            try:
                raw_call("new_build", {}, 10.0)
            except Exception:
                pass
            return False

    try:
        raw_call("import_code", {"code": target_code, "mode": "new"}, 15.0)
        return True
    except Exception as exc:
        if "no build is loaded" in str(exc).lower():
            try:
                raw_call("new_build", {}, 10.0)
                raw_call("import_code", {"code": target_code, "mode": "replace"}, 15.0)
                return True
            except Exception:
                try:
                    raw_call("new_build", {}, 10.0)
                except Exception:
                    pass
                return False
        try:
            raw_call("new_build", {}, 10.0)
        except Exception:
            pass
        return False


def collect_batch_responses(
    out_queue: queue.Queue[dict[str, Any] | Exception | None],
    id_to_index: dict[int, int],
    expected: int,
    timeout: float,
    on_crash: Callable[[], None],
) -> dict[int, dict[str, Any]]:
    """Read `expected` matching responses for a pipelined batch RPC from stdout queue."""
    raw_responses: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    while len(raw_responses) < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            on_crash()
            raise BridgeTimeoutError(
                f"Batch RPC timed out after {timeout:.1f}s "
                f"(received {len(raw_responses)}/{expected} responses); worker restarted and healed."
            )
        try:
            msg = out_queue.get(timeout=remaining)
        except queue.Empty as exc:
            on_crash()
            raise BridgeTimeoutError(
                f"Batch RPC timed out after {timeout:.1f}s "
                f"(received {len(raw_responses)}/{expected} responses); worker restarted and healed."
            ) from exc

        if msg is None or isinstance(msg, Exception):
            on_crash()
            raise BridgeError(
                f"Headless engine crashed during batch RPC "
                f"(received {len(raw_responses)}/{expected} responses): {msg}"
            )

        msg_id = msg.get("id")
        if msg_id not in id_to_index:
            # Ignore asynchronous events or stale response IDs (R8)
            continue
        raw_responses[id_to_index[msg_id]] = msg
    return raw_responses


def decode_batch_responses(
    calls: list[tuple[str, dict[str, Any] | None]],
    pre_errors: dict[int, str],
    send_entries: list[tuple[int, int, str, dict[str, Any]]],
    raw_responses: dict[int, dict[str, Any]],
    *,
    check_tv_mismatch: Callable[[str], str | None],
    sync_state: Callable[[str, dict[str, Any] | None, dict[str, Any]], None],
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Assemble ordered batch RPC results, capturing first error and failed import_code flag."""
    results: list[dict[str, Any]] = [{} for _ in range(len(calls))]
    first_error: str | None = None
    had_import_error = False

    for idx, err_msg in pre_errors.items():
        results[idx] = {"ok": False, "error": err_msg, "_error": err_msg, "method": calls[idx][0]}
        if first_error is None:
            first_error = err_msg

    for idx, _, method, params in send_entries:
        msg = raw_responses[idx]
        if msg.get("ok") is False or ("error" in msg and msg["error"] is not None):
            err_text = format_rpc_error(msg.get("error"))
            full_err = f"RPC '{method}' failed: {err_text}"
            if method == "import_code":
                had_import_error = True
                tv_hint = check_tv_mismatch(str(params.get("code") or ""))
                if tv_hint and tv_hint not in full_err:
                    full_err = f"{full_err} | {tv_hint}"
            results[idx] = {"ok": False, "error": err_text, "_error": err_text, "method": method}
            if first_error is None:
                first_error = full_err
        else:
            result = msg.get("result")
            res_dict = result if isinstance(result, dict) else {"value": result}
            sync_state(method, params, res_dict)
            results[idx] = res_dict

    return results, first_error, had_import_error
