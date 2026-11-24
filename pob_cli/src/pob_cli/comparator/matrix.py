"""Multi-scenario evaluation matrix (`run_matrix`)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..analyzer import dewater_build_in_place, verify_restored
from ..bridge_client import BridgeWorker
from ..mutators import _apply_config_in_place
from ..snapshot import _call_batch, capture_digest, compact_digest_summary, diff_digest

DEFAULT_MATRIX_SCENARIOS: list[str] = ["Original", "Pinnacle", "Uber", "FlasksOff"]

_MATRIX_TOKEN_SPLIT_RE = re.compile(
    r",\s*(?=[A-Za-z_][A-Za-z0-9_]*\s*=|(?:original|pinnacle|uber|flasks?_?(?:on|off)|no_?flasks)(?=\s*(?:,|$)))",
    re.IGNORECASE,
)


def _parse_matrix_scenario(spec: str | dict[str, Any]) -> dict[str, Any]:
    """Normalize a string or dict matrix scenario specification into a canonical execution plan."""
    if isinstance(spec, dict):
        name = str(spec.get("name") or "").strip()
        base_raw = str(spec.get("base") or "Original").strip()
        base_low = base_raw.lower()
        if base_low == "pinnacle":
            base = "Pinnacle"
        elif base_low == "uber":
            base = "Uber"
        elif base_low == "original":
            base = "Original"
        else:
            raise ValueError(
                f"Unsupported matrix base '{base_raw}' (expected 'Original', 'Pinnacle', or 'Uber')."
            )

        cfg_raw = spec.get("config") or {}
        if isinstance(cfg_raw, dict):
            config_pairs = list(cfg_raw.items())
        elif isinstance(cfg_raw, list):
            config_pairs = [(k, v) for k, v in cfg_raw]
        else:
            raise ValueError("Matrix scenario 'config' must be a dict or list of (key, value) pairs.")

        flasks = spec.get("flasks")
        if flasks is not None:
            flasks = bool(flasks)

        return {
            "name": name or base,
            "base": base,
            "config": config_pairs,
            "flasks": flasks,
            "customMods": spec.get("customMods"),
            "appendCustomMods": spec.get("appendCustomMods"),
        }

    raw = str(spec or "").strip()
    if not raw:
        raise ValueError("Matrix scenario specification cannot be empty.")

    raw_low = raw.lower()
    if raw_low == "original":
        return {"name": "Original", "base": "Original", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low == "pinnacle":
        return {"name": "Pinnacle", "base": "Pinnacle", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low == "uber":
        return {"name": "Uber", "base": "Uber", "config": [], "flasks": None, "customMods": None, "appendCustomMods": None}
    if raw_low in ("flasksoff", "flasks_off", "noflasks", "no_flasks"):
        return {"name": "FlasksOff", "base": "Original", "config": [], "flasks": False, "customMods": None, "appendCustomMods": None}
    if raw_low in ("flaskson", "flasks_on"):
        return {"name": "FlasksOn", "base": "Original", "config": [], "flasks": True, "customMods": None, "appendCustomMods": None}

    label = raw
    body = raw
    colon_idx = raw.find(":")
    eq_idx = raw.find("=")
    if colon_idx != -1 and (eq_idx == -1 or colon_idx < eq_idx):
        label = raw[:colon_idx].strip() or raw
        body = raw[colon_idx + 1 :].strip()

    base = "Original"
    flasks: bool | None = None
    config_pairs: list[tuple[str, Any]] = []
    custom_mods: str | None = None
    append_custom_mods: str | None = None

    tokens = [tok.strip() for tok in _MATRIX_TOKEN_SPLIT_RE.split(body) if tok.strip()]
    if not tokens:
        raise ValueError(f"Matrix scenario '{raw}' has an empty specification body.")

    for tok in tokens:
        t_low = tok.lower()
        if "=" in tok:
            k, v = tok.split("=", 1)
            k_clean = k.strip()
            v_clean = v.strip()
            if k_clean.lower() == "custommods":
                custom_mods = v_clean
            elif k_clean.lower() == "appendcustommods":
                append_custom_mods = v_clean
            elif k_clean.lower() == "flasks":
                flasks = v_clean.lower() in ("true", "1", "yes", "on")
            elif k_clean.lower() == "base":
                b_low = v_clean.lower()
                if b_low == "pinnacle":
                    base = "Pinnacle"
                elif b_low == "uber":
                    base = "Uber"
                elif b_low == "original":
                    base = "Original"
                else:
                    raise ValueError(f"Unsupported matrix base '{v_clean}' in scenario '{raw}'.")
            else:
                config_pairs.append((k_clean, v_clean))
        elif t_low == "original":
            base = "Original"
        elif t_low == "pinnacle":
            base = "Pinnacle"
        elif t_low == "uber":
            base = "Uber"
        elif t_low in ("flasksoff", "flasks_off", "noflasks", "no_flasks"):
            flasks = False
        elif t_low in ("flaskson", "flasks_on"):
            flasks = True
        else:
            raise ValueError(
                f"Unrecognized matrix token '{tok}' in scenario '{raw}'. "
                "Use Original/Pinnacle/Uber/FlasksOff or key=value pairs."
            )

    return {
        "name": label,
        "base": base,
        "config": config_pairs,
        "flasks": flasks,
        "customMods": custom_mods,
        "appendCustomMods": append_custom_mods,
    }


def _apply_matrix_scenario_in_place(
    worker: BridgeWorker,
    plan: dict[str, Any],
    keep: Iterable[str] | str | None = None,
    *,
    already_dewatered: bool = False,
    cached_stripped: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Apply a single parsed matrix scenario in place on the worker."""
    applied_info: dict[str, Any] = {
        "base": plan["base"],
        "stripped": cached_stripped if already_dewatered else None,
        "config": [],
        "flasks": plan["flasks"],
    }

    if plan["base"] == "Pinnacle":
        if not already_dewatered:
            applied_info["stripped"] = dewater_build_in_place(worker, keep=keep)
    elif plan["base"] == "Uber":
        if not already_dewatered:
            applied_info["stripped"] = dewater_build_in_place(worker, keep=keep)
        uber_cfg = _apply_config_in_place(worker, [("enemyIsBoss", "Uber")])
        applied_info["config"].extend(uber_cfg.get("assignments", []))

    if plan["flasks"] is not None:
        active_flag = bool(plan["flasks"])
        _call_batch(
            worker,
            [("set_slot_active", {"slotName": f"Flask {idx}", "active": active_flag}) for idx in range(1, 6)],
        )

    if plan["config"] or plan["customMods"] is not None or plan["appendCustomMods"] is not None:
        cfg_res = _apply_config_in_place(
            worker,
            plan["config"],
            custom_mods=plan["customMods"],
            append_custom_mods=plan["appendCustomMods"],
        )
        applied_info["config"].extend(cfg_res.get("assignments", []))

    return applied_info


def _plan_has_post_base_mutations(plan: dict[str, Any]) -> bool:
    return bool(
        plan["base"] == "Uber"
        or plan["flasks"] is not None
        or plan["config"]
        or plan["customMods"] is not None
        or plan["appendCustomMods"] is not None
    )


def run_matrix(
    worker: BridgeWorker,
    scenarios: list[str | dict[str, Any]] | None = None,
    *,
    full: bool = False,
    keep: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """Evaluate multiple build scenarios in a single atomic 0-residue transaction."""
    import pob_cli.comparator as _comp

    raw_scenarios = list(scenarios) if scenarios else list(DEFAULT_MATRIX_SCENARIOS)
    parsed_plans = [_parse_matrix_scenario(s) for s in raw_scenarios]
    pinnacle_count = sum(1 for p in parsed_plans if p["base"] in ("Pinnacle", "Uber"))
    first_is_unmutated_original = bool(
        parsed_plans
        and parsed_plans[0]["base"] == "Original"
        and not _plan_has_post_base_mutations(parsed_plans[0])
    )

    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        caps = getattr(worker, "caps", None) or {}
        use_cow = bool(caps.get("cowSnapshot"))
        try:
            baseline_snap_id: str | None = None
            pinnacle_snap_id: str | None = None
            baseline_code: str | None = None
            baseline_digest: dict[str, Any] | None = None
            results: list[dict[str, Any]] = []
            first_digest: dict[str, Any] | None = None
            pinnacle_code: str | None = None
            pinnacle_stripped: dict[str, list[str]] | None = None
            worker_state = "original"
            ever_mutated = False

            try:
                if use_cow:
                    baseline_snap_id = str(worker.call("cow_snapshot", {})["snapId"])
                else:
                    baseline_code = worker.call("export_code", {})["code"]
                baseline_digest = (
                    None if first_is_unmutated_original else capture_digest(worker)
                )
                for idx, plan in enumerate(parsed_plans):
                    needs_pinnacle = plan["base"] in ("Pinnacle", "Uber")
                    has_pinnacle_cache = (pinnacle_snap_id is not None) if use_cow else (pinnacle_code is not None)
                    want_base = "pinnacle" if (needs_pinnacle and has_pinnacle_cache) else "original"

                    if worker_state != want_base:
                        worker_state = "dirty"
                        if use_cow:
                            target_snap = pinnacle_snap_id if want_base == "pinnacle" else baseline_snap_id
                            worker.call("cow_restore", {"snapId": target_snap, "drop": False})
                        else:
                            target_code = pinnacle_code if want_base == "pinnacle" else baseline_code
                            worker.call("import_code", {"code": target_code, "mode": "replace"})
                        worker_state = want_base

                    has_post_mutations = _plan_has_post_base_mutations(plan)

                    if needs_pinnacle and not has_pinnacle_cache:
                        worker_state = "dirty"
                        ever_mutated = True
                        pinnacle_stripped = dewater_build_in_place(worker, keep=keep)
                        if pinnacle_count >= 2:
                            if use_cow:
                                pinnacle_snap_id = str(worker.call("cow_snapshot", {})["snapId"])
                            else:
                                pinnacle_code = worker.call("export_code", {})["code"]
                            if not has_post_mutations:
                                worker_state = "pinnacle"
                        applied_info = _comp._apply_matrix_scenario_in_place(
                            worker,
                            plan,
                            keep=keep,
                            already_dewatered=True,
                            cached_stripped=pinnacle_stripped,
                        )
                    elif needs_pinnacle and has_pinnacle_cache:
                        if has_post_mutations:
                            worker_state = "dirty"
                            ever_mutated = True
                        applied_info = _comp._apply_matrix_scenario_in_place(
                            worker,
                            plan,
                            keep=keep,
                            already_dewatered=True,
                            cached_stripped=pinnacle_stripped,
                        )
                    else:
                        if has_post_mutations:
                            worker_state = "dirty"
                            ever_mutated = True
                        applied_info = _comp._apply_matrix_scenario_in_place(worker, plan, keep=keep)

                    digest = capture_digest(worker)
                    if idx == 0 and first_is_unmutated_original:
                        baseline_digest = digest
                    if first_digest is None:
                        first_digest = digest
                    deltas_vs_first = diff_digest(first_digest, digest)

                    compact = compact_digest_summary(digest)
                    summary_only = {k: v for k, v in compact.items() if k != "coreStats"}
                    if full:
                        entry_digest = digest
                    else:
                        entry_digest = {
                            "primaryDpsKey": compact["primaryDpsKey"],
                            "primaryDps": compact["primaryDps"],
                            "totalEhp": compact["totalEhp"],
                            "life": compact["life"],
                            "warnings": compact["warnings"],
                        }

                    results.append(
                        {
                            "name": plan["name"],
                            "base": plan["base"],
                            "applied": applied_info,
                            "summary": summary_only,
                            "coreStats": compact["coreStats"],
                            "digest": entry_digest,
                            "deltasVsFirst": deltas_vs_first,
                        }
                    )
            finally:
                if use_cow:
                    if pinnacle_snap_id is not None:
                        try:
                            worker.call("cow_drop", {"snapId": pinnacle_snap_id})
                        except Exception:
                            pass
                    if baseline_snap_id is not None:
                        if worker_state != "original":
                            worker.call("cow_restore", {"snapId": baseline_snap_id, "drop": True})
                        else:
                            worker.call("cow_drop", {"snapId": baseline_snap_id})
                elif worker_state != "original":
                    worker.call("import_code", {"code": baseline_code, "mode": "replace"})

            if not ever_mutated:
                restore_check: dict[str, Any] = {"verified": True, "detail": "build was not mutated"}
            elif use_cow:
                from ..snapshot import verify_cow_restored

                restore_check = verify_cow_restored(worker, baseline_digest or {})
            else:
                restore_check = verify_restored(worker, baseline_code or "", baseline_digest or {})

            scenario_envelope: dict[str, Any] | None = None
            if caps.get("agentProbes"):
                try:
                    env_res = worker.call("eval_scenario_envelope", {})
                    if isinstance(env_res, dict) and not env_res.get("_error"):
                        scenario_envelope = env_res
                except Exception:
                    scenario_envelope = None
        finally:
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good

        out_mat: dict[str, Any] = {
            "baseline": results[0]["name"] if results else "Original",
            "full": bool(full),
            "scenarios": results,
            "restoreCheck": restore_check,
        }
        if scenario_envelope is not None:
            out_mat["scenarioEnvelope"] = scenario_envelope
        return out_mat
