"""PoB what-if verification (`verify_candidate`, `verify_candidates_batch`, `eval_trade_items`)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..errors import BootstrapMissingError
from ..game_data import fold_han
from .scoring import (
    DEFAULT_WEIGHTS as DEFAULT_WEIGHTS,
)
from .scoring import (
    MIN_SURROGATE_PREFILTER_K as MIN_SURROGATE_PREFILTER_K,
)
from .scoring import (
    POB_EHP_WEIGHT as POB_EHP_WEIGHT,
)
from .scoring import (
    PREFERRED_BONUS as PREFERRED_BONUS,
)
from .scoring import (
    Candidate as Candidate,
)
from .scoring import (
    ModDelta as ModDelta,
)
from .scoring import (
    PobEval as PobEval,
)
from .scoring import (
    _finite_float as _finite_float,
)
from .scoring import (
    _finite_sort_key as _finite_sort_key,
)
from .scoring import (
    _is_pos_inf as _is_pos_inf,
)
from .scoring import (
    _json_safe_optional_float as _json_safe_optional_float,
)
from .scoring import (
    _json_safe_stat as _json_safe_stat,
)
from .scoring import (
    _matches_preferred as _matches_preferred,
)
from .scoring import (
    _safe_pct as _safe_pct,
)
from .scoring import (
    compare_items as compare_items,
)
from .scoring import (
    compute_grad_weights as compute_grad_weights,
)
from .scoring import (
    deltas_for_example as deltas_for_example,
)
from .scoring import (
    parse_weights as parse_weights,
)
from .scoring import (
    preferred_templates as preferred_templates,
)
from .scoring import (
    prefilter_candidates_with_surrogate as prefilter_candidates_with_surrogate,
)
from .scoring import (
    score_candidate_surrogate as score_candidate_surrogate,
)
from .scoring import (
    score_deltas as score_deltas,
)

# ---------------------------------------------------------------------------
# PoB verification (single RPC batch + legacy `item_try` fallback)
# ---------------------------------------------------------------------------


def _pob_eval_from_snapshot(
    before: dict[str, Any],
    after: dict[str, Any],
    diff: dict[str, Any],
) -> PobEval:
    """Build an `inf`/`NaN`-safe :class:`PobEval` from `trade_eval_items` or `item_try` dicts."""
    before = before or {}
    after = after or {}
    diff = diff or {}
    dps_key = str(after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS")
    return PobEval(
        dps_key=dps_key,
        dps_before=_json_safe_stat(before.get("primaryDps")),
        dps_after=_json_safe_stat(after.get("primaryDps")),
        ehp_before=_json_safe_stat(before.get("totalEhp")),
        ehp_after=_json_safe_stat(after.get("totalEhp")),
        life_before=_finite_float(before.get("life")),
        life_after=_finite_float(after.get("life")),
        warnings=tuple(str(item) for item in (diff.get("newWarnings") or [])[:8]),
        resolved_warnings=tuple(str(item) for item in (diff.get("resolvedWarnings") or [])[:8]),
    )


def verify_candidate(
    candidate: Candidate,
    *,
    slot: str,
    target: str = "main",
    daemon_call: Callable[..., dict[str, Any]] | None = None,
) -> PobEval:
    """Evaluate a single candidate via ``item_try`` (what-if, never applied)."""
    if daemon_call is None:
        raise ValueError("daemon_call callback is required for PoB verification.")
    payload = daemon_call(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": candidate.item_text,
            "tooltip_only": False,
            "apply": False,
        },
    )
    mutation = payload.get("mutation") or {}
    return _pob_eval_from_snapshot(
        mutation.get("before") or {},
        mutation.get("after") or {},
        mutation.get("diff") or {},
    )


def verify_candidates_batch(
    candidates: list[Candidate],
    *,
    slot: str,
    target: str = "main",
    limit: int = 5,
    daemon_call: Callable[..., dict[str, Any]] | None = None,
) -> None:
    """Verify up to ``limit`` candidates in ONE ``trade_eval_items`` daemon RPC.

    Falls back sequentially to ``item_try`` if an older running daemon responds with
    ``Unknown daemon action``.
    """
    if limit <= 0 or not candidates:
        return
    if daemon_call is None:
        raise ValueError("daemon_call callback is required for PoB verification.")

    subset = candidates[:limit]
    items_payload = [{"id": idx, "raw": candidate.item_text} for idx, candidate in enumerate(subset)]

    try:
        resp = daemon_call(
            "trade_eval_items",
            {
                "target": target,
                "slot": slot,
                "items": items_payload,
            },
        )
    except BootstrapMissingError:
        raise
    except Exception as exc:
        msg = str(exc)
        if "unknown daemon action" in msg.lower():
            for candidate in subset:
                try:
                    candidate.pob = verify_candidate(
                        candidate,
                        slot=slot,
                        target=target,
                        daemon_call=daemon_call,
                    )
                    candidate.pob_error = None
                except BootstrapMissingError:
                    raise
                except Exception as item_exc:
                    candidate.pob = None
                    candidate.pob_error = str(item_exc)
            return
        for candidate in subset:
            candidate.pob = None
            candidate.pob_error = msg
        return

    default_before = resp.get("before") or {}
    seen: set[int] = set()
    for pos, entry in enumerate(resp.get("results") or []):
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        idx = raw_id if isinstance(raw_id, int) else pos
        if not (0 <= idx < len(subset)):
            continue
        seen.add(idx)
        candidate = subset[idx]
        if entry.get("ok"):
            candidate.pob = _pob_eval_from_snapshot(
                entry.get("before") or default_before,
                entry.get("after") or {},
                entry.get("diff") or {},
            )
            candidate.pob_error = None
        else:
            candidate.pob = None
            candidate.pob_error = str(entry.get("error") or "PoB evaluation failed.")

    for idx, candidate in enumerate(subset):
        if idx not in seen:
            candidate.pob = None
            candidate.pob_error = "Missing PoB evaluation result."


# ---------------------------------------------------------------------------
# Fast in-place worker batch evaluation (`eval_trade_items`)
# ---------------------------------------------------------------------------

_TWO_HANDED_ITEM_TYPES = frozenset({
    "Bow",
    "Staff",
    "Warstaff",
    "Two Handed Sword",
    "Two Handed Axe",
    "Two Handed Mace",
    "Fishing Rod",
})

_TWO_HANDED_RAW_RE = re.compile(
    r"(?:item\s+class\s*:\s*(?:bows?|staves|warstaves|two\s+hand|fishing\s+rods?)"
    r"|^\s*(?:two\s+handed|warstaff|staff|bow|fishing\s+rod)\b)",
    re.IGNORECASE | re.MULTILINE,
)

_COMPLEX_TRADE_RAW_RE = re.compile(
    r"(?:grants\s+level\s+\d+|grants\s+[^\n]*\bskill\b|triggers?\s+level\s+\d+|has\s+\d+\s+abyssal\s+socket"
    r"|\bexplode\b"
    r"|cluster\s+jewel|timeless\s+jewel|thread\s+of\s+hope|impossible\s+escape"
    r"|unnatural\s+instinct|intuitive\s+leap|militant\s+faith|lethal\s+pride"
    r"|brutal\s+restraint|elegant\s+hubris|glorious\s+vanity"
    r"|赋予|触发|深渊插槽|星团珠宝|永恒珠宝|希望之线|不可能的逃脱|超自然本能|直觉之跃|爆炸)",
    re.IGNORECASE,
)

_WEAPON_SLOTS = frozenset({"Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap"})
_PARTNER_WEAPON_SLOT: dict[str, str] = {
    "Weapon 1": "Weapon 2",
    "Weapon 2": "Weapon 1",
    "Weapon 1 Swap": "Weapon 2 Swap",
    "Weapon 2 Swap": "Weapon 1 Swap",
}


def _is_complex_trade_item(raw_text: str, slot_name: str | None) -> bool:
    """Return True if raw item text could mutate 2H/offhand slots, granted skill groups, abyssal sockets, or tree radius."""
    if _COMPLEX_TRADE_RAW_RE.search(fold_han(raw_text)):  # folded: the Chinese keywords are Simplified
        return True
    if slot_name in _WEAPON_SLOTS and _TWO_HANDED_RAW_RE.search(raw_text):
        return True
    return False


def _digest_from_direct_calc(worker: Any, before_digest: dict[str, Any]) -> dict[str, Any]:
    """Capture fresh stats, passive points, and warnings via `capture_direct_digest` (C1)."""
    from ..snapshot import capture_direct_digest

    return capture_direct_digest(worker, base_digest=before_digest)


def _prefilter_raw_items_for_eval(
    worker: Any,
    items: list[dict[str, Any]],
    prefilter_k: int | None,
) -> list[dict[str, Any]]:
    """Enforce `K >= 50` surrogate pre-filtering before exact PoB evaluation when requested."""
    if prefilter_k is None:
        return items
    effective_k = max(MIN_SURROGATE_PREFILTER_K, int(prefilter_k))
    if len(items) <= effective_k:
        return items
    grad = compute_grad_weights(worker)
    if not grad:
        return items
    from ..trade_item import parse_item_text

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for idx, entry in enumerate(items):
        raw_clean = str(entry.get("raw") or "").strip()
        if not raw_clean:
            scored.append((-1e30, idx, entry))
            continue
        try:
            parsed = parse_item_text(raw_clean)
            sc = score_candidate_surrogate(parsed, grad)
        except Exception:
            sc = 0.0
        scored.append((sc, idx, entry))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:effective_k]]


def _eval_trade_items_cow(
    worker: Any,
    *,
    resolved_slot: str | None,
    items: list[dict[str, Any]],
    before_summary: dict[str, Any],
    digest_before: dict[str, Any],
    use_direct: bool,
) -> list[dict[str, Any]]:
    """Evaluate candidates using `cow_snapshot`/`cow_restore` (C2c) and optional `direct_calc` (C1)."""
    from ..snapshot import capture_digest, diff_digest

    orig_proc = getattr(worker, "_proc", None)
    fallback_code: str | None = getattr(worker, "last_good_code", None)
    snap_id: str | None = str(worker.call("cow_snapshot", {})["snapId"])
    results: list[dict[str, Any]] = []
    total_items = len(items)

    def _restore_base(*, drop: bool) -> None:
        nonlocal snap_id, orig_proc
        cur_proc = getattr(worker, "_proc", None)
        if snap_id is not None and cur_proc is orig_proc:
            try:
                worker.call("cow_restore", {"snapId": snap_id, "drop": drop})
                if drop:
                    snap_id = None
                return
            except Exception:
                snap_id = None
        snap_id = None
        if fallback_code is not None:
            worker.call("import_code", {"code": fallback_code, "mode": "replace"})
            if not drop and (getattr(worker, "caps", None) or {}).get("cowSnapshot"):
                try:
                    orig_proc = getattr(worker, "_proc", None)
                    snap_id = str(worker.call("cow_snapshot", {})["snapId"])
                except Exception:
                    snap_id = None

    try:
        for pos, entry in enumerate(items):
            item_id = entry.get("id")
            raw_clean = str(entry.get("raw") or "").strip()
            if not raw_clean:
                results.append({"id": item_id, "ok": False, "error": "Empty item text."})
                continue
            try:
                add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
                if resolved_slot:
                    add_params["slotName"] = resolved_slot
                worker.call("add_item", add_params)
                digest_after = (
                    _digest_from_direct_calc(worker, digest_before)
                    if use_direct
                    else capture_digest(worker)
                )
                diff = diff_digest(digest_before, digest_after)
                results.append(
                    {
                        "id": item_id,
                        "ok": True,
                        "before": before_summary,
                        "after": {
                            "primaryDpsKey": digest_after["primaryDpsKey"],
                            "primaryDps": digest_after["primaryDps"],
                            "totalEhp": digest_after["totalEhp"],
                            "life": digest_after["life"],
                            "points": digest_after.get("buildInfo", {}).get("points", {}),
                        },
                        "diff": diff,
                    }
                )
            except Exception as exc:
                results.append({"id": item_id, "ok": False, "error": str(exc)})
            if pos < total_items - 1:
                _restore_base(drop=False)
    finally:
        _restore_base(drop=True)
    return results


def eval_trade_items(
    worker: Any,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
    prefilter_k: int | None = None,
) -> dict[str, Any]:
    """Read-only batch what-if evaluation of candidate items against a single build snapshot.

    Uses `cow_snapshot`/`cow_restore` (+ `direct_calc`) when advertised in `worker.caps`;
    otherwise keeps the fast in-place swap and `import_code` fallback path.
    """
    if not items:
        return {"slotName": slot_name, "before": None, "results": []}

    from ..analyzer import resolve_slot_name
    from ..snapshot import _call_batch, capture_digest, diff_digest

    caps = getattr(worker, "caps", None) or {}
    use_cow = bool(caps.get("cowSnapshot"))
    use_direct = bool(caps.get("directCalc"))

    with worker.lock:
        eval_items = _prefilter_raw_items_for_eval(worker, items, prefilter_k)
        prev_last_good = getattr(worker, "last_good_code", None)
        if use_cow:
            items_res = worker.call("list_items", {})
            resolved_slot = resolve_slot_name(worker, slot_name, items_res=items_res) if slot_name else None
            digest_before = capture_digest(worker)
            before_summary = {
                "primaryDpsKey": digest_before["primaryDpsKey"],
                "primaryDps": digest_before["primaryDps"],
                "totalEhp": digest_before["totalEhp"],
                "life": digest_before["life"],
                "points": digest_before.get("buildInfo", {}).get("points", {}),
            }
            try:
                results = _eval_trade_items_cow(
                    worker,
                    resolved_slot=resolved_slot,
                    items=eval_items,
                    before_summary=before_summary,
                    digest_before=digest_before,
                    use_direct=use_direct,
                )
            finally:
                if hasattr(worker, "last_good_code"):
                    worker.last_good_code = prev_last_good
            return {
                "slotName": resolved_slot or slot_name,
                "before": before_summary,
                "results": results,
            }

        items_res, export_res, skills_res = _call_batch(
            worker,
            [
                ("list_items", {}),
                ("export_code", {}),
                ("list_skills", {}),
            ],
        )
        resolved_slot = resolve_slot_name(worker, slot_name, items_res=items_res) if slot_name else None
        snap_before = export_res["code"]
        digest_before = capture_digest(worker)
        before_summary = {
            "primaryDpsKey": digest_before["primaryDpsKey"],
            "primaryDps": digest_before["primaryDps"],
            "totalEhp": digest_before["totalEhp"],
            "life": digest_before["life"],
            "points": digest_before.get("buildInfo", {}).get("points", {}),
        }

        slots = items_res.get("slots", []) if isinstance(items_res, dict) else []
        items_by_id = {it["id"]: it for it in (items_res.get("items", []) if isinstance(items_res, dict) else []) if "id" in it}
        prev_slot = next((s for s in slots if s.get("name") == resolved_slot), None) if resolved_slot else None
        raw_prev_id = prev_slot.get("selItemId") if isinstance(prev_slot, dict) else None
        prev_item_id = int(raw_prev_id) if raw_prev_id else None
        prev_item = items_by_id.get(prev_item_id) if prev_item_id else None

        prev_item_type = str(prev_item.get("type") or "") if prev_item else ""
        prev_is_2h = bool(prev_item_type in _TWO_HANDED_ITEM_TYPES)
        prev_is_jewel_socket = bool(prev_slot and prev_slot.get("nodeId") is not None)
        prev_has_granted_skill = any(
            bool(g.get("source")) and g.get("slot") in (None, "", resolved_slot)
            for g in (skills_res.get("groups", []) if isinstance(skills_res, dict) else [])
        )
        prev_has_abyssal = any(
            not s.get("inactive")
            and str(s.get("name") or "").startswith(f"{resolved_slot} Abyssal Socket")
            for s in slots
        ) if resolved_slot else False
        partner_slot_name = _PARTNER_WEAPON_SLOT.get(resolved_slot or "")
        partner_slot = (
            next((s for s in slots if s.get("name") == partner_slot_name), None)
            if partner_slot_name
            else None
        )
        partner_weapon_equipped = bool(partner_slot and partner_slot.get("selItemId"))

        slot_fast_swap_eligible = (
            bool(resolved_slot)
            and prev_item_id is not None
            and not prev_is_2h
            and not prev_is_jewel_socket
            and not prev_has_granted_skill
            and not prev_has_abyssal
        )

        results = []
        needs_full_restore = False
        any_mutation = False
        total_items = len(eval_items)

        try:
            for pos, entry in enumerate(eval_items):
                item_id = entry.get("id")
                raw_clean = str(entry.get("raw") or "").strip()
                if not raw_clean:
                    results.append({"id": item_id, "ok": False, "error": "Empty item text."})
                    continue

                if needs_full_restore:
                    worker.call("import_code", {"code": snap_before, "mode": "replace"})
                    needs_full_restore = False

                use_fast_swap = slot_fast_swap_eligible and not _is_complex_trade_item(raw_clean, resolved_slot)

                try:
                    add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
                    if resolved_slot:
                        add_params["slotName"] = resolved_slot
                    any_mutation = True
                    add_res = worker.call("add_item", add_params)
                    new_item_obj = add_res.get("item") if isinstance(add_res, dict) else None
                    new_item_id = new_item_obj.get("id") if isinstance(new_item_obj, dict) else None
                    new_item_type = str(new_item_obj.get("type") or "") if isinstance(new_item_obj, dict) else ""
                    if new_item_type in _TWO_HANDED_ITEM_TYPES:
                        use_fast_swap = False
                    if partner_weapon_equipped and new_item_type != prev_item_type:
                        use_fast_swap = False

                    digest_after = capture_digest(worker)
                    diff = diff_digest(digest_before, digest_after)
                    results.append(
                        {
                            "id": item_id,
                            "ok": True,
                            "before": before_summary,
                            "after": {
                                "primaryDpsKey": digest_after["primaryDpsKey"],
                                "primaryDps": digest_after["primaryDps"],
                                "totalEhp": digest_after["totalEhp"],
                                "life": digest_after["life"],
                                "points": digest_after.get("buildInfo", {}).get("points", {}),
                            },
                            "diff": diff,
                        }
                    )

                    has_more = pos < total_items - 1
                    if has_more:
                        if use_fast_swap and prev_item_id is not None and new_item_id is not None and int(new_item_id) != prev_item_id:
                            try:
                                _call_batch(
                                    worker,
                                    [
                                        ("equip_item", {"id": prev_item_id, "slotName": resolved_slot}),
                                        ("delete_item", {"id": int(new_item_id)}),
                                    ],
                                )
                            except Exception:
                                needs_full_restore = True
                        else:
                            needs_full_restore = True
                except Exception as exc:
                    needs_full_restore = True
                    results.append({"id": item_id, "ok": False, "error": str(exc)})
        finally:
            if any_mutation:
                worker.call("import_code", {"code": snap_before, "mode": "replace"})
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        return {
            "slotName": resolved_slot or slot_name,
            "before": before_summary,
            "results": results,
        }
