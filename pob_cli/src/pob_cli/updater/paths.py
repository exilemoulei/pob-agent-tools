"""Runtime directory, engine root, cache directory, and platform command resolution."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..locale_cfg import _has_runtime_data_l0
from .constants import LEGACY_SEED_DIRS, LEGACY_SEED_FILES, UpdateError


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def detect_source_checkout_root() -> Path | None:
    """Return the repository root when running from a source checkout, else None (installed/frozen)."""
    if getattr(sys, "frozen", False):
        return None
    try:
        parents = Path(__file__).resolve().parents
        if len(parents) > 4:
            repo_root = parents[4]
            if (repo_root / "pob_cli" / "pyproject.toml").is_file():
                return repo_root
    except OSError:
        return None
    return None


def _has_runtime_data(path: Path) -> bool:
    """Return True if ``path`` contains initialized pob runtime state (ignoring binary-only ``bin/``)."""
    return _has_runtime_data_l0(path)


def get_default_runtime_dir() -> Path:
    """Return the default persistent runtime directory for source checkouts or installed/frozen runs.

    Priority:
      1. `POB_RUNTIME_DIR` environment variable.
      2. `POB_HOME` environment variable (`<POB_HOME>/.runtime`).
      3. Source checkout `<repo>/.runtime` (when running directly from git checkout).
      4. OS user app data directory (`%LOCALAPPDATA%/pob4agents` on Windows,
         `$XDG_DATA_HOME/pob4agents` or `~/.local/share/pob4agents` on POSIX).
         If the new directory has no runtime data yet while a legacy `pob-agent-tools`
         directory already holds runtime state, seamlessly reuse the legacy directory.
    """
    env_rt = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if env_rt:
        return Path(env_rt)

    env_home = os.environ.get("POB_HOME", "").strip()
    if env_home:
        return Path(env_home) / ".runtime"

    checkout_root = detect_source_checkout_root()
    if checkout_root is not None:
        return checkout_root / ".runtime"

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            primary = Path(local_app_data) / "pob4agents"
            legacy = Path(local_app_data) / "pob-agent-tools"
        else:
            primary = Path.home() / ".pob4agents"
            legacy = Path.home() / ".pob-agent-tools"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            primary = Path(xdg_data) / "pob4agents"
            legacy = Path(xdg_data) / "pob-agent-tools"
        else:
            primary = Path.home() / ".local" / "share" / "pob4agents"
            legacy = Path.home() / ".local" / "share" / "pob-agent-tools"

    if not _has_runtime_data(primary) and _has_runtime_data(legacy):
        return legacy
    return primary


def get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    if runtime_dir is not None:
        return Path(runtime_dir)
    return get_default_runtime_dir()


def _get_project_root() -> Path:
    env_home = os.environ.get("POB_HOME", "").strip()
    if env_home:
        return Path(env_home)
    checkout_root = detect_source_checkout_root()
    if checkout_root is not None:
        return checkout_root
    return get_default_runtime_dir()


def _get_runtime_dir(runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir)


def is_windows() -> bool:
    """Return True when running on Windows."""
    return sys.platform == "win32"


def get_posix_platform_tag() -> str:
    """Return canonical POSIX runtime tag such as 'darwin-arm64', 'darwin-x64', 'linux-x64', 'linux-arm64'."""
    os_part = "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch_part = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch_part = "x64"
    else:
        arch_part = machine or "x64"
    return f"{os_part}-{arch_part}"


def get_bundled_headless_runner_path() -> Path:
    """Return the path to the bundled POSIX LuaJIT headless_runner.lua script."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "pob_cli" / "lua" / "headless_runner.lua"
        if cand.is_file():
            return cand
    return Path(__file__).resolve().parents[1] / "lua" / "headless_runner.lua"


def resolve_luajit_binary(engine_dir: Path | None = None) -> Path | None:
    """Locate a runnable LuaJIT binary for macOS/Linux headless mode.

    Priority:
      1. `POB_LUAJIT` environment variable (explicit override).
      2. Managed `<engine_dir>/bin/luajit` (built/downloaded by `pob bootstrap`).
      3. `luajit` on `PATH`.
    """
    env_lj = os.environ.get("POB_LUAJIT", "").strip()
    if env_lj:
        p = Path(env_lj)
        if p.is_file():
            return p
        found = shutil.which(env_lj)
        if found:
            return Path(found)
        return None

    if engine_dir is not None:
        managed = Path(engine_dir) / "bin" / "luajit"
        if managed.is_file():
            return managed

    which_lj = shutil.which("luajit")
    if which_lj:
        return Path(which_lj)
    return None


def build_engine_command(engine_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Build the headless engine subprocess command and extra environment variables for the current OS.

    Windows uses `pob-zh.exe --engine-headless <PoE1>`.
    macOS/Linux uses native LuaJIT + bundled `headless_runner.lua` mounting `Data/bridge/bridge.lua`.
    Advanced users may override via `POB_ENGINE_CMD` (supporting `{engine_dir}`, `{game_dir}`,
    `{bridge_lua}`, `{runner_lua}` placeholders).
    """
    eng = Path(engine_dir)
    game_dir = eng / "PoE1"
    bridge_lua = eng / "Data" / "bridge" / "bridge.lua"
    runner_lua = get_bundled_headless_runner_path()
    on_windows = bool(_pkg().is_windows())

    if not game_dir.is_dir():
        raise UpdateError(f"PoE1 directory not found at {game_dir}")

    override = os.environ.get("POB_ENGINE_CMD", "").strip()
    if override:
        rendered = override.format(
            engine_dir=str(eng),
            game_dir=str(game_dir),
            bridge_lua=str(bridge_lua),
            runner_lua=str(runner_lua),
        )
        cmd = shlex.split(rendered, posix=not on_windows)
        if not cmd:
            raise UpdateError("POB_ENGINE_CMD resolved to an empty command")
        return cmd, {
            "POB_ZH_HEADLESS": "1",
            "POB_GAME": "poe1",
            "POB_ENGINE_ROOT": str(eng),
        }

    if on_windows:
        if (
            (game_dir / "lua51.dll").is_file()
            and (game_dir / "zlib1.dll").is_file()
            and bridge_lua.is_file()
        ):
            if not runner_lua.is_file():
                raise UpdateError(f"Bundled headless_runner.lua not found at {runner_lua}")
            host_prefix = (
                [sys.executable, "_lua-host"]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "pob_cli", "_lua-host"]
            )
            extra_env = {
                "POB_ZH_HEADLESS": "1",
                "POB_GAME": "poe1",
                "POB_ENGINE_ROOT": str(eng),
            }
            return [*host_prefix, str(runner_lua), str(game_dir), str(bridge_lua)], extra_env
        exe_path = eng / "pob-zh.exe"
        if not exe_path.is_file():
            raise UpdateError(
                f"Neither PoE1/lua51.dll+zlib1.dll (with Data/bridge/bridge.lua) nor legacy pob-zh.exe found under {eng}"
            )
        return [str(exe_path), "--engine-headless", str(game_dir)], {}

    luajit_bin = resolve_luajit_binary(eng)
    if luajit_bin is None:
        raise UpdateError(
            f"No compatible LuaJIT runtime found for macOS/Linux (checked {eng / 'bin' / 'luajit'} and PATH). "
            "Run `pob bootstrap` to install/build the native POSIX LuaJIT runtime."
        )
    if not bridge_lua.is_file():
        raise UpdateError(
            f"bridge.lua not found at {bridge_lua}. Run `pob bootstrap` to download PobTools bridge files."
        )
    if not runner_lua.is_file():
        raise UpdateError(f"Bundled headless_runner.lua not found at {runner_lua}")

    extra_env = {
        "POB_ZH_HEADLESS": "1",
        "POB_GAME": "poe1",
        "POB_ENGINE_ROOT": str(eng),
    }
    return [str(luajit_bin), str(runner_lua), str(game_dir), str(bridge_lua)], extra_env


def is_valid_engine_dir(path: Path | None) -> bool:
    """Return True if path contains a runnable headless PoB engine for the current OS."""
    if path is None:
        return False
    p = Path(path)
    if not (p / "PoE1").is_dir():
        return False
    if _pkg().is_windows():
        if (
            (p / "PoE1" / "lua51.dll").is_file()
            and (p / "PoE1" / "zlib1.dll").is_file()
            and (p / "Data" / "bridge" / "bridge.lua").is_file()
        ):
            return True
        if os.environ.get("POB_ENGINE_CMD", "").strip() and (p / "Data" / "bridge" / "bridge.lua").is_file():
            return True
        return (p / "pob-zh.exe").is_file()
    if not (p / "Data" / "bridge" / "bridge.lua").is_file():
        return False
    if os.environ.get("POB_ENGINE_CMD", "").strip():
        return True
    return resolve_luajit_binary(p) is not None


def _find_legacy_dir(legacy_dir: Path | None = None) -> Path | None:
    if legacy_dir is not None:
        p = Path(legacy_dir)
        return p if is_valid_engine_dir(p) else None

    env_legacy = os.environ.get("POB_LEGACY_DIR")
    if env_legacy is not None:
        stripped = env_legacy.strip()
        if not stripped:
            return None
        p = Path(stripped)
        return p if is_valid_engine_dir(p) else None

    candidates: list[Path] = []
    env_home = os.environ.get("POB_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home) / "PobTools[20261102]")
    else:
        checkout_root = detect_source_checkout_root()
        if checkout_root is not None:
            candidates.append(checkout_root / "PobTools[20261102]")

    for cand in candidates:
        if is_valid_engine_dir(cand):
            return cand
    return None


def resolve_engine_root(
    runtime_dir: Path | None = None,
    legacy_dir: Path | None = None,
) -> Path:
    """Resolve the active PoB engine directory.

    Priority:
      1. `POB_ROOT` environment variable if valid (`pob-zh.exe` + `PoE1/`).
      2. Managed `<runtime_dir>/engine` (`POB_RUNTIME_DIR/engine`, default `<project>/.runtime/engine`) if valid.
      3. Legacy `PobTools[20261102]` fallback if valid.
      4. Default managed path `<runtime_dir>/engine` (for fresh bootstrap).
    """
    env_root = os.environ.get("POB_ROOT", "").strip()
    if env_root:
        p = Path(env_root)
        from ..guardrails import validate_engine_root

        validate_engine_root(p, expected_game="poe1", source_label="POB_ROOT")
        if is_valid_engine_dir(p):
            return p

    managed = _get_runtime_dir(runtime_dir) / "engine"
    if is_valid_engine_dir(managed):
        return managed

    leg = _find_legacy_dir(legacy_dir)
    if leg is not None:
        return leg

    if env_root:
        return Path(env_root)
    return managed


def _is_pob_cache(cache_dir: Path) -> bool:
    """Whether pob has written to this folder: it holds a game-data index or a trade cache file."""
    try:
        return any(cache_dir.glob("gamedata_*.sqlite")) or any(cache_dir.glob("trade_*.json"))
    except OSError:
        return False


def get_shared_cache_dir(
    runtime_dir: Path | None = None,
    *,
    pob_root: Path | None = None,
) -> Path:
    """Resolve the shared cache directory for SQLite gamedata, external caches, and rate limits.

    Priority:
      1. `POB_CACHE_DIR` environment variable if set.
      2. If the effective engine dir (`pob_root` if explicitly given, else `resolve_engine_root(runtime_dir)`)
         lives outside `runtime_dir` and its parent is named `.runtime` / `pob4agents` /
         `pob-agent-tools`, or already holds a `cache/` that pob has written to, share
         `<engine_dir.parent>/cache` (falling back to `<runtime_dir>/cache` on read-only/permission
         errors). A folder that is merely called `cache` is left alone: an engine unpacked into
         Downloads must not turn an unrelated `Downloads/cache` into pob's cache.
      3. Fallback `<runtime_dir>/cache`.
    """
    env_cache = os.environ.get("POB_CACHE_DIR", "").strip()
    if env_cache:
        cache_dir = Path(env_cache)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir
        except OSError:
            pass

    rt = get_runtime_dir(runtime_dir)
    eng = Path(pob_root) if pob_root is not None else resolve_engine_root(runtime_dir=rt)

    try:
        eng_res = eng.resolve()
        rt_res = rt.resolve()
    except OSError:
        eng_res = eng
        rt_res = rt

    try:
        eng_res.relative_to(rt_res)
        inside_rt = True
    except ValueError:
        inside_rt = False

    if not inside_rt:
        eng_parent = eng_res.parent
        parent_cache = eng_parent / "cache"
        if eng_parent.name.lower() in (".runtime", "pob4agents", "pob-agent-tools") or _is_pob_cache(
            parent_cache
        ):
            try:
                parent_cache.mkdir(parents=True, exist_ok=True)
                return parent_cache
            except OSError:
                pass

    fallback = rt / "cache"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    except OSError:
        tmp_fallback = Path(tempfile.gettempdir()) / "pob4agents-cache"
        try:
            tmp_fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return tmp_fallback


def seed_from_legacy_dir(legacy_dir: Path, target_dir: Path) -> bool:
    """Losslessly copy clean host + PoE1 assets from legacy_dir into an uninitialized target_dir.

    Never modifies or deletes anything inside legacy_dir.
    Returns True if target_dir was seeded, False if already initialized or legacy_dir is invalid.
    """
    src = Path(legacy_dir)
    dst = Path(target_dir)
    if is_valid_engine_dir(dst):
        return False
    if not is_valid_engine_dir(src):
        return False

    dst.mkdir(parents=True, exist_ok=True)
    for fname in LEGACY_SEED_FILES:
        src_file = src / fname
        if src_file.is_file():
            shutil.copy2(src_file, dst / fname)

    for dname in (*LEGACY_SEED_DIRS, "bin"):
        src_sub = src / dname
        if src_sub.is_dir():
            shutil.copytree(src_sub, dst / dname, dirs_exist_ok=True)

    return is_valid_engine_dir(dst)
