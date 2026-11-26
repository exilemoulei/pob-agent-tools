# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 keyword-free full-slot unique ranking (including Flask 1..2 & Charm 1..3) + Sanctified 0.78x-1.22x (L2)."""

from __future__ import annotations

from typing import Any

from ..analyzer.version_stamp import attach_version_stamp
from ..errors import DaemonRequestError
from ..guardrails import GuardrailScope, check_input

_SLOT_ALIASES: dict[str, str] = {
    "weapon": "Weapon 1",
    "weapon1": "Weapon 1",
    "weapon 1": "Weapon 1",
    "weapon2": "Weapon 2",
    "weapon 2": "Weapon 2",
    "offhand": "Weapon 2",
    "weapon1swap": "Weapon 1 Swap",
    "weapon 1 swap": "Weapon 1 Swap",
    "weapon2swap": "Weapon 2 Swap",
    "weapon 2 swap": "Weapon 2 Swap",
    "helmet": "Helmet",
    "helm": "Helmet",
    "body": "Body Armour",
    "body armour": "Body Armour",
    "chest": "Body Armour",
    "gloves": "Gloves",
    "boots": "Boots",
    "amulet": "Amulet",
    "ring1": "Ring 1",
    "ring 1": "Ring 1",
    "ring": "Ring 1",
    "ring2": "Ring 2",
    "ring 2": "Ring 2",
    "belt": "Belt",
    "flask1": "Flask 1",
    "flask 1": "Flask 1",
    "flask": "Flask 1",
    "flask2": "Flask 2",
    "flask 2": "Flask 2",
    "charm1": "Charm 1",
    "charm 1": "Charm 1",
    "charm": "Charm 1",
    "charm2": "Charm 2",
    "charm 2": "Charm 2",
    "charm3": "Charm 3",
    "charm 3": "Charm 3",
}


def normalize_slot_arg(slot: str) -> str:
    raw = (slot or "").strip()
    if not raw:
        raise DaemonRequestError("`uniques` requires `--slot <slot>` (e.g. Ring 1, Amulet, Flask 1, Charm 1).")
    check_input(raw, GuardrailScope.SLOT, expected_game="poe2", emit_stderr=False)
    return _SLOT_ALIASES.get(raw.lower(), raw)


def rank_uniques(
    worker: Any,
    *,
    slot: str,
    stat: str | None = None,
    top: int = 15,
    eval_sanctified: bool = True,
    max_scan: int = 300,
) -> dict[str, Any]:
    """Keyword-free full-slot PoE2 unique ranking via Lua unique_rank_kernel."""
    canon_slot = normalize_slot_arg(slot)
    rpc_params: dict[str, Any] = {
        "slot": canon_slot,
        "top": max(1, min(int(top), 50)),
        "evalSanctified": bool(eval_sanctified),
        "maxScan": max(10, min(int(max_scan), 600)),
    }
    if stat:
        rpc_params["stat"] = str(stat).strip()
    with worker.lock:
        res = worker.call("rank_uniques_slot", rpc_params, timeout=60.0)
        info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        dict(res),
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = ["normalize_slot_arg", "rank_uniques"]
