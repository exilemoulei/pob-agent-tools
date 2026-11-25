"""Tests for `pob data mod` drop-only guidance, full stat-template resolution for short/affix/multi-line queries,
multi-word slot quoting, `dropOnlyReason` semantics, and SKILL.md trade Ground Truth + offline fallback rules."""

from __future__ import annotations

from pathlib import Path

from test_data_mod_droponly_and_slot import _write_synthetic_poe1_mod_data

from pob_cli import game_data
from pob_cli.formatters import format_game_data
from pob_cli.game_data.mod_guidance import _build_trade_stat_arg


def test_drop_only_guidance_expands_short_query_and_quotes_multiword_slot(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    res = game_data.query_local_data("mod", "minion gems", pob_dir=pob_root, runtime_dir=rt, slot="gloves")
    assert res["count"] == 1
    assert res["dropOnlyReason"] == "excluded-by-slot-table"
    cands = res["dropOnlyCandidates"]
    assert {"Citaqualotl's", "Subterranean", "Summoner's"} <= {c.get("affix") for c in cands}
    assert all(c["reason"] == "excluded-by-slot-table" for c in cands)
    note = res["dropOnlyNote"]
    assert 'pob trade search --slot gloves --stat "+# to Level of Socketed Minion Gems>=2" --fetch-limit 5 --pob off' in note
    assert "unverified (offline)" in note

    res_body = game_data.query_local_data("mod", "minion gems", pob_dir=pob_root, runtime_dir=rt, slot="Body Armour")
    assert '--slot "Body Armour" --stat "+# to Level of Socketed Minion Gems>=2"' in res_body["dropOnlyNote"]

    md = format_game_data(res, kind="mod", query="minion gems")
    assert "DROP-ONLY / SLOT-BLIND-SPOT" in md
    assert "Citaqualotl's" in md and "Subterranean" in md


def test_drop_only_guidance_resolves_affix_and_multiline_stat_queries(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    res_zero = game_data.query_local_data("mod", "Citaqualotl", pob_dir=pob_root, runtime_dir=rt, slot="gloves")
    assert res_zero["count"] == 0
    assert res_zero["dropOnlyReason"] == "excluded-by-slot-table"
    hint = res_zero.get("hint") or ""
    assert '--stat "+# to Level of Socketed Minion Gems>=2" --fetch-limit 5 --pob off' in hint
    assert '--stat "Citaqualotl"' not in hint

    res_range = game_data.query_local_data("mod", "of Citaqualotl", pob_dir=pob_root, runtime_dir=rt, slot="gloves")
    assert '--stat "Minions deal #% increased Damage" --fetch-limit 5 --pob off' in (res_range.get("hint") or "")

    # Multi-line mod selects the specific stat line matching the query rather than blindly taking line 0
    hybrid_cand = [
        {
            "stats": ["Minions deal (20-30)% increased Damage", "Minions have (15-20)% increased maximum Life"],
            "stat": "Minions deal (20-30)% increased Damage; Minions have (15-20)% increased maximum Life",
        }
    ]
    assert _build_trade_stat_arg("maximum Life", [], hybrid_cand) == "Minions have #% increased maximum Life"


def test_drop_only_direct_hit_formatting_and_hit_reason(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    res_all = game_data.query_local_data(
        "mod", "+2 to Level of Socketed Minion Gems", pob_dir=pob_root, runtime_dir=rt
    )
    assert res_all["dropOnlyReason"] == "hit"
    md_all = format_game_data(res_all, kind="mod", query="+2 to Level of Socketed Minion Gems")
    assert "Delve[Boots] (drop-only)" in md_all
    assert "Incursion (drop-only; no roll weight)" in md_all
    assert "DROP-ONLY WEIGHTS NOTE" in md_all
    assert 'pob trade search --slot <slot> --stat "+# to Level of Socketed Minion Gems>=2" --fetch-limit 5 --pob off' in md_all


def test_query_multi_and_skill_docs_codify_trade_ground_truth_and_offline_fallback(tmp_path: Path) -> None:
    pob_root, rt = tmp_path / "pob", tmp_path / "rt"
    _write_synthetic_poe1_mod_data(pob_root / "PoE1")

    multi = game_data.query_multi(
        kind="mod", queries=["minion gems", "Citaqualotl"], pob_dir=pob_root, runtime_dir=rt, slot="gloves"
    )
    assert multi["results"]["minion gems"]["dropOnlyReason"] == "excluded-by-slot-table"
    assert '--stat "+# to Level of Socketed Minion Gems>=2"' in (multi["results"]["Citaqualotl"].get("hint") or "")

    repo_root = Path(__file__).resolve().parents[2]
    skill_md = (repo_root / "skills" / "pob" / "SKILL.md").read_text(encoding="utf-8")
    trade_ref = (repo_root / "skills" / "pob" / "references" / "trade-and-bench.md").read_text(encoding="utf-8")
    for doc in (skill_md, trade_ref):
        assert "pob trade search" in doc
        assert "+# to Level of Socketed Minion Gems>=2" in doc
        assert "--fetch-limit 5 --pob off" in doc
        assert "unverified (offline)" in doc
