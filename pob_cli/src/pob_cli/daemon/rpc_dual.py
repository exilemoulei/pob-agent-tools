"""Dual-slot RPC dispatch handlers (`compare`, `tree_diff`, `borrow`, `diagnose`) for `DaemonManager`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..snapshot import history_of
from .read_cache import compute_source_hash, is_live_source
from .rpc_mutate import norm_keep
from .runtime import normalize_slot_name

if TYPE_CHECKING:
    from .slots import DaemonManager


def same_slot_error(action: str, slot: str) -> str:
    """Why a two-slot command refuses one slot named twice, in that command's own flags."""
    if action == "tree_diff":
        return (
            f"`pob tree diff` compares two build slots, and both are '{slot}'. Pass a different "
            "`--against`, or `--spec-a` / `--spec-b` to compare two tree specs of this one build."
        )
    return (
        f"The reference slot must differ from the main slot (both are '{slot}'): loading the reference "
        "would overwrite the main build and clear its undo history. Pass a different `--ref-target`."
    )


def dispatch_dual_slot_action(
    manager: DaemonManager,
    action: str,
    params: dict[str, Any],
    mw: BridgeWorker,
    rw: BridgeWorker,
) -> dict[str, Any]:
    from ..comparator import borrow_from_ref, compare_builds

    if action == "compare":
        cmp_res = compare_builds(
            mw,
            rw,
            ref_source=params.get("ref_source"),
            ref_xml_code=params.get("ref_xml_code"),
            ref_stage=params.get("ref_stage"),
            ref_spec=params.get("ref_spec"),
            ref_item_set=params.get("ref_item_set"),
            ref_skill_set=params.get("ref_skill_set"),
            keep=norm_keep(params),
            auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
        )
        return manager._attach_state_meta(cmp_res, mw)

    if action == "tree_diff":
        from ..analyzer.tree_diff import diff_tree_slots

        label_a = normalize_slot_name(params.get("main_target") or params.get("target") or getattr(mw, "slot_name", None))
        label_b = normalize_slot_name(params.get("ref_target") or getattr(rw, "slot_name", None) or "ref")
        return diff_tree_slots(mw, rw, label_a=label_a, label_b=label_b)

    if action == "borrow":
        if not rw.last_good_code:
            ref_slot = getattr(rw, "slot_name", None) or "ref"
            raise DaemonRequestError(
                f"No reference build loaded in '{ref_slot}' slot. "
                f"Run `pob load <source> --target {ref_slot}` or `pob compare <source>` first."
            )
        b_res = borrow_from_ref(
            mw,
            rw,
            item_slots=params.get("item_slots"),
            skill_group=params.get("skill_group"),
            apply=bool(params.get("apply", False)),
        )
        return manager._attach_state_meta(b_res, mw)

    if action == "diagnose":
        from ..analyzer import diagnose_compare_builds

        top_val = int(params["top"]) if params.get("top") is not None else 5
        main_slot_name = normalize_slot_name(
            params.get("main_target") or params.get("target") or getattr(mw, "slot_name", None)
        )
        default_ref_name = "ref" if main_slot_name == "main" else f"{main_slot_name}_ref"
        ref_slot_name = normalize_slot_name(
            params.get("ref_target") or getattr(rw, "slot_name", None) or default_ref_name
        )
        read_cache = getattr(manager, "_read_cache", None)
        main_load_params = {
            "source": params.get("source"),
            "xml_code": params.get("xml_code"),
            "char_name": params.get("char_name"),
            "league": params.get("league"),
            "realm": params.get("realm"),
            "stage": params.get("stage"),
            "spec": params.get("spec"),
            "item_set": params.get("item_set"),
            "skill_set": params.get("skill_set"),
            "source_hash": params.get("source_hash"),
        }
        ref_load_params = {
            "source": params.get("compare_source"),
            "xml_code": params.get("compare_xml_code"),
            "stage": params.get("ref_stage"),
            "spec": params.get("ref_spec"),
            "item_set": params.get("ref_item_set"),
            "skill_set": params.get("ref_skill_set"),
            "source_hash": params.get("compare_source_hash"),
        }
        main_hash = compute_source_hash(main_load_params)
        ref_hash = compute_source_hash(ref_load_params)
        skip_main = bool(
            read_cache is not None
            and main_hash
            and read_cache.matching_slot_load(
                main_slot_name, history_of(mw).rev, bool(getattr(mw, "last_good_code", None)), main_hash
            )
            is not None
        )
        skip_ref = bool(
            read_cache is not None
            and ref_hash
            and read_cache.matching_slot_load(
                ref_slot_name, history_of(rw).rev, bool(getattr(rw, "last_good_code", None)), ref_hash
            )
            is not None
        )
        diag_cmp = diagnose_compare_builds(
            mw,
            rw,
            source=None if skip_main else params.get("source"),
            xml_code=None if skip_main else params.get("xml_code"),
            compare_source=None if skip_ref else params.get("compare_source"),
            compare_xml_code=None if skip_ref else params.get("compare_xml_code"),
            stage=None if skip_main else params.get("stage"),
            spec=None if skip_main else params.get("spec"),
            item_set=None if skip_main else params.get("item_set"),
            skill_set=None if skip_main else params.get("skill_set"),
            ref_stage=None if skip_ref else params.get("ref_stage"),
            ref_spec=None if skip_ref else params.get("ref_spec"),
            ref_item_set=None if skip_ref else params.get("ref_item_set"),
            ref_skill_set=None if skip_ref else params.get("ref_skill_set"),
            char_name=params.get("char_name"),
            league=params.get("league"),
            realm=None if skip_main else params.get("realm"),
            keep=norm_keep(params),
            auto_keep=not bool(params.get("no_auto_keep", params.get("noAutoKeep", False))),
            top=top_val,
            no_rank=bool(params.get("no_rank", params.get("noRank", True))),
            ask=params.get("ask"),
            pob_dir=manager.pob_dir,
            runtime_dir=manager.runtime_dir,
            target=main_slot_name,
            ref_target=ref_slot_name,
        )
        if read_cache is not None:
            if main_hash and not skip_main:
                read_cache.record_slot_load(
                    main_slot_name, history_of(mw).rev, main_hash, live=is_live_source(main_load_params)
                )
            if ref_hash and not skip_ref:
                read_cache.record_slot_load(
                    ref_slot_name, history_of(rw).rev, ref_hash, live=is_live_source(ref_load_params)
                )
        return manager._attach_warning_if_degraded(diag_cmp)

    raise ValueError(f"Unknown dual-slot daemon action '{action}'.")
