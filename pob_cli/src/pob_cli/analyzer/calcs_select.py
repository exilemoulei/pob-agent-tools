"""`pob calc --group/--skill`: point the Calcs tab at another skill for one read, and put it back."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..bridge_protocol import BridgeError
from ..formatters import strip_pob_escapes
from ..game_data import fold_han
from .inspectors import resolve_skill_group_index


def _active_skills_of(grp: dict[str, Any]) -> list[dict[str, Any]]:
    raw = grp.get("skills")
    if isinstance(raw, list) and raw:
        return [{"index": int(s.get("index") or i), "name": s.get("name"), "nameZh": s.get("nameZh")} for i, s in enumerate(raw, 1) if isinstance(s, dict)]
    gems = [g for g in (grp.get("gems") or []) if isinstance(g, dict) and not g.get("support") and g.get("enabled", True)]
    return [{"index": i, "name": g.get("name") or g.get("nameSpec"), "nameZh": g.get("nameZh")} for i, g in enumerate(gems, 1)]


def _flbl(v: Any) -> str:
    return fold_han(strip_pob_escapes(str(v or "")).strip().lower())


def _resolve_calcs_target(skills_res: dict[str, Any], group: str | int | None, skill: str | int | None) -> tuple[int, int | None]:
    groups = [g for g in (skills_res.get("groups") or []) if isinstance(g, dict)]
    if group is not None and str(group).strip() != "":
        g_idx = resolve_skill_group_index(skills_res, group)
        grp = next((g for g in groups if int(g.get("index") or 0) == g_idx), groups[g_idx - 1] if 1 <= g_idx <= len(groups) else {})
        s_str = str(skill or "").strip()
        if not s_str:
            return g_idx, None
        if s_str.lower() == "main":
            return g_idx, int(grp.get("mainActiveSkill") or 1)
        acts = _active_skills_of(grp)
        if s_str.isdigit():
            idx = int(s_str)
            if 1 <= idx <= max(1, len(acts)):
                return g_idx, idx
            raise ValueError(f"Active skill index {idx} out of range for group #{g_idx} ({len(acts)} active).")
        q = _flbl(s_str)
        for exact in (True, False):
            for a in acts:
                nms = (_flbl(a.get("name")), _flbl(a.get("nameZh")))
                if (q in nms) if exact else any(q in n for n in nms if n):
                    return g_idx, int(a["index"])
        raise ValueError(f"Active skill '{s_str}' not found in skill group #{g_idx}.")

    s_raw = str(skill or "").strip()
    if s_raw.isdigit() or s_raw.lower() == "main":
        return resolve_skill_group_index(skills_res, s_raw), None
    q, best = _flbl(s_raw), None
    for pos, g in enumerate(groups, 1):
        g_idx = int(g.get("index") or pos)
        tie = (0 if g.get("enabled", True) else 2) + (0 if g.get("isMain") else 1)
        for a in _active_skills_of(g):
            nms = (_flbl(a.get("name")), _flbl(a.get("nameZh")))
            rk = 0 if q in nms else (2 if any(q in n for n in nms if n) else -1)
            if rk >= 0 and (best is None or (rk, tie, pos) < best[:3]):
                best = (rk, tie, pos, g_idx, int(a["index"]))
        lbls = [_flbl(g.get(k)) for k in ("label", "displayLabel", "displayLabelZh")]
        lbls.extend(_flbl(gm.get(k)) for gm in (g.get("gems") or []) if isinstance(gm, dict) for k in ("name", "nameZh", "nameSpec"))
        rk = 1 if q in lbls else (3 if any(q in lb for lb in lbls if lb) else -1)
        if rk >= 0 and (best is None or (rk, tie, pos) < best[:3]):
            best = (rk, tie, pos, g_idx, None)
    if best is not None:
        return best[3], best[4]
    raise ValueError(f"Skill or group matching '{s_raw}' not found.")


def select_calcs_target(w: BridgeWorker, group: str | int | None, skill: str | int | None, restores: list[dict[str, Any]]) -> dict[str, Any]:
    skills_res, init_c = w.call("list_skills", {}), w.call("get_calcs", {})
    sel = init_c.get("selectors") if isinstance(init_c.get("selectors"), dict) else {}
    o_grp, o_sk = ((sel.get(k) or {}).get("index") if isinstance(sel.get(k), dict) else None for k in ("mainSocketGroup", "mainSkill"))
    if o_grp is None:  # read-only: nothing is changed that cannot be put back
        raise ValueError("This engine does not report the Calcs selection; --group/--skill need it.")
    t_grp, t_sk = _resolve_calcs_target(skills_res, group, skill)
    need_g = int(t_grp) != int(o_grp)
    need_s = t_sk is not None and (need_g or o_sk is None or int(t_sk) != int(o_sk))
    if not (need_g or need_s):
        return init_c
    if o_sk is not None:
        restores.append({"var": "mainActiveSkill", "value": int(o_sk)})
    if need_g:
        restores.insert(0, {"var": "skill_number", "value": int(o_grp)})
    if need_g:
        w.call("set_calcs_input", {"var": "skill_number", "value": int(t_grp)})
        if t_sk is not None:
            # The target group's own Calcs-tab skill goes back too; only the selectors report it.
            go = ((w.call("get_calcs", {}).get("selectors") or {}).get("mainSkill") or {}).get("index")
            if go is not None and int(go) != int(t_sk):
                restores.insert(0, {"var": "mainActiveSkill", "value": int(go)})
    if need_s and t_sk is not None:
        w.call("set_calcs_input", {"var": "mainActiveSkill", "value": int(t_sk)})
    return w.call("get_calcs", {})


def restore_calcs_target(w: BridgeWorker, restores: list[dict[str, Any]], *, quiet: bool = False) -> None:
    """Run every queued restore; a failed one is reported, because the command is read-only.

    `quiet` is for a caller that is already failing: its own error says more than the restore's.
    """
    first: Exception | None = None
    for rc in restores:
        try:
            w.call("set_calcs_input", rc)
        except Exception as exc:
            first = first or exc
    if first is not None and not quiet:
        raise BridgeError(f"The Calcs tab could not be put back as it was after --group/--skill: {first}") from first
