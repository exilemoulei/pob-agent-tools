"""Parallel timeless jewel seed search orchestrator and batch runner."""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .lut import (
    FLAT_TABLES,
    JEWEL_TYPE_IDS,
    LUT_STEMS,
    SELF_CHECK,
    STAT_KEYS,
    _is_historic_socket_jewel,
    _json_safe_stat,
    _resolve_conqueror,
    _resolve_game_dir,
    _stat_rank_key,
    _worker_batch,
    default_workers,
    group_seeds,
    jewel_text,
    load_jewels,
    load_node_index,
    load_table,
    narrow_down,
)
from .worker import (
    Engine,
    _abort_engines,
    _prevalidate_search,
    _resolve_jewel,
    _seed_nodes,
    _used_tree_nodes,
    ensure_lut_files,
)
from .worker import (
    describe as describe,
)
from .worker import (
    describe_worker as describe_worker,
)


class _SeedBatchRunner:
    """Manages lazy worker engine spawning and parallel batch seed evaluation."""

    def __init__(
        self,
        *,
        code: str,
        template: str,
        slot: str,
        clear: Sequence[str],
        engine_kwargs: dict[str, Any],
        groups: dict[bytes, list[int]],
        precomputed: dict[bytes, dict[str, Any]],
        n_workers: int,
        engines_lock: threading.Lock,
        active_engines: list[Engine],
        engines: list[Engine | None],
        stop: threading.Event,
        results: dict[bytes, dict[str, Any]],
        say: Callable[[str], None],
    ) -> None:
        self.code = code
        self.template = template
        self.slot = slot
        self.clear = clear
        self.engine_kwargs = engine_kwargs
        self.groups = groups
        self.precomputed = precomputed
        self.n_workers = n_workers
        self.engines_lock = engines_lock
        self.active_engines = active_engines
        self.engines = engines
        self.stop = stop
        self.results = results
        self.say = say
        self.lock = threading.Lock()

    def _get_or_spawn_engine(self, i: int) -> Engine | None:
        import pob_cli.timeless as _tl

        with self.engines_lock:
            if self.stop.is_set():
                return None
            existing = self.engines[i]
            if existing is not None:
                return existing
        if self.stop.is_set():
            return None
        eng = _tl.Engine(self.code, self.template, self.slot, self.clear, **self.engine_kwargs)
        should_abort = False
        with self.engines_lock:
            self.active_engines.append(eng)
            if self.stop.is_set():
                should_abort = True
            else:
                self.engines[i] = eng
        if should_abort:
            worker = getattr(eng, "worker", None)
            abort_fn = getattr(worker, "abort", None)
            if callable(abort_fn):
                try:
                    abort_fn()
                except Exception:
                    pass
            try:
                eng.stop()
            except Exception:
                pass
            return None
        return eng

    def calculate(self, sigs: Sequence[bytes]) -> None:
        todo: list[bytes] = []
        for sig in sigs:
            if sig in self.precomputed:
                with self.lock:
                    self.results[sig] = self.precomputed[sig]
            else:
                todo.append(sig)
        if not todo:
            return

        n = min(self.n_workers, len(todo))
        with self.engines_lock:
            if n > len(self.engines):
                self.engines.extend([None] * (n - len(self.engines)))
        first_exc: list[BaseException] = []

        def work(i: int) -> list[Any]:
            try:
                if self.stop.is_set():
                    return []
                engine = self._get_or_spawn_engine(i)
                if engine is None:
                    return []
                for sig in todo[i::n]:
                    if self.stop.is_set():
                        return []
                    raw_stats = engine.stats(self.groups[sig][0])
                    clean_stats = {key: _json_safe_stat(raw_stats.get(key)) for key in STAT_KEYS}
                    with self.lock:
                        self.results[sig] = clean_stats
                        if len(self.results) % 1000 == 0:
                            self.say(f"{len(self.results)} calculated")
                return []
            except BaseException as exc:
                with self.engines_lock:
                    was_stopped = self.stop.is_set()
                    if not was_stopped and not first_exc:
                        first_exc.append(exc)
                    self.stop.set()
                _abort_engines(self.engines_lock, self.active_engines, self.stop)
                if not was_stopped:
                    raise
                return []

        with ThreadPoolExecutor(n) as pool:
            futures = [pool.submit(work, i) for i in range(n)]
            aborted_sent = False
            try:
                # polled: on Windows a plain wait on the pool sees Ctrl+C only after every engine is done
                while not all(future.done() for future in futures):
                    if self.stop.is_set() and not aborted_sent:
                        aborted_sent = True
                        _abort_engines(self.engines_lock, self.active_engines, self.stop)
                    time.sleep(0.05)
            except BaseException:
                if not aborted_sent:
                    aborted_sent = True
                    _abort_engines(self.engines_lock, self.active_engines, self.stop)
                raise
            if first_exc:
                raise first_exc[0]
            for future in futures:
                future.result()


def _group_via_prng(
    first: Engine,
    name: str,
    socket: int,
    seeds: Sequence[int],
    xml: str,
    code: str,
    game_dir: Path,
) -> tuple[dict[bytes, list[int]], list[int], bool, bytes] | None:
    """Group seeds via in-engine `timeless_prng_group_hashes` RPC (all 11 jewels, ~0 MiB RSS)."""
    jtype = JEWEL_TYPE_IDS.get(name)
    if jtype is None:
        return None
    state = first.worker.call("get_tree_state", {}, timeout=60)
    used_nodes = _used_tree_nodes(first.worker, state, xml, code=code or getattr(first, "code", ""))
    conquered = {int(k) for k, v in (state.get("overrides") or {}).items() if v.get("why") == "conquered"}
    used_set = set(used_nodes)

    query_nodes: list[int]
    if 2 <= jtype <= 6:
        try:
            index = load_node_index(game_dir)
            query_nodes = sorted(n for n in (conquered & used_set) if n in index)
        except Exception:
            query_nodes = sorted(conquered & used_set)
    elif jtype == 1:
        query_nodes = sorted(conquered & used_set)
    else:
        query_nodes = []

    step = (seeds[1] - seeds[0]) if len(seeds) > 1 else 1
    payload = {
        "jewel": name,
        "jewelType": jtype,
        "socketId": socket,
        "nodes": query_nodes,
        "usedNodes": used_nodes,
        "seedMin": seeds[0],
        "seedMax": seeds[-1],
        "seedStep": step,
    }
    resp = first.worker.call("timeless_prng_group_hashes", payload, timeout=60.0)
    if not isinstance(resp, dict) or not resp.get("supported"):
        return None

    additive = bool(resp.get("additive"))
    nodes = [int(x) for x in (resp.get("nodes") or [])]
    groups: dict[bytes, list[int]] = {}
    seed0_sig = b""
    for item in resp.get("groups") or []:
        raw_sig = str(item.get("sig") or "")
        sig = bytes.fromhex(raw_sig) if additive else raw_sig.encode("utf-8")
        members = [int(s) for s in (item.get("seeds") or [])]
        if not members:
            continue
        groups[sig] = members
        if members[0] == seeds[0]:
            seed0_sig = sig
    if not groups:
        return None
    return groups, nodes, additive, seed0_sig


def _resolve_seed_groups(
    first: Engine,
    name: str,
    socket: int,
    seeds: Sequence[int],
    xml: str,
    code: str,
    game_dir: Path,
    runtime_dir: Path | str | None,
) -> tuple[dict[bytes, list[int]], list[int], bool, bytes, bool]:
    """Compute seed equivalence groups via PRNG RPC (preferred) or flat LUT / 1-per-seed fallback."""
    prng_active = bool((getattr(first.worker, "caps", None) or {}).get("timelessPrng"))
    if prng_active:
        try:
            prng_grouped = _group_via_prng(first, name, socket, seeds, xml, code, game_dir)
        except Exception:
            prng_grouped = None
            if seeds:
                first.stats(seeds[0])
        if prng_grouped is not None:
            groups, nodes, additive, seed0_sig = prng_grouped
            return groups, nodes, additive, seed0_sig, True
    if name in FLAT_TABLES:
        lut_dir: Path | None = None
        if prng_active:
            try:
                lut_dir = ensure_lut_files(game_dir, FLAT_TABLES[name], runtime_dir=runtime_dir)
            except Exception:
                lut_dir = None
        index = load_node_index(game_dir)
        table = load_table(game_dir, FLAT_TABLES[name], runtime_dir=runtime_dir, cache_dir=lut_dir)
        if len(table) != len(index) * len(seeds):
            raise ValueError(f"{name} lookup table has an unexpected size for its seed range.")
        nodes = _seed_nodes(first, xml, index, code=code)
        groups = group_seeds(seeds, table, index, nodes)
        seed0_sig = bytes(table[index[node] * len(seeds)] for node in nodes)
        return groups, nodes, True, seed0_sig, False
    if prng_active:
        stem = LUT_STEMS.get(name, name.replace(" ", ""))
        try:
            ensure_lut_files(game_dir, stem, runtime_dir=runtime_dir)
        except Exception:
            pass
    groups = {str(seed).encode(): [seed] for seed in seeds}
    return groups, [], False, str(seeds[0]).encode(), False


_PRNG_SHORTLIST_THRESHOLD = 200
_MIN_PRNG_SHORTLIST_K = 50


def _shortlist_non_additive_groups(
    first: Engine,
    name: str,
    socket: int,
    stat: str,
    top: int,
    groups: dict[bytes, list[int]],
    seed0_sig: bytes,
    say: Callable[[str], None],
) -> list[bytes] | None:
    """Shortlist top `K >= max(top*4, 50)` non-additive PRNG groups via PoB's weighted finder."""
    jtype = JEWEL_TYPE_IDS.get(name)
    if jtype is None or len(groups) <= _PRNG_SHORTLIST_THRESHOLD:
        return None
    target_k = min(len(groups), max(top * 4, _MIN_PRNG_SHORTLIST_K))
    fetch_top = min(sum(len(v) for v in groups.values()), max(target_k * 2, 120))
    try:
        resp = first.worker.call(
            "timeless_prng_search",
            {"jewel": name, "jewelType": jtype, "socketId": socket, "stat": stat, "top": fetch_top},
            timeout=60.0,
        )
    except Exception:
        return None
    top_seeds = (resp or {}).get("topSeeds") if isinstance(resp, dict) else None
    if not top_seeds:
        return None

    seed_to_sig = {s: sig for sig, members in groups.items() for s in members}
    selected: list[bytes] = []
    seen: set[bytes] = set()
    for entry in top_seeds:
        if not isinstance(entry, dict) or entry.get("seed") is None:
            continue
        sig = seed_to_sig.get(int(entry["seed"]))
        if sig is not None and sig not in seen:
            seen.add(sig)
            selected.append(sig)
            if len(selected) >= target_k:
                break
    if not selected:
        return None
    if seed0_sig in groups and seed0_sig not in seen and len(selected) < target_k:
        selected.append(seed0_sig)
    say(f"shortlisted {len(selected)} of {len(groups)} groups via timeless_prng_search; verifying in PoB")
    return selected


def _build_search_outcomes(
    results: dict[bytes, dict[str, Any]],
    groups: dict[bytes, list[int]],
    stat: str,
    top: int,
    estimate: dict[str, Any] | None,
) -> tuple[int | float | str | None, list[dict[str, Any]]]:
    outcomes: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for sig, stats in results.items():
        outcomes[tuple(_json_safe_stat(stats.get(key)) for key in STAT_KEYS)].extend(groups[sig])
    rank = STAT_KEYS.index(stat)
    ranked = sorted(outcomes.items(), key=lambda kv: _stat_rank_key(kv[0][rank]), reverse=True)
    # The estimate only speaks for seeds at or above the value of the `top`-th best calculated group. What the
    # sample happened to hit below that is left out: it would read as a ranking and is not one.
    cutoff: int | float | str | None = None
    if estimate and results:
        sorted_vals = sorted((s.get(stat) for s in results.values()), key=_stat_rank_key, reverse=True)
        cutoff_raw = sorted_vals[min(top, len(sorted_vals)) - 1]
        cutoff_key = _stat_rank_key(cutoff_raw)
        cutoff = _json_safe_stat(cutoff_raw)
        ranked = [kv for kv in ranked if _stat_rank_key(kv[0][rank]) >= cutoff_key]
    return cutoff, [
        {"stats": dict(zip(STAT_KEYS, key, strict=False)), "seeds": sorted(members)}
        for key, members in ranked
    ]


def search(
    code: str,
    xml: str,
    socket: int,
    jewel_name: str,
    *,
    conqueror: str | None = None,
    mods: Sequence[str] = (),
    stat: str = "CombinedDPS",
    top: int = 15,
    workers: int | None = None,
    exact: bool = False,
    progress: Callable[[str], None] | None = None,
    pob_dir: Path | str | None = None,
    runtime_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Find the seeds of a timeless jewel at `socket` that give the highest `stat`.

    The jewel replaces whatever is in that socket, and other Historic jewels are taken out (`removed`);
    `baseline` is the build as loaded. With `exact` every group is calculated; otherwise large searches
    calculate a sample, estimate the rest, and calculate the estimated-best until none of the remaining is
    estimated to reach the top `top` (see `narrow_down`).
    """
    import pob_cli.timeless as _tl

    pre_game_dir, pre_jewels, conqueror = _prevalidate_search(
        jewel_name, conqueror, mods, stat, socket, xml, pob_dir, runtime_dir
    )

    started = time.time()
    top = max(1, top)
    say = progress or (lambda _msg: None)
    slot = f"Jewel {socket}"
    engine_kwargs = {k: v for k, v in (("pob_dir", pob_dir), ("runtime_dir", runtime_dir)) if v}
    first = _tl.Engine(code, "", slot, **engine_kwargs)
    engines_lock = threading.Lock()
    active_engines: list[Engine] = [first]
    engines: list[Engine | None] = [first]
    stop = threading.Event()

    try:
        if hasattr(first, "ensure_loaded"):
            first.ensure_loaded()
        else:
            first.reset()
        game_dir = _resolve_game_dir(first.worker, pob_dir=pob_dir, runtime_dir=runtime_dir)
        jewels = pre_jewels if (pre_jewels is not None and pre_game_dir == game_dir) else load_jewels(game_dir)

        state, dialog, _closed = _worker_batch(
            first.worker,
            [("get_tree_state", {}), ("tj_open", {}), ("tj_close", {})],
            timeout=60.0,
        )
        name = _resolve_jewel(jewel_name, jewels, first, dialog=dialog)
        jewel = jewels[name]
        conqueror = _resolve_conqueror(conqueror, jewel, name)

        allocated = {int(x) for x in ((state or {}).get("allocatedNodes") or [])}
        sockets = {int(s["nodeId"]): s for s in ((state or {}).get("sockets") or [])}
        # PoB puts a jewel aimed at a slot that does not exist into any free socket instead of refusing it
        if socket not in sockets or socket not in allocated:
            raise ValueError(f"Node {socket} is not an allocated jewel socket of this build (see `pob timeless list`).")
        if sockets[socket].get("charm"):
            raise ValueError(f"Node {socket} is a charm socket, not a timeless jewel socket (see `pob timeless list`).")
        valid_tj_sockets = {
            int(x)
            for x in ((dialog or {}).get("socketIds") or [])
            if isinstance(x, (int, float)) and int(x) > 0
        }
        if valid_tj_sockets and socket not in valid_tj_sockets:
            raise ValueError(f"Node {socket} is not a valid timeless jewel socket (see `pob timeless list`).")

        removed = [
            {
                "jewel": s.get("title") or s.get("name") or s.get("baseName") or "Timeless Jewel",
                "socket": node,
            }
            for node, s in sockets.items()
            if node != socket and node in allocated and _is_historic_socket_jewel(s, jewels)
        ]
        clear = first.clear = [f"Jewel {other['socket']}" for other in removed]
        template = first.template = jewel_text(jewel, name, conqueror, mods)
        seeds = list(jewel["seeds"])
        baseline_raw = first.read()
        baseline = {key: _json_safe_stat(baseline_raw.get(key)) for key in STAT_KEYS}

        first_stats_raw = first.stats(seeds[0])  # socket one so PoB reports what the jewel conquers
        first_stats = {key: _json_safe_stat(first_stats_raw.get(key)) for key in STAT_KEYS}
        groups, nodes, additive, seed0_sig, used_prng = _resolve_seed_groups(
            first, name, socket, seeds, xml, code, game_dir, runtime_dir
        )

        precomputed: dict[bytes, dict[str, Any]] = {}
        if seed0_sig in groups and groups[seed0_sig][0] == seeds[0]:
            precomputed[seed0_sig] = first_stats

        shortlist: list[bytes] | None = None
        if used_prng and not additive and not exact and len(groups) > _PRNG_SHORTLIST_THRESHOLD:
            shortlist = _shortlist_non_additive_groups(
                first, name, socket, stat, top, groups, seed0_sig, say
            )

        target_count = len(shortlist) if shortlist is not None else len(groups)
        default_w = 1 if (used_prng and (not exact or len(groups) <= _PRNG_SHORTLIST_THRESHOLD)) else default_workers()
        n_workers = max(1, min(workers if workers is not None else default_w, max(1, target_count)))
        results: dict[bytes, dict[str, Any]] = {}
        runner = _SeedBatchRunner(
            code=code,
            template=template,
            slot=slot,
            clear=clear,
            engine_kwargs=engine_kwargs,
            groups=groups,
            precomputed=precomputed,
            n_workers=n_workers,
            engines_lock=engines_lock,
            active_engines=active_engines,
            engines=engines,
            stop=stop,
            results=results,
            say=say,
        )

        rng = random.Random(1)
        all_sigs = list(groups)
        estimate: dict[str, Any] | None = None
        if shortlist is not None:
            runner.calculate(shortlist)
            estimate = {
                "calculated": len(results),
                "shortlisted": True,
                "groupsConsidered": len(shortlist),
                "groupsTotal": len(groups),
                "maxError": 0.0,
                "relative": True,
            }
        elif exact or not nodes or not additive:
            runner.calculate(all_sigs)
        else:
            estimate = narrow_down(all_sigs, results, runner.calculate, stat, top, rng, say)

        # Self-check: another member of a group must give the very same numbers as its representative.
        multi = [sig for sig in results if len(groups[sig]) > 1]
        picks = rng.sample(multi, min(SELF_CHECK, len(multi)))
        mismatches = []
        for sig in picks:
            other = rng.choice(groups[sig][1:])
            other_raw = first.stats(other)
            other_clean = {key: _json_safe_stat(other_raw.get(key)) for key in STAT_KEYS}
            if other_clean != results[sig]:
                mismatches.append(other)
    finally:
        seen_ids: set[int] = set()
        with engines_lock:
            to_stop = list(active_engines) + [e for e in engines if e is not None]
        for engine in to_stop:
            if id(engine) in seen_ids:
                continue
            seen_ids.add(id(engine))
            try:
                engine.stop()
            except Exception:
                pass

    cutoff, outcomes_list = _build_search_outcomes(results, groups, stat, top, estimate)
    return {
        "cutoff": cutoff,
        "jewel": name,
        "conqueror": conqueror,
        "socket": socket,
        "mods": list(mods),
        "removed": removed,
        "stat": stat,
        "seedCount": len(seeds),
        "groupCount": len(groups),
        "calculated": len(results),
        "estimate": estimate,
        "seedNodes": nodes,
        "workers": n_workers,
        "baseline": baseline,
        "outcomes": outcomes_list,
        "selfCheck": {"checked": len(picks), "mismatches": mismatches},
        "seconds": round(time.time() - started, 1),
    }
