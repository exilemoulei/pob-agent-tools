"""Unit tests for hybrid Timeless PRNG + mmap engine, metadata generator, and slim-install fallback."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import random
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pob_cli import timeless
from pob_cli.timeless import lut as timeless_lut
from pob_cli.timeless import worker as timeless_worker
from pob_cli.updater.constants import ManifestFileEntry, PobManifest

PKG_ROOT = Path(__file__).resolve().parents[1] / "src" / "pob_cli"
SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"

SLIM_JEWEL_LUA = """return {
[[
Emperor's Wit
Cobalt Jewel
(4-6)% increased Intelligence
]],[[
Glorious Vanity
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
{variant:1}Bathed in the blood of (100-119) sacrificed in the name of Doryani
Passives in radius are Conquered by the Vaal
Historic
]],[[
Lethal Pride
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
{variant:1}Commanded leadership over (10000-10999) warriors under Kaom
Passives in radius are Conquered by the Karui
Historic
]],[[
Festering Vengeance
Murderous Eye Jewel
Limited to: 1 Historic
Implicits: 0
Subjugating (100-119) souls in the thrall of Tecrod
Passives affected are Conquered by the Abyssal
Historic
]],[[
Reclaimed Malevolence
Assembled Eye Jewel
Limited to: 1 Historic
Implicits: 0
Binding (100-119) souls to phylacteries to sustain Zorath
Passives affected are Conquered by the Abyssal
Historic
]],[[
Future Relic
Timeless Jewel
Limited to: 1 Historic
Radius: Large
Implicits: 0
Whispering (100-104) echoes of FutureBoss
Passives in radius are Conquered by the Future
Historic
]],
}
"""

EXPECTED_SEARCH_KEYS = {
    "cutoff",
    "jewel",
    "conqueror",
    "socket",
    "mods",
    "removed",
    "stat",
    "seedCount",
    "groupCount",
    "calculated",
    "estimate",
    "seedNodes",
    "workers",
    "baseline",
    "outcomes",
    "selfCheck",
    "seconds",
}


@pytest.fixture
def slim_game_dir(tmp_path: Path) -> Path:
    """Slim PoE1 data directory with ZERO .zip* LUT files on disk."""
    (tmp_path / "Data" / "Uniques").mkdir(parents=True)
    (tmp_path / "Data" / "Uniques" / "jewel.lua").write_text(SLIM_JEWEL_LUA, encoding="utf-8")
    return tmp_path


def test_bundled_timeless_meta_gz_integrity_and_size() -> None:
    meta_gz = PKG_ROOT / "data" / "timeless_meta_3_30.lua.gz"
    assert meta_gz.is_file(), "Bundled timeless_meta_3_30.lua.gz must exist"
    raw_gz = meta_gz.read_bytes()
    assert 2048 <= len(raw_gz) <= 5120, f"Unexpected compressed size: {len(raw_gz)} bytes"
    src = gzip.decompress(raw_gz).decode("utf-8")
    assert 'M.tree_version = "3_30"' in src
    assert "M.additions_count = 337" in src
    assert "M.root_out = {" in src
    for jt in range(1, 12):
        assert f"[{jt}]={{" in src, f"Missing version entry [{jt}]"
    assert "return M" in src


def test_lua_timeless_modules_enforce_contracts_and_3_30_pitfalls() -> None:
    prng_src = (PKG_ROOT / "lua" / "timeless_prng.lua").read_text(encoding="utf-8")
    mmap_src = (PKG_ROOT / "lua" / "timeless_mmap.lua").read_text(encoding="utf-8")

    # Module signatures & RPC handlers
    assert "function M.install_post_launch(" in prng_src
    assert "M.rpc_handlers = {" in prng_src
    assert "timeless_prng_group_hashes" in prng_src
    assert "timeless_prng_search" in prng_src
    assert "data.readLUT =" in prng_src
    assert "data.readAbyssJewelLUT =" in prng_src

    # Mandatory 3.30 Pitfalls
    assert "n.proxyKey == nil" in prng_src, "Pitfall #1: Abyss walkable() must exclude proxyKey nodes"
    assert "if pt == 4 then" in prng_src and "jtype >= 7" in prng_src, (
        "Pitfall #2: Abyss 7..10 must apply fixed PassiveType==4 keystone replacements"
    )
    assert "if ascName == nil then" in prng_src and "ASC_G.all_asc" in prng_src, (
        "Pitfall #3: readAbyssJewelLUT with nil ascendancyName must iterate all ascendancies"
    )
    assert "data.timelessJewelAdditions ~= META.additions_count then return false" in prng_src, (
        "PRNG ids are offset by PoB's addition count: a different count must fall back to the LUT"
    )
    assert "if jt <= 6 then return true end" in prng_src and "PTYPE_CACHE[tv]" in prng_src, (
        "Legion/Heroic (1..6) must work on older tree versions (e.g. 3_29) with per-tree NODE_PTYPE cache"
    )

    # Cross-platform mmap bindings (Win32 MapViewOfFile + POSIX mmap)
    assert "MapViewOfFile" in mmap_src and "CreateFileMappingW" in mmap_src
    assert 'asm("mmap")' in mmap_src and 'asm("munmap")' in mmap_src


def test_update_timeless_meta_script_renders_deterministic_gz(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("update_timeless_meta", SCRIPTS_ROOT / "update_timeless_meta.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    raw_dir = tmp_path / "raw_meta"
    raw_dir.mkdir()
    versions = [
        {"_key": 2, "Var1": 0, "Var2": 0, "Var3": 1, "Var4": 1, "Var5": 1, "Var6": 1, "Var7": 1, "Var8": 1, "Var9": 0},
        {"_key": 7, "Var1": 1, "Var2": 1, "Var3": 0, "Var4": 0, "Var5": 0, "Var6": 0, "Var7": 1, "Var8": 1, "Var9": 0},
    ]
    skills = [
        {
            "_key": 182, "AlternateTreeVersionsKey": 7, "PassiveType": [4], "SpawnWeight": 100,
            "StatsKeys": [1], "Stat1Min": 1, "Stat1Max": 1, "Stat2Min": 0, "Stat2Max": 0,
            "Var9": 0, "Var10": 0, "Var11": 0, "Var12": 0, "RandomMin": 0, "RandomMax": 0,
        }
    ]
    adds = [
        {
            "_key": 40, "AlternateTreeVersionsKey": 2, "PassiveType": [3], "SpawnWeight": 100,
            "StatsKeys": [1], "Stat1Min": 20, "Stat1Max": 20, "Var6": 0, "Var7": 0,
        }
    ]
    import json

    for stem, rows in (
        ("alternate_tree_versions", versions),
        ("alternate_passive_skills", skills),
        ("alternate_passive_additions", adds),
    ):
        (raw_dir / f"{stem}.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode("utf-8")))

    pob_dir = tmp_path / "PoB"
    (pob_dir / "TreeData" / "3_30").mkdir(parents=True)
    (pob_dir / "TreeData" / "3_30" / "tree.lua").write_text(
        'return { ["nodes"] = { ["root"] = { ["out"] = { "50459", "47175" } } } }\n',
        encoding="utf-8",
    )

    out1 = tmp_path / "a.lua.gz"
    out2 = tmp_path / "b.lua.gz"
    info1 = mod.generate_timeless_meta(raw_dir, pob_dir=pob_dir, output_path=out1)
    info2 = mod.generate_timeless_meta(raw_dir, pob_dir=pob_dir, output_path=out2)
    assert out1.read_bytes() == out2.read_bytes()  # deterministic mtime=0 gzip
    assert info1["versions"] == 2 and info2["root_out_len"] == 2
    decoded = gzip.decompress(out1.read_bytes()).decode("utf-8")
    assert "M.root_out = {50459,47175}" in decoded
    assert "[7]={sa_rep=true,sn_rep=true," in decoded and "msvc=true" in decoded


class _PrngFakeEngine:
    """FakeEngine advertising `caps = {'timelessPrng': True}` with configurable group_hashes/search RPCs."""

    game_dir: Path
    made: list[_PrngFakeEngine]
    value: Callable[[int], float]
    group_handler: Callable[[dict[str, Any]], dict[str, Any]]
    search_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    rpc_log: list[tuple[str, dict[str, Any]]]

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
        self.caps = {"timelessPrng": True}
        self.worker = self
        self.made.append(self)

    def call(self, method: str, params: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
        self.rpc_log.append((method, params))
        if method == "get_tree_state":
            return {
                "allocatedNodes": [7, 9, 101, 102, 103],
                "overrides": {str(n): {"why": "conquered"} for n in (101, 102, 103)},
                "sockets": [{"nodeId": 7}, {"nodeId": 9}],
            }
        if method == "tj_open":
            return {"jewel": {"options": []}, "socketIds": [-1, 7, 9], "socket": {"options": []}}
        if method == "tj_close":
            return {"ok": True}
        if method == "timeless_prng_group_hashes":
            return type(self).group_handler(params)
        if method == "timeless_prng_search":
            handler = type(self).search_handler
            if handler is not None:
                return handler(params)
            return {"topSeeds": []}
        raise AssertionError(f"Unexpected RPC {method}")

    def reset(self) -> None:
        pass

    stop = reset

    def read(self) -> dict[str, float]:
        return dict.fromkeys(timeless.STAT_KEYS, 1000.0)

    def stats(self, seed: int) -> dict[str, float]:
        return dict.fromkeys(timeless.STAT_KEYS, type(self).value(seed))


def test_prng_hybrid_search_legion_and_abyss_without_zip_files(
    slim_game_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _PrngFakeEngine.game_dir = slim_game_dir
    _PrngFakeEngine.made = []
    _PrngFakeEngine.rpc_log = []

    # 1. Legion flat jewel (Lethal Pride) on slim dir (ZERO .zip files): additive PRNG grouping + 1 worker
    rng = random.Random(42)
    seeds_lp = list(range(10000, 11000))
    sig_by_seed = {s: bytes([rng.randrange(10), rng.randrange(10), rng.randrange(10)]) for s in seeds_lp}

    def lp_group_handler(params: dict[str, Any]) -> dict[str, Any]:
        assert params["jewelType"] == 2
        by_sig: dict[str, list[int]] = {}
        for s in seeds_lp:
            by_sig.setdefault(sig_by_seed[s].hex(), []).append(s)
        return {
            "supported": True,
            "jewelType": 2,
            "additive": True,
            "nodes": [101, 102, 103],
            "seedCount": len(seeds_lp),
            "groupCount": len(by_sig),
            "groups": [{"sig": k, "seeds": v} for k, v in by_sig.items()],
        }

    def lp_val(seed: int) -> float:
        a, b, c = sig_by_seed[seed]
        return 1000.0 * (1 + a / 40) * (1 + b / 60) * (1 + c / 80)

    _PrngFakeEngine.group_handler = lp_group_handler
    _PrngFakeEngine.value = lp_val
    monkeypatch.setattr(timeless, "Engine", _PrngFakeEngine)

    res_lp = timeless.search("CODE", "", 9, "Lethal Pride", top=5)
    assert set(res_lp.keys()) == EXPECTED_SEARCH_KEYS
    assert res_lp["workers"] == 1  # PRNG active defaults to 1 worker (~0 MiB RSS delta)
    assert res_lp["estimate"] is not None and res_lp["calculated"] < res_lp["groupCount"]
    assert res_lp["selfCheck"]["mismatches"] == []
    best_seed = max(seeds_lp, key=lp_val)
    assert best_seed in res_lp["outcomes"][0]["seeds"]

    # 2. Abyss socket-keyed jewel (Festering Vengeance, type 7): non-additive PRNG grouping collapses 20 seeds -> 4 groups
    def abyss_group_handler(params: dict[str, Any]) -> dict[str, Any]:
        assert params["jewelType"] == 7
        assert params["socketId"] == 9
        assert set(params["usedNodes"]) == {7, 9, 101, 102, 103}
        buckets: dict[str, list[int]] = {}
        for s in range(100, 120):
            buckets.setdefault(f"101:r{400 + (s % 4)}", []).append(s)
        return {
            "supported": True,
            "jewelType": 7,
            "additive": False,
            "nodes": [101],
            "seedCount": 20,
            "groupCount": len(buckets),
            "groups": [{"sig": k, "seeds": v} for k, v in buckets.items()],
        }

    _PrngFakeEngine.group_handler = abyss_group_handler
    _PrngFakeEngine.value = lambda s: 2000.0 + (s % 4) * 100.0
    res_abyss = timeless.search("CODE", "", 9, "Festering Vengeance", top=3)
    assert set(res_abyss.keys()) == EXPECTED_SEARCH_KEYS
    assert res_abyss["seedCount"] == 20
    assert res_abyss["groupCount"] == 4
    assert res_abyss["calculated"] == 4
    assert res_abyss["workers"] == 1
    assert res_abyss["estimate"] is None
    assert res_abyss["outcomes"][0]["stats"]["CombinedDPS"] == 2300.0
    assert len(res_abyss["outcomes"][0]["seeds"]) == 5

    # 3. Abyss Zorath (Reclaimed Malevolence, type 11): also grouped via PRNG without .zip*
    def zorath_group_handler(params: dict[str, Any]) -> dict[str, Any]:
        assert params["jewelType"] == 11
        return {
            "supported": True,
            "jewelType": 11,
            "additive": False,
            "nodes": [101, 102],
            "seedCount": 20,
            "groupCount": 2,
            "groups": [
                {"sig": "101:r500", "seeds": list(range(100, 110))},
                {"sig": "101:r501;asc:102:r520", "seeds": list(range(110, 120))},
            ],
        }

    _PrngFakeEngine.group_handler = zorath_group_handler
    _PrngFakeEngine.value = lambda s: 3000.0 if s >= 110 else 2500.0
    res_z = timeless.search("CODE", "", 9, "Reclaimed Malevolence", top=2)
    assert res_z["groupCount"] == 2 and res_z["calculated"] == 2 and res_z["workers"] == 1
    assert res_z["outcomes"][0]["stats"]["CombinedDPS"] == 3000.0


def test_prng_describe_worker_marks_all_11_jewels_grouped(slim_game_dir: Path) -> None:
    class _PrngDescWorker:
        def __init__(self, gd: Path, prng: bool) -> None:
            self.game_dir = gd
            self.caps = {"timelessPrng": prng}
            self.types = list(range(1, 12))

        def call_batch(self, calls: list[tuple[str, Any]], timeout: float = 60.0) -> list[dict[str, Any]]:
            return [
                {"jewel": {"options": []}, "socketIds": [-1, 9], "socket": {"options": [{}, {"label": "S9"}]}},
                {"ok": True},
                {"allocatedNodes": [9], "sockets": [{"nodeId": 9}]},
            ]

        def call(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
            assert method == "timeless_prng_types"
            return {"types": list(self.types)}

    info_prng = timeless.describe_worker(_PrngDescWorker(slim_game_dir, True))
    by_name = {j["name"]: j for j in info_prng["jewels"]}
    assert by_name["Lethal Pride"]["grouped"] is True
    assert by_name["Glorious Vanity"]["grouped"] is True
    assert by_name["Festering Vengeance"]["grouped"] is True
    assert by_name["Reclaimed Malevolence"]["grouped"] is True
    assert by_name["Future Relic"]["grouped"] is False  # jewelType > 11

    # The engine's gate, not caps alone: on another tree the Abyss types 7..11 are not generated, so not grouped
    other_tree = _PrngDescWorker(slim_game_dir, True)
    other_tree.types = [1, 2, 3, 4, 5, 6]
    by_name_tree = {j["name"]: j for j in timeless.describe_worker(other_tree)["jewels"]}
    assert by_name_tree["Glorious Vanity"]["grouped"] is True
    assert by_name_tree["Festering Vengeance"]["grouped"] is False
    assert by_name_tree["Reclaimed Malevolence"]["grouped"] is False

    info_legacy = timeless.describe_worker(_PrngDescWorker(slim_game_dir, False))
    by_name_leg = {j["name"]: j for j in info_legacy["jewels"]}
    assert by_name_leg["Lethal Pride"]["grouped"] is True
    assert by_name_leg["Festering Vengeance"]["grouped"] is False


def test_tree_mismatch_and_jeweltype_gt11_fallback_fetches_lut_into_cache(
    slim_game_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prepare NodeIndexMapping.lua (metadata present, .zip missing in slim game_dir)
    lut_dir = slim_game_dir / "Data" / "TimelessJewelData"
    lut_dir.mkdir(parents=True, exist_ok=True)
    mapping_text = 'nodeIDList["sizeNotable"] = 3\n' + "".join(
        f"nodeIDList[{101 + i}] = {{ index = {i}, size = 1 }}\n" for i in range(3)
    )
    (lut_dir / "NodeIndexMapping.lua").write_text(mapping_text, encoding="utf-8")
    remote_mapping_sha1 = hashlib.sha1((lut_dir / "NodeIndexMapping.lua").read_bytes()).hexdigest()

    rt_dir = tmp_path / "rt"
    fake_table = zlib.compress(bytes([i % 3 for i in range(3 * 1000)]))
    fetched_stems: list[str] = []

    def fake_manifest(**_kw: Any) -> tuple[str, PobManifest, str]:
        e_lp = ManifestFileEntry(
            raw_name="Data/TimelessJewelData/LethalPride.zip",
            local_rel="Data/TimelessJewelData/LethalPride.zip",
            part="runtime",
            sha1="0" * 40,
            runtime="win32",
            platform=None,
        )
        e_fut = ManifestFileEntry(
            raw_name="Data/TimelessJewelData/FutureRelic.zip.part0",
            local_rel="Data/TimelessJewelData/FutureRelic.zip.part0",
            part="runtime",
            sha1="0" * 40,
            runtime="win32",
            platform=None,
        )
        e_map = ManifestFileEntry(
            raw_name="Data/TimelessJewelData/NodeIndexMapping.lua",
            local_rel="Data/TimelessJewelData/NodeIndexMapping.lua",
            part="program",
            sha1=remote_mapping_sha1,
            runtime=None,
            platform=None,
        )
        return (
            "ok",
            PobManifest(
                version="2.70.0",
                branch="master",
                platform="win32",
                sources={},
                files={e_lp.local_rel: e_lp, e_fut.local_rel: e_fut, e_map.local_rel: e_map},
                raw_xml="<PoBVersion/>",
            ),
            '"etag-1"',
        )

    def fake_stage(
        manifest: PobManifest, entries: list[ManifestFileEntry], staging_dir: Path, **_kw: Any
    ) -> dict[Path, Path]:
        out: dict[Path, Path] = {}
        for entry in entries:
            fetched_stems.append(Path(entry.local_rel).name)
            p = staging_dir / "PoE1" / entry.local_rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(fake_table)
            out[Path("PoE1") / entry.local_rel] = p
        return out

    for var in ("POB_OFFLINE", "POB_CACHE_DIR", "POB_ROOT", "POB_RUNTIME_DIR", "POB_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(timeless_worker, "fetch_pob_manifest_remote", fake_manifest)
    monkeypatch.setattr(timeless_worker, "_download_pob_files_to_staging", fake_stage)

    _PrngFakeEngine.game_dir = slim_game_dir
    _PrngFakeEngine.made = []
    _PrngFakeEngine.rpc_log = []
    # Simulate non-3_30 tree mismatch: PRNG returns supported=False -> triggers on-demand cache fetch + flat LUT fallback!
    _PrngFakeEngine.group_handler = lambda params: {"supported": False, "activeTreeVersion": "3_31"}
    _PrngFakeEngine.value = lambda s: 1000.0 + (s % 3)
    monkeypatch.setattr(timeless, "Engine", _PrngFakeEngine)

    # Simulate CI real-engine env where POB_ROOT points to a bootstrapped engine with an active sibling cache/
    ext_rt = tmp_path / "ci_gate_rt"
    (ext_rt / "engine" / "PoE1").mkdir(parents=True)
    (ext_rt / "engine" / "PoE1" / "lua51.dll").write_bytes(b"dll")
    (ext_rt / "engine" / "PoE1" / "zlib1.dll").write_bytes(b"zlib")
    (ext_rt / "engine" / "Data" / "bridge").mkdir(parents=True)
    (ext_rt / "engine" / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")
    (ext_rt / "cache").mkdir(parents=True)
    (ext_rt / "cache" / "gamedata_3_30.sqlite").write_bytes(b"sqlite")
    monkeypatch.setenv("POB_ROOT", str(ext_rt / "engine"))

    # Keyed by this PoB's NodeIndexMapping.lua, as lua/timeless_mmap.lua reads it
    cache_lut = timeless_worker.lut_cache_dir(slim_game_dir, rt_dir)
    assert cache_lut.parent.name == "timeless_lut" and cache_lut.name == remote_mapping_sha1[:16]
    res = timeless.search("CODE", "", 9, "Lethal Pride", workers=1, runtime_dir=rt_dir)
    assert fetched_stems == ["LethalPride.zip"]
    assert not (lut_dir / "LethalPride.zip").exists(), "Must NEVER write .zip into slim game_dir"
    assert (cache_lut / "LethalPride.zip").is_file()
    assert res["seedCount"] == 1000 and res["groupCount"] == 3

    # Verify jewelType > 11 ("Future Relic") also triggers on-demand single-jewel fetch into shared cache
    res_future = timeless.search("CODE", "", 9, "Future Relic", workers=1, runtime_dir=rt_dir)
    assert "FutureRelic.zip.part0" in fetched_stems
    assert (cache_lut / "FutureRelic.zip.part0").is_file()
    assert (cache_lut / "FutureRelic.zip.complete").is_file()
    assert res_future["seedCount"] == 5 and res_future["groupCount"] == 5

    # A newer PoB whose node index differs: its table would be misread, so nothing is fetched
    fetched_stems.clear()
    remote_mapping_sha1 = "f" * 40
    with pytest.raises(RuntimeError, match="indexes its BrutalRestraint table differently"):
        timeless_worker.ensure_lut_files(slim_game_dir, "BrutalRestraint", runtime_dir=rt_dir)
    assert fetched_stems == []

    # Offline mode must not attempt network fetch, and the search says why the table is missing
    monkeypatch.setenv("POB_OFFLINE", "1")
    with pytest.raises(RuntimeError, match="POB_OFFLINE=1"):
        timeless_worker.ensure_lut_files(slim_game_dir, "MissingJewel", runtime_dir=rt_dir)
    (cache_lut / "LethalPride.zip").unlink()
    with pytest.raises(RuntimeError, match=r"LethalPride lookup table is neither installed .*POB_OFFLINE=1"):
        timeless.search("CODE", "", 9, "Lethal Pride", workers=1, runtime_dir=rt_dir)
    (cache_lut / "FutureRelic.zip.complete").unlink()  # parts without the marker may be half written
    with pytest.raises(RuntimeError, match=r"Future Relic needs PoB's FutureRelic lookup table"):
        timeless.search("CODE", "", 9, "Future Relic", workers=1, runtime_dir=rt_dir)
    assert fetched_stems == []
    assert timeless_lut.JEWEL_TYPE_IDS["Reclaimed Malevolence"] == 11


def test_prng_abyss_large_groups_shortlist_and_exact_parallel_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 500 distinct Abyss groups: non-exact calculates a shortlist of K = max(top*4, 200) groups on parallel
    # engines and claims no error bound for the rest; --exact calculates all of them
    gd = tmp_path / "slim_abyss"
    (gd / "Data" / "Uniques").mkdir(parents=True)
    (gd / "Data" / "Uniques" / "jewel.lua").write_text(
        """return {
[[
Emperor's Wit
Cobalt Jewel
(4-6)% increased Intelligence
]],[[
Festering Vengeance
Murderous Eye Jewel
Limited to: 1 Historic
Implicits: 0
Subjugating (100-599) souls in the thrall of Tecrod
Passives affected are Conquered by the Abyssal
Historic
]],
}
""",
        encoding="utf-8",
    )
    seeds = list(range(100, 600))
    _PrngFakeEngine.game_dir = gd
    _PrngFakeEngine.made = []
    _PrngFakeEngine.rpc_log = []
    _PrngFakeEngine.group_handler = lambda params: {
        "supported": True,
        "jewelType": 7,
        "additive": False,
        "nodes": [101],
        "seedCount": len(seeds),
        "groupCount": len(seeds),
        "groups": [{"sig": f"101:r{s}", "seeds": [s]} for s in seeds],
    }
    _PrngFakeEngine.search_handler = lambda params: {
        "topSeeds": [{"seed": s, "total": float(s)} for s in sorted(seeds, reverse=True)[: int(params["top"])]]
    }
    # The finder's weights put seed 599 first, but the build's real best seed (100) is outside its shortlist
    _PrngFakeEngine.value = lambda s: 99999.0 if s == 100 else 1000.0 + float(s)
    monkeypatch.setattr(timeless, "Engine", _PrngFakeEngine)
    monkeypatch.setattr(timeless.engine, "default_workers", lambda: 2)

    res_fast = timeless.search("CODE", "", 9, "Festering Vengeance", top=10)
    assert set(res_fast.keys()) == EXPECTED_SEARCH_KEYS
    assert res_fast["workers"] == 2
    assert res_fast["calculated"] == 200
    assert res_fast["estimate"] == {
        "method": "weightShortlist",
        "calculated": 200,
        "shortlisted": 200,
        "groupsTotal": 500,
        "maxError": None,
    }
    assert res_fast["cutoff"] is None
    assert len(res_fast["outcomes"]) == 10
    assert res_fast["outcomes"][0]["seeds"] == [599]

    from pob_cli.cli_timeless import _format_search_markdown

    text = _format_search_markdown(res_fast, "CombinedDPS", 10)
    assert "Shortlist: 200 of 500 seed groups" in text and "a better seed may exist" in text
    assert "error allowed for" not in text

    # --exact: skips the shortlist, calculates all 500 groups in parallel, and finds what the shortlist missed
    res_exact = timeless.search("CODE", "", 9, "Festering Vengeance", top=5, exact=True)
    assert res_exact["estimate"] is None
    assert res_exact["calculated"] == 500
    assert res_exact["workers"] == 2
    assert res_exact["outcomes"][0]["seeds"] == [100]

    # A small set (<= 200 groups) needs no shortlist: every group, one engine
    seeds[:] = list(range(100, 250))
    res_small = timeless.search("CODE", "", 9, "Festering Vengeance", top=10)
    assert res_small["estimate"] is None and res_small["calculated"] == 150 and res_small["workers"] == 1
    _PrngFakeEngine.search_handler = None


def test_update_timeless_meta_default_output_follows_tree_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("update_timeless_meta", SCRIPTS_ROOT / "update_timeless_meta.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path / "data")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    import json

    for stem in ("alternate_tree_versions", "alternate_passive_skills", "alternate_passive_additions"):
        (raw_dir / f"{stem}.json").write_text(json.dumps([]), encoding="utf-8")
    pob_dir = tmp_path / "PoB"
    (pob_dir / "TreeData" / "3_29").mkdir(parents=True)
    (pob_dir / "TreeData" / "3_29" / "tree.lua").write_text(
        'return { ["nodes"] = { ["root"] = { ["out"] = { "1" } } } }\n', encoding="utf-8"
    )
    assert mod.main(["--raw-meta-dir", str(raw_dir), "--pob-dir", str(pob_dir), "--tree-version", "3_29"]) == 0
    assert (tmp_path / "data" / "timeless_meta_3_29.lua.gz").is_file()
    assert not (tmp_path / "data" / "timeless_meta_3_30.lua.gz").exists()


_MMAP_PROBE = """
local M = dofile(arg[1])
data = {
    timelessJewelTypes = { "Glorious Vanity", "Lethal Pride", [7] = "Abyss Tecrod" },
    timelessJewelSeedMin = { 100, 10000, [7] = 100 }, timelessJewelSeedMax = { 8000, 18000, [7] = 8000 },
    nodeIDList = { size = 3, sizeNotable = 2, [11] = { index = 0, size = 50 }, [12] = { index = 1, size = 50 },
        [13] = { index = 2, size = 50 } },
}
M.configure({ game_dir = arg[2], cache_dir = arg[2] .. "/nocache" })
for _, jt in ipairs({ 7, 2, 1 }) do
    print(jt, tostring(M.ensure_mapped(jt)), tostring(M.readLUT(10000, 11, jt)))
end
"""


def _write_truncated_tables(lut_dir: Path) -> None:
    lut_dir.mkdir(parents=True)
    # ABYS header promising 3 sockets x 7901 seeds of records, then cut off after a few bytes
    header = b"ABYS" + bytes([1, 7]) + (100).to_bytes(2, "little") + (8000).to_bytes(2, "little")
    header += (1).to_bytes(2, "little") + bytes([3, 0]) + b"".join(n.to_bytes(2, "little") for n in (1, 2, 3))
    (lut_dir / "AbyssTecrod.bin").write_bytes(header + bytes([2, 0, 0]))
    (lut_dir / "LethalPride.bin").write_bytes(bytes(300))  # 2 notables x 8001 seeds expected
    (lut_dir / "GloriousVanity.bin").write_bytes(bytes(1000))  # 3 nodes x 7901 seeds of sizes expected


def test_truncated_lut_fails_cleanly_instead_of_crashing_luajit(tmp_path: Path) -> None:
    import shutil
    import subprocess

    from engine_paths import find_runnable_engine

    eng = find_runnable_engine()
    luajit = shutil.which("luajit") or (str(eng / "bin" / "luajit") if eng else None)
    if not luajit or not Path(luajit).is_file():
        pytest.skip("no LuaJIT (bootstrap an engine or put luajit on PATH)")
    game = tmp_path / "game"
    _write_truncated_tables(game / "Data" / "TimelessJewelData")
    probe = tmp_path / "probe.lua"
    probe.write_text(_MMAP_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [luajit, str(probe), str(PKG_ROOT / "lua" / "timeless_mmap.lua"), str(game)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)  # a segfault reads as -11 / 139
    assert proc.stdout.split("\n")[:3] == ["7\tnil\tnil", "2\tnil\tnil", "1\tnil\tnil"]
    assert proc.stderr.count("[timeless_mmap] skipped") == 3
