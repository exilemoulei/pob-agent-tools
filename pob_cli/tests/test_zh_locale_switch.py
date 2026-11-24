"""Tests for zh-CN / zh-TW / en locale configuration, zh_translate.lua, and dual-locale game_data."""

from __future__ import annotations

import ast
import faulthandler
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_engine_data

from pob_cli.bridge_client import BridgeWorker
from pob_cli.game_data import ensure_gamedata_index, query_local_data
from pob_cli.game_data.paths import _resolve_poe1_data_dirs, list_available_zh_dirs
from pob_cli.game_data.zh_match import _english_for_zh_cached, english_for_zh
from pob_cli.locale_cfg import (
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    current_lang,
    get_active_lang,
    lang_to_data_subdir,
    normalize_lang,
    resolve_runtime_dir_l0,
    set_active_lang,
)
from pob_cli.profile_import.cn_zh2en import Zh2EnDictionary
from pob_cli.timeless.lut import _local_jewel_name, _resolve_jewel_static, load_jewels
from pob_cli.updater.paths import get_runtime_dir
from pob_cli.updater.win_lua_host import run_lua_script

PKG_DIR = Path(__file__).resolve().parents[1] / "src" / "pob_cli"
LUA_DIR = PKG_DIR / "lua"


def _find_win_lua51_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    env_dir = os.environ.get("POB_LUA51_DIR", "").strip()
    if env_dir:
        cand = Path(env_dir)
        if (cand / "lua51.dll").is_file():
            return cand
    eng = find_engine_data()
    if eng is not None and (eng / "PoE1" / "lua51.dll").is_file():
        return eng / "PoE1"
    return None


def _run_lua_inproc(lua_dir: Path, script: Path) -> int:
    was_enabled = faulthandler.is_enabled()
    if was_enabled:
        faulthandler.disable()
    try:
        return run_lua_script(lua_dir, script, [], dll_path=lua_dir / "lua51.dll")
    finally:
        if was_enabled:
            faulthandler.enable()


def _build_synthetic_dual_locale_engine(root: Path) -> Path:
    poe1_data = root / "PoE1" / "Data"
    (poe1_data / "Skills").mkdir(parents=True)
    (poe1_data / "Bases").mkdir(parents=True)
    (poe1_data / "Uniques").mkdir(parents=True)
    (root / "PoE1" / "manifest.xml").write_text(
        "<?xml version='1.0' encoding='UTF-8'?><PoBVersion><Version number='2.68.1'/></PoBVersion>\n",
        encoding="utf-8",
    )
    (poe1_data / "Gems.lua").write_text(
        'return {\n'
        '  ["Metadata/Items/Gems/SkillGemArc"] = {\n'
        '    name = "Arc", baseTypeName = "Arc", gameId = "Metadata/Items/Gems/SkillGemArc",\n'
        '    variantId = "Arc", grantedEffectId = "Arc", tags = { spell = true, lightning = true },\n'
        '    tagString = "Spell, Lightning", reqStr = 0, reqDex = 0, reqInt = 100, naturalMaxLevel = 20,\n'
        '  },\n'
        '  ["Metadata/Items/Gems/SupportGemSpellEcho"] = {\n'
        '    name = "Spell Echo", baseTypeName = "Spell Echo Support",\n'
        '    gameId = "Metadata/Items/Gems/SupportGemSpellEcho", variantId = "SupportSpellEcho",\n'
        '    grantedEffectId = "SupportSpellEcho", tags = { support = true, spell = true },\n'
        '    tagString = "Support, Spell", reqStr = 0, reqDex = 0, reqInt = 100, naturalMaxLevel = 20,\n'
        '  },\n'
        '}\n',
        encoding="utf-8",
    )
    (poe1_data / "Skills" / "act_int.lua").write_text(
        'skills["Arc"] = { name = "Arc", baseTypeName = "Arc", color = 3, '
        'skillTypes = {}, statSets = { [1] = { baseEffectiveness = 1, incrementalEffectiveness = 0, '
        'constantStats = {}, stats = {}, levels = { [1] = { levelRequirement = 12 } } } }, '
        'levels = { [1] = { levelRequirement = 12 } } }\n',
        encoding="utf-8",
    )
    (poe1_data / "Skills" / "sup_int.lua").write_text(
        'skills["SupportSpellEcho"] = { name = "Spell Echo", support = true, color = 3, '
        'skillTypes = {}, statSets = { [1] = { constantStats = {}, stats = {}, levels = { [1] = { levelRequirement = 38 } } } }, '
        'levels = { [1] = { levelRequirement = 38 } } }\n',
        encoding="utf-8",
    )
    (poe1_data / "Bases" / "belt.lua").write_text(
        'itemBases["Heavy Belt"] = { type = "Belt", tags = { belt = true, default = true }, '
        'implicit = "+(25-35) to Strength", implicitModTypes = {}, req = { level = 8 } }\n',
        encoding="utf-8",
    )
    (poe1_data / "ModItem.lua").write_text("return {}\n", encoding="utf-8")
    (poe1_data / "Uniques" / "belt.lua").write_text(
        'return {\n[[\nMageblood\nHeavy Belt\nRequires Level 44\nImplicits: 1\n+(25-35) to Strength\n'
        'Magic Utility Flasks cannot be Used\n]],\n[[\nHeadhunter\nLeather Belt\nRequires Level 40\n'
        'Implicits: 1\n+(25-40) to maximum Life\n+(40-55) to Strength\n]],\n}\n',
        encoding="utf-8",
    )
    (poe1_data / "Uniques" / "jewel.lua").write_text(
        'return {\n[[\nCobalt Watch\nCobalt Jewel\nImplicits: 0\n]],\n'
        '[[\nBrutal Restraint\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'Denoted service of (500-8000) dekhara in the akhara of Balbala\nPassives in radius are Conquered by the Maraketh\nHistoric\n]],'
        '[[\nGlorious Vanity\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'Bathed in the blood of (100-8000) sacrificed in the name of Doryani\nPassives in radius are Conquered by the Vaal\nHistoric\n]],'
        '[[\nLethal Pride\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'Commanded leadership over (10000-18000) warriors under Kaom\nPassives in radius are Conquered by the Karui\nHistoric\n]],'
        '[[\nElegant Hubris\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'Commissioned (2000-160000) coins to commemorate Cadiro\nPassives in radius are Conquered by the Eternal Empire\nHistoric\n]],'
        '[[\nHeroic Tragedy\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'Remembrancing (100-8000) songworthy deeds by the line of Vorana\nPassives in radius are Conquered by the Kalguur\nHistoric\n]],'
        '[[\nExtinguishing Grasp\nTimeless Jewel\nLimited to: 1 Historic\nRadius: Large\nImplicits: 0\n'
        'tribute of (100-8000) souls to Ulaman\nPassives in radius are Conquered by the Abyss\nHistoric\n]],\n}\n',
        encoding="utf-8",
    )

    cn_dir = root / "Data" / "poe1" / "zh-rCN"
    tw_dir = root / "Data" / "poe1" / "zh-rTW"
    cn_dir.mkdir(parents=True)
    tw_dir.mkdir(parents=True)

    (cn_dir / "meta.json").write_text(json.dumps({"version": "1.0.0", "locale": "zh-rCN"}), encoding="utf-8")
    (cn_dir / "uniques.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Mageblood": "法师之血",
                    "Headhunter": "猎首",
                    "Brutal Restraint": "残酷的约束",
                    "Extinguishing Grasp": "熄灭之握",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (cn_dir / "items.json").write_text(
        json.dumps({"entries": {"Heavy Belt": "重革腰带", "Leather Belt": "皮革腰带"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (cn_dir / "gems.json").write_text(
        json.dumps(
            {"entries": {"Arc": "电弧", "Spell Echo": "法术回响（辅）", "Absolution of Inspiring": "赦免·启发"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (cn_dir / "passives.json").write_text(
        json.dumps({"entries": {"Warden": "守望者", "Hierophant": "圣宗"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (cn_dir / "ui.json").write_text(
        json.dumps({"entries": {"Life": "生命", "Combined DPS": "结合DPS"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (cn_dir / "tags.json").write_text(
        json.dumps({"entries": {"Potent": "强效的", "of Heat": "热火之"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    (tw_dir / "meta.json").write_text(json.dumps({"version": "1.0.0", "locale": "zh-rTW"}), encoding="utf-8")
    (tw_dir / "uniques.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Mageblood": "魔血",
                    "Headhunter": "獵首",
                    "Brutal Restraint": "殘酷的紀律",
                    "Extinguishing Grasp": "撲滅之握",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tw_dir / "items.json").write_text(
        json.dumps({"entries": {"Heavy Belt": "重革腰帶", "Leather Belt": "皮革腰帶"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tw_dir / "gems.json").write_text(
        json.dumps(
            {"entries": {"Arc": "電弧", "Spell Echo": "施放迴響輔助", "Spell Echo Support": "施放迴響輔助"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tw_dir / "gem_names.json").write_text(
        json.dumps({"entries": {"Absolution of Inspiring": "赦免．啟發"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tw_dir / "gem_names_by_id.json").write_text(
        json.dumps(
            {
                "entries": {"AbsolutionAltX": "赦免．啟發", "Arc": "電弧"},
                "templates": {"AbsolutionAltX": "Absolution of Inspiring", "Arc": "Arc"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tw_dir / "ascendancy_by_id.json").write_text(
        json.dumps(
            {"entries": {"Raider": "守林人", "Hierophant": "聖宗"}, "templates": {"Raider": "Warden", "Hierophant": "Hierophant"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tw_dir / "passives.json").write_text(
        json.dumps({"entries": {"Warden": "守護者", "Hierophant": "聖宗"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tw_dir / "ui.json").write_text(
        json.dumps({"entries": {"Life": "生命", "Combined DPS": "綜合秒傷"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tw_dir / "affixes.json").write_text(
        json.dumps({"entries": {"Potent": "強效的", "of Abjuration": "防護之"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tw_dir / "title_words.json").write_text(
        json.dumps({"entries": {"Keep": "堅城", "Gear": "齒輪"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def test_locale_cfg_l0_contracts_and_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert SUPPORTED_LANGS == ("zh-CN", "zh-TW", "en")
    assert DEFAULT_LANG == "zh-CN"

    # Verify locale_cfg.py has zero imports from pob_cli (pure stdlib L0)
    tree = ast.parse((PKG_DIR / "locale_cfg.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("pob_cli")
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith("pob_cli")

    for alias in ("zh", "zh-cn", "zh_cn", "zh-rcn", "cn", "chs", "simplified", "ZH-CN"):
        assert normalize_lang(alias) == "zh-CN"
    for alias in ("zh-tw", "zh_tw", "zh-rtw", "tw", "cht", "traditional", "ZH-TW"):
        assert normalize_lang(alias) == "zh-TW"
    for alias in ("en", "en-us", "english", "EN"):
        assert normalize_lang(alias) == "en"

    for bad in (None, "", "   ", "ko-KR", "ja"):
        with pytest.raises(ValueError, match="valid choices"):
            normalize_lang(bad)

    assert lang_to_data_subdir("zh-CN") == "zh-rCN"
    assert lang_to_data_subdir("tw") == "zh-rTW"
    assert lang_to_data_subdir("en") is None

    # Verify 100% parity with updater.paths.get_runtime_dir
    rt = tmp_path / "rt"
    monkeypatch.delenv("POB_LANG", raising=False)
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    assert resolve_runtime_dir_l0() == get_runtime_dir()
    assert resolve_runtime_dir_l0(tmp_path / "explicit") == get_runtime_dir(tmp_path / "explicit")

    monkeypatch.delenv("POB_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("POB_HOME", str(tmp_path / "home_dir"))
    assert resolve_runtime_dir_l0() == get_runtime_dir()

    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    assert get_active_lang() == ("zh-CN", "default")
    assert current_lang() == "zh-CN"

    # Seed an unrelated key in config.json and verify set_active_lang preserves it
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "config.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    res = set_active_lang("tw")
    assert res == {"lang": "zh-TW", "configPath": str(rt / "config.json")}
    saved = json.loads((rt / "config.json").read_text(encoding="utf-8"))
    assert saved == {"theme": "dark", "lang": "zh-TW"}
    assert get_active_lang() == ("zh-TW", "config")

    # POB_LANG env overrides config.json; invalid POB_LANG falls back to config.json
    monkeypatch.setenv("POB_LANG", "en")
    assert get_active_lang() == ("en", "env")
    monkeypatch.setenv("POB_LANG", "not-a-lang")
    assert get_active_lang() == ("zh-TW", "config")


@pytest.mark.skipif(_find_win_lua51_dir() is None, reason="No Windows PoE1/lua51.dll available")
def test_zh_translate_lua_synthetic_and_missing_fallback(tmp_path: Path) -> None:
    lua_dir = _find_win_lua51_dir()
    assert lua_dir is not None
    eng = _build_synthetic_dual_locale_engine(tmp_path / "dual_eng")
    no_tw_eng = tmp_path / "only_cn_eng"
    (no_tw_eng / "Data" / "poe1" / "zh-rCN").mkdir(parents=True)
    (no_tw_eng / "Data" / "poe1" / "zh-rCN" / "uniques.json").write_text(
        json.dumps({"entries": {"Mageblood": "法师之血"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    out_file = tmp_path / "zt_out.json"
    script = tmp_path / "run_zt.lua"
    script.write_text(
        f"""
local zt = assert(loadfile({json.dumps((LUA_DIR / "zh_translate.lua").as_posix())}))()
assert(zt.install_pre_bridge({json.dumps(eng.as_posix())}, {{ lang = "zh-CN" }}) == true)
assert(type(PobToolsSetTranslate) == "function" and type(PobToolsGetTranslate) == "function")
assert(PobToolsGetTranslate() == true)

assert(PobToolsTranslateDisplay("Mageblood") == "法师之血")
assert(PobToolsTranslateDisplay("^7Combined DPS") == "^7结合DPS")
assert(PobToolsTranslateDisplay("Mageblood, Heavy Belt") == "法师之血, 重革腰带")
assert(PobToolsAscendancyNameZh("Raider", "Warden") == "守望者")
assert(PobToolsItemNameZh({{ title = "Mageblood", baseName = "Heavy Belt (Variant)" }}) == "法师之血, 重革腰带")
assert(PobToolsItemNameZh({{ title = "Foulborn Mageblood", baseName = "Heavy Belt" }}) == "秽生 法师之血, 重革腰带")

-- Switch live to zh-TW via rpc_handlers
local r_tw = zt.rpc_handlers.set_locale({{ lang = "zh-TW" }})
assert(r_tw.ok == true and r_tw.lang == "zh-TW" and r_tw.translate == true and r_tw.dictLoaded == true)
assert(PobToolsTranslateDisplay("Mageblood") == "魔血")
assert(PobToolsTranslateDisplay("^xFFAA00Headhunter") == "^xFFAA00獵首")
assert(PobToolsTranslateDisplay("^7Combined DPS") == "^7綜合秒傷")
assert(PobToolsGemNameZh("AbsolutionAltX", "Absolution of Inspiring") == "赦免．啟發")
-- Raider id maps Warden -> 守林人 (NOT passives.json's 守護者!)
assert(PobToolsAscendancyNameZh("Raider", "Warden") == "守林人")
assert(PobToolsItemNameZh({{ title = "Keep Gear", baseName = "Heavy Belt" }}) == "堅城齒輪, 重革腰帶")
assert(PobToolsItemNameZh({{ baseName = "Heavy Belt", name = "Potent Heavy Belt of Abjuration" }}) == "強效的 重革腰帶 防護之")

-- Switch live to en -> instant pass-through & translate=false
local r_en = zt.rpc_handlers.set_locale({{ lang = "en" }})
assert(r_en.ok == true and r_en.lang == "en" and r_en.translate == false and r_en.dictLoaded == false)
assert(PobToolsGetTranslate() == false)
assert(PobToolsTranslateDisplay("Mageblood") == "Mageblood")
assert(PobToolsItemNameZh({{ title = "Mageblood", baseName = "Heavy Belt" }}) == nil)

-- Missing locale dir on disk falls back gracefully to English without error
zt.install_pre_bridge({json.dumps(no_tw_eng.as_posix())}, {{ lang = "zh-TW" }})
local r_miss = zt.set_locale("zh-TW")
assert(r_miss.ok == true and r_miss.lang == "zh-TW" and r_miss.dictLoaded == false)
assert(PobToolsTranslateDisplay("Mageblood") == "Mageblood")
local r_cn2 = zt.set_locale("zh-CN")
assert(r_cn2.dictLoaded == true and PobToolsTranslateDisplay("Mageblood") == "法师之血")

local f = assert(io.open({json.dumps(out_file.as_posix())}, "wb"))
f:write("PASS")
f:close()
""",
        encoding="utf-8",
    )
    rc = _run_lua_inproc(lua_dir, script)
    assert rc == 0
    assert out_file.read_text(encoding="utf-8") == "PASS"


@pytest.mark.skipif(
    _find_win_lua51_dir() is None
    or find_engine_data() is None
    or not (find_engine_data() / "Data" / "poe1" / "zh-rCN").is_dir()  # type: ignore[operator]
    or not (find_engine_data() / "Data" / "poe1" / "zh-rTW").is_dir(),  # type: ignore[operator]
    reason="Real dual-dictionary PoE1 engine (zh-rCN + zh-rTW + lua51.dll) not present",
)
def test_zh_translate_lua_real_dictionaries_speed_and_accuracy(tmp_path: Path) -> None:
    lua_dir = _find_win_lua51_dir()
    real_eng = find_engine_data()
    assert lua_dir is not None and real_eng is not None

    out_file = tmp_path / "real_zt.json"
    script = tmp_path / "bench_real_zt.lua"
    script.write_text(
        f"""
local zt = assert(loadfile({json.dumps((LUA_DIR / "zh_translate.lua").as_posix())}))()
zt.install_pre_bridge({json.dumps(real_eng.as_posix())}, {{ lang = "zh-CN" }})

local t0 = os.clock()
local cn_mb = PobToolsTranslateDisplay("Mageblood")
local cn_ms = (os.clock() - t0) * 1000
assert(cn_mb == "法师之血", "expected 法师之血, got " .. tostring(cn_mb))

local t1 = os.clock()
zt.set_locale("zh-TW")
local tw_mb = PobToolsTranslateDisplay("Mageblood")
local tw_ms = (os.clock() - t1) * 1000
assert(tw_mb == "魔血", "expected 魔血, got " .. tostring(tw_mb))
assert(PobToolsAscendancyNameZh("Raider", "Warden") == "守林人")

local f = assert(io.open({json.dumps(out_file.as_posix())}, "wb"))
f:write(string.format('{{"cnMs":%.2f,"twMs":%.2f}}', cn_ms, tw_ms))
f:close()
""",
        encoding="utf-8",
    )
    rc = _run_lua_inproc(lua_dir, script)
    assert rc == 0
    metrics = json.loads(out_file.read_text(encoding="utf-8"))
    assert metrics["cnMs"] < 80.0
    assert metrics["twMs"] < 80.0


def test_bridge_worker_set_locale_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    set_active_lang("en", runtime_dir=rt)

    worker = BridgeWorker("test", pob_dir=tmp_path / "fake_eng", runtime_dir=rt)
    # Calling set_locale before start() updates target locale without spawning subprocess
    pre = worker.set_locale("tw")
    assert pre == {"ok": True, "lang": "zh-TW", "translate": True, "started": False}
    assert worker.locale == "zh-TW"

    sent: list[dict[str, Any]] = []
    allow_set_locale = True

    class _FakeStdin:
        def write(self, data: str) -> int:
            for line in data.splitlines():
                if line.strip():
                    sent.append(json.loads(line))
            return len(data)

        def flush(self) -> None:
            req = sent[-1]
            m = req["method"]
            if m == "set_locale":
                if allow_set_locale:
                    lang = req["params"]["lang"]
                    worker._stdout_queue.put(
                        {"id": req["id"], "result": {"ok": True, "lang": lang, "translate": lang != "en", "dictLoaded": lang != "en"}}
                    )
                else:
                    worker._stdout_queue.put({"id": req["id"], "error": {"message": "unknown method: set_locale"}})
            elif m == "set_translate":
                worker._stdout_queue.put({"id": req["id"], "result": {"enabled": bool(req["params"]["enabled"])}})

    class _FakeProc:
        stdin = _FakeStdin()

        def poll(self) -> None:
            return None

    worker._proc = _FakeProc()  # type: ignore[assignment]
    worker.caps = {"translateToggle": True, "localeSwitch": True}

    r1 = worker.set_locale("en")
    assert r1["lang"] == "en" and r1["translate"] is False
    assert worker.translate_enabled is False
    assert worker.locale == "en"

    # Legacy pob-zh.exe fallback when set_locale RPC returns unknown method
    allow_set_locale = False
    r2 = worker.set_locale("zh-CN")
    assert r2 == {"ok": True, "lang": "zh-CN", "translate": True, "fallback": "set_translate"}
    assert worker.translate_enabled is True


def test_dual_locale_game_data_zh_match_cn_zh2en_and_timeless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"
    eng = _build_synthetic_dual_locale_engine(rt / "engine")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.delenv("POB_LANG", raising=False)

    avail_names = [d.name for d in list_available_zh_dirs(eng)]
    assert avail_names == ["zh-rCN", "zh-rTW"]

    # 1. Default zh-CN index keeps standard DB filename (no _zh_rtw suffix)
    _, zh_cn_dir = _resolve_poe1_data_dirs(eng, runtime_dir=rt)
    assert zh_cn_dir is not None and zh_cn_dir.name == "zh-rCN"
    db_cn = ensure_gamedata_index(pob_dir=eng, runtime_dir=rt)
    assert not db_cn.name.endswith("_zh_rtw.sqlite")
    res_cn = query_local_data("unique", "Mageblood", pob_dir=eng, runtime_dir=rt)
    assert res_cn["results"][0]["zh_name"] == "法师之血"

    # Both Simplified AND Traditional names resolve to canonical English under zh-CN!
    _english_for_zh_cached.cache_clear()
    assert english_for_zh("法师之血", pob_dir=eng) == "Mageblood"
    assert english_for_zh("魔血", pob_dir=eng) == "Mageblood"
    assert english_for_zh("残酷的约束", pob_dir=eng) == "Brutal Restraint"
    assert english_for_zh("殘酷的紀律", pob_dir=eng) == "Brutal Restraint"
    assert english_for_zh("撲滅之握", pob_dir=eng) == "Extinguishing Grasp"

    d_cn = Zh2EnDictionary.from_engine_dir(eng)
    assert d_cn.translate_unique("法师之血") == "Mageblood"
    assert d_cn.translate_unique("魔血") == "Mageblood"
    assert d_cn.translate_gem("施放迴響輔助", is_support=True) == "Spell Echo Support"
    assert d_cn.translate_gem("赦免．啟發") == "Absolution of Inspiring"

    # 2. Switch to zh-TW -> separate *_zh_rtw.sqlite DB, preserving db_cn on disk!
    set_active_lang("zh-TW", runtime_dir=rt)
    _, zh_tw_dir = _resolve_poe1_data_dirs(eng, runtime_dir=rt)
    assert zh_tw_dir is not None and zh_tw_dir.name == "zh-rTW"
    db_tw = ensure_gamedata_index(pob_dir=eng, runtime_dir=rt)
    assert db_tw.name.endswith("_zh_rtw.sqlite")
    assert db_cn.is_file()
    res_tw = query_local_data("unique", "Mageblood", pob_dir=eng, runtime_dir=rt)
    assert res_tw["results"][0]["zh_name"] == "魔血"

    # Both Simplified AND Traditional names STILL resolve to canonical English under zh-TW!
    _english_for_zh_cached.cache_clear()
    assert english_for_zh("魔血", pob_dir=eng) == "Mageblood"
    assert english_for_zh("法师之血", pob_dir=eng) == "Mageblood"
    assert english_for_zh("殘酷的紀律", pob_dir=eng) == "Brutal Restraint"
    assert english_for_zh("残酷的约束", pob_dir=eng) == "Brutal Restraint"
    assert english_for_zh("熄灭之握", pob_dir=eng) == "Extinguishing Grasp"

    tr_from_cn = query_local_data("translate", "法师之血", pob_dir=eng, runtime_dir=rt)
    assert tr_from_cn["results"][0]["en"] == "Mageblood"
    assert tr_from_cn["results"][0]["zh"] == "魔血"

    d_tw = Zh2EnDictionary.from_engine_dir(eng)
    assert d_tw.translate_unique("魔血") == "Mageblood"
    assert d_tw.translate_unique("法师之血") == "Mageblood"
    assert d_tw.translate_gem("法术回响（辅）", is_support=True) == "Spell Echo Support"

    # 3. Timeless jewel display names and aliases across zh-CN, zh-TW, and en
    jewels = load_jewels(eng / "PoE1")
    assert _local_jewel_name("Brutal Restraint", {}) == "殘酷的紀律"
    assert _local_jewel_name("Glorious Vanity", {}) == "輝煌的虛榮"
    set_active_lang("zh-CN", runtime_dir=rt)
    assert _local_jewel_name("Brutal Restraint", {}) == "残酷的约束"
    assert _local_jewel_name("Glorious Vanity", {}) == "光彩夺目"
    set_active_lang("en", runtime_dir=rt)
    assert _local_jewel_name("Brutal Restraint", {}) == "Brutal Restraint"

    for alias, expected in (
        ("残酷的约束", "Brutal Restraint"),
        ("殘酷的約束", "Brutal Restraint"),
        ("殘酷的紀律", "Brutal Restraint"),
        ("光彩夺目", "Glorious Vanity"),
        ("光彩奪目", "Glorious Vanity"),
        ("军团的荣耀", "Glorious Vanity"),
        ("軍團的榮耀", "Glorious Vanity"),
        ("致命的骄傲", "Lethal Pride"),
        ("致命的驕傲", "Lethal Pride"),
        ("优雅的傲慢", "Elegant Hubris"),
        ("優雅的傲慢", "Elegant Hubris"),
        ("英雄悲剧", "Heroic Tragedy"),
        ("英雄悲劇", "Heroic Tragedy"),
        ("撲滅之握", "Extinguishing Grasp"),
    ):
        assert _resolve_jewel_static(alias, jewels) == expected

    # 4. Missing Chinese locale directory fallback (zh-rTW <-> zh-rCN)
    only_cn = tmp_path / "only_cn"
    (only_cn / "PoE1" / "Data").mkdir(parents=True)
    (only_cn / "Data" / "poe1" / "zh-rCN").mkdir(parents=True)
    _, fb_cn = _resolve_poe1_data_dirs(only_cn, lang="zh-TW")
    assert fb_cn is not None and fb_cn.name == "zh-rCN"

    only_tw = tmp_path / "only_tw"
    (only_tw / "PoE1" / "Data").mkdir(parents=True)
    (only_tw / "Data" / "poe1" / "zh-rTW").mkdir(parents=True)
    _, fb_tw = _resolve_poe1_data_dirs(only_tw, lang="zh-CN")
    assert fb_tw is not None and fb_tw.name == "zh-rTW"

    # 5. Isolated slot POB_RUNTIME_DIR config.json wins over shared POB_ROOT parent config.json
    set_active_lang("zh-CN", runtime_dir=rt)  # shared parent of `eng` has zh-CN
    slot_rt = tmp_path / "slot_tw_rt"
    set_active_lang("zh-TW", runtime_dir=slot_rt)
    monkeypatch.setenv("POB_RUNTIME_DIR", str(slot_rt))
    _, slot_zh = _resolve_poe1_data_dirs(eng)
    assert slot_zh is not None and slot_zh.name == "zh-rTW"
    q_slot = query_local_data("unique", "法师之血", pob_dir=eng)
    assert q_slot["count"] == 1 and q_slot["results"][0]["zh_name"] == "魔血"
