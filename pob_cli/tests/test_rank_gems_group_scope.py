"""`rank-gems` judges a support against the main skill's stats only when it ranks the main group."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli, comparator


class _IgniteMainWorker:
    """Main group 1 is an ignite skill; group 2 is a hit skill the main stats say nothing about."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stats_calls = 0

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {"index": 1, "isMain": True, "displayLabel": "Fireball", "gems": [{"index": 1, "name": "Fireball"}]},
                    {"index": 2, "isMain": False, "displayLabel": "Arc", "gems": [{"index": 1, "name": "Arc"}]},
                ],
            }
        if method == "get_stats":
            self.stats_calls += 1
            return {"stats": {"IgniteDPS": 900_000.0, "HitDPS": 50_000.0, "ManaCost": 0.0}}
        if method == "get_gem_options":
            return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
        if method == "gem_search":
            return {
                "dpsField": "CombinedDPS",
                "baseDps": 1_000_000,
                "gems": [
                    {"name": "Elemental Focus Support", "delta": 300_000, "pct": 30.0},
                    {"name": "Inspiration Support", "delta": 100_000, "pct": 10.0},
                ],
            }
        return {"ok": True}


def _names(res: dict[str, Any], key: str) -> list[str]:
    return [str(c.get("name")) for c in res.get(key) or []]


def test_main_group_still_drops_a_support_that_bricks_the_main_skill() -> None:
    res = comparator.rank_gems(_IgniteMainWorker(), group_selector="1")  # type: ignore[arg-type]
    assert _names(res, "excludedSupports") == ["Elemental Focus Support"]
    assert _names(res, "candidates") == ["Inspiration Support"]


def test_another_group_is_not_filtered_or_annotated_by_the_main_skills_stats() -> None:
    worker = _IgniteMainWorker()
    res = comparator.rank_gems(worker, group_selector="2")  # type: ignore[arg-type]
    assert "excludedSupports" not in res
    assert _names(res, "candidates") == ["Elemental Focus Support", "Inspiration Support"]
    # Inspiration's "0 charges on 0-mana skill" caveat comes from the main skill's mana cost.
    assert "caveat" not in res["candidates"][1]
    assert worker.stats_calls == 0


def test_rank_gems_markdown_names_the_supports_it_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    res = comparator.rank_gems(_IgniteMainWorker(), group_selector="1")  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_daemon_call", lambda *_a, **_k: res)
    out = CliRunner().invoke(cli.main, ["rank-gems"])
    assert out.exit_code == 0, out.output
    assert "Excluded conflicting supports: Elemental Focus Support (Cannot inflict Elemental Ailments" in out.output
