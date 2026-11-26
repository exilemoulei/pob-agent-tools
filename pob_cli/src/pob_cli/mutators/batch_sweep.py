"""Composite `run_batch`, multi-variant `run_sweep`, and `trade_eval_items`."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..errors import CrossGameHallucinationError
from ..guardrails import GuardrailScope, check_input
from ..snapshot import (
    BuildTransaction,
    capture_direct_digest,
    diff_digest,
    run_mutation,
    supports_direct_calc,
)
from .batch_ops import _execute_batch_ops, ops_scope
from .common import _BatchCache, _merge_cautions, _normalize_keep_list
from .sweep_report import _compact_sweep_summary, _extract_compact_calcs


def _normalize_return_sections(return_sections: list[str] | str | None) -> list[str]:
    if not return_sections:
        return []
    if isinstance(return_sections, str):
        pieces = [x.strip() for x in return_sections.split(",") if x.strip()]
    else:
        pieces = [str(x).strip() for x in return_sections if str(x).strip()]
    raw_items: list[str] = []
    for piece in pieces:
        if ":" not in piece and any(ch.isspace() for ch in piece):
            raw_items.extend(tok for tok in piece.split() if tok)
        else:
            raw_items.append(piece)
    return raw_items


def _collect_return_sections(
    worker: BridgeWorker,
    sections: list[str],
    *,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Lazily import inspection helpers and collect requested readouts on the active build state."""
    from .. import analyzer as _analyzer
    from .. import comparator as _comparator

    keep_arg = _normalize_keep_list(keep) if keep is not None else None
    out: dict[str, Any] = {}
    for raw_sec in sections:
        sec = raw_sec.strip()
        if not sec:
            continue
        if ":" in sec:
            head, body = sec.split(":", 1)
            head_low = head.strip().lower()
            body = body.strip()
        else:
            head_low = sec.lower()
            body = ""

        if head_low == "summary":
            fn = getattr(_analyzer, "extract_summary", None) or _analyzer.get_summary
            out["summary"] = fn(worker)
        elif head_low == "audit":
            body_low = body.lower()
            if body_low == "":
                attr = False
            elif body_low in ("full", "attribution"):
                attr = True
            else:
                raise ValueError(
                    f"Unsupported audit return modifier '{raw_sec}'. "
                    "Use 'audit', 'audit:full', or 'audit:attribution'."
                )
            out["audit"] = _analyzer.audit_build(worker, keep=keep_arg, attribution=attr)
        elif head_low == "matrix":
            scenarios = [s.strip() for s in body.split(";") if s.strip()] if body else None
            out["matrix"] = _comparator.run_matrix(worker, scenarios=scenarios, keep=keep_arg)
        elif head_low == "calc":
            kws = [k.strip() for k in body.split(";") if k.strip()] if body else []
            if not kws:
                raise ValueError("Return section 'calc:<keywords>' requires at least one keyword.")
            calc_fn = getattr(_analyzer, "search_calcs_breakdown", None) or _analyzer.inspect_calcs
            sig_params = inspect.signature(calc_fn).parameters
            if len(kws) == 1:
                out[sec] = calc_fn(worker, kws[0])
            elif "keywords" in sig_params:
                out[sec] = calc_fn(worker, keywords=kws)
            else:
                out[sec] = {kw: calc_fn(worker, kw) for kw in kws}
        elif head_low == "export":
            out["export"] = worker.call("export_code", {}).get("code", "")
        elif head_low == "items":
            out["items"] = _analyzer.inspect_items(worker)
        elif head_low == "skills":
            out["skills"] = _analyzer.inspect_skills(worker)
        elif head_low == "tree":
            out["tree"] = _analyzer.inspect_tree(worker)
        elif head_low == "config":
            out["config"] = _analyzer.inspect_config(worker)
        else:
            raise ValueError(
                f"Unsupported return section '{raw_sec}'. "
                "Supported: summary, audit[:full|attribution], matrix[:s1;s2], calc:<kw1;kw2>, export, items, skills, tree, config."
            )
    return out


def _step_warnings(step_results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Merged cautions and step-prefixed no-op warnings over a batch's step results."""
    cautions: list[str] = []
    noops: list[str] = []
    for st in step_results:
        cautions = _merge_cautions(cautions, st.get("cautions"))
        noops.extend(f"step {st.get('step')}: {msg}" for msg in st.get("noopWarnings") or [])
    return cautions, noops


def run_batch(
    worker: BridgeWorker,
    ops: list[dict[str, Any]],
    *,
    apply: bool = False,
    no_undo: bool = False,
    return_sections: list[str] | str | None = None,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Execute a composite list of mutations in a single transaction with optional inline readouts."""
    import pob_cli.mutators as _mut

    if not ops:
        raise ValueError("Batch operations list cannot be empty.")

    sections = _normalize_return_sections(return_sections)

    if apply and no_undo:
        with BuildTransaction(worker, scope=ops_scope(ops), will_commit=True) as tx:
            cache = _BatchCache(worker)
            step_results = _execute_batch_ops(worker, ops, cache)
            digest_after = _mut.capture_digest(worker)
            returns_map = _collect_return_sections(worker, sections, keep=keep) if sections else None
            exported = returns_map.get("export") if returns_map else None
            tx.commit(undoable=False, after_code=exported if isinstance(exported, str) and exported else None)

            batch_cautions, batch_noops = _step_warnings(step_results)
            result: dict[str, Any] = {
                "applied": True,
                "noUndo": True,
                "mode": "APPLIED (no-undo fast mode)",
                "detail": {"steps": step_results},
                "before": None,
                "after": {
                    "primaryDpsKey": digest_after["primaryDpsKey"],
                    "primaryDps": digest_after["primaryDps"],
                    "totalEhp": digest_after["totalEhp"],
                    "life": digest_after["life"],
                    "points": digest_after.get("buildInfo", {}).get("points", {}),
                },
                "diff": None,
                **tx.history.depths(),
            }
            if batch_cautions:
                result["cautions"] = batch_cautions
            if batch_noops:
                result["noopWarnings"] = batch_noops
            if returns_map is not None:
                result["returns"] = returns_map
            return result

    captured_returns: dict[str, Any] | None = None

    def _do_batch(w: BridgeWorker) -> dict[str, Any]:
        nonlocal captured_returns
        cache = _BatchCache(w)
        step_results = _execute_batch_ops(w, ops, cache)
        if sections:
            captured_returns = _collect_return_sections(w, sections, keep=keep)
        batch_cautions, batch_noops = _step_warnings(step_results)
        detail: dict[str, Any] = {"steps": step_results}
        if batch_cautions:
            detail["cautions"] = batch_cautions
        if batch_noops:
            detail["noopWarnings"] = batch_noops
        return detail

    res = run_mutation(worker, _do_batch, apply=apply, scope=ops_scope(ops))
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    noops = (res.get("detail") or {}).get("noopWarnings")
    if noops:
        res["noopWarnings"] = list(noops)
    if captured_returns is not None:
        res["returns"] = captured_returns
    return res


def _sweep_keep(keep: Iterable[str] | str | None, scenario: str | dict[str, Any] | None) -> list[str] | None:
    merged_keep: list[str] = []
    for tok in _normalize_keep_list(keep):
        if tok not in merged_keep:
            merged_keep.append(tok)
    if isinstance(scenario, dict) and scenario.get("keep") is not None:
        for tok in _normalize_keep_list(scenario.get("keep")):
            if tok not in merged_keep:
                merged_keep.append(tok)
    return merged_keep or None


def _sweep_variant(
    worker: BridgeWorker,
    tx: BuildTransaction,
    var: dict[str, Any],
    v_name: str,
    base_digest: dict[str, Any],
    kw_list: list[str],
    use_direct: bool,
) -> dict[str, Any]:
    """Run one variant's ops on the base build and measure it; raises on a bad variant."""
    import pob_cli.mutators as _mut

    v_ops = var.get("ops") or []
    if not isinstance(v_ops, list):
        raise ValueError(f"Variant '{v_name}' 'ops' must be a list of batch operations.")
    step_results = _execute_batch_ops(worker, v_ops, _BatchCache(worker)) if v_ops else []
    v_digest = capture_direct_digest(worker, base_digest=base_digest) if use_direct else _mut.capture_digest(worker)
    entry: dict[str, Any] = {
        "name": v_name,
        "ok": True,
        "steps": step_results,
        "summary": _compact_sweep_summary(v_digest),
        "diffVsBase": diff_digest(base_digest, v_digest),
        "warnings": v_digest.get("warnings", []),
    }
    if kw_list:
        # get_calcs reads the calc output of the last engine frame, which a lazy restore left stale.
        tx.fresh()
        entry["calcs"] = _extract_compact_calcs(worker, kw_list)
    return entry


def run_sweep(
    worker: BridgeWorker,
    variants: list[dict[str, Any]],
    *,
    base_ops: list[dict[str, Any]] | None = None,
    calc_keywords: list[str] | str | None = None,
    scenario: str | dict[str, Any] | None = None,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Evaluate N named build variants in ONE warm daemon worker with fault isolation and 0-residue rollback."""
    import pob_cli.mutators as _mut

    if not variants:
        raise ValueError("Sweep variants list cannot be empty.")

    kw_list: list[str] = []
    if isinstance(calc_keywords, str):
        kw_list = [k.strip() for k in calc_keywords.split(",") if k.strip()]
    elif calc_keywords:
        kw_list = [str(k).strip() for k in calc_keywords if str(k).strip()]
    for kw in kw_list:
        check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")

    eff_keep = _sweep_keep(keep, scenario)
    variant_ops = [v["ops"] for v in variants if isinstance(v, dict) and isinstance(v.get("ops"), list)]
    scope = ops_scope(base_ops, *variant_ops)
    has_scenario = scenario is not None and (not isinstance(scenario, str) or bool(scenario.strip()))

    with BuildTransaction(worker, scope=scope) as tx:
        assert tx.base is not None
        use_direct = supports_direct_calc(worker)
        results: list[dict[str, Any]] = []
        base_steps: list[dict[str, Any]] = []
        scenario_applied: dict[str, Any] | None = None
        orig_digest: dict[str, Any] | None = None
        fallbacks: list[str] = []

        if base_ops or has_scenario:
            orig_digest = (
                capture_direct_digest(worker) if (use_direct and tx.base.kind == "cow") else _mut.capture_digest(worker)
            )
            tx.verify_digest = orig_digest
        if base_ops:
            base_steps = _execute_batch_ops(worker, base_ops, _BatchCache(worker))
        if has_scenario:
            from ..comparator import _apply_matrix_scenario_in_place, _parse_matrix_scenario

            plan = _parse_matrix_scenario(scenario)
            scenario_applied = _apply_matrix_scenario_in_place(worker, plan, keep=eff_keep)
        base_cp = tx.checkpoint() if (base_ops or has_scenario) else tx.base

        full_base = _mut.capture_digest(worker)
        if orig_digest is None:
            orig_digest = full_base
            tx.verify_digest = orig_digest
        # Variants are measured by direct_calc, so their baseline must come from the same pass.
        base_digest = capture_direct_digest(worker, base_digest=full_base) if use_direct else full_base
        base_calcs = _extract_compact_calcs(worker, kw_list) if kw_list else None

        dirty = False
        for idx, var in enumerate(variants):
            if not isinstance(var, dict):
                results.append(
                    {
                        "name": f"Variant {idx + 1}",
                        "ok": False,
                        "error": f"Variant entry #{idx + 1} must be a JSON object.",
                    }
                )
                continue
            v_name = str(var.get("name") or f"Variant {idx + 1}").strip()
            if dirty:
                # direct_calc recalculates on its own, so the restore can leave the calc output stale.
                note = tx.restore(base_cp, rebuild=not use_direct)
                if note:
                    fallbacks.append(f"before '{v_name}': {note}")
            dirty = bool(var.get("ops"))
            try:
                results.append(_sweep_variant(worker, tx, var, v_name, base_digest, kw_list, use_direct))
            except CrossGameHallucinationError:
                raise
            except Exception as exc:
                dirty = True
                results.append({"name": v_name, "ok": False, "error": str(exc)})

        restore_check = tx.rollback_verified(orig_digest)
        if restore_check.get("fallback"):
            fallbacks.append(str(restore_check["fallback"]))

    baseline_payload: dict[str, Any] = {
        "summary": _compact_sweep_summary(base_digest),
        "warnings": base_digest.get("warnings", []),
    }
    if base_calcs is not None:
        baseline_payload["calcs"] = base_calcs

    out: dict[str, Any] = {
        "scenario": scenario,
        "scenarioApplied": scenario_applied,
        "baseOpsCount": len(base_ops or []),
        "baseSteps": base_steps,
        "calcKeywords": kw_list,
        "baseline": baseline_payload,
        "variants": results,
        "succeeded": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "restoreCheck": restore_check,
    }
    if fallbacks:
        out["restoreFallbacks"] = fallbacks
    return out


def trade_eval_items(
    worker: BridgeWorker,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
    prefilter_k: int | None = None,
) -> dict[str, Any]:
    """Delegate read-only batch trade evaluation to `trade_eval.eval_trade_items`."""
    from ..trade_eval import eval_trade_items

    return eval_trade_items(worker, slot_name=slot_name, items=items, prefilter_k=prefilter_k)
