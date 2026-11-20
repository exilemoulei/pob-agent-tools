"""Local HTTP daemon managing persistent `main` and lazy `ref` headless PoB workers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from .analyzer import (
    audit_build,
    extract_summary,
    inspect_config,
    inspect_items,
    inspect_skills,
    inspect_tree,
    search_calcs_breakdown,
)
from .bridge_client import (
    DEFAULT_POB_DIR,
    DEFAULT_PROJECT_ROOT,
    RUNTIME_DIR,
    BridgeError,
    BridgeWorker,
)
from .comparator import borrow_from_ref, compare_builds, rank_gems, rank_nodes, run_matrix
from .errors import DaemonRequestError
from .game_data import ensure_gamedata_index, query_local_data
from .importers import load_build_into_worker, share_code_to_xml
from .mutators import (
    add_skill_gem,
    anoint_item,
    change_tree,
    edit_item_mods,
    modify_config,
    modify_skill,
    remove_skill_gem,
    run_batch,
    search_and_try_item_db,
    set_class_ascendancy,
    set_flask_active,
    switch_main,
    trade_eval_items,
    try_item,
)
from .snapshot import redo_build, undo_build
from .updater import (
    OK_STATES,
    STATE_BOOTSTRAP_MISSING,
    check_and_apply_updates,
    format_stderr_update_alert,
    get_runtime_dir,
    get_update_warning,
    is_valid_engine_dir,
    load_update_state,
    resolve_engine_root,
)

DAEMON_STATE_FILE = RUNTIME_DIR / "daemon.json"
DAEMON_LOCK_FILE = RUNTIME_DIR / "daemon.start.lock"
REF_IDLE_TIMEOUT_SEC = 300.0        # 5 minutes
GLOBAL_IDLE_TIMEOUT_SEC = 1800.0    # 30 minutes
UPDATE_RECHECK_INTERVAL_SEC = 1800.0  # 30 minutes

_stderr_alert_emitted = False


class BootstrapMissingError(RuntimeError):
    """Raised when no local PoB engine exists and GitHub bootstrap failed or is offline."""

    def __init__(self, message: str, update_status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.update_status = update_status or {}


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir)


def _build_daemon_spawn_cmd(runtime_dir: Path | None = None) -> list[str]:
    """Build the command line to spawn the background daemon (supports Python and frozen exe)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_daemon"]
    else:
        cmd = [sys.executable, "-m", "pob_cli.daemon"]
    if runtime_dir is not None:
        cmd.extend(["--runtime-dir", str(runtime_dir)])
    return cmd


def _daemon_state_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.json"


def _daemon_lock_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.start.lock"


def _daemon_boot_error_file(runtime_dir: Path | None = None) -> Path:
    return _get_runtime_dir(runtime_dir) / "daemon.boot_error.json"


def _is_offline_env() -> bool:
    val = os.environ.get("POB_OFFLINE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def reset_stderr_alert_flag() -> None:
    """Reset the per-process stderr update alert deduplication flag (used by tests)."""
    global _stderr_alert_emitted
    _stderr_alert_emitted = False


def emit_stderr_update_alert(state: dict[str, Any] | None, *, force: bool = False) -> bool:
    """Print the prominent single-block Chinese update alert to stderr at most once per CLI process."""
    global _stderr_alert_emitted
    if _stderr_alert_emitted and not force:
        return False
    alert = format_stderr_update_alert(state)
    if not alert:
        return False
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.stderr.write(alert + "\n")
    sys.stderr.flush()
    _stderr_alert_emitted = True
    return True


class DaemonManager:
    """Holds `main` and lazy `ref` BridgeWorkers with idle recycling and auto-update management."""

    def __init__(
        self,
        pob_dir: Path | None = None,
        token: str = "",
        *,
        runtime_dir: Path | None = None,
        offline: bool | None = None,
        proxy: str | None = None,
        mirror: str | None = None,
        skip_update_check: bool = False,
    ) -> None:
        self.runtime_dir = _get_runtime_dir(runtime_dir)
        self._explicit_pob_dir = Path(pob_dir) if pob_dir is not None else None
        self.pob_dir = self._explicit_pob_dir or resolve_engine_root(runtime_dir=self.runtime_dir)
        self.token = token
        self.offline = _is_offline_env() if offline is None else bool(offline)
        self.proxy = proxy
        self.mirror = mirror
        self.skip_update_check = skip_update_check
        self.update_status: dict[str, Any] = load_update_state(self.runtime_dir)
        self.started_at = time.time()
        self.last_active_ts = time.time()
        self._lock = threading.RLock()
        self.main_worker = BridgeWorker(slot_name="main", pob_dir=self.pob_dir)
        self.ref_worker: BridgeWorker | None = None
        self.server: ThreadingHTTPServer | None = None
        self._stopping = False

    def start(self) -> None:
        """Check/apply updates before launching any engine worker, then start main_worker."""
        with self._lock:
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
            fresh_ok = (
                self.update_status.get("state") in OK_STATES
                and (time.time() - checked_at) < UPDATE_RECHECK_INTERVAL_SEC
                and is_valid_engine_dir(self.pob_dir)
                and str(self.update_status.get("engineDir") or "") == str(self.pob_dir)
            )
            if not self.skip_update_check and not fresh_ok:
                self.update_status = check_and_apply_updates(
                    engine_dir=self._explicit_pob_dir,
                    runtime_dir=self.runtime_dir,
                    offline=self.offline,
                    proxy=self.proxy,
                    mirror=self.mirror,
                )

            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)

            if self.update_status.get("state") == STATE_BOOTSTRAP_MISSING and not is_valid_engine_dir(self.pob_dir):
                hint = get_update_warning(self.update_status) or (
                    f"No usable PoB engine found at {self.pob_dir}. "
                    "Configure HTTPS_PROXY / POB_PROXY and run `pob bootstrap`, or use `pob bootstrap --from-dir <path>`."
                )
                raise BootstrapMissingError(hint, update_status=self.update_status)

            if self.main_worker.pob_root != self.pob_dir:
                self.main_worker = BridgeWorker(slot_name="main", pob_dir=self.pob_dir)
            self.main_worker.start()

    def _current_update_warning(self) -> str | None:
        with self._lock:
            return get_update_warning(self.update_status)

    def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
        warn = self._current_update_warning()
        if warn:
            payload["updateWarning"] = warn
        return payload

    def _update_daemon_state_pob_dir(self) -> None:
        state_file = _daemon_state_file(self.runtime_dir)
        if not state_file.is_file():
            return
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pid") == os.getpid():
                data["pob_dir"] = str(self.pob_dir)
                tmp = state_file.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, state_file)
        except Exception:
            pass

    def _hot_reload_update(
        self,
        *,
        proxy: str | None = None,
        mirror: str | None = None,
        force: bool = False,
        offline: bool = False,
    ) -> dict[str, Any]:
        """Atomically snapshot slots, stop workers to unlock Windows DLLs, update engine, and restore slots."""
        with self._lock:
            main_code = self.main_worker.last_good_code
            main_undo = list(self.main_worker.undo_stack)
            main_redo = list(self.main_worker.redo_stack)
            main_src = self.main_worker.source_label
            if self.main_worker.is_alive():
                try:
                    exported = self.main_worker.update_checkpoint()
                    if exported:
                        main_code = exported
                except Exception:
                    pass

            ref_was_alive = self.ref_worker is not None and self.ref_worker.is_alive()
            ref_code = self.ref_worker.last_good_code if self.ref_worker is not None else None
            ref_undo = list(self.ref_worker.undo_stack) if self.ref_worker is not None else []
            ref_redo = list(self.ref_worker.redo_stack) if self.ref_worker is not None else []
            ref_src = self.ref_worker.source_label if self.ref_worker is not None else None
            if ref_was_alive and self.ref_worker is not None:
                try:
                    exported_ref = self.ref_worker.update_checkpoint()
                    if exported_ref:
                        ref_code = exported_ref
                except Exception:
                    pass

            try:
                self.main_worker.stop()
            except Exception:
                pass
            if self.ref_worker is not None:
                try:
                    self.ref_worker.stop()
                except Exception:
                    pass

            self.update_status = check_and_apply_updates(
                engine_dir=self._explicit_pob_dir,
                runtime_dir=self.runtime_dir,
                proxy=proxy if proxy is not None else self.proxy,
                mirror=mirror if mirror is not None else self.mirror,
                offline=offline,
                check_only=False,
                force=force,
            )

            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)
            self._update_daemon_state_pob_dir()

            self.main_worker = BridgeWorker(slot_name="main", pob_dir=self.pob_dir)
            self.main_worker.last_good_code = main_code
            self.main_worker.undo_stack = main_undo
            self.main_worker.redo_stack = main_redo
            self.main_worker.source_label = main_src
            if is_valid_engine_dir(self.pob_dir):
                self.main_worker.start()

            if ref_was_alive or ref_code:
                self.ref_worker = BridgeWorker(slot_name="ref", pob_dir=self.pob_dir)
                self.ref_worker.last_good_code = ref_code
                self.ref_worker.undo_stack = ref_undo
                self.ref_worker.redo_stack = ref_redo
                self.ref_worker.source_label = ref_src
                if ref_was_alive and is_valid_engine_dir(self.pob_dir):
                    self.ref_worker.start()
            else:
                self.ref_worker = None

            return self.update_status

    def _maybe_periodic_update_check(self) -> None:
        """If daemon has been alive > 30m since last update check, check remote and hot-reload if updated."""
        if self.offline or self.skip_update_check:
            return
        checked_at = float(self.update_status.get("checkedAt") or 0.0)
        if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
            return

        with self._lock:
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
            if (time.time() - checked_at) <= UPDATE_RECHECK_INTERVAL_SEC:
                return
            probe_state = check_and_apply_updates(
                engine_dir=self._explicit_pob_dir,
                runtime_dir=self.runtime_dir,
                proxy=self.proxy,
                mirror=self.mirror,
                offline=False,
                check_only=True,
            )
            self.update_status = probe_state
            if probe_state.get("updateAvailable"):
                self._hot_reload_update(proxy=self.proxy, mirror=self.mirror, force=False, offline=False)

    def get_worker(self, target: str = "main") -> BridgeWorker:
        with self._lock:
            self.last_active_ts = time.time()
            if target == "ref":
                if self.ref_worker is None:
                    self.ref_worker = BridgeWorker(slot_name="ref", pob_dir=self.pob_dir)
                self.ref_worker.ensure_alive()
                return self.ref_worker
            self.main_worker.ensure_alive()
            return self.main_worker

    def check_idle(self) -> bool:
        """Recycle `ref` if idle > 5m; return True if global daemon should shut down (> 30m idle)."""
        now = time.time()
        with self._lock:
            if (
                self.ref_worker is not None
                and self.ref_worker.is_alive()
                and (now - self.ref_worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC
            ):
                try:
                    self.ref_worker.stop()
                except Exception:
                    pass
                self.ref_worker = None
        return (now - self.last_active_ts) > GLOBAL_IDLE_TIMEOUT_SEC

    def shutdown_all(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        with self._lock:
            try:
                self.main_worker.stop()
            except Exception:
                pass
            if self.ref_worker is not None:
                try:
                    self.ref_worker.stop()
                except Exception:
                    pass
                self.ref_worker = None
        state_file = _daemon_state_file(self.runtime_dir)
        try:
            if state_file.exists():
                state_file.unlink()
        except Exception:
            pass
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.last_active_ts = time.time()
        target = str(params.get("target") or "main").lower()

        if action == "stop":
            self.shutdown_all()
            return {"stopped": True}

        if action == "game_data":
            with self._lock:
                pob_dir = self.pob_dir
                rt_dir = self.runtime_dir
            if params.get("rebuild"):
                ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=rt_dir, force_rebuild=True)
            return query_local_data(
                kind=str(params.get("kind") or ""),
                query=str(params.get("query") or ""),
                pob_dir=pob_dir,
                runtime_dir=rt_dir,
                slot=params.get("slot"),
                ilvl=int(params["ilvl"]) if params.get("ilvl") is not None else None,
                limit=int(params.get("limit") or 25),
            )

        with self._lock:
            return self._dispatch_locked(action, params, target)

    def _dispatch_locked(self, action: str, params: dict[str, Any], target: str) -> dict[str, Any]:
        """Execute worker/state-mutating RPC actions under `self._lock` to prevent hot-reload DLL races."""
        if action == "status":
            self._maybe_periodic_update_check()
            main_alive = self.main_worker.is_alive()
            main_info = None
            if main_alive:
                try:
                    main_info = self.main_worker.call("get_build_info", {})
                except Exception:
                    pass
            ref_alive = self.ref_worker is not None and self.ref_worker.is_alive()
            ref_info = None
            if ref_alive and self.ref_worker is not None:
                try:
                    ref_info = self.ref_worker.call("get_build_info", {})
                except Exception:
                    pass
            res: dict[str, Any] = {
                "pid": os.getpid(),
                "uptimeSec": round(time.time() - self.started_at, 1),
                "idleSec": round(time.time() - self.last_active_ts, 1),
                "engineDir": str(self.pob_dir),
                "updateStatus": dict(self.update_status),
                "main": {
                    "alive": main_alive,
                    "hello": self.main_worker.hello_info,
                    "buildInfo": main_info,
                    "undoDepth": len(self.main_worker.undo_stack),
                    "redoDepth": len(self.main_worker.redo_stack),
                    "hasLoadedBuild": bool(self.main_worker.last_good_code),
                },
                "ref": {
                    "alive": ref_alive,
                    "buildInfo": ref_info,
                    "hasLoadedBuild": bool(self.ref_worker and self.ref_worker.last_good_code),
                },
            }
            return self._attach_warning_if_degraded(res)

        if action == "update":
            check_only = bool(params.get("check_only", params.get("checkOnly", False)))
            force = bool(params.get("force", False))
            proxy = params.get("proxy")
            mirror = params.get("mirror")
            offline = bool(params.get("offline", False))
            if check_only or offline:
                self.update_status = check_and_apply_updates(
                    engine_dir=self._explicit_pob_dir,
                    runtime_dir=self.runtime_dir,
                    proxy=proxy if proxy is not None else self.proxy,
                    mirror=mirror if mirror is not None else self.mirror,
                    offline=offline,
                    check_only=check_only,
                    force=force,
                )
                return self._attach_warning_if_degraded(dict(self.update_status))
            updated = self._hot_reload_update(
                proxy=proxy,
                mirror=mirror,
                force=force,
                offline=False,
            )
            return self._attach_warning_if_degraded(dict(updated))

        if action == "load":
            self._maybe_periodic_update_check()
            w = self.get_worker(target)
            try:
                load_res = load_build_into_worker(
                    w,
                    source=params.get("source"),
                    xml_code=params.get("xml_code"),
                )
            except Exception as exc:
                warn = self._current_update_warning()
                if warn:
                    guidance = (
                        f"\n[Engine Update Warning] Local PoB engine could not update due to GitHub network failure: {warn}\n"
                        "Guidance for AI Agent: Tell the user to configure a proxy (HTTPS_PROXY / POB_PROXY) and run "
                        "`pob update --proxy http://127.0.0.1:<port>` (or `pob bootstrap --from-dir <local_dir>`). "
                        "Do NOT assume the build code is corrupted, and NEVER manually parse Build.xml to compute stats (R5)."
                    )
                    if isinstance(exc, BridgeError):
                        raise BridgeError(f"{exc}{guidance}") from exc
                    if isinstance(exc, (DaemonRequestError, ValueError)):
                        raise DaemonRequestError(f"{exc}{guidance}") from exc
                    raise RuntimeError(f"{exc}{guidance}") from exc
                raise
            return self._attach_warning_if_degraded(load_res)

        if action == "export":
            w = self.get_worker(target)
            with w.lock:
                code_res = w.call("export_code", {})
                code_str = code_res.get("code", "")
                xml_str = None
                if params.get("include_xml"):
                    xml_res = w.call("save_xml", {})
                    xml_str = xml_res.get("xml") or share_code_to_xml(code_str)
                return {"code": code_str, "xml": xml_str}

        if action == "switch_main":
            w = self.get_worker(target)
            return switch_main(
                w,
                spec_index=params.get("spec_index"),
                skill_group=params.get("skill_group"),
                main_skill_index=params.get("main_skill_index"),
                skill_part_index=params.get("skill_part_index"),
                item_set_id=params.get("item_set_id"),
                skill_set_id=params.get("skill_set_id"),
                config_set_id=params.get("config_set_id"),
                loadout_index=params.get("loadout_index"),
                apply=bool(params.get("apply", True)),
            )

        if action == "undo":
            return undo_build(self.get_worker(target))

        if action == "redo":
            return redo_build(self.get_worker(target))

        if action == "summary":
            sum_res = extract_summary(self.get_worker(target))
            return self._attach_warning_if_degraded(sum_res)

        if action == "audit":
            return audit_build(self.get_worker(target))

        if action == "items":
            return inspect_items(
                self.get_worker(target),
                slot_name=params.get("slot"),
                raw=bool(params.get("raw", False)),
                all_raw=bool(params.get("allRaw", params.get("all_raw", False))),
            )

        if action == "skills":
            return inspect_skills(
                self.get_worker(target),
                group_selector=params.get("group"),
            )

        if action == "tree":
            return inspect_tree(
                self.get_worker(target),
                search=params.get("search"),
            )

        if action == "calc":
            return search_calcs_breakdown(
                self.get_worker(target),
                keyword=str(params.get("keyword") or ""),
            )

        if action == "item_try":
            return try_item(
                self.get_worker(target),
                raw_text=str(params.get("raw") or ""),
                slot_name=params.get("slot"),
                tooltip_only=bool(params.get("tooltip_only", False)),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_edit":
            raw_reps = params.get("replace_mods") or params.get("replaceMods") or []
            rep_tuples = [(str(p[0]), str(p[1])) for p in raw_reps if isinstance(p, (list, tuple)) and len(p) == 2]
            return edit_item_mods(
                self.get_worker(target),
                slot_name=str(params.get("slot") or params.get("slotName") or ""),
                replace_mods=rep_tuples or None,
                add_mods=params.get("add_mods") or params.get("addMods"),
                remove_mods=params.get("remove_mods") or params.get("removeMods"),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_db":
            idx_val = params.get("index") if params.get("index") is not None else params.get("item_index")
            return search_and_try_item_db(
                self.get_worker(target),
                query=str(params.get("query") or ""),
                db_type=str(params.get("db_type") or params.get("dbType") or "UNIQUE"),
                slot_name=params.get("slot") or params.get("slotName"),
                item_index=int(idx_val) if idx_val is not None else None,
                equip=bool(params.get("equip", False)),
                apply=bool(params.get("apply", False)),
                limit=int(params.get("limit") or 15),
            )

        if action == "item_flask":
            return set_flask_active(
                self.get_worker(target),
                flask_index=params["flask_index"],
                active=bool(params["active"]),
                apply=bool(params.get("apply", False)),
            )

        if action == "item_anoint":
            return anoint_item(
                self.get_worker(target),
                slot_name=str(params.get("slot") or "Amulet"),
                notable_query=str(params.get("notable") or ""),
                anoint_index=int(params.get("index") or 1),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_set":
            return modify_skill(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                gem_selector=params.get("gem"),
                new_gem=params.get("new_gem"),
                level=params.get("level"),
                quality=params.get("quality"),
                enabled=params.get("enabled"),
                count=params.get("count"),
                include_in_full_dps=params.get("include_in_full_dps"),
                group_enabled=params.get("group_enabled"),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_add":
            return add_skill_gem(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                gem_name=str(params.get("gem") or ""),
                level=int(params.get("level") or 20),
                quality=int(params.get("quality") or 20),
                enabled=bool(params.get("enabled", True)),
                apply=bool(params.get("apply", False)),
            )

        if action == "skill_rm":
            return remove_skill_gem(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                gem_selector=params["gem"],
                apply=bool(params.get("apply", False)),
            )

        if action == "tree_change":
            return change_tree(
                self.get_worker(target),
                allocate=params.get("allocate"),
                deallocate=params.get("deallocate"),
                apply=bool(params.get("apply", False)),
            )

        if action == "tree_set_class":
            return set_class_ascendancy(
                self.get_worker(target),
                class_name=params.get("class_name") or params.get("className"),
                ascendancy_name=params.get("ascendancy_name") or params.get("ascendancyName"),
                secondary_ascendancy_name=(
                    params.get("secondary_ascendancy_name") or params.get("secondaryAscendancyName")
                ),
                apply=bool(params.get("apply", False)),
            )

        if action == "config_list":
            return inspect_config(
                self.get_worker(target),
                include_defaults=bool(params.get("includeDefaults", params.get("include_defaults", False))),
            )

        if action == "config_set":
            append_val = (
                params.get("appendCustomMods")
                if params.get("appendCustomMods") is not None
                else params.get("append_custom_mods")
            )
            return modify_config(
                self.get_worker(target),
                assignments=params.get("assignments") or [],
                custom_mods=params.get("custom_mods"),
                append_custom_mods=append_val,
                apply=bool(params.get("apply", False)),
            )

        if action == "batch":
            return run_batch(
                self.get_worker(target),
                ops=params.get("ops") or [],
                apply=bool(params.get("apply", False)),
            )

        if action == "trade_eval_items":
            return trade_eval_items(
                self.get_worker(target),
                slot_name=params.get("slot") or params.get("slotName"),
                items=list(params.get("items") or []),
            )

        if action == "matrix":
            return run_matrix(
                self.get_worker(target),
                scenarios=params.get("scenarios"),
            )

        if action == "rank_nodes":
            return rank_nodes(
                self.get_worker(target),
                stat=str(params.get("stat") or "CombinedDPS"),
                max_dist=int(params.get("max_dist") or 5),
                top=int(params.get("top") or 15),
            )

        if action == "rank_gems":
            return rank_gems(
                self.get_worker(target),
                group_selector=str(params.get("group") or "main"),
                slot_idx=params.get("slot_idx"),
                add_link=bool(params.get("add_link", False)),
                field=str(params.get("field") or "CombinedDPS"),
                top=int(params.get("top") or 15),
            )

        if action == "compare":
            mw = self.get_worker("main")
            rw = self.get_worker("ref")
            return compare_builds(
                mw,
                rw,
                ref_source=params.get("ref_source"),
                ref_xml_code=params.get("ref_xml_code"),
            )

        if action == "borrow":
            mw = self.get_worker("main")
            rw = self.get_worker("ref")
            if not rw.last_good_code:
                raise DaemonRequestError(
                    "No reference build loaded in 'ref' slot. Run `pob load <source> --target ref` or `pob compare <source>` first."
                )
            return borrow_from_ref(
                mw,
                rw,
                item_slots=params.get("item_slots"),
                skill_group=params.get("skill_group"),
                apply=bool(params.get("apply", False)),
            )

        raise ValueError(f"Unknown daemon action '{action}'.")


def make_handler(manager: DaemonManager) -> type[BaseHTTPRequestHandler]:
    class RpcHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress noisy stdout access logs
            pass

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            token = self.headers.get("X-PoB-Token", "")
            if not secrets.compare_digest(token, manager.token):
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing X-PoB-Token"})
                return

            if self.path != "/rpc":
                self._send_json(404, {"ok": False, "error": "Not found"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                req = json.loads(raw_body) if raw_body else {}
                action = req.get("action")
                params = req.get("params") or {}
                result = manager.dispatch(action, params)
                self._send_json(200, {"ok": True, "result": result})
            except (DaemonRequestError, ValueError) as exc:
                self._send_json(
                    400,
                    {"ok": False, "error": str(exc), "type": type(exc).__name__, "errorKind": "request"},
                )
            except BridgeError as exc:
                self._send_json(
                    400,
                    {"ok": False, "error": str(exc), "type": "BridgeError", "errorKind": "daemon"},
                )
            except Exception as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                        "type": exc.__class__.__name__,
                        "errorKind": "daemon",
                        "trace": traceback.format_exc(limit=4),
                    },
                )

    return RpcHandler


def run_daemon_server(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Start the local daemon server, write `<runtime_dir>/daemon.json`, and serve until stopped or idle."""
    rt_dir = _get_runtime_dir(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    state_file = _daemon_state_file(rt_dir)

    try:
        if boot_err_file.exists():
            boot_err_file.unlink()
    except OSError:
        pass

    token = secrets.token_hex(32)
    manager = DaemonManager(
        pob_dir=pob_dir,
        token=token,
        runtime_dir=rt_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
    )
    try:
        manager.start()
    except Exception as exc:
        err_payload = {
            "error": str(exc),
            "type": exc.__class__.__name__,
            "state": (
                exc.update_status.get("state")
                if isinstance(exc, BootstrapMissingError)
                else "boot_error"
            ),
            "updateStatus": exc.update_status if isinstance(exc, BootstrapMissingError) else None,
            "ts": time.time(),
        }
        try:
            tmp_err = boot_err_file.with_suffix(f".{os.getpid()}.tmp")
            tmp_err.write_text(json.dumps(err_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_err, boot_err_file)
        except OSError:
            pass
        raise

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager))
    manager.server = server
    actual_port = server.server_address[1]

    state_payload = {
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "started_at": manager.started_at,
        "pob_dir": str(manager.pob_dir),
    }
    tmp_state_file = state_file.with_suffix(f".{os.getpid()}.tmp")
    tmp_state_file.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    os.replace(tmp_state_file, state_file)

    def _watchdog() -> None:
        while not manager._stopping:
            time.sleep(15.0)
            if manager.check_idle():
                manager.shutdown_all()
                break

    threading.Thread(target=_watchdog, name="pob-daemon-watchdog", daemon=True).start()

    try:
        server.serve_forever()
    finally:
        manager.shutdown_all()


# --- Client helper used by CLI ---


def read_daemon_state(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    state_file = _daemon_state_file(runtime_dir)
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_boot_error(runtime_dir: Path | None = None) -> dict[str, Any] | None:
    err_file = _daemon_boot_error_file(runtime_dir)
    if not err_file.exists():
        return None
    try:
        data = json.loads(err_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _rpc_result(data: dict[str, Any]) -> dict[str, Any]:
    """Preserve the daemon's request/failure classification in the CLI process."""
    if not data.get("ok"):
        message = data.get("error") or "Daemon error"
        if data.get("errorKind") == "request":
            raise DaemonRequestError(message)
        raise RuntimeError(message)
    return data["result"]


def call_daemon(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
    runtime_dir: Path | None = None,
    emit_cold_start_alert: bool = True,
) -> dict[str, Any]:
    """Send an authenticated RPC to the local PoB daemon, starting it automatically if not running."""
    rt_dir = _get_runtime_dir(runtime_dir)
    state_file = _daemon_state_file(rt_dir)
    lock_file = _daemon_lock_file(rt_dir)
    boot_err_file = _daemon_boot_error_file(rt_dir)

    state = read_daemon_state(rt_dir)
    if state is not None:
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                resp = client.post(
                    f"http://127.0.0.1:{state['port']}/rpc",
                    headers={"X-PoB-Token": state["token"]},
                    json={"action": action, "params": params or {}},
                )
                data = resp.json()
                return _rpc_result(data)
        except httpx.RequestError:
            # Stale daemon.json; clean up and fall through to auto_start
            try:
                state_file.unlink()
            except Exception:
                pass
            state = None

    if not auto_start:
        raise RuntimeError("PoB daemon is not running.")

    # Spawn background daemon process serialized via startup lockfile (P1-8)
    rt_dir.mkdir(parents=True, exist_ok=True)
    log_path = rt_dir / "daemon.log"

    acquired_lock = False
    if lock_file.exists():
        try:
            if time.time() - lock_file.stat().st_mtime > 30.0:
                lock_file.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        acquired_lock = True
    except OSError:
        acquired_lock = False

    spawned_proc: subprocess.Popen[Any] | None = None
    ping_status_result: dict[str, Any] | None = None

    try:
        if acquired_lock:
            try:
                if boot_err_file.exists():
                    boot_err_file.unlink()
            except OSError:
                pass

            popen_kwargs: dict[str, Any] = {
                "cwd": str(rt_dir),
                "stdin": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                popen_kwargs["start_new_session"] = True

            cmd = _build_daemon_spawn_cmd(rt_dir if runtime_dir is not None else None)

            with open(log_path, "a", encoding="utf-8") as log_fh:
                spawned_proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=log_fh,
                    **popen_kwargs,
                )

        # Wait up to 60 seconds for daemon.json to appear and respond (fast-failing on boot_error)
        deadline = time.monotonic() + 60.0
        with httpx.Client(timeout=5.0, trust_env=False) as ping_client:
            while time.monotonic() < deadline:
                boot_err = _read_boot_error(rt_dir)
                if boot_err is not None:
                    err_msg = str(boot_err.get("error") or "PoB daemon failed to start.")
                    if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                        raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                    raise RuntimeError(err_msg)

                st = read_daemon_state(rt_dir)
                if st is not None:
                    try:
                        ping = ping_client.post(
                            f"http://127.0.0.1:{st['port']}/rpc",
                            headers={"X-PoB-Token": st["token"]},
                            json={"action": "status", "params": {}},
                        )
                        if ping.status_code == 200:
                            ping_json = ping.json()
                            if ping_json.get("ok"):
                                ping_status_result = ping_json["result"]
                                break
                    except Exception:
                        pass

                if spawned_proc is not None and spawned_proc.poll() is not None:
                    boot_err = _read_boot_error(rt_dir)
                    if boot_err is not None:
                        err_msg = str(boot_err.get("error") or "PoB daemon exited during startup.")
                        if boot_err.get("state") == STATE_BOOTSTRAP_MISSING:
                            raise BootstrapMissingError(err_msg, update_status=boot_err.get("updateStatus"))
                        raise RuntimeError(err_msg)
                    raise RuntimeError(
                        f"PoB daemon exited immediately with code {spawned_proc.returncode}. Check {log_path}."
                    )

                time.sleep(0.05)
            else:
                tail = ""
                try:
                    if log_path.is_file():
                        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if lines:
                            tail = " | log tail: " + " ; ".join(lines[-8:])
                except OSError:
                    pass
                raise RuntimeError(f"Timed out waiting for PoB daemon to start. Check {log_path}.{tail}")
    finally:
        if acquired_lock:
            try:
                lock_file.unlink()
            except OSError:
                pass

    # Emit prominent Chinese stderr warning once on cold start when update state is degraded_*
    if emit_cold_start_alert:
        upd_state = (
            (ping_status_result or {}).get("updateStatus")
            or load_update_state(rt_dir)
        )
        if str((upd_state or {}).get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)

    if action == "status" and ping_status_result is not None:
        return ping_status_result

    st = read_daemon_state(rt_dir)
    if not st:
        raise RuntimeError("PoB daemon state file disappeared after startup.")

    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.post(
            f"http://127.0.0.1:{st['port']}/rpc",
            headers={"X-PoB-Token": st["token"]},
            json={"action": action, "params": params or {}},
        )
        data = resp.json()
        return _rpc_result(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless PoB local HTTP daemon")
    parser.add_argument("--pob-dir", type=Path, default=None)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--mirror", type=str, default=None)
    args = parser.parse_args()
    run_daemon_server(
        pob_dir=args.pob_dir,
        port=args.port,
        runtime_dir=args.runtime_dir,
        offline=True if args.offline else None,
        proxy=args.proxy,
        mirror=args.mirror,
    )


if __name__ == "__main__":
    main()
