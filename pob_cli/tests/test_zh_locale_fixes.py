"""Locale regressions: zh-rTW id tables, Simplified-first matching, `pob lang` validation and sync."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_zh_locale_switch import _build_synthetic_dual_locale_engine

import pob_cli.cli_lifecycle as cli_lifecycle
import pob_cli.daemon as daemon_pkg
from pob_cli.cli import main
from pob_cli.daemon.slot_updates import handle_set_lang_action
from pob_cli.game_data import ensure_gamedata_index, query_local_data
from pob_cli.game_data.zh_match import _english_for_zh_cached, english_for_zh
from pob_cli.locale_cfg import get_active_lang, invalid_env_lang, set_active_lang
from pob_cli.profile_import.cn_zh2en import Zh2EnDictionary


def _write_entries(path: Path, entries: dict[str, str], **extra: Any) -> None:
    path.write_text(json.dumps({"entries": entries, **extra}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def dual_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    rt = tmp_path / "rt"
    eng = _build_synthetic_dual_locale_engine(rt / "engine")
    cn = eng / "Data" / "poe1" / "zh-rCN"
    tw = eng / "Data" / "poe1" / "zh-rTW"
    # Same characters after folding, different items: the Simplified name must stay Sorcerer Gloves.
    _write_entries(cn / "items.json", {"Heavy Belt": "重革腰带", "Sorcerer Gloves": "术士手套"})
    _write_entries(
        tw / "items.json",
        {"Heavy Belt": "重革腰帶", "Warlock Gloves": "術士手套", "Sorcerer Gloves": "術者手套"},
    )
    # The real zh-rTW folder also ships id-keyed tables and lookup configs next to the name files.
    _write_entries(tw / "passives_by_id.json", {"30940": "聖宗", "6": "雙重懼刃"}, templates={})
    _write_entries(tw / "passive_stats_by_id.json", {"27": "map_beyond_portal_search_radius_+%"})
    _write_entries(tw / "mod_stats_by_id.json", {"AbyssAccuracy": "accuracy_rating local_life_+%"})
    _write_entries(tw / "ggpk_full.json", {"Hierophant Banner": "聖宗旗"})
    (tw / "synonyms.json").write_text(json.dumps({"replacements": {"聖": "圣"}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.delenv("POB_LANG", raising=False)
    _english_for_zh_cached.cache_clear()
    yield eng, rt
    _english_for_zh_cached.cache_clear()


def test_zh_tw_index_skips_id_tables(dual_engine: tuple[Path, Path]) -> None:
    eng, rt = dual_engine
    set_active_lang("zh-TW", runtime_dir=rt)
    db = ensure_gamedata_index(pob_dir=eng, runtime_dir=rt)
    assert db.name.endswith("_zh_rtw.sqlite")
    with sqlite3.connect(db) as conn:
        ens = [r[0] for r in conn.execute("SELECT en FROM translations")]
    assert ens and not any(e.isdigit() or ("_" in e and " " not in e) for e in ens)
    assert "Hierophant Banner" not in ens
    res = query_local_data("translate", "圣宗", pob_dir=eng, runtime_dir=rt)
    assert res["results"][0]["en"] == "Hierophant"
    assert english_for_zh("圣宗", pob_dir=eng) == "Hierophant"
    assert english_for_zh("聖宗", pob_dir=eng) == "Hierophant"


@pytest.mark.parametrize("lang", ["zh-CN", "zh-TW", "en"])
def test_simplified_names_resolve_by_zh_rcn_under_any_display_lang(
    dual_engine: tuple[Path, Path], lang: str
) -> None:
    eng, rt = dual_engine
    set_active_lang(lang, runtime_dir=rt)
    assert english_for_zh("术士手套", pob_dir=eng) == "Sorcerer Gloves"
    assert english_for_zh("術士手套", pob_dir=eng) == "Warlock Gloves"
    assert english_for_zh("術者手套", pob_dir=eng) == "Sorcerer Gloves"
    assert english_for_zh("魔血", pob_dir=eng) == "Mageblood"
    top = query_local_data("translate", "术士手套", pob_dir=eng, runtime_dir=rt)["results"][0]
    assert top["en"] == "Sorcerer Gloves"
    # CN-server imports are Simplified whatever the display language is.
    assert Zh2EnDictionary.from_engine_dir(eng).translate_base("术士手套") == "Sorcerer Gloves"


def test_invalid_pob_lang_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("POB_LANG", "zh-Hant-test")
    assert invalid_env_lang() == "zh-Hant-test"
    assert get_active_lang(tmp_path) == ("zh-CN", "default")
    assert get_active_lang(tmp_path) == ("zh-CN", "default")
    err = capsys.readouterr().err
    assert err.count("ignoring POB_LANG='zh-Hant-test'") == 1
    monkeypatch.setenv("POB_LANG", "tw")
    assert invalid_env_lang() is None


@pytest.fixture
def lang_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_LANG", raising=False)
    monkeypatch.setattr(cli_lifecycle, "_installed_locales", lambda _rt: ["zh-CN", "en"])
    monkeypatch.setattr(daemon_pkg, "read_daemon_state", lambda _rt=None: None)
    return tmp_path


def test_lang_rejects_locale_missing_from_engine(lang_cli: Path) -> None:
    res = CliRunner().invoke(main, ["lang", "zh-TW"])
    assert res.exit_code == 1
    assert "Error: Language 'zh-TW' is not installed" in res.output
    assert not (lang_cli / "config.json").exists()

    ok = CliRunner().invoke(main, ["lang", "zh-CN", "--json"])
    assert ok.exit_code == 0, ok.output
    payload = json.loads(ok.output)
    assert payload["lang"] == "zh-CN" and payload["daemonRunning"] is False
    assert payload["availableLocales"] == ["zh-CN", "en"]


def test_lang_reports_ignored_env(lang_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_LANG", "xx")
    payload = json.loads(_json_out(["lang", "--json"]))
    assert payload["envIgnored"] == "xx" and payload["source"] == "default"


def _json_out(argv: list[str]) -> str:
    out = CliRunner().invoke(main, argv).stdout
    return out[out.index("{") :]


def test_lang_reports_daemon_failure_and_dict_state(lang_cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_pkg, "read_daemon_state", lambda _rt=None: {"pid": 1})

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(daemon_pkg, "call_daemon", _boom)
    res = CliRunner().invoke(main, ["lang", "zh-CN"])
    assert res.exit_code == 5
    assert "did not answer the language request: connection reset" in res.output

    sent: list[dict[str, Any]] = []

    def _ok(method: str, params: dict[str, Any], **_k: Any) -> dict[str, Any]:
        sent.append(params)
        return {
            "effectiveLang": "zh-CN",
            "availableLocales": ["zh-CN", "en"],
            "slotsUpdated": {"main": {"ok": True, "lang": "zh-CN", "dictLoaded": True}},
        }

    monkeypatch.setattr(daemon_pkg, "call_daemon", _ok)
    payload = json.loads(_json_out(["lang", "zh-CN", "--json"]))
    assert sent[-1] == {"lang": "zh-CN", "persist": False}
    assert payload["daemonSynced"] is True and payload["dictLoaded"] is True


class _FakeManager:
    def __init__(self, rt: Path, workers: dict[str, Any]) -> None:
        self.runtime_dir = rt
        self.pob_dir = None
        self._workers = workers
        self._lock = threading.Lock()

    def _exclusive_barrier(self) -> Any:
        return nullcontext()

    def _reader_barrier(self) -> Any:
        return nullcontext()

    def _get_slot_lock(self, _name: str) -> Any:
        return nullcontext()


class _FakeWorker:
    def __init__(self) -> None:
        self.langs: list[str] = []

    def set_locale(self, lang: str) -> dict[str, Any]:
        self.langs.append(lang)
        return {"ok": True, "lang": lang, "dictLoaded": True}


def test_daemon_explicit_switch_beats_start_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_LANG", "zh-TW")  # what the daemon was started with
    worker = _FakeWorker()
    out = handle_set_lang_action(_FakeManager(tmp_path, {"main": worker}), {"lang": "zh-CN", "persist": False})
    assert worker.langs == ["zh-CN"]
    assert out["effectiveLang"] == "zh-CN" and out["daemonEnvOverridden"] == "zh-TW"
    assert get_active_lang(tmp_path)[0] == "zh-CN"
