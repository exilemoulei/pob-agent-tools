"""Parallel timeless jewel seed search orchestrator (`TimelessSearchEngine` + `search`)."""

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
    _build_radius_summary,
    _prevalidate_search,
    _resolve_jewel,
    _seed_nodes,
    _used_tree_nodes,
    describe,
    describe_worker,
    ensure_lut_files,
)

__all__ = ["TimelessSearchEngine", "describe", "describe_worker", "search"]

_MIN_PRNG_SHORTLIST = 200


def _clean_stats(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: _json_safe_stat(raw.get(k)) for k in STAT_KEYS}


def _group_via_prng(
    first: Engine,
    name: str,
    socket: int,
    seeds: Sequence[int],
    xml: str,
    code: str,
    game_dir: Path,
    state: dict[str, Any] | None = None,
) -> tuple[dict[bytes, list[int]], list[int], bool, bytes] | None:
    jtype = JEWEL_TYPE_IDS.get(name)
    if jtype is None:
        return None
    if state is None:
        state = first.worker.call("get_tree_state", {}, timeout=60)
    used_nodes = _used_tree_nodes(first.worker, state, xml, code=code or getattr(first, "code", ""))
    conquered = {int(k) for k, v in (state.get("overrides") or {}).items() if v.get("why") == "conquered"}
    used_set = set(used_nodes)
    if 2 <= jtype <= 6:
        try:
            idx = load_node_index(game_dir)
            q_nodes = sorted(n for n in (conquered & used_set) if n in idx)
        except Exception:
            q_nodes = sorted(conquered & used_set)
    else:
        q_nodes = sorted(conquered & used_set) if jtype == 1 else []
    resp = first.worker.call(
        "timeless_prng_group_hashes",
        {
            "jewel": name, "jewelType": jtype, "socketId": socket, "nodes": q_nodes, "usedNodes": used_nodes,
            "seedMin": seeds[0], "seedMax": seeds[-1], "seedStep": (seeds[1] - seeds[0]) if len(seeds) > 1 else 1,
        },
        timeout=60.0,
    )
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
        if members:
            groups[sig] = members
            if members[0] == seeds[0]:
                seed0_sig = sig
    return (groups, nodes, additive, seed0_sig) if groups else None


def _fetch_lut(game_dir: Path, stem: str, runtime_dir: Path | str | None) -> tuple[Path | None, str]:
    try:
        return ensure_lut_files(game_dir, stem, runtime_dir=runtime_dir), ""
    except Exception as exc:
        return None, str(exc).strip() or type(exc).__name__


def _resolve_seed_groups(
    first: Engine,
    name: str,
    socket: int,
    seeds: Sequence[int],
    xml: str,
    code: str,
    game_dir: Path,
    runtime_dir: Path | str | None,
    state: dict[str, Any] | None = None,
) -> tuple[dict[bytes, list[int]], list[int], bool, bytes, bool]:
    prng_active = bool((getattr(first.worker, "caps", None) or {}).get("timelessPrng"))
    unsupported = False
    if prng_active:
        try:
            grouped = _group_via_prng(first, name, socket, seeds, xml, code, game_dir, state=state)
        except Exception:
            grouped, state = None, None
            first.reset()
            if seeds:
                first.stats(seeds[0])
        else:
            unsupported = grouped is None
        if grouped is not None:
            return (*grouped, True)
    if name in FLAT_TABLES:
        lut_dir, why = _fetch_lut(game_dir, FLAT_TABLES[name], runtime_dir)
        idx = load_node_index(game_dir)
        tbl = load_table(game_dir, FLAT_TABLES[name], runtime_dir=runtime_dir, cache_dir=lut_dir, missing_reason=why)
        if len(tbl) != len(idx) * len(seeds):
            raise ValueError(f"{name} lookup table has an unexpected size for its seed range.")
        nodes = _seed_nodes(first, xml, idx, code=code, state=state)
        return group_seeds(seeds, tbl, idx, nodes), nodes, True, bytes(tbl[idx[n] * len(seeds)] for n in nodes), False
    if prng_active:
        stem = LUT_STEMS.get(name, name.replace(" ", ""))
        lut_dir, why = _fetch_lut(game_dir, stem, runtime_dir)
        if lut_dir is None and unsupported:
            raise RuntimeError(f"{name} needs PoB's {stem} lookup table on this build, and it is unavailable: {why}")
    return {str(s).encode(): [s] for s in seeds}, [], False, str(seeds[0]).encode(), False


def _shortlist_non_additive_groups(
    first: Engine, name: str, socket: int, stat: str, top: int,
    groups: dict[bytes, list[int]], seed0_sig: bytes, say: Callable[[str], None],
) -> list[bytes] | None:
    jtype = JEWEL_TYPE_IDS.get(name)
    tk = max(top * 4, _MIN_PRNG_SHORTLIST)
    if jtype is None or len(groups) <= tk:
        return None
    try:
        resp = first.worker.call(
            "timeless_prng_search",
            {"jewel": name, "jewelType": jtype, "socketId": socket, "stat": stat, "top": min(sum(len(v) for v in groups.values()), tk * 4)},
            timeout=60.0,
        )
    except Exception:
        return None
    top_seeds = (resp or {}).get("topSeeds") if isinstance(resp, dict) else None
    if not top_seeds:
        return None
    by_seed = {s: sig for sig, members in groups.items() for s in members}
    selected: list[bytes] = []
    seen: set[bytes] = set()
    for entry in top_seeds:
        if isinstance(entry, dict) and entry.get("seed") is not None:
            sig = by_seed.get(int(entry["seed"]))
            if sig is not None and sig not in seen:
                seen.add(sig)
                selected.append(sig)
                if len(selected) >= tk:
                    break
    if not selected:
        return None
    if seed0_sig in groups and seed0_sig not in seen and len(selected) < tk:
        selected.append(seed0_sig)
    say(f"shortlisted {len(selected)} of {len(groups)} groups with PoB's timeless weight finder; calculating them")
    return selected


def _build_search_outcomes(
    results: dict[bytes, dict[str, Any]], groups: dict[bytes, list[int]],
    stat: str, top: int, estimate: dict[str, Any] | None,
) -> tuple[int | float | str | None, list[dict[str, Any]]]:
    outcomes: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for sig, stats in results.items():
        outcomes[tuple(_json_safe_stat(stats.get(k)) for k in STAT_KEYS)].extend(groups[sig])
    rank = STAT_KEYS.index(stat)
    dps_rank = STAT_KEYS.index("CombinedDPS")
    ranked = sorted(
        outcomes.items(),
        key=lambda kv: (_stat_rank_key(kv[0][rank]), _stat_rank_key(kv[0][dps_rank])),
        reverse=True,
    )
    cutoff: int | float | str | None = None
    if estimate and estimate.get("method") == "weightShortlist":
        ranked = ranked[:top]
    elif estimate and ranked:
        cutoff = ranked[min(top, len(ranked)) - 1][0][rank]
        ck = _stat_rank_key(cutoff)
        ranked = [kv for kv in ranked if _stat_rank_key(kv[0][rank]) >= ck]
    return cutoff, [{"stats": dict(zip(STAT_KEYS, k, strict=False)), "seeds": sorted(m)} for k, m in ranked]


class TimelessSearchEngine:
    """Reusable timeless search worker pool evaluating one or multiple sockets without worker restarts."""

    def __init__(
        self, code: str, xml: str, socket_spec: int | Sequence[int] | str, jewel_name: str, *,
        conqueror: str | None = None, mods: Sequence[str] = (), stat: str = "CombinedDPS", top: int = 15,
        workers: int | None = None, exact: bool = False, progress: Callable[[str], None] | None = None,
        pob_dir: Path | str | None = None, runtime_dir: Path | str | None = None,
    ) -> None:
        import pob_cli.timeless as _tl

        pre_gd, pre_jw, resolved_conq = _prevalidate_search(
            jewel_name, conqueror, mods, stat, socket_spec, xml, pob_dir, runtime_dir
        )
        self.code, self.xml, self.raw_jewel = code, xml, jewel_name
        self.conqueror, self.mods, self.stat = resolved_conq, list(mods), stat
        self.top, self.workers, self.exact = max(1, top), workers, exact
        self.say = progress or (lambda _msg: None)
        self.pob_dir, self.runtime_dir = pob_dir, runtime_dir
        self.engine_kwargs = {k: v for k, v in (("pob_dir", pob_dir), ("runtime_dir", runtime_dir)) if v}
        init_slot = f"Jewel {socket_spec}" if isinstance(socket_spec, int) and not isinstance(socket_spec, bool) else ""
        self.first = _tl.Engine(code, "", init_slot, **self.engine_kwargs)
        self.engines_lock = threading.Lock()
        self.active_engines: list[Engine] = [self.first]
        self.engines: list[Engine | None] = [self.first]
        self.stop = threading.Event()
        self._pre = (pre_gd, pre_jw)
        self._ctx: dict[str, Any] | None = None
        self._runs = 0

    def __enter__(self) -> TimelessSearchEngine:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        seen: set[int] = set()
        with self.engines_lock:
            to_stop = list(self.active_engines) + [e for e in self.engines if e is not None]
        for eng in to_stop:
            if id(eng) not in seen:
                seen.add(id(eng))
                try:
                    eng.stop()
                except Exception:
                    pass

    def _spawn(self, i: int, tpl: str, slot: str, clear: Sequence[str]) -> Engine | None:
        import pob_cli.timeless as _tl

        with self.engines_lock:
            if self.stop.is_set() or self.engines[i] is not None:
                return None if self.stop.is_set() else self.engines[i]
        if self.stop.is_set():
            return None
        eng = _tl.Engine(self.code, tpl, slot, clear, **self.engine_kwargs)
        with self.engines_lock:
            self.active_engines.append(eng)
            abort_now = self.stop.is_set()
            if not abort_now:
                self.engines[i] = eng
        if abort_now:
            for fn in (getattr(getattr(eng, "worker", None), "abort", None), eng.stop):
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            return None
        return eng

    def _run_batch(
        self, sigs: Sequence[bytes], groups: dict[bytes, list[int]], pre: dict[bytes, dict[str, Any]],
        res: dict[bytes, dict[str, Any]], nw: int, tpl: str, slot: str, clear: Sequence[str], lk: threading.Lock,
    ) -> None:
        todo: list[bytes] = []
        for sig in sigs:
            if sig in pre:
                with lk:
                    res[sig] = pre[sig]
            else:
                todo.append(sig)
        if not todo:
            return
        n = min(nw, len(todo))
        with self.engines_lock:
            if n > len(self.engines):
                self.engines.extend([None] * (n - len(self.engines)))
        errs: list[BaseException] = []

        def work(i: int) -> None:
            try:
                eng = None if self.stop.is_set() else self._spawn(i, tpl, slot, clear)
                if eng is None:
                    return
                for sig in todo[i::n]:
                    if self.stop.is_set():
                        return
                    clean = _clean_stats(eng.stats(groups[sig][0]))
                    with lk:
                        res[sig] = clean
                        if len(res) % 1000 == 0:
                            self.say(f"{len(res)} calculated")
            except BaseException as exc:
                with self.engines_lock:
                    was = self.stop.is_set()
                    if not was and not errs:
                        errs.append(exc)
                    self.stop.set()
                _abort_engines(self.engines_lock, self.active_engines, self.stop)
                if not was:
                    raise

        with ThreadPoolExecutor(n) as pool:
            futs = [pool.submit(work, i) for i in range(n)]
            aborted = False
            try:
                while not all(f.done() for f in futs):
                    if self.stop.is_set() and not aborted:
                        aborted = True
                        _abort_engines(self.engines_lock, self.active_engines, self.stop)
                    time.sleep(0.05)
            except BaseException:
                if not aborted:
                    _abort_engines(self.engines_lock, self.active_engines, self.stop)
                raise
            if errs:
                raise errs[0]
            for f in futs:
                f.result()

    def _ensure_ctx(self) -> dict[str, Any]:
        if self._ctx is not None:
            return self._ctx
        self.first.ensure_loaded() if hasattr(self.first, "ensure_loaded") else self.first.reset()
        gd = _resolve_game_dir(self.first.worker, pob_dir=self.pob_dir, runtime_dir=self.runtime_dir)
        pre_gd, pre_jw = self._pre
        jewels = pre_jw if (pre_jw is not None and pre_gd == gd) else load_jewels(gd)
        state, dialog, _ = _worker_batch(
            self.first.worker, [("get_tree_state", {}), ("tj_open", {}), ("tj_close", {})], timeout=60.0
        )
        name = _resolve_jewel(self.raw_jewel, jewels, self.first, dialog=dialog)
        jewel = jewels[name]
        self.conqueror = _resolve_conqueror(self.conqueror, jewel, name)
        raw_ids = (dialog or {}).get("socketIds") or []
        order, labels = [], {}
        for rn, opt in zip(raw_ids, ((dialog or {}).get("socket") or {}).get("options") or [], strict=False):
            if isinstance(rn, (int, float)) and int(rn) > 0:
                nid = int(rn)
                order.append(nid)
                if isinstance(opt, dict) and (opt.get("labelZh") or opt.get("label")):
                    labels[nid] = str(opt.get("labelZh") or opt.get("label"))
        self._ctx = {
            "gd": gd, "jewels": jewels, "name": name, "jewel": jewel,
            "tpl": jewel_text(jewel, name, self.conqueror, self.mods),
            "alloc": {int(x) for x in ((state or {}).get("allocatedNodes") or [])},
            "sockets": {int(s["nodeId"]): s for s in ((state or {}).get("sockets") or [])},
            "valid_tj": {int(x) for x in raw_ids if isinstance(x, (int, float)) and int(x) > 0},
            "order": order, "labels": labels,
        }
        return self._ctx

    def _check_socket(self, ctx: dict[str, Any], socket: int) -> None:
        if socket not in ctx["sockets"] or socket not in ctx["alloc"]:
            raise ValueError(f"Node {socket} is not an allocated jewel socket of this build (see `pob timeless list`).")
        if ctx["sockets"][socket].get("charm"):
            raise ValueError(f"Node {socket} is a charm socket, not a timeless jewel socket (see `pob timeless list`).")
        if ctx["valid_tj"] and socket not in ctx["valid_tj"]:
            raise ValueError(f"Node {socket} is not a valid timeless jewel socket (see `pob timeless list`).")

    def resolve_sockets(self, socket_spec: int | Sequence[int] | str) -> list[int]:
        ctx = self._ensure_ctx()
        if isinstance(socket_spec, str) and socket_spec.strip().lower() == "all":
            out = [
                n for n in dict.fromkeys(ctx["order"] or sorted(ctx["sockets"]))
                if n in ctx["sockets"] and n in ctx["alloc"] and not ctx["sockets"][n].get("charm")
                and (not ctx["valid_tj"] or n in ctx["valid_tj"])
            ]
            if not out:
                raise ValueError("No allocated timeless jewel sockets found on this build (see `pob timeless list`).")
            return out
        raw = [socket_spec] if isinstance(socket_spec, int) and not isinstance(socket_spec, bool) else [int(s) for s in socket_spec]
        deduped = list(dict.fromkeys(raw))
        for nid in deduped:
            self._check_socket(ctx, nid)
        return deduped

    def _retarget(self, slot: str, clear: list[str], tpl: str) -> None:
        self.stop.clear()
        if self._runs == 0:
            self.first.slot, self.first.clear, self.first.template = slot, clear, tpl
            return
        with self.engines_lock:
            live = [e for e in self.engines if e is not None]
        for eng in live:
            eng.slot, eng.clear, eng.template = slot, list(clear), tpl
            if hasattr(eng, "_loaded"):
                eng._loaded, eng._cleared_slots, eng.count = False, (), 0
            else:
                eng.reset()
        self.first.reset()

    def search_socket(self, socket: int) -> dict[str, Any]:
        started = time.time()
        ctx = self._ensure_ctx()
        self._check_socket(ctx, socket)
        name, jewel, gd, tpl = ctx["name"], ctx["jewel"], ctx["gd"], ctx["tpl"]
        removed = [
            {"jewel": s.get("title") or s.get("name") or s.get("baseName") or "Timeless Jewel", "socket": node}
            for node, s in ctx["sockets"].items()
            if node != socket and node in ctx["alloc"] and _is_historic_socket_jewel(s, ctx["jewels"])
        ]
        slot, clear = f"Jewel {socket}", [f"Jewel {r['socket']}" for r in removed]
        self._retarget(slot, clear, tpl)
        self._runs += 1

        seeds = list(jewel["seeds"])
        baseline = _clean_stats(self.first.read())
        first_stats = _clean_stats(self.first.stats(seeds[0]))
        post_state = self.first.worker.call("get_tree_state", {}, timeout=60)
        radius_summary = _build_radius_summary(self.first, socket, jewel, post_state, self.xml, self.code, gd)
        groups, nodes, additive, seed0_sig, used_prng = _resolve_seed_groups(
            self.first, name, socket, seeds, self.xml, self.code, gd, self.runtime_dir, state=post_state
        )

        pre = {seed0_sig: first_stats} if seed0_sig in groups and groups[seed0_sig][0] == seeds[0] else {}
        shortlist = (
            _shortlist_non_additive_groups(self.first, name, socket, self.stat, self.top, groups, seed0_sig, self.say)
            if used_prng and not additive and not self.exact else None
        )
        target_n = len(shortlist) if shortlist is not None else len(groups)
        one = used_prng and shortlist is None and ((additive and not self.exact) or len(groups) <= _MIN_PRNG_SHORTLIST)
        nw = max(1, min(self.workers if self.workers is not None else (1 if one else default_workers()), max(1, target_n)))
        results: dict[bytes, dict[str, Any]] = {}
        lk = threading.Lock()

        def calc(sigs: Sequence[bytes]) -> None:
            self._run_batch(sigs, groups, pre, results, nw, tpl, slot, clear, lk)

        rng = random.Random(1)
        all_sigs = list(groups)
        estimate: dict[str, Any] | None = None
        if shortlist is not None:
            calc(shortlist)
            estimate = {"method": "weightShortlist", "calculated": len(results), "shortlisted": len(shortlist), "groupsTotal": len(groups), "maxError": None}
        elif self.exact or not nodes or not additive:
            calc(all_sigs)
        else:
            estimate = narrow_down(all_sigs, results, calc, self.stat, self.top, rng, self.say)

        multi = [sig for sig in results if len(groups[sig]) > 1]
        picks = rng.sample(multi, min(SELF_CHECK, len(multi)))
        mismatches = [
            other for sig in picks for other in (rng.choice(groups[sig][1:]),)
            if _clean_stats(self.first.stats(other)) != results[sig]
        ]
        cutoff, outcomes = _build_search_outcomes(results, groups, self.stat, self.top, estimate)
        return {
            "cutoff": cutoff, "jewel": name, "conqueror": self.conqueror, "socket": socket, "mods": list(self.mods),
            "removed": removed, "stat": self.stat, "seedCount": len(seeds), "groupCount": len(groups),
            "calculated": len(results), "estimate": estimate, "seedNodes": nodes, "radiusSummary": radius_summary,
            "workers": nw, "baseline": baseline, "outcomes": outcomes,
            "selfCheck": {"checked": len(picks), "mismatches": mismatches}, "seconds": round(time.time() - started, 1),
        }

    def search_sockets(self, socket_spec: Sequence[int] | str) -> dict[str, Any]:
        started = time.time()
        ctx = self._ensure_ctx()
        targets = self.resolve_sockets(socket_spec)
        s_results = [self.search_socket(sid) for sid in targets]
        cands: list[dict[str, Any]] = []
        for sres in s_results:
            sid = int(sres["socket"])
            lbl = ctx["labels"].get(sid)
            for out in sres.get("outcomes") or []:
                slist = list(out.get("seeds") or [])
                item: dict[str, Any] = {"socket": sid, "stats": out.get("stats") or {}, "seeds": slist, "seed": slist[0] if slist else None}
                if lbl:
                    item["socketLabel"] = lbl
                cands.append(item)
        cands.sort(key=lambda it: (_stat_rank_key((it.get("stats") or {}).get(self.stat)), _stat_rank_key((it.get("stats") or {}).get("CombinedDPS")), -int(it["socket"])), reverse=True)
        return {
            "jewel": ctx["name"], "conqueror": self.conqueror, "sockets": targets, "mods": list(self.mods), "stat": self.stat,
            "workers": max((int(r.get("workers") or 1) for r in s_results), default=1),
            "baseline": s_results[0].get("baseline") if s_results else {},
            "socketResults": s_results, "topSeeds": cands[: self.top],
            "calculatedTotal": sum(int(r.get("calculated") or 0) for r in s_results),
            "seconds": round(time.time() - started, 1),
        }


def search(
    code: str, xml: str, socket: int | Sequence[int] | str, jewel_name: str, *,
    conqueror: str | None = None, mods: Sequence[str] = (), stat: str = "CombinedDPS", top: int = 15,
    workers: int | None = None, exact: bool = False, progress: Callable[[str], None] | None = None,
    pob_dir: Path | str | None = None, runtime_dir: Path | str | None = None,
) -> dict[str, Any]:
    with TimelessSearchEngine(
        code, xml, socket, jewel_name, conqueror=conqueror, mods=mods, stat=stat,
        top=top, workers=workers, exact=exact, progress=progress, pob_dir=pob_dir, runtime_dir=runtime_dir,
    ) as runner:
        return runner.search_socket(socket) if isinstance(socket, int) and not isinstance(socket, bool) else runner.search_sockets(socket)
