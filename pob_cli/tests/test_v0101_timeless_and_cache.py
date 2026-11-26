"""Offline unit tests for v0.10.1 Layer 3:
- `AGY05` multi-socket `pob timeless search`, allocated-vs-unallocated `radiusSummary`,
  `Engine` one recalculation per seed, calm `weightShortlist` note, and 0ms `Putrid Seed` check;
- `SlotReadCache` bounded `OrderedDict` LRU, `noAutoKeep` canonicalization, dual-slot `compare`
  caching, slot invalidation on eviction/lang switch, and mutation `rev` increment contract;
- `--surrogate-k` + `--pob-limit` slicing and `tx.restore(rebuild=False)` in `_eval_trade_items_cow`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import pob_cli.comparator as comparator_mod
import pob_cli.daemon as daemon_mod
from pob_cli import cli_timeless, cli_trade, timeless
from pob_cli.cli import main
from pob_cli.daemon import rpc_handlers
from pob_cli.daemon.read_cache import SlotReadCache
from pob_cli.daemon.runtime import REF_IDLE_TIMEOUT_SEC
from pob_cli.snapshot import (
    BuildScope,
    BuildTransaction,
    history_of,
    load_build,
    redo_build,
    run_mutation,
    undo_build,
)
from pob_cli.timeless.engine import TimelessSearchEngine, _build_search_outcomes
from pob_cli.timeless.lut import RESET_EVERY
from pob_cli.timeless.worker import CliUsageError, _check_putrid_seed
from pob_cli.trade_client import MAX_FETCH_MANY, TradeClient
from pob_cli.trade_eval import eval_trade_items

TIMELESS_JEWEL_LUA = """return {
[[
Lethal Pride
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
{variant:1}Commanded leadership over (10000-10004) warriors under Kaom
Passives in radius are Conquered by the Karui
Historic
]],[[
Festering Vengeance
Murderous Eye Jewel
Limited to: 1 Historic
Implicits: 0
Subjugating (100-104) souls in the thrall of Tecrod
Passives affected are Conquered by the Abyssal
Historic
]],
}
"""


# ---------------------------------------------------------------------------
# 1. SlotReadCache LRU, `noAutoKeep`, dual-slot `compare`, & slot invalidation
# ---------------------------------------------------------------------------


def test_slot_read_cache_bounded_lru_no_auto_keep_and_invalidation() -> None:
    cache = SlotReadCache(max_entries=3)
    cache.store("main", 1, "en", "summary", {}, {"v": "s"})
    cache.store("main", 1, "en", "items", {}, {"v": "i"})
    cache.store("main", 1, "en", "skills", {}, {"v": "k"})

    # Touch "summary" so "items" becomes the oldest LRU entry
    hit_s = cache.lookup("main", 1, "en", "summary", {})
    assert hit_s is not None and hit_s["v"] == "s" and hit_s["cached"] is True

    # Storing 4th entry evicts "items" (LRU), preserving "summary" (MRU)
    cache.store("main", 1, "en", "audit", {"noAutoKeep": True}, {"v": "a_no_keep"})
    assert len(cache._entries) == 3
    assert cache.lookup("main", 1, "en", "items", {}) is None
    assert cache.lookup("main", 1, "en", "summary", {}) is not None

    # `noAutoKeep` <-> `no_auto_keep` boolean canonicalization & snake_case precedence
    hit_alias = cache.lookup("main", 1, "en", "audit", {"no_auto_keep": 1})
    assert hit_alias is not None and hit_alias["v"] == "a_no_keep"
    assert cache.lookup("main", 1, "en", "audit", {"noAutoKeep": False}) is None
    assert cache.lookup("main", 1, "en", "audit", {"noAutoKeep": True, "no_auto_keep": False}) is None
    assert cache.lookup("main", 1, "en", "audit", {"no_auto_keep": False, "noAutoKeep": True}) is None
    assert cache.lookup("main", 1, "en", "audit", {"noAutoKeep": False, "no_auto_keep": True}) is not None
    assert cache.lookup("main", 1, "en", "audit", {}) is None

    # Dual-slot compare key & slot invalidation clears both single-slot and compare entries
    cache.store_compare("main", 1, "ref", 2, "en", {"noAutoKeep": True}, {"cmp": 42})
    cmp_hit = cache.lookup_compare("main", 1, "ref", 2, "en", {"no_auto_keep": True})
    assert cmp_hit is not None and cmp_hit["cmp"] == 42 and cmp_hit["cached"] is True

    cache.invalidate_slot("ref")
    assert cache.lookup_compare("main", 1, "ref", 2, "en", {"noAutoKeep": True}) is None
    assert cache.lookup("main", 1, "en", "summary", {}) is not None

    cache.invalidate_slot("main")
    assert cache.lookup("main", 1, "en", "summary", {}) is None


class _StubDaemonSlotWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.alive = True
        self.generation = 1
        self.restart_on_ensure = False
        self.last_used = time.time()
        self.last_good_code: str | None = f"CODE_{slot_name}"
        self.hello_info: dict[str, Any] = {"pobVersion": "2.69.0"}
        self.locale = "en"
        self.lock = threading.RLock()

    @property
    def last_active_ts(self) -> float:
        return self.last_used

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def ensure_alive(self) -> None:
        self.alive = True
        if self.restart_on_ensure:
            self.restart_on_ensure = False
            self.generation += 1
            self.last_good_code = None

    def set_locale(self, lang: str) -> dict[str, Any]:
        self.locale = lang
        return {"ok": True, "lang": lang}

    def call(self, _method: str, _params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {"ok": True}


def test_daemon_compare_cache_and_eviction_lang_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    rt = tmp_path / "rt"
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubDaemonSlotWorker)
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=True, skip_update_check=True)

    cmp_calls = 0

    def _fake_compare(
        _mw: Any, rw: Any, *, ref_source: str | None = None, ref_xml_code: str | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        nonlocal cmp_calls
        cmp_calls += 1
        if ref_source or ref_xml_code:
            history_of(rw).load_build(str(ref_source or ref_xml_code), "ref")
        return {"baselines": {"original": []}, "callIndex": cmp_calls}

    monkeypatch.setattr(comparator_mod, "compare_builds", _fake_compare)
    monkeypatch.setattr(
        "pob_cli.analyzer.extract_summary",
        lambda w: {"slot": w.slot_name, "dps": 123_456},
    )

    # Seed loaded builds on main (rev=1) and ref (rev=1)
    history_of(mgr.get_worker("main")).load_build("MAIN_V1", "main")
    history_of(mgr.get_worker("ref")).load_build("REF_V1", "ref")

    c1 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert cmp_calls == 1 and "cached" not in c1
    c2 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert cmp_calls == 1 and c2["cached"] is True and c2["callIndex"] == 1

    # Passing ref_source reloads ref and bypasses compare cache
    c_reload = mgr.dispatch("compare", {"target": "main", "ref_target": "ref", "ref_source": "REF_V2"})
    assert cmp_calls == 2 and "cached" not in c_reload

    # Subsequent compare at new (main rev=1, ref rev=2) caches cleanly
    c3 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    c4 = mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert cmp_calls == 3 and "cached" not in c3 and c4["cached"] is True

    # Populate single-slot caches on main & ref, then verify `lang` switch invalidates slots
    mgr.dispatch("summary", {"target": "main"})
    mgr.dispatch("summary", {"target": "ref"})
    invalidated: list[str] = []
    orig_inv = mgr._read_cache.invalidate_slot
    monkeypatch.setattr(mgr._read_cache, "invalidate_slot", lambda s: (invalidated.append(s), orig_inv(s))[1])

    mgr.dispatch("lang", {"lang": "zh-CN", "persist": False})
    assert {"main", "ref"}.issubset(set(invalidated))
    assert len(mgr._read_cache._entries) == 0

    # Re-populate ref + compare cache, then verify idle eviction of `ref` invalidates `ref`
    mgr.dispatch("summary", {"target": "ref"})
    mgr.dispatch("compare", {"target": "main", "ref_target": "ref"})
    assert len(mgr._read_cache._entries) == 2
    invalidated.clear()
    ref_w = mgr.get_worker("ref")
    ref_w.last_used = time.time() - REF_IDLE_TIMEOUT_SEC - 10.0  # type: ignore[attr-defined]
    mgr.check_idle()
    assert "ref" in invalidated
    assert len(mgr._read_cache._entries) == 0


# ---------------------------------------------------------------------------
# 2. Mutation `rev` contract assertion across BuildTransaction & all mutators
# ---------------------------------------------------------------------------


class _ContractMutatorWorker:
    """Deterministic fake worker supporting all mutator RPCs for the `rev` increment contract."""

    def __init__(self) -> None:
        self.slot_name = "main"
        self.runtime_dir = None
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {"cowSnapshot": True, "directCalc": True}
        self._seq = 0
        self._snaps: dict[str, str] = {}
        self._state = "INIT"
        self.last_good_code: str | None = "INIT"
        self._groups: list[dict[str, Any]] = [
            {
                "index": 1,
                "label": "Main",
                "enabled": True,
                "isMain": True,
                "gems": [
                    {"index": 1, "name": "Arc", "nameSpec": "Arc", "level": 20, "quality": 20, "enabled": True},
                    {"index": 2, "name": "Added Lightning Damage", "nameSpec": "Added Lightning Damage", "level": 20, "quality": 20, "enabled": True, "support": True},
                ],
            }
        ]

    def call_batch(self, calls: list[tuple[str, dict[str, Any] | None]], **_: Any) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        if method == "export_code":
            self._seq += 1
            return {"code": f"CODE_{self._seq}"}
        if method == "import_code":
            self._state = str(p.get("code") or "")
            return {"ok": True}
        if method == "cow_snapshot":
            self._seq += 1
            sid = f"snap_{self._seq}"
            self._snaps[sid] = self._state
            return {"snapId": sid}
        if method == "cow_restore":
            self._state = self._snaps.get(str(p.get("snapId")), self._state)
            return {"ok": True}
        if method == "cow_drop":
            self._snaps.pop(str(p.get("snapId")), None)
            return {"ok": True}
        if method == "pump":
            return {}
        if method in ("get_stats", "direct_calc"):
            return {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0, "Life": 4_500.0}, "points": {"used": 100, "usedMax": 123}, "warnings": []}
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return {"className": "Witch", "ascendClassName": "Elementalist", "level": 95, "treeVersion": "3_30", "points": {"used": 100, "usedMax": 123}}
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1}}
        if method == "list_warnings":
            return {"warnings": []}
        if method == "list_items":
            return {
                "slots": [
                    {"name": "Amulet", "selItemId": 1, "shown": True},
                    {"name": "Helmet", "selItemId": 2, "shown": True},
                    {"name": "Flask 1", "selItemId": 3, "isFlask": True, "active": True, "shown": True},
                ],
                "items": [
                    {"id": 1, "name": "Agate Amulet", "type": "Amulet", "rarity": "RARE", "raw": "Rarity: RARE\nTorc\nAgate Amulet\nImplicits: 1\n+24 to Strength and Intelligence\n+50 to maximum Life"},
                    {"id": 2, "name": "Hubris Circlet", "type": "Helmet", "rarity": "RARE", "raw": "Rarity: RARE\nBrow\nHubris Circlet\nImplicits: 0\n+60 to maximum Life"},
                    {"id": 3, "name": "Ruby Flask", "type": "Flask", "rarity": "MAGIC"},
                ],
            }
        if method == "item_raw":
            return {"raw": "Rarity: RARE\nTorc\nAgate Amulet\nItem Level: 85\nImplicits: 1\n+24 to Strength and Intelligence\n+50 to maximum Life"}
        if method == "add_item":
            return {"item": {"id": 9, "name": "Added", "type": "Helmet", "primarySlot": p.get("slotName") or "Helmet"}}
        if method == "item_db":
            return {"items": [{"index": 1, "name": "Goldrim", "baseName": "Leather Cap", "type": "Helmet", "raw": "Rarity: UNIQUE\nGoldrim\nLeather Cap\nImplicits: 0\n+35% to all Elemental Resistances"}]}
        if method == "item_edit_begin":
            return {"name": "Agate Amulet", "actions": {"anoint": True}}
        if method == "item_edit_popup":
            return {"controls": [{"name": "notableDB", "options": [{"id": 1, "label": "Allocate Whispers of Doom"}]}]}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": [dict(g, gems=[dict(gm) for gm in g["gems"]]) for g in self._groups]}
        if method == "gem_search":
            q = str(p.get("query") or "Arc")
            return {"gems": [{"gemId": f"Metadata/Items/Gems/{q}", "name": q, "support": "Support" in q}]}
        if method == "add_group":
            idx = len(self._groups) + 1
            self._groups.append({"index": idx, "label": p.get("label") or "", "enabled": True, "gems": []})
            return {"index": idx}
        if method == "add_gem":
            return {"gem": {"index": 3, "name": p.get("nameSpec") or "Gem", "level": 20, "quality": 20, "enabled": True}}
        if method == "set_gem":
            return {"gem": {"index": 1, "name": "Arc", "level": p.get("level", 21), "quality": 20, "enabled": True}}
        if method == "get_tree_state":
            return {"treeVersion": "3_30", "allocatedNodes": [100], "classes": [{"id": 3, "name": "Witch", "ascendancies": [{"id": 1, "name": "Elementalist"}]}]}
        if method == "list_classes":
            return {
                "current": {"classId": 3, "className": "Witch", "ascendClassId": 1, "ascendClassName": "Elementalist"},
                "classes": [{"id": 3, "name": "Witch", "ascendancies": [{"id": 1, "name": "Elementalist"}]}],
                "secondary": [],
            }
        if method == "node_hover":
            nid = int(p.get("id", 101))
            return {"id": nid, "name": f"Node {nid}", "allocated": nid == 100, "cost": 1, "path": [nid]}
        if method == "list_config":
            return {"activeConfigSetId": 1, "configSets": [{"id": 1, "title": "Default"}], "sections": [{"name": "General", "items": [{"var": "usePowerCharges", "type": "check", "value": False}]}], "customMods": []}
        if method == "list_specs":
            return {"activeSpecIndex": 1, "specs": [{"index": 1, "title": "Default"}]}
        return {"ok": True}

    def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
        return {
            "nodes": {"100": {"id": 100, "name": "Start", "type": "Normal"}, "101": {"id": 101, "name": "Heart and Soul", "type": "Notable"}},
            "classes": [{"id": 3, "name": "Witch", "ascendancies": [{"id": 1, "name": "Elementalist"}]}],
        }


def test_mutation_rev_contract_across_transactions_and_all_mutators(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _ContractMutatorWorker()
    hist = history_of(w)
    assert hist.rev == 0

    # 1. Direct BuildTransaction commit vs rollback
    with BuildTransaction(w, scope=BuildScope.ACTIVE_SETS, will_commit=False) as tx_ro:
        tx_ro.rollback()
    assert hist.rev == 0

    with BuildTransaction(w, scope=BuildScope.ACTIVE_SETS, will_commit=True) as tx_rw:
        tx_rw.commit(after_code="COMMITTED_1")
    assert hist.rev == 1

    # 2. What-if `run_mutation(apply=False)` NEVER bumps `rev`; `apply=True` ALWAYS bumps `rev`
    run_mutation(w, lambda _w: {"ok": True}, apply=False)  # type: ignore[arg-type]
    assert hist.rev == 1
    run_mutation(w, lambda _w: {"ok": True}, apply=True)  # type: ignore[arg-type]
    assert hist.rev == 2

    # 3. `undo` and `redo` both bump `rev` monotonically
    undo_build(w)  # type: ignore[arg-type]
    assert hist.rev == 3
    redo_build(w)  # type: ignore[arg-type]
    assert hist.rev == 4

    # 4. `load_build` bumps `rev`
    monkeypatch.setattr("pob_cli.importers.load_build_into_worker", lambda *_a, **_kw: {"code": "LOADED"})
    load_build(w, xml_code="LOADED")  # type: ignore[arg-type]
    assert hist.rev == 5

    # 5. Exercise every mutating RPC handler in MUTATE_ACTION_HANDLERS with apply=True
    dummy_mgr: Any = type("_M", (), {"_attach_state_meta": staticmethod(daemon_mod.DaemonManager._attach_state_meta)})()
    mutating_cases: list[tuple[str, dict[str, Any]]] = [
        ("new_build", {"className": "Witch", "apply": True}),
        ("switch_main", {"skill_group": 1, "apply": True}),
        ("item_try", {"slot": "Helmet", "raw": "Rarity: RARE\nCap\nHubris Circlet\n+50 to maximum Life", "apply": True}),
        ("item_edit", {"slot": "Amulet", "add_mods": ["+20% to Fire Resistance"], "allow_illegal_affixes": True, "apply": True}),
        ("item_unequip", {"slot": "Helmet", "apply": True}),
        ("item_db", {"query": "Goldrim", "index": 1, "equip": True, "slot": "Helmet", "apply": True}),
        ("item_flask", {"flask_index": 1, "active": False, "apply": True}),
        ("item_anoint", {"slot": "Amulet", "notable": "Whispers of Doom", "index": 1, "apply": True}),
        ("skill_set", {"group": 1, "gem": 1, "level": 21, "apply": True}),
        ("skill_add", {"group": 1, "gem": "Faster Casting Support", "apply": True}),
        ("skill_rm", {"group": 1, "gem": 2, "apply": True}),
        ("skill_new_group", {"gems": ["Vitality"], "label": "Aura", "apply": True}),
        ("skill_replace_group", {"group": 1, "gems": ["Arc"], "apply": True}),
        ("tree_change", {"allocate": ["101"], "apply": True}),
        ("tree_set_class", {"class_name": "Witch", "ascendancy_name": "Elementalist", "apply": True}),
        ("config_set", {"assignments": [["usePowerCharges", True]], "apply": True}),
        ("batch", {"ops": [{"op": "config", "set": {"usePowerCharges": False}}], "apply": True}),
        ("batch", {"ops": [{"op": "config", "set": {"usePowerCharges": True}}], "apply": True, "no_undo": True}),
    ]
    for action_name, params in mutating_cases:
        before_rev = hist.rev
        rpc_handlers.dispatch_slot_action(dummy_mgr, action_name, params, w)  # type: ignore[arg-type]
        assert hist.rev == before_rev + 1, f"Mutating action {action_name!r} failed to increment rev"


# ---------------------------------------------------------------------------
# 3. AGY05 Timeless: 0ms `Putrid Seed`, `--socket` multi/all, `radiusSummary`, item cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_jewel", ["Putrid Seed", "putrid seed", "腐朽之种", "腐败之种", "腐敗之種"])
def test_putrid_seed_fails_in_0ms_before_spawning_workers(
    bad_jewel: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[Any] = []

    class _NeverSpawnEngine:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            spawned.append(True)

    monkeypatch.setattr(timeless, "Engine", _NeverSpawnEngine)
    t0 = time.perf_counter()
    with pytest.raises(CliUsageError) as exc_info:
        timeless.search("CODE", "", 9, bad_jewel, pob_dir=tmp_path)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    msg = str(exc_info.value)
    slack = max(1.0, float(os.environ.get("POB_TEST_PERF_SLACK", "1") or 1.0))
    assert elapsed_ms < 200.0 * slack
    assert spawned == []
    assert "Festering Vengeance" in msg and "溃烂复仇" in msg
    assert "Putrid Seed" in msg and "Mycelium Core" in msg

    # CLI check: fails in 0ms BEFORE contacting daemon
    daemon_called: list[str] = []
    monkeypatch.setattr(cli_timeless, "_daemon_call", lambda a, *_args, **_kw: daemon_called.append(a) or {})
    res_cli = CliRunner().invoke(main, ["timeless", "search", "--jewel", bad_jewel, "--socket", "9"])
    assert res_cli.exit_code == 1
    assert daemon_called == []
    assert "Festering Vengeance" in res_cli.output


def test_non_putrid_seed_jewel_names_not_misrouted_and_outcome_tiebreak_cutoff() -> None:
    for valid_or_other in ("Festering Vengeance", "溃烂复仇", "Lethal Pride", "致命的骄傲", "Unknown Seed"):
        _check_putrid_seed(valid_or_other)  # must NOT raise Putrid Seed CliUsageError

    # Duplicate signatures scoring 100 must NOT drop the 2nd distinct outcome (90) when top=2
    s100 = {k: 100.0 for k in timeless.STAT_KEYS}
    s90 = {k: 90.0 for k in timeless.STAT_KEYS}
    cutoff, outs = _build_search_outcomes(
        {b"a": s100, b"b": s100, b"c": s90},
        {b"a": [1], b"b": [2], b"c": [3]},
        "CombinedDPS",
        2,
        {"method": "narrowDown"},
    )
    assert cutoff == 90.0
    assert [o["seeds"] for o in outs] == [[1, 2], [3]]

    # CombinedDPS tie-break applied BEFORE weightShortlist top=1 truncation when ranking by TotalEHP
    low_dps = {**dict.fromkeys(timeless.STAT_KEYS, 0.0), "TotalEHP": 50_000.0, "CombinedDPS": 100_000.0}
    high_dps = {**dict.fromkeys(timeless.STAT_KEYS, 0.0), "TotalEHP": 50_000.0, "CombinedDPS": 250_000.0}
    _, sl_outs = _build_search_outcomes(
        {b"low": low_dps, b"high": high_dps},
        {b"low": [10], b"high": [20]},
        "TotalEHP",
        1,
        {"method": "weightShortlist"},
    )
    assert len(sl_outs) == 1 and sl_outs[0]["seeds"] == [20]


def test_parse_socket_spec_and_multi_socket_single_pool_and_radius_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli_timeless.parse_socket_spec("all") == "all"
    assert cli_timeless.parse_socket_spec("ALL") == "all"
    assert cli_timeless.parse_socket_spec("61419") == 61419
    assert cli_timeless.parse_socket_spec(61419) == 61419
    assert cli_timeless.parse_socket_spec("61419,6230") == [61419, 6230]
    assert cli_timeless.parse_socket_spec("61419, 6230, 61419") == [61419, 6230]
    for bad in ("", "abc", "61419,", "61419,xyz"):
        with pytest.raises(ValueError):
            cli_timeless.parse_socket_spec(bad)

    (tmp_path / "Data" / "Uniques").mkdir(parents=True)
    (tmp_path / "Data" / "Uniques" / "jewel.lua").write_text(TIMELESS_JEWEL_LUA, encoding="utf-8")

    made_engines: list[_MultiSocketFakeEngine] = []

    class _MultiSocketFakeEngine:
        game_dir = tmp_path

        def __init__(self, code: str, template: str, slot: str, clear: Any = (), **_kw: Any) -> None:
            self.code, self.template, self.slot, self.clear = code, template, slot, list(clear)
            self.worker = self
            made_engines.append(self)

        def reset(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def read(self) -> dict[str, float]:
            return dict.fromkeys(timeless.STAT_KEYS, 1000.0)

        def stats(self, seed: int) -> dict[str, float]:
            sock_bonus = 500.0 if self.slot == "Jewel 6230" else 100.0
            return dict.fromkeys(timeless.STAT_KEYS, 1000.0 + sock_bonus + float(seed))

        def call(self, method: str, _params: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
            if method == "get_tree_state":
                conquered = (
                    {"101": {"why": "conquered"}, "102": {"why": "conquered"}}
                    if self.slot == "Jewel 61419"
                    else {"201": {"why": "conquered"}, "202": {"why": "conquered"}}
                )
                return {
                    "treeVersion": "3_30",
                    "allocatedNodes": [61419, 6230, 101, 201],
                    "overrides": conquered,
                    "sockets": [{"nodeId": 61419}, {"nodeId": 6230}, {"nodeId": 9999}],
                }
            if method == "tj_open":
                return {
                    "jewel": {"options": []},
                    "socketIds": [-1, 61419, 6230, 9999],
                    "socket": {
                        "options": [
                            {"label": "All"},
                            {"label": "Near Marauder"},
                            {"label": "Near Templar"},
                            {"label": "Unallocated"},
                        ]
                    },
                }
            if method == "tj_close":
                return {"ok": True}
            raise AssertionError(f"Unexpected RPC {method}")

        def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
            return {
                "nodes": {
                    "101": {"id": 101, "name": "Golem's Blood", "type": "Notable"},
                    "102": {"id": 102, "name": "Cloth and Chain", "type": "Notable"},
                    "201": {"id": 201, "name": "Devotion", "type": "Notable"},
                    "202": {"id": 202, "name": "Sanctity", "type": "Notable"},
                }
            }

    monkeypatch.setattr(timeless, "Engine", _MultiSocketFakeEngine)

    # Single int preserves backward-compatible single-socket schema + includes radiusSummary
    single = timeless.search("CODE", "", 61419, "Festering Vengeance", top=3, workers=2, pob_dir=tmp_path)
    assert single["socket"] == 61419 and "socketResults" not in single
    assert single["radiusSummary"] == {
        "allocatedNodeIds": [101],
        "allocatedNotables": [{"id": 101, "name": "Golem's Blood"}],
        "unallocatedNotables": [{"id": 102, "name": "Cloth and Chain"}],
    }
    md_single = cli_timeless._format_search_markdown(single, "CombinedDPS", 3)
    assert "allocated (0 extra points, 1): Golem's Blood (101)" in md_single
    assert "unallocated in radius (1): Cloth and Chain (102)" in md_single

    # Multi-socket ("all" and [61419, 6230]) reuses ONE TimelessSearchEngine pool (2 workers total, NOT 4!)
    made_engines.clear()
    multi = timeless.search("CODE", "", "all", "Festering Vengeance", top=3, workers=2, pob_dir=tmp_path)
    assert len(made_engines) == 2
    assert multi["sockets"] == [61419, 6230]
    assert len(multi["socketResults"]) == 2
    assert multi["topSeeds"][0]["socket"] == 6230
    assert multi["topSeeds"][0]["seed"] == 104
    assert multi["topSeeds"][0]["socketLabel"] == "Near Templar"

    # CLI `--socket 61419,6230 --json` & Markdown
    monkeypatch.setattr(
        cli_timeless,
        "_daemon_call",
        lambda _a, _p=None: {"code": "CODE", "xml": "", "pobDir": str(tmp_path)},
    )
    made_engines.clear()
    cli_res = CliRunner().invoke(
        main,
        ["timeless", "search", "--jewel", "Festering Vengeance", "--socket", "61419,6230", "--workers", "2", "--json"],
    )
    assert cli_res.exit_code == 0, cli_res.output
    assert len(made_engines) == 2
    parsed_multi = json.loads(cli_res.stdout)
    assert [r["socket"] for r in parsed_multi["socketResults"]] == [61419, 6230]
    assert parsed_multi["topSeeds"][0]["socket"] == 6230
    assert isinstance(TimelessSearchEngine, type)


def test_timeless_worker_recalculates_once_per_seed_and_reloads_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seed costs one recalculating RPC. Deleting the replaced jewel would be a second one
    (`delete_item` ends in a frame like every bridge edit), so the item list is cleared by a reload
    every RESET_EVERY seeds instead."""
    batches: list[list[tuple[str, dict[str, Any] | None]]] = []
    recalculating = {"add_item", "delete_item", "unequip_slot", "equip_item", "import_code"}

    class _CountingWorker:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.last_good_code: str | None = None

        def start(self, *, restore_code: str | None = None) -> None:
            self.last_good_code = restore_code

        def call_batch(self, calls: list[tuple[str, dict[str, Any] | None]], timeout: float = 60.0) -> list[dict[str, Any]]:
            batches.append(list(calls))
            # `add_item` answers with the new item's id, as the bridge does: the id is what a
            # per-seed delete would need, so a reply without one could not catch its return.
            answers = {"get_stats": {"stats": {"CombinedDPS": 5000.0}}, "add_item": {"item": {"id": len(batches)}}}
            return [answers.get(m, {"ok": True}) for m, _p in calls]

        def stop(self) -> None:
            pass

    monkeypatch.setattr(timeless, "BridgeWorker", _CountingWorker)
    eng = timeless.Engine("CODE", "seed {seed}", "Jewel 9")
    seeds = RESET_EVERY * 2 + 20
    for s in range(seeds):
        eng.stats(1000 + s)

    all_methods = [m for b in batches for m, _ in b]
    assert all_methods.count("add_item") == seeds
    assert "delete_item" not in all_methods
    assert all_methods.count("import_code") == 2  # one reload per RESET_EVERY seeds, none at the start
    assert sum(m in recalculating for m in all_methods) == seeds + 2
    seed_batches = [b for b in batches if any(m == "add_item" for m, _ in b)]
    assert all([m for m, _ in b] == ["add_item", "get_stats"] for b in seed_batches)


# ---------------------------------------------------------------------------
# 4. Trade `--surrogate-k` + `--pob-limit` slicing & `tx.restore(rebuild=False)`
# ---------------------------------------------------------------------------


class _TradeCowDirectWorker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.caps = {"cowSnapshot": True, "directCalc": True, "agentProbes": True}
        self._proc = object()
        self.last_good_code = "TRADE_BASE"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._active_life = 4000.0

    def call_batch(self, calls: list[tuple[str, dict[str, Any] | None]], **_: Any) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "cow_snapshot":
            return {"snapId": "cow_1"}
        if method == "cow_restore":
            self._active_life = 4000.0
            return {"ok": True}
        if method == "cow_drop":
            return {"ok": True}
        if method == "pump":
            return {}
        if method == "compute_grad_build":
            return {"stat": "CombinedDPS", "baseStat": 100_000.0, "weights": {"flat_life": {"scorePerUnit": 1.0}}}
        if method == "list_items":
            return {"slots": [{"name": "Ring 1", "selItemId": 1, "shown": True}], "items": [{"id": 1, "name": "Old", "type": "Ring"}]}
        if method == "add_item":
            raw = str(p.get("raw") or "")
            bonus = 0.0
            for line in raw.splitlines():
                if "to maximum Life" in line:
                    bonus = float(line.split("+", 1)[1].split(" ", 1)[0])
            self._active_life = 4000.0 + bonus
            return {"item": {"id": 2, "type": "Ring"}}
        if method == "direct_calc":
            return {
                "stats": {"CombinedDPS": 100_000.0 + (self._active_life - 4000.0) * 100.0, "TotalEHP": 50_000.0, "Life": self._active_life},
                "points": {"used": 100, "usedMax": 123},
                "warnings": [],
            }
        if method == "get_stats":
            return {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0, "Life": self._active_life}}
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return {"className": "Witch", "level": 95, "points": {"used": 100, "usedMax": 123}}
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1}}
        raise AssertionError(f"Unexpected RPC {method}")


def test_surrogate_k_plus_pob_limit_slicing_and_cow_restore_no_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _TradeCowDirectWorker()
    raw_items = [
        {"id": i, "raw": f"Rarity: RARE\nRing {i}\nRuby Ring\nImplicits: 0\n+{10 + i} to maximum Life"}
        for i in range(60)
    ]
    res = eval_trade_items(w, slot_name="Ring 1", items=raw_items, prefilter_k=50, pob_limit=4)
    assert len(res["results"]) == 4
    assert [r["id"] for r in res["results"]] == [59, 58, 57, 56]

    add_calls = [p for m, p in w.calls if m == "add_item"]
    assert len(add_calls) == 4  # ONLY pob_limit=4 full PoB evaluations, NOT 50!

    restores = [p for m, p in w.calls if m == "cow_restore"]
    assert len(restores) == 4  # 3 intermediate + 1 final rollback(drop=True)
    assert all(r.get("rebuild") is False for r in restores[:-1])
    assert restores[-1].get("drop") is True and "rebuild" not in restores[-1]

    # End-to-end CLI -> rpc_handlers._h_trade_eval_items -> eval_trade_items with --surrogate-k 50 --pob-limit 3
    mock_stats = [{"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"}]
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: (mock_stats, "mock"))
    monkeypatch.setattr(cli_trade, "resolve_trade_league", lambda *_a, **_kw: ("Keepers", None))

    # Give low indices high price/irrelevant order, high indices (50..54) highest Life so surrogate picks 54, 53, 52
    rows_by_id = {
        f"id_{i}": {
            "id": f"id_{i}",
            "item": {
                "name": f"Band {i}",
                "typeLine": "Ruby Ring",
                "baseType": "Ruby Ring",
                "frameType": 2,
                "ilvl": 85,
                "identified": True,
                "explicitMods": [f"+{20 + i} to maximum Life"],
            },
            "listing": {"account": {"name": f"Seller#{i}"}, "price": {"amount": 10, "currency": "chaos"}},
        }
        for i in range(55)
    }

    class _FakeTradeClient(TradeClient):
        def search(self, _league: str, _query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": len(rows_by_id), "result": list(rows_by_id)}

        def fetch_many(self, ids: list[str], _qid: str, limit: int = 10) -> list[dict[str, Any]]:
            return [rows_by_id[k] for k in ids[: min(limit, MAX_FETCH_MANY)]]

    w_e2e = _TradeCowDirectWorker()

    def _daemon_bridge(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        if action == "items":
            return {"raw": "Rarity: RARE\nEquipped\nRuby Ring\nImplicits: 0\n+25 to maximum Life"}
        if action == "trade_eval_items":
            return rpc_handlers._h_trade_eval_items(object(), p, w_e2e)  # type: ignore[arg-type]
        raise AssertionError(f"Unexpected action {action}")

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeTradeClient)
    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _daemon_bridge)

    cli_out = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "ring1", "--stat", "maximum Life=20", "--surrogate-k", "50", "--pob-limit", "3", "--pob", "on", "--json"],
    )
    assert cli_out.exit_code == 0, cli_out.output
    assert sum(1 for m, _ in w_e2e.calls if m == "add_item") == 3
    report = json.loads(cli_out.stdout)
    assert report["fetched"] == 50
    top3 = report["candidates"][:3]
    assert all(c["pob"] is not None for c in top3)
    assert [c["listing_id"] for c in top3] == ["id_49", "id_48", "id_47"]
