"""CoW-verified zero-buy auto-rebalance solver (`solve_rebalance`) with Tier 5 trade fallback (L2)."""

from __future__ import annotations

import itertools
import time
from typing import Any

from ..analyzer import inspect_items
from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..guardrails import GuardrailScope, check_input, check_input_item_raw
from ..mutators import apply_tattoo_in_place, edit_raw_item_mods, resolve_slot_from_items
from ..snapshot import BuildTransaction, _call_batch, capture_digest, diff_digest
from .deficits import BuildDeficits, RebalanceStep, extract_build_deficits
from .steps_catalyst_tattoo import apply_catalyst_to_raw, enumerate_catalyst_steps, enumerate_tattoo_steps
from .steps_craft_harvest import (
    CRAFTABLE_GEAR_SLOTS,
    enumerate_bench_recraft_steps,
    enumerate_harvest_swap_steps,
)
from .trade_fallback import build_trade_fallback as _build_trade_fallback
from .trade_fallback import compute_stat_delta, pct_rank_key

_EVAL_TIMEOUT_SEC = 30.0


def _are_steps_compatible(combo: tuple[RebalanceStep, ...], deficits: BuildDeficits) -> bool:
    seen_keys: set[str] = set()
    harvest_dst_by_slot: set[tuple[str, str]] = set()
    attr_delta: dict[str, float] = {}
    for st in combo:
        if st.target_key in seen_keys:
            return False
        seen_keys.add(st.target_key)
        if st.kind == "harvest_swap":
            pair = (st.slot, str(st.apply_spec.get("harvestTo") or ""))
            if pair in harvest_dst_by_slot:
                return False
            harvest_dst_by_slot.add(pair)
        for k, v in st.covers.items():
            if k in ("Str", "Dex", "Int", "Omni"):
                attr_delta[k] = attr_delta.get(k, 0.0) + v
    for ak, dv in attr_delta.items():
        if dv < 0 and (-dv) > deficits.attr_surplus(ak):
            return False
    return True


def _combo_estimate(combo: tuple[RebalanceStep, ...], deficits: BuildDeficits) -> tuple[bool, float, float]:
    """Return `(estimated_full_close, remaining_gap, total_cost)` analytically for fast candidate pruning."""
    needs = deficits.need_map()
    combined: dict[str, float] = {}
    total_cost = 0.0
    for st in combo:
        total_cost += st.est_cost_chaos
        for k, v in st.covers.items():
            combined[k] = combined.get(k, 0.0) + v

    rem_gap = 0.0
    for k, need in needs.items():
        rem_gap += max(0.0, need - combined.get(k, 0.0))
    for elem in ("Fire", "Cold", "Lightning"):
        if elem == "Lightning" and deficits.has_doryani:
            continue
        delta = combined.get(elem, 0.0)
        if delta < 0 and (-delta) > deficits.resist_slack(elem):
            rem_gap += (-delta) - deficits.resist_slack(elem)
    for attr in ("Str", "Dex", "Int", "Omni"):
        delta = combined.get(attr, 0.0)
        if delta < 0 and (-delta) > deficits.attr_surplus(attr):
            rem_gap += (-delta) - deficits.attr_surplus(attr)
    return rem_gap <= 1e-6, rem_gap, total_cost


def _select_candidate_combos(
    steps: list[RebalanceStep],
    deficits: BuildDeficits,
    *,
    max_steps: int,
    max_eval: int = 24,
) -> tuple[list[tuple[RebalanceStep, ...]], bool]:
    if not steps:
        return [], False
    needs = deficits.need_map()
    active_need_total = sum(needs.values())

    def _step_score(st: RebalanceStep) -> tuple[float, float, int]:
        gain = sum(min(max(0.0, st.covers.get(k, 0.0)), v) for k, v in needs.items())
        return (-gain, st.est_cost_chaos, st.tier)

    pool_capped = len(steps) > 18
    pool = sorted(steps, key=_step_score)[:18]
    full_combos: list[tuple[float, int, int, tuple[RebalanceStep, ...]]] = []
    partial_combos: list[tuple[float, float, int, tuple[RebalanceStep, ...]]] = []

    for r in range(1, max(1, min(3, max_steps)) + 1):
        for combo in itertools.combinations(pool, r):
            if not _are_steps_compatible(combo, deficits):
                continue
            closes, rem_gap, cost = _combo_estimate(combo, deficits)
            max_tier = max(s.tier for s in combo)
            if closes:
                full_combos.append((cost, r, max_tier, combo))
            elif rem_gap < active_need_total - 1e-6:
                partial_combos.append((rem_gap, cost, r, combo))

    full_combos.sort(key=lambda x: (x[0], x[1], x[2]))
    partial_combos.sort(key=lambda x: (x[0], x[1], x[2]))
    selected = [c for _, _, _, c in full_combos[:max_eval]]
    combo_capped = len(full_combos) > max_eval
    if len(selected) < max_eval:
        need_more = min(8, max_eval - len(selected))
        if len(partial_combos) > need_more:
            combo_capped = True
        selected.extend(c for _, _, _, c in partial_combos[:need_more])
    return selected, (pool_capped or combo_capped)


def _apply_combo_in_place(
    worker: BridgeWorker,
    combo: tuple[RebalanceStep, ...],
    raw_by_slot: dict[str, str],
) -> None:
    """Apply all item and tattoo steps of `combo` cleanly in place (composing multi-step edits per item)."""
    item_steps_by_slot: dict[str, list[RebalanceStep]] = {}
    tattoo_steps: list[RebalanceStep] = []
    for st in combo:
        if st.apply_spec.get("kind") == "tattoo":
            tattoo_steps.append(st)
        else:
            item_steps_by_slot.setdefault(st.slot, []).append(st)

    for slot, slot_steps in item_steps_by_slot.items():
        cur_raw = raw_by_slot.get(slot)
        if not cur_raw:
            raise DaemonRequestError(f"Slot '{slot}' raw item text unavailable for rebalance step.")
        for st in slot_steps:
            spec = st.apply_spec
            skind = spec.get("kind")
            if skind == "item_edit":
                rep = [(str(a), str(b)) for a, b in (spec.get("replaceMods") or ())]
                add = [str(x) for x in (spec.get("addMods") or ())]
                cur_raw, _ = edit_raw_item_mods(cur_raw, replace_mods=rep or None, add_mods=add or None)
            elif skind == "item_catalyst":
                cur_raw = apply_catalyst_to_raw(cur_raw, str(spec["catalyst"]), int(spec.get("quality") or 20))
        worker.call("add_item", {"raw": cur_raw, "slotName": slot, "equip": True})

    for st in tattoo_steps:
        spec = st.apply_spec
        apply_tattoo_in_place(worker, node=spec["node"], tattoo=str(spec["tattoo"]))


def _evaluate_combo_in_pob(
    worker: BridgeWorker,
    combo: tuple[RebalanceStep, ...],
    *,
    raw_by_slot: dict[str, str],
    digest_base: dict[str, Any],
    base_deficits: BuildDeficits,
    skills_res: dict[str, Any],
    include_chaos: bool,
    include_suppression: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with BuildTransaction(worker) as sub_tx:
            _apply_combo_in_place(worker, combo, raw_by_slot)
            after_digest = capture_digest(worker)
            after_items = worker.call("list_items", {})
            sub_tx.rollback()
    except Exception as exc:
        return None, str(exc)

    after_def = extract_build_deficits(
        after_digest["stats"],
        items_res=after_items,
        skills_res=skills_res,
        include_chaos=include_chaos,
        include_suppression=include_suppression,
    )
    primary_key = str(digest_base.get("primaryDpsKey") or "CombinedDPS")
    base_stats = digest_base.get("stats") if isinstance(digest_base.get("stats"), dict) else {}
    after_stats = after_digest.get("stats") if isinstance(after_digest.get("stats"), dict) else {}
    delta_dps, delta_dps_pct = compute_stat_delta(base_stats, after_stats, primary_key)
    delta_ehp, delta_ehp_pct = compute_stat_delta(base_stats, after_stats, "TotalEHP")

    fully_resolved = (not after_def.has_hard_deficits) and (not after_def.need_map())
    improved = after_def.hard_gap_total() < base_deficits.hard_gap_total() - 1e-6 or (
        sum(after_def.need_map().values()) < sum(base_deficits.need_map().values()) - 1e-6
    )
    if not fully_resolved and not improved:
        return None, None

    warnings: list[str] = []
    for st in combo:
        for w_txt in st.warnings:
            if w_txt not in warnings:
                warnings.append(w_txt)

    return (
        {
            "fullyResolved": fully_resolved,
            "estCostChaos": round(sum(st.est_cost_chaos for st in combo), 1),
            "stepCount": len(combo),
            "maxTier": max(st.tier for st in combo),
            "steps": [st.to_dict() for st in combo],
            "_combo": combo,
            "deltaDps": delta_dps,
            "deltaDpsPct": delta_dps_pct,
            "deltaEhp": delta_ehp,
            "deltaEhpPct": delta_ehp_pct,
            "remainingDeficits": after_def.need_map(),
            "remainingHardGap": round(after_def.hard_gap_total(), 1),
            "warnings": warnings,
        },
        None,
    )


def _enumerate_and_verify_plans(
    worker: BridgeWorker,
    *,
    equipped_raw: list[dict[str, Any]],
    raw_by_slot: dict[str, str],
    tree_state: dict[str, Any],
    skills_res: dict[str, Any],
    digest_base: dict[str, Any],
    deficits: BuildDeficits,
    max_steps: int,
    max_evals: int,
    include_chaos: bool,
    include_suppression: bool,
    eval_timeout_s: float = _EVAL_TIMEOUT_SEC,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], bool, int, int, int, bool]:
    """Enumerate Tier 1-4 candidates, prune combos, CoW-verify in PoB, and return ranked raw plan lists."""
    t0 = time.monotonic()
    pob_dir = getattr(worker, "pob_dir", None) or getattr(worker, "pob_root", None)
    runtime_dir = getattr(worker, "runtime_dir", None)

    s_bench = enumerate_bench_recraft_steps(equipped_raw, deficits, pob_dir=pob_dir, runtime_dir=runtime_dir)
    s_harvest = enumerate_harvest_swap_steps(equipped_raw, deficits)
    s_catalyst = enumerate_catalyst_steps(equipped_raw, deficits)

    caps = getattr(worker, "caps", None)
    s_tattoo: list[RebalanceStep] = []
    if not (isinstance(caps, dict) and caps and caps.get("tattoos") is False):
        nodes_db: dict[str, Any] = {}
        if hasattr(worker, "get_cached_tree_data"):
            try:
                nodes_db = (worker.get_cached_tree_data(tree_state.get("treeVersion")) or {}).get("nodes", {})
            except Exception:
                nodes_db = {}
        s_tattoo = enumerate_tattoo_steps(tree_state, nodes_db, deficits, worker=worker)

    all_steps = [*s_bench, *s_harvest, *s_catalyst, *s_tattoo]
    combos, cap_applied = _select_candidate_combos(all_steps, deficits, max_steps=max_steps, max_eval=max_evals)

    verified_raw: list[dict[str, Any]] = []
    partial_raw: list[dict[str, Any]] = []
    eval_errors = 0
    evaluated = 0
    timed_out = False
    for combo in combos:
        if evaluated > 0 and (time.monotonic() - t0) >= eval_timeout_s:
            timed_out = True
            break
        evaluated += 1
        ev, err_txt = _evaluate_combo_in_pob(
            worker,
            combo,
            raw_by_slot=raw_by_slot,
            digest_base=digest_base,
            base_deficits=deficits,
            skills_res=skills_res,
            include_chaos=include_chaos,
            include_suppression=include_suppression,
        )
        if err_txt is not None:
            eval_errors += 1
            continue
        if ev is None:
            continue
        (verified_raw if ev["fullyResolved"] else partial_raw).append(ev)

    elapsed_ms = round((time.monotonic() - t0) * 1000.0)
    eval_budget_reached = bool(cap_applied or timed_out)

    verified_raw.sort(
        key=lambda p: (
            float(p["estCostChaos"]),
            int(p["stepCount"]),
            -pct_rank_key(p.get("deltaDpsPct")),
            -pct_rank_key(p.get("deltaEhpPct")),
        )
    )
    partial_raw.sort(
        key=lambda p: (
            float(p["remainingHardGap"]),
            sum(float(v) for v in (p.get("remainingDeficits") or {}).values()),
            float(p["estCostChaos"]),
            -pct_rank_key(p.get("deltaDpsPct")),
        )
    )
    counts = {"bench": len(s_bench), "harvest": len(s_harvest), "catalyst": len(s_catalyst), "tattoo": len(s_tattoo)}
    return verified_raw, partial_raw, counts, cap_applied, evaluated, eval_errors, elapsed_ms, eval_budget_reached


def solve_rebalance(
    worker: BridgeWorker,
    *,
    try_item_raw: str | None = None,
    slot_name: str | None = None,
    max_steps: int = 3,
    max_evals: int = 24,
    include_chaos: bool = False,
    include_suppression: bool = False,
    apply_plan: int | None = None,
) -> dict[str, Any]:
    """Find, CoW-verify, and rank zero-buy Bench/Harvest/Catalyst/Tattoo rebalance plans (with Tier 5 trade fallback)."""
    max_steps = max(1, min(3, int(max_steps)))
    max_evals = max(1, min(60, int(max_evals)))
    if try_item_raw is not None:
        try_item_raw = try_item_raw.lstrip("\ufeff")
    if try_item_raw is not None and try_item_raw.strip():
        check_input_item_raw(try_item_raw, expected_game="poe1")
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")

    with worker.lock, BuildTransaction(worker) as tx:
        digest_orig = capture_digest(worker)
        try_info: dict[str, Any] | None = None

        if try_item_raw is not None and try_item_raw.strip():
            items_pre = worker.call("list_items", {})
            resolved_slot = resolve_slot_from_items(items_pre, slot_name) if slot_name else None
            add_params: dict[str, Any] = {"raw": try_item_raw.strip(), "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            add_res = worker.call("add_item", add_params)
            it_obj = add_res.get("item") if isinstance(add_res, dict) else None
            try_info = {
                "slot": resolved_slot or (it_obj.get("primarySlot") if isinstance(it_obj, dict) else slot_name),
                "name": (it_obj.get("title") or it_obj.get("name")) if isinstance(it_obj, dict) else None,
            }

        digest_base = capture_digest(worker)
        items_res, skills_res, tree_state = _call_batch(
            worker,
            [("list_items", {}), ("list_skills", {}), ("get_tree_state", {})],
            raise_on_error=False,
        )
        items_res = items_res if isinstance(items_res, dict) else {"items": [], "slots": []}
        skills_res = skills_res if isinstance(skills_res, dict) else {"groups": []}
        tree_state = tree_state if isinstance(tree_state, dict) else {}

        deficits = extract_build_deficits(
            digest_base["stats"],
            items_res=items_res,
            skills_res=skills_res,
            include_chaos=include_chaos,
            include_suppression=include_suppression,
        )

        if not deficits.has_hard_deficits and not deficits.need_map():
            restore_check = tx.rollback_verified(digest_orig)
            res_bal: dict[str, Any] = {
                "status": "already_balanced",
                "applied": False,
                "tryItem": try_info,
                "deficits": deficits.to_dict(),
                "verifiedPlans": [],
                "partialPlans": [],
                "tradeFallback": None,
                "candidateCounts": {"bench": 0, "harvest": 0, "catalyst": 0, "tattoo": 0},
                "candidateCapApplied": False,
                "evaluatedCombos": 0,
                "evalErrors": 0,
                "elapsedMs": 0,
                "evalBudgetReached": False,
                "restoreCheck": restore_check,
            }
            if apply_plan is not None:
                res_bal["message"] = (
                    f"Build is already balanced — no rebalance plan needed; --apply-plan {apply_plan} was not applied."
                )
            return res_bal

        equipped_raw = [
            e
            for e in (inspect_items(worker, all_raw=True).get("equippedSlots") or ())
            if e.get("slotName") in CRAFTABLE_GEAR_SLOTS and str(e.get("raw") or "").strip()
        ]
        raw_by_slot = {str(e["slotName"]): str(e["raw"]) for e in equipped_raw}
        (
            verified_raw,
            partial_raw,
            candidate_counts,
            cap_applied,
            eval_combos,
            eval_errors,
            elapsed_ms,
            eval_budget_reached,
        ) = _enumerate_and_verify_plans(
            worker,
            equipped_raw=equipped_raw,
            raw_by_slot=raw_by_slot,
            tree_state=tree_state,
            skills_res=skills_res,
            digest_base=digest_base,
            deficits=deficits,
            max_steps=max_steps,
            max_evals=max_evals,
            include_chaos=include_chaos,
            include_suppression=include_suppression,
        )

        # Assign continuous 1-based plan indices across verifiedPlans (#1..#V) and partialPlans (#V+1..#V+P).
        top_verified = verified_raw[:10]
        top_partial = partial_raw[:5]
        verified_plans: list[dict[str, Any]] = []
        for idx, p in enumerate(top_verified, start=1):
            pub = {k: v for k, v in p.items() if k != "_combo"}
            pub["index"] = idx
            pub["planIndex"] = idx
            verified_plans.append(pub)
        partial_plans: list[dict[str, Any]] = []
        for idx, p in enumerate(top_partial, start=len(verified_plans) + 1):
            pub = {k: v for k, v in p.items() if k != "_combo"}
            pub["index"] = idx
            pub["planIndex"] = idx
            partial_plans.append(pub)

        # Single-state consistency: always compute Tier 5 trade fallback against base equipped_raw + base deficits.
        trade_fallback = _build_trade_fallback(equipped_raw, deficits)

        applied_res: dict[str, Any] | None = None
        if apply_plan is not None:
            combined_apply = [*top_verified, *top_partial]
            if apply_plan < 1 or apply_plan > len(combined_apply):
                valid_hint = f"1..{len(combined_apply)}" if combined_apply else "none available"
                raise DaemonRequestError(
                    f"--apply-plan {apply_plan} out of range (valid: {valid_hint}; verified: {len(verified_plans)}, partial: {len(partial_plans)})."
                )
            chosen_combo: tuple[RebalanceStep, ...] = combined_apply[apply_plan - 1]["_combo"]
            _apply_combo_in_place(worker, chosen_combo, raw_by_slot)
            final_digest = capture_digest(worker)
            tx.commit()
            applied_res = {
                "planIndex": apply_plan,
                "delta": diff_digest(digest_orig, final_digest),
            }
            restore_check = None
        else:
            restore_check = tx.rollback_verified(digest_orig)

        status = "solved" if verified_plans else ("partial" if partial_plans else "trade_needed")
        out: dict[str, Any] = {
            "status": status,
            "applied": apply_plan is not None,
            "tryItem": try_info,
            "deficits": deficits.to_dict(),
            "verifiedPlans": verified_plans,
            "partialPlans": partial_plans,
            "tradeFallback": trade_fallback,
            "candidateCounts": candidate_counts,
            "candidateCapApplied": cap_applied,
            "evaluatedCombos": eval_combos,
            "evalErrors": eval_errors,
            "elapsedMs": elapsed_ms,
            "evalBudgetReached": eval_budget_reached,
            "restoreCheck": restore_check,
        }
        if applied_res is not None:
            out["appliedPlan"] = applied_res
        return out
