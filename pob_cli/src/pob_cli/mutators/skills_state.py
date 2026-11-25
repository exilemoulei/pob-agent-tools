"""Per-gem state (skill part, stages, minion, global-effect flags) carried across a group edit."""

from __future__ import annotations

from typing import Any

_CARRY_GEM_STATE_FIELDS: tuple[str, ...] = (
    "skillPart",
    "skillPartCalcs",
    "skillStageCount",
    "skillStageCountCalcs",
    "skillMineCount",
    "skillMineCountCalcs",
    "skillMinion",
    "skillMinionCalcs",
    "skillMinionSkill",
    "skillMinionSkillCalcs",
    "skillMinionItemSet",
    "skillMinionItemSetCalcs",
    "statSet",
    "statSetCalcs",
    "enableGlobal1",
    "enableGlobal2",
)


def _build_explicit_gem_state(
    index: int,
    *,
    skill_part: int | None = None,
    stage_count: int | None = None,
) -> dict[str, Any] | None:
    """Build a single `gemStates` entry for `apply_group_gem_states` from explicit `skill_part` / `stage_count`."""
    entry: dict[str, Any] = {"index": int(index)}
    if skill_part is not None:
        entry["skillPart"] = int(skill_part)
        entry["skillPartCalcs"] = int(skill_part)
    if stage_count is not None:
        entry["skillStageCount"] = int(stage_count)
        entry["skillStageCountCalcs"] = int(stage_count)
    return entry if len(entry) > 1 else None


def _build_specs_gem_states(
    resolved_specs: list[dict[str, Any]],
    added_gems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build `gemStates` entries for newly added gems that explicitly specified `skill_part` or `stage_count`."""
    states: list[dict[str, Any]] = []
    for pos, (sp, added) in enumerate(zip(resolved_specs, added_gems, strict=False)):
        gem_obj = added.get("gem") if isinstance(added.get("gem"), dict) else {}
        idx = int(gem_obj.get("index") or (pos + 1))
        entry = _build_explicit_gem_state(
            idx,
            skill_part=sp.get("skill_part"),
            stage_count=sp.get("stage_count"),
        )
        if entry is not None:
            states.append(entry)
    return states


def _build_replaced_group_state_params(
    g_idx: int,
    prior_state: dict[str, Any] | None,
    resolved_specs: list[dict[str, Any]],
    added_gems: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Merge snapshotted prior gem states, explicit spec overrides, and prior active-skill selection."""
    prior_gems = list(prior_state.get("gems") or []) if isinstance(prior_state, dict) else []
    used_prior: set[int] = set()
    gem_states: list[dict[str, Any]] = []
    new_gem_ids: set[str] = set()
    new_gem_names: set[str] = set()

    for pos, (sp, added) in enumerate(zip(resolved_specs, added_gems, strict=False)):
        gem_obj = added.get("gem") if isinstance(added.get("gem"), dict) else {}
        new_idx = int(gem_obj.get("index") or (pos + 1))
        gid = str(gem_obj.get("gemId") or sp.get("gemId") or "").strip()
        if gid:
            new_gem_ids.add(gid)
        cand_names = {
            str(x).strip().lower()
            for x in (gem_obj.get("name"), gem_obj.get("nameSpec"), sp.get("name"))
            if x and str(x).strip()
        }
        new_gem_names.update(cand_names)

        matched_pg: dict[str, Any] | None = None
        if gid:
            for pi, pg in enumerate(prior_gems):
                if pi not in used_prior and isinstance(pg, dict) and str(pg.get("gemId") or "").strip() == gid:
                    used_prior.add(pi)
                    matched_pg = pg
                    break
        if matched_pg is None and cand_names:
            for pi, pg in enumerate(prior_gems):
                if pi in used_prior or not isinstance(pg, dict):
                    continue
                pg_name = str(pg.get("nameSpec") or pg.get("name") or "").strip().lower()
                if pg_name and pg_name in cand_names:
                    used_prior.add(pi)
                    matched_pg = pg
                    break

        entry: dict[str, Any] = {"index": new_idx}
        if matched_pg is not None:
            for field in _CARRY_GEM_STATE_FIELDS:
                if matched_pg.get(field) is not None:
                    entry[field] = matched_pg[field]
        if sp.get("skill_part") is not None:
            sp_val = int(sp["skill_part"])
            entry["skillPart"] = sp_val
            entry["skillPartCalcs"] = sp_val
        if sp.get("stage_count") is not None:
            sc_val = int(sp["stage_count"])
            entry["skillStageCount"] = sc_val
            entry["skillStageCountCalcs"] = sc_val
        for flag in ("enableGlobal1", "enableGlobal2"):
            if sp.get(flag) is not None:
                entry[flag] = bool(sp[flag])  # named in the spec: not the replaced gem's setting
        if len(entry) > 1:
            gem_states.append(entry)

    params: dict[str, Any] = {"group": g_idx}
    if gem_states:
        params["gemStates"] = gem_states

    if isinstance(prior_state, dict) and resolved_specs:
        for idx_key, gid_key, name_key, sid_key in (
            ("mainActiveSkill", "mainActiveGemId", "mainActiveNameSpec", "mainActiveSkillId"),
            ("mainActiveSkillCalcs", "mainActiveCalcsGemId", "mainActiveCalcsNameSpec", "mainActiveCalcsSkillId"),
        ):
            p_idx = prior_state.get(idx_key)
            a_gid = str(prior_state.get(gid_key) or "").strip()
            a_name = str(prior_state.get(name_key) or "").strip()
            a_sid = str(prior_state.get(sid_key) or "").strip()
            has_id = bool(a_gid or a_name)
            matches_new = (bool(a_gid) and a_gid in new_gem_ids) or (
                bool(a_name) and a_name.lower() in new_gem_names
            )
            if matches_new or (not has_id and isinstance(p_idx, int) and p_idx > 1):
                if isinstance(p_idx, int) and p_idx >= 1:
                    params[idx_key] = p_idx
                if a_gid:
                    params[gid_key] = a_gid
                if a_name:
                    params[name_key] = a_name
                if a_sid:
                    params[sid_key] = a_sid

    return params if len(params) > 1 else None
