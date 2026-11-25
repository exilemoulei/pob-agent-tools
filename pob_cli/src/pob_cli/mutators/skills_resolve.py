"""Gem candidate resolution, spec normalization, and Vaal skill default flags."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .. import game_data as _game_data
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
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


def _gem_spec_allows_illegal_level(entry: str | dict[str, Any], allow_illegal_level: bool) -> bool:
    """The command-wide flag, or the same permission set on one gem of a `--gems` JSON list."""
    if allow_illegal_level or not isinstance(entry, dict):
        return bool(allow_illegal_level)
    return bool(entry.get("allowIllegalLevel", entry.get("allow_illegal_level", False)))


def _legacy_warn_cautions(warns: list[Any]) -> list[str]:
    return [f"[POE1-PATCH-WARN] {w.summary}" for w in warns if getattr(w, "category", "") == "legacy_patch"]


def _gem_name_cautions(
    raw_gems: Sequence[str | dict[str, Any]], allow_legacy: bool, *, names: Sequence[Any] | None = None
) -> list[str]:
    """Guardrail check of every gem name in a `--gems` list; a legacy gem needs the flag or its own `allowLegacy`."""
    cautions: list[str] = []
    for pos, entry in enumerate(raw_gems):
        if names is not None:
            name = str(names[pos] or "")
        elif isinstance(entry, str):
            name = entry
        else:
            name = str(entry.get("name") or entry.get("nameSpec") or entry.get("gem") or "")
        if not name:
            continue
        own = isinstance(entry, dict) and bool(entry.get("allowLegacy", entry.get("allow_legacy", False)))
        warns = check_input(
            name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not (allow_legacy or own)
        )
        cautions = _merge_cautions(cautions, _legacy_warn_cautions(warns))
    return cautions


_VALID_GEM_SPEC_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "nameSpec",
        "gem",
        "gemId",
        "id",
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

# What `pob skills --json` reports about a gem and nothing can set. They are let through so a
# reported gem can be handed back as a spec; every other unknown key is a typo and is refused.
_REPORTED_GEM_KEYS: frozenset[str] = frozenset(
    {
        "index",
        "nameZh",
        "skillId",
        "support",
        "errMsg",
        "color",
        "hasGlobalEffect",
        "naturalMaxLevel",
        "reqLevel",
        "matchesSocket",
        "fromItem",
        "fromNode",
        "tooltipDps",  # added by `pob skills --group N --json`
    }
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
        unknown = sorted(k for k in entry if k not in _VALID_GEM_SPEC_KEYS and k not in _REPORTED_GEM_KEYS)
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
        eff_allow_illegal = _gem_spec_allows_illegal_level(entry, allow_illegal_level)
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
        for flag in ("enableGlobal1", "enableGlobal2"):
            if entry.get(flag) is not None:
                out[flag] = bool(entry[flag])
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
    raw_specs: Sequence[str | dict[str, Any]],
    *,
    allow_illegal_level: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    cautions: list[str] = []
    for sp in norm_specs:
        cautions = _merge_cautions(cautions, sp.get("cautions"))
    resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)
    for sp, exp_lv, raw_spec in zip(resolved_specs, explicit_levels, raw_specs, strict=False):
        r_name = str(sp.get("name") or "").strip()
        if r_name:
            # Same permission as the first pass in `_normalize_gem_spec`, or this pass refuses what that one let through.
            allow_lv = _gem_spec_allows_illegal_level(raw_spec, allow_illegal_level)
            eff_lv, lv_c = _validate_gem_level(r_name, exp_lv, allow_illegal_level=allow_lv)
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
    """Build `added_gems` records and batch the `set_gem` calls for the global-effect flags.

    A Vaal gem defaults to `enableGlobal1=False, enableGlobal2=True`; a flag the spec names wins,
    on a Vaal gem or any other.
    """
    added_gems: list[dict[str, Any]] = []
    vaal_fix_positions: list[int] = []
    vaal_fix_calls: list[tuple[str, dict[str, Any]]] = []
    for pos, (sp, res) in enumerate(zip(resolved_specs, gem_results, strict=False)):
        gem_obj = res.get("gem") if isinstance(res, dict) else None
        flags: dict[str, bool] = {}
        if _is_vaal_gem_entry(gem_obj, str(sp.get("name") or "")):
            flags = {"enableGlobal1": False, "enableGlobal2": True}
        flags.update({k: bool(sp[k]) for k in ("enableGlobal1", "enableGlobal2") if sp.get(k) is not None})
        if flags:
            gem_idx = gem_obj.get("index") if isinstance(gem_obj, dict) else None
            if gem_idx is None:
                raise RuntimeError("add_gem did not return the added gem index")
            vaal_fix_positions.append(pos)
            vaal_fix_calls.append(("set_gem", {"group": g_idx, "index": gem_idx, **flags}))
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
