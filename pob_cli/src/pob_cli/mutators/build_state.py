"""Class/ascendancy changes, fresh build creation, and spec/set/loadout switching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import game_data as _game_data
from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import check_poe2_class_or_ascendancy
from ..snapshot import BuildScope, run_mutation
from .common import _BatchCache, _fold_search_text

LOADOUT_SENTINELS = frozenset(
    {
        "loadouts:",
        "-----",
        "sync",
        "new loadout",
        "manage loadouts",
        "no loadouts",
        "help >>",
    }
)


def _query_forms(query: str, pob_dir: Path | None) -> set[str]:
    """Folded query plus its English name: the engine may label options in Traditional or
    Simplified Chinese, and a dictionary hit also matches the always-present English label."""
    q_en = _game_data.english_for_zh(query, pob_dir=pob_dir) or ""
    return {_fold_search_text(query), _fold_search_text(q_en)} - {""}


def _labels(opt: dict[str, Any]) -> tuple[str, str]:
    return _fold_search_text(str(opt.get("name") or "")), _fold_search_text(str(opt.get("nameZh") or ""))


def _match_named_option(
    options: list[dict[str, Any]],
    query: str,
    entity_label: str,
    *,
    pob_dir: Path | None = None,
) -> dict[str, Any]:
    q = query.strip()
    q_low = _fold_search_text(q)
    if q_low in ("none", "无", "clear", "0"):
        for opt in options:
            if opt.get("id") == 0 or str(opt.get("name") or "").lower() == "none":
                return opt

    wanted = _query_forms(q, pob_dir)
    for opt in options:
        if wanted & set(_labels(opt)) or (q.isdigit() and int(q) == opt.get("id")):
            return opt

    sub_matches = [opt for opt in options if any(w in label for w in wanted for label in _labels(opt) if label)]
    if len(sub_matches) == 1:
        return sub_matches[0]
    if len(sub_matches) > 1:
        samples = ", ".join(f"{o.get('name')} ({o.get('nameZh')})" for o in sub_matches[:6])
        raise ValueError(f"Ambiguous {entity_label} '{query}' matched multiple options: {samples}")

    available = ", ".join(f"{o.get('name')}/{o.get('nameZh')}" for o in options[:12])
    raise ValueError(f"Unknown {entity_label} '{query}'. Available: {available}")


def _apply_class_ascendancy_in_place(
    w: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy_name: str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    pob_dir = getattr(w, "pob_root", None)
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy_name, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy_name, field="secondary_ascendancy")

    lc = cache.get_classes() if cache is not None else w.call("list_classes", {})
    classes = lc.get("classes", [])
    secondary = lc.get("secondary", [])
    before = dict(lc.get("current") or {})

    target_class_obj: dict[str, Any] | None = None
    target_asc_obj: dict[str, Any] | None = None
    target_sec_obj: dict[str, Any] | None = None
    changes: list[str] = []
    class_change_mode: str | None = None

    if class_name is not None and str(class_name).strip():
        target_class_obj = _match_named_option(classes, str(class_name), "class", pob_dir=pob_dir)
    else:
        cur_cid = before.get("classId")
        target_class_obj = next((c for c in classes if c.get("id") == cur_cid), None)

    if ascendancy_name is not None and str(ascendancy_name).strip():
        asc_q = str(ascendancy_name).strip()
        if class_name is not None and str(class_name).strip():
            if target_class_obj is None:
                raise ValueError(f"Unable to resolve class '{class_name}' for ascendancy '{asc_q}'.")
            target_asc_obj = _match_named_option(
                target_class_obj.get("ascendancies", []),
                asc_q,
                f"ascendancy for {target_class_obj.get('name')}",
                pob_dir=pob_dir,
            )
        else:
            try:
                if target_class_obj is None:
                    raise ValueError(f"Current character class is unknown for ascendancy '{asc_q}'.")
                target_asc_obj = _match_named_option(
                    target_class_obj.get("ascendancies", []),
                    asc_q,
                    f"ascendancy for {target_class_obj.get('name')}",
                    pob_dir=pob_dir,
                )
            except ValueError:
                found_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
                wanted = _query_forms(asc_q, pob_dir)
                for cls in classes:
                    for asc in cls.get("ascendancies", []):
                        if asc.get("id") == 0:
                            continue
                        if any(w_q in label for w_q in wanted for label in _labels(asc) if label):
                            found_pairs.append((cls, asc))
                exact_pairs = [p for p in found_pairs if wanted & set(_labels(p[1]))]
                chosen_pairs = exact_pairs or found_pairs
                if len(chosen_pairs) == 1:
                    target_class_obj, target_asc_obj = chosen_pairs[0]
                elif len(chosen_pairs) > 1:
                    samples = ", ".join(f"{c['name']}/{a['name']}" for c, a in chosen_pairs[:6])
                    raise ValueError(f"Ambiguous ascendancy '{asc_q}': {samples}") from None
                else:
                    raise

    if secondary_ascendancy_name is not None and str(secondary_ascendancy_name).strip():
        target_sec_obj = _match_named_option(
            secondary, str(secondary_ascendancy_name), "secondary ascendancy", pob_dir=pob_dir
        )

    if target_class_obj is not None and target_class_obj.get("id") != before.get("classId"):
        cid = int(target_class_obj["id"])
        res = w.call("set_class", {"classId": cid})
        if res.get("needsConfirm") == "class_change":
            res = w.call("set_class", {"classId": cid, "confirm": "connect"})
            if res.get("connectFailed") or res.get("needsConfirm") == "class_change":
                res = w.call("set_class", {"classId": cid, "confirm": "reset"})
                class_change_mode = "reset"
            else:
                class_change_mode = "connect"
        else:
            class_change_mode = "direct"
        changes.append(f"class: {before.get('className')} -> {target_class_obj.get('name')} ({class_change_mode})")

    if target_asc_obj is not None:
        aid = int(target_asc_obj["id"])
        w.call("set_ascendancy", {"ascendClassId": aid})
        changes.append(f"ascendancy: {before.get('ascendClassName')} -> {target_asc_obj.get('name')}")

    if target_sec_obj is not None:
        sid = int(target_sec_obj["id"])
        w.call("set_secondary_ascendancy", {"ascendClassId": sid})
        changes.append(f"secondaryAscendancy: {target_sec_obj.get('name')} (id={sid})")

    after_lc = w.call("list_classes", {})
    if cache is not None:
        cache.classes = after_lc
        cache.invalidate("tree", "items", "skills", "specs")
    after = dict(after_lc.get("current") or {})
    return {
        "before": before,
        "after": after,
        "changes": changes,
        "classChangeMode": class_change_mode,
    }


def set_class_ascendancy(
    worker: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy_name: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Change character class, primary ascendancy, and/or secondary ascendancy (Bloodline)."""
    if not any(x is not None and str(x).strip() != "" for x in (class_name, ascendancy_name, secondary_ascendancy_name)):
        raise ValueError("Specify at least one of class_name, ascendancy_name, or secondary_ascendancy_name.")
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy_name, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy_name, field="secondary_ascendancy")

    def _do_set_class(w: BridgeWorker) -> dict[str, Any]:
        return _apply_class_ascendancy_in_place(
            w,
            class_name=class_name,
            ascendancy_name=ascendancy_name,
            secondary_ascendancy_name=secondary_ascendancy_name,
        )

    return run_mutation(worker, _do_set_class, apply=apply)


def _apply_new_build_in_place(
    w: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy: str | None = None,
    secondary_ascendancy: str | None = None,
    level: int | None = None,
    name: str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_ascendancy, field="secondary_ascendancy")

    nb_params: dict[str, Any] = {}
    if name and str(name).strip():
        nb_params["name"] = str(name).strip()
    w.call("new_build", nb_params)
    if cache is not None:
        cache.invalidate_all()

    changes: list[str] = ["new_build"]
    cls_detail = None
    if any(x is not None and str(x).strip() != "" for x in (class_name, ascendancy, secondary_ascendancy)):
        cls_detail = _apply_class_ascendancy_in_place(
            w,
            class_name=class_name,
            ascendancy_name=ascendancy,
            secondary_ascendancy_name=secondary_ascendancy,
            cache=cache,
        )
        changes.extend(cls_detail.get("changes", []))

    if level is not None:
        lv = int(level)
        if lv < 1 or lv > 100:
            raise ValueError(f"Character level must be between 1 and 100 (got {lv}).")
        w.call("set_build_field", {"field": "level", "value": lv})
        changes.append(f"level={lv}")

    info = w.call("get_build_info", {})
    return {
        "name": nb_params.get("name"),
        "buildInfo": info,
        "class": (cls_detail or {}).get("after")
        or {"className": info.get("className"), "ascendClassName": info.get("ascendClassName")},
        "level": info.get("level"),
        "changes": changes,
    }


def create_new_build(
    worker: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy: str | None = None,
    secondary_ascendancy_name: str | None = None,
    level: int | None = None,
    name: str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Create a fresh blank PoB build with optional class, ascendancy, bloodline, and character level."""
    eff_asc = ascendancy if ascendancy is not None else ascendancy_name
    eff_sec = secondary_ascendancy if secondary_ascendancy is not None else secondary_ascendancy_name
    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(eff_asc, field="ascendancy")
    check_poe2_class_or_ascendancy(eff_sec, field="secondary_ascendancy")

    def _do_new_build(w: BridgeWorker) -> dict[str, Any]:
        return _apply_new_build_in_place(
            w,
            class_name=class_name,
            ascendancy=eff_asc,
            secondary_ascendancy=eff_sec,
            level=level,
            name=name,
        )

    res = run_mutation(worker, _do_new_build, apply=apply, scope=BuildScope.REPLACES_BUILD)
    res["buildInfo"] = (res.get("detail") or {}).get("buildInfo")
    return res


def _resolve_id_or_title(
    items: list[dict[str, Any]],
    selector: int | str,
    *,
    id_key: str = "id",
    title_keys: tuple[str, ...] = ("title", "titleZh", "label", "labelZh"),
    entity_name: str = "item",
) -> int:
    s = str(selector).strip()
    if s.isdigit():
        val = int(s)
        if any(int(it.get(id_key, -1)) == val for it in items):
            return val
        if 1 <= val <= len(items):
            return int(items[val - 1][id_key])
    s_low = s.lower()
    for it in items:
        for k in title_keys:
            if s_low == strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    for it in items:
        for k in title_keys:
            if s_low in strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    raise ValueError(f"Could not find {entity_name} matching '{selector}'.")


def _apply_switch_main_in_place(
    w: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    changes: list[str] = []
    if loadout_index is not None:
        lo_res = w.call("list_loadouts", {})
        raw_list = lo_res.get("entries") if "entries" in lo_res else lo_res.get("list", [])
        real_loadouts = [
            it
            for it in (raw_list or [])
            if isinstance(it, dict)
            and str(it.get("kind", "loadout")).strip().lower() == "loadout"
            and strip_pob_escapes(str(it.get("label") or "")).strip().lower() not in LOADOUT_SENTINELS
        ]
        if not real_loadouts:
            raise ValueError("Build has no synced loadouts available.")
        s_lo = str(loadout_index).strip()
        if s_lo.isdigit():
            k = int(s_lo)
            if 1 <= k <= len(real_loadouts):
                target_raw_idx = int(real_loadouts[k - 1]["index"])
            elif any(int(it["index"]) == k for it in real_loadouts):
                target_raw_idx = k
            else:
                raise ValueError(f"Loadout #{k} out of range (1..{len(real_loadouts)}).")
        else:
            target_raw_idx = _resolve_id_or_title(
                real_loadouts, s_lo, id_key="index", title_keys=("label", "labelZh"), entity_name="loadout"
            )
        w.call("select_loadout", {"index": target_raw_idx})
        changes.append(f"loadout={loadout_index} (rawIndex={target_raw_idx})")
        if cache is not None:
            cache.invalidate_all()
    if spec_index is not None:
        specs_res = cache.get_specs() if cache is not None else w.call("list_specs", {})
        resolved_spec = _resolve_id_or_title(
            specs_res.get("specs", []), spec_index, id_key="index", entity_name="tree spec"
        )
        w.call("set_active_spec", {"index": resolved_spec})
        changes.append(f"activeSpec={resolved_spec}")
        if cache is not None:
            cache.invalidate("tree", "classes", "specs", "items", "skills")
    if item_set_id is not None:
        items_res = cache.get_items() if cache is not None else w.call("list_items", {})
        resolved_iset = _resolve_id_or_title(
            items_res.get("itemSets", []), item_set_id, id_key="id", entity_name="item set"
        )
        w.call("set_item_set", {"id": resolved_iset})
        changes.append(f"itemSetId={resolved_iset}")
        if cache is not None:
            cache.invalidate("items", "skills", "tree")
    if skill_set_id is not None:
        skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
        resolved_sset = _resolve_id_or_title(
            skills_res.get("skillSets", []), skill_set_id, id_key="id", entity_name="skill set"
        )
        w.call("set_skill_set", {"id": resolved_sset})
        changes.append(f"skillSetId={resolved_sset}")
        if cache is not None:
            cache.invalidate("skills")
    if config_set_id is not None:
        cfg_res = cache.get_config() if cache is not None else w.call("list_config", {})
        resolved_cset = _resolve_id_or_title(
            cfg_res.get("configSets", []), config_set_id, id_key="id", entity_name="config set"
        )
        w.call("set_config_set", {"id": resolved_cset})
        changes.append(f"configSetId={resolved_cset}")
        if cache is not None:
            cache.invalidate("config_schema")
    if skill_group is not None:
        skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, skill_group)
        w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})
        changes.append(f"mainSocketGroup={g_idx}")
        if cache is not None:
            cache.invalidate("skills")
    if main_skill_index is not None:
        w.call("set_build_field", {"field": "mainSkill", "value": int(main_skill_index)})
        changes.append(f"mainSkill={main_skill_index}")
    if skill_part_index is not None:
        w.call("set_build_field", {"field": "mainSkillPart", "value": int(skill_part_index)})
        changes.append(f"mainSkillPart={skill_part_index}")
    return {"changes": changes, "header": w.call("get_build_header", {})}


def switch_main(
    worker: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Switch active spec, main skill group, active skill/part, item/skill/config set, or synced loadout."""

    def _do_switch(w: BridgeWorker) -> dict[str, Any]:
        return _apply_switch_main_in_place(
            w,
            spec_index=spec_index,
            skill_group=skill_group,
            main_skill_index=main_skill_index,
            skill_part_index=skill_part_index,
            item_set_id=item_set_id,
            skill_set_id=skill_set_id,
            config_set_id=config_set_id,
            loadout_index=loadout_index,
        )

    switches_set = any(
        v is not None for v in (spec_index, item_set_id, skill_set_id, config_set_id, loadout_index)
    )
    return run_mutation(worker, _do_switch, apply=apply, scope=BuildScope.SETS if switches_set else BuildScope.ACTIVE_SETS)
