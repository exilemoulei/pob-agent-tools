"""Tests for locale-aware `<Localized> (<English>)` formatting in main CLI/Markdown formatters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob_cli import cli_build_io, cli_inspect_parts, cli_optimize
from pob_cli.cli import main
from pob_cli.formatters import (
    clear_bilingual_cache,
    format_bilingual,
    format_compare_markdown,
    format_diagnose_markdown,
    format_mutation_report,
    lookup_localized_name,
)


def _make_dual_zh_engine(root: Path) -> Path:
    (root / "PoE1" / "Data").mkdir(parents=True)
    cn = root / "Data" / "poe1" / "zh-rCN"
    tw = root / "Data" / "poe1" / "zh-rTW"
    cn.mkdir(parents=True)
    tw.mkdir(parents=True)

    (cn / "meta.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (cn / "gems.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Enlighten": "启蒙(辅)",
                    "Arc": "电弧",
                    "Discipline": "纪律",
                    "Firestorm of Pelting": "碎焰之烈炎风暴",
                }
            }
        ),
        encoding="utf-8",
    )
    (cn / "uniques.json").write_text(
        json.dumps({"entries": {"The Ivory Tower": "象牙之塔", "Ephemeral Edge": "影殒"}}),
        encoding="utf-8",
    )
    (cn / "items.json").write_text(
        json.dumps({"entries": {"Saint's Hauberk": "圣者链甲", "Dusk Blade": "暮色之刃"}}),
        encoding="utf-8",
    )
    (cn / "passives.json").write_text(
        json.dumps({"entries": {"Reliquarian": "守藏者", "Scion": "贵族"}}),
        encoding="utf-8",
    )

    (tw / "meta.json").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (tw / "gems.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Enlighten Support": "啟蒙輔助",
                    "Arc": "電弧",
                    "Discipline": "紀律",
                    "Firestorm of Pelting": "烈炎風暴．猛擲",
                }
            }
        ),
        encoding="utf-8",
    )
    (tw / "uniques.json").write_text(
        json.dumps({"entries": {"The Ivory Tower": "象牙塔", "Ephemeral Edge": "影殞"}}),
        encoding="utf-8",
    )
    (tw / "items.json").write_text(
        json.dumps({"entries": {"Saint's Hauberk": "聖者鏈甲", "Dusk Blade": "暮色之刃"}}),
        encoding="utf-8",
    )
    (tw / "ascendancy_by_id.json").write_text(
        json.dumps({"entries": {"Reliquarian": "遺守使徒", "Scion": "貴族"}}),
        encoding="utf-8",
    )
    return root


def test_bilingual_gem_key_normalization_and_locales(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_dual_zh_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    clear_bilingual_cache()

    # zh-CN: gems.json omits " Support" suffix -> still resolves "Enlighten Support"
    assert format_bilingual("Enlighten Support", kind="gem", lang="zh-CN") == "启蒙(辅) (Enlighten Support)"
    assert format_bilingual("Vaal Discipline", kind="gem", lang="zh-CN") == "瓦尔：纪律 (Vaal Discipline)"
    assert format_bilingual("Firestorm of Pelting", kind="gem", lang="zh-CN") == "碎焰之烈炎风暴 (Firestorm of Pelting)"
    assert format_bilingual("The Ivory Tower", kind="unique", lang="zh-CN") == "象牙之塔 (The Ivory Tower)"

    # zh-TW: gems.json includes " Support" suffix -> resolves both "Enlighten" and "Enlighten Support"
    assert format_bilingual("Enlighten Support", kind="gem", lang="zh-TW") == "啟蒙輔助 (Enlighten Support)"
    assert format_bilingual("Enlighten", kind="gem", lang="zh-TW") == "啟蒙輔助 (Enlighten)"
    assert format_bilingual("Vaal Discipline", kind="gem", lang="zh-TW") == "瓦爾．紀律 (Vaal Discipline)"
    assert format_bilingual("The Ivory Tower", kind="unique", lang="zh-TW") == "象牙塔 (The Ivory Tower)"
    assert format_bilingual("Reliquarian", kind="asc", lang="zh-TW") == "遺守使徒 (Reliquarian)"

    # en: returns pure English even if zh_name is supplied
    assert format_bilingual("Enlighten Support", "啟蒙輔助", kind="gem", lang="en") == "Enlighten Support"
    assert lookup_localized_name("The Ivory Tower", kind="unique", lang="en") is None

    # Idempotency: never double-wrap
    already = "啟蒙輔助 (Enlighten Support)"
    assert format_bilingual(already, kind="gem", lang="zh-TW") == already
    assert format_bilingual("Enlighten Support", already, kind="gem", lang="zh-TW") == already


def test_bilingual_fallback_without_zh_dictionaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "bare_eng"
    (bare / "PoE1" / "Data").mkdir(parents=True)
    monkeypatch.setenv("POB_ROOT", str(bare))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("POB_LANG", "zh-TW")
    clear_bilingual_cache()

    assert format_bilingual("Enlighten Support", kind="gem") == "Enlighten Support"
    assert format_bilingual("The Ivory Tower", kind="unique") == "The Ivory Tower"
    assert format_bilingual("The Ivory Tower", "The Ivory Tower", kind="unique") == "The Ivory Tower"
    # If caller explicitly supplies a translated zh_name, still formats cleanly
    assert format_bilingual("The Ivory Tower", "象牙塔", kind="unique") == "象牙塔 (The Ivory Tower)"


def test_bilingual_integrated_into_main_formatters_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _make_dual_zh_engine(tmp_path / "eng")
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("POB_LANG", "zh-TW")
    clear_bilingual_cache()

    diag_md = format_diagnose_markdown(
        {
            "buildInfo": {"level": 95, "className": "Scion", "ascendClassName": "Reliquarian"},
            "mainGroup": {
                "index": 1,
                "displayLabel": "Arc",
                "slot": "Body Armour",
                "gems": [
                    {"name": "Arc", "level": 20, "quality": 20, "enabled": True},
                    {"name": "Enlighten Support", "level": 4, "quality": 0, "enabled": True},
                ],
            },
            "equippedItems": [
                {"slot": "Body Armour", "name": "The Ivory Tower", "rarity": "UNIQUE"},
                {"slot": "Weapon 1", "name": "Ephemeral Edge", "rarity": "UNIQUE"},
            ],
        }
    )
    assert "貴族 (Scion) / 遺守使徒 (Reliquarian)" in diag_md
    assert "啟蒙輔助 (Enlighten Support)" in diag_md
    assert "象牙塔 (The Ivory Tower)" in diag_md
    assert "影殞 (Ephemeral Edge)" in diag_md

    cmp_md = format_compare_markdown(
        {
            "mainBuildInfo": {"level": 95, "ascendClassName": "Reliquarian"},
            "refBuildInfo": {"level": 96, "ascendClassName": "Reliquarian"},
            "skillDiff": {"mainGroupMain": {"index": 1, "displayLabel": "Arc"}, "sharedActiveSkills": ["Arc"]},
            "itemSlotDiff": [{"slot": "Body Armour", "mainItem": "The Ivory Tower", "refItem": "Ephemeral Edge"}],
        }
    )
    assert "遺守使徒 (Reliquarian)" in cmp_md
    assert "象牙塔 (The Ivory Tower)" in cmp_md

    mut_md = format_mutation_report(
        "Item Try",
        {
            "mode": "what-if",
            "before": {"primaryDps": 1000, "totalEhp": 5000, "life": 1000},
            "after": {"primaryDps": 1200, "totalEhp": 5500, "life": 1000},
            "detail": {"item": {"title": "The Ivory Tower", "baseName": "Saint's Hauberk", "rarity": "UNIQUE"}},
        },
    )
    assert "象牙塔 (The Ivory Tower)" in mut_md
    assert "聖者鏈甲 (Saint's Hauberk)" in mut_md

    runner = CliRunner()
    monkeypatch.setattr(
        cli_inspect_parts,
        "invoke_daemon",
        lambda action, _params=None: (
            {
                "equippedSlots": [
                    {
                        "slotName": "Body Armour",
                        "item": {"title": "The Ivory Tower", "baseName": "Saint's Hauberk", "rarity": "UNIQUE"},
                    }
                ]
            }
            if action == "items"
            else {
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "displayLabel": "Arc",
                        "slot": "Body Armour",
                        "gems": [{"name": "Enlighten Support", "enabled": True}],
                    }
                ]
            }
        ),
    )
    res_items = runner.invoke(main, ["items"])
    assert res_items.exit_code == 0, res_items.output
    assert "象牙塔 (The Ivory Tower)" in res_items.output
    assert "聖者鏈甲 (Saint's Hauberk)" in res_items.output

    # JSON output preserves raw English machine keys
    res_items_json = runner.invoke(main, ["items", "--json"])
    assert res_items_json.exit_code == 0
    raw_items = json.loads(res_items_json.output)
    assert raw_items["equippedSlots"][0]["item"]["title"] == "The Ivory Tower"

    res_skills = runner.invoke(main, ["skills"])
    assert res_skills.exit_code == 0, res_skills.output
    assert "啟蒙輔助 (Enlighten Support)" in res_skills.output

    monkeypatch.setattr(
        cli_optimize,
        "invoke_daemon",
        lambda *_a, **_kw: {
            "groupIndex": 1,
            "groupLabel": "Arc",
            "mode": "add",
            "dpsField": "CombinedDPS",
            "baseDps": 1000,
            "candidates": [{"name": "Enlighten Support", "dps": 1100, "delta": 100, "pct": 10.0}],
        },
    )
    res_rank = runner.invoke(main, ["rank-gems"])
    assert res_rank.exit_code == 0, res_rank.output
    assert "啟蒙輔助" in res_rank.output
    assert "Enlighten Support" in res_rank.output

    monkeypatch.setattr(
        cli_build_io,
        "invoke_daemon",
        lambda *_a, **_kw: {
            "importMethod": "code",
            "buildInfo": {"className": "Scion", "ascendClassName": "Reliquarian", "level": 95, "points": {}},
            "buildHeader": {
                "mainSocketGroup": {"index": 1, "list": [{"label": "Arc"}]},
                "mainSkill": {"index": 1, "list": [{"label": "Arc"}]},
            },
        },
    )
    res_load = runner.invoke(main, ["load", "dummy_code"])
    assert res_load.exit_code == 0, res_load.output
    assert "遺守使徒 (Reliquarian)" in res_load.output
    assert "電弧 (Arc)" in res_load.output


def test_bilingual_caches_locale_and_zh_dir_and_tracks_env_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pob_cli.formatters.bilingual as bi_mod

    eng = _make_dual_zh_engine(tmp_path / "eng")
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_ROOT", str(eng))
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("POB_LANG", "zh-TW")
    clear_bilingual_cache()

    calls = {"dir": 0}
    orig_resolve = bi_mod._resolve_zh_dir_for_lang

    def _counting_resolve(*args, **kwargs):
        calls["dir"] += 1
        return orig_resolve(*args, **kwargs)

    monkeypatch.setattr(bi_mod, "_resolve_zh_dir_for_lang", _counting_resolve)

    for _ in range(10):
        assert format_bilingual("The Ivory Tower", kind="unique") == "象牙塔 (The Ivory Tower)"
    assert calls["dir"] == 1

    # Switching POB_LANG takes effect immediately without manual cache clear
    monkeypatch.setenv("POB_LANG", "zh-CN")
    assert format_bilingual("The Ivory Tower", kind="unique") == "象牙之塔 (The Ivory Tower)"
    assert calls["dir"] == 2

    monkeypatch.setenv("POB_LANG", "en")
    assert format_bilingual("The Ivory Tower", kind="unique") == "The Ivory Tower"
