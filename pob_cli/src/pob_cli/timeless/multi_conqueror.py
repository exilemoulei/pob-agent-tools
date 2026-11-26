"""Multi-conqueror PoB evaluation helper for `pob timeless search --conqueror any`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .engine import search as _default_search
from .lut import _stat_rank_key
from .trade_data import resolve_conqueror_stat_ids


def _tag_outcomes(outcomes: list[dict[str, Any]], conqueror: str) -> list[dict[str, Any]]:
    return [{**o, "conqueror": conqueror} for o in outcomes if isinstance(o, dict)]


def _outcome_sort_key(item: dict[str, Any], stat: str) -> tuple[tuple[int, float], tuple[int, float]]:
    stats = item.get("stats") or {}
    return (_stat_rank_key(stats.get(stat)), _stat_rank_key(stats.get("CombinedDPS")))


def _merge_single_socket(
    per_conq: list[tuple[str, dict[str, Any]]],
    *,
    stat: str,
    top: int,
) -> dict[str, Any]:
    merged_outcomes: list[dict[str, Any]] = []
    for conq, res in per_conq:
        merged_outcomes.extend(_tag_outcomes(list(res.get("outcomes") or []), conq))
    merged_outcomes.sort(key=lambda o: _outcome_sort_key(o, stat), reverse=True)
    first = per_conq[0][1]
    return {
        **first,
        "conqueror": "any",
        "conquerorsEvaluated": [c for c, _ in per_conq],
        "calculated": sum(int(r.get("calculated") or 0) for _, r in per_conq),
        "seconds": round(sum(float(r.get("seconds") or 0.0) for _, r in per_conq), 1),
        "outcomes": merged_outcomes[:top],
    }


def _merge_multi_socket(
    per_conq: list[tuple[str, dict[str, Any]]],
    *,
    stat: str,
    top: int,
) -> dict[str, Any]:
    merged_top: list[dict[str, Any]] = []
    by_socket: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    socket_order: list[int] = []
    for conq, res in per_conq:
        merged_top.extend(_tag_outcomes(list(res.get("topSeeds") or []), conq))
        for sres in res.get("socketResults") or []:
            if not isinstance(sres, dict) or sres.get("socket") is None:
                continue
            sid = int(sres["socket"])
            if sid not in by_socket:
                by_socket[sid] = []
                socket_order.append(sid)
            by_socket[sid].append((conq, sres))

    merged_top.sort(
        key=lambda it: (*_outcome_sort_key(it, stat), -int(it.get("socket") or 0)),
        reverse=True,
    )
    merged_socket_results = [
        _merge_single_socket(by_socket[sid], stat=stat, top=top)
        for sid in socket_order
    ]
    first = per_conq[0][1]
    return {
        **first,
        "conqueror": "any",
        "conquerorsEvaluated": [c for c, _ in per_conq],
        "socketResults": merged_socket_results,
        "topSeeds": merged_top[:top],
        "calculatedTotal": sum(int(r.get("calculatedTotal") or 0) for _, r in per_conq),
        "seconds": round(sum(float(r.get("seconds") or 0.0) for _, r in per_conq), 1),
    }


def search_multi_conqueror(
    code: str,
    xml: str,
    socket: int | Sequence[int] | str,
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
    search_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run PoB timeless search for one conqueror, or genuinely evaluate every conqueror when `conqueror='any'`."""
    runner = search_fn or _default_search
    is_any = bool(conqueror and str(conqueror).strip().lower() in ("any", "*"))
    if not is_any:
        return runner(
            code, xml, socket, jewel_name, conqueror=conqueror, mods=mods, stat=stat,
            top=top, workers=workers, exact=exact, progress=progress, pob_dir=pob_dir, runtime_dir=runtime_dir,
        )

    conquerors, _ = resolve_conqueror_stat_ids(jewel_name, "any")
    if len(conquerors) <= 1:
        single_conq = conquerors[0] if conquerors else None
        res = runner(
            code, xml, socket, jewel_name, conqueror=single_conq, mods=mods, stat=stat,
            top=top, workers=workers, exact=exact, progress=progress, pob_dir=pob_dir, runtime_dir=runtime_dir,
        )
        res["conquerorsEvaluated"] = list(conquerors)
        return res

    say = progress or (lambda _msg: None)
    per_conq: list[tuple[str, dict[str, Any]]] = []
    for idx, conq in enumerate(conquerors, start=1):
        say(f"evaluating conqueror {idx}/{len(conquerors)}: {conq}")
        res = runner(
            code, xml, socket, jewel_name, conqueror=conq, mods=mods, stat=stat,
            top=top, workers=workers, exact=exact, progress=progress, pob_dir=pob_dir, runtime_dir=runtime_dir,
        )
        per_conq.append((conq, res))

    if "socketResults" in per_conq[0][1]:
        return _merge_multi_socket(per_conq, stat=stat, top=top)
    return _merge_single_socket(per_conq, stat=stat, top=top)
