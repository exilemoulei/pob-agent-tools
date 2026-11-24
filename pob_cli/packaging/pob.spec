# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for standalone `pob` on Windows, macOS and Linux.

--onedir, for fast CLI startup and a daemon that keeps running from the same files. On macOS and
Linux, set POB_BUNDLE_RUNTIME to a directory holding bin/luajit and bin/lua-utf8.so (from
build_posix_runtime.sh) to ship the engine runtime inside the build; `pob bootstrap` then installs
it from there instead of downloading or compiling one.
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

spec_dir = SPECPATH
src_dir = os.path.abspath(os.path.join(spec_dir, "..", "src"))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
entry_script = os.path.join(spec_dir, "pyinstaller_entry.py")

datas = collect_data_files("certifi") + [
    (os.path.join(src_dir, "pob_cli", "data", "bench_costs.json"), os.path.join("pob_cli", "data")),
    (os.path.join(src_dir, "pob_cli", "lua", "headless_runner.lua"), os.path.join("pob_cli", "lua")),
]
bundle_runtime = os.environ.get("POB_BUNDLE_RUNTIME", "").strip()
if bundle_runtime:
    # Relative to where pyinstaller runs; PyInstaller would resolve it from this spec's directory.
    bundle_runtime = os.path.abspath(bundle_runtime)
if bundle_runtime:
    # As data, not binaries: lua-utf8.so resolves lua_* from luajit at load time, which
    # PyInstaller's dependency analysis would otherwise try (and fail) to satisfy.
    for name in ("luajit", "lua-utf8.so"):
        path = os.path.join(bundle_runtime, "bin", name)
        if not os.path.isfile(path):
            raise SystemExit(f"POB_BUNDLE_RUNTIME: missing {path}")
        datas.append((path, os.path.join("runtime", "bin")))
hiddenimports = sorted(
    {
        "socksio",
        *collect_submodules("pob_cli"),
    }
)

a = Analysis(
    [entry_script],
    pathex=[src_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pob",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pob",
)
