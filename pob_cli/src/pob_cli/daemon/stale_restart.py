"""Stale-daemon upgrade helpers: disk-first slot snapshot, recovery backup, and validated replay."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..bridge_protocol import read_slot_state
from ..errors import DaemonRequestError
from ..json_io import read_json_dict
from .runtime import _daemon_state_file, write_private_json


def _has_valid_persisted_slot(rt_dir: Path, slot_name: str, slots: dict[str, Any] | None) -> bool:
    disk_slot = read_slot_state(rt_dir / f"slot_{slot_name}.json")
    s_info = slots.get(slot_name) if isinstance(slots, dict) else None
    has_warn = isinstance(s_info, dict) and bool(s_info.get("persistWarning"))
    return bool(isinstance(disk_slot, dict) and str(disk_slot.get("last_good_code") or "").strip() and not has_warn)


def restart_stale_daemon_for_new_action(
    rt_dir: Path,
    port: int,
    token: str,
    action: str,
    *,
    old_pid: int | None = None,
) -> dict[str, str] | None:
    """Snapshot unpersisted slots, persist backup, and cleanly stop a stale daemon for a registered action."""
    import pob_cli.daemon as _daemon_pkg

    from . import client as _client
    from .rpc_handlers import _SLOT_ACTION_HANDLERS

    if action not in _SLOT_ACTION_HANDLERS:
        return None
    _, st_data = _daemon_pkg._http_post_rpc(
        port, token, {"action": "status", "params": {"skip_periodic_check": True}}, timeout=5.0
    )
    st_res = _client._rpc_result(st_data)
    saved: dict[str, str] = {}
    slots = st_res.get("slots") if isinstance(st_res, dict) else None
    if isinstance(slots, dict):
        for s_name, s_info in slots.items():
            if not (isinstance(s_info, dict) and (s_info.get("hasLoadedBuild") or s_info.get("buildLoaded"))):
                continue
            if _has_valid_persisted_slot(rt_dir, str(s_name), slots):
                continue
            if int(s_info.get("undoDepth") or 0) > 0 or int(s_info.get("redoDepth") or 0) > 0:
                raise DaemonRequestError(
                    f"Stale PoB daemon rejected '{action}', and slot '{s_name}' has unpersisted undo/redo history; "
                    "run `pob stop` once to upgrade the resident daemon without losing history."
                )
            _, ex_data = _daemon_pkg._http_post_rpc(
                port, token, {"action": "export", "params": {"target": s_name}}, timeout=10.0
            )
            code = _client._rpc_result(ex_data).get("code")
            if not isinstance(code, str) or not code.strip():
                raise RuntimeError(
                    f"Failed to export slot '{s_name}' from stale PoB daemon before restart; run `pob stop` and retry."
                )
            saved[str(s_name)] = code

    cur_state = _daemon_pkg.read_daemon_state(rt_dir)
    if cur_state is None:
        return {}
    if old_pid is not None and cur_state.get("pid") != old_pid:
        cur_pid = cur_state.get("pid")
        is_live_replacement = (
            isinstance(cur_pid, int) and not isinstance(cur_pid, bool) and _client._is_pid_alive(cur_pid)
        ) or (_client._probe_daemon(cur_state, timeout=5.0) is not None)
        if not is_live_replacement:
            _daemon_state_file(rt_dir).unlink(missing_ok=True)
        return {}

    raw_prior = read_json_dict(rt_dir / "recovery_slots.json")
    slots_map = slots if isinstance(slots, dict) else None
    prior = (
        {
            k: v
            for k, v in raw_prior.items()
            if isinstance(k, str)
            and isinstance(v, str)
            and v.strip()
            and not _has_valid_persisted_slot(rt_dir, k, slots_map)
        }
        if isinstance(raw_prior, dict)
        else {}
    )
    merged_saved = {**prior, **saved}
    if merged_saved:
        # Persist exported unpersisted builds BEFORE stopping the old daemon so a failed restart never loses them.
        write_private_json(rt_dir / "recovery_slots.json", merged_saved)

    stop_res = _daemon_pkg.stop_daemon(rt_dir)
    if isinstance(old_pid, int) and not isinstance(old_pid, bool):
        if not stop_res.get("stopped") and _client._is_pid_alive(old_pid):
            raise RuntimeError(f"Failed to stop stale PoB daemon (pid {old_pid}) cleanly; run `pob stop` and retry.")
        deadline = time.monotonic() + 5.0
        while _client._is_pid_alive(old_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _client._is_pid_alive(old_pid):
            raise RuntimeError(f"Old PoB daemon (pid {old_pid}) did not exit within 5s; run `pob stop` and retry.")
        after_st = _daemon_pkg.read_daemon_state(rt_dir)
        if after_st is not None and after_st.get("pid") == old_pid:
            _daemon_state_file(rt_dir).unlink(missing_ok=True)
    return merged_saved


def replay_saved_slots(rt_dir: Path, port: int, token: str, saved_slots: dict[str, str]) -> None:
    """Replay exported unpersisted slots into the fresh daemon, preserving `recovery_slots.json` on failure."""
    import pob_cli.daemon as _daemon_pkg

    from . import client as _client

    recovery_path = rt_dir / "recovery_slots.json"
    if not recovery_path.exists():
        write_private_json(recovery_path, saved_slots)
    _, st_data = _daemon_pkg._http_post_rpc(
        port, token, {"action": "status", "params": {"skip_periodic_check": True}}, timeout=5.0
    )
    st_res = _client._rpc_result(st_data)
    new_slots = st_res.get("slots") if isinstance(st_res, dict) else None
    remaining = dict(saved_slots)
    for s_name, code in saved_slots.items():
        cur = new_slots.get(s_name) if isinstance(new_slots, dict) else None
        if isinstance(cur, dict) and (cur.get("hasLoadedBuild") or int(cur.get("rev") or 0) > 0):
            write_private_json(recovery_path, remaining)
            raise RuntimeError(
                f"Slot '{s_name}' was concurrently populated during daemon restart; exported backup preserved at {recovery_path}."
            )
        try:
            _, ld_data = _daemon_pkg._http_post_rpc(
                port,
                token,
                {"action": "load", "params": {"target": s_name, "source": code, "skip_periodic_check": True}},
                timeout=60.0,
            )
            _client._rpc_result(ld_data)
        except Exception as exc:
            write_private_json(recovery_path, remaining)
            raise RuntimeError(
                f"Failed to replay slot '{s_name}' after daemon restart ({exc}); exported backup preserved at {recovery_path}."
            ) from exc
        remaining.pop(s_name, None)
    recovery_path.unlink(missing_ok=True)
