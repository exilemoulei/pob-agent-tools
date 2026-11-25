"""Engine update check, atomic hot-reload, and slot status snapshot helpers for `DaemonManager`."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from ..guardrails import get_auth_env_status
from ..locale_cfg import get_active_lang, normalize_lang, set_active_lang
from ..snapshot import history_of
from ..updater import (
    is_valid_engine_dir,
    resolve_engine_root,
)
from .runtime import GAME_ID, UPDATE_RECHECK_INTERVAL_SEC, _daemon_state_file

if TYPE_CHECKING:
    from ..bridge_client import BridgeWorker
    from .slots import DaemonManager


def update_daemon_state_pob_dir(manager: DaemonManager) -> None:
    state_file = _daemon_state_file(manager.runtime_dir)
    if not state_file.is_file():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("pid") == os.getpid():
            data["pob_dir"] = str(manager.pob_dir)
            tmp = state_file.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, state_file)
    except Exception:
        pass


def hot_reload_update(
    manager: DaemonManager,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    force: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    """Stop every slot's worker (unlocking Windows DLLs), update the engine, and restart the slots.

    Each slot keeps its `SlotHistory`, so a new worker comes back on the committed build with the
    same undo/redo stacks and revision; outside a transaction the live build is the committed one.
    """
    import pob_cli.daemon as _daemon_pkg

    with manager._update_check_lock, manager._exclusive_barrier():
        if manager._stopping:
            return manager.update_status
        saved_slots: dict[str, dict[str, Any]] = {}
        for slot_name, worker in list(manager._workers.items()):
            history = history_of(worker)
            manager._histories[slot_name] = history
            saved_slots[slot_name] = {"was_alive": worker.is_alive(), "code": history.committed}
            try:
                worker.stop()
            except Exception:
                pass

        manager.update_status = _daemon_pkg.check_and_apply_updates(
            engine_dir=manager._explicit_pob_dir,
            runtime_dir=manager.runtime_dir,
            proxy=proxy if proxy is not None else manager.proxy,
            mirror=mirror if mirror is not None else manager.mirror,
            offline=offline,
            check_only=False,
            force=force,
        )

        if manager._explicit_pob_dir is None:
            manager.pob_dir = resolve_engine_root(runtime_dir=manager.runtime_dir)
        update_daemon_state_pob_dir(manager)

        engine_valid = is_valid_engine_dir(manager.pob_dir)
        new_workers: dict[str, BridgeWorker] = {}
        for slot_name, snap in saved_slots.items():
            if slot_name != "main" and not (snap["was_alive"] or snap["code"]):
                continue
            w = manager._new_worker(slot_name)
            if engine_valid and (slot_name == "main" or snap["was_alive"]):
                w.start()
            new_workers[slot_name] = w

        if "main" not in new_workers:
            main_w = manager._new_worker("main")
            if engine_valid:
                main_w.start()
            new_workers["main"] = main_w

        manager._workers = new_workers
        return manager.update_status


def run_background_boot_update_check(manager: DaemonManager) -> None:
    """Non-blocking startup check_only update probe so cold start serves immediately."""
    import pob_cli.daemon as _daemon_pkg

    try:
        probe_state: dict[str, Any] | None = None
        with manager._update_check_lock:
            if manager._stopping:
                return
            probe_state = _daemon_pkg.check_and_apply_updates(
                engine_dir=manager._explicit_pob_dir,
                runtime_dir=manager.runtime_dir,
                offline=False,
                proxy=manager.proxy,
                mirror=manager.mirror,
                check_only=True,
            )
            with manager._lock:
                if not manager._stopping:
                    manager.update_status = probe_state
        if probe_state and probe_state.get("updateAvailable") and not manager._stopping:
            hot_reload_update(manager, proxy=manager.proxy, mirror=manager.mirror, force=False, offline=False)
    except Exception:
        pass


def maybe_periodic_update_check(manager: DaemonManager) -> None:
    """If daemon has been alive > 30m since last update check, check remote and hot-reload if updated."""
    import pob_cli.daemon as _daemon_pkg

    if manager.offline or manager.skip_update_check or manager._stopping:
        return
    if manager._boot_update_thread is not None and manager._boot_update_thread.is_alive():
        return
    with manager._lock:
        checked_at = float(manager.update_status.get("checkedAt") or 0.0)
    if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
        return

    with manager._update_check_lock:
        with manager._lock:
            checked_at = float(manager.update_status.get("checkedAt") or 0.0)
        if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
            return
        probe_state = _daemon_pkg.check_and_apply_updates(
            engine_dir=manager._explicit_pob_dir,
            runtime_dir=manager.runtime_dir,
            proxy=manager.proxy,
            mirror=manager.mirror,
            offline=False,
            check_only=True,
        )
        with manager._lock:
            manager.update_status = probe_state
        if probe_state.get("updateAvailable"):
            hot_reload_update(manager, proxy=manager.proxy, mirror=manager.mirror, force=False, offline=False)


def handle_update_action(manager: DaemonManager, params: dict[str, Any]) -> dict[str, Any]:
    import pob_cli.daemon as _daemon_pkg

    check_only = bool(params.get("check_only", params.get("checkOnly", False)))
    force = bool(params.get("force", False))
    proxy = params.get("proxy")
    mirror = params.get("mirror")
    offline = bool(params.get("offline", False))
    if check_only or offline:
        with manager._update_check_lock:
            new_state = _daemon_pkg.check_and_apply_updates(
                engine_dir=manager._explicit_pob_dir,
                runtime_dir=manager.runtime_dir,
                proxy=proxy if proxy is not None else manager.proxy,
                mirror=mirror if mirror is not None else manager.mirror,
                offline=offline,
                check_only=check_only,
                force=force,
            )
            with manager._lock:
                manager.update_status = new_state
        return manager._attach_warning_if_degraded(dict(new_state))
    updated = hot_reload_update(manager, proxy=proxy, mirror=mirror, force=force, offline=False)
    return manager._attach_warning_if_degraded(dict(updated))


def _available_locales(pob_dir: Any) -> list[str]:
    avail: list[str] = []
    try:
        from pathlib import Path

        from ..game_data.paths import list_available_zh_dirs

        names = {d.name for d in list_available_zh_dirs(Path(pob_dir))} if pob_dir else set()
        avail = [lang for lang, sub in (("zh-CN", "zh-rCN"), ("zh-TW", "zh-rTW")) if sub in names]
    except Exception:
        pass
    return [*avail, "en"]


def handle_set_lang_action(manager: DaemonManager, params: dict[str, Any]) -> dict[str, Any]:
    rt_dir = getattr(manager, "runtime_dir", None)
    raw_lang = params.get("lang")
    persisted_info: dict[str, Any] | None = None
    env_overridden: str | None = None
    updated_slots: dict[str, Any] = {}
    barrier_fn = getattr(manager, "_exclusive_barrier", manager._reader_barrier)

    with barrier_fn():
        if raw_lang is not None and str(raw_lang).strip():
            norm = normalize_lang(str(raw_lang))
            if params.get("persist", True):
                persisted_info = set_active_lang(norm, runtime_dir=rt_dir)
            target_lang = norm
            # POB_LANG copied into the daemon at start would otherwise outrank this explicit
            # switch forever: status would keep reporting it and new slots would start in it.
            daemon_env = os.environ.get("POB_LANG", "").strip()
            if daemon_env and daemon_env != norm:
                env_overridden = daemon_env
                os.environ["POB_LANG"] = norm
            with manager._lock:
                workers_snap = list(manager._workers.items())
            for sname, w in workers_snap:
                slock = manager._get_slot_lock(sname)
                with slock:
                    set_fn = getattr(w, "set_locale", None)
                    if callable(set_fn):
                        try:
                            updated_slots[sname] = set_fn(target_lang)
                        except Exception as exc:
                            updated_slots[sname] = {"ok": False, "error": str(exc)}
        else:
            target_lang, _ = get_active_lang(rt_dir)
        eff_lang, eff_src = get_active_lang(rt_dir)
    out: dict[str, Any] = {
        "lang": eff_lang if raw_lang is None else target_lang,
        "effectiveLang": eff_lang,
        "source": eff_src,
        "availableLocales": _available_locales(getattr(manager, "pob_dir", None)),
        "slotsUpdated": updated_slots,
    }
    if persisted_info:
        out["persistedLang"] = persisted_info["lang"]
        out["configPath"] = persisted_info["configPath"]
    if env_overridden:
        out["daemonEnvOverridden"] = env_overridden
    return out


def build_status_payload(manager: DaemonManager) -> dict[str, Any]:
    # Never wait for the boot update check (it can take a network timeout): report it as running.
    boot_t = getattr(manager, "_boot_update_thread", None)
    boot_running = boot_t is not None and boot_t.is_alive()
    with manager._reader_barrier():
        with manager._lock:
            workers_snap = dict(manager._workers)
            pob_dir_val = getattr(manager, "pob_dir", None)
            pob_dir_str = str(pob_dir_val)
            upd_copy = dict(manager.update_status)
        if not upd_copy and boot_running:
            upd_copy = {"state": "checking"}
        slots_status = {sname: manager._slot_status_entry(sname, w) for sname, w in workers_snap.items()}
        main_w = workers_snap.get("main")
        main_caps = dict(getattr(main_w, "caps", None) or {})
        main_entry = slots_status.get("main") or manager._slot_status_entry("main", None)
        if main_caps and isinstance(main_entry, dict):
            main_entry.setdefault("caps", main_caps)
        ref_entry = slots_status.get("ref") or {
            "alive": False,
            "buildInfo": None,
            "hasLoadedBuild": False,
        }
        eff_lang, eff_src = get_active_lang(getattr(manager, "runtime_dir", None))
        res: dict[str, Any] = {
            "game": GAME_ID,
            "pid": os.getpid(),
            "uptimeSec": round(time.time() - manager.started_at, 1),
            "idleSec": round(time.time() - manager.last_active_ts, 1),
            "engineDir": pob_dir_str,
            "lang": eff_lang,
            "langSource": eff_src,
            "availableLocales": _available_locales(pob_dir_val),
            "winLuaHost": bool(main_caps.get("winLuaHost")),
            "caps": main_caps,
            "updateStatus": upd_copy,
            "auth": get_auth_env_status(),
            "main": main_entry,
            "ref": ref_entry,
            "slots": slots_status,
        }
        return manager._attach_warning_if_degraded(res)
