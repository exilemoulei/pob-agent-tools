"""Hardening tests for mutators (Exceptional gem max-level, Agnostic CWS guard),
comparator (`rank_gems` legacy filter + caveats, `rank_nodes` Minion fallback + weapon mastery filter),
and CLI (`skill add --level`, `rank-gems --include-legacy`).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli, comparator, mutators


class _FakeMutationWorker:
    """Minimal in-memory BridgeWorker fake for mutators unit tests."""

    def __init__(
        self,
        *,
        groups: list[dict[str, Any]] | None = None,
        items_res: dict[str, Any] | None = None,
        allocated_nodes: list[int] | None = None,
        tree_nodes: dict[str, dict[str, Any]] | None = None,
        gem_search_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.last_good_code: str | None = "CODE_INIT"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._groups = groups if groups is not None else [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "displayLabel": "Main",
                "gems": [
                    {"index": 1, "name": "Hexblast of Contradiction", "nameSpec": "Hexblast of Contradiction", "level": 20, "quality": 20, "enabled": True, "support": False},
                    {"index": 2, "name": "Greater Spell Echo", "nameSpec": "Greater Spell Echo", "level": 3, "quality": 20, "enabled": True, "support": True},
                ],
            }
        ]
        self._items_res = items_res if items_res is not None else {"slots": [], "items": []}
        self._allocated = list(allocated_nodes or [])
        self._tree_nodes = tree_nodes or {
            "19732": {"id": 19732, "name": "The Agnostic", "nameZh": "不可知论"},
            "100": {"id": 100, "name": "Test Node"},
        }
        self._gem_search_map = gem_search_map or {}

    def get_cached_tree_data(self, _tv: Any = None) -> dict[str, Any]:
        return {"nodes": self._tree_nodes}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for method, params in calls:
            if raise_on_error:
                out.append(self.call(method, params or {}))
            else:
                try:
                    out.append(self.call(method, params or {}))
                except Exception as exc:
                    out.append({"ok": False, "error": str(exc)})
        return out

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "export_code":
            return {"code": "CODE_SNAP"}
        if method == "import_code":
            return {"ok": True}
        if method == "get_stats":
            return {"stats": {"CombinedDPS": 1_000_000.0, "TotalEHP": 50_000.0, "Life": 4000.0}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "get_build_info":
            return {"points": {"used": 100, "usedMax": 123, "ascUsed": 8, "ascMax": 8}}
        if method == "get_build_header":
            return {"level": 95}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": self._groups}
        if method == "list_items":
            return self._items_res
        if method == "gem_search":
            q = str(p.get("query") or "")
            if q in self._gem_search_map:
                return {"gems": self._gem_search_map[q]}
            return {"gems": [{"name": q, "gemId": f"Gem/{q}"}]}
        if method == "add_gem":
            return {
                "gem": {
                    "index": 3,
                    "name": p.get("nameSpec"),
                    "level": p.get("level"),
                    "quality": p.get("quality"),
                    "support": True,
                }
            }
        if method == "set_gem":
            return {"gem": dict(p)}
        if method == "delete_gem":
            return {"ok": True}
        if method == "add_group":
            return {"index": len(self._groups) + 1}
        if method == "set_group":
            return {"ok": True}
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "allocatedNodes": list(self._allocated),
                "dynamicNodes": [],
                "points": {"used": len(self._allocated), "usedMax": 123},
            }
        if method == "tree_click":
            nid = int(p["id"])
            if nid in self._allocated:
                self._allocated.remove(nid)
            else:
                self._allocated.append(nid)
            return {
                "allocatedNodes": list(self._allocated),
                "points": {"used": len(self._allocated), "usedMax": 123},
            }
        if method in ("node_info", "node_hover"):
            return {"id": p.get("id"), "name": self._tree_nodes.get(str(p.get("id")), {}).get("name", "")}
        return {}


# ---------------------------------------------------------------------------
# 1. Exceptional Gem Max-Level Enforcement & Legacy 3.28+ Cautions
# ---------------------------------------------------------------------------


def test_validate_gem_level_exceptional_awakened_and_normal() -> None:
    # Normal gem defaults to Lv.20 and allows Lv.20/21
    assert mutators._validate_gem_level("Fireball", None) == (20, [])
    assert mutators._validate_gem_level("Fireball", 21) == (21, [])

    # PoE1 3.28+ Exceptional supports -> nat 3 / corrupt 4
    for gem in (
        "Greater Spell Echo",
        "Greater Spell Echo Support",
        "Cooldown Recovery",
        "Eldritch Blasphemy",
        "Hexpass",
        "Hextoad",
        "Congregation",
        "Empower",
        "Enhance",
        "Enlighten",
        "Eclipse",
    ):
        assert mutators._validate_gem_level(gem, None) == (3, [])
        assert mutators._validate_gem_level(gem, 3) == (3, [])
        assert mutators._validate_gem_level(gem, 4) == (4, [])
        with pytest.raises(ValueError, match=r"Invalid level 20 for Exceptional Support .*max natural level is Lv\.3 \(corrupted max Lv\.4\)"):
            mutators._validate_gem_level(gem, 20)
        with pytest.raises(ValueError, match=r"PoE1 3\.28\+ Exceptional supports cannot reach Lv\.20"):
            mutators._validate_gem_level(gem, 5)

    # Awakened Empower / Enhance / Enlighten -> nat 4 / corrupt 5
    for gem in ("Awakened Empower", "Awakened Enhance", "Awakened Enlighten"):
        assert mutators._validate_gem_level(gem, None) == (4, [])
        assert mutators._validate_gem_level(gem, 5) == (5, [])
        with pytest.raises(ValueError, match=r"max natural level is Lv\.4 \(corrupted max Lv\.5\)"):
            mutators._validate_gem_level(gem, 6)

    # Legacy 3.28+ removed Awakened supports -> caution emitted, not raised
    lv, cautions = mutators._validate_gem_level("Awakened Spell Echo", None)
    assert lv == 5
    assert len(cautions) == 1
    assert "[LEGACY 3.28+ REMOVED]" in cautions[0]
    assert "Awakened Spell Echo" in cautions[0]


def test_add_skill_gem_enforces_exceptional_level_and_defaults() -> None:
    w = _FakeMutationWorker()

    # Default level=None -> auto Lv.3 for Greater Spell Echo
    res_gse = mutators.add_skill_gem(w, group_selector="main", gem_name="Greater Spell Echo")
    add_calls = [p for m, p in w.calls if m == "add_gem"]
    assert add_calls[-1]["level"] == 3
    assert "cautions" not in res_gse

    # Corrupted max Lv.4 allowed
    mutators.add_skill_gem(w, group_selector="main", gem_name="Greater Spell Echo", level=4)
    add_calls = [p for m, p in w.calls if m == "add_gem"]
    assert add_calls[-1]["level"] == 4

    # Lv.20 rejected with ValueError
    with pytest.raises(ValueError, match="Invalid level 20 for Exceptional Support 'Greater Spell Echo'"):
        mutators.add_skill_gem(w, group_selector="main", gem_name="Greater Spell Echo", level=20)

    # Awakened Empower defaults to Lv.4, rejects Lv.6
    mutators.add_skill_gem(w, group_selector="main", gem_name="Awakened Empower")
    assert [p for m, p in w.calls if m == "add_gem"][-1]["level"] == 4
    with pytest.raises(ValueError, match="Awakened Empower"):
        mutators.add_skill_gem(w, group_selector="main", gem_name="Awakened Empower", level=6)

    # Normal gem defaults to Lv.20
    mutators.add_skill_gem(w, group_selector="main", gem_name="Increased Critical Damage")
    assert [p for m, p in w.calls if m == "add_gem"][-1]["level"] == 20

    # Legacy Awakened gem attaches caution to res["cautions"]
    res_leg = mutators.add_skill_gem(w, group_selector="main", gem_name="Awakened Elemental Focus", level=5)
    assert any("[LEGACY 3.28+ REMOVED]" in c for c in res_leg.get("cautions", []))


def test_modify_skill_checks_existing_gem_and_new_gem_level() -> None:
    w = _FakeMutationWorker()

    # Setting level=20 on EXISTING Greater Spell Echo (#2) MUST fail
    with pytest.raises(ValueError, match="Invalid level 20 for Exceptional Support 'Greater Spell Echo'"):
        mutators.modify_skill(w, group_selector="main", gem_selector=2, level=20)

    # Setting level=4 on existing Greater Spell Echo succeeds
    res_ok = mutators.modify_skill(w, group_selector="main", gem_selector="Greater Spell Echo", level=4)
    assert [p for m, p in w.calls if m == "set_gem"][-1]["level"] == 4
    assert "cautions" not in res_ok

    # Setting new_gem="Cooldown Recovery" with level=20 MUST fail
    with pytest.raises(ValueError, match="Invalid level 20 for Exceptional Support 'Cooldown Recovery'"):
        mutators.modify_skill(w, group_selector="main", gem_selector=2, new_gem="Cooldown Recovery", level=20)

    # Swapping to new_gem="Cooldown Recovery" without level auto-clamps to Lv.3
    mutators.modify_skill(w, group_selector="main", gem_selector=2, new_gem="Cooldown Recovery")
    assert [p for m, p in w.calls if m == "set_gem"][-1]["level"] == 3

    # Swapping to legacy Awakened gem attaches caution
    res_leg = mutators.modify_skill(w, group_selector="main", gem_selector=2, new_gem="Awakened Spell Echo", level=5)
    assert any("[LEGACY 3.28+ REMOVED]" in c for c in res_leg.get("cautions", []))


def test_normalize_gem_spec_add_and_replace_group_enforcement() -> None:
    spec_gse = mutators._normalize_gem_spec("Greater Spell Echo")
    assert spec_gse["level"] == 3
    assert "cautions" not in spec_gse

    spec_ae = mutators._normalize_gem_spec({"name": "Awakened Enlighten"})
    assert spec_ae["level"] == 4

    with pytest.raises(ValueError, match="Greater Spell Echo"):
        mutators._normalize_gem_spec("Greater Spell Echo:20")

    with pytest.raises(ValueError, match="Awakened Empower"):
        mutators._normalize_gem_spec({"name": "Awakened Empower", "level": 20})

    w = _FakeMutationWorker()
    res_ag = mutators.add_skill_group(
        w,
        gems=["Fireball", "Greater Spell Echo", "Awakened Spell Echo:5"],
        label="TestGroup",
    )
    add_calls = [p for m, p in w.calls if m == "add_gem"]
    levels_by_name = {p["nameSpec"]: p["level"] for p in add_calls}
    assert levels_by_name["Fireball"] == 20
    assert levels_by_name["Greater Spell Echo"] == 3
    assert levels_by_name["Awakened Spell Echo"] == 5
    assert any("[LEGACY 3.28+ REMOVED]" in c for c in res_ag.get("cautions", []))

    with pytest.raises(ValueError, match="Greater Spell Echo"):
        mutators.replace_skill_group(w, group="main", gems=["Fireball", "Greater Spell Echo:20"])

    res_rg = mutators.replace_skill_group(w, group="main", gems=["Fireball", "Greater Spell Echo", "Awakened Burning Damage:5"])
    assert any("[LEGACY 3.28+ REMOVED]" in c for c in res_rg.get("cautions", []))


# ---------------------------------------------------------------------------
# 2. The Agnostic (19732) Deallocation Guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items_res", "groups"),
    [
        (
            {
                "slots": [{"name": "Ring 1", "selItemId": 1}],
                "items": [{"id": 1, "title": "Valyrium", "name": "Valyrium Moonstone Ring"}],
            },
            [],
        ),
        (
            {
                "slots": [{"name": "Ring 2", "selItemId": 2}],
                "items": [{"id": 2, "titleZh": "女武神", "name": "Moonstone Ring"}],
            },
            [],
        ),
        (
            {
                "slots": [{"name": "Ring 2", "selItemId": 2}],
                "items": [{"id": 2, "titleZh": "戰日", "name": "Moonstone Ring"}],
            },
            [],
        ),
        (
            {
                "slots": [{"name": "Jewel 1", "selItemId": 3}],
                "items": [{"id": 3, "title": "Bloodnotch", "titleZh": "缺血症"}],
            },
            [],
        ),
        (
            {
                "slots": [{"name": "Jewel 2", "selItemId": 4}],
                "items": [{"id": 4, "title": "Immutable Force", "name": "Crimson Jewel"}],
            },
            [],
        ),
        (
            {"slots": [], "items": []},
            [
                {
                    "index": 1,
                    "enabled": True,
                    "gems": [{"index": 1, "name": "Cast when Stunned Support", "enabled": True}],
                }
            ],
        ),
        (
            {"slots": [], "items": []},
            [
                {
                    "index": 1,
                    "enabled": True,
                    "gems": [{"index": 1, "nameZh": "晕眩时施放", "enabled": True}],
                }
            ],
        ),
    ],
)
def test_change_tree_agnostic_dealloc_guard_triggers_caution(
    items_res: dict[str, Any],
    groups: list[dict[str, Any]],
) -> None:
    w = _FakeMutationWorker(
        groups=groups,
        items_res=items_res,
        allocated_nodes=[19732, 100],
    )
    res = mutators.change_tree(w, deallocate=["19732"], apply=False)
    cautions = res.get("cautions") or []
    assert len(cautions) == 1
    assert "[BUILD-BRICK RISK]" in cautions[0]
    assert "The Agnostic" in cautions[0]
    assert "50% Stun Avoidance" in cautions[0]
    assert "Valyrium" in cautions[0]
    assert "Blood Magic" in cautions[0]


def test_change_tree_agnostic_guard_silent_without_cws_and_on_other_nodes() -> None:
    # No CWS / Valyrium / Bloodnotch / Immutable Force -> no caution
    w_plain = _FakeMutationWorker(
        groups=[{"index": 1, "enabled": True, "gems": [{"index": 1, "name": "Fireball", "enabled": True}]}],
        items_res={"slots": [{"name": "Ring 1", "selItemId": 1}], "items": [{"id": 1, "title": "Ruby Ring"}]},
        allocated_nodes=[19732, 100],
    )
    res_plain = mutators.change_tree(w_plain, deallocate=["19732"], apply=False)
    assert not res_plain.get("cautions")

    # Has Valyrium, but deallocating node 100 (NOT 19732) -> no caution
    w_other = _FakeMutationWorker(
        groups=[],
        items_res={"slots": [{"name": "Ring 1", "selItemId": 1}], "items": [{"id": 1, "title": "Valyrium"}]},
        allocated_nodes=[19732, 100],
    )
    res_other = mutators.change_tree(w_other, deallocate=["100"], apply=False)
    assert not res_other.get("cautions")

    # Also test batch op hoisting Agnostic caution
    res_batch = mutators.run_batch(w_other, [{"op": "tree", "deallocate": ["19732"]}], apply=False)
    assert any("[BUILD-BRICK RISK]" in c for c in res_batch.get("cautions", []))


# ---------------------------------------------------------------------------
# 3. Comparator: rank_gems (legacy 3.28+ filter + expanded caveats)
# ---------------------------------------------------------------------------


def test_rank_gems_filters_legacy_awakened_and_keeps_exceptional_awakened() -> None:
    captured_search: list[dict[str, Any]] = []

    class _FakeGemRankWorker:
        def __init__(self, mana_cost: float = 450.0) -> None:
            self.lock = threading.RLock()
            self._mana_cost = mana_cost

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            p = dict(params or {})
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [
                        {
                            "index": 1,
                            "isMain": True,
                            "displayLabel": "Hexblast",
                            "gems": [{"index": 1, "name": "Hexblast", "enabled": True, "support": False}],
                        }
                    ],
                }
            if method == "get_stats":
                return {"stats": {"ManaCost": self._mana_cost}}
            if method == "get_gem_options":
                return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
            if method == "set_gem_options":
                return {"ok": True}
            if method == "gem_search":
                captured_search.append(p)
                return {
                    "dpsField": "CombinedDPS",
                    "baseDps": 1_000_000,
                    "gems": [
                        {"name": "Awakened Spell Echo", "delta": 400_000, "pct": 40.0},
                        {"name": "Awakened Elemental Focus Support", "delta": 380_000, "pct": 38.0},
                        {"name": "Awakened Empower", "delta": 350_000, "pct": 35.0},
                        {"name": "Awakened Enhance", "delta": 320_000, "pct": 32.0},
                        {"name": "Awakened Enlighten", "delta": 310_000, "pct": 31.0},
                        {"name": "Sadism", "delta": 300_000, "pct": 30.0},
                        {"name": "Inspiration", "delta": 280_000, "pct": 28.0},
                        {"name": "Spell Echo", "delta": 250_000, "pct": 25.0},
                    ],
                }
            return {}

    # Default include_legacy=False
    res = comparator.rank_gems(_FakeGemRankWorker(mana_cost=450.0), top=5)  # type: ignore[arg-type]
    assert captured_search[-1]["limit"] > 5  # fetch_limit bumped to keep top-N full
    names = [c["name"] for c in res["candidates"]]
    assert len(names) == 5
    assert "Awakened Spell Echo" not in names
    assert "Awakened Elemental Focus Support" not in names
    assert "Awakened Empower" in names
    assert "Awakened Enhance" in names
    assert "Awakened Enlighten" in names
    assert "Sadism" in names
    assert "Awakened Spell Echo" in res["filtered_legacy"]
    assert "Awakened Elemental Focus Support" in res["filtered_legacy"]

    # Check expanded caveats on top-6 including Inspiration (ManaCost=450 >= 400)
    res_all_non_legacy = comparator.rank_gems(_FakeGemRankWorker(mana_cost=450.0), top=6)  # type: ignore[arg-type]
    by_name = {c["name"]: c for c in res_all_non_legacy["candidates"]}
    assert "70% LESS Ailment Duration" in (by_name["Sadism"].get("caveat") or "")
    assert "mirror-tier endgame budget" in (by_name["Awakened Empower"].get("caveat") or "")
    assert "Max Lv4 / corrupt Lv5" in (by_name["Awakened Enhance"].get("caveat") or "")
    assert "Max Lv4 / corrupt Lv5" in (by_name["Awakened Enlighten"].get("caveat") or "")
    assert "High ManaCost (450)" in (by_name["Inspiration"].get("caveat") or "")
    assert "800-mana reset threshold" in (by_name["Inspiration"].get("caveat") or "")

    # include_legacy=True keeps legacy Awakened supports
    res_inc = comparator.rank_gems(_FakeGemRankWorker(mana_cost=50.0), top=5, include_legacy=True)  # type: ignore[arg-type]
    inc_names = [c["name"] for c in res_inc["candidates"]]
    assert "Awakened Spell Echo" in inc_names
    assert "Awakened Elemental Focus Support" in inc_names
    assert res_inc["filtered_legacy"] == []


def test_gem_caveat_inspiration_mana_thresholds() -> None:
    assert comparator._gem_caveat("Inspiration", main_mana_cost=0.0) == "0 charges on 0-mana skill!"
    assert comparator._gem_caveat("Inspiration", main_mana_cost=399.0) is None
    c400 = comparator._gem_caveat("Inspiration Support", main_mana_cost=400.0)
    assert c400 is not None and "High ManaCost (400)" in c400 and "5 charges" in c400


# ---------------------------------------------------------------------------
# 4. Comparator: rank_nodes (MinionCombinedDPS fallback + Weapon Mastery filter)
# ---------------------------------------------------------------------------


def test_rank_nodes_minion_fallback_and_weapon_mastery_filter() -> None:
    node_power_stats_called: list[str] = []

    class _FakeNodeWorker:
        def __init__(self, items_res: dict[str, Any], extra_items: dict[int, dict[str, Any]] | None = None) -> None:
            self.lock = threading.RLock()
            self._items_res = items_res
            self._extra_items = extra_items or {}

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            p = dict(params or {})
            if method == "node_power":
                st = str(p.get("stat") or "")
                node_power_stats_called.append(st)
                if st == "MinionCombinedDPS":
                    raise RuntimeError("unknown power stat: MinionCombinedDPS")
                return {
                    "stat": st,
                    "stats": ["CombinedDPS", "TotalEHP"],
                    "report": [
                        {"id": 1, "name": "Staff Mastery: +30% Crit Multi", "allocated": False, "pathDist": 2, "pathPower": 10.0, "power": 20.0},
                        {"id": 2, "name": "Mace Mastery: All Damage with Maces can Chill", "allocated": False, "pathDist": 2, "pathPower": 9.0, "power": 18.0},
                        {"id": 3, "name": "Bow Mastery: +10% Movement Speed", "allocated": False, "pathDist": 2, "pathPower": 8.0, "power": 16.0},
                        {"id": 4, "name": "Wand Mastery: +15% Spell Damage", "allocated": False, "pathDist": 2, "pathPower": 7.5, "power": 15.0},
                        {"id": 5, "name": "Claw Mastery: Leech", "allocated": False, "pathDist": 2, "pathPower": 7.0, "power": 14.0},
                        {"id": 6, "name": "Dagger Mastery: Elusive", "allocated": False, "pathDist": 2, "pathPower": 6.5, "power": 13.0},
                        {"id": 7, "name": "Axe Mastery: Onslaught", "allocated": False, "pathDist": 2, "pathPower": 6.0, "power": 12.0},
                        {"id": 8, "name": "Sword Mastery: Frenzy", "allocated": False, "pathDist": 2, "pathPower": 5.5, "power": 11.0},
                        {"id": 9, "name": "Life Mastery: +30 to maximum Life", "allocated": False, "pathDist": 1, "pathPower": 5.0, "power": 5.0},
                    ],
                }
            if method == "list_items":
                return self._items_res
            if method == "get_item":
                return self._extra_items.get(int(p["id"]), {})
            return {}

    # A. Sceptre + Shield: Sceptre counts as Mace (NOT Staff!) + MinionCombinedDPS fallback
    sceptre_items = {
        "slots": [
            {"name": "Weapon 1", "selItemId": 10},
            {"name": "Weapon 2", "selItemId": 11},
        ],
        "items": [
            {"id": 10, "name": "Void Sceptre", "type": "Sceptre", "baseName": "Void Sceptre"},
            {"id": 11, "name": "Titanium Spirit Shield", "type": "Shield", "baseName": "Titanium Spirit Shield"},
        ],
    }
    res_sceptre = comparator.rank_nodes(_FakeNodeWorker(sceptre_items), stat="MinionCombinedDPS", max_dist=5, top=15)  # type: ignore[arg-type]
    assert node_power_stats_called == ["MinionCombinedDPS", "CombinedDPS"]
    assert res_sceptre["stat"] == "CombinedDPS"
    assert res_sceptre["stat_fallback"] == "CombinedDPS"
    kept_names = [n["name"] for n in res_sceptre["byPerPoint"]]
    assert any("Mace Mastery" in nm for nm in kept_names)
    assert any("Life Mastery" in nm for nm in kept_names)
    assert not any("Staff Mastery" in nm for nm in kept_names)
    assert not any("Bow Mastery" in nm for nm in kept_names)
    assert not any("Wand Mastery" in nm for nm in kept_names)
    assert not any("Claw Mastery" in nm for nm in kept_names)
    assert not any("Dagger Mastery" in nm for nm in kept_names)
    assert not any("Axe Mastery" in nm for nm in kept_names)
    assert not any("Sword Mastery" in nm for nm in kept_names)

    # B. Wand via get_item fallback (list_items item lacks type/baseName)
    wand_items = {
        "slots": [{"name": "Weapon 1", "selItemId": 20}, {"name": "Weapon 2", "selItemId": 0}],
        "items": [{"id": 20, "title": "Chimeric Call"}],
    }
    res_wand = comparator.rank_nodes(
        _FakeNodeWorker(wand_items, extra_items={20: {"id": 20, "type": "Wand", "baseName": "Imbued Wand"}}),  # type: ignore[arg-type]
        stat="CombinedDPS",
    )
    assert "stat_fallback" not in res_wand
    wand_kept = [n["name"] for n in res_wand["byPerPoint"]]
    assert any("Wand Mastery" in nm for nm in wand_kept)
    assert not any("Staff Mastery" in nm for nm in wand_kept)
    assert not any("Mace Mastery" in nm for nm in wand_kept)


# ---------------------------------------------------------------------------
# 5. CLI: `pob skill add --level` default None & `pob rank-gems --include-legacy`
# ---------------------------------------------------------------------------


def test_cli_skill_add_default_level_none_and_caution_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        return {
            "mode": "WHAT-IF",
            "detail": {"effectiveLevel": 3, "addedGem": {"gem": {"level": 3}}},
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000, "totalEhp": 5000, "life": 3000, "points": {}},
            "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1200, "totalEhp": 5000, "life": 3000, "points": {}},
            "diff": {"deltas": []},
            "cautions": ["[LEGACY 3.28+ REMOVED] test caution"],
        }

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    r1 = runner.invoke(cli.main, ["skill", "add", "--gem", "Greater Spell Echo"])
    assert r1.exit_code == 0, r1.output
    assert captured[-1][0] == "skill_add"
    assert captured[-1][1]["level"] is None
    assert "(3/20%)" in r1.output
    assert "> [CAUTION] [LEGACY 3.28+ REMOVED] test caution" in r1.output

    r2 = runner.invoke(cli.main, ["skill", "add", "--gem", "Greater Spell Echo", "--level", "4"])
    assert r2.exit_code == 0, r2.output
    assert captured[-1][1]["level"] == 4


def test_cli_rank_gems_include_legacy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        captured.append((action, dict(params or {})))
        return {
            "groupIndex": 1,
            "groupLabel": "Fireball",
            "mode": "add_link (slot #2)",
            "dpsField": "CombinedDPS",
            "baseDps": 1_000_000,
            "candidates": [{"name": "Spell Echo", "nameZh": "施法回响", "dps": 1_300_000, "delta": 300_000, "pct": 30.0}],
            "filtered_legacy": [] if (params or {}).get("include_legacy") else ["Awakened Spell Echo"],
        }

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    r_def = runner.invoke(cli.main, ["rank-gems"])
    assert r_def.exit_code == 0, r_def.output
    assert captured[-1][0] == "rank_gems"
    assert captured[-1][1]["include_legacy"] is False
    assert "Awakened Spell Echo" in r_def.output

    r_inc = runner.invoke(cli.main, ["rank-gems", "--include-legacy"])
    assert r_inc.exit_code == 0, r_inc.output
    assert captured[-1][1]["include_legacy"] is True


@pytest.mark.parametrize("op", ["replace", "add"])
def test_illegal_level_permission_on_one_gem_of_a_gems_list_is_honoured(op: str) -> None:
    def _run(gems: list[dict[str, Any]]) -> dict[str, Any]:
        w = _FakeMutationWorker()
        if op == "replace":
            return mutators.replace_skill_group(w, "main", gems)
        return mutators.add_skill_group(w, gems=gems)

    res = _run([{"name": "Greater Spell Echo", "level": 20, "allowIllegalLevel": True}])
    assert any("[ILLEGAL-GEM-LEVEL]" in c for c in res.get("cautions", []))

    # The permission is per gem: a second gem in the list is still held to its bounds.
    with pytest.raises(ValueError, match="Invalid level 20 for Exceptional Support 'Cooldown Recovery'"):
        _run(
            [
                {"name": "Greater Spell Echo", "level": 20, "allowIllegalLevel": True},
                {"name": "Cooldown Recovery", "level": 20},
            ]
        )
