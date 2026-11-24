"""Real-engine checks for the native LuaJIT host: eager legacy mod tables, Chinese item text,
raw-deflate share codes, and builds on tree versions the slim engine leaves out."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from engine_paths import find_runnable_engine, has_zh_data

from pob_cli.bridge_client import BridgeError, BridgeWorker

LUA_DIR = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"
ENGINE = find_runnable_engine()
LUAJIT = ENGINE / "bin" / "luajit" if ENGINE is not None else None
needs_engine = pytest.mark.skipif(ENGINE is None, reason="no runnable engine")
needs_luajit = pytest.mark.skipif(
    LUAJIT is None or not LUAJIT.is_file(), reason="no bootstrapped POSIX LuaJIT runtime"
)

CN_RING = """物品类别: 戒指
稀 有 度: 稀有
末日 之环
紫晶戒指
--------
需求:
等级: 60
--------
物品等级: 84
--------
+17% 混沌抗性 (implicit)
--------
+70 最大生命
+35% 混沌抗性
攻击速度加快 8%
附加 10 - 20 基础物理伤害
--------
已腐化
"""
TW_BELT = """物品種類: 腰帶
稀有度: 傳奇
魔血
重革腰帶
--------
+40 最大生命 (implicit)
--------
增加 10% 攻擊速度
"""


def _xml(tree_version: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><PathOfBuilding>'
        '<Build level="90" targetVersion="3_0" className="Templar" ascendClassName="None" mainSocketGroup="1"/>'
        f'<Tree activeSpec="1"><Spec classId="5" ascendClassId="0" treeVersion="{tree_version}" nodes="19635"/></Tree>'
        "</PathOfBuilding>"
    )


def _zlib_code(xml: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml.encode())).decode()


@pytest.fixture(scope="module")
def worker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BridgeWorker]:
    assert ENGINE is not None
    w = BridgeWorker("lua-checks", pob_dir=ENGINE, runtime_dir=tmp_path_factory.mktemp("rt"))
    w.start()
    yield w
    w.stop()


@needs_luajit
def test_legacy_mod_tables_iterate_with_pairs(tmp_path: Path) -> None:
    assert ENGINE is not None and LUAJIT is not None
    script = tmp_path / "pairs.lua"
    script.write_text(
        f"""
local lazy = assert(loadfile({str(LUA_DIR / "lazy_hooks.lua")!r}))()
lazy.install_pre_launch(".", {{}})
local function count(t) local n = 0 for _ in pairs(t) do n = n + 1 end return n end
io.write(count(LoadModule("Data/ModScourge")), " ", count(LoadModule("Data/Crucible")))
""",
        encoding="utf-8",
    )
    out = subprocess.run(
        [str(LUAJIT), str(script)], cwd=ENGINE / "PoE1", capture_output=True, text=True, timeout=60, check=True
    ).stdout.split()
    assert int(out[0]) > 500 and int(out[1]) > 500


@needs_engine
@pytest.mark.skipif(not has_zh_data(ENGINE), reason="engine has no Chinese dictionaries")
def test_chinese_item_text_is_reversed(worker: BridgeWorker) -> None:
    res = worker.call("parse_item_text", {"raw": CN_RING})
    assert res["reversed"] is True and res["parsed"] is True
    assert res["rarity"] == "RARE" and res["baseName"] == "Amethyst Ring" and res["corrupted"] is True
    lines = {(ln["kind"], ln["line"]) for ln in res["lines"]}
    assert ("implicit", "+17% to Chaos Resistance") in lines
    assert ("explicit", "+70 to maximum Life") in lines
    assert ("explicit", "8% increased Attack Speed") in lines
    assert ("explicit", "Adds 10 to 20 Physical Damage") in lines
    assert not any(ln["unsupported"] for ln in res["lines"])

    tw = worker.call("parse_item_text", {"raw": TW_BELT})
    assert tw["parsed"] is True and tw["title"] == "Mageblood" and tw["baseName"] == "Heavy Belt"

    worker.call("new_build", {})
    added = worker.call("add_item", {"raw": CN_RING})
    assert added.get("reversed") is True


@needs_engine
def test_raw_deflate_share_code_imports(worker: BridgeWorker) -> None:
    comp = zlib.compressobj(wbits=-15)
    raw = comp.compress(_xml("3_30").encode()) + comp.flush()
    worker.call("import_code", {"code": base64.urlsafe_b64encode(raw).decode(), "mode": "replace"})
    assert worker.call("get_tree_state", {})["treeVersion"] == "3_30"


def _private_engine(src: Path, dst: Path) -> Path:
    """A view of `src` whose TreeData is private, so on-demand downloads never touch `src`."""
    (dst / "PoE1" / "TreeData").mkdir(parents=True)
    for name in ("bin", "Data"):
        if (src / name).exists():
            (dst / name).symlink_to(src / name)
    for entry in (src / "PoE1").iterdir():
        if entry.name != "TreeData":
            (dst / "PoE1" / entry.name).symlink_to(entry)
    for entry in (src / "PoE1" / "TreeData").iterdir():
        if entry.name != "3_19":
            (dst / "PoE1" / "TreeData" / entry.name).symlink_to(entry)
    (dst / "PoE1" / "TreeData" / "3_19").mkdir()
    assets = src / "PoE1" / "TreeData" / "3_19" / "Assets.lua"
    (dst / "PoE1" / "TreeData" / "3_19" / "Assets.lua").write_bytes(assets.read_bytes())
    return dst


@needs_engine
@pytest.mark.skipif(sys.platform == "win32", reason="symlinked engine view needs POSIX symlinks")
def test_build_on_omitted_tree_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ENGINE is not None
    if not (ENGINE / "PoE1" / "manifest.xml").is_file() or not (
        ENGINE / "PoE1" / "TreeData" / "3_19" / "Assets.lua"
    ).is_file():
        pytest.skip("not a slim managed engine")
    eng = _private_engine(ENGINE, tmp_path / "engine")
    w = BridgeWorker("tree319", pob_dir=eng, runtime_dir=tmp_path / "rt")
    w.start()
    try:
        code = _zlib_code(_xml("3_19"))
        was_offline = os.environ.get("POB_OFFLINE") == "1"
        monkeypatch.setenv("POB_OFFLINE", "1")
        with pytest.raises(BridgeError, match=r"tree version '3_19'.*Run pob online"):
            w.call("import_code", {"code": code, "mode": "replace"})
        if was_offline:
            return  # the download half needs the network
        monkeypatch.delenv("POB_OFFLINE")
        w.call("import_code", {"code": code, "mode": "replace"})
        assert w.call("get_tree_state", {})["treeVersion"] == "3_19"
        assert (eng / "PoE1" / "TreeData" / "3_19" / "tree.lua").is_file()
    finally:
        w.stop()


@needs_engine
@pytest.mark.skipif(not has_zh_data(ENGINE), reason="engine has no Chinese dictionaries")
def test_tree_names_follow_every_locale_switch(worker: BridgeWorker) -> None:
    # bridge.lua caches tree_data per ":en"/":zh" only; zh-CN and zh-TW must not share it.
    from pob_cli.bridge_protocol import apply_worker_locale

    if not worker.caps.get("localeSwitch"):
        pytest.skip("engine has no set_locale")
    tw_dir = ENGINE / "Data" / "poe1" / "zh-rTW"
    if not tw_dir.is_dir() or not (ENGINE / "Data" / "poe1" / "zh-rCN").is_dir():
        pytest.skip("engine lacks one of zh-rCN / zh-rTW")

    def socket_name() -> tuple[str, str]:
        node = worker.get_cached_tree_data()["nodes"]["36634"]
        return str(node.get("name")), str(node.get("nameZh"))

    try:
        seen: dict[str, tuple[str, str]] = {}
        for lang in ("zh-CN", "zh-TW", "en", "zh-CN"):
            with worker.lock:
                apply_worker_locale(worker, lang)
            seen[lang + ("#2" if lang in seen else "")] = socket_name()
        en_name = seen["en"][0]
        assert seen["en"][1] == en_name
        assert seen["zh-CN"][1] != en_name and seen["zh-TW"][1] != en_name
        assert seen["zh-CN"][1] != seen["zh-TW"][1]
        assert seen["zh-CN#2"] == seen["zh-CN"]
    finally:
        with worker.lock:
            apply_worker_locale(worker, "zh-CN")
