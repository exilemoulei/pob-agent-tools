"""Unit tests for `pob_cli.timeless` (no PoB engine needed) plus one check against real engine data when present."""

from __future__ import annotations

import random
import signal
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pob_cli import timeless
from pob_cli.importers import xml_to_share_code

JEWEL_LUA = """return {
[[
Emperor's Wit
Cobalt Jewel
(4-6)% increased Intelligence
]],
-- Jewel: Timeless
[[
Brutal Restraint
Timeless Jewel
League: Legion
Limited to: 1 Historic
Variant: Asenath (Dance with Death)
Variant: Balbala (The Traitor)
Radius: Large
Implicits: 0
{variant:1}Denoted service of (500-8000) dekhara in the akhara of Asenath
{variant:2}Denoted service of (500-8000) dekhara in the akhara of Balbala
Passives in radius are Conquered by the Maraketh
Historic
]],[[
Elegant Hubris
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
Commissioned (2000-160000) coins to commemorate Cadiro
Passives in radius are Conquered by the Eternal Empire
Historic
]],[[
Glorious Vanity
Timeless Jewel
Limited to: 1 Historic
Variant: Doryani (Corrupted Soul)
Variant: Mazcotl (Immortal Ambition)
Radius: Large
Implicits: 0
{variant:1}Bathed in the blood of (100-8000) sacrificed in the name of Doryani
{variant:2}Bathed in the blood of (100-8000) sacrificed in the name of Mazcotl
Passives in radius are Conquered by the Vaal
Historic
]],[[
Lethal Pride
Timeless Jewel
Limited to: 1 Historic
Variant: Kaom (Strength of Blood)
Variant: Akoya (Chainbreaker)
Radius: Large
Implicits: 0
{variant:1}Commanded leadership over (10000-18000) warriors under Kaom
{variant:2}Commanded leadership over (10000-18000) warriors under Akoya
Passives in radius are Conquered by the Karui
Historic
]],[[
Militant Faith
Timeless Jewel
Limited to: 1 Historic
Variant: Dominus (Inner Conviction)
Variant: Totem Damage
Radius: Large
Implicits: 0
{variant:1}Carved to glorify (2000-10000) new faithful converted by High Templar Dominus
{variant:2}4% increased Totem Damage per 10 Devotion
Passives in radius are Conquered by the Templars
Historic
]],[[
Festering Vengeance
Murderous Eye Jewel
Limited to: 1 Historic
Implicits: 0
Subjugating (100-8000) souls in the thrall of Tecrod
Passives affected are Conquered by the Abyssal
Historic
]],
}
"""


@pytest.fixture
def game_dir(tmp_path: Path) -> Path:
    (tmp_path / "Data" / "Uniques").mkdir(parents=True)
    (tmp_path / "Data" / "Uniques" / "jewel.lua").write_text(JEWEL_LUA, encoding="utf-8")
    return tmp_path


def test_load_jewels_reads_only_timeless_entries(game_dir: Path) -> None:
    jewels = timeless.load_jewels(game_dir)
    # the entry after a `-- comment` separator must not be glued to the item before it
    assert list(jewels) == [
        "Brutal Restraint",
        "Elegant Hubris",
        "Glorious Vanity",
        "Lethal Pride",
        "Militant Faith",
        "Festering Vengeance",
    ]
    assert jewels["Brutal Restraint"]["base"] == "Timeless Jewel"
    assert list(jewels["Brutal Restraint"]["conquerors"]) == ["Asenath", "Balbala"]
    assert jewels["Brutal Restraint"]["seeds"] == range(500, 8001)
    assert jewels["Festering Vengeance"]["base"] == "Murderous Eye Jewel"


def test_elegant_hubris_seeds_step_by_twenty(game_dir: Path) -> None:
    seeds = timeless.load_jewels(game_dir)["Elegant Hubris"]["seeds"]
    assert (seeds[0], seeds[1], seeds[-1], len(seeds)) == (2000, 2020, 160000, 7901)


def test_jewel_text_fills_conqueror_and_keeps_optional_mods_out(game_dir: Path) -> None:
    jewels = timeless.load_jewels(game_dir)
    text = timeless.jewel_text(jewels["Brutal Restraint"], "Brutal Restraint", "Balbala").format(seed=777)
    assert text.splitlines() == [
        "Rarity: UNIQUE", "Brutal Restraint", "Timeless Jewel", "Item Level: 84", "Limited to: 1 Historic",
        "Radius: Large", "Implicits: 0", "Denoted service of 777 dekhara in the akhara of Balbala",
        "Passives in radius are Conquered by the Maraketh", "Historic",
    ]
    faith = timeless.jewel_text(jewels["Militant Faith"], "Militant Faith")
    assert "Totem Damage" not in faith  # devotion mods only when asked for
    asked = timeless.jewel_text(jewels["Militant Faith"], "Militant Faith", mods=["4% increased Totem Damage per 10 Devotion"])
    assert asked.splitlines()[-3] == "4% increased Totem Damage per 10 Devotion"


def test_conqueror_case_insensitive_and_ahuana_mazcotl_alias(game_dir: Path) -> None:
    jewels = timeless.load_jewels(game_dir)
    # Case-insensitive match
    lp = timeless.jewel_text(jewels["Lethal Pride"], "Lethal Pride", "kaom").replace("{seed}", "12345")
    assert "warriors under Kaom" in lp
    # 3.30 PoB has Mazcotl -> passing legacy Ahuana (any case) resolves to Mazcotl
    gv_330 = timeless.jewel_text(jewels["Glorious Vanity"], "Glorious Vanity", "ahuana").replace("{seed}", "500")
    assert "name of Mazcotl" in gv_330
    # Pre-3.30 PoB has Ahuana -> passing Mazcotl resolves to Ahuana
    legacy_gv = {
        **jewels["Glorious Vanity"],
        "conquerors": {"Doryani": "Bathed in the blood of {seed} sacrificed in the name of Doryani",
                       "Ahuana": "Bathed in the blood of {seed} sacrificed in the name of Ahuana"},
    }
    gv_pre = timeless.jewel_text(legacy_gv, "Glorious Vanity", "mazcotl").replace("{seed}", "500")
    assert "name of Ahuana" in gv_pre
    # Invalid conqueror still rejected
    with pytest.raises(ValueError, match="Lethal Pride conquerors"):
        timeless.jewel_text(jewels["Lethal Pride"], "Lethal Pride", "Ahuana")


def test_group_seeds_only_splits_on_the_given_nodes() -> None:
    seeds = [10, 11, 12, 13]
    index = {100: 0, 200: 1}
    #               node 100    node 200  (one byte per seed)
    table = bytes([1, 1, 2, 2,  5, 6, 5, 6])
    assert timeless.group_seeds(seeds, table, index, [100]) == {b"\x01": [10, 11], b"\x02": [12, 13]}
    assert list(timeless.group_seeds(seeds, table, index, [100, 200]).values()) == [[10], [11], [12], [13]]
    assert timeless.group_seeds(seeds, table, index, []) == {b"": [10, 11, 12, 13]}  # nothing seed-dependent is used


def test_fit_pieces_recovers_additive_effects_for_unseen_combinations() -> None:
    effect = [{1: 0.0, 2: 5.0, 3: -2.0}, {1: 0.0, 2: 1.0, 3: 7.0}, {1: 0.0, 2: -4.0, 3: 3.0}]

    def true_value(sig: bytes) -> float:
        return 100.0 + sum(effect[p][sig[p]] for p in range(3))

    every = [bytes([a, b, c]) for a in (1, 2, 3) for b in (1, 2, 3) for c in (1, 2, 3)]
    seen, unseen = every[::2], every[1::2]
    predict = timeless.fit_pieces([(sig, true_value(sig)) for sig in seen])
    assert max(abs(predict(sig) - true_value(sig)) for sig in unseen) < 1e-6
    assert max(unseen, key=predict) == max(unseen, key=true_value)


def test_narrow_down_finds_a_rare_valuable_byte_without_calculating_everything() -> None:
    # 3 nodes x 12 table bytes; byte 99 on node 0 is rare (5 groups of 1733) and worth far more than the rest
    sigs = [bytes([a, b, c]) for a in range(12) for b in range(12) for c in range(12)]
    sigs += [bytes([99, b, 0]) for b in range(5)]
    truth = {sig: 1000.0 * (1 + sig[0] / 50) * (1 + sig[1] / 80) * (1 + sig[2] / 200) * (3 if sig[0] == 99 else 1)
             for sig in sigs}
    for rng_seed in range(20):  # a purely random first sample misses byte 99 in most of these
        results: dict[bytes, dict[str, float]] = {}
        estimate = timeless.narrow_down(
            sigs, results, lambda todo, _res=results: _res.update({sig: {"X": truth[sig]} for sig in todo}),
            "X", 5, random.Random(rng_seed), lambda _msg: None,
        )
        assert estimate is not None and len(results) < len(sigs) // 2
        assert sorted(truth, key=truth.get, reverse=True)[:5] == sorted(results, key=lambda s: truth[s], reverse=True)[:5]


def test_engine_keeps_braces_in_item_lines_and_takes_other_jewels_out_after_each_reload() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeWorker:
        def call(self, method: str, params: dict, timeout: float = 0) -> dict:
            calls.append((method, params))
            return {}

    engine = timeless.Engine.__new__(timeless.Engine)  # no PoB process
    engine.code, engine.template, engine.slot, engine.clear, engine.count = "CODE", "{crafted}x\nseed {seed}", "Jewel 1", ["Jewel 2"], 0
    engine.worker = FakeWorker()  # type: ignore[assignment]
    engine.stats(777)
    assert [method for method, _ in calls] == ["import_code", "unequip_slot", "add_item", "get_stats"]
    assert calls[1][1] == {"slotName": "Jewel 2"}
    assert calls[2][1]["raw"] == "{crafted}x\nseed 777"


def test_engine_call_batch_pipelining_and_no_double_import(monkeypatch: pytest.MonkeyPatch) -> None:
    batches: list[list[tuple[str, dict[str, Any] | None]]] = []
    start_restore_codes: list[str | None] = []

    class BatchFakeWorker:
        def __init__(self, slot_name: str, pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_dir = pob_dir
            self.runtime_dir = runtime_dir
            self.last_good_code: str | None = None

        def start(self, *, restore_code: str | None = None) -> None:
            start_restore_codes.append(restore_code)
            self.last_good_code = restore_code

        def call_batch(
            self,
            calls: list[tuple[str, dict[str, Any] | None]],
            timeout: float = 60.0,
        ) -> list[dict[str, Any]]:
            batches.append(list(calls))
            return [{"stats": {"CombinedDPS": 1234.0}} if m == "get_stats" else {} for m, _ in calls]

        def stop(self) -> None:
            pass

    monkeypatch.setattr(timeless, "BridgeWorker", BatchFakeWorker)
    engine = timeless.Engine("CODE_A", "seed {seed}", "Jewel 9", clear=["Jewel 7"])
    assert start_restore_codes == ["CODE_A"]
    engine.ensure_loaded()
    assert batches == []  # no redundant import_code after start(restore_code=code)

    s1 = engine.stats(100)
    assert s1["CombinedDPS"] == 1234.0
    # First seed pipelines pending unequip_slot + add_item + get_stats in ONE batch without import_code
    assert [m for m, _ in batches[0]] == ["unequip_slot", "add_item", "get_stats"]

    s2 = engine.stats(101)
    assert s2["CombinedDPS"] == 1234.0
    # Subsequent seeds pipeline add_item + get_stats in ONE batch
    assert [m for m, _ in batches[1]] == ["add_item", "get_stats"]

    # Periodic reset pipelines import_code + unequip_slot in ONE batch
    engine.count = timeless.RESET_EVERY
    engine.stats(102)
    assert [m for m, _ in batches[2]] == ["import_code", "unequip_slot"]
    assert [m for m, _ in batches[3]] == ["add_item", "get_stats"]


def test_seed_nodes_handles_xml_entities_crlf_active_itemset_and_code_fallback() -> None:
    xml_crlf = (
        "<PathOfBuilding>\r\n"
        "  <Items activeItemSet=\"2\">\r\n"
        "    <Item id=\"1\">\r\nRarity: RARE\r\nOld Amulet\r\n{crafted}Allocates Whispers of Doom\r\n</Item>\r\n"
        "    <Item id=\"2\">\r\nRarity: RARE\r\nActive Amulet\r\n{crafted}Allocates Golem&apos;s Blood\r\n</Item>\r\n"
        "    <ItemSet id=\"1\">\r\n      <Slot name=\"Amulet\" itemId=\"1\"/>\r\n    </ItemSet>\r\n"
        "    <ItemSet id=\"2\">\r\n      <Slot name=\"Amulet\" itemId=\"2\"/>\r\n    </ItemSet>\r\n"
        "  </Items>\r\n"
        "</PathOfBuilding>\r\n"
    )
    code = xml_to_share_code(xml_crlf)

    class FakeAnointWorker:
        def call(self, method: str, params: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
            assert method == "get_tree_state"
            return {
                "treeVersion": "3_26",
                "allocatedNodes": [101],
                "overrides": {
                    "101": {"why": "conquered"},
                    "202": {"why": "conquered"},  # Golem's Blood (anointed in active ItemSet 2)
                    "303": {"why": "conquered"},  # Whispers of Doom (inactive ItemSet 1 -> ignored)
                },
            }

        def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
            return {
                "nodes": {
                    "101": {"name": "Regular Notable"},
                    "202": {"name": "Golem's Blood"},
                    "303": {"name": "Whispers of Doom"},
                }
            }

    eng = timeless.Engine.__new__(timeless.Engine)
    eng.code = code
    eng.worker = FakeAnointWorker()  # type: ignore[assignment]
    index = {101: 0, 202: 1, 303: 2}

    # Explicit CRLF + &apos; XML scoped to active ItemSet 2
    assert timeless._seed_nodes(eng, xml_crlf, index) == [101, 202]
    # Empty XML falls back to decoding share code
    assert timeless._seed_nodes(eng, "", index, code=code) == [101, 202]
    # Raw snippet fallback (no <Items> structure) still decodes &apos; and strips \r
    assert timeless._seed_nodes(eng, "{crafted}Allocates Golem&apos;s Blood\r\n", index) == [101, 202]


def test_describe_worker_uses_call_batch_and_chinese_fallback(game_dir: Path) -> None:
    batched_methods: list[list[str]] = []

    class PosixBatchWorker:
        def __init__(self, gd: Path) -> None:
            self.game_dir = gd

        def call_batch(
            self,
            calls: list[tuple[str, dict[str, Any] | None]],
            timeout: float = 60.0,
        ) -> list[dict[str, Any]]:
            batched_methods.append([m for m, _ in calls])
            return [
                {
                    # Simulate POSIX headless_runner.lua where labelZh == label
                    "jewel": {"options": [{"label": "Lethal Pride", "labelZh": "Lethal Pride"}]},
                    "socketIds": [-1, 7, 9, 11],
                    "socket": {
                        "options": [
                            {"label": "All Sockets", "labelZh": "All Sockets"},
                            {"label": "Near Duelist", "labelZh": "Near Duelist"},
                            {"label": "Near Ranger", "labelZh": "Near Ranger"},
                            {"label": "Unallocated", "labelZh": "Unallocated"},
                        ]
                    },
                },
                {"ok": True},
                {
                    "allocatedNodes": [7, 9],
                    "sockets": [{"nodeId": 7}, {"nodeId": 9}, {"nodeId": 11}],
                },
            ]

    w = PosixBatchWorker(game_dir)
    info = timeless.describe_worker(w)
    assert batched_methods == [["tj_open", "tj_close", "get_tree_state"]]
    by_name = {j["name"]: j for j in info["jewels"]}
    assert by_name["Lethal Pride"]["nameLocal"] == "致命的骄傲"
    assert by_name["Festering Vengeance"]["nameLocal"] == "溃烂复仇"
    assert [s["nodeId"] for s in info["sockets"]] == [7, 9]

    # Sequential fallback when worker lacks call_batch
    seq_calls: list[str] = []

    class SeqOnlyWorker:
        def __init__(self, gd: Path) -> None:
            self.game_dir = gd

        def call(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
            seq_calls.append(method)
            if method == "tj_open":
                return {
                    "jewel": {"options": [{"label": "Lethal Pride", "labelZh": "致命的骄傲"}]},
                    "socketIds": [-1, 7],
                    "socket": {"options": [{"label": "All"}, {"label": "Near Duelist", "labelZh": "决斗者附近"}]},
                }
            if method == "tj_close":
                return {"ok": True}
            return {"allocatedNodes": [7], "sockets": [{"nodeId": 7}]}

    info_seq = timeless.describe_worker(SeqOnlyWorker(game_dir))
    assert seq_calls == ["tj_open", "tj_close", "get_tree_state"]
    assert info_seq["sockets"] == [{"nodeId": 7, "label": "Near Duelist", "labelLocal": "决斗者附近"}]

    # Chinese --jewel resolution works even when PoB returns English labelZh == label
    jewels = timeless.load_jewels(game_dir)
    eng = timeless.Engine.__new__(timeless.Engine)
    eng.worker = w  # type: ignore[assignment]
    assert timeless._resolve_jewel("致命的骄傲", jewels, eng) == "Lethal Pride"
    assert timeless._resolve_jewel("辉煌的虚荣", jewels, eng) == "Glorious Vanity"
    assert timeless._resolve_jewel("溃烂复仇", jewels, eng) == "Festering Vengeance"
    # Traditional spellings without a hand-written alias resolve through the fold.
    assert timeless._resolve_jewel("潰爛復仇", jewels, eng) == "Festering Vengeance"


class FakeEngine:
    """Stands in for a PoB engine. Jewel sockets 7 (holding a Brutal Restraint) and 9 are allocated, socket 11 is
    not; 101-103 are allocated notables inside the jewel's radius, 922 is one outside it. A seed is worth `value`."""

    game_dir: Path
    made: list[FakeEngine]
    value: Callable[[int], Any]
    sockets_override: list[dict[str, Any]] | None = None
    allocated_override: list[int] | None = None
    tj_socket_ids: list[int] | None = None

    def __init__(
        self,
        code: str,
        template: str,
        slot: str,
        clear: object = (),
        pob_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.code = code
        self.template = template
        self.slot = slot
        self.clear = clear
        self.pob_dir = pob_dir
        self.runtime_dir = runtime_dir
        self.worker = self
        self.made.append(self)

    def call(self, method: str, params: dict, timeout: float = 0) -> dict:
        if method == "get_tree_state":
            return {
                "allocatedNodes": self.allocated_override or [7, 9, 101, 102, 103, 922],
                "overrides": {str(node): {"why": "conquered"} for node in (101, 102, 103)},
                "sockets": self.sockets_override
                or [{"nodeId": 7, "title": "Brutal Restraint"}, {"nodeId": 9}, {"nodeId": 11}],
            }
        if method == "tj_open":
            return {
                "jewel": {"options": []},
                "socketIds": self.tj_socket_ids if self.tj_socket_ids is not None else [-1, 7, 9, 11],
                "socket": {"options": []},
            }
        if method == "tj_close":
            return {"ok": True}
        raise AssertionError(f"Unexpected RPC {method}")

    def reset(self) -> None:
        pass

    stop = reset

    def read(self) -> dict:
        return dict.fromkeys(timeless.STAT_KEYS, 0.0)

    def stats(self, seed: int) -> dict:
        return dict.fromkeys(timeless.STAT_KEYS, type(self).value(seed))


@pytest.fixture
def fake_engine(game_dir: Path, monkeypatch: pytest.MonkeyPatch) -> type[FakeEngine]:
    FakeEngine.game_dir = game_dir
    FakeEngine.made = []
    FakeEngine.value = lambda seed: 1.0
    FakeEngine.sockets_override = None
    FakeEngine.allocated_override = None
    FakeEngine.tj_socket_ids = None
    monkeypatch.setattr(timeless, "Engine", FakeEngine)
    return FakeEngine


def test_search_rejects_non_sockets_and_takes_out_a_second_historic_jewel(fake_engine: type[FakeEngine]) -> None:
    for node in (922, 11, 5):  # an allocated notable, an unallocated socket, no such node
        with pytest.raises(ValueError, match="not an allocated jewel socket"):
            timeless.search("code", "", node, "Festering Vengeance")
    res = timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    assert res["removed"] == [{"jewel": "Brutal Restraint", "socket": 7}]  # the game allows one Historic jewel
    assert [engine.clear for engine in fake_engine.made[-2:]] == [["Jewel 7"]] * 2  # ... so no engine keeps it
    # replacing it in its own socket leaves nothing else to take out
    assert timeless.search("code", "", 7, "Festering Vengeance", workers=1)["removed"] == []


def test_search_removes_finder_titled_historic_jewel_and_rejects_charm_and_inner_cluster_sockets(
    fake_engine: type[FakeEngine],
) -> None:
    fake_engine.allocated_override = [7, 9, 12, 65536, 101, 102, 103]
    fake_engine.sockets_override = [
        {"nodeId": 7, "title": "Lethal Pride 12345; 2; Marauder", "baseName": "Timeless Jewel"},
        {"nodeId": 9},
        {"nodeId": 11, "title": "Brutal Restraint"},  # unallocated -> must NOT be removed
        {"nodeId": 12, "charm": True},  # charm socket -> rejected
        {"nodeId": 65536, "expansion": True},  # inner cluster socket (absent from tj_open.socketIds) -> rejected
    ]
    fake_engine.tj_socket_ids = [-1, 7, 9, 11]

    with pytest.raises(ValueError, match="charm socket"):
        timeless.search("code", "", 12, "Festering Vengeance")
    with pytest.raises(ValueError, match="not a valid timeless jewel socket"):
        timeless.search("code", "", 65536, "Festering Vengeance")

    res = timeless.search("code", "", 9, "Festering Vengeance", workers=1)
    assert res["removed"] == [{"jewel": "Lethal Pride 12345; 2; Marauder", "socket": 7}]
    assert fake_engine.made[-1].clear == ["Jewel 7"]


def test_search_and_narrow_down_handle_inf_nan_and_none_stats(fake_engine: type[FakeEngine]) -> None:
    # fit_pieces ignores non-finite samples cleanly
    pred = timeless.fit_pieces([(b"\x01", 10.0), (b"\x02", float("inf")), (b"\x03", float("nan"))])
    assert pred(b"\x01") == pytest.approx(10.0)

    # narrow_down encountering "inf" / None / "nan" falls back to exact without crashing or nan poisoning
    sigs = [bytes([a, b]) for a in range(30) for b in range(30)]  # 900 sigs > 3 * 200
    truth: dict[bytes, Any] = {s: (1000.0 + s[0] * 10 + s[1]) for s in sigs}
    truth[bytes([0, 0])] = "inf"
    truth[bytes([0, 1])] = None
    truth[bytes([0, 2])] = "nan"
    results: dict[bytes, dict[str, Any]] = {}
    est = timeless.narrow_down(
        sigs,
        results,
        lambda todo: results.update({s: {"TotalEHP": truth[s]} for s in todo}),
        "TotalEHP",
        5,
        random.Random(0),
        lambda _m: None,
    )
    assert est is None and len(results) == len(sigs)

    # Full search with "inf", None, "nan", and finite stats ranks "inf" highest and never raises TypeError
    def weird_value(seed: int) -> Any:
        if seed == 150:
            return "inf"
        if seed == 151:
            return None
        if seed == 152:
            return "nan"
        return float(seed)

    fake_engine.value = weird_value
    res = timeless.search("code", "", 9, "Festering Vengeance", stat="TotalEHP", workers=1)
    assert res["outcomes"][0]["stats"]["TotalEHP"] == "inf"
    assert res["outcomes"][0]["seeds"] == [150]
    assert res["outcomes"][1]["stats"]["TotalEHP"] == 8000.0
    assert res["outcomes"][-1]["stats"]["TotalEHP"] is None
    assert set(res["outcomes"][-1]["seeds"]) == {151, 152}


def test_search_stops_every_engine_when_one_fails(fake_engine: type[FakeEngine], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class Dying(FakeEngine):
        def stats(self, seed: int) -> dict:
            if self is not self.made[0]:
                raise RuntimeError("engine died")
            calls.append(seed)
            time.sleep(0.001)
            return {}

    monkeypatch.setattr(timeless, "Engine", Dying)
    with pytest.raises(RuntimeError, match="engine died"):
        timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    assert len(calls) < 500  # the surviving engine's share is 3,950 seeds: it must not work through them


def test_search_reuses_first_seed_stats_and_forwards_pob_dir(
    fake_engine: type[FakeEngine],
    tmp_path: Path,
) -> None:
    seen_seeds: list[int] = []

    def track(seed: int) -> float:
        seen_seeds.append(seed)
        return float(seed)

    fake_engine.value = track
    res = timeless.search(
        "code",
        "",
        9,
        "Festering Vengeance",
        workers=1,
        pob_dir=str(fake_engine.game_dir),
        runtime_dir=tmp_path,
    )
    assert res["seedCount"] == 7901
    # seeds[0] (100) was evaluated once to probe overrides and reused rather than recalculated
    assert seen_seeds.count(100) == 1
    assert len(seen_seeds) == 7901
    assert fake_engine.made[-1].pob_dir == str(fake_engine.game_dir)
    assert fake_engine.made[-1].runtime_dir == tmp_path


def test_search_fast_aborts_blocked_workers_on_error_and_sigint(
    fake_engine: type[FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aborted: list[int] = []

    class BlockingEngine(FakeEngine):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._unblock = threading.Event()
            self._first_probe_done = False

        def abort(self) -> None:
            aborted.append(id(self))
            self._unblock.set()

        def stats(self, seed: int) -> dict:
            if self is self.made[0]:
                if not self._first_probe_done:
                    self._first_probe_done = True
                    return dict.fromkeys(timeless.STAT_KEYS, 1.0)
                # Simulate an engine blocked on a 30s RPC waiting for stdout; abort() must wake it immediately.
                self._unblock.wait(timeout=30.0)
                raise RuntimeError("aborted by shutdown")
            time.sleep(0.05)
            raise RuntimeError("root cause worker failure")

    monkeypatch.setattr(timeless, "Engine", BlockingEngine)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="root cause worker failure"):
        timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"abort() should unblock blocked workers immediately, took {elapsed:.2f}s"
    assert aborted, "expected abort() to be called on active workers"

    # Also verify SIGINT fast-aborts blocked workers in milliseconds
    aborted_sig: list[int] = []

    class SigBlockingEngine(FakeEngine):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._unblock = threading.Event()
            self._first_probe_done = False

        def abort(self) -> None:
            aborted_sig.append(id(self))
            self._unblock.set()

        def stats(self, seed: int) -> dict:
            if self is self.made[0] and not self._first_probe_done:
                self._first_probe_done = True
                return dict.fromkeys(timeless.STAT_KEYS, 1.0)
            self._unblock.wait(timeout=30.0)
            raise RuntimeError("aborted by sigint")

    SigBlockingEngine.made = []
    monkeypatch.setattr(timeless, "Engine", SigBlockingEngine)
    t1 = time.monotonic()
    timer = threading.Timer(0.15, lambda: signal.raise_signal(signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    finally:
        timer.cancel()
    elapsed_sig = time.monotonic() - t1
    assert elapsed_sig < 2.0, f"SIGINT abort() should unblock immediately, took {elapsed_sig:.2f}s"
    assert aborted_sig, "expected abort() on SIGINT"


def test_search_stops_on_ctrl_c(fake_engine: type[FakeEngine]) -> None:
    calls: list[int] = []

    def slow(seed: int) -> float:
        calls.append(seed)
        time.sleep(0.001)
        return 1.0

    fake_engine.value = slow
    timer = threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT))  # off the main thread, like Ctrl+C
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    finally:
        timer.cancel()
    assert len(calls) < 4000  # of 7,901: on Windows a plain wait on the pool would have let them all finish first


def test_estimated_search_misses_nothing_and_reports_only_what_it_covers(fake_engine: type[FakeEngine]) -> None:
    seeds = range(2000, 10001)
    rng = random.Random(0)
    rows = [bytes(rng.randrange(12) for _ in seeds) for _ in range(3)]  # the table rows of notables 101-103
    folder = fake_engine.game_dir / "Data" / "TimelessJewelData"
    folder.mkdir()
    (folder / "MilitantFaith.zip").write_bytes(zlib.compress(b"".join(rows)))
    (folder / "NodeIndexMapping.lua").write_text(
        'nodeIDList["sizeNotable"] = 3\n' + "".join(f"nodeIDList[{101 + i}] = {{ index = {i}, size = 1 }}\n" for i in range(3)),
        encoding="utf-8",
    )

    def value(seed: int) -> float:
        a, b, c = (row[seed - 2000] for row in rows)
        return 1000.0 * (1 + a / 50) * (1 + b / 80) * (1 + c / 200)

    fake_engine.value = value
    res = timeless.search("code", "", 9, "Militant Faith", top=5, workers=2)
    assert res["estimate"] and res["calculated"] < res["groupCount"] // 2
    found = {seed: outcome["stats"]["CombinedDPS"] for outcome in res["outcomes"] for seed in outcome["seeds"]}
    # every seed at or above the reported level and nothing below it: rows the sample merely hit are not a ranking
    assert found == {seed: value(seed) for seed in seeds if value(seed) >= res["cutoff"]}


def test_headless_runner_lua_implements_posix_stat_mtime() -> None:
    runner_path = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua" / "headless_runner.lua"
    src = runner_path.read_text(encoding="utf-8")
    assert 'asm("stat$INODE64")' in src
    assert 'asm("__xstat")' in src
    assert "uint8_t _head[48];" in src
    assert "uint8_t _head[88];" in src
    assert "st.st_mtime_nsec" in src
    assert "sec + nsec * 1e-9" in src
    assert "return it and posix_file_mtime(it.full) or 0" in src


def test_parse_stat_guards_bool_and_blank_and_narrow_down_negative_stat_with_none_nan() -> None:
    # _parse_stat must not coerce bools or blank strings to 1.0/0.0
    for bad in (None, True, False, "", "   ", "nan", float("nan"), "not-a-number"):
        assert timeless._parse_stat(bad) is None
    assert timeless._parse_stat(-40) == -40.0
    assert timeless._parse_stat("0") == 0.0
    assert timeless._parse_stat("inf") == float("inf")
    assert timeless._parse_stat("-inf") == float("-inf")

    # Negative-stat (ChaosResist in [-60, -10]) + None / "nan" WITHOUT "inf":
    # narrow_down must fall back to exact rather than mapping None/"nan" -> 0.0 and pruning -10!
    sigs = [bytes([a, b]) for a in range(30) for b in range(30)]  # 900 sigs > 3 * 200
    truth: dict[bytes, Any] = {s: (-60.0 + (s[0] + s[1]) * 0.5) for s in sigs}
    top_sig = bytes([29, 29])
    truth[top_sig] = -10.0  # best negative ChaosResist; would be pruned if None mapped to 0.0!
    truth[bytes([0, 0])] = None
    truth[bytes([0, 1])] = "nan"

    results: dict[bytes, dict[str, Any]] = {}
    est = timeless.narrow_down(
        sigs,
        results,
        lambda todo: results.update({s: {"ChaosResist": truth[s]} for s in todo}),
        "ChaosResist",
        1,
        random.Random(0),
        lambda _m: None,
    )
    assert est is None
    assert len(results) == len(sigs)
    assert top_sig in results
    ranked = sorted(results, key=lambda s: timeless._stat_rank_key(results[s]["ChaosResist"]), reverse=True)
    assert ranked[0] == top_sig
    assert results[ranked[0]]["ChaosResist"] == -10.0

    # Also verify "nan" alone (no None, no "inf") triggers exact fallback on negative stats
    truth_nan = {s: (-50.0 + s[0] * 0.25) for s in sigs}
    truth_nan[top_sig] = -5.0
    truth_nan[bytes([1, 1])] = "nan"
    results_nan: dict[bytes, dict[str, Any]] = {}
    est_nan = timeless.narrow_down(
        sigs,
        results_nan,
        lambda todo: results_nan.update({s: {"ChaosResist": truth_nan[s]} for s in todo}),
        "ChaosResist",
        1,
        random.Random(1),
        lambda _m: None,
    )
    assert est_nan is None
    assert len(results_nan) == len(sigs)
    assert top_sig in results_nan


def test_extract_allocated_notable_names_includes_active_tree_spec_sockets() -> None:
    xml_with_tree_sockets = (
        "<PathOfBuilding>\n"
        '  <Tree activeSpec="2">\n'
        '    <Spec nodes="101,48768">\n'
        "      <Sockets>\n"
        '        <Socket nodeId="48768" itemId="4"/>\n'
        "      </Sockets>\n"
        "    </Spec>\n"
        '    <Spec nodes="101,41263,26196">\n'
        "      <Sockets>\n"
        '        <Socket nodeId="41263" itemId="3"/>\n'
        '        <Socket nodeId="26196" itemId="0"/>\n'
        "      </Sockets>\n"
        "    </Spec>\n"
        "  </Tree>\n"
        '  <Items activeItemSet="1">\n'
        '    <Item id="1">Rarity: RARE\nAmulet\n{crafted}Allocates Whispers of Doom\n</Item>\n'
        '    <Item id="3">Rarity: UNIQUE\nTree Jewel\nAllocates Golem&apos;s Blood\n</Item>\n'
        '    <Item id="4">Rarity: UNIQUE\nInactive Spec Jewel\nAllocates Beef\n</Item>\n'
        '    <Item id="5">Rarity: UNIQUE\nUnequipped Jewel\nAllocates Heart of the Warrior\n</Item>\n'
        '    <ItemSet id="1">\n'
        '      <Slot name="Amulet" itemId="1"/>\n'
        '      <SocketIdURL name="Jewel 41263" nodeId="41263" itemPbURL=""/>\n'
        "    </ItemSet>\n"
        "  </Items>\n"
        "</PathOfBuilding>\n"
    )
    assert timeless._extract_allocated_notable_names(xml_with_tree_sockets) == {
        "Whispers of Doom",
        "Golem's Blood",
    }

    # Tree-only sockets (activeSpec="1" with no <Slot> elements in <Items>)
    xml_tree_only = (
        "<PathOfBuilding>\n"
        '  <Tree activeSpec="1">\n'
        "    <Spec>\n"
        "      <Sockets>\n"
        '        <Socket nodeId="41263" itemId="3"/>\n'
        "      </Sockets>\n"
        "    </Spec>\n"
        "  </Tree>\n"
        "  <Items>\n"
        '    <Item id="3">Rarity: UNIQUE\nTree Jewel\nAllocates Golem&apos;s Blood\n</Item>\n'
        '    <Item id="9">Rarity: UNIQUE\nStashed Jewel\nAllocates Beef\n</Item>\n'
        "  </Items>\n"
        "</PathOfBuilding>\n"
    )
    assert timeless._extract_allocated_notable_names(xml_tree_only) == {"Golem's Blood"}


def test_cli_timeless_pct_negative_baseline() -> None:
    from pob_cli import cli_timeless

    assert cli_timeless._pct(-10, -40) == "+75.0%"
    assert cli_timeless._pct(-60, -40) == "-50.0%"
    assert cli_timeless._pct(10, -40) == "+125.0%"
    assert cli_timeless._pct(150, 100) == "+50.0%"
    assert cli_timeless._pct(10, 0) == "-"
    assert cli_timeless._pct(True, -40) == "-"
    assert cli_timeless._pct(-10, False) == "-"
    assert cli_timeless._pct("inf", -40) == "-"
    assert cli_timeless._pct(float("nan"), -40) == "-"


def test_corrupt_table_and_cli_timeless_exit_5_without_traceback(
    fake_engine: type[FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from pob_cli import cli_timeless
    from pob_cli.cli import main

    folder = fake_engine.game_dir / "Data" / "TimelessJewelData"
    folder.mkdir(exist_ok=True)
    (folder / "MilitantFaith.zip").write_bytes(b"not-valid-zlib-bytes")
    (folder / "NodeIndexMapping.lua").write_text("-- missing sizeNotable\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Failed to decompress"):
        timeless.load_table(fake_engine.game_dir, "MilitantFaith")
    with pytest.raises(RuntimeError, match=r"Malformed NodeIndexMapping\.lua"):
        timeless.load_node_index(fake_engine.game_dir)

    # Restore valid NodeIndexMapping.lua so timeless.search reaches load_table on the corrupt .zip
    (folder / "NodeIndexMapping.lua").write_text(
        'nodeIDList["sizeNotable"] = 3\n'
        + "".join(f"nodeIDList[{101 + i}] = {{ index = {i}, size = 1 }}\n" for i in range(3)),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli_timeless,
        "_daemon_call",
        lambda _action, _params=None: {"code": "FAKE_CODE", "xml": "", "pobDir": str(fake_engine.game_dir)},
    )
    runner = CliRunner()

    # End-to-end through cli_timeless -> real timeless.search -> corrupt .zip -> clean exit 5, no Traceback
    res_corrupt = runner.invoke(
        main,
        ["timeless", "search", "--jewel", "Militant Faith", "--socket", "9", "--workers", "1"],
    )
    assert res_corrupt.exit_code == 5, res_corrupt.output
    assert "Traceback" not in res_corrupt.output
    assert "Error:" in res_corrupt.output
    assert "Failed to decompress" in res_corrupt.output

    # Direct OSError, raw zlib.error, and empty RuntimeError on search -> exit 5, non-empty msg, no Traceback
    for exc_obj, expected_substr in (
        (FileNotFoundError("jewel.lua missing"), "jewel.lua missing"),
        (zlib.error("corrupt stream"), "corrupt stream"),
        (RuntimeError(""), "RuntimeError"),
    ):
        monkeypatch.setattr(
            cli_timeless,
            "search",
            lambda *_a, _e=exc_obj, **_kw: (_ for _ in ()).throw(_e),
        )
        res = runner.invoke(main, ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "9"])
        assert res.exit_code == 5, res.output
        assert "Traceback" not in res.output
        assert "Error:" in res.output
        assert expected_substr in res.output

    # timeless list: OSError -> exit 5, empty ValueError -> exit 1 with fallback type name, no Traceback
    monkeypatch.setattr(
        cli_timeless,
        "_daemon_call",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("pipe broken")),
    )
    res_list_os = runner.invoke(main, ["timeless", "list"])
    assert res_list_os.exit_code == 5
    assert "Traceback" not in res_list_os.output
    assert "pipe broken" in res_list_os.output

    monkeypatch.setattr(
        cli_timeless,
        "_daemon_call",
        lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("")),
    )
    res_list_val = runner.invoke(main, ["timeless", "list"])
    assert res_list_val.exit_code == 1
    assert "Traceback" not in res_list_val.output
    assert "ValueError" in res_list_val.output


def test_search_pre_validates_before_spawning_engine_and_aborts_once(
    fake_engine: type[FakeEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invalid ASCII jewel, invalid conqueror (English or Chinese jewel), non-positive socket,
    # and unallocated socket in <Spec nodes="..."> must fail BEFORE spawning any Engine.
    with pytest.raises(ValueError, match="Unknown timeless jewel"):
        timeless.search("code", "", 9, "NoSuchJewel")
    assert fake_engine.made == []

    with pytest.raises(ValueError, match="Lethal Pride conquerors"):
        timeless.search("code", "", 9, "致命的骄傲", conqueror="NoSuchConqueror")
    assert fake_engine.made == []

    with pytest.raises(ValueError, match="not an allocated jewel socket"):
        timeless.search("code", "", 0, "Lethal Pride")
    assert fake_engine.made == []

    xml_tree = '<PathOfBuilding><Tree activeSpec="1"><Spec nodes="7,9,101"/></Tree></PathOfBuilding>'
    with pytest.raises(ValueError, match="not an allocated jewel socket"):
        timeless.search("code", xml_tree, 999999, "Lethal Pride")
    assert fake_engine.made == []

    # Verify calculate() poll loop does not spam _abort_all() every 50ms while a worker finishes exiting
    abort_counts: dict[int, int] = {}

    class SlowExitEngine(FakeEngine):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._first_done = False

        def abort(self) -> None:
            abort_counts[id(self)] = abort_counts.get(id(self), 0) + 1

        def stats(self, seed: int) -> dict:
            if self is self.made[0]:
                if not self._first_done:
                    self._first_done = True
                    return dict.fromkeys(timeless.STAT_KEYS, 1.0)
                time.sleep(0.25)  # spans 5 x 50ms poll ticks after worker 1 fails
                return dict.fromkeys(timeless.STAT_KEYS, 1.0)
            raise RuntimeError("boom")

    monkeypatch.setattr(timeless, "Engine", SlowExitEngine)
    with pytest.raises(RuntimeError, match="boom"):
        timeless.search("code", "", 9, "Festering Vengeance", workers=2)
    # At most 2 calls per engine (1 from failing worker thread + 1 from main poll loop), never 5+ from 50ms spam
    assert abort_counts and max(abort_counts.values()) <= 2


def test_bridge_worker_start_wraps_popen_oserror_and_kills_on_boot_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import queue as _queue

    from pob_cli import bridge_client as bc

    fake_root = tmp_path / "eng"
    (fake_root / "PoE1").mkdir(parents=True)
    monkeypatch.setattr(bc, "build_engine_command", lambda _root: (["missing-pob-bin"], {}))

    # 1. subprocess.Popen OSError is wrapped as BridgeError
    monkeypatch.setattr(
        bc.subprocess,
        "Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("missing-pob-bin")),
    )
    w = bc.BridgeWorker(slot_name="test", pob_dir=fake_root)
    with pytest.raises(bc.BridgeError, match="Failed to launch PoB engine"):
        w.start()
    assert w._proc is None

    # 2. KeyboardInterrupt during handshake kills the spawned subprocess instead of leaking it
    killed: list[bool] = []
    real_queue_cls = _queue.Queue

    class _InterruptProc:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.stderr = io.StringIO("")
            self.stdin = io.StringIO()
            self._q: _queue.Queue[str | None] = real_queue_cls()
            self.stdout = self._iter_stdout()

        def _iter_stdout(self):
            while True:
                item = self._q.get()
                if item is None:
                    break
                yield item

        def poll(self) -> int | None:
            return None if not killed else 0

        def kill(self) -> None:
            killed.append(True)
            self._q.put(None)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(bc.subprocess, "Popen", _InterruptProc)
    w2 = bc.BridgeWorker(slot_name="test", pob_dir=fake_root)

    class _InterruptQueue(real_queue_cls):
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt("ctrl-c during boot")

    monkeypatch.setattr(bc.queue, "Queue", _InterruptQueue)
    with pytest.raises(KeyboardInterrupt):
        w2.start()
    assert killed == [True]
    assert w2._proc is None

    # 3. Boot RPC BridgeError (e.g. version fails after handshake) also kills the spawned subprocess
    monkeypatch.setattr(bc.queue, "Queue", real_queue_cls)
    killed_rpc: list[bool] = []

    class _BootRpcErrorProc:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            import json

            self.stderr = io.StringIO("")
            self.stdin = self
            self._q: _queue.Queue[str | None] = real_queue_cls()
            self._q.put(json.dumps({"event": "gate_result", "data": {"ok": True}}) + "\n")
            self._q.put(json.dumps({"event": "hello", "data": {"pobVersion": "2.68.1", "bridge": "0.2.0"}}) + "\n")
            self.stdout = self._iter_stdout()

        def _iter_stdout(self):
            while True:
                item = self._q.get()
                if item is None:
                    break
                yield item

        def write(self, data: str) -> int:
            import json

            for line in data.splitlines():
                if not line.strip():
                    continue
                req = json.loads(line)
                self._q.put(json.dumps({"id": req["id"], "ok": False, "error": {"message": "version boom"}}) + "\n")
            return len(data)

        def flush(self) -> None:
            pass

        def poll(self) -> int | None:
            return None if not killed_rpc else 0

        def kill(self) -> None:
            killed_rpc.append(True)
            self._q.put(None)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(bc.subprocess, "Popen", _BootRpcErrorProc)
    w3 = bc.BridgeWorker(slot_name="test", pob_dir=fake_root)
    with pytest.raises(bc.BridgeError, match="version boom"):
        w3.start()
    assert killed_rpc == [True]
    assert w3._proc is None


def test_engine_start_worker_stops_on_failure_and_keeps_typeerror_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[str] = []

    class FailingStartWorker:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def start(self, *, restore_code: str | None = None) -> None:
            raise KeyboardInterrupt("interrupted during Engine.__init__")

        def stop(self) -> None:
            stopped.append("failing")

    monkeypatch.setattr(timeless, "BridgeWorker", FailingStartWorker)
    with pytest.raises(KeyboardInterrupt):
        timeless.Engine("CODE", "tpl", "Jewel 1")
    assert stopped == ["failing"]

    legacy_calls: list[str] = []

    class LegacyStartWorker:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def start(self) -> None:
            legacy_calls.append("legacy_start")

        def stop(self) -> None:
            stopped.append("legacy")

    monkeypatch.setattr(timeless, "BridgeWorker", LegacyStartWorker)
    eng = timeless.Engine("CODE", "tpl", "Jewel 1")
    assert legacy_calls == ["legacy_start"]
    assert eng._loaded is False
    assert stopped == ["failing"]


def test_real_engine_tables_match_their_seed_ranges() -> None:
    """With a local engine: every flat table must be exactly notables x seeds bytes, or grouping would misread it."""
    from pob_cli.bridge_client import BridgeWorker

    game_dir = BridgeWorker("test").game_dir
    if not (game_dir / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua").is_file():
        pytest.skip("no local PoB engine data")
    jewels = timeless.load_jewels(game_dir)
    index = timeless.load_node_index(game_dir)
    assert set(timeless.FLAT_TABLES) <= set(jewels)
    for name, stem in timeless.FLAT_TABLES.items():
        assert len(timeless.load_table(game_dir, stem)) == len(index) * len(jewels[name]["seeds"]), name
