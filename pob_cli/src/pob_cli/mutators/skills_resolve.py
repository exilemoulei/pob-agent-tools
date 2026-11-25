"""Gem candidate resolution, spec normalization, and Vaal skill default flags."""

from __future__ import annotations

from typing import Any

from .. import game_data as _game_data
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from .common import _call_batch, _fold_search_text, _is_batch_error, _merge_cautions, _validate_gem_level


def _resolve_gem_in_group(group: dict[str, Any], gem_selector: str | int) -> dict[str, Any]:
    gems = group.get("gems", [])
    s = str(gem_selector).strip()
    target_gem = None
    if s.isdigit():
        idx = int(s)
        target_gem = next((gm for gm in gems if gm["index"] == idx), None)
    else:
        s_low = s.lower()
        s_fold = _fold_search_text(s)
        for gm in gems:
            names = [
                str(gm.get("nameSpec") or ""),
                str(gm.get("name") or ""),
                str(gm.get("nameZh") or ""),
            ]
            if any(s_low in n.lower() or (s_fold and s_fold in _fold_search_text(n)) for n in names):
                target_gem = gm
                break
    if not target_gem:
        raise ValueError(f"Gem '{gem_selector}' not found in skill group #{group.get('index')}.")
    return target_gem


def _gem_search_fallback_query(gem_name: str) -> str | None:
    """Strip common 'Support' / '(辅)' / '辅助' suffixes that PoB omits in its internal gem DB."""
    s = gem_name.strip()
    s_low = s.lower()
    if s_low.endswith(" support") and len(s) > len(" support"):
        return s[:-8].strip()
    for zh_suf in ("(辅)", "（辅）", "辅助"):
        if s.endswith(zh_suf) and len(s) > len(zh_suf):
            return s[: -len(zh_suf)].strip()
    return None


def _pick_best_gem_candidate(candidates: list[dict[str, Any]], gem_name: str) -> dict[str, Any] | None:
    if not candidates:
        return None
    q_low = gem_name.strip().lower()
    q_fold = _fold_search_text(gem_name)
    fb = _gem_search_fallback_query(gem_name)
    fb_low = fb.lower() if fb else ""
    for c in candidates:
        n_en = str(c.get("name") or "").lower()
        n_zh = str(c.get("nameZh") or "").lower()
        if (
            q_low in (n_en, n_zh)
            or (fb_low and fb_low in (n_en, n_zh))
            or (q_fold and q_fold in (_fold_search_text(c.get("name")), _fold_search_text(c.get("nameZh"))))
        ):
            return c
    return candidates[0]


def _resolve_gem_candidate(w: BridgeWorker, g_idx: int, gem_name: str) -> dict[str, Any] | None:
    search_res = w.call(
        "gem_search",
        {"group": g_idx, "query": gem_name.strip(), "byDps": False, "supportableOnly": False, "limit": 20},
    )
    cand = _pick_best_gem_candidate(search_res.get("gems", []), gem_name)
    if cand is not None:
        return cand
    fb_query = _gem_search_fallback_query(gem_name)
    if fb_query:
        fb_res = w.call(
            "gem_search",
            {"group": g_idx, "query": fb_query, "byDps": False, "supportableOnly": False, "limit": 20},
        )
        cand = _pick_best_gem_candidate(fb_res.get("gems", []), gem_name)
        if cand is not None:
            return cand
    # Simplified and Traditional translations can name a gem differently; retry by English name.
    q_en = _game_data.english_for_zh(gem_name, pob_dir=getattr(w, "pob_root", None))
    if q_en:
        en_res = w.call(
            "gem_search",
            {"group": g_idx, "query": q_en, "byDps": False, "supportableOnly": False, "limit": 20},
        )
        return _pick_best_gem_candidate(en_res.get("gems", []), q_en)
    return None


def _extract_explicit_gem_level(entry: str | dict[str, Any]) -> int | None:
    """Return the explicitly requested gem level from a spec, or None if omitted."""
    if isinstance(entry, str):
        raw = entry.strip()
        parts = raw.rsplit(":", 2)
        if len(parts) == 3 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
            return int(parts[1].strip())
        parts2 = raw.rsplit(":", 1)
        if len(parts2) == 2 and parts2[1].strip().isdigit():
            return int(parts2[1].strip())
        return None
    if isinstance(entry, dict) and entry.get("level") is not None:
        return int(entry["level"])
    return None


_VALID_GEM_SPEC_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "nameSpec",
        "nameZh",
        "gem",
        "gemId",
        "id",
        "skillId",
        "variant",
        "qualityId",
        "index",
        "support",
        "level",
        "quality",
        "enabled",
        "count",
        "allowIllegalLevel",
        "allow_illegal_level",
        "allowLegacy",
        "allow_legacy",
        "skillPart",
        "skill_part",
        "stageCount",
        "stage_count",
        "skillStageCount",
        "enableGlobal1",
        "enableGlobal2",
    }
)

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


def _parse_positive_int(val: Any, field_name: str) -> int | None:
    """Validate and convert an optional positive integer (`>= 1`) such as `skillPart` or `stageCount`."""
    if val is None:
        return None
    if isinstance(val, bool) or (isinstance(val, float) and not val.is_integer()):
        raise ValueError(f"'{field_name}' must be a positive integer (>= 1), got {val!r}.")
    try:
        iv = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"'{field_name}' must be a positive integer (>= 1), got {val!r}.") from None
    if iv < 1:
        raise ValueError(f"'{field_name}' must be a positive integer (>= 1), got {iv}.")
    return iv


def _normalize_gem_spec(
    entry: str | dict[str, Any],
    *,
    allow_illegal_level: bool = False,
) -> dict[str, Any]:
    """Normalize a gem spec (`dict` or `'Name[:level[:quality]]'` string) into a canonical dict."""
    if isinstance(entry, str):
        raw = entry.strip()
        if not raw:
            raise ValueError("Gem specification string cannot be empty.")
        explicit_level: int | None = None
        quality = 20
        parts = raw.rsplit(":", 2)
        if len(parts) == 3 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
            name = parts[0].strip()
            explicit_level = int(parts[1].strip())
            quality = int(parts[2].strip())
        else:
            parts2 = raw.rsplit(":", 1)
            if len(parts2) == 2 and parts2[1].strip().isdigit():
                name = parts2[0].strip()
                explicit_level = int(parts2[1].strip())
            else:
                name = raw
        if not name:
            raise ValueError(f"Invalid gem specification '{entry}'.")
        eff_level, cautions = _validate_gem_level(name, explicit_level, allow_illegal_level=allow_illegal_level)
        out: dict[str, Any] = {
            "name": name,
            "gemId": None,
            "level": eff_level if eff_level is not None else 20,
            "quality": quality,
            "enabled": True,
            "count": 1,
        }
        if cautions:
            out["cautions"] = cautions
        return out

    if isinstance(entry, dict):
        unknown = sorted(k for k in entry if k not in _VALID_GEM_SPEC_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown key(s) {unknown} in gem spec dict "
                f"(valid keys: {', '.join(sorted(_VALID_GEM_SPEC_KEYS))})."
            )
        name = str(entry.get("name") or entry.get("nameSpec") or entry.get("gem") or "").strip()
        gem_id = entry.get("gemId") or entry.get("id")
        if not name and not gem_id:
            raise ValueError("Gem spec dict must include 'name', 'nameSpec', 'gem', or 'gemId'.")
        raw_sp = entry.get("skillPart") if "skillPart" in entry else entry.get("skill_part")
        skill_part = _parse_positive_int(raw_sp, "skillPart")
        raw_sc = (
            entry.get("stageCount")
            if "stageCount" in entry
            else (entry.get("stage_count") if "stage_count" in entry else entry.get("skillStageCount"))
        )
        stage_count = _parse_positive_int(raw_sc, "stageCount")
        explicit_level = int(entry["level"]) if entry.get("level") is not None else None
        eff_allow_illegal = allow_illegal_level or bool(
            entry.get("allowIllegalLevel", entry.get("allow_illegal_level", False))
        )
        if name:
            eff_level, cautions = _validate_gem_level(name, explicit_level, allow_illegal_level=eff_allow_illegal)
        else:
            eff_level = explicit_level if explicit_level is not None else 20
            cautions = []
        out = {
            "name": name,
            "gemId": str(gem_id) if gem_id else None,
            "level": eff_level if eff_level is not None else 20,
            "quality": int(entry["quality"]) if entry.get("quality") is not None else 20,
            "enabled": bool(entry.get("enabled", True)),
            "count": int(entry["count"]) if entry.get("count") is not None else 1,
        }
        if skill_part is not None:
            out["skill_part"] = skill_part
        if stage_count is not None:
            out["stage_count"] = stage_count
        if cautions:
            out["cautions"] = cautions
        return out

    raise ValueError(f"Unsupported gem spec type: {type(entry).__name__}")


def _resolve_gem_specs_batch(
    w: BridgeWorker,
    g_idx: int,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve `gemId`/`nameSpec` for a list of normalized gem specs in ONE batched `gem_search`."""
    search_indices: list[int] = []
    search_calls: list[tuple[str, dict[str, Any]]] = []
    for i, sp in enumerate(specs):
        if not sp.get("gemId") and sp.get("name"):
            search_indices.append(i)
            search_calls.append(
                (
                    "gem_search",
                    {"group": g_idx, "query": sp["name"], "byDps": False, "supportableOnly": False, "limit": 20},
                )
            )

    resolved: list[dict[str, Any]] = [dict(sp) for sp in specs]
    if search_calls:
        search_results = _call_batch(w, search_calls, raise_on_error=False)
        retry_indices: list[int] = []
        retry_calls: list[tuple[str, dict[str, Any]]] = []
        for i, res in zip(search_indices, search_results, strict=False):
            cand = None
            if not _is_batch_error(res):
                cand = _pick_best_gem_candidate(res.get("gems", []), specs[i]["name"])
            if cand:
                if cand.get("gemId"):
                    resolved[i]["gemId"] = cand["gemId"]
                if cand.get("name"):
                    resolved[i]["name"] = cand["name"]
                resolved[i]["resolvedCandidate"] = cand
            else:
                fb_q = _gem_search_fallback_query(specs[i]["name"])
                if fb_q:
                    retry_indices.append(i)
                    retry_calls.append(
                        (
                            "gem_search",
                            {"group": g_idx, "query": fb_q, "byDps": False, "supportableOnly": False, "limit": 20},
                        )
                    )
        if retry_calls:
            retry_results = _call_batch(w, retry_calls, raise_on_error=False)
            for i, res in zip(retry_indices, retry_results, strict=False):
                if not _is_batch_error(res):
                    cand = _pick_best_gem_candidate(res.get("gems", []), specs[i]["name"])
                    if cand:
                        if cand.get("gemId"):
                            resolved[i]["gemId"] = cand["gemId"]
                        if cand.get("name"):
                            resolved[i]["name"] = cand["name"]
                        resolved[i]["resolvedCandidate"] = cand
    return resolved


def _build_add_gem_params(g_idx: int, spec: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "group": g_idx,
        "level": int(spec.get("level", 20)),
        "quality": int(spec.get("quality", 20)),
        "enabled": bool(spec.get("enabled", True)),
        "count": int(spec.get("count", 1)),
    }
    if spec.get("gemId"):
        params["gemId"] = spec["gemId"]
    if spec.get("name"):
        params["nameSpec"] = spec["name"]
    return params


def _resolve_and_validate_specs(
    w: BridgeWorker,
    g_idx: int,
    norm_specs: list[dict[str, Any]],
    explicit_levels: list[int | None],
    *,
    allow_illegal_level: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    cautions: list[str] = []
    for sp in norm_specs:
        cautions = _merge_cautions(cautions, sp.get("cautions"))
    resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)
    for sp, exp_lv in zip(resolved_specs, explicit_levels, strict=False):
        r_name = str(sp.get("name") or "").strip()
        if r_name:
            eff_lv, lv_c = _validate_gem_level(r_name, exp_lv, allow_illegal_level=allow_illegal_level)
            if eff_lv is not None:
                sp["level"] = eff_lv
            cautions = _merge_cautions(cautions, lv_c)
    return resolved_specs, cautions


def _is_vaal_gem_entry(gem: dict[str, Any] | None, fallback_name: str = "") -> bool:
    """Return True if `gem` or `fallback_name` refers to a Vaal skill gem."""
    g = gem if isinstance(gem, dict) else {}
    if g.get("support"):
        return False
    skill_id = str(g.get("skillId") or "").strip()
    if skill_id.startswith("Vaal") and skill_id != "Vaal":
        return True
    for raw in (g.get("name"), g.get("nameSpec"), g.get("nameZh"), fallback_name):
        if not raw:
            continue
        cleaned = strip_pob_escapes(str(raw)).strip()
        low = cleaned.lower()
        if low.startswith("vaal ") or " vaal " in low or "瓦尔" in cleaned:
            return True
    return False


def _add_gem_with_default_skill_flags(w: BridgeWorker, params: dict[str, Any]) -> dict[str, Any]:
    """Add a gem and, for Vaal gems only, default to base skill ON (`enableGlobal2=True`) and Vaal burst OFF."""
    added = w.call("add_gem", params)
    gem = added.get("gem") if isinstance(added, dict) else None
    fallback_name = str(params.get("nameSpec") or "")
    if not _is_vaal_gem_entry(gem, fallback_name):
        return added
    gem_index = gem.get("index") if isinstance(gem, dict) else None
    if gem_index is None:
        raise RuntimeError("add_gem did not return the added gem index")
    updated = w.call(
        "set_gem",
        {"group": params["group"], "index": gem_index, "enableGlobal1": False, "enableGlobal2": True},
    )
    if isinstance(added, dict):
        merged = dict(added)
        if isinstance(updated, dict) and "gem" in updated:
            merged["gem"] = updated["gem"]
        return merged
    return updated


def _apply_vaal_defaults_batch(
    w: BridgeWorker,
    g_idx: int,
    resolved_specs: list[dict[str, Any]],
    gem_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build `added_gems` records and batch `set_gem(enableGlobal1=False, enableGlobal2=True)` ONLY for Vaal gems."""
    added_gems: list[dict[str, Any]] = []
    vaal_fix_positions: list[int] = []
    vaal_fix_calls: list[tuple[str, dict[str, Any]]] = []
    for pos, (sp, res) in enumerate(zip(resolved_specs, gem_results, strict=False)):
        gem_obj = res.get("gem") if isinstance(res, dict) else None
        if _is_vaal_gem_entry(gem_obj, str(sp.get("name") or "")):
            gem_idx = gem_obj.get("index") if isinstance(gem_obj, dict) else None
            if gem_idx is None:
                raise RuntimeError("add_gem did not return the added gem index")
            vaal_fix_positions.append(pos)
            vaal_fix_calls.append(
                (
                    "set_gem",
                    {"group": g_idx, "index": gem_idx, "enableGlobal1": False, "enableGlobal2": True},
                )
            )
        added_gems.append(
            {
                "name": sp.get("name"),
                "gemId": sp.get("gemId"),
                "level": sp.get("level"),
                "quality": sp.get("quality"),
                "enabled": sp.get("enabled"),
                "count": sp.get("count"),
                "gem": gem_obj,
            }
        )
    if vaal_fix_calls:
        fix_results = _call_batch(w, vaal_fix_calls)
        for pos, fix_res in zip(vaal_fix_positions, fix_results, strict=False):
            if isinstance(fix_res, dict) and "gem" in fix_res:
                added_gems[pos]["gem"] = fix_res["gem"]
    return added_gems


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
