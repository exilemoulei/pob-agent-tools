"""Per-dimension PoB de-watering mutation builders (config, customMods, flasks, and skills)."""

from __future__ import annotations

import math
import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from .constants import (
    _POB_TRUTHY_CONFIG_DEFAULTS,
    DEWATER_SENTINEL_TITLE,
    INFLATION_CONFIG_EXACT,
    INFLATION_CONFIG_PREFIXES,
    PERMANENT_CONFIG_VARS,
    POB_GEM_FLAG_DEFAULTS,
    TEMPORARY_SKILL_PATTERNS,
)
from .summary import (
    _has_bloodsoaked_blade_keystone,
    _is_tincture_item,
)


def _same_state(a: Any, b: Any) -> bool:
    """Compare a read-back value with the expected one (PoB stores unset checks as nil)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_state(a[k], b[k]) for k in a)
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return bool(a) == bool(b)
    try:
        fa = float(a)
        fb = float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(fa) or math.isnan(fb):
        return math.isnan(fa) and math.isnan(fb)
    if math.isinf(fa) or math.isinf(fb):
        return fa == fb
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa))


# The lines PoB 2.69 already counts from an equipped Viscera or allocated Runegraft: they scale gloves
# and boots bonuses only (Data/Viscera.lua, TattooPassives.lua). English only, since that is all PoB's
# customMods parser reads; a line it cannot read counts for nothing wherever it stands.
_SIM_330_CUSTOM_MOD_RE = re.compile(
    r"(?:increased|reduced)\s+bonuses\s+gained\s+from\s+equipped\s+(?:gloves|boots)",
    re.IGNORECASE,
)


def _is_custom_mod_comment(line: str) -> bool:
    return line.startswith(("#", "//"))


def _preview_mod_lines(mod_lines: list[str], limit: int = 3) -> str:
    head = "; ".join(mod_lines[:limit])
    extra = len(mod_lines) - limit
    return f"{head}; ... (+{extra} more)" if extra > 0 else head


def _classify_custom_mod_block(
    display_title: str,
    lines: list[str],
) -> tuple[str, str | None, list[str], list[str]]:
    sim_330_lines = [ln for ln in lines if _SIM_330_CUSTOM_MOD_RE.search(ln)]
    other_lines = [ln for ln in lines if not _SIM_330_CUSTOM_MOD_RE.search(ln)]
    if not sim_330_lines:
        label = f"customMods[{display_title}] ({len(lines)} active lines: {_preview_mod_lines(lines)})"
        return label, None, sim_330_lines, other_lines

    sim_str = _preview_mod_lines(sim_330_lines)
    if other_lines:
        other_str = _preview_mod_lines(other_lines)
        label = (
            f"customMods[{display_title}] (MIXED: Viscera/Runegraft lines [{sim_str}] "
            f"+ other custom lines [{other_str}]; PoB models an equipped Viscera or allocated Runegraft itself)"
        )
        note = (
            f"`customMods[{display_title}]` mixes Viscera/Runegraft lines [{sim_str}] with other custom lines "
            f"[{other_str}]. PoB models an equipped Wickerman Viscera (`pob items`) and an allocated Runegraft "
            "(`pob tree`) itself, so a line that repeats one counts twice. `--keep customMods` keeps the block "
            "whole: take the repeated lines out of it first."
        )
        return label, note, sim_330_lines, other_lines

    label = (
        f"customMods[{display_title}] (Viscera/Runegraft lines [{sim_str}]; "
        "PoB models an equipped Viscera or allocated Runegraft itself)"
    )
    note = (
        f"`customMods[{display_title}]` writes Viscera/Runegraft lines [{sim_str}] by hand. PoB models an equipped "
        "Wickerman Viscera (`pob items`) and an allocated Runegraft (`pob tree`) itself, so a line that repeats one "
        "counts twice; pass `--keep customMods` only if the build does not already carry it (the block is kept whole)."
    )
    return label, note, sim_330_lines, other_lines


def _normalize_custom_mod_blocks(custom_mods: Any) -> list[dict[str, Any]]:
    """Normalize PoB customMods block list (matching bridge.lua's ``enabled ~= false`` semantics)."""
    return [
        {
            "title": b.get("title") or "Default",
            "text": b.get("text") or "",
            "enabled": bool(b.get("enabled", True) is not False),
        }
        for b in (custom_mods or [])
        if isinstance(b, dict)
    ]


def _is_vaal_text(text: str) -> bool:
    low = strip_pob_escapes(text or "").strip().lower()
    if not low:
        return False
    return low.startswith("vaal ") or " vaal " in low or "瓦尔" in low


def _is_vaal_skill_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in ("name", "nameZh"):
        if _is_vaal_text(str(entry.get(key) or "")):
            return True
    sid = strip_pob_escapes(str(entry.get("skillId") or "")).strip().lower()
    return sid.startswith("vaal")


def _dewater_config_and_custom_mods(
    cfg_res: dict[str, Any],
    keep_set: set[str],
    known: dict[str, str],
    stripped: dict[str, list[str]],
    mutations: list[tuple[str, dict[str, Any]]],
    records: list[dict[str, Any]],
    *,
    auto_keep_reasons: dict[str, str] | None = None,
) -> None:
    current_boss = None
    had_preset_boss_skills = False

    for sec in cfg_res.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            val = item.get("value")
            if var:
                known[str(var).lower()] = str(var)
            if not var or var in PERMANENT_CONFIG_VARS:
                continue
            if var == "enemyIsBoss":
                current_boss = val
                continue
            if val in (None, False, "", 0):
                continue
            if item.get("isDefault") is True:
                continue
            if "defaultState" in item and _same_state(val, item.get("defaultState")):
                continue
            if var == "presetBossSkills" and val == "None":
                continue
            if var in _POB_TRUTHY_CONFIG_DEFAULTS and _same_state(val, _POB_TRUTHY_CONFIG_DEFAULTS[var]):
                continue

            is_inflation = (
                var == "presetBossSkills"
                or var.startswith(INFLATION_CONFIG_PREFIXES)
                or var in INFLATION_CONFIG_EXACT
            )
            if not is_inflation:
                continue
            if var.lower() in keep_set:
                stripped["kept"].append(f"{var}={val!r} (kept)")
                continue
            auto_reason = (auto_keep_reasons or {}).get(var.lower())
            if auto_reason:
                stripped.setdefault("autoKept", []).append(f"{var}={val!r} (auto-kept: {auto_reason})")
                continue
            if var == "presetBossSkills":
                had_preset_boss_skills = True
            stripped["config"].append(f"{var}={val!r} -> reset")
            mutations.append(("reset_config", {"var": var}))
            records.append(
                {
                    "dimension": "config",
                    "key": var,
                    "label": f"{var}={val!r}",
                    "restore": [("set_config", {"var": var, "value": val})],
                    "revert": [("reset_config", {"var": var})],
                    "probe": {"kind": "config", "var": var},
                    "original": val,
                }
            )

    if "enemyisboss" in keep_set:
        stripped["kept"].append(f"enemyIsBoss={current_boss!r} (kept)")
    else:
        if current_boss != "Pinnacle":
            stripped["config"].append(f"enemyIsBoss={current_boss!r} -> 'Pinnacle'")
        mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))
        if had_preset_boss_skills:
            mutations.append(("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}))

    custom_mods = cfg_res.get("customMods", [])
    blocks = _normalize_custom_mod_blocks(custom_mods)
    for b in blocks:
        raw_title = str(b["title"])
        clean_title = strip_pob_escapes(raw_title).strip()
        display_title = clean_title or raw_title
        known[f"custommods:{raw_title.lower()}"] = f"customMods:{display_title}"
        if clean_title:
            known[f"custommods:{clean_title.lower()}"] = f"customMods:{display_title}"

    dewatered_blocks = [dict(b) for b in blocks]
    group_records: list[tuple[int, str, str, str | None]] = []
    for i, b in enumerate(blocks):
        lines = [
            s
            for ln in b["text"].splitlines()
            if (s := strip_pob_escapes(ln).strip()) and not _is_custom_mod_comment(s)
        ]
        if not b["enabled"] or not lines:
            continue
        raw_title = str(b["title"])
        clean_title = strip_pob_escapes(raw_title).strip()
        display_title = clean_title or raw_title
        label, note_330, _sim_lines, _other_lines = _classify_custom_mod_block(display_title, lines)
        keep_keys = {f"custommods:{raw_title.lower()}"}
        if clean_title:
            keep_keys.add(f"custommods:{clean_title.lower()}")
        if "custommods" in keep_set or bool(keep_keys & keep_set):
            kept_label = f"customMods[{display_title}] ({len(lines)} active lines: {_preview_mod_lines(lines)})"
            stripped["kept"].append(f"{kept_label} (kept)")
            continue
        dewatered_blocks[i]["enabled"] = False
        stripped["config"].append(f"{label} -> disabled")
        if note_330:
            stripped.setdefault("customMods330Notes", []).append(note_330)
        group_records.append((i, display_title, label, note_330))

    if group_records:
        has_sentinel = any(
            b["title"] == DEWATER_SENTINEL_TITLE
            and b["enabled"]
            and bool(b["text"].strip())
            and all(
                not (s := strip_pob_escapes(ln).strip()) or _is_custom_mod_comment(s)
                for ln in b["text"].splitlines()
            )
            for b in dewatered_blocks
        )
        if not has_sentinel:
            dewatered_blocks.append({"title": DEWATER_SENTINEL_TITLE, "text": "# disabled", "enabled": True})
        mutations.append(("set_custom_mods", {"list": dewatered_blocks}))
        for i, display_title, label, note_330 in group_records:
            restored_blocks = [dict(b) for b in dewatered_blocks]
            restored_blocks[i]["enabled"] = True
            rec_entry: dict[str, Any] = {
                "dimension": "config",
                "key": f"customMods:{display_title}",
                "label": label,
                "restore": [("set_custom_mods", {"list": restored_blocks})],
                "revert": [("set_custom_mods", {"list": dewatered_blocks})],
                "probe": {"kind": "customMods"},
                "original": restored_blocks,
            }
            if note_330:
                rec_entry["customMods330Notes"] = [note_330]
            records.append(rec_entry)


def _dewater_flasks(
    worker: BridgeWorker,
    items_res: dict[str, Any],
    keep_set: set[str],
    known: dict[str, str],
    stripped: dict[str, list[str]],
    mutations: list[tuple[str, dict[str, Any]]],
    records: list[dict[str, Any]],
    *,
    has_bloodsoaked_blade: bool | None,
    flask_notes: dict[str, str] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> None:
    from .dewater_mageblood import mageblood_exempt_flask_slots

    mb_exempt, mb_spec = mageblood_exempt_flask_slots(
        worker, items_res, equipped_slots_raw=equipped_slots_raw
    )
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("isFlask") and slot.get("name"):
            known[str(slot["name"]).lower()] = str(slot["name"])
    has_active_tincture = any(
        slot.get("isFlask")
        and slot.get("active")
        and _is_tincture_item(items_by_id.get(slot.get("selItemId") or 0))
        for slot in items_res.get("slots", [])
    )
    bloodsoaked_active = (
        bool(has_bloodsoaked_blade)
        if has_bloodsoaked_blade is not None
        else (_has_bloodsoaked_blade_keystone(worker) if has_active_tincture else False)
    )
    for slot in items_res.get("slots", []):
        if slot.get("isFlask") and slot.get("active"):
            slot_name = slot["name"]
            item = items_by_id.get(slot.get("selItemId") or 0, {})
            item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
            if "flasks" in keep_set or slot_name.lower() in keep_set:
                stripped["kept"].append(f"{slot_name} ({item_title}) (kept)")
                continue
            if slot_name in mb_exempt:
                stripped.setdefault("autoKept", []).append(
                    f"{slot_name} ({item_title}) (auto-kept: {mb_spec})"
                )
                continue
            if bloodsoaked_active and _is_tincture_item(item):
                stripped["kept"].append(
                    f"{slot_name} ({item_title}) (kept: Bloodsoaked Blade sustained Tincture; Tinctures != Flasks)"
                )
                continue
            f_note = (flask_notes or {}).get(slot_name)
            note_suffix = f" [{f_note}]" if f_note else ""
            stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False{note_suffix}")
            mutations.append(("set_slot_active", {"slotName": slot_name, "active": False}))
            records.append(
                {
                    "dimension": "flasks",
                    "key": slot_name,
                    "label": f"{slot_name} ({item_title})",
                    "restore": [("set_slot_active", {"slotName": slot_name, "active": True})],
                    "revert": [("set_slot_active", {"slotName": slot_name, "active": False})],
                    "probe": {"kind": "flask", "slot": slot_name},
                    "original": True,
                }
            )


def _dewater_skills(
    skills_res: dict[str, Any],
    keep_set: set[str],
    known: dict[str, str],
    stripped: dict[str, list[str]],
    mutations: list[tuple[str, dict[str, Any]]],
    records: list[dict[str, Any]],
) -> None:
    main_socket_group = skills_res.get("mainSocketGroup")
    for group in skills_res.get("groups", []):
        g_idx = group["index"]
        group_enabled = group.get("enabled", True)
        is_main_group = bool(group.get("isMain")) or (main_socket_group is not None and g_idx == main_socket_group)
        skills_list = group.get("skills") or []
        orig_active = int(group.get("mainActiveSkill") or 1)
        for gem in group.get("gems", []):
            raw_names = [
                str(gem.get("nameSpec") or ""),
                str(gem.get("name") or ""),
                str(gem.get("nameZh") or ""),
                str(gem.get("skillId") or ""),
            ]
            names = [strip_pob_escapes(n).strip() for n in raw_names]
            for n in names:
                if n:
                    known[n.lower()] = n
            if not group_enabled or not gem.get("enabled", True) or gem.get("support"):
                continue
            combined_lower = " ".join(names).lower()
            is_vaal = (
                combined_lower.startswith("vaal ")
                or " vaal " in combined_lower
                or "瓦尔" in combined_lower
            )
            is_temp = any(pat in combined_lower for pat in TEMPORARY_SKILL_PATTERNS)
            original_vaal_g1 = bool(gem.get("enableGlobal1", POB_GEM_FLAG_DEFAULTS["enableGlobal1"]))
            is_active_vaal = is_vaal and original_vaal_g1

            selected_main_is_vaal = False
            next_skill: dict[str, Any] = {}
            if (
                is_vaal
                and not is_temp
                and not gem.get("hasGlobalEffect")
                and is_main_group
                and 1 <= orig_active
                and orig_active + 1 <= len(skills_list)
            ):
                cur_skill = skills_list[orig_active - 1]
                cand_next = skills_list[orig_active]
                if _is_vaal_skill_entry(cur_skill) and not _is_vaal_skill_entry(cand_next):
                    cur_names = {
                        strip_pob_escapes(str(cur_skill.get(k) or "")).strip().lower()
                        for k in ("name", "nameZh", "skillId")
                        if cur_skill.get(k)
                    }
                    gem_names = {n.lower() for n in names if n}
                    if not cur_names or bool(cur_names & gem_names) or any(
                        cn in gn or gn in cn
                        for cn in cur_names
                        for gn in gem_names
                        if len(cn) >= 4 and len(gn) >= 4
                    ):
                        selected_main_is_vaal = True
                        next_skill = cand_next

            disp = strip_pob_escapes(gem.get("nameZh") or gem.get("name") or gem.get("nameSpec") or "")
            if not (is_temp or is_active_vaal or selected_main_is_vaal):
                continue
            if any(n.lower() in keep_set for n in names if n):
                stripped["kept"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) (kept)")
                continue
            gem_ref = {"group": g_idx, "index": gem["index"]}
            if is_temp:
                stripped["skills"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enabled=False")
                mutations.append(("set_gem", {**gem_ref, "enabled": False}))
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp}",
                        "restore": [("set_gem", {**gem_ref, "enabled": True})],
                        "revert": [("set_gem", {**gem_ref, "enabled": False})],
                        "probe": {"kind": "gem", **gem_ref, "fields": ["enabled"]},
                        "original": {"enabled": True},
                    }
                )
            else:
                original_base_enabled = bool(gem.get("enableGlobal2", POB_GEM_FLAG_DEFAULTS["enableGlobal2"]))
                dewater_calls: list[tuple[str, dict[str, Any]]] = [
                    ("set_gem", {**gem_ref, "enableGlobal1": False, "enableGlobal2": original_base_enabled})
                ]
                restore_calls: list[tuple[str, dict[str, Any]]] = [
                    ("set_gem", {**gem_ref, "enableGlobal1": original_vaal_g1, "enableGlobal2": original_base_enabled})
                ]
                probe_fields = ["enabled", "enableGlobal1", "enableGlobal2"]
                orig_state: dict[str, Any] = {
                    "enabled": True,
                    "enableGlobal1": original_vaal_g1,
                    "enableGlobal2": original_base_enabled,
                }
                if selected_main_is_vaal:
                    dewater_calls.append(("set_group", {"index": g_idx, "mainActiveSkill": orig_active + 1}))
                    restore_calls.append(("set_group", {"index": g_idx, "mainActiveSkill": orig_active}))
                    probe_fields.append("mainActiveSkill")
                    orig_state["mainActiveSkill"] = orig_active
                    next_name = strip_pob_escapes(
                        str(next_skill.get("nameZh") or next_skill.get("name") or f"skill #{orig_active + 1}")
                    )
                    stripped["skills"].append(
                        f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal1=False, "
                        f"mainActiveSkill={orig_active + 1} ({next_name})"
                    )
                else:
                    stripped["skills"].append(
                        f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal1=False (base skill kept)"
                    )
                mutations.extend(dewater_calls)
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp} (Vaal)",
                        "restore": restore_calls,
                        "revert": list(dewater_calls),
                        "probe": {"kind": "gem", **gem_ref, "fields": probe_fields},
                        "original": orig_state,
                    }
                )
