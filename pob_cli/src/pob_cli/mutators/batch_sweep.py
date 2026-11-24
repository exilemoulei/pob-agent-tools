"""Composite `run_batch`, multi-variant `run_sweep`, and `trade_eval_items`."""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..errors import CrossGameHallucinationError
from ..formatters import _parse_numeric_or_none, fmt_num, strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import (
    capture_direct_digest,
    diff_digest,
    drop_snapshot,
    restore_snapshot,
    run_mutation,
    supports_direct_calc,
    take_snapshot,
    verify_cow_restored,
)
from .batch_ops import _execute_batch_ops
from .common import _BatchCache, _call_compat, _merge_cautions, _normalize_keep_list

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


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
            out["audit"] = _call_compat(_analyzer.audit_build, worker, keep=keep_arg, attribution=attr)
        elif head_low == "matrix":
            scenarios = [s.strip() for s in body.split(";") if s.strip()] if body else None
            out["matrix"] = _call_compat(_comparator.run_matrix, worker, scenarios=scenarios, keep=keep_arg)
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
        with worker.lock:
            fallback_code = getattr(worker, "last_good_code", None)
            if not fallback_code:
                fallback_code = str(worker.call("export_code", {}).get("code") or "") or None
            cache = _BatchCache(worker)
            try:
                step_results = _execute_batch_ops(worker, ops, cache)
                digest_after = _mut.capture_digest(worker)
                returns_map = _collect_return_sections(worker, sections, keep=keep) if sections else None
            except Exception:
                if fallback_code:
                    try:
                        worker.call("import_code", {"code": fallback_code, "mode": "replace"})
                    except Exception:
                        pass
                raise

            if returns_map and isinstance(returns_map.get("export"), str) and returns_map["export"]:
                snap_after = returns_map["export"]
            else:
                snap_after = str(worker.call("export_code", {}).get("code") or "")
            if snap_after:
                worker.last_good_code = snap_after
            worker.redo_stack.clear()
            save_fn = getattr(worker, "save_slot_state", None)
            if callable(save_fn):
                save_fn()

            batch_cautions: list[str] = []
            for st in step_results:
                batch_cautions = _merge_cautions(batch_cautions, st.get("cautions"))
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
                "undoDepth": len(worker.undo_stack),
                "redoDepth": len(worker.redo_stack),
            }
            if batch_cautions:
                result["cautions"] = batch_cautions
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
        batch_cautions: list[str] = []
        for st in step_results:
            batch_cautions = _merge_cautions(batch_cautions, st.get("cautions"))
        detail: dict[str, Any] = {"steps": step_results}
        if batch_cautions:
            detail["cautions"] = batch_cautions
        return detail

    res = run_mutation(worker, _do_batch, apply=apply)
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    if captured_returns is not None:
        res["returns"] = captured_returns
    return res


def _json_safe_num(val: Any) -> float | str | None:
    if val is None:
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None:
        return str(val)
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


def _safe_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    """Compute elemental max hit (`min(Fire, Cold, Lightning)`) safely across `inf`/`NaN`/None."""
    vals: list[float] = []
    for k in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw_v = stats.get(k)
        if raw_v is None:
            continue
        parsed = _parse_numeric_or_none(raw_v)
        if parsed is not None and not math.isnan(parsed):
            vals.append(parsed)
    if not vals:
        return None
    m = min(vals)
    return m if math.isfinite(m) else fmt_num(m)


def _compact_sweep_summary(digest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, RFC-8259-safe offence/defence summary from a `capture_digest` dict."""
    stats = digest.get("stats") or {}
    return {
        "primaryDpsKey": digest.get("primaryDpsKey"),
        "primaryDps": digest.get("primaryDps"),
        "fullDps": _json_safe_num(stats.get("FullDPS")),
        "combinedDps": _json_safe_num(stats.get("CombinedDPS")),
        "totalDps": _json_safe_num(stats.get("TotalDPS")),
        "totalDotDps": _json_safe_num(stats.get("TotalDotDPS")),
        "minionCombinedDps": _json_safe_num(stats.get("MinionCombinedDPS")),
        "averageDamage": _json_safe_num(stats.get("AverageDamage")),
        "speed": _json_safe_num(stats.get("Speed")),
        "critChance": _json_safe_num(stats.get("CritChance")),
        "critMultiplier": _json_safe_num(stats.get("CritMultiplier")),
        "hitChance": _json_safe_num(stats.get("HitChance")),
        "manaCost": _json_safe_num(stats.get("ManaCost")),
        "totalEhp": digest.get("totalEhp"),
        "life": digest.get("life"),
        "energyShield": _json_safe_num(stats.get("EnergyShield")),
        "mana": _json_safe_num(stats.get("Mana")),
        "manaUnreserved": _json_safe_num(stats.get("ManaUnreserved")),
        "armour": _json_safe_num(stats.get("Armour")),
        "evasion": _json_safe_num(stats.get("Evasion")),
        "physMaxHit": _json_safe_num(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": _safe_ele_max_hit(stats),
        "fireMaxHit": _json_safe_num(stats.get("FireMaximumHitTaken")),
        "coldMaxHit": _json_safe_num(stats.get("ColdMaximumHitTaken")),
        "lightningMaxHit": _json_safe_num(stats.get("LightningMaximumHitTaken")),
        "chaosMaxHit": _json_safe_num(stats.get("ChaosMaximumHitTaken")),
        "fireResist": _json_safe_num(stats.get("FireResist")),
        "coldResist": _json_safe_num(stats.get("ColdResist")),
        "lightningResist": _json_safe_num(stats.get("LightningResist")),
        "chaosResist": _json_safe_num(stats.get("ChaosResist")),
        "spellSuppression": _json_safe_num(stats.get("EffectiveSpellSuppressionChance")),
        "blockChance": _json_safe_num(stats.get("EffectiveBlockChance")),
        "spellBlockChance": _json_safe_num(stats.get("EffectiveSpellBlockChance")),
        "points": (digest.get("buildInfo") or {}).get("points", {}),
    }


def _extract_compact_calcs(
    worker: BridgeWorker,
    calc_keywords: list[str],
    *,
    limit_per_keyword: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch `get_calcs` ONCE and extract compact matching row values (no breakdown dumps)."""
    has_cjk = any(_CJK_RE.search(str(kw)) for kw in calc_keywords)
    toggled_translate = False
    if has_cjk:
        caps = getattr(worker, "caps", None) or {}
        if caps.get("translateToggle", not caps):
            tracked = getattr(worker, "translate_enabled", None)
            if tracked is None:
                info_dict = getattr(worker, "hello_info", None) or getattr(worker, "info", None) or {}
                if isinstance(info_dict, dict) and "translate" in info_dict:
                    tracked = info_dict.get("translate")
            if tracked is False:
                try:
                    worker.call("set_translate", {"enabled": True})
                    toggled_translate = True
                except Exception:
                    toggled_translate = False
    try:
        calcs = worker.call("get_calcs", {})
    finally:
        if toggled_translate:
            try:
                worker.call("set_translate", {"enabled": False})
            except Exception:
                pass

    rows_index: list[dict[str, Any]] = []
    for sec in calcs.get("sections", []):
        sec_id = str(sec.get("id") or "")
        for sub in sec.get("subsections", []):
            sub_en = strip_pob_escapes(str(sub.get("label") or ""))
            sub_zh = strip_pob_escapes(str(sub.get("labelZh") or sub_en))
            for row in sub.get("rows", []):
                r_en = strip_pob_escapes(str(row.get("label") or ""))
                r_zh = strip_pob_escapes(str(row.get("labelZh") or r_en))
                vals: list[dict[str, Any]] = []
                cell_texts: list[str] = []
                for cell in row.get("cells", []):
                    raw_v = strip_pob_escapes(str(cell.get("raw") or ""))
                    txt_v = strip_pob_escapes(str(cell.get("text") or raw_v))
                    if raw_v or txt_v:
                        vals.append({"raw": raw_v or txt_v, "text": txt_v or raw_v})
                        cell_texts.append(f"{raw_v} {txt_v}")
                hay_row = f"{r_en} {r_zh}".lower()
                hay_sub = f"{sec_id} {sub_en} {sub_zh}".lower()
                hay_full = f"{hay_sub} {hay_row} {' '.join(cell_texts).lower()}"
                rows_index.append(
                    {
                        "sectionId": sec_id,
                        "subsection": sub_en,
                        "subsectionZh": sub_zh,
                        "rowLabel": r_en,
                        "rowLabelZh": r_zh,
                        "values": vals,
                        "_hay_row": hay_row,
                        "_hay_row_compact": re.sub(r"[\s_\-]+", "", hay_row),
                        "_hay_full": hay_full,
                        "_hay_full_compact": re.sub(r"[\s_\-]+", "", hay_full),
                    }
                )

    out: dict[str, list[dict[str, Any]]] = {}
    for kw in calc_keywords:
        q = kw.strip().lower()
        q_compact = re.sub(r"[\s_\-]+", "", q)
        if not q:
            out[kw] = []
            continue
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for pos, r in enumerate(rows_index):
            if q in r["_hay_row"] or (q_compact and q_compact in r["_hay_row_compact"]):
                score = 3
            elif q in r["_hay_full"] or (q_compact and q_compact in r["_hay_full_compact"]):
                score = 1
            else:
                continue
            scored.append((score, pos, r))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out[kw] = [
            {
                "sectionId": r["sectionId"],
                "subsection": r["subsection"],
                "rowLabel": r["rowLabel"],
                "rowLabelZh": r["rowLabelZh"],
                "values": r["values"],
            }
            for _, _, r in scored[:limit_per_keyword]
        ]
    return out


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

    merged_keep: list[str] = []
    for tok in _normalize_keep_list(keep):
        if tok not in merged_keep:
            merged_keep.append(tok)
    if isinstance(scenario, dict) and scenario.get("keep") is not None:
        for tok in _normalize_keep_list(scenario.get("keep")):
            if tok not in merged_keep:
                merged_keep.append(tok)
    eff_keep: list[str] | None = merged_keep or None

    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        use_direct = supports_direct_calc(worker)
        orig_snap = take_snapshot(worker)
        base_snap = orig_snap
        results: list[dict[str, Any]] = []
        base_steps: list[dict[str, Any]] = []
        scenario_applied: dict[str, Any] | None = None
        orig_digest: dict[str, Any] | None = None
        restore_check: dict[str, Any] | None = None
        will_mutate_base = bool(base_ops) or (
            scenario is not None and (not isinstance(scenario, str) or scenario.strip())
        )

        try:
            try:
                if will_mutate_base:
                    orig_digest = (
                        capture_direct_digest(worker)
                        if (use_direct and orig_snap.kind == "cow")
                        else _mut.capture_digest(worker)
                    )

                mutated_base = False

                if base_ops:
                    base_steps = _execute_batch_ops(worker, base_ops, _BatchCache(worker))
                    mutated_base = True

                if scenario is not None and (not isinstance(scenario, str) or scenario.strip()):
                    from ..comparator import _apply_matrix_scenario_in_place, _parse_matrix_scenario

                    plan = _parse_matrix_scenario(scenario)
                    scenario_applied = _call_compat(
                        _apply_matrix_scenario_in_place, worker, plan, keep=eff_keep
                    )
                    mutated_base = True

                if mutated_base:
                    upd_ckpt = getattr(worker, "update_checkpoint", None)
                    if callable(upd_ckpt):
                        upd_ckpt()
                    base_snap = take_snapshot(worker)

                base_digest = _mut.capture_digest(worker)
                if orig_digest is None:
                    orig_digest = base_digest
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
                    v_ops = var.get("ops") or []

                    if dirty:
                        restore_snapshot(worker, base_snap, rebuild=not use_direct)
                        dirty = False

                    try:
                        if not isinstance(v_ops, list):
                            raise ValueError(f"Variant '{v_name}' 'ops' must be a list of batch operations.")
                        step_results: list[dict[str, Any]] = []
                        if v_ops:
                            dirty = True
                            step_results = _execute_batch_ops(worker, v_ops, _BatchCache(worker))

                        v_digest = (
                            capture_direct_digest(worker, base_digest=base_digest)
                            if use_direct
                            else _mut.capture_digest(worker)
                        )
                        v_diff = diff_digest(base_digest, v_digest)
                        v_calcs = _extract_compact_calcs(worker, kw_list) if kw_list else None

                        entry: dict[str, Any] = {
                            "name": v_name,
                            "ok": True,
                            "steps": step_results,
                            "summary": _compact_sweep_summary(v_digest),
                            "diffVsBase": v_diff,
                            "warnings": v_digest.get("warnings", []),
                        }
                        if v_calcs is not None:
                            entry["calcs"] = v_calcs
                        results.append(entry)
                    except CrossGameHallucinationError:
                        dirty = True
                        raise
                    except Exception as exc:
                        dirty = True
                        results.append({"name": v_name, "ok": False, "error": str(exc)})
            finally:
                if base_snap is not orig_snap:
                    drop_snapshot(worker, base_snap)
                restore_snapshot(worker, orig_snap, drop=True)

            if orig_snap.kind == "cow":
                restore_check = verify_cow_restored(worker, orig_digest or {})
            else:
                from ..analyzer import verify_restored

                restore_check = verify_restored(worker, orig_snap.token, orig_digest or {})
        finally:
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        baseline_payload: dict[str, Any] = {
            "summary": _compact_sweep_summary(base_digest),
            "warnings": base_digest.get("warnings", []),
        }
        if base_calcs is not None:
            baseline_payload["calcs"] = base_calcs

        return {
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


def trade_eval_items(
    worker: BridgeWorker,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delegate read-only batch trade evaluation to `trade_eval.eval_trade_items`."""
    from ..trade_eval import eval_trade_items

    return eval_trade_items(worker, slot_name=slot_name, items=items)
