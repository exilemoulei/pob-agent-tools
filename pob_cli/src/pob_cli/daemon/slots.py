"""DaemonManager: named PoB worker lifecycle, RW barriers, hot-reload, and dispatch."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..locale_cfg import get_active_lang
from ..snapshot import SlotHistory, history_of, rebuild_if_stale
from ..updater import (
    OK_STATES,
    STATE_BOOTSTRAP_MISSING,
    get_update_warning,
    is_valid_engine_dir,
    load_update_state,
    recover_idle_engine,
    resolve_engine_root,
    unrestored_swap,
)
from .read_cache import READ_CACHE_ACTIONS, SlotReadCache
from .rpc_handlers import dispatch_dual_slot_action, dispatch_game_data, dispatch_slot_action
from .runtime import (
    GAME_ID,
    GLOBAL_IDLE_TIMEOUT_SEC,
    REF_IDLE_TIMEOUT_SEC,
    UPDATE_RECHECK_INTERVAL_SEC,
    BootstrapMissingError,
    _daemon_state_file,
    _get_runtime_dir,
    _is_offline_env,
    normalize_slot_name,
)
from .slot_updates import (
    build_status_payload,
    handle_set_lang_action,
    handle_update_action,
    hot_reload_update,
    maybe_periodic_update_check,
    run_background_boot_update_check,
    slot_status_entry,
    update_daemon_state_pob_dir,
)

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

_STATE_META_ACTIONS = frozenset(
    {
        "load",
        "summary",
        "audit",
        "diagnose",
        "undo",
        "redo",
        "new_build",
        "switch_main",
        "item_try",
        "item_edit",
        "item_unequip",
        "item_db",
        "item_flask",
        "item_anoint",
        "skill_set",
        "skill_add",
        "skill_rm",
        "skill_new_group",
        "skill_replace_group",
        "tree_change",
        "tree_set_class",
        "config_set",
        "batch",
        "sweep",
        "matrix",
    }
)


class DaemonManager:
    """Manages named headless PoB BridgeWorkers with per-slot locks, idle recycling, and hot-reload barriers."""

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
        disable_global_idle: bool = False,
    ) -> None:
        self.runtime_dir = _get_runtime_dir(runtime_dir)
        self._explicit_pob_dir = Path(pob_dir) if pob_dir is not None else None
        self.pob_dir = self._explicit_pob_dir or resolve_engine_root(runtime_dir=self.runtime_dir)
        self.token = token
        self.offline = _is_offline_env() if offline is None else bool(offline)
        self.proxy = proxy
        self.mirror = mirror
        self.skip_update_check = skip_update_check
        self.disable_global_idle = disable_global_idle
        self.update_status: dict[str, Any] = load_update_state(self.runtime_dir)
        self._engine_fault: str | None = None
        self.started_at = time.time()
        self.last_active_ts = time.time()

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._update_check_lock = threading.RLock()
        self._reloading = False
        self._reload_owner: int | None = None
        self._reload_depth = 0
        self._active_ops = 0
        self._reader_depths: dict[int, int] = {}

        # A slot is its worker, its history and its lock; the history outlives the worker, so idle
        # recycling and hot reload start a new worker on the same committed build and revision.
        self._histories: dict[str, SlotHistory] = {}
        self._read_cache = SlotReadCache()
        self._workers: dict[str, BridgeWorker] = {"main": self._new_worker("main")}
        self._slot_locks: dict[str, threading.RLock] = {"main": threading.RLock()}
        self.server: ThreadingHTTPServer | None = None
        self._stopping = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_done = threading.Event()
        self._boot_update_thread: threading.Thread | None = None

    def _new_worker(self, slot_name: str) -> BridgeWorker:
        import pob_cli.daemon as _daemon_pkg

        worker = _daemon_pkg.BridgeWorker(
            slot_name=slot_name,
            pob_dir=self.pob_dir,
            runtime_dir=self.runtime_dir,
        )
        with self._lock:
            history = self._histories.get(slot_name)
            if history is None:
                history = SlotHistory.load(slot_name, self.runtime_dir)
                self._histories[slot_name] = history
        history.bind(worker)
        return worker

    @property
    def main_worker(self) -> BridgeWorker:
        with self._lock:
            return self._workers["main"]

    @main_worker.setter
    def main_worker(self, worker: BridgeWorker) -> None:
        with self._lock:
            self._workers["main"] = worker
        self._read_cache.invalidate_slot("main")

    @property
    def ref_worker(self) -> BridgeWorker | None:
        with self._lock:
            return self._workers.get("ref")

    @ref_worker.setter
    def ref_worker(self, worker: BridgeWorker | None) -> None:
        with self._lock:
            if worker is None:
                self._workers.pop("ref", None)
            else:
                self._workers["ref"] = worker
        self._read_cache.invalidate_slot("ref")

    def _get_slot_lock(self, slot: str) -> threading.RLock:
        with self._lock:
            lock = self._slot_locks.get(slot)
            if lock is None:
                lock = threading.RLock()
                self._slot_locks[slot] = lock
            return lock

    @contextlib.contextmanager
    def _reader_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            if self._reload_owner != cur_tid:
                depth = self._reader_depths.get(cur_tid, 0)
                if depth == 0:
                    while self._reloading and self._reload_owner != cur_tid:
                        self._cond.wait()
                    self._active_ops += 1
                self._reader_depths[cur_tid] = depth + 1
        try:
            yield
        finally:
            with self._cond:
                if self._reload_owner != cur_tid:
                    new_depth = self._reader_depths.get(cur_tid, 1) - 1
                    if new_depth <= 0:
                        self._reader_depths.pop(cur_tid, None)
                        self._active_ops -= 1
                        if self._active_ops == 0:
                            self._cond.notify_all()
                    else:
                        self._reader_depths[cur_tid] = new_depth

    @contextlib.contextmanager
    def _exclusive_barrier(self) -> Iterator[None]:
        cur_tid = threading.get_ident()
        with self._cond:
            while self._reloading and self._reload_owner != cur_tid:
                self._cond.wait()
            if self._reload_owner == cur_tid:
                self._reload_depth += 1
            else:
                self._reloading = True
                self._reload_owner = cur_tid
                self._reload_depth = 1
                while self._active_ops > 0:
                    self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._reload_depth -= 1
                if self._reload_depth == 0:
                    self._reloading = False
                    self._reload_owner = None
                    self._cond.notify_all()

    def _run_background_boot_update_check(self) -> None:
        run_background_boot_update_check(self)

    def start(self) -> None:
        """Start the `main` worker immediately when an engine exists; run boot update check asynchronously."""
        import pob_cli.daemon as _daemon_pkg

        launch_bg_check = False
        with self._exclusive_barrier():
            # An update killed mid-swap leaves a mix of two versions that still looks valid.
            recover_idle_engine(self.pob_dir)
            self._engine_fault = unrestored_swap(self.pob_dir)
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
            engine_valid = is_valid_engine_dir(self.pob_dir)
            fresh_ok = (
                self.update_status.get("state") in OK_STATES
                and not self.update_status.get("updateAvailable")
                and (time.time() - checked_at) < UPDATE_RECHECK_INTERVAL_SEC
                and engine_valid
                and str(self.update_status.get("engineDir") or "") == str(self.pob_dir)
            )

            if not self.skip_update_check and not fresh_ok:
                if not engine_valid or self.offline:
                    self.update_status = _daemon_pkg.check_and_apply_updates(
                        engine_dir=self._explicit_pob_dir,
                        runtime_dir=self.runtime_dir,
                        offline=self.offline,
                        proxy=self.proxy,
                        mirror=self.mirror,
                    )
                else:
                    launch_bg_check = True

            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)

            if self.update_status.get("state") == STATE_BOOTSTRAP_MISSING and not is_valid_engine_dir(self.pob_dir):
                hint = get_update_warning(self.update_status) or (
                    f"No usable PoB engine found at {self.pob_dir}. "
                    "Configure HTTPS_PROXY / POB_PROXY and run `pob bootstrap`, or use `pob bootstrap --from-dir <path>`."
                )
                raise BootstrapMissingError(hint, update_status=self.update_status)

            if launch_bg_check:
                t = threading.Thread(
                    target=self._run_background_boot_update_check,
                    name="pob-boot-update-check",
                    daemon=True,
                )
                self._boot_update_thread = t
                t.start()

            main_w = self._workers["main"]
            if main_w.pob_root != self.pob_dir:
                main_w = self._new_worker("main")
                self._workers["main"] = main_w
            main_w.start()

    def _current_update_warning(self) -> str | None:
        with self._lock:
            # Results from a half-updated engine are not to be trusted, whatever the update state says.
            if self._engine_fault and not unrestored_swap(self.pob_dir):
                self._engine_fault = None
            return self._engine_fault or get_update_warning(self.update_status)

    def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
        warn = self._current_update_warning()
        if warn:
            payload["updateWarning"] = warn
        return payload

    def _update_daemon_state_pob_dir(self) -> None:
        update_daemon_state_pob_dir(self)

    def _hot_reload_update(
        self,
        *,
        proxy: str | None = None,
        mirror: str | None = None,
        force: bool = False,
        offline: bool = False,
    ) -> dict[str, Any]:
        self._read_cache.clear_all()
        return hot_reload_update(self, proxy=proxy, mirror=mirror, force=force, offline=offline)

    def _maybe_periodic_update_check(self) -> None:
        maybe_periodic_update_check(self)

    def _get_or_create_worker_unlocked(self, slot: str) -> BridgeWorker:
        worker = self._workers.get(slot)
        if worker is None:
            worker = self._new_worker(slot)
            self._workers[slot] = worker
        if slot not in self._slot_locks:
            self._slot_locks[slot] = threading.RLock()
        return worker

    def _get_worker_in_barrier(self, slot: str) -> BridgeWorker:
        with self._lock:
            self.last_active_ts = time.time()
            worker = self._get_or_create_worker_unlocked(slot)
            slot_lock = self._slot_locks[slot]
        with slot_lock:
            before_gen = getattr(worker, "generation", None)
            before_code = getattr(worker, "last_good_code", None)
            worker.ensure_alive()
            if (
                getattr(worker, "generation", None) != before_gen
                or getattr(worker, "last_good_code", None) != before_code
            ):
                rc = getattr(self, "_read_cache", None)
                if rc is not None:
                    rc.invalidate_slot(slot)
            return worker

    def get_worker(self, target: str = "main") -> BridgeWorker:
        slot = normalize_slot_name(target)
        with self._reader_barrier():
            return self._get_worker_in_barrier(slot)

    def check_idle(self) -> bool:
        """Recycle non-main workers idle > 5m; return True if global daemon should shut down (> 30m idle)."""
        now = time.time()
        with self._reader_barrier():
            with self._lock:
                candidates = [(name, w) for name, w in self._workers.items() if name != "main"]

            for slot_name, worker in candidates:
                if not worker.is_alive() or (now - worker.last_active_ts) <= REF_IDLE_TIMEOUT_SEC:
                    continue
                slot_lock = self._get_slot_lock(slot_name)
                if not slot_lock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        still_same = self._workers.get(slot_name) is worker
                    if still_same and worker.is_alive() and (time.time() - worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC:
                        try:
                            worker.stop()
                        except Exception:
                            pass
                        evicted = False
                        with self._lock:
                            if self._workers.get(slot_name) is worker:
                                self._workers.pop(slot_name, None)
                                evicted = True
                        if evicted:
                            self._read_cache.invalidate_slot(slot_name)
                finally:
                    slot_lock.release()

        if self.disable_global_idle:
            return False
        return (now - self.last_active_ts) > GLOBAL_IDLE_TIMEOUT_SEC

    def shutdown_all(self) -> None:
        import pob_cli.daemon as _daemon_pkg

        with self._shutdown_lock:
            already_started = self._shutdown_started
            if not already_started:
                self._shutdown_started = True
                self._stopping = True
        if already_started:
            self._shutdown_done.wait(timeout=15.0)
            return

        try:
            with self._lock:
                workers_to_abort = list(self._workers.values())
            for worker in workers_to_abort:
                try:
                    abort_fn = getattr(worker, "abort", None)
                    if callable(abort_fn):
                        abort_fn()
                    else:
                        worker.stop()
                except Exception:
                    pass

            rc = getattr(self, "_read_cache", None)
            with self._exclusive_barrier():
                for slot_name, worker in list(self._workers.items()):
                    try:
                        worker.stop()
                    except Exception:
                        pass
                    if rc is not None:
                        rc.invalidate_slot(slot_name)
                if rc is not None:
                    rc.clear_all()
                self._workers = {"main": self._workers.get("main") or self._new_worker("main")}
            state_file = _daemon_state_file(self.runtime_dir)
            try:
                if state_file.exists():
                    existing = _daemon_pkg.read_daemon_state(self.runtime_dir)
                    if existing is None or (
                        existing.get("game") in (None, GAME_ID)
                        and existing.get("pid") in (None, os.getpid())
                    ):
                        state_file.unlink()
            except Exception:
                pass
            if self.server is not None:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        finally:
            self._shutdown_done.set()

    def _slot_status_entry(self, slot_name: str, worker: BridgeWorker | None) -> dict[str, Any]:
        return slot_status_entry(self, slot_name, worker)

    def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.last_active_ts = time.time()

        if action == "stop":
            self.shutdown_all()
            return {"stopped": True}

        if action == "game_data":
            return dispatch_game_data(self, params)

        if action == "update":
            return handle_update_action(self, params)

        if action == "lang":
            return handle_set_lang_action(self, params)

        if action in ("status", "load") and not params.get("skip_periodic_check"):
            self._maybe_periodic_update_check()

        if action == "status":
            return build_status_payload(self)

        if action in ("compare", "borrow") or (
            action == "tree_diff" and not (params.get("spec_a") or params.get("spec_b"))
        ) or (
            action == "diagnose" and (params.get("compare_source") or params.get("compare_xml_code"))
        ):
            main_slot = normalize_slot_name(params.get("main_target") or params.get("target") or "main")
            default_ref = "ref" if main_slot == "main" else f"{main_slot}_ref"
            ref_slot = normalize_slot_name(params.get("ref_target") or default_ref)
            if ref_slot == main_slot:
                raise DaemonRequestError(
                    f"The reference slot must differ from the main slot (both are '{main_slot}'): "
                    "loading the reference would overwrite the main build and clear its undo history. "
                    f"Pass a different `--ref-target` (default '{default_ref}')."
                )
            with self._reader_barrier():
                locks = [self._get_slot_lock(s) for s in sorted({main_slot, ref_slot})]
                with contextlib.ExitStack() as stack:
                    for lk in locks:
                        stack.enter_context(lk)
                    mw = self._get_worker_in_barrier(main_slot)
                    rw = self._get_worker_in_barrier(ref_slot)
                    loads = any(params.get(k) for k in ("source", "xml_code", "ref_source", "ref_xml_code"))
                    for w in (mw, rw):
                        rebuild_if_stale(w, required=not loads)
                    if action == "compare" and not loads:
                        eff_lang, _ = get_active_lang(self.runtime_dir)
                        return self._read_cache.run_cached_compare(
                            main_slot,
                            mw,
                            ref_slot,
                            rw,
                            eff_lang,
                            params,
                            lambda: self._dispatch_dual_slot(action, params, mw, rw),
                        )
                    return self._dispatch_dual_slot(action, params, mw, rw)

        target = normalize_slot_name(params.get("target"))
        with self._reader_barrier():
            slot_lock = self._get_slot_lock(target)
            with slot_lock:
                worker = self._get_worker_in_barrier(target)
                drift_warn: str | None = None
                if (
                    target == "main"
                    and action not in ("load", "new_build", "diagnose", "map_regex")
                    and not getattr(worker, "last_good_code", None)
                ):
                    with self._lock:
                        loaded_others = sorted(
                            name
                            for name, other in self._workers.items()
                            if name != "main" and getattr(other, "last_good_code", None)
                        )
                    if loaded_others:
                        drift_warn = (
                            f"[WARN] Target slot 'main' has no loaded build, while named slot(s) "
                            f"{', '.join(loaded_others)} are loaded. Pass `--target {loaded_others[0]}` "
                            "if you intended to inspect or mutate that build."
                        )

                eff_lang = get_active_lang(self.runtime_dir)[0] if action in READ_CACHE_ACTIONS else ""
                cache_lang, cached_hit = self._read_cache.check_slot_read(
                    target, worker, eff_lang, action, params
                )
                if cached_hit is not None:
                    return cached_hit

                replaces = action in ("load", "new_build") or bool(params.get("source") or params.get("xml_code"))
                rebuild_if_stale(worker, required=not replaces)
                try:
                    res_slot = self._dispatch_slot(action, params, worker)
                except DaemonRequestError as exc:
                    if drift_warn and drift_warn not in str(exc):
                        raise DaemonRequestError(f"{drift_warn}\n{exc}") from exc
                    raise
                if drift_warn and isinstance(res_slot, dict):
                    res_slot.setdefault("slotDriftWarning", drift_warn)
                if action in _STATE_META_ACTIONS:
                    res_slot = self._attach_state_meta(res_slot, worker)
                self._read_cache.store_slot_read(target, worker, cache_lang, action, params, res_slot)
                return res_slot

    @staticmethod
    def _attach_state_meta(res: Any, w: BridgeWorker) -> Any:
        if isinstance(res, dict):
            res.setdefault("rev", history_of(w).rev)
            slot_nm = getattr(w, "slot_name", None)
            if isinstance(slot_nm, str) and slot_nm:
                res.setdefault("targetSlot", slot_nm)
        return res

    def _dispatch_dual_slot(
        self,
        action: str,
        params: dict[str, Any],
        mw: BridgeWorker,
        rw: BridgeWorker,
    ) -> dict[str, Any]:
        return dispatch_dual_slot_action(self, action, params, mw, rw)

    def _dispatch_slot(
        self,
        action: str,
        params: dict[str, Any],
        w: BridgeWorker,
    ) -> dict[str, Any]:
        return dispatch_slot_action(self, action, params, w)
