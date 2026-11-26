"""``DaemonManager``: named PoB2 worker lifecycle, RW barriers, idle recycling, and dispatch."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..game_profile import GAME_ID, resolve_data_dir_override
from ..locale_cfg import get_active_lang
from ..snapshot import SlotHistory, history_of
from ..updater import (
    OK_STATES,
    get_update_warning,
    is_valid_engine_dir,
    load_update_state,
    recover_idle_engine,
    resolve_engine_root,
    unrestored_swap,
)
from .read_cache import READ_CACHE_ACTIONS, SlotReadCache
from .rpc_handlers import dispatch_slot_action
from .runtime import (
    REF_IDLE_TIMEOUT_SEC,
    UPDATE_RECHECK_INTERVAL_SEC,
    BootstrapMissingError,
    _daemon_state_file,
    _get_runtime_dir,
    _is_offline_env,
    normalize_slot_name,
    resolve_global_idle_sec,
)
from .slot_updates import (
    build_status_payload,
    handle_set_lang_action,
    hot_reload_update,
    slot_status_entry,
)

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

_STATE_META_ACTIONS = frozenset(
    {
        "load",
        "summary",
        "stats",
        "build_info",
        "tree_state",
        "items",
        "skills",
        "weapon_set_state",
        "node_power",
        "probe",
        "diagnostics",
        "undo",
        "redo",
        "new_build",
        "rpc",
    }
)


class DaemonManager:
    """Manages named headless PoB2 BridgeWorkers with per-slot locks, barriers, and read cache."""

    def __init__(
        self,
        pob_dir: Path | None = None,
        token: str = "",
        *,
        data_dir: Path | None = None,
        runtime_dir: Path | None = None,
        offline: bool | None = None,
        proxy: str | None = None,
        mirror: str | None = None,
        skip_update_check: bool = False,
        disable_global_idle: bool = False,
    ) -> None:
        self.runtime_dir = _get_runtime_dir(runtime_dir)
        self._explicit_pob_dir = Path(pob_dir) if pob_dir is not None else None
        self.data_dir = Path(data_dir) if data_dir is not None else resolve_data_dir_override()
        self.pob_dir = self._explicit_pob_dir or resolve_engine_root(runtime_dir=self.runtime_dir)
        self.token = token
        self.offline = _is_offline_env() if offline is None else bool(offline)
        self.proxy = proxy
        self.mirror = mirror
        self.skip_update_check = skip_update_check
        self.disable_global_idle = disable_global_idle
        self.global_idle_timeout_sec = resolve_global_idle_sec()
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
        import pob2_cli.daemon as _daemon_pkg

        worker = _daemon_pkg.BridgeWorker(
            slot_name=slot_name,
            pob_dir=self.pob_dir,
            data_dir=self.data_dir,
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

    def start(self) -> None:
        import pob2_cli.daemon as _daemon_pkg

        launch_bg = False
        with self._exclusive_barrier():
            recover_idle_engine(self.pob_dir)
            self._engine_fault = unrestored_swap(self.pob_dir)
            checked_at = float(self.update_status.get("checkedAt") or 0.0)
            engine_valid = is_valid_engine_dir(self.pob_dir, data_dir=self.data_dir)
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
                    launch_bg = True
            if self._explicit_pob_dir is None:
                self.pob_dir = resolve_engine_root(runtime_dir=self.runtime_dir)
            if not is_valid_engine_dir(self.pob_dir, data_dir=self.data_dir):
                hint = get_update_warning(self.update_status) or (
                    f"No usable PoB2 engine found at {self.pob_dir}. "
                    "Set POB2_ENGINE_DIR or configure POB2_PROXY and bootstrap PoB2."
                )
                raise BootstrapMissingError(hint, update_status=self.update_status)
            if launch_bg:
                t = threading.Thread(target=self._run_bg_update_check, name="pob2-boot-update-check", daemon=True)
                self._boot_update_thread = t
                t.start()
            main_w = self._workers["main"]
            if main_w.pob_root != self.pob_dir:
                main_w = self._new_worker("main")
                self._workers["main"] = main_w
            main_w.start()

    def _run_bg_update_check(self) -> None:
        import pob2_cli.daemon as _daemon_pkg

        try:
            with self._update_check_lock:
                if self._stopping:
                    return
                probe = _daemon_pkg.check_and_apply_updates(
                    engine_dir=self._explicit_pob_dir,
                    runtime_dir=self.runtime_dir,
                    offline=False,
                    proxy=self.proxy,
                    mirror=self.mirror,
                    check_only=True,
                )
                with self._lock:
                    if not self._stopping:
                        self.update_status = probe
        except Exception:
            pass

    def _current_update_warning(self) -> str | None:
        with self._lock:
            if self._engine_fault and not unrestored_swap(self.pob_dir):
                self._engine_fault = None
            return self._engine_fault or get_update_warning(self.update_status)

    def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
        warn = self._current_update_warning()
        if warn:
            payload["updateWarning"] = warn
        return payload

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
            if getattr(worker, "generation", None) != before_gen or getattr(worker, "last_good_code", None) != before_code:
                self._read_cache.invalidate_slot(slot)
            return worker

    def get_worker(self, target: str = "main") -> BridgeWorker:
        slot = normalize_slot_name(target)
        with self._reader_barrier():
            return self._get_worker_in_barrier(slot)

    def check_idle(self) -> bool:
        now = time.time()
        with self._reader_barrier():
            with self._lock:
                candidates = [(name, w) for name, w in self._workers.items() if name != "main"]
            for sname, worker in candidates:
                if not worker.is_alive() or (now - worker.last_active_ts) <= REF_IDLE_TIMEOUT_SEC:
                    continue
                slock = self._get_slot_lock(sname)
                if not slock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        still_same = self._workers.get(sname) is worker
                    if still_same and worker.is_alive() and (time.time() - worker.last_active_ts) > REF_IDLE_TIMEOUT_SEC:
                        try:
                            worker.stop()
                        except Exception:
                            pass
                        with self._lock:
                            if self._workers.get(sname) is worker:
                                self._workers.pop(sname, None)
                                self._read_cache.invalidate_slot(sname)
                finally:
                    slock.release()
        if self.disable_global_idle:
            return False
        return (now - self.last_active_ts) > self.global_idle_timeout_sec

    def shutdown_all(self) -> None:
        import pob2_cli.daemon as _daemon_pkg

        with self._shutdown_lock:
            already = self._shutdown_started
            if not already:
                self._shutdown_started = True
                self._stopping = True
        if already:
            self._shutdown_done.wait(timeout=15.0)
            return
        try:
            with self._lock:
                to_abort = list(self._workers.values())
            for w in to_abort:
                try:
                    w.abort()
                except Exception:
                    pass
            with self._exclusive_barrier():
                for sname, w in list(self._workers.items()):
                    try:
                        w.stop()
                    except Exception:
                        pass
                    self._read_cache.invalidate_slot(sname)
                self._read_cache.clear_all()
                self._workers = {"main": self._workers.get("main") or self._new_worker("main")}
            state_file = _daemon_state_file(self.runtime_dir)
            try:
                if state_file.exists():
                    existing = _daemon_pkg.read_daemon_state(self.runtime_dir)
                    if existing is None or (
                        existing.get("game") in (None, GAME_ID) and existing.get("pid") in (None, os.getpid())
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
            return {"game": GAME_ID, "stopped": True}
        if action == "lang":
            return handle_set_lang_action(self, params)
        if action == "update":
            if params.get("check_only") or params.get("offline"):
                import pob2_cli.daemon as _daemon_pkg

                with self._update_check_lock:
                    st = _daemon_pkg.check_and_apply_updates(
                        engine_dir=self._explicit_pob_dir,
                        runtime_dir=self.runtime_dir,
                        offline=bool(params.get("offline", False)),
                        proxy=params.get("proxy", self.proxy),
                        mirror=params.get("mirror", self.mirror),
                        check_only=bool(params.get("check_only", False)),
                        force=bool(params.get("force", False)),
                    )
                    with self._lock:
                        self.update_status = st
                return self._attach_warning_if_degraded(dict(st))
            return self._attach_warning_if_degraded(
                dict(
                    hot_reload_update(
                        self,
                        proxy=params.get("proxy"),
                        mirror=params.get("mirror"),
                        force=bool(params.get("force", False)),
                    )
                )
            )
        if action == "status":
            return build_status_payload(self)

        target = normalize_slot_name(params.get("target"))
        with self._reader_barrier():
            with self._get_slot_lock(target):
                worker = self._get_worker_in_barrier(target)
                eff_lang = get_active_lang(self.runtime_dir)[0] if action in READ_CACHE_ACTIONS else ""
                cache_lang, cached_hit = self._read_cache.check_slot_read(target, worker, eff_lang, action, params)
                if cached_hit is not None:
                    return cached_hit
                res_slot = dispatch_slot_action(self, action, params, worker)
                if action in _STATE_META_ACTIONS and isinstance(res_slot, dict):
                    res_slot.setdefault("game", GAME_ID)
                    res_slot.setdefault("rev", history_of(worker).rev)
                    res_slot.setdefault("targetSlot", worker.slot_name)
                self._read_cache.store_slot_read(target, worker, cache_lang, action, params, res_slot)
                return res_slot


__all__ = ["DaemonManager"]
