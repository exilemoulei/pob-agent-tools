"""Offline unit tests for the clean-room T16/T17 map modifier corpus and zh-CN overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pob_cli.map_regex import load_corpus, resolve_preset_ids

_DATA_JSON = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "data" / "map_mods_3_30.json"


def test_corpus_json_size_and_license_metadata() -> None:
    raw_bytes = _DATA_JSON.read_bytes()
    assert len(raw_bytes) <= 35_000, f"map_mods_3_30.json is {len(raw_bytes)}B (>35KB budget)"
    data = json.loads(raw_bytes.decode("utf-8"))
    assert data["meta"]["modCount"] >= 40
    assert any("awakened-poe-trade" in s for s in data["meta"]["sources"])
    assert len(data["safeCorpus"]["mapNames"]["en"]) >= 100


def test_runtime_cn_overlay_uses_tencent_stats_json_and_degrades_cleanly(tmp_path: Path) -> None:
    stats_dir = tmp_path / "Data" / "poe1" / "zh-rCN"
    stats_dir.mkdir(parents=True)
    (stats_dir / "stats.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Area contains Drowning Orbs": "区域内有淹没法球（国服实时词库）",
                    "Monsters cannot be Leeched from": "怪物无法被偷取",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corp = load_corpus("zh-CN", engine_root=tmp_path, use_cn_overlay=True)
    assert corp["corpusVersion"] == "3.30"
    assert corp["langOverlay"] == "zh-rCN"
    assert corp["cnOverlayActive"] is True
    assert corp["modsById"]["drowning_orbs"]["lines"] == ["区域内有淹没法球（国服实时词库）"]
    assert corp["modsById"]["no_leech"]["lines"] == ["怪物无法被偷取"]

    # Missing engine root degrades cleanly without raising
    missing_corp = load_corpus("zh-CN", engine_root=tmp_path / "nonexistent", use_cn_overlay=True)
    assert missing_corp["corpusVersion"] == "3.30"
    assert missing_corp["langOverlay"] == "none"
    assert missing_corp["cnOverlayActive"] is False
    assert missing_corp["modsById"]["drowning_orbs"]["lines"] == ["区域内有淹没法球"]


def test_bilingual_preset_resolution_and_unknown_error() -> None:
    corpus = load_corpus("zh-CN", use_cn_overlay=False)
    assert resolve_preset_ids("元反,无回复,drowning", corpus) == ["reflect_ele", "no_regen", "drowning_orbs"]
    assert resolve_preset_ids("溺亡之球", corpus) == ["drowning_orbs"]
    with pytest.raises(ValueError, match="Available presets:"):
        resolve_preset_ids("totally_nonexistent_mod", corpus)
