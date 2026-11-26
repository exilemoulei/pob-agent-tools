"""Request shape, CLI overlay and complexity budget for PoB-generated (`--auto-query`) trade searches."""

from __future__ import annotations

from typing import Any

from .aliases import INDEXED_WINDOWS, OPEN_AFFIX_ID, OPEN_PREFIX_ID, OPEN_SUFFIX_ID

# The site rejects queries above 200; PoB's generator budgets against that, this keeps headroom.
COMPLEXITY_LIMIT = 160
# PoB TradeQueryGenerator's accounting: 54 for the weighted group, 4 for rarity + category,
# 4 for the "and" group, 4 per stat filter and 3 per other filter field.
_BASE_COST = 54 + 4 + 4
_WEIGHT_GROUP_COST = 54
_STAT_COST = 4
_FIELD_COST = 3
_FREE_TYPE_FIELDS = frozenset({"category", "rarity"})
# PoB's own "Find best" search sorts the weighted group first (TradeQuery.lua).
_DEFAULT_SORT = {"statgroup.0": "desc"}


def wrap_auto_query(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return a ``{"query": ..., "sort": ...}`` search body, as ``build_query`` does.

    PoB hands its requester the bare query table and adds the sort only when it searches,
    so a bare table is wrapped here, once.
    """
    body = dict(parsed) if isinstance(parsed.get("query"), dict) else {"query": parsed}
    if not isinstance(body.get("sort"), dict):
        body["sort"] = dict(_DEFAULT_SORT)
    return body


def apply_cli_filters_to_auto_query(
    body: dict[str, Any],
    *,
    status: str = "available",
    budget: float | None = None,
    currency: str | None = "chaos_equiv",
    max_age: float | None = None,
    ilvl_min: int = 0,
    open_prefix: bool = False,
    open_suffix: bool = False,
    open_affix: bool = False,
    category: str | None = None,
    search_sort: str | None = None,
    allow_corrupted: bool | None = None,
    extra_stat_groups: list[dict[str, Any]] | None = None,
    name: str | None = None,
    item_type: str | None = None,
    corrupted: str | None = None,
) -> dict[str, Any]:
    """Overlay CLI price/status/category/name/type/corrupted/sort/stat/open-affix constraints onto a wrapped auto query."""
    inner = body.setdefault("query", {})
    if status:
        inner["status"] = {"option": status.strip().lower()}
    if name and name.strip():
        inner["name"] = name.strip()
    if item_type and item_type.strip():
        inner["type"] = item_type.strip()
    filters = inner.setdefault("filters", {})
    if category:
        filters.setdefault("type_filters", {}).setdefault("filters", {})["category"] = {"option": category}
    if ilvl_min > 0:
        filters.setdefault("type_filters", {}).setdefault("filters", {})["ilvl"] = {"min": ilvl_min}
    cor_norm = (corrupted or "").strip().lower()
    if cor_norm in ("yes", "true", "1"):
        filters.setdefault("misc_filters", {}).setdefault("filters", {})["corrupted"] = {"option": "true"}
    elif cor_norm in ("no", "false", "0"):
        filters.setdefault("misc_filters", {}).setdefault("filters", {})["corrupted"] = {"option": "false"}
    elif cor_norm == "any":
        misc_blk = filters.get("misc_filters")
        if isinstance(misc_blk, dict) and isinstance(misc_blk.get("filters"), dict):
            misc_blk["filters"].pop("corrupted", None)
            if not misc_blk["filters"]:
                filters.pop("misc_filters", None)
    elif allow_corrupted is False:
        filters.setdefault("misc_filters", {}).setdefault("filters", {}).setdefault("corrupted", {"option": "false"})
    trade_f: dict[str, Any] = {}
    if budget is not None and budget > 0:
        curr_norm = (currency or "").strip().lower()
        trade_f["price"] = {"max": budget} if curr_norm in ("", "chaos_equiv", "equiv") else {"option": curr_norm, "max": budget}
    if max_age is not None and max_age > 0:
        window = next((opt for hours, opt in INDEXED_WINDOWS if max_age <= hours), None)
        if window:
            trade_f["indexed"] = {"option": window}
    if trade_f:
        filters.setdefault("trade_filters", {}).setdefault("filters", {}).update(trade_f)
    if extra_stat_groups:
        inner.setdefault("stats", []).extend(extra_stat_groups)
    pseudo: list[dict[str, Any]] = []
    if open_prefix:
        pseudo.append({"id": OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False})
    if open_suffix:
        pseudo.append({"id": OPEN_SUFFIX_ID, "value": {"min": 1}, "disabled": False})
    if open_affix and not (open_prefix or open_suffix):
        pseudo.append({"id": OPEN_AFFIX_ID, "value": {"min": 1}, "disabled": False})
    if pseudo:
        inner.setdefault("stats", []).append({"type": "and", "filters": pseudo})
    s_norm = (search_sort or "").strip().lower()
    if s_norm in ("asc", "desc"):
        body["sort"] = {"price": s_norm}
    elif s_norm in ("sum", "weight"):
        body["sort"] = dict(_DEFAULT_SORT)
    return body


def _live_filters(group: Any) -> list[dict[str, Any]]:
    if not isinstance(group, dict):
        return []
    return [f for f in group.get("filters") or [] if isinstance(f, dict) and not f.get("disabled")]


def query_complexity(body: dict[str, Any]) -> int:
    """Estimate the trade site's complexity of a search body with PoB's accounting."""
    inner = body.get("query") if isinstance(body.get("query"), dict) else body
    total = _BASE_COST
    for idx, group in enumerate(inner.get("stats") or []):
        total += _STAT_COST * len(_live_filters(group))
        if idx > 0 and isinstance(group, dict) and group.get("type") == "weight":
            total += _WEIGHT_GROUP_COST
    for section, block in (inner.get("filters") or {}).items():
        fields = block.get("filters") if isinstance(block, dict) else None
        for name in fields or {}:
            if not (section == "type_filters" and name in _FREE_TYPE_FIELDS):
                total += _FIELD_COST
    return total


def _abs_weight(entry: dict[str, Any]) -> float:
    try:
        return abs(float((entry.get("value") or {}).get("weight") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def fit_complexity(body: dict[str, Any], limit: int = COMPLEXITY_LIMIT) -> tuple[int, list[str]]:
    """Drop the lowest-weight generated filters until the whole body fits ``limit``.

    Only the generated weighted group (``stats[0]``) is trimmed: the CLI's own filters are
    what the user asked for. Returns (final complexity, dropped stat ids).
    """
    inner = body.get("query") if isinstance(body.get("query"), dict) else body
    stats = inner.get("stats") or []
    group = stats[0] if stats and isinstance(stats[0], dict) and stats[0].get("type") == "weight" else None
    complexity = query_complexity(body)
    if group is None or complexity <= limit:
        return complexity, []
    filters = group.get("filters") or []
    before = sum(_abs_weight(f) for f in filters if isinstance(f, dict))
    dropped: list[str] = []
    while complexity > limit and len(filters) > 1:
        # Lowest |weight| goes first; on ties the later (lower-impact) entry.
        idx = min(range(len(filters)), key=lambda i: (_abs_weight(filters[i]), -i))
        dropped.append(str(filters.pop(idx).get("id") or ""))
        complexity = query_complexity(body)
    value = group.get("value")
    if dropped and isinstance(value, dict) and isinstance(value.get("min"), (int, float)) and before > 0:
        # Fewer filters can reach a smaller weighted sum; lower the floor in proportion, as
        # upstream_kernels.lua does when it trims, but never below a quarter.
        kept = sum(_abs_weight(f) for f in filters if isinstance(f, dict))
        value["min"] = value["min"] * min(1.0, max(0.25, kept / before))
    return complexity, dropped


def finalize_auto_query(auto: dict[str, Any], limit: int = COMPLEXITY_LIMIT) -> dict[str, Any]:
    """Fit ``auto["query"]`` to the budget and make its metadata describe the final body."""
    body = auto["query"]
    complexity, dropped = fit_complexity(body, limit)
    if dropped:
        gone = set(dropped)
        auto["topFilters"] = [f for f in auto.get("topFilters") or [] if f.get("id") not in gone]
        labels = auto.get("statLabels")
        if isinstance(labels, dict):
            for fid in gone:
                labels.pop(fid, None)
    first = (body["query"].get("stats") or [None])[0]
    if isinstance(first, dict) and first.get("type") == "weight":
        auto["filters"] = len(_live_filters(first))
        if isinstance(first.get("value"), dict) and first["value"].get("min") is not None:
            auto["minWeight"] = first["value"]["min"]
    auto["complexity"] = complexity
    auto["complexityWithinLimit"] = complexity <= limit
    if dropped:
        auto["droppedFilters"] = [*(auto.get("droppedFilters") or []), *dropped]
    return auto

