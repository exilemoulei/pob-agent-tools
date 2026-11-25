"""Local version detection, persisted update state, alerts, and engine gate probe."""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .constants import (
    OK_STATES,
    POBTOOLS_META_FILENAME,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_NETWORK,
    STATE_DEGRADED_ROLLBACK,
    STATE_UP_TO_DATE,
    UPDATE_STATE_FILENAME,
)
from .paths import _get_runtime_dir


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def _state_describes_engine(state: dict[str, Any], engine_dir: Path | None) -> bool:
    """False when `state` was recorded for another engine than `engine_dir`.

    The state file is per runtime dir, not per engine: a `POB_ROOT` engine without its own version
    files must not take the managed engine's PobTools tag and data version, or the update check
    reports it up to date and never installs the bridge or the Chinese data there.
    """
    recorded = state.get("engineDir")
    if engine_dir is None or not recorded:
        return True
    try:
        return Path(str(recorded)).resolve() == Path(engine_dir).resolve()
    except OSError:
        return str(recorded) == str(engine_dir)


def detect_local_versions(engine_dir: Path | None, runtime_dir: Path | None = None) -> dict[str, str | None]:
    """Detect local PoB, PobTools-zh, and DataV2 versions from disk metadata + cached state."""
    pob_ver: str | None = None
    pobtools_ver: str | None = None
    pobtools_tag: str | None = None
    data_ver: str | None = None

    if engine_dir is not None:
        eng = Path(engine_dir)
        manifest_path = eng / "PoE1" / "manifest.xml"
        if manifest_path.is_file():
            try:
                import xml.etree.ElementTree as ET

                root = ET.parse(manifest_path).getroot()
                v = root.find("Version")
                if v is not None and v.attrib.get("number"):
                    pob_ver = v.attrib["number"].strip()
            except Exception:
                pass

        trans_path = eng / "Data" / "translations_version.json"
        if trans_path.is_file():
            try:
                tdata = json.loads(trans_path.read_text(encoding="utf-8"))
                if isinstance(tdata, dict) and tdata.get("dataVersion"):
                    data_ver = str(tdata["dataVersion"]).strip()
            except Exception:
                pass

        meta_path = eng / POBTOOLS_META_FILENAME
        if meta_path.is_file():
            try:
                mdata = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(mdata, dict):
                    pobtools_ver = mdata.get("pobToolsVersion") or pobtools_ver
                    pobtools_tag = mdata.get("pobToolsTag") or pobtools_tag
                    data_ver = data_ver or mdata.get("dataVersion")
            except Exception:
                pass

    prev = _pkg().load_update_state(runtime_dir)
    if prev and _state_describes_engine(prev, engine_dir):
        if not pob_ver and prev.get("localPobVersion"):
            pob_ver = str(prev["localPobVersion"])
        if not pobtools_ver and prev.get("pobToolsVersion"):
            pobtools_ver = str(prev["pobToolsVersion"])
        if not pobtools_tag and prev.get("pobToolsTag"):
            pobtools_tag = str(prev["pobToolsTag"])
        if not data_ver and prev.get("dataVersion"):
            data_ver = str(prev["dataVersion"])

    return {
        "localPobVersion": pob_ver,
        "pobToolsVersion": pobtools_ver,
        "pobToolsTag": pobtools_tag,
        "dataVersion": data_ver,
    }


def _write_pobtools_meta(
    engine_dir: Path,
    *,
    pobtools_version: str | None,
    pobtools_tag: str | None,
    data_version: str | None,
) -> None:
    meta_path = Path(engine_dir) / POBTOOLS_META_FILENAME
    payload = {
        "pobToolsVersion": pobtools_version,
        "pobToolsTag": pobtools_tag,
        "dataVersion": data_version,
        "updatedAt": time.time(),
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_update_state_path(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / UPDATE_STATE_FILENAME


def load_update_state(runtime_dir: Path | None = None) -> dict[str, Any]:
    """Load persisted update_state.json from runtime_dir, returning {} if absent/corrupt."""
    path = get_update_state_path(runtime_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_state(state: dict[str, Any], runtime_dir: Path | None = None) -> Path:
    """Atomically persist update_state.json to runtime_dir."""
    path = get_update_state_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A name of its own: the CLI and the daemon both save, and with one shared name the second
    # `os.replace` finds its file already moved (or moves the other writer's half-written one).
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def _build_user_hint(
    state_name: str,
    local_pob_version: str | None = None,
    network_error: str | None = None,
    *,
    rolled_back: bool = True,
) -> str | None:
    if state_name in OK_STATES:
        return None
    ver_str = local_pob_version or "unknown"
    err_part = f"；原因: {network_error}" if network_error else ""
    if state_name == STATE_DEGRADED_NETWORK:
        return (
            f"GitHub 连接失败，PoB 未能检查/更新至最新版（当前本地版本 {ver_str}，"
            f"可能缺失最新赛季天赋树或技能改动{err_part}）。"
            "请检查网络代理（设置环境变量 HTTPS_PROXY / POB_PROXY 或执行 "
            "`pob update --proxy http://127.0.0.1:<port>`），"
            "或使用 `pob bootstrap --from-dir <本地目录>` 离线导入。"
        )
    if state_name == STATE_BOOTSTRAP_MISSING:
        return (
            f"本地未找到可用 PoB 引擎且无法从 GitHub 下载（{network_error or '离线或网络不通'}）。"
            "请配置代理后执行 `pob bootstrap --proxy http://127.0.0.1:<port>`"
            "（或设置 HTTPS_PROXY / POB_PROXY / POB_GITHUB_MIRROR），"
            "或使用 `pob bootstrap --from-dir <本地PobTools目录>` 离线导入。"
        )
    if state_name == STATE_DEGRADED_ROLLBACK and not rolled_back:
        # The swap was refused or could not be undone in full: do not promise a clean old engine.
        return (
            f"PoB 引擎更新未完成，引擎目录未能确认恢复到更新前的状态{err_part}。"
            "请先执行 `pob stop`，再执行 `pob update --force` 重试。"
        )
    if state_name == STATE_DEGRADED_ROLLBACK:
        return (
            f"PoB 引擎更新后冒烟探针 gate_result 未通过{err_part}，"
            f"已自动回滚至原本地版本 {ver_str} 以保持可用。"
            "可稍后执行 `pob update --force` 重试。"
        )
    return f"PoB 更新状态异常 ({state_name}, 本地版本 {ver_str}{err_part})。"


def get_update_warning(state: dict[str, Any] | None) -> str | None:
    """Return structured userHint warning whenever state is not up_to_date/updated."""
    if not state:
        return None
    st = str(state.get("state") or "")
    if not st or st in OK_STATES:
        return None
    hint = state.get("userHint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    return _build_user_hint(
        st,
        local_pob_version=state.get("localPobVersion"),
        network_error=state.get("networkError"),
        rolled_back=bool(state.get("rolledBack")),
    )


def format_stderr_update_alert(state: dict[str, Any] | None) -> str | None:
    """Format a concise, visible single-block Chinese warning for stderr when degraded."""
    warning = get_update_warning(state)
    if not warning or not state:
        return None
    st = state.get("state") or "unknown"
    ver = state.get("localPobVersion") or "unknown"
    return f"[pob 告警 | {st} | 本地 PoB {ver}] {warning}"


def probe_engine_gate(engine_dir: Path, timeout_sec: float = 20.0) -> dict[str, Any]:
    """Run headless engine smoke probe (pob-zh.exe on Windows, LuaJIT runner on macOS/Linux) and verify gate_result + hello."""
    eng = Path(engine_dir)
    game_dir = eng / "PoE1"

    try:
        cmd, extra_env = _pkg().build_engine_command(eng)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "stderrTail": []}

    out_queue: queue.Queue[dict[str, Any] | Exception | None] = queue.Queue()
    stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc_env = os.environ.copy()
    proc_env.update(extra_env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(game_dir),
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

    def _read_stdout() -> None:
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

    def _read_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                s = line.rstrip("\r\n")
                if s:
                    stderr_tail.append(s)
        except Exception:
            pass

    threading.Thread(target=_read_stdout, name="pob-gate-stdout", daemon=True).start()
    threading.Thread(target=_read_stderr, name="pob-gate-stderr", daemon=True).start()

    gate_data: dict[str, Any] | None = None
    hello_data: dict[str, Any] | None = None
    ver_data: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_sec

    try:
        while gate_data is None or hello_data is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }
            try:
                msg = out_queue.get(timeout=remaining)
            except queue.Empty:
                return {
                    "ok": False,
                    "error": f"Timed out after {timeout_sec:.1f}s waiting for gate_result/hello",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }

            if msg is None or isinstance(msg, Exception):
                return {
                    "ok": False,
                    "error": f"Engine exited before startup handshake completed: {msg}",
                    "gate": gate_data,
                    "hello": hello_data,
                    "stderrTail": list(stderr_tail)[-10:],
                }

            event = msg.get("event")
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            if event == "gate_result":
                gate_data = data
                if not data.get("ok"):
                    return {
                        "ok": False,
                        "error": f"gate_result failed: {data.get('failed')}",
                        "failed": data.get("failed"),
                        "gate": gate_data,
                        "hello": hello_data,
                        "stderrTail": list(stderr_tail)[-10:],
                    }
            elif event == "hello":
                hello_data = data

        # Optional quick version{} RPC to capture caps
        remaining = max(0.0, deadline - time.monotonic())
        if proc.stdin is not None and remaining > 0.5:
            try:
                proc.stdin.write(json.dumps({"id": 1, "method": "version", "params": {}}) + "\n")
                proc.stdin.flush()
                ver_deadline = time.monotonic() + min(5.0, remaining)
                while time.monotonic() < ver_deadline:
                    msg = out_queue.get(timeout=max(0.1, ver_deadline - time.monotonic()))
                    if msg is None or isinstance(msg, Exception):
                        break
                    if msg.get("id") == 1 and isinstance(msg.get("result"), dict):
                        ver_data = msg["result"]
                        break
            except Exception:
                pass

        return {
            "ok": True,
            "gate": gate_data,
            "hello": hello_data,
            "pobVersion": ver_data.get("pobVersion") or hello_data.get("pobVersion"),
            "bridgeVersion": hello_data.get("bridge"),
            "caps": ver_data.get("caps") or {},
            "stderrTail": list(stderr_tail)[-10:],
        }
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass


def _save_bootstrap_missing_state(
    eng_dir: Path, rt_dir: Path, err: str, *, hint: str | None = None
) -> dict[str, Any]:
    """Record that no engine is installed. `hint` replaces the default advice, which is about the network."""
    state = {
        "state": STATE_BOOTSTRAP_MISSING,
        "localPobVersion": None,
        "remotePobVersion": None,
        "pobToolsVersion": None,
        "dataVersion": None,
        "checkedAt": time.time(),
        "engineDir": str(eng_dir),
        "networkError": err,
        "userHint": hint or _build_user_hint(STATE_BOOTSTRAP_MISSING, None, err),
    }
    save_update_state(state, rt_dir)
    return state


def _save_degraded_state(
    state_name: str,
    *,
    local_vers: dict[str, str | None],
    remote_pob_ver: str | None,
    prev_state: dict[str, Any],
    engine_dir: Path,
    rt_dir: Path,
    err_msg: str,
    rolled_back: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "state": state_name,
        "localPobVersion": local_vers["localPobVersion"],
        "remotePobVersion": remote_pob_ver,
        "pobToolsVersion": local_vers["pobToolsVersion"],
        "pobToolsTag": local_vers["pobToolsTag"],
        "dataVersion": local_vers["dataVersion"],
        "pobManifestEtag": prev_state.get("pobManifestEtag"),
        "checkedAt": time.time(),
        "engineDir": str(engine_dir),
        "networkError": err_msg,
        "userHint": _build_user_hint(state_name, local_vers["localPobVersion"], err_msg, rolled_back=rolled_back),
    }
    if rolled_back:
        state["rolledBack"] = True
    save_update_state(state, rt_dir)
    return state


def _save_offline_state(
    local_vers: dict[str, str | None], prev_state: dict[str, Any], engine_dir: Path, rt_dir: Path
) -> dict[str, Any]:
    state = {
        "state": STATE_UP_TO_DATE,
        "localPobVersion": local_vers["localPobVersion"],
        "remotePobVersion": prev_state.get("remotePobVersion"),
        "pobToolsVersion": local_vers["pobToolsVersion"],
        "pobToolsTag": local_vers["pobToolsTag"],
        "dataVersion": local_vers["dataVersion"],
        "pobManifestEtag": prev_state.get("pobManifestEtag"),
        "checkedAt": time.time(),
        "engineDir": str(engine_dir),
        "offline": True,
        "networkError": None,
        "userHint": None,
    }
    save_update_state(state, rt_dir)
    return state


def _save_check_result_state(
    *,
    local_vers: dict[str, str | None],
    remote_pob_ver: str | None,
    remote_tools_ver: str,
    remote_tools_tag: str,
    remote_data_tag: str | None,
    pob_etag: str | None,
    active_dir: Path,
    rt_dir: Path,
    update_available: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "state": STATE_UP_TO_DATE,
        "localPobVersion": local_vers["localPobVersion"],
        "remotePobVersion": remote_pob_ver,
        "pobToolsVersion": local_vers["pobToolsVersion"] if update_available else (local_vers["pobToolsVersion"] or remote_tools_ver),
        "pobToolsTag": local_vers["pobToolsTag"] if update_available else (local_vers["pobToolsTag"] or remote_tools_tag),
        "dataVersion": local_vers["dataVersion"] if update_available else (local_vers["dataVersion"] or remote_data_tag),
        "pobManifestEtag": pob_etag,
        "checkedAt": time.time(),
        "engineDir": str(active_dir),
        "updateAvailable": update_available,
        "networkError": None,
        "userHint": None,
    }
    if extra:
        state.update(extra)
    save_update_state(state, rt_dir)
    return state
