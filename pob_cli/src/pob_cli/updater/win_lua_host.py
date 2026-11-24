"""Windows native ctypes LuaJIT host embedding Path of Building's official lua51.dll.

Invoked via `pob _lua-host <runner_lua> <game_dir> [bridge_lua ...]` (or `python -m pob_cli _lua-host ...`)
before importing Click or heavy CLI modules, giving Windows the exact same zero-wrapper
LuaJIT headless runtime architecture as macOS/Linux without shipping `pob-zh.exe`.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence
from ctypes import POINTER, byref, c_char, c_char_p, c_int, c_size_t, c_void_p
from pathlib import Path
from typing import Any

LUA_GLOBALSINDEX = -10002
LUA_TSTRING = 4
TREE_FETCH_CB = ctypes.CFUNCTYPE(c_int, c_char_p)


class LuaHostError(RuntimeError):
    """Raised when loading or running lua51.dll fails."""


def _configure_lua_dll(dll: ctypes.CDLL) -> ctypes.CDLL:
    """Bind the minimal Lua 5.1 / LuaJIT C API prototypes needed to host headless_runner.lua."""
    dll.luaL_newstate.restype = c_void_p
    dll.luaL_openlibs.argtypes = [c_void_p]
    dll.lua_close.argtypes = [c_void_p]

    dll.luaL_loadbuffer.argtypes = [c_void_p, c_char_p, c_size_t, c_char_p]
    dll.luaL_loadbuffer.restype = c_int
    dll.lua_pcall.argtypes = [c_void_p, c_int, c_int, c_int]
    dll.lua_pcall.restype = c_int

    dll.lua_getfield.argtypes = [c_void_p, c_int, c_char_p]
    dll.lua_setfield.argtypes = [c_void_p, c_int, c_char_p]
    dll.lua_createtable.argtypes = [c_void_p, c_int, c_int]
    dll.lua_rawseti.argtypes = [c_void_p, c_int, c_int]

    dll.lua_pushlstring.argtypes = [c_void_p, c_char_p, c_size_t]
    dll.lua_pushboolean.argtypes = [c_void_p, c_int]

    dll.lua_type.argtypes = [c_void_p, c_int]
    dll.lua_type.restype = c_int
    dll.lua_tolstring.argtypes = [c_void_p, c_int, POINTER(c_size_t)]
    dll.lua_tolstring.restype = POINTER(c_char)
    dll.lua_gettop.argtypes = [c_void_p]
    dll.lua_gettop.restype = c_int
    dll.lua_settop.argtypes = [c_void_p, c_int]
    return dll


def _read_lua_string(dll: ctypes.CDLL, state: c_void_p, idx: int) -> str:
    length = c_size_t(0)
    ptr = dll.lua_tolstring(state, idx, byref(length))
    if not ptr:
        return ""
    return ctypes.string_at(ptr, length.value).decode("utf-8", errors="replace")


def _push_utf8(dll: ctypes.CDLL, state: c_void_p, text: str) -> None:
    raw = text.encode("utf-8")
    dll.lua_pushlstring(state, raw, len(raw))


def _load_lua51_dll(game_dir: Path, dll_path: Path | None = None) -> tuple[ctypes.CDLL, Any]:
    """Load PoE1/lua51.dll (preloading bundled vcruntime140.dll when present)."""
    resolved_dir = game_dir.resolve()
    target_dll = (dll_path or (resolved_dir / "lua51.dll")).resolve()
    if not target_dll.is_file():
        raise LuaHostError(f"lua51.dll not found at {target_dll}")

    dll_dir_handle: Any = None
    add_dll_dir = getattr(os, "add_dll_directory", None)
    if callable(add_dll_dir):
        dll_dir_handle = add_dll_dir(str(target_dll.parent))

    vcrt = target_dll.parent / "vcruntime140.dll"
    if vcrt.is_file():
        try:
            ctypes.CDLL(str(vcrt))
        except OSError:
            pass

    try:
        dll = ctypes.CDLL(str(target_dll))
    except OSError as exc:
        if dll_dir_handle is not None:
            dll_dir_handle.close()
        raise LuaHostError(f"Failed to load {target_dll}: {exc}") from exc

    return _configure_lua_dll(dll), dll_dir_handle


def _make_ensure_tree_callback(game_dir: Path) -> Any:
    """Create a plain C ABI callback `int(const char *tree_version)` for LuaJIT FFI."""

    @TREE_FETCH_CB
    def _cb(ver_raw: bytes | None) -> int:
        if not ver_raw or os.environ.get("POB_OFFLINE") == "1":
            return 0
        tree_ver = ver_raw.decode("utf-8", errors="replace").strip()
        if not tree_ver:
            return 0
        try:
            from .remote import ensure_tree_data_files

            fetched = ensure_tree_data_files(game_dir, tree_ver)
            return 1 if fetched else 0
        except Exception:
            return 0

    return _cb


def _install_ffi_ensure_tree(dll: ctypes.CDLL, state: c_void_p, ensure_tree_cb: Any) -> None:
    """Bind `_G.__POB_ENSURE_TREE(ver)` via LuaJIT FFI (avoids re-entering lua51.dll from ctypes)."""
    addr = ctypes.cast(ensure_tree_cb, c_void_p).value or 0
    if not addr:
        return
    hi = (addr >> 32) & 0xFFFFFFFF
    lo = addr & 0xFFFFFFFF
    bootstrap = (
        "do\n"
        '  local ok, ffi = pcall(require, "ffi")\n'
        "  if ok and ffi then\n"
        f'    local ptr = ffi.cast("uintptr_t", {hi}) * 4294967296ULL + ffi.cast("uintptr_t", {lo})\n'
        '    local cb = ffi.cast("int (*)(const char *)", ptr)\n'
        "    _G.__POB_ENSURE_TREE = function(ver)\n"
        '      if type(ver) ~= "string" or ver == "" then return false end\n'
        "      return cb(ver) ~= 0\n"
        "    end\n"
        "  end\n"
        "end\n"
    ).encode()
    if dll.luaL_loadbuffer(state, bootstrap, len(bootstrap), b"=@win_lua_host_ffi_cb") == 0:
        if dll.lua_pcall(state, 0, 0, 0) != 0:
            dll.lua_settop(state, -2)
    else:
        dll.lua_settop(state, -2)


def _populate_lua_globals(
    dll: ctypes.CDLL,
    state: c_void_p,
    runner_lua: Path,
    script_args: Sequence[str],
    ensure_tree_cb: Any,
) -> None:
    """Set Lua `arg`, host markers, and on-demand tree fallback callback before running script."""
    dll.lua_createtable(state, len(script_args), 1)
    _push_utf8(dll, state, str(runner_lua))
    dll.lua_rawseti(state, -2, 0)
    for idx, val in enumerate(script_args, start=1):
        _push_utf8(dll, state, val)
        dll.lua_rawseti(state, -2, idx)
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"arg")

    dll.lua_pushboolean(state, 1)
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_WIN_LUA_HOST")

    _push_utf8(dll, state, runner_lua.resolve().parent.as_posix())
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_LUA_DIR")

    _install_ffi_ensure_tree(dll, state, ensure_tree_cb)


def _push_traceback_handler(dll: ctypes.CDLL, state: c_void_p) -> int:
    """Push `debug.traceback` onto the stack and return its stack index (or 0 if unavailable)."""
    dll.lua_getfield(state, LUA_GLOBALSINDEX, b"debug")
    if dll.lua_type(state, -1) != 5:  # LUA_TTABLE
        dll.lua_settop(state, -2)
        return 0
    dll.lua_getfield(state, -1, b"traceback")
    # Remove `debug` table, leaving `debug.traceback` on top
    dll.lua_setfield(state, LUA_GLOBALSINDEX, b"__POB_HOST_TB_TMP")
    dll.lua_settop(state, -2)
    dll.lua_getfield(state, LUA_GLOBALSINDEX, b"__POB_HOST_TB_TMP")
    return dll.lua_gettop(state)


def run_lua_script(
    game_dir: Path,
    runner_lua: Path,
    script_args: Sequence[str] = (),
    *,
    dll_path: Path | None = None,
) -> int:
    """Execute `runner_lua` inside `game_dir/lua51.dll` with `arg[1..N] = script_args`."""
    runner_resolved = Path(runner_lua).resolve()
    if not runner_resolved.is_file():
        raise LuaHostError(f"Lua runner script not found at {runner_resolved}")

    dll, dll_dir_handle = _load_lua51_dll(Path(game_dir), dll_path=dll_path)
    state = dll.luaL_newstate()
    if not state:
        if dll_dir_handle is not None:
            dll_dir_handle.close()
        raise LuaHostError("luaL_newstate() returned NULL")

    # Keep callback reference alive for the full lua_State lifetime
    ensure_tree_cb = _make_ensure_tree_callback(Path(game_dir))
    try:
        dll.luaL_openlibs(state)
        _populate_lua_globals(dll, state, runner_resolved, script_args, ensure_tree_cb)

        sys.stdout.flush()
        sys.stderr.flush()

        raw_script = runner_resolved.read_bytes()
        chunk_name = f"@{runner_resolved.as_posix()}".encode()
        err_idx = _push_traceback_handler(dll, state)

        rc = dll.luaL_loadbuffer(state, raw_script, len(raw_script), chunk_name)
        if rc != 0:
            err_msg = _read_lua_string(dll, state, -1)
            sys.stderr.write(f"win_lua_host load error: {err_msg}\n")
            sys.stderr.flush()
            return 1

        rc = dll.lua_pcall(state, 0, 0, err_idx)
        if rc != 0:
            err_msg = _read_lua_string(dll, state, -1)
            sys.stderr.write(f"win_lua_host runtime error: {err_msg}\n")
            sys.stderr.flush()
            return 1
        return 0
    finally:
        dll.lua_close(state)
        if dll_dir_handle is not None:
            dll_dir_handle.close()


def _parse_host_args(args: Sequence[str]) -> tuple[Path | None, Path, Path, list[str]]:
    """Parse `[--dll <path>] <runner_lua> <game_dir> [bridge_lua ...]`."""
    idx = 0
    dll_override: Path | None = None
    items = list(args)
    if len(items) >= 2 and items[0] == "--dll":
        dll_override = Path(items[1])
        idx = 2
    positional = items[idx:]
    if len(positional) < 2:
        raise ValueError("Usage: pob _lua-host [--dll <lua51.dll>] <runner_lua> <game_dir> [bridge_lua ...]")
    runner_lua = Path(positional[0])
    game_dir = Path(positional[1])
    script_args = positional[1:]
    if dll_override is None:
        env_dll = os.environ.get("POB_LUA51_DLL", "").strip()
        if env_dll:
            dll_override = Path(env_dll)
    return dll_override, runner_lua, game_dir, script_args


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `pob _lua-host <runner_lua> <game_dir> [bridge_lua ...]`."""
    if argv is None:
        raw_args = list(sys.argv[2:]) if sys.argv[1:2] == ["_lua-host"] else list(sys.argv[1:])
    else:
        raw_args = list(argv)

    try:
        dll_override, runner_lua, game_dir, script_args = _parse_host_args(raw_args)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 2

    try:
        return run_lua_script(game_dir, runner_lua, script_args, dll_path=dll_override)
    except LuaHostError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
