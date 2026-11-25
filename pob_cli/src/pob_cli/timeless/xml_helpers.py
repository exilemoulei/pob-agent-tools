"""XML passive-tree spec and `Allocates <Notable>` extraction helpers for `pob_cli.timeless`."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

from ..importers import share_code_to_xml

_ALLOCATES_RE = re.compile(r"Allocates ([^\r\n<]+)")


def _active_tree_spec(root: ET.Element) -> ET.Element | None:
    """Return the active `<Spec>` element under `<Tree>` (1-based `activeSpec`, defaulting to 1st)."""
    tree_el = root if root.tag == "Tree" else root.find(".//Tree")
    if tree_el is None:
        return None
    specs = tree_el.findall("Spec")
    if not specs:
        return None
    raw_spec = (tree_el.attrib.get("activeSpec") or "").strip()
    if raw_spec:
        try:
            spec_idx = int(raw_spec)
        except ValueError:
            spec_idx = 1
        if 1 <= spec_idx <= len(specs):
            return specs[spec_idx - 1]
    return specs[0]


def _extract_xml_allocated_nodes(xml: str) -> set[int] | None:
    """Return allocated node IDs from the active `<Tree><Spec nodes="..."/>` when explicitly present."""
    if not xml or "<Tree" not in xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    active_spec = _active_tree_spec(root)
    if active_spec is None:
        return None
    nodes_attr = (active_spec.attrib.get("nodes") or "").strip()
    if not nodes_attr:
        return None
    parsed = {int(tok) for tok in re.findall(r"\d+", nodes_attr)}
    return parsed or None


def _extract_allocated_notable_names(xml: str, code: str = "") -> set[str]:
    """Extract notable names from `Allocates <Name>` lines across active ItemSet slots and active Tree sockets."""
    text = xml
    if not text and code:
        try:
            text = share_code_to_xml(code)
        except Exception:
            text = ""
    if not text:
        return set()

    scoped_texts: list[str] | None = None
    if "<Items" in text:
        try:
            root = ET.fromstring(text)
            items_el = root if root.tag == "Items" else root.find(".//Items")
            if items_el is not None:
                items_by_id: dict[str, str] = {}
                for item_el in items_el.findall("Item"):
                    item_id = (item_el.attrib.get("id") or "").strip()
                    if item_id:
                        items_by_id[item_id] = "".join(item_el.itertext())
                active_set_id = (items_el.attrib.get("activeItemSet") or "").strip()
                item_sets = items_el.findall("ItemSet")
                active_set = None
                if item_sets:
                    if active_set_id:
                        active_set = next(
                            (s for s in item_sets if (s.attrib.get("id") or "").strip() == active_set_id),
                            None,
                        )
                    if active_set is None:
                        active_set = item_sets[0]
                slot_parent = active_set if active_set is not None else items_el
                slot_els = slot_parent.findall("Slot")
                active_spec = _active_tree_spec(root)
                tree_socket_els = active_spec.findall("./Sockets/Socket") if active_spec is not None else []
                if (slot_els or tree_socket_els) and items_by_id:
                    equipped_ids = {
                        (el.attrib.get("itemId") or "").strip()
                        for el in (*slot_els, *tree_socket_els)
                    } - {"", "0"}
                    scoped_texts = [items_by_id[iid] for iid in equipped_ids if iid in items_by_id]
        except ET.ParseError:
            scoped_texts = None

    sources = scoped_texts if scoped_texts is not None else [text]
    granted: set[str] = set()
    for src in sources:
        for match in _ALLOCATES_RE.findall(src):
            cleaned = html.unescape(match).strip()
            if cleaned:
                granted.add(cleaned)
    return granted


def _is_historic_socket_jewel(socket_info: dict[str, Any], jewels: dict[str, Any]) -> bool:
    """Detect equipped Historic jewels, including PoB Timeless Jewel Finder titles like 'Lethal Pride 12345; ...'."""
    if socket_info.get("baseName") == "Timeless Jewel":
        return True
    title = str(socket_info.get("title") or "")
    if not title:
        return False
    return any(title == k or title.startswith(k + " ") for k in jewels)
