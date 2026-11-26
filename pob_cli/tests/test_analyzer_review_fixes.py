"""Offline regressions for analyzer/comparator review fixes (resist caps, sockets, keystones, ranking)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pob_cli import analyzer, comparator
from pob_cli.analyzer.audit import _collect_static_health_issues
from pob_cli.analyzer.constants import POB_GEM_FLAG_DEFAULTS
from pob_cli.analyzer.dewater import _extract_probe_state
from pob_cli.analyzer.diagnose_compare import _run_diagnose_rankings
from pob_cli.analyzer.eb_sustain import collect_mana_sustain_issues
from pob_cli.analyzer.summary import _count_item_sockets

_CAPPED = {f"{e}Resist": 75 for e in ("Fire", "Cold", "Lightning")} | {
    f"{e}ResistOverCap": 30 for e in ("Fire", "Cold", "Lightning")
}


def _health(stats: dict[str, Any], **kw: Any) -> list[dict[str, str]]:
    return _collect_static_health_issues(
        {**_CAPPED, "ChaosResist": 0, "Life": 4000, **stats},
        {"points": {}},
        kw.pop("items_res", {"slots": [], "items": []}),
        None,
        [],
        **kw,
    )


def _resist_msgs(issues: list[dict[str, str]]) -> list[str]:
    return [i["message"] for i in issues if i["category"] == "Resistance" and "Fire" in i["message"]]


def test_resist_gap_uses_engine_missing_resist_for_raised_max() -> None:
    # 80% max, 77% shown: PoB reports MissingFireResist=3 and clamps OverCap at 0.
    msgs = _resist_msgs(_health({"FireResist": 77, "FireResistOverCap": 0, "MissingFireResist": 3}))
    assert msgs == ["Fire Resist is uncapped: 77% of 80% max (gap: 3%)"]

    # Capped at a raised 80% max: no uncapped report, only the thin-overcap note.
    msgs = _resist_msgs(_health({"FireResist": 80, "FireResistOverCap": 5, "MissingFireResist": 0}))
    assert len(msgs) == 1 and "overcap is thin" in msgs[0]

    # An explicit max wins.
    msgs = _resist_msgs(_health({"FireResist": 77, "FireResistOverCap": 0, "FireResistMax": 82}))
    assert msgs == ["Fire Resist is uncapped: 77% of 82% max (gap: 5%)"]


def test_resist_gap_falls_back_to_75_only_when_max_unknown() -> None:
    msgs = _resist_msgs(_health({"FireResist": 70, "FireResistOverCap": 0}))
    assert msgs == ["Fire Resist is uncapped: 70% of 75% max (gap: 5%)"]
    # Overcap > 0 means the shown value is the (lowered) cap: not uncapped.
    msgs = _resist_msgs(_health({"FireResist": 70, "FireResistOverCap": 20}))
    assert not any("uncapped" in m for m in msgs)


def test_count_item_sockets_ignores_abyssal_and_delve() -> None:
    assert _count_item_sockets("Rarity: RARE\nSockets: R-G-B-A-A\n") == 3
    assert _count_item_sockets("Rarity: RARE\nSockets: R-G-B-B-D\n") == 4
    assert _count_item_sockets("Rarity: RARE\nSockets: W-W-W-W-W-W\n") == 6
    assert _count_item_sockets("Rarity: RARE\nSockets: A\n") is None


def test_abyssal_sockets_do_not_count_as_empty_gem_sockets() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "slot": "Body Armour",
                "gems": [
                    {"index": i, "name": n, "support": i > 1, "enabled": True, "level": 20}
                    for i, n in enumerate(("Fireball", "Added Fire Damage", "Elemental Focus", "Controlled Destruction"), 1)
                ],
            }
        ],
    }
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 10},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
        equipped_slots_raw=[{"slotName": "Body Armour", "raw": "Rarity: RARE\nSockets: R-R-R-R A\n"}],
    )
    assert not any("empty socket" in g["message"] for g in gaps)


def test_hollow_palm_skips_empty_weapon_and_glove_slots() -> None:
    items_res = {
        "slots": [{"name": n} for n in ("Weapon 1", "Gloves", "Helmet")],
        "items": [],
    }
    plain = [i["message"] for i in _health({}, items_res=items_res) if i["category"] == "Equipment"]
    assert len(plain) == 3
    palm = [
        i["message"]
        for i in _health({}, items_res=items_res, tree_summary={"keystones": [{"name": "Hollow Palm Technique"}]})
        if i["category"] == "Equipment"
    ]
    assert palm == ["Equipment slot 'Helmet' is empty"]


class _RankWorker:
    def __init__(self, node_res: Any) -> None:
        self.lock = threading.RLock()
        self.node_res = node_res
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method == "node_power":
            return self.node_res
        if method == "list_items":
            return {"slots": [], "items": []}
        raise AssertionError(method)


def test_rank_nodes_propagates_node_power_error() -> None:
    w = _RankWorker({"ok": False, "error": "PowerBuilder exploded"})
    with pytest.raises(RuntimeError, match="PowerBuilder exploded"):
        comparator.rank_nodes(w, stat="CombinedDPS")  # type: ignore[arg-type]


def test_diagnose_ranks_nodes_and_gems_on_full_dps(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_nodes(worker: Any, *, stat: str = "CombinedDPS", max_dist: int = 5, top: int = 15, baseline_label: str | None = None) -> dict[str, Any]:
        seen["node"] = stat
        return {"stat": stat}

    def _fake_gems(worker: Any, *, field: str = "CombinedDPS", baseline_label: str | None = None, **_kw: Any) -> dict[str, Any]:
        seen["gem"] = field
        return {"dpsField": field}

    monkeypatch.setattr(comparator, "rank_nodes", _fake_nodes)
    monkeypatch.setattr(comparator, "rank_gems", _fake_gems)
    audit = {"original": {"primaryDpsKey": "FullDPS"}}
    _, _, errs = _run_diagnose_rankings(object(), audit, {"gems": [{"name": "x"}]}, top=5)  # type: ignore[arg-type]
    assert errs == [] and seen == {"node": "FullDPS", "gem": "FullDPS"}

    def _raising_nodes(worker: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("node_power failed (CombinedDPS): boom")

    monkeypatch.setattr(comparator, "rank_nodes", _raising_nodes)
    _, _, errs = _run_diagnose_rankings(object(), {"original": {}}, {}, top=5)  # type: ignore[arg-type]
    assert errs == ["rank_nodes: node_power failed (CombinedDPS): boom"]


def _spend_stats() -> dict[str, Any]:
    return {
        "ManaCost": 100,
        "Speed": 5.0,
        "ManaRegenRecovery": 100.0,
        "Mana": 2000,
        "ManaUnreserved": 2000,
        "Life": 4000,
        "LifeUnreserved": 4000,
    }


def _main_group(*gems: tuple[str, bool]) -> dict[str, Any]:
    return {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [{"index": i, "name": n, "support": s, "enabled": True} for i, (n, s) in enumerate(gems, 1)],
            }
        ],
    }


def test_mana_spend_note_is_labelled_estimate_and_skips_unmodelled_skills() -> None:
    plain = collect_mana_sustain_issues(_spend_stats(), _main_group(("Fireball", False)))
    (note,) = [i for i in plain if "Mana spend rate" in i["message"]]
    assert "estimate" in note["message"]

    for skills, extra, flags in (
        (_main_group(("Fireball", False), ("Spell Totem Support", True)), {}, None),
        (_main_group(("Raise Spectre", False)), {}, None),
        (_main_group(("Fireball", False), ("Cast when Damage Taken Support", True)), {}, None),
        (_main_group(("Fireball", False)), {"SkillTriggerRate": 4.0}, None),
        (_main_group(("Fireball", False)), {}, {"totem": True}),
    ):
        issues = collect_mana_sustain_issues({**_spend_stats(), **extra}, skills, skill_flags=flags)
        assert not any("Mana spend rate" in i["message"] for i in issues), skills


def test_trigger_overspeed_without_probe_is_medium_estimate() -> None:
    from pob_cli.analyzer.pitfalls_triggers import ESTIMATE_SUFFIX, _fmt_overspeed_gap

    tick = {"effectiveCapPerSec": 4.0, "ticks": 8, "effectiveCdMs": 264}
    est = _fmt_overspeed_gap("Cospri's Malice", "Weapon 1", 5.0, 0.0, tick, estimate=True)
    assert est["severity"] == "MEDIUM" and est["message"].endswith(ESTIMATE_SUFFIX)
    eng = _fmt_overspeed_gap("Cospri's Malice", "Weapon 1", 5.0, 0.0, tick)
    assert eng["severity"] == "HIGH" and ESTIMATE_SUFFIX not in eng["message"]


def _aura_skills() -> dict[str, Any]:
    return {
        "mainSocketGroup": 1,
        "groups": [
            {"index": 1, "isMain": True, "slot": "Body Armour", "enabled": True,
             "gems": [{"name": "Lightning Arrow", "enabled": True, "support": False}]},
            {"index": 4, "slot": "-", "enabled": True,
             "gems": [{"name": "Haste", "enabled": True, "support": False}]},
        ],
    }


def test_simulated_aura_note_is_conditional_without_spectre_signal() -> None:
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaUnreserved": -50, "ManaCost": 20},
        skills_res=_aura_skills(),
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    (sim,) = [g for g in gaps if "[SIMULATED AURA RESERVATION]" in g["message"]]
    assert sim["severity"] == "LOW"
    assert "If a group stands for a Spectre" in sim["message"] and "likely" not in sim["message"]

    skills = _aura_skills()
    skills["groups"].append(
        {"index": 5, "slot": "Helmet", "enabled": True, "gems": [{"name": "Raise Spectre", "enabled": True, "support": False}]}
    )
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaUnreserved": -50, "ManaCost": 20},
        skills_res=skills,
        items_res={"slots": [], "items": []},
        cfg_res={},
    )
    (sim,) = [g for g in gaps if "[SIMULATED AURA RESERVATION]" in g["message"]]
    assert sim["severity"] == "MEDIUM" and "raises Spectres" in sim["message"]


def test_vaal_gem_flag_defaults_match_pob_loader() -> None:
    # SkillsTab:Load: enableGlobal1 defaults true, enableGlobal2 defaults false.
    assert POB_GEM_FLAG_DEFAULTS == {"enabled": True, "enableGlobal1": True, "enableGlobal2": False}
    probe = {"kind": "gem", "group": 1, "index": 1, "fields": ["enabled", "enableGlobal1", "enableGlobal2"]}
    res = {"groups": [{"index": 1, "gems": [{"index": 1}]}]}
    assert _extract_probe_state(probe, res) == {"enabled": True, "enableGlobal1": True, "enableGlobal2": False}


def test_zero_mana_inspiration_message_explains_mana_spend_rule() -> None:
    gaps, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0},
        skills_res=_main_group(("Ice Nova", False), ("Inspiration Support", True)),
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
    )
    (insp,) = [g for g in gaps if "Dead Inspiration" in g["message"]]
    assert "spend mana" in insp["message"] and "0-mana" in insp["message"]
