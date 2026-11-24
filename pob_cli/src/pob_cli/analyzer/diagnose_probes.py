"""Live PoB engine probes for trigger attack APS measurement and 0%-delta dead support detection."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch, _finite_float
from .constants import _DEAD_SUPPORT_EXEMPT_NAMES, _TRIGGER_ATTACK_HINTS
from .summary import _normalize_support_base_name


def _extract_trigger_attack_aps(
    worker: BridgeWorker,
    skills_res: dict[str, Any],
    items_res: dict[str, Any],
    base_stats: dict[str, Any],
) -> float | None:
    """Measure the real PoB Attack Rate (`Speed`) of the trigger attack in CoC/Cospri/Mjolner/Poet's Pen setups."""
    groups = skills_res.get("groups") or []
    main_idx = skills_res.get("mainSocketGroup")
    main_group = next(
        (g for g in groups if g.get("isMain") or (main_idx is not None and g.get("index") == main_idx)),
        None,
    )
    if not main_group or not main_group.get("enabled", True):
        return None

    mg_gems = [gm for gm in (main_group.get("gems") or []) if gm.get("enabled", True)]
    has_coc = any(
        "cast on critical strike" in strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
        or str(gm.get("skillId") or "") in ("SupportCastOnCriticalStrike", "SupportAwakenedCastOnCriticalStrike")
        for gm in mg_gems
    )
    items_by_id = {it["id"]: it for it in (items_res.get("items") or [])}
    has_atk_trigger_item = False
    for s in items_res.get("slots") or []:
        if s.get("selItemId") and not s.get("inactive") and "Swap" not in str(s.get("name") or ""):
            it = items_by_id.get(s["selItemId"], {})
            nm = strip_pob_escapes(str(it.get("title") or it.get("name") or "")).lower()
            if any(k in nm for k in ("cospri's malice", "mjölner", "mjolner", "the poet's pen")):
                has_atk_trigger_item = True
                break
    if not has_coc and not has_atk_trigger_item:
        return None

    skills_list = main_group.get("skills") or []
    orig_active = int(main_group.get("mainActiveSkill") or 1)
    if skills_list and 1 <= orig_active <= len(skills_list):
        cur_sk = skills_list[orig_active - 1]
        cur_nm = strip_pob_escapes(str(cur_sk.get("name") or cur_sk.get("skillId") or "")).lower()
        if any(h in cur_nm for h in _TRIGGER_ATTACK_HINTS):
            sp = _finite_float(base_stats.get("Speed"), default=0.0)
            return sp if sp > 0 else None
        for idx_0, sk in enumerate(skills_list):
            sk_nm = strip_pob_escapes(str(sk.get("name") or sk.get("skillId") or "")).lower()
            if any(h in sk_nm for h in _TRIGGER_ATTACK_HINTS):
                atk_idx = idx_0 + 1
                g_idx = int(main_group["index"])
                try:
                    with worker.lock:
                        res_list = _call_batch(
                            worker,
                            [
                                ("set_group", {"index": g_idx, "mainActiveSkill": atk_idx}),
                                ("get_stats", {}),
                                ("set_group", {"index": g_idx, "mainActiveSkill": orig_active}),
                            ],
                            raise_on_error=False,
                        )
                    if len(res_list) >= 2 and isinstance(res_list[1], dict) and isinstance(res_list[1].get("stats"), dict):
                        sp = _finite_float(res_list[1]["stats"].get("Speed"), default=0.0)
                        if sp > 0:
                            return sp
                except Exception:
                    try:
                        worker.call("set_group", {"index": g_idx, "mainActiveSkill": orig_active})
                    except Exception:
                        pass
                break

    if has_atk_trigger_item:
        orig_main_idx = int(main_group["index"])
        for g in groups:
            if not g.get("enabled", True) or "Swap" in str(g.get("slot") or "") or int(g.get("index") or 0) == orig_main_idx:
                continue
            for gm in g.get("gems") or []:
                if gm.get("enabled", True) and not gm.get("support"):
                    gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
                    if any(h in gnm for h in _TRIGGER_ATTACK_HINTS):
                        try:
                            with worker.lock:
                                res_list = _call_batch(
                                    worker,
                                    [
                                        ("set_build_field", {"field": "mainSocketGroup", "value": int(g["index"])}),
                                        ("get_stats", {}),
                                        ("set_build_field", {"field": "mainSocketGroup", "value": orig_main_idx}),
                                    ],
                                    raise_on_error=False,
                                )
                            if len(res_list) >= 2 and isinstance(res_list[1], dict) and isinstance(res_list[1].get("stats"), dict):
                                sp = _finite_float(res_list[1]["stats"].get("Speed"), default=0.0)
                                if sp > 0:
                                    return sp
                        except Exception:
                            try:
                                worker.call("set_build_field", {"field": "mainSocketGroup", "value": orig_main_idx})
                            except Exception:
                                pass
                        break
    return None


def _detect_dead_main_supports(
    worker: BridgeWorker,
    main_group: dict[str, Any] | None,
    base_stats: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Engine-verified 0% dead support detector on `mainGroup` via one batched toggle+restore pass."""
    if not main_group or not main_group.get("enabled", True):
        return [], []
    mg_idx = main_group.get("index")
    if mg_idx is None:
        return [], []

    base_comb = _finite_float(base_stats.get("CombinedDPS"), default=0.0)
    base_full = _finite_float(base_stats.get("FullDPS"), default=0.0)
    base_tot = _finite_float(base_stats.get("TotalDPS"), default=0.0)
    base_ehp = _finite_float(base_stats.get("TotalEHP"), default=0.0)
    base_mana_cost = _finite_float(base_stats.get("ManaCost"), default=-1.0)
    if base_comb <= 0.0 and base_full <= 0.0 and base_tot <= 0.0:
        return [], []

    candidates: list[dict[str, Any]] = []
    for gm in main_group.get("gems") or []:
        if not gm.get("enabled", True) or not gm.get("support"):
            continue
        if gm.get("fromItem") or gm.get("fromNode"):
            continue
        if gm.get("index") is None:
            continue
        gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
        norm = _normalize_support_base_name(gnm)
        if not norm or norm in _DEAD_SUPPORT_EXEMPT_NAMES:
            continue
        if norm == "inspiration" and base_mana_cost > 0.0:
            continue
        candidates.append(gm)

    if not candidates:
        return [], []

    batch_calls: list[tuple[str, dict[str, Any]]] = []
    for gm in candidates:
        g_ref = {"group": int(mg_idx), "index": int(gm["index"])}
        batch_calls.append(("set_gem", {**g_ref, "enabled": False}))
        batch_calls.append(("get_stats", {}))
        batch_calls.append(("set_gem", {**g_ref, "enabled": True}))

    try:
        with worker.lock:
            responses = _call_batch(worker, batch_calls, raise_on_error=False)
    except Exception:
        for gm in candidates:
            try:
                worker.call("set_gem", {"group": int(mg_idx), "index": int(gm["index"]), "enabled": True})
            except Exception:
                pass
        return [], []

    def _unchanged(a: float, b: float) -> bool:
        return abs(a - b) <= 1e-5 * max(1.0, abs(b))

    dead_gaps: list[dict[str, str]] = []
    dead_records: list[dict[str, Any]] = []
    for i, gm in enumerate(candidates):
        res_off = responses[i * 3] if (i * 3) < len(responses) else None
        res_stats = responses[i * 3 + 1] if (i * 3 + 1) < len(responses) else None
        if not isinstance(res_off, dict) or res_off.get("_error") or res_off.get("ok") is False:
            continue
        if not isinstance(res_stats, dict) or res_stats.get("_error") or not isinstance(res_stats.get("stats"), dict):
            continue
        st_off = res_stats["stats"]
        off_comb = _finite_float(st_off.get("CombinedDPS"), default=base_comb)
        off_full = _finite_float(st_off.get("FullDPS"), default=base_full)
        off_tot = _finite_float(st_off.get("TotalDPS"), default=base_tot)
        off_ehp = _finite_float(st_off.get("TotalEHP"), default=base_ehp)

        if (
            _unchanged(off_comb, base_comb)
            and _unchanged(off_full, base_full)
            and _unchanged(off_tot, base_tot)
            and _unchanged(off_ehp, base_ehp)
        ):
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            gnm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip()
            disp = f"{gnm_zh} ({gnm})" if (gnm_zh and gnm_zh != gnm) else gnm
            norm = _normalize_support_base_name(gnm)
            hint = ""
            if norm == "hypothermia":
                hint = " (requires Enemy Chilled — blocked by Elemental Focus or missing Chill source)"
            elif norm == "immolate":
                hint = " (requires Enemy Burning/Ignited)"
            elif norm == "inspiration":
                hint = " (0-mana group generates 0 Inspiration Charges)"
            dead_records.append(
                {
                    "groupIndex": int(mg_idx),
                    "gemIndex": int(gm["index"]),
                    "name": gnm,
                    "nameZh": gnm_zh or None,
                    "deltaCombinedDpsPct": 0.0,
                    "deltaFullDpsPct": 0.0,
                    "deltaTotalEhpPct": 0.0,
                }
            )
            dead_gaps.append(
                {
                    "category": "DeadSupport",
                    "severity": "HIGH",
                    "message": (
                        f"Engine-Verified 0% Dead Support in Group #{mg_idx}: '{disp}' contributes 0.0% "
                        f"CombinedDPS, 0.0% FullDPS, and 0.0% TotalEHP when toggled off{hint} — replace with an active damage support!"
                    ),
                }
            )

    return dead_gaps, dead_records
