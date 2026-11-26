"""Throwaway PoB engine wrapper, build tree inspection, and search pre-validation."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import CrossGameHallucinationError
from ..game_data import fold_han
from ..guardrails import GuardrailScope, check_input, check_poe2_timeless_jewel
from ..updater import (
    _download_pob_files_to_staging,
    fetch_pob_manifest_remote,
    get_shared_cache_dir,
    verify_pob_sha1,
)
from .lut import (
    FLAT_TABLES,
    JEWEL_TYPE_IDS,
    RESET_EVERY,
    STAT_KEYS,
    _extract_allocated_notable_names,
    _extract_xml_allocated_nodes,
    _json_safe_stat,
    _local_jewel_name,
    _resolve_conqueror,
    _resolve_game_dir,
    _resolve_jewel_static,
    _worker_batch,
    load_jewels,
    load_node_index,
)


class CliUsageError(ValueError):
    """Raised when a CLI/search argument names a non-jewel entity before spawning any PoB engine."""


_PUTRID_SEED_KEYS = frozenset({"putridseed", "腐朽之种", "腐败之种"})


def _check_putrid_seed(jewel_name: str) -> None:
    folded = fold_han(jewel_name.strip()).lower()
    compact = folded.replace(" ", "").replace("_", "").replace("-", "")
    if compact in _PUTRID_SEED_KEYS:
        raise CliUsageError(
            f"{jewel_name!r} is not a Timeless Jewel item. "
            "If you meant the PoE1 3.30 Abyss Timeless Jewel, pass `--jewel \"Festering Vengeance\"` "
            "(`溃烂复仇`). If you meant the passive-tree socket cluster notable fork (`Putrid Seed` / "
            "`Mycelium Core`), inspect it with `pob tree --search \"Putrid Seed\"`."
        )


class Engine:
    """One throwaway PoB engine holding the build; recalculates it with a given seed socketed."""

    def __init__(
        self,
        code: str,
        template: str,
        slot: str,
        clear: Sequence[str] = (),
        pob_dir: Path | str | None = None,
        runtime_dir: Path | str | None = None,
    ) -> None:
        import pob_cli.timeless as _tl

        self.code = code
        self.template = template
        self.slot = slot
        self.clear: Sequence[str] = list(clear)
        self.count = 0
        self.pob_dir = Path(pob_dir) if pob_dir else None
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        self._loaded = False
        self._cleared_slots: tuple[str, ...] = ()
        self.worker = _tl.BridgeWorker("test", pob_dir=self.pob_dir, runtime_dir=self.runtime_dir)
        self._start_worker()

    def _start_worker(self) -> None:
        try:
            if self.code:
                try:
                    self.worker.start(restore_code=self.code)
                except TypeError:
                    self.worker.start()
                    return
                if getattr(self.worker, "last_good_code", None) == self.code:
                    self._loaded = True
            else:
                self.worker.start()
        except BaseException:
            try:
                self.worker.stop()
            except Exception:
                pass
            raise

    def ensure_loaded(self) -> None:
        if not getattr(self, "_loaded", False):
            self.reset()

    def reset(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("import_code", {"code": self.code, "mode": "replace"}),
        ]
        for slot in self.clear:  # other Historic jewels: the game allows one, PoB would count them all
            calls.append(("unequip_slot", {"slotName": slot}))
        _worker_batch(self.worker, calls, timeout=120.0)
        self.count = 0
        self._loaded = True
        self._cleared_slots = tuple(self.clear)

    def read(self) -> dict[str, Any]:
        stats = self.worker.call("get_stats", {}, timeout=60).get("stats") or {}
        return {key: _json_safe_stat(stats.get(key)) for key in STAT_KEYS}

    def stats(self, seed: int) -> dict[str, Any]:
        loaded = getattr(self, "_loaded", False)
        need_reset = (not loaded) or (self.count > 0 and self.count % RESET_EVERY == 0)
        pending_clear: list[str] = []
        if need_reset:
            self.reset()
        else:
            cleared = getattr(self, "_cleared_slots", ())
            pending_clear = [s for s in self.clear if s not in cleared]
        self.count += 1
        # not str.format: item lines may carry braces of their own, e.g. `{crafted}`
        raw = self.template.replace("{seed}", str(seed))
        calls: list[tuple[str, dict[str, Any]]] = [
            ("unequip_slot", {"slotName": slot}) for slot in pending_clear
        ]
        # The replaced jewel stays in the item list until the next reload: deleting it is a second
        # full recalculation per seed (measured 72 ms a seed against 51 ms on PoB 2.69), and a
        # reload every RESET_EVERY seeds costs far less than that.
        calls.append(("add_item", {"raw": raw, "equip": True, "slotName": self.slot}))
        calls.append(("get_stats", {}))
        responses = _worker_batch(self.worker, calls, timeout=60.0)
        if pending_clear:
            self._cleared_slots = tuple(self.clear)
        stats = (responses[-1] if responses else {}).get("stats") or {}
        return {key: _json_safe_stat(stats.get(key)) for key in STAT_KEYS}

    def stop(self) -> None:
        self.worker.stop()


def describe_worker(
    worker: Any,
    pob_dir: Path | str | None = None,
    runtime_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Jewel types (English + local display name) and allocated jewel sockets on a live worker."""
    game_dir = _resolve_game_dir(worker, pob_dir=pob_dir, runtime_dir=runtime_dir)
    jewels = load_jewels(game_dir)
    dialog, _closed, tree_state = _worker_batch(
        worker,
        [("tj_open", {}), ("tj_close", {}), ("get_tree_state", {})],
        timeout=60.0,
    )
    allocated = {int(x) for x in ((tree_state or {}).get("allocatedNodes") or [])}
    charm_ids = {
        int(s["nodeId"])
        for s in ((tree_state or {}).get("sockets") or [])
        if isinstance(s, dict) and s.get("charm") and s.get("nodeId") is not None
    }
    jewel_opts = ((dialog or {}).get("jewel") or {}).get("options") or []
    dialog_local = {
        str(o["label"]): str(o.get("labelZh") or o["label"])
        for o in jewel_opts
        if isinstance(o, dict) and o.get("label")
    }
    socket_opts = ((dialog or {}).get("socket") or {}).get("options") or []
    socket_ids = (dialog or {}).get("socketIds") or []
    sockets = [
        {"nodeId": int(node), "label": opt.get("label"), "labelLocal": opt.get("labelZh")}
        for node, opt in zip(socket_ids, socket_opts, strict=False)
        if isinstance(node, (int, float)) and int(node) > 0 and int(node) in allocated and int(node) not in charm_ids
    ]
    prng_types = _prng_types(worker) if (getattr(worker, "caps", None) or {}).get("timelessPrng") else set()
    rt = runtime_dir or getattr(worker, "runtime_dir", None)
    return {
        "jewels": [
            {
                "name": name,
                "nameLocal": _local_jewel_name(name, dialog_local, rt),
                "seeds": len(j["seeds"]),
                "conquerors": list(j["conquerors"]),
                "grouped": (name in FLAT_TABLES) or (JEWEL_TYPE_IDS.get(name) in prng_types),
            }
            for name, j in jewels.items()
        ],
        "sockets": sockets,
    }


def _prng_types(worker: Any) -> set[int]:
    """Jewel types the engine's generator serves for the loaded build: the gate the search's grouping applies."""
    try:
        resp = worker.call("timeless_prng_types", {}, timeout=60)
    except Exception:  # a host without this RPC: claim only what the flat tables can group
        return set()
    return {int(x) for x in ((resp or {}).get("types") or [])}


def describe(
    code: str,
    pob_dir: Path | str | None = None,
    runtime_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Jewel types (English + this PoB's display language) and the build's allocated jewel sockets."""
    import pob_cli.timeless as _tl

    engine_kwargs = {k: v for k, v in (("pob_dir", pob_dir), ("runtime_dir", runtime_dir)) if v}
    engine = _tl.Engine(code, "", "", **engine_kwargs)
    try:
        if hasattr(engine, "ensure_loaded"):
            engine.ensure_loaded()
        else:
            engine.reset()
        return describe_worker(engine.worker, pob_dir=pob_dir, runtime_dir=runtime_dir)
    finally:
        engine.stop()


def _resolve_jewel(
    name: str,
    jewels: dict[str, Any],
    engine: Engine,
    dialog: dict[str, Any] | None = None,
) -> str:
    """Resolve English name, static Chinese display name/alias, or PoB's localized display name."""
    static_match = _resolve_jewel_static(name, jewels)
    if static_match is not None:
        return static_match
    query = name.strip()
    q_low = query.lower()
    if dialog is None:
        dialog, _closed = _worker_batch(engine.worker, [("tj_open", {}), ("tj_close", {})], timeout=60.0)
    options = ((dialog or {}).get("jewel") or {}).get("options") or []
    for option in options:
        if not isinstance(option, dict):
            continue
        label = option.get("label")
        label_zh = option.get("labelZh")
        if label in jewels and (
            query in (label_zh, label)
            or (isinstance(label_zh, str) and label_zh.strip().lower() == q_low)
            or (isinstance(label, str) and label.strip().lower() == q_low)
        ):
            return str(label)
    raise ValueError(f"Unknown timeless jewel {name!r}. Known: {', '.join(jewels)}")


def _seed_nodes(
    engine: Engine,
    xml: str,
    index: dict[int, int],
    code: str = "",
    state: dict[str, Any] | None = None,
) -> list[int]:
    """Notables whose table entry can change this build: conquered by the socketed jewel AND either allocated
    or granted by an item ("Allocates X", e.g. an anoint) - PoB uses the conquered version for those too."""
    if state is None:
        state = engine.worker.call("get_tree_state", {}, timeout=60)
    conquered = {int(k) for k, v in (state.get("overrides") or {}).items() if v.get("why") == "conquered"}
    used = {int(x) for x in (state.get("allocatedNodes") or [])}
    granted = _extract_allocated_notable_names(xml, code or getattr(engine, "code", ""))
    if granted:
        tree = engine.worker.get_cached_tree_data(state.get("treeVersion")).get("nodes", {})
        used |= {int(node) for node, info in tree.items() if info.get("name") in granted}
    return sorted(node for node in conquered & used if node in index)


def _build_radius_summary(
    engine: Engine,
    socket: int,
    jewel: dict[str, Any],
    state: dict[str, Any] | None,
    xml: str,
    code: str,
    game_dir: Path,
) -> dict[str, Any]:
    """Classify allocated vs unallocated notables inside the jewel's radius using worker tree data."""
    allocated_set = {int(x) for x in ((state or {}).get("allocatedNodes") or [])}
    overrides = (state or {}).get("overrides") or {}
    conquered_ids = {int(k) for k, v in overrides.items() if isinstance(v, dict) and v.get("why") == "conquered"}
    tree_nodes: dict[Any, Any] = {}
    get_tree_fn = getattr(engine.worker, "get_cached_tree_data", None)
    if callable(get_tree_fn):
        try:
            tree_nodes = (get_tree_fn((state or {}).get("treeVersion")) or {}).get("nodes") or {}
        except Exception:
            tree_nodes = {}

    radius_ids = set(conquered_ids)
    sock_info = (tree_nodes.get(str(socket)) or tree_nodes.get(socket)) if tree_nodes else None
    sx = sock_info.get("x") if isinstance(sock_info, dict) else None
    sy = sock_info.get("y") if isinstance(sock_info, dict) else None
    if any(str(h).startswith("Radius:") for h in (jewel.get("head") or [])) and isinstance(sx, (int, float)) and isinstance(sy, (int, float)):
        tv = str((state or {}).get("treeVersion") or "")
        large_r = 1500.0 if tv.startswith(("3_15", "3_14", "3_13", "3_12", "3_11", "3_10", "3_9", "3_8")) else 1800.0
        for raw_nid, info in tree_nodes.items():
            if not isinstance(info, dict) or info.get("asc") or info.get("proxy") or info.get("expansion"):
                continue
            if info.get("type") in ("ClassStart", "AscendClassStart"):
                continue
            nx, ny = info.get("x"), info.get("y")
            if isinstance(nx, (int, float)) and isinstance(ny, (int, float)) and math.hypot(float(nx) - float(sx), float(ny) - float(sy)) <= large_r:
                radius_ids.add(int(info.get("id", raw_nid)))

    node_index: dict[int, int] | None = None
    if (Path(game_dir) / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua").is_file():
        try:
            node_index = load_node_index(game_dir)
        except Exception:
            node_index = None

    granted = _extract_allocated_notable_names(xml, code or getattr(engine, "code", ""))
    alloc_notables: list[dict[str, Any]] = []
    unalloc_notables: list[dict[str, Any]] = []
    for nid in sorted(radius_ids - {socket}):
        info = (tree_nodes.get(str(nid)) or tree_nodes.get(nid)) if tree_nodes else None
        ov = overrides.get(str(nid)) if isinstance(overrides, dict) else None
        if isinstance(info, dict) and ("type" in info or "isNotable" in info):
            is_notable = info.get("type") == "Notable" or bool(info.get("isNotable"))
        elif node_index is not None:
            is_notable = nid in node_index
        else:
            is_notable = nid in conquered_ids
        if not is_notable:
            continue
        nm = str((info.get("name") if isinstance(info, dict) else None) or (ov.get("name") if isinstance(ov, dict) else None) or f"Node {nid}")
        zh = (info.get("nameZh") if isinstance(info, dict) else None) or (ov.get("nameZh") if isinstance(ov, dict) else None)
        entry: dict[str, Any] = {"id": nid, "name": nm}
        if isinstance(zh, str) and zh and zh != nm:
            entry["nameZh"] = zh
        target_list = alloc_notables if ((nid in allocated_set) or (isinstance(info, dict) and info.get("name") in granted)) else unalloc_notables
        target_list.append(entry)

    return {
        "allocatedNodeIds": sorted(nid for nid in radius_ids if nid in allocated_set and nid != socket),
        "allocatedNotables": alloc_notables,
        "unallocatedNotables": unalloc_notables,
    }


def _prevalidate_search(
    jewel_name: str,
    conqueror: str | None,
    mods: Sequence[str],
    stat: str,
    socket: int | Sequence[int] | str,
    xml: str,
    pob_dir: Path | str | None,
    runtime_dir: Path | str | None,
) -> tuple[Path | None, dict[str, dict[str, Any]] | None, str | None]:
    """Run guardrails and fast pre-spawn validation before launching a throwaway engine."""
    import pob_cli.timeless as _tl

    _check_putrid_seed(jewel_name)
    check_poe2_timeless_jewel(jewel_name, expected_game="poe1")
    check_input(jewel_name, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    for mod in mods:
        check_input(mod, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")

    if stat not in STAT_KEYS:
        raise ValueError(f"Unknown stat {stat!r}. Known: {', '.join(STAT_KEYS)}")

    if isinstance(socket, str):
        if socket.strip().lower() != "all":
            raise ValueError(f"Invalid socket {socket!r}; expected socket ID(s) or 'all'.")
        explicit_sockets: list[int] = []
    elif isinstance(socket, int) and not isinstance(socket, bool):
        explicit_sockets = [socket]
    else:
        explicit_sockets = [int(s) for s in socket]
        if not explicit_sockets:
            raise ValueError("At least one socket node ID is required.")

    for sock_id in explicit_sockets:
        if sock_id <= 0:
            raise ValueError(f"Node {sock_id} is not an allocated jewel socket of this build (see `pob timeless list`).")

    pre_game_dir: Path | None = None
    if pob_dir is not None:
        pre_game_dir = _resolve_game_dir(None, pob_dir=pob_dir, runtime_dir=runtime_dir)
    elif getattr(_tl.Engine, "game_dir", None) is not None:
        pre_game_dir = Path(_tl.Engine.game_dir)
    else:
        try:
            pre_game_dir = _resolve_game_dir(None, runtime_dir=runtime_dir)
        except CrossGameHallucinationError:
            raise
        except Exception:
            pre_game_dir = None

    pre_jewels: dict[str, dict[str, Any]] | None = None
    if pre_game_dir is not None and (pre_game_dir / "Data" / "Uniques" / "jewel.lua").is_file():
        pre_jewels = load_jewels(pre_game_dir)
        if pre_jewels:
            pre_name = _resolve_jewel_static(jewel_name, pre_jewels)
            if pre_name is not None:
                conqueror = _resolve_conqueror(conqueror, pre_jewels[pre_name], pre_name)
            elif jewel_name.strip().isascii():
                raise ValueError(f"Unknown timeless jewel {jewel_name!r}. Known: {', '.join(pre_jewels)}")

    xml_nodes = _extract_xml_allocated_nodes(xml)
    if xml_nodes is not None:
        for sock_id in explicit_sockets:
            if sock_id not in xml_nodes:
                raise ValueError(f"Node {sock_id} is not an allocated jewel socket of this build (see `pob timeless list`).")

    return pre_game_dir, pre_jewels, conqueror


def _used_tree_nodes(worker: Any, state: dict[str, Any], xml: str, code: str = "") -> list[int]:
    """Return sorted node IDs allocated on the tree or granted via `Allocates <Notable>`."""
    used = {int(x) for x in (state.get("allocatedNodes") or [])}
    granted = _extract_allocated_notable_names(xml, code)
    if granted:
        tree = worker.get_cached_tree_data(state.get("treeVersion")).get("nodes", {})
        used |= {int(node) for node, info in tree.items() if info.get("name") in granted}
    return sorted(used)


_MAPPING_REL = "data/timelessjeweldata/nodeindexmapping.lua"


def lut_cache_dir(game_dir: Path, runtime_dir: Path | str | None = None) -> Path:
    """Shared folder for fetched tables, keyed (as `lua/timeless_mmap.lua` does) by the SHA-1 of this PoB's
    NodeIndexMapping.lua: a table read against another PoB version's node index gives wrong mods silently."""
    mapping = Path(game_dir) / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua"
    try:
        key = hashlib.sha1(mapping.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        raise RuntimeError(f"cannot read {mapping} ({exc}); run `pob update`") from exc
    rt = Path(runtime_dir) if runtime_dir else None
    return get_shared_cache_dir(rt, pob_root=Path(game_dir).parent) / "timeless_lut" / key


def ensure_lut_files(
    game_dir: Path,
    stem: str,
    *,
    runtime_dir: Path | str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
) -> Path:
    """Folder holding `<stem>.zip` / `.zip.part*`: PoB's own, else `lut_cache_dir`, fetched there on demand.

    Used only for fallback (`jewelType > 11`, a tree-version or PoB mismatch on slim installs); never writes
    into `game_dir` (safe for read-only seeds and ETag-304 slim installs). A table is fetched only from a PoB
    whose NodeIndexMapping.lua is the installed one. Raises RuntimeError saying why when none can be had.
    """
    data_dir = Path(game_dir) / "Data" / "TimelessJewelData"
    if (data_dir / f"{stem}.zip").is_file() or (data_dir / f"{stem}.zip.part0").is_file():
        return data_dir

    cache_dir = lut_cache_dir(game_dir, runtime_dir)
    if (cache_dir / f"{stem}.zip").is_file() or (
        (cache_dir / f"{stem}.zip.part0").is_file() and (cache_dir / f"{stem}.zip.complete").is_file()
    ):
        return cache_dir

    if os.environ.get("POB_OFFLINE", "").strip() == "1":
        raise RuntimeError(f"{stem} is not installed and POB_OFFLINE=1 rules out fetching it")

    status, manifest, _etag = fetch_pob_manifest_remote(proxy=proxy, mirror=mirror)
    if status != "ok" or manifest is None:
        raise RuntimeError(f"PoB's manifest could not be read to fetch {stem} ({status})")
    all_entries = list(manifest.files.values() if isinstance(manifest.files, dict) else manifest.files)
    mapping = next((e for e in all_entries if e.local_rel.replace("\\", "/").lower() == _MAPPING_REL), None)
    if mapping is None or not verify_pob_sha1((data_dir / "NodeIndexMapping.lua").read_bytes(), mapping.sha1):
        raise RuntimeError(
            f"PoB {manifest.version} indexes its {stem} table differently from the installed PoB; "
            "run `pob update` first"
        )
    prefix = f"data/timelessjeweldata/{stem.lower()}.zip"
    entries = [e for e in all_entries if e.local_rel.replace("\\", "/").lower().startswith(prefix)]
    if not entries:
        raise RuntimeError(f"PoB {manifest.version} ships no {stem} table")

    cache_dir.mkdir(parents=True, exist_ok=True)
    has_parts = any(".part" in Path(e.local_rel).name.lower() for e in entries)
    complete_marker = cache_dir / f"{stem}.zip.complete"
    if has_parts:
        complete_marker.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="pob_lut_stage_") as stage_str:
        staged = _download_pob_files_to_staging(
            manifest, entries, Path(stage_str), proxy=proxy, mirror=mirror, max_workers=4
        )
        for _rel, src_path in staged.items():
            dst = cache_dir / src_path.name
            tmp_dst = cache_dir / f".{src_path.name}.{os.getpid()}.tmp"
            shutil.copy2(src_path, tmp_dst)
            os.replace(tmp_dst, dst)
    if has_parts:
        complete_marker.write_text("ok\n", encoding="utf-8")
    return cache_dir


def _abort_engines(
    engines_lock: threading.Lock,
    active_engines: list[Engine],
    stop: threading.Event,
) -> None:
    with engines_lock:
        stop.set()
        snapshot = list(active_engines)
    for eng in snapshot:
        worker = getattr(eng, "worker", None)
        abort_fn = getattr(worker, "abort", None)
        if callable(abort_fn):
            try:
                abort_fn()
            except Exception:
                pass
