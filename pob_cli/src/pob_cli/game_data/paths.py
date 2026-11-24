"""Filesystem, cache directory, PID liveness, and PoB version detection helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .. import updater as _updater
from ..updater import detect_source_checkout_root, get_runtime_dir
from .constants import (
    _MANIFEST_VER_CACHE,
    _ZH_META_VER_CACHE,
)


def _sqlite_ro_uri(db_path: Path) -> str:
    """Build a properly percent-encoded read-only SQLite file URI."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def _is_pid_alive(pid: int) -> bool:
    """Best-effort OS check whether process ``pid`` is currently alive."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        if pid > 0xFFFFFFFF:
            return False
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            open_process.restype = ctypes.c_void_p
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            get_exit_code.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            handle = open_process(0x1000, 0, int(pid))
            if not handle:
                # ERROR_ACCESS_DENIED (5): process exists under higher privilege
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_ulong(0)
                if get_exit_code(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259  # STILL_ACTIVE
                return False
            finally:
                close_handle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def resolve_runtime_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve runtime directory: explicit arg -> POB_RUNTIME_DIR -> POB_HOME -> checkout/.runtime -> user appdata."""
    return get_runtime_dir(runtime_dir)


def _shared_cache_dir(
    runtime_dir: Path | None = None,
    *,
    pob_root: Path | None = None,
) -> Path:
    """Resolve shared engine-scoped cache dir via updater.get_shared_cache_dir if present."""
    env_cache = os.environ.get("POB_CACHE_DIR", "").strip()
    if env_cache:
        cache_dir = Path(env_cache)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    fn = getattr(_updater, "get_shared_cache_dir", None)
    if callable(fn):
        if pob_root is not None:
            try:
                cache_dir = Path(fn(runtime_dir, pob_root=pob_root))
                cache_dir.mkdir(parents=True, exist_ok=True)
                return cache_dir
            except TypeError:
                # Older/in-flight updater.get_shared_cache_dir without pob_root kwarg:
                # fall through to local explicit-pob_root check below.
                pass
        else:
            cache_dir = Path(fn(runtime_dir))
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir

    rt = resolve_runtime_dir(runtime_dir)
    eng_candidate: Path | None = Path(pob_root) if pob_root is not None else None
    if eng_candidate is None:
        env_root = os.environ.get("POB_ROOT", "").strip()
        if env_root:
            eng_candidate = Path(env_root)

    shared_candidate: Path | None = None
    if eng_candidate is not None:
        try:
            eng_parent = eng_candidate.resolve().parent
            same_rt = eng_parent == rt.resolve()
        except OSError:
            eng_parent = eng_candidate.parent
            same_rt = False
        if not same_rt and (
            (eng_parent / "cache").is_dir()
            or eng_parent.name.lower() in (".runtime", "pob4agents", "pob-agent-tools")
        ):
            shared_candidate = eng_parent / "cache"

    if shared_candidate is not None:
        try:
            shared_candidate.mkdir(parents=True, exist_ok=True)
            return shared_candidate
        except OSError:
            pass

    cache_dir = rt / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def resolve_pob_dir(pob_dir: Path | None = None, runtime_dir: Path | None = None) -> Path:
    """Resolve PoB engine root: explicit arg -> POB_ROOT -> <runtime_dir>/engine -> PobTools[20261102]."""
    if pob_dir is not None:
        return Path(pob_dir)
    env_pob = os.environ.get("POB_ROOT", "").strip()
    if env_pob:
        return Path(env_pob)
    rt = resolve_runtime_dir(runtime_dir)
    managed_engine = rt / "engine"
    if (managed_engine / "PoE1" / "Data").is_dir() or (managed_engine / "Data").is_dir():
        return managed_engine
    checkout_root = detect_source_checkout_root()
    if checkout_root is not None:
        local_pob = checkout_root / "PobTools[20261102]"
        if local_pob.is_dir():
            return local_pob
    return managed_engine


def _resolve_poe1_data_dirs(pob_dir: Path) -> tuple[Path, Path | None]:
    """Return `(poe1_dir, zh_dir_or_none)` for a given PoB root directory."""
    if (pob_dir / "PoE1" / "Data").is_dir():
        poe1_dir = pob_dir / "PoE1"
    elif (pob_dir / "Data").is_dir():
        poe1_dir = pob_dir
    else:
        raise FileNotFoundError(f"PoE1/Data directory not found under {pob_dir}")

    zh_candidate = pob_dir / "Data" / "poe1" / "zh-rCN"
    zh_dir = zh_candidate if zh_candidate.is_dir() else None
    return poe1_dir, zh_dir


def _scan_dir_entries(hasher: Any, directory: Path, suffix: str) -> None:
    if not directory.is_dir():
        return
    with os.scandir(directory) as it:
        entries = [e for e in it if e.name.endswith(suffix) and e.is_file()]
    entries.sort(key=lambda e: e.name)
    for e in entries:
        st = e.stat()
        hasher.update(f"{e.name}:{st.st_size}:{st.st_mtime_ns};".encode())


def detect_versions(poe1_dir: Path, zh_dir: Path | None) -> tuple[str, str]:
    """Detect `(pobVersion, dataVersion)` so engine or dictionary updates trigger index rebuild."""
    pob_version = "unknown"
    manifest_path = poe1_dir / "manifest.xml"
    hasher = hashlib.blake2b(digest_size=4)

    if manifest_path.is_file():
        st = manifest_path.stat()
        hasher.update(f"manifest.xml:{st.st_size}:{st.st_mtime_ns};".encode())
        mkey = (str(manifest_path), st.st_size, st.st_mtime_ns)
        cached_ver = _MANIFEST_VER_CACHE.get(mkey)
        if cached_ver is not None:
            pob_version = cached_ver
        else:
            try:
                root = ET.parse(manifest_path).getroot()
                ver_el = root.find("Version")
                if ver_el is not None and ver_el.get("number"):
                    pob_version = ver_el.get("number", "unknown").strip()
                    _MANIFEST_VER_CACHE[mkey] = pob_version
            except ET.ParseError:
                pass
    if pob_version == "unknown":
        gv_path = poe1_dir / "GameVersions.lua"
        if gv_path.is_file():
            gv_txt = gv_path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"treeVersionList\s*=\s*\{([^}]+)\}", gv_txt)
            if m:
                vers = re.findall(r'"([^"]+)"', m.group(1))
                if vers:
                    pob_version = vers[-1]

    data_dir = poe1_dir / "Data"
    _scan_dir_entries(hasher, data_dir, ".lua")
    _scan_dir_entries(hasher, data_dir, ".jsonc")
    _scan_dir_entries(hasher, data_dir / "Skills", ".lua")
    _scan_dir_entries(hasher, data_dir / "Bases", ".lua")
    _scan_dir_entries(hasher, data_dir / "Uniques", ".lua")
    _scan_dir_entries(hasher, data_dir / "Uniques" / "Special", ".lua")

    zh_prefix = "nozh"
    if zh_dir is not None and zh_dir.is_dir():
        zh_ver = "1.0"
        meta_path = zh_dir / "meta.json"
        if meta_path.is_file():
            st = meta_path.stat()
            zkey = (str(meta_path), st.st_size, st.st_mtime_ns)
            cached_zver = _ZH_META_VER_CACHE.get(zkey)
            if cached_zver is not None:
                zh_ver = cached_zver
            else:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    zh_ver = str(meta.get("version") or "1.0")
                    _ZH_META_VER_CACHE[zkey] = zh_ver
                except Exception:
                    pass
        zh_prefix = f"zh-{zh_ver}"
        _scan_dir_entries(hasher, zh_dir, ".json")

    safe_data = re.sub(r"[^A-Za-z0-9._-]", "_", f"{zh_prefix}-{hasher.hexdigest()}")
    return pob_version, safe_data
