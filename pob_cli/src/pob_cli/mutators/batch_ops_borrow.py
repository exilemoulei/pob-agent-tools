"""Cross-slot `borrow` (alias `transplant`) batch operation handler for `pob batch` / `pob sweep`."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from .common import _BatchCache
from .strict_args import parse_bool

BORROW_OP_KINDS: frozenset[str] = frozenset({"borrow", "transplant"})


def resolve_bool_alias_group(
    op: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: bool,
    group_label: str | None = None,
) -> bool:
    """Resolve a boolean alias group, skipping `None` values and rejecting disagreeing non-`None` aliases."""
    vals = [(k, parse_bool(op[k], k)) for k in keys if op.get(k) is not None]
    if not vals:
        return default
    if len({v for _, v in vals}) > 1:
        lbl = group_label or keys[0]
        raise ValueError(f"conflicting {lbl} aliases {dict(vals)}; pass only one or matching booleans.")
    return vals[0][1]


def _op_kind(op: Any) -> str:
    if not isinstance(op, dict):
        return ""
    return str(op.get("op") or op.get("type") or op.get("action") or "").strip().lower().replace("-", "_")


def ops_need_ref(*op_lists: list[dict[str, Any]] | None) -> bool:
    """Return True iff any batch/sweep op in `op_lists` uses `op: 'borrow'` (or `'transplant'`)."""
    for ops in op_lists:
        if not isinstance(ops, list):
            continue
        for op in ops:
            if _op_kind(op) in BORROW_OP_KINDS:
                return True
    return False


def extract_borrow_item_slots(op: dict[str, Any]) -> list[str]:
    """Extract normalized item slot list from a `borrow` batch step."""
    slots: list[str] = []
    for key in ("items", "item_slots", "itemSlots", "slots"):
        val = op.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            slots.extend(x.strip() for x in val.split(",") if x.strip())
        elif isinstance(val, list):
            slots.extend(str(x).strip() for x in val if str(x).strip())
    for key in ("slot", "item_slot"):
        val = op.get(key)
        if val is not None and str(val).strip():
            slots.extend(x.strip() for x in str(val).split(",") if x.strip())
    deduped: list[str] = []
    for s in slots:
        if s not in deduped:
            deduped.append(s)
    return deduped


def _op_borrow(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    """Execute `op: "borrow"` using the pre-captured `cache.ref_bundle` (0 RPCs to `ref_worker`)."""
    if cache.ref_bundle is None:
        raise ValueError(
            f"Batch op 'borrow' at step {idx} requires a loaded reference build "
            "(load one into --ref-target or pass `ref_bundle`)."
        )
    item_slots = extract_borrow_item_slots(op)
    skill_group = op.get("skill_group")
    if skill_group is None or (isinstance(skill_group, str) and not skill_group.strip()):
        skill_group = op.get("skillGroup")
    borrow_tree = resolve_bool_alias_group(
        op, ("tree", "borrow_tree", "borrowTree"), default=False, group_label="tree"
    )
    borrow_tree_jewels = resolve_bool_alias_group(
        op, ("tree_jewels", "treeJewels"), default=True, group_label="tree_jewels"
    )

    from ..comparator.borrow import apply_borrow_in_place

    detail = apply_borrow_in_place(
        w,
        cache.ref_bundle,
        item_slots=item_slots or None,
        skill_group=skill_group,
        borrow_tree=borrow_tree,
        borrow_tree_jewels=borrow_tree_jewels,
        cache=cache,
    )
    return {"step": idx, "op": "borrow", **detail}
