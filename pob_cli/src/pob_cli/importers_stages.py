"""Multi-stage PoB XML (`Spec` / `ItemSet` / `SkillSet`) inspection and stage selection (L1)."""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from .game_data import fold_han

_POB_COLOR_RE = re.compile(r"\^x[0-9a-fA-F]{6}|\^\d")
_STAGE_FRAG_KEYS = frozenset({"stage", "spec", "item-set", "item_set", "itemset", "skill-set", "skill_set", "skillset"})


def _strip_colors(text: str | None) -> str:
    return _POB_COLOR_RE.sub("", str(text or "")).strip()


def split_source_stage_fragment(source: str) -> tuple[str, dict[str, str | None]]:
    """Extract optional `#stage=...` / `#spec=...` / `#item-set=...` / `#skill-set=...` fragment from source.

    Preserves PoE account discriminators such as `Account#1234` untouched.
    """
    raw = str(source or "")
    if "#" not in raw:
        return raw, {}
    base, frag = raw.rsplit("#", 1)
    frag_s = frag.strip()
    if "=" not in frag_s:
        return raw, {}

    parsed: dict[str, str | None] = {}
    matched_any = False
    for part in frag_s.split("&"):
        if "=" not in part:
            continue
        k_raw, v_raw = part.split("=", 1)
        k = k_raw.strip().lower()
        if k not in _STAGE_FRAG_KEYS:
            continue
        matched_any = True
        val = urllib.parse.unquote(v_raw.strip())
        if not val:
            continue
        if k == "stage":
            parsed["stage"] = val
        elif k == "spec":
            parsed["spec"] = val
        elif k in ("item-set", "item_set", "itemset"):
            parsed["item_set"] = val
        elif k in ("skill-set", "skill_set", "skillset"):
            parsed["skill_set"] = val

    if not matched_any:
        return raw, {}
    return base, parsed


def extract_available_stages(xml_text: str) -> dict[str, Any]:
    """Inspect `<Tree>`, `<Items>`, and `<Skills>` stages in a PoB build XML without calculating stats."""
    root = ET.fromstring(xml_text.lstrip("\ufeff"))

    tree_el = root.find("Tree")
    active_spec_raw = str(tree_el.attrib.get("activeSpec", "1")) if tree_el is not None else "1"
    try:
        active_spec_idx = int(active_spec_raw)
    except ValueError:
        active_spec_idx = 1

    specs: list[dict[str, Any]] = []
    if tree_el is not None:
        for idx, sp in enumerate(tree_el.findall("Spec"), start=1):
            raw_nodes = sp.attrib.get("nodes", "")
            node_cnt = len([tok for tok in raw_nodes.split(",") if tok.strip()]) if raw_nodes else 0
            title = _strip_colors(sp.attrib.get("title") or f"Spec {idx}")
            specs.append(
                {
                    "index": idx,
                    "id": str(idx),
                    "title": title,
                    "treeVersion": sp.attrib.get("treeVersion"),
                    "nodeCount": node_cnt,
                    "active": idx == active_spec_idx,
                }
            )

    items_el = root.find("Items")
    active_item_id = str(items_el.attrib.get("activeItemSet", "1")) if items_el is not None else "1"
    item_sets: list[dict[str, Any]] = []
    if items_el is not None:
        for idx, iset in enumerate(items_el.findall("ItemSet"), start=1):
            sid = str(iset.attrib.get("id") or idx)
            title = _strip_colors(iset.attrib.get("title") or f"Item Set {idx}")
            item_sets.append(
                {
                    "index": idx,
                    "id": sid,
                    "title": title,
                    "active": sid == active_item_id,
                }
            )

    skills_el = root.find("Skills")
    active_skill_id = str(skills_el.attrib.get("activeSkillSet", "1")) if skills_el is not None else "1"
    skill_sets: list[dict[str, Any]] = []
    if skills_el is not None:
        for idx, sset in enumerate(skills_el.findall("SkillSet"), start=1):
            sid = str(sset.attrib.get("id") or idx)
            title = _strip_colors(sset.attrib.get("title") or f"Skill Set {idx}")
            skill_sets.append(
                {
                    "index": idx,
                    "id": sid,
                    "title": title,
                    "active": sid == active_skill_id,
                }
            )

    has_multi = len(specs) > 1 or len(item_sets) > 1 or len(skill_sets) > 1
    return {
        "hasMultipleStages": has_multi,
        "specs": specs,
        "itemSets": item_sets,
        "skillSets": skill_sets,
        "active": {
            "spec": active_spec_idx,
            "itemSet": active_item_id,
            "skillSet": active_skill_id,
        },
    }


def _match_stage_entry(
    entries: list[dict[str, Any]],
    selector: str,
    *,
    allow_id: bool = True,
) -> dict[str, Any] | None:
    if not entries:
        return None
    sel = selector.strip()
    if not sel:
        return None
    if sel.isdigit():
        num = int(sel)
        if allow_id:
            by_id = next((e for e in entries if str(e.get("id")) == sel), None)
            if by_id is not None:
                return by_id
        if 1 <= num <= len(entries):
            return entries[num - 1]
        return None

    low = sel.lower()
    folded = fold_han(sel).lower()
    # Exact title match first, then substring match.
    for e in entries:
        t = str(e.get("title") or "")
        if t.lower() == low or fold_han(t).lower() == folded:
            return e
    for e in entries:
        t = str(e.get("title") or "")
        if low in t.lower() or (folded and folded in fold_han(t).lower()):
            return e
    return None


def apply_stage_selection_to_xml(
    xml_text: str,
    *,
    stage: str | None = None,
    spec: str | None = None,
    item_set: str | None = None,
    skill_set: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Select active `<Tree activeSpec>`, `<Items activeItemSet>`, and `<Skills activeSkillSet>` in PoB XML."""
    clean_xml = xml_text.lstrip("\ufeff")
    stages = extract_available_stages(clean_xml)
    if not any(x is not None and str(x).strip() for x in (stage, spec, item_set, skill_set)):
        return clean_xml, {"stages": stages, "selected": None}

    root = ET.fromstring(clean_xml)
    specs = stages["specs"]
    item_sets = stages["itemSets"]
    skill_sets = stages["skillSets"]

    chosen_spec: dict[str, Any] | None = None
    chosen_item: dict[str, Any] | None = None
    chosen_skill: dict[str, Any] | None = None

    stage_notes: list[str] = []
    if stage is not None and str(stage).strip():
        st_q = str(stage).strip()
        # Unified `--stage` matches Spec/ItemSet/SkillSet by 1-based positional index when numeric
        # (never XML id, which can diverge from display order, e.g. id="2" at index 1 and id="7" at index 2).
        chosen_spec = _match_stage_entry(specs, st_q, allow_id=False)
        chosen_item = _match_stage_entry(item_sets, st_q, allow_id=False)
        chosen_skill = _match_stage_entry(skill_sets, st_q, allow_id=False)
        if chosen_spec is None and chosen_item is None and chosen_skill is None:
            avail_s = ", ".join(f"{e['index']}:{e['title']}" for e in specs) or "-"
            avail_i = ", ".join(f"{e['id']}:{e['title']}" for e in item_sets) or "-"
            avail_k = ", ".join(f"{e['id']}:{e['title']}" for e in skill_sets) or "-"
            raise ValueError(
                f"Stage {st_q!r} did not match any Spec, ItemSet, or SkillSet. "
                f"Available specs=[{avail_s}], itemSets=[{avail_i}], skillSets=[{avail_k}]."
            )
        missed: list[str] = []
        if chosen_spec is None and len(specs) > 1:
            missed.append(f"Spec (count={len(specs)})")
        if chosen_item is None and len(item_sets) > 1:
            missed.append(f"ItemSet (count={len(item_sets)})")
        if chosen_skill is None and len(skill_sets) > 1:
            missed.append(f"SkillSet (count={len(skill_sets)})")
        if missed:
            stage_notes.append(
                f"Stage {st_q!r} did not match {', '.join(missed)}; kept default active set (use --spec/--item-set/--skill-set to override)."
            )

    if spec is not None and str(spec).strip():
        chosen_spec = _match_stage_entry(specs, str(spec).strip(), allow_id=False)
        if chosen_spec is None:
            avail = ", ".join(f"{e['index']}:{e['title']}" for e in specs) or "-"
            raise ValueError(f"Spec {spec!r} not found. Available specs: [{avail}].")

    if item_set is not None and str(item_set).strip():
        chosen_item = _match_stage_entry(item_sets, str(item_set).strip(), allow_id=True)
        if chosen_item is None:
            avail = ", ".join(f"{e['id']}:{e['title']}" for e in item_sets) or "-"
            raise ValueError(f"ItemSet {item_set!r} not found. Available itemSets: [{avail}].")

    if skill_set is not None and str(skill_set).strip():
        chosen_skill = _match_stage_entry(skill_sets, str(skill_set).strip(), allow_id=True)
        if chosen_skill is None:
            avail = ", ".join(f"{e['id']}:{e['title']}" for e in skill_sets) or "-"
            raise ValueError(f"SkillSet {skill_set!r} not found. Available skillSets: [{avail}].")

    tree_el = root.find("Tree")
    if chosen_spec is not None and tree_el is not None:
        tree_el.attrib["activeSpec"] = str(chosen_spec["index"])
    items_el = root.find("Items")
    if chosen_item is not None and items_el is not None:
        items_el.attrib["activeItemSet"] = str(chosen_item["id"])
    skills_el = root.find("Skills")
    if chosen_skill is not None and skills_el is not None:
        skills_el.attrib["activeSkillSet"] = str(chosen_skill["id"])

    updated_xml = ET.tostring(root, encoding="unicode")
    updated_stages = extract_available_stages(updated_xml)
    selected_info: dict[str, Any] = {}
    if stage:
        selected_info["stageQuery"] = stage
    if chosen_spec is not None:
        selected_info["spec"] = {"index": chosen_spec["index"], "title": chosen_spec["title"]}
    if chosen_item is not None:
        selected_info["itemSet"] = {"id": chosen_item["id"], "index": chosen_item["index"], "title": chosen_item["title"]}
    if chosen_skill is not None:
        selected_info["skillSet"] = {"id": chosen_skill["id"], "index": chosen_skill["index"], "title": chosen_skill["title"]}
    if stage_notes:
        selected_info["stageNotes"] = stage_notes
    return updated_xml, {"stages": updated_stages, "selected": selected_info}


def parse_xml_spec_snapshot(xml_text: str, spec_selector: str) -> dict[str, Any]:
    """Extract a single `<Spec>` passive tree snapshot from PoB XML for read-only `--spec-a/--spec-b` diffing."""
    clean_xml = xml_text.lstrip("\ufeff")
    stages = extract_available_stages(clean_xml)
    matched = _match_stage_entry(stages["specs"], spec_selector.strip(), allow_id=False)
    if matched is None:
        avail = ", ".join(f"{e['index']}:{e['title']}" for e in stages["specs"]) or "-"
        raise ValueError(f"Spec {spec_selector!r} not found. Available specs: [{avail}].")

    root = ET.fromstring(clean_xml)
    tree_el = root.find("Tree")
    spec_els = tree_el.findall("Spec") if tree_el is not None else []
    sp = spec_els[int(matched["index"]) - 1]

    alloc_ids = sorted(
        {
            int(tok.strip())
            for tok in (sp.attrib.get("nodes") or "").split(",")
            if tok.strip().isdigit()
        }
    )
    mastery_sel: dict[int, int] = {}
    raw_me = sp.attrib.get("masteryEffects") or ""
    for m in re.finditer(r"\{(\d+),(\d+)\}", raw_me):
        mastery_sel[int(m.group(1))] = int(m.group(2))

    sockets: list[dict[str, Any]] = []
    sock_el = sp.find("Sockets")
    if sock_el is not None:
        for sk in sock_el.findall("Socket"):
            nid_s = sk.attrib.get("nodeId", "")
            iid_s = sk.attrib.get("itemId", "0")
            if nid_s.isdigit():
                iid = int(iid_s) if iid_s.isdigit() else 0
                sockets.append({"nodeId": int(nid_s), "itemId": iid if iid > 0 else None})

    raw_cls = (sp.attrib.get("classId") or "").strip()
    return {
        "label": f"Spec #{matched['index']}: {matched['title']}",
        "specIndex": matched["index"],
        "title": matched["title"],
        "classId": int(raw_cls) if raw_cls.isdigit() else None,
        "treeVersion": sp.attrib.get("treeVersion"),
        "allocatedNodes": alloc_ids,
        "masterySelections": mastery_sel,
        "sockets": sockets,
        "dynamicNodes": [],
        "overrides": {},
        "itemsRes": {"items": [], "slots": []},
    }


def prepare_code_with_stages(
    raw_code_or_xml: str,
    *,
    decode_fn: Any,
    encode_fn: Any,
    stage: str | None = None,
    spec: str | None = None,
    item_set: str | None = None,
    skill_set: str | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Inspect XML stages and apply optional stage selection before `import_code`."""
    txt = raw_code_or_xml.lstrip("\ufeff").strip()
    is_xml = txt.startswith("<")
    if is_xml:
        xml_text = txt
    else:
        try:
            xml_text = decode_fn(txt)
        except ValueError:
            if any(x is not None and str(x).strip() for x in (stage, spec, item_set, skill_set)):
                raise
            return txt, None, None

    if not xml_text.lstrip().startswith("<"):
        return (encode_fn(xml_text) if is_xml else txt), None, None

    if any(x is not None and str(x).strip() for x in (stage, spec, item_set, skill_set)):
        updated_xml, info = apply_stage_selection_to_xml(
            xml_text, stage=stage, spec=spec, item_set=item_set, skill_set=skill_set
        )
        return encode_fn(updated_xml), info["stages"], info["selected"]

    try:
        stages = extract_available_stages(xml_text)
    except Exception:
        stages = None
    return (encode_fn(xml_text) if is_xml else txt), stages, None

