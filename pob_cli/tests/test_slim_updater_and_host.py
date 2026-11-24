"""Unit & real-DLL tests for slim manifest filtering, PobTools slim extraction, and Windows LuaJIT host."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from engine_paths import find_engine_data

from pob_cli.updater import (
    POB_MANIFEST_URL,
    POBTOOLS_LATEST_URL,
    STATE_UP_TO_DATE,
    STATE_UPDATED,
    UpdateError,
    bootstrap_engine,
    build_engine_command,
    check_and_apply_updates,
    find_local_manifest_mismatches,
    is_valid_engine_dir,
    parse_pob_manifest,
)
from pob_cli.updater.remote import (
    POBTOOLS_APP_ONLY_PREFIXES,
    POBTOOLS_DATA_ONLY_PREFIXES,
    _extract_zip_safely,
    ensure_timeless_lut_files,
    ensure_tree_data_files,
)
from pob_cli.updater.win_lua_host import main as win_lua_host_main
from pob_cli.updater.win_lua_host import run_lua_script

LUA_DIR = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _patch_updater_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import pob_cli.updater as upd

    transport = httpx.MockTransport(handler)

    def _fake_client(
        *,
        timeout: httpx.Timeout,
        proxy: str | None = None,
        follow_redirects: bool = True,
        max_connections: int = 16,
    ) -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(upd, "_make_http_client", _fake_client)


def _sample_rich_manifest(payloads: dict[str, bytes], *, version: str = "2.68.1") -> str:
    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<PoBVersion>",
        f'\t<Version number="{version}" platform="win32" branch="master" />',
        '\t<Source part="default" url="https://example.invalid/default/" />',
        '\t<Source part="runtime" platform="win32" url="https://example.invalid/runtime/" />',
        '\t<Source part="program" url="https://example.invalid/src/" />',
        '\t<Source part="tree" url="https://example.invalid/src/" />',
    ]
    for name, data in payloads.items():
        if name.endswith((".dll", ".exe")):
            lines.append(f'\t<File name="{name}" part="runtime" runtime="win32" sha1="{_sha1(data)}" />')
        elif name.startswith("TreeData/"):
            lines.append(f'\t<File name="{name}" part="tree" sha1="{_sha1(data)}" />')
        elif name.startswith(("lua/", "SimpleGraphic/")):
            lines.append(f'\t<File name="{name}" part="runtime" sha1="{_sha1(data)}" />')
        elif name.endswith((".txt", ".md")):
            lines.append(f'\t<File name="{name}" part="default" sha1="{_sha1(data)}" />')
        else:
            lines.append(f'\t<File name="{name}" part="program" sha1="{_sha1(data)}" />')
    lines.append("</PoBVersion>")
    return "\n".join(lines) + "\n"


def _rich_payloads() -> dict[str, bytes]:
    return {
        "Launch.lua": b"-- launch\n",
        "GameVersions.lua": b"-- versions\n",
        "UpdateCheck.lua": b"return 'official'\n",
        "changelog.txt": b"changes\n",
        "lua51.dll": b"MZ-lua51",
        "zlib1.dll": b"MZ-zlib1",
        "lua-utf8.dll": b"MZ-utf8",
        "vcruntime140.dll": b"MZ-vcrt",
        "SimpleGraphic.dll": b"MZ-sg",
        "Path{space}of{space}Building.exe": b"MZ-pob-exe",
        "SimpleGraphic/shader.hlsl": b"shader",
        "Assets/icon.png": b"pngdata",
        "lua/dkjson.lua": b"return {}\n",
        "lua/base64.lua": b"return {}\n",
        "Modules/Main.lua": b"-- main\n",
        "Classes/PassiveTree.lua": b"-- tree class\n",
        "Data/Gems.lua": b"return {}\n",
        "Data/TimelessJewelData/NodeIndexMapping.lua": b"return {}\n",
        "Data/TimelessJewelData/LegionPassives.lua": b"return {}\n",
        "Data/TimelessJewelData/LethalPride.zip": b"PK-lethal",
        "Data/TimelessJewelData/GloriousVanity.zip.part0": b"PK-gv0",
        "Data/TimelessJewelData/GloriousVanity.zip.part1": b"PK-gv1",
        "TreeData/3_19/Assets.lua": b"return {}\n",
        "TreeData/3_19/tree.lua": b"return {}\n",
        "TreeData/3_28/tree.lua": b"return { ver = '3_28' }\n",
        "TreeData/3_28/sprites.lua": b"return {}\n",
        "TreeData/3_29/tree.lua": b"return { ver = '3_29' }\n",
        "TreeData/3_29/sprites.lua": b"return {}\n",
        "TreeData/3_30/tree.lua": b"return { ver = '3_30' }\n",
        "TreeData/3_30/sprites.lua": b"return {}\n",
        "TreeData/3_30/skills-3.jpg": b"jpgdata",
        "TreeData/3_30_ruthless/tree.lua": b"return { ver = '3_30_ruthless' }\n",
        "TreeData/legion/tree-legion.lua": b"return {}\n",
        "TreeData/legion/legion-art.png": b"pngdata",
    }


def test_parse_pob_manifest_slim_vs_full_win32_and_posix() -> None:
    xml_text = _sample_rich_manifest(_rich_payloads())

    full_win = parse_pob_manifest(xml_text, include_win32_runtime=True)
    assert "SimpleGraphic.dll" in full_win.files
    assert "Path{space}of{space}Building.exe" in full_win.files
    assert "Data/TimelessJewelData/LethalPride.zip" in full_win.files
    assert "TreeData/3_28/tree.lua" in full_win.files

    slim_win = parse_pob_manifest(xml_text, include_win32_runtime=True, slim=True)
    expected_kept = {
        "Launch.lua",
        "GameVersions.lua",
        "UpdateCheck.lua",
        "lua51.dll",
        "zlib1.dll",
        "lua-utf8.dll",
        "vcruntime140.dll",
        "lua/dkjson.lua",
        "lua/base64.lua",
        "Modules/Main.lua",
        "Classes/PassiveTree.lua",
        "Data/Gems.lua",
        "Data/TimelessJewelData/NodeIndexMapping.lua",
        "Data/TimelessJewelData/LegionPassives.lua",
        "TreeData/3_19/Assets.lua",
        "TreeData/3_29/tree.lua",
        "TreeData/3_29/sprites.lua",
        "TreeData/3_30/tree.lua",
        "TreeData/3_30/sprites.lua",
        "TreeData/legion/tree-legion.lua",
    }
    assert set(slim_win.files) == expected_kept
    assert "TreeData/3_28/tree.lua" not in slim_win.files

    slim_posix = parse_pob_manifest(xml_text, include_win32_runtime=False, slim=True)
    for dll in ("lua51.dll", "zlib1.dll", "lua-utf8.dll", "vcruntime140.dll"):
        assert dll not in slim_posix.files
    assert "Modules/Main.lua" in slim_posix.files
    assert "TreeData/3_29/tree.lua" in slim_posix.files
    assert "TreeData/3_30/tree.lua" in slim_posix.files


def test_slim_etag_304_has_zero_false_mismatches_and_still_catches_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pob_cli.updater as upd_mod

    monkeypatch.setattr(upd_mod, "is_windows", lambda: True)
    payloads = _rich_payloads()
    xml_text = _sample_rich_manifest(payloads)
    slim_manifest = parse_pob_manifest(xml_text, include_win32_runtime=True, slim=True)

    rt = tmp_path / "rt"
    eng = rt / "engine"
    poe1 = eng / "PoE1"
    (eng / "Data" / "bridge").mkdir(parents=True)
    (eng / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")
    (eng / ".pobtools_version.json").write_text(
        json.dumps({"pobToolsVersion": "1.8.12", "pobToolsTag": "v1.8.12", "dataVersion": "data-24"}),
        encoding="utf-8",
    )
    for entry in slim_manifest.files.values():
        fp = poe1 / entry.local_rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(payloads[entry.raw_name])
    (poe1 / "manifest.xml").write_text(xml_text, encoding="utf-8")

    # Full manifest check flags skipped files, while slim=True returns 0 mismatches!
    assert len(find_local_manifest_mismatches(poe1, slim=False)) > 5
    assert find_local_manifest_mismatches(poe1, slim=True, fast_stat=True, cache_dir=rt / "cache") == []

    # 304 ETag check stays STATE_UP_TO_DATE without downloading skipped files
    (rt / "update_state.json").write_text(
        json.dumps(
            {
                "state": STATE_UP_TO_DATE,
                "localPobVersion": "2.68.1",
                "pobToolsVersion": "1.8.12",
                "pobToolsTag": "v1.8.12",
                "dataVersion": "data-24",
                "pobManifestEtag": 'W/"slim-304"',
            }
        ),
        encoding="utf-8",
    )
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_urls.append(url)
        if url == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/x/y/releases/tag/v1.8.12"})
        if url == POB_MANIFEST_URL:
            if request.headers.get("if-none-match") == 'W/"slim-304"':
                return httpx.Response(304, headers={"ETag": 'W/"slim-304"'})
            return httpx.Response(200, text=xml_text, headers={"ETag": 'W/"slim-304"'})
        if url.endswith("/src/Modules/Main.lua"):
            return httpx.Response(200, content=payloads["Modules/Main.lua"])
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)
    res_ok = check_and_apply_updates(engine_dir=eng, runtime_dir=rt, include_zh=False)
    assert res_ok["state"] == STATE_UP_TO_DATE
    assert len(seen_urls) == 2

    # Tampering a kept file IS detected and repaired even when ETag returns 304
    (poe1 / "Modules" / "Main.lua").write_bytes(b"-- tampered!\n")
    res_rep = check_and_apply_updates(
        engine_dir=eng,
        runtime_dir=rt,
        include_zh=False,
        probe_fn=lambda _p: {"ok": True, "pobVersion": "2.68.1", "caps": {}},
    )
    assert res_rep["state"] == STATE_UPDATED
    assert (poe1 / "Modules" / "Main.lua").read_bytes() == payloads["Modules/Main.lua"]


def test_pobtools_zip_slim_extraction_skips_exe_fonts_and_poe2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.updater as upd_mod

    app_zip = tmp_path / "PobTools-1.8.12.zip"
    app_zip.write_bytes(
        _make_zip_bytes(
            {
                "PobTools-1.8.12/pob-zh.exe": b"MZ-exe",
                "PobTools-1.8.12/Fonts/Noto.ttf": b"font",
                "PobTools-1.8.12/PoE2/Launch.lua": b"poe2",
                "PobTools-1.8.12/Data/poe2/zh-rCN/gems.json": b"{}",
                "PobTools-1.8.12/Data/poe1/ko-KR/items.json": b"{}",
                "PobTools-1.8.12/Data/bridge/bridge.lua": b"-- bridge",
                "PobTools-1.8.12/Data/poe1/zh-rCN/items.json": b"{}",
                "PobTools-1.8.12/Data/poe1/zh-rTW/items.json": b"{}",
                "PobTools-1.8.12/engine/lua51.dll": b"old-dll",
            }
        )
    )
    app_out = tmp_path / "app_out"
    extracted_app = _extract_zip_safely(app_zip, app_out, only_prefixes=POBTOOLS_APP_ONLY_PREFIXES)
    rel_app = {p.as_posix() for p in extracted_app}
    assert rel_app == {
        "Data/bridge/bridge.lua",
        "Data/poe1/zh-rCN/items.json",
        "Data/poe1/zh-rTW/items.json",
    }
    assert not (app_out / "pob-zh.exe").exists()
    assert not (app_out / "Fonts").exists()
    assert not (app_out / "PoE2").exists()
    assert not (app_out / "Data" / "poe1" / "ko-KR").exists()

    data_zip = tmp_path / "PobTools-DataV2-24.zip"
    data_zip.write_bytes(
        _make_zip_bytes(
            {
                "Data/translations_version.json": b'{"dataVersion":"data-24"}',
                "Data/poe1/zh-rCN/gems.json": b"{}",
                "Data/poe1/zh-rTW/gems.json": b"{}",
                "Data/poe1/ko-KR/gems.json": b"{}",
                "Data/poe2/zh-rCN/gems.json": b"{}",
                "PoE2/foo.lua": b"x",
            }
        )
    )
    data_out = tmp_path / "data_out"
    extracted_data = _extract_zip_safely(data_zip, data_out, only_prefixes=POBTOOLS_DATA_ONLY_PREFIXES)
    assert {p.as_posix() for p in extracted_data} == {
        "Data/translations_version.json",
        "Data/poe1/zh-rCN/gems.json",
        "Data/poe1/zh-rTW/gems.json",
    }

    # Verify slim bootstrap_engine on simulated Windows produces 0 pob-zh.exe, 0 .zip*, 0 .png
    monkeypatch.setattr(upd_mod, "is_windows", lambda: True)
    payloads = _rich_payloads()
    xml_text = _sample_rich_manifest(payloads)
    app_bytes = app_zip.read_bytes()
    data_bytes = data_zip.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/x/y/releases/tag/v1.8.12"})
        if url.endswith("PobTools-manifest-v1.8.12.json"):
            return httpx.Response(
                200,
                json={
                    "version": "1.8.12",
                    "tag": "v1.8.12",
                    "dataTag": "data-24",
                    "assets": [{"name": "PobTools-1.8.12.zip", "sha256": hashlib.sha256(app_bytes).hexdigest()}],
                },
            )
        if url.endswith("PobTools-manifest-data-24.json"):
            return httpx.Response(
                200,
                json={
                    "tag": "data-24",
                    "minApp": "1.8.0",
                    "assets": [{"name": "PobTools-DataV2-24.zip", "sha256": hashlib.sha256(data_bytes).hexdigest()}],
                },
            )
        if url.endswith("PobTools-1.8.12.zip"):
            return httpx.Response(200, content=app_bytes)
        if url.endswith("PobTools-DataV2-24.zip"):
            return httpx.Response(200, content=data_bytes)
        if url == POB_MANIFEST_URL:
            return httpx.Response(200, text=xml_text, headers={"ETag": 'W/"slim-boot"'})
        for name, data in payloads.items():
            if url.endswith("/" + name.replace("{space}", "%7Bspace%7D")):
                return httpx.Response(200, content=data)
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)
    rt = tmp_path / "boot_rt"
    eng = rt / "engine"
    res = bootstrap_engine(
        target_dir=eng,
        runtime_dir=rt,
        include_zh=True,
        probe_fn=lambda _p: {"ok": True, "pobVersion": "2.68.1", "bridgeVersion": "0.2.0", "caps": {}},
    )
    assert res["state"] == STATE_UPDATED
    assert not (eng / "pob-zh.exe").exists()
    assert not (eng / "Fonts").exists()
    assert not (eng / "PoE2").exists()
    assert (eng / "PoE1" / "lua51.dll").is_file()
    assert (eng / "Data" / "bridge" / "bridge.lua").is_file()
    assert (eng / "Data" / "poe1" / "zh-rCN" / "gems.json").is_file()
    assert (eng / "Data" / "poe1" / "zh-rTW" / "gems.json").is_file()
    assert not (eng / "Data" / "poe1" / "ko-KR").exists()
    assert not list((eng / "PoE1").rglob("*.zip*"))
    assert not list((eng / "PoE1").rglob("*.png"))


def test_windows_build_engine_command_native_and_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pob_cli.updater as upd_mod

    monkeypatch.setattr(upd_mod, "is_windows", lambda: True)
    monkeypatch.delenv("POB_ENGINE_CMD", raising=False)

    # 1. Slim native engine (PoE1/lua51.dll + Data/bridge/bridge.lua, NO pob-zh.exe)
    slim_eng = tmp_path / "slim_eng"
    (slim_eng / "PoE1").mkdir(parents=True)
    assert is_valid_engine_dir(slim_eng) is False

    (slim_eng / "PoE1" / "lua51.dll").write_bytes(b"dll")
    (slim_eng / "Data" / "bridge").mkdir(parents=True)
    (slim_eng / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")
    assert is_valid_engine_dir(slim_eng) is True

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd, env = build_engine_command(slim_eng)
    assert cmd[:4] == [sys.executable, "-m", "pob_cli", "_lua-host"]
    assert cmd[4].endswith("headless_runner.lua")
    assert Path(cmd[5]) == slim_eng / "PoE1"
    assert Path(cmd[6]) == slim_eng / "Data" / "bridge" / "bridge.lua"
    assert env["POB_ZH_HEADLESS"] == "1"
    assert env["POB_ENGINE_ROOT"] == str(slim_eng)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd_frozen, _ = build_engine_command(slim_eng)
    assert cmd_frozen[:2] == [sys.executable, "_lua-host"]

    # 2. Legacy PobTools seed (pob-zh.exe without PoE1/lua51.dll) falls back cleanly
    legacy_eng = tmp_path / "legacy_eng"
    (legacy_eng / "PoE1").mkdir(parents=True)
    (legacy_eng / "pob-zh.exe").write_bytes(b"MZ")
    assert is_valid_engine_dir(legacy_eng) is True
    cmd_leg, env_leg = build_engine_command(legacy_eng)
    assert cmd_leg == [str(legacy_eng / "pob-zh.exe"), "--engine-headless", str(legacy_eng / "PoE1")]
    assert env_leg == {}


def test_ondemand_tree_and_timeless_fetch_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _rich_payloads()
    xml_text = _sample_rich_manifest(payloads)
    poe1 = tmp_path / "PoE1"
    poe1.mkdir()
    (poe1 / "manifest.xml").write_text(xml_text, encoding="utf-8")

    fetched_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched_urls.append(url)
        for name, data in payloads.items():
            if url.endswith("/" + name):
                return httpx.Response(200, content=data)
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)
    monkeypatch.delenv("POB_OFFLINE", raising=False)

    tree_paths = ensure_tree_data_files(poe1, "3_28")
    assert {p.name for p in tree_paths} == {"tree.lua", "sprites.lua"}
    assert (poe1 / "TreeData" / "3_28" / "tree.lua").read_bytes() == payloads["TreeData/3_28/tree.lua"]

    # Second call sees valid SHA1 on disk and performs zero HTTP requests
    fetched_urls.clear()
    ensure_tree_data_files(poe1, "3_28")
    assert fetched_urls == []

    lut_paths = ensure_timeless_lut_files(poe1, "GloriousVanity")
    assert {p.name for p in lut_paths} == {"GloriousVanity.zip.part0", "GloriousVanity.zip.part1"}

    with pytest.raises(UpdateError, match="Invalid tree_version"):
        ensure_tree_data_files(poe1, "../evil")


def test_win_lua_host_cli_validation(tmp_path: Path) -> None:
    assert win_lua_host_main([]) == 2
    assert win_lua_host_main(["only_one_arg"]) == 2
    assert win_lua_host_main([str(tmp_path / "missing.lua"), str(tmp_path)]) == 1


def _find_real_win_lua51_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    env_dir = os.environ.get("POB_LUA51_DIR", "").strip()
    if env_dir:
        cand = Path(env_dir)
        if (cand / "lua51.dll").is_file() and (cand / "zlib1.dll").is_file():
            return cand
    eng = find_engine_data()
    if eng is not None and (eng / "PoE1" / "lua51.dll").is_file() and (eng / "PoE1" / "zlib1.dll").is_file():
        return eng / "PoE1"
    return None


@pytest.mark.skipif(_find_real_win_lua51_dir() is None, reason="No Windows PoE1/lua51.dll + zlib1.dll available")
def test_real_win_lua_host_win32_ffi_and_lazy_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_poe1 = _find_real_win_lua51_dir()
    assert real_poe1 is not None
    monkeypatch.setenv("POB_OFFLINE", "1")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    ondemand = tmp_path / "ondemand"
    (ondemand / "TreeData" / "3_29").mkdir(parents=True)
    (ondemand / "TreeData" / "3_29" / "tree.lua").write_text("return { ok = 'from_ondemand' }\n", encoding="utf-8")

    # Minimal synthetic game modules inside tmp_path to exercise lazy_hooks cleanly
    fake_game = tmp_path / "game"
    (fake_game / "Data").mkdir(parents=True)
    (fake_game / "Modules").mkdir(parents=True)
    (fake_game / "Data" / "PearlSupports.lua").write_text(
        'return { [1] = { baseItemName = "Brutality Support" } }\n', encoding="utf-8"
    )
    (fake_game / "Data" / "Crucible.lua").write_text('return { modA = "crucible_val" }\n', encoding="utf-8")
    (fake_game / "Data" / "ModScourge.lua").write_text('return { scA = "scourge_val" }\n', encoding="utf-8")
    (fake_game / "Modules" / "Data.lua").write_text(
        "data = { itemMods = { Item = {}, Scourge = LoadModule('Data/ModScourge') } }\nreturn data\n",
        encoding="utf-8",
    )
    (fake_game / "Modules" / "StatDescriber.lua").write_text(
        'return function(stats, scope) return { "REAL:" .. tostring(scope) }, {} end\n',
        encoding="utf-8",
    )
    (fake_game / "Modules" / "Main.lua").write_text(
        "main = { uniqueDB = { loading = true }, onFrameFuncs = {} }\n"
        "function main:Init()\n"
        "  self.onFrameFuncs['LoadItems'] = function()\n"
        "    self.uniqueDB.drained = true\n"
        "    self.onFrameFuncs['LoadItems'] = nil\n"
        "  end\n"
        "end\n"
        "return main\n",
        encoding="utf-8",
    )

    out_json = tmp_path / "lua_test_out.json"
    script = tmp_path / "verify_modules.lua"
    script.write_text(
        f"""
local win32 = assert(loadfile({json.dumps((LUA_DIR / "win32_ffi.lua").as_posix(), ensure_ascii=False)}))()
local lazy = assert(loadfile({json.dumps((LUA_DIR / "lazy_hooks.lua").as_posix(), ensure_ascii=False)}))()

local plat = win32.install({json.dumps(real_poe1.as_posix(), ensure_ascii=False)})
assert(plat.installed == true)
assert(GetTime() >= 0)

local sub = {json.dumps((sandbox / "nested_dir").as_posix(), ensure_ascii=False)}
assert(MakeDir(sub) == true)
local tf = assert(io.open(sub .. "/hello.txt", "wb"))
tf:write("abc123")
tf:close()

local fs = assert(NewFileSearch(sub .. "/*", false))
assert(fs:GetFileName() == "hello.txt")
assert(fs:GetFileSize() == 6)
assert(fs:GetFileModifiedTime() > 1500000000)
assert(fs:NextFile() == false)

local payload = string.rep("PathOfBuildingLuaJIT!", 50)
local z = assert(Deflate(payload, false))
assert(Inflate(z) == payload)
local gz = assert(Deflate(payload, true))
assert(Inflate(gz) == payload)
assert(RemoveDir(sub, true) == true)

-- Test lazy_hooks on synthetic fake_game dir
local gdir = {json.dumps(fake_game.as_posix(), ensure_ascii=False)}
plat.chdir(gdir)
package.path = gdir .. "/?.lua;" .. package.path
lazy.install_pre_launch(gdir, {{ ondemand_tree_dir = {json.dumps(ondemand.as_posix(), ensure_ascii=False)} }})

local flav = LoadModule("Data/FlavourText")
assert(type(flav) == "table" and next(flav) == nil)
assert(lazy.stats.hook_flavour_skipped == 1)

local desc = LoadModule("Modules/StatDescriber")
local lines, _ = desc({{
    local_pearl_random_support_gem_2_slot_index = 1,
    local_pearl_random_support_gem_2_index = 1,
    local_pearl_random_support_gem_2_level = 20,
}}, "stat_descriptions")
assert(lines[1] == "Skills Socketed in your Helmet are Supported by level 20 Brutality")
assert(lazy.stats.hook_pearl_fastpath_calls == 1)
assert(lazy.stats.hook_stat_desc_real_loads == 0)

local real_lines = desc({{ some_other = 1 }}, "skill_stat_descriptions")
assert(real_lines[1] == "REAL:skill_stat_descriptions")
assert(lazy.stats.hook_stat_desc_real_loads == 1)

local cruc = LoadModule("Data/Crucible")
assert(lazy.stats.hook_crucible_deferred == 1 and lazy.stats.hook_crucible_real_loads == 0)
assert(cruc.modA == "crucible_val")
assert(lazy.stats.hook_crucible_real_loads == 1)

LoadModule("Modules/Data")
assert(lazy.stats.hook_scourge_deferred == 1 and lazy.stats.hook_scourge_real_loads == 0)
assert(data.itemMods.Item.scA == "scourge_val")
assert(lazy.stats.hook_scourge_real_loads == 1)

LoadModule("Modules/Main")
main:Init()
assert(main.onFrameFuncs["LoadItems"] == nil)
assert(main.uniqueDB.loading == nil)
assert(lazy.stats.hook_item_dbs_skipped == 1)
assert(lazy.ensure_item_dbs() == true)
assert(main.uniqueDB.drained == true)

local tree29 = LoadModule("TreeData/3_29/tree")
assert(tree29.ok == "from_ondemand")
assert(lazy.stats.tree_fallback_hits >= 1)

-- Verify __POB_ENSURE_TREE FFI callback runs cleanly from Lua without crashing
assert(type(_G.__POB_ENSURE_TREE) == "function")
assert(_G.__POB_ENSURE_TREE("3_30") == false)
assert(_G.__POB_ENSURE_TREE("") == false)

-- Verify io.open on missing TreeData/*.png does NOT trigger tree fallback or __POB_ENSURE_TREE
local cb_calls = 0
local orig_ensure = _G.__POB_ENSURE_TREE
_G.__POB_ENSURE_TREE = function(v)
    cb_calls = cb_calls + 1
    return orig_ensure(v)
end
local missing_img = io.open("TreeData/3_30/AscendancyButton.png", "r")
assert(missing_img == nil)
assert(cb_calls == 0)

-- While missing TreeData/*.lua DOES invoke __POB_ENSURE_TREE (once per version)
local missing_tree = io.open("TreeData/3_99/tree.lua", "r")
assert(missing_tree == nil)
assert(cb_calls == 1)
_G.__POB_ENSURE_TREE = orig_ensure

local out = assert(io.open({json.dumps(out_json.as_posix(), ensure_ascii=False)}, "wb"))
out:write("OK")
out:close()
""",
        encoding="utf-8",
    )

    rc = run_lua_script(real_poe1, script, [str(real_poe1)], dll_path=real_poe1 / "lua51.dll")
    assert rc == 0
    assert out_json.read_text(encoding="utf-8") == "OK"
