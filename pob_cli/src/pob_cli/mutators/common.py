"""Shared helpers, gem level validation, Agnostic CWS guard, and per-batch tab cache."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from .. import game_data as _game_data
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import normalize_equipment_slot

_EXCEPTIONAL_AWAKENED_KEEP: frozenset[str] = frozenset(
    {
        "awakened empower",
        "awakened enhance",
        "awakened enlighten",
    }
)

_EXCEPTIONAL_GEM_BOUNDS_FALLBACK: dict[str, tuple[int, int]] = {
    "greater spell echo": (3, 4),
    "cooldown recovery": (3, 4),
    "eldritch blasphemy": (3, 4),
    "hexpass": (3, 4),
    "hextoad": (3, 4),
    "congregation": (3, 4),
    "empower": (3, 4),
    "enhance": (3, 4),
    "enlighten": (3, 4),
    "eclipse": (3, 4),
    "pyre": (3, 4),
    "fissure": (3, 4),
    "gluttony": (3, 4),
    "hiveborn": (3, 4),
    "overheat": (3, 4),
    "pacifism": (3, 4),
    "bonespire": (3, 4),
    "communion": (3, 4),
    "awakened empower": (4, 5),
    "awakened enhance": (4, 5),
    "awakened enlighten": (4, 5),
}

_LOCAL_TW_GEM_ALIASES: dict[str, str] = {
    "高階施放迴響輔助": "Greater Spell Echo",
    "高階施放迴響": "Greater Spell Echo",
    "高阶施放回响辅助": "Greater Spell Echo",
    "高阶施放回响": "Greater Spell Echo",
    "冷卻恢復輔助": "Cooldown Recovery",
    "冷卻恢復": "Cooldown Recovery",
    "冷却回复辅助": "Cooldown Recovery",
    "異界詛咒光環輔助": "Eldritch Blasphemy",
    "異界詛咒光環": "Eldritch Blasphemy",
    "咒閃輔助": "Hexpass",
    "咒閃": "Hexpass",
    "聚眾輔助": "Congregation",
    "聚眾": "Congregation",
    "覺醒．賦予輔助": "Awakened Empower",
    "覺醒·賦予輔助": "Awakened Empower",
    "覺醒．元素集中輔助": "Awakened Elemental Focus",
    "覺醒·元素集中": "Awakened Elemental Focus",
    "覺醒．施放迴響輔助": "Awakened Spell Echo",
    "覺醒·施放迴響": "Awakened Spell Echo",
}

_THE_AGNOSTIC_NODE_ID = 19732
_AGNOSTIC_CWS_ITEM_TOKENS: tuple[str, ...] = (
    "valyrium",
    "女武神",
    "战日",
    "戰日",
    "bloodnotch",
    "血痕",
    "缺血症",
    "immutable force",
    "不变之力",
    "不變之力",
    "永恒之力",
)
_AGNOSTIC_CWS_SKILL_TOKENS: tuple[str, ...] = (
    "cast when stunned",
    "晕眩时施放",
    "暈眩時施放",
)

_APOSTROPHE_TRANS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u00b4": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_WS_COLLAPSE_RE = re.compile(r"\s+")
_CASCADE_WARN_THRESHOLD = 3


def _normalize_gem_key(name: str) -> str:
    cleaned = strip_pob_escapes(str(name or "")).strip()
    norm_tw = getattr(_game_data, "normalize_tw_query", None)
    if callable(norm_tw):
        try:
            mapped = norm_tw(cleaned)
            if isinstance(mapped, str) and mapped.strip():
                cleaned = mapped.strip()
        except Exception:
            pass
    if cleaned in _LOCAL_TW_GEM_ALIASES:
        cleaned = _LOCAL_TW_GEM_ALIASES[cleaned]
    low = cleaned.lower()
    if low.endswith(" support"):
        low = low[: -len(" support")].strip()
    return low


def _is_legacy_awakened_gem_safe(name: str) -> bool:
    fn = getattr(_game_data, "is_legacy_awakened_gem", None)
    if callable(fn):
        try:
            return bool(fn(name))
        except Exception:
            pass
    low = _normalize_gem_key(name)
    return low.startswith("awakened ") and low not in _EXCEPTIONAL_AWAKENED_KEEP


def _get_gem_level_bounds_safe(gem_name: str) -> dict[str, Any]:
    fn = getattr(_game_data, "get_gem_level_bounds", None)
    if callable(fn):
        try:
            bounds = fn(gem_name)
            if isinstance(bounds, dict):
                return bounds
        except Exception:
            pass
    low = _normalize_gem_key(gem_name)
    if low in _EXCEPTIONAL_GEM_BOUNDS_FALLBACK:
        nat_max, cor_max = _EXCEPTIONAL_GEM_BOUNDS_FALLBACK[low]
        return {
            "name": gem_name,
            "natural_max_level": nat_max,
            "corrupted_max_level": cor_max,
            "is_exceptional": True,
            "legacy_removed_328": False,
        }
    if _is_legacy_awakened_gem_safe(gem_name):
        return {
            "name": gem_name,
            "natural_max_level": 5,
            "corrupted_max_level": 6,
            "is_exceptional": False,
            "legacy_removed_328": True,
        }
    return {
        "name": gem_name,
        "natural_max_level": 20,
        "corrupted_max_level": 21,
        "is_exceptional": False,
        "legacy_removed_328": False,
    }


def _validate_gem_level(
    gem_name: str,
    level: int | None,
    *,
    allow_illegal_level: bool = False,
) -> tuple[int | None, list[str]]:
    """Validate requested gem level against PoE1 3.28+ Exceptional / Legacy bounds."""
    bounds = _get_gem_level_bounds_safe(gem_name)
    nat_max = int(bounds.get("natural_max_level") or 20)
    corrupt_max = int(bounds.get("corrupted_max_level") or (nat_max + 1))
    is_exceptional = bool(bounds.get("is_exceptional"))
    legacy_removed = bool(bounds.get("legacy_removed_328"))

    cautions: list[str] = []
    if is_exceptional and level is not None and int(level) > corrupt_max:
        msg = (
            f"Invalid level {level} for Exceptional Support '{gem_name}': "
            f"max natural level is Lv.{nat_max} (corrupted max Lv.{corrupt_max}). "
            "PoE1 3.28+ Exceptional supports cannot reach Lv.20."
        )
        if not allow_illegal_level:
            raise ValueError(msg)
        cautions.append(f"[ILLEGAL-GEM-LEVEL] {msg}")

    if legacy_removed:
        cautions.append(
            f"[LEGACY 3.28+ REMOVED] '{gem_name}' was drop-disabled in PoE1 3.28 (Mirage); "
            "unavailable in current leagues."
        )

    eff_level = nat_max if level is None else int(level)
    return eff_level, cautions


def _merge_cautions(*lists: list[str] | None) -> list[str]:
    merged: list[str] = []
    for lst in lists:
        for item in lst or []:
            if item and item not in merged:
                merged.append(item)
    return merged


def _check_agnostic_dealloc_caution(
    w: BridgeWorker,
    dealloc_ids: set[int],
    *,
    cache: _BatchCache | None = None,
) -> list[str]:
    """Warn if deallocating `The Agnostic` (19732) risks bricking a CWS / Valyrium / Bloodnotch loop."""
    if _THE_AGNOSTIC_NODE_ID not in dealloc_ids:
        return []

    items_res: dict[str, Any] = {}
    skills_res: dict[str, Any] = {}
    try:
        if cache is not None:
            items_res = cache.get_items() or {}
            skills_res = cache.get_skills() or {}
        else:
            res_pair = _call_batch(
                w,
                [("list_items", {}), ("list_skills", {})],
                raise_on_error=False,
            )
            if len(res_pair) >= 1 and not _is_batch_error(res_pair[0]):
                items_res = res_pair[0]
            if len(res_pair) >= 2 and not _is_batch_error(res_pair[1]):
                skills_res = res_pair[1]
    except Exception:
        return []

    has_cws_loop = False
    slots = items_res.get("slots") or []
    all_items = items_res.get("items") or []
    if slots:
        equipped_ids = {s.get("selItemId") for s in slots if s.get("selItemId")}
        items_to_scan = [it for it in all_items if it.get("id") in equipped_ids]
        slot_blobs = [
            strip_pob_escapes(
                " ".join(str(s.get(k) or "") for k in ("itemName", "title", "titleZh", "name", "nameZh"))
            ).lower()
            for s in slots
        ]
        if any(tok in blob for blob in slot_blobs for tok in _AGNOSTIC_CWS_ITEM_TOKENS):
            has_cws_loop = True
    else:
        items_to_scan = list(all_items)

    if not has_cws_loop:
        for it in items_to_scan:
            blob = strip_pob_escapes(
                " ".join(str(it.get(k) or "") for k in ("title", "titleZh", "name", "nameZh", "raw"))
            ).lower()
            if any(tok in blob for tok in _AGNOSTIC_CWS_ITEM_TOKENS):
                has_cws_loop = True
                break

    if not has_cws_loop:
        for grp in skills_res.get("groups") or []:
            if not grp.get("enabled", True):
                continue
            for gm in grp.get("gems") or []:
                if not gm.get("enabled", True):
                    continue
                blob = strip_pob_escapes(
                    " ".join(str(gm.get(k) or "") for k in ("name", "nameSpec", "nameZh", "skillId"))
                ).lower()
                if any(tok in blob for tok in _AGNOSTIC_CWS_SKILL_TOKENS):
                    has_cws_loop = True
                    break
            if has_cws_loop:
                break

    if not has_cws_loop:
        return []
    return [
        "[BUILD-BRICK RISK] Deallocating 'The Agnostic' (19732) restores Energy Shield > 0, "
        "granting inherent 50% Stun Avoidance and raising Valyrium's ES-based Stun Threshold "
        "— breaking Cast when Stunned / Bloodnotch recovery loops! "
        "Do NOT unspec The Agnostic even with Blood Magic."
    ]


def _is_batch_error(res: Any) -> bool:
    """Return True if a `call_batch(raise_on_error=False)` result represents an RPC error."""
    if not isinstance(res, dict):
        return True
    return res.get("ok") is False or bool(res.get("error")) or bool(res.get("_error"))


def _call_batch(
    worker: BridgeWorker,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout: float = 30.0,
    raise_on_error: bool = True,
) -> list[dict[str, Any]]:
    """Pipeline multiple RPCs via `worker.call_batch` when available, falling back to sequential `call`."""
    if not calls:
        return []
    batch_fn = getattr(worker, "call_batch", None)
    if callable(batch_fn):
        return batch_fn(calls, timeout=timeout, raise_on_error=raise_on_error)
    results: list[dict[str, Any]] = []
    for method, params in calls:
        if raise_on_error:
            results.append(worker.call(method, params or {}))
        else:
            try:
                results.append(worker.call(method, params or {}))
            except Exception as exc:
                results.append({"error": str(exc)})
    return results


def _fold_search_text(text: str | None) -> str:
    """Fold diacritics (NFKD), apostrophes/dashes, case, spaces, and Traditional -> Simplified Chinese.

    The Chinese fold matters because pob-zh.exe labels default to Traditional while queries are
    mostly Simplified; both sides go through this function, so either script matches.
    """
    if not text:
        return ""
    cleaned = strip_pob_escapes(str(text)).translate(_APOSTROPHE_TRANS)
    decomposed = unicodedata.normalize("NFKD", cleaned)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _game_data.fold_han(_WS_COLLAPSE_RE.sub(" ", stripped).strip().lower())


def _fold_no_apos(text: str | None) -> str:
    return _fold_search_text(text).replace("'", "")


def _resolve_slot_from_items(items_res: dict[str, Any], slot_query: str) -> str:
    """Resolve slot query against an already-fetched `list_items` response (avoids duplicate RPCs)."""
    q = normalize_equipment_slot(_game_data.fold_han(slot_query)) or slot_query.strip()
    slots = items_res.get("slots", [])

    m_jewel = re.match(r"^#?(\d+)$", q)
    if m_jewel:
        idx = int(m_jewel.group(1))
        for s in slots:
            if s.get("socketIndex") == idx:
                return s["name"]
        raise ValueError(f"Jewel socket #{idx} not found in build slots.")

    q_lower = q.lower()
    for s in slots:
        if (
            s.get("name", "").lower() == q_lower
            or _game_data.fold_han(s.get("label") or "").lower() == q_lower
            or _game_data.fold_han(s.get("labelZh") or "").lower() == q_lower
        ):
            return s["name"]

    for s in slots:
        if q_lower in s.get("name", "").lower() or q_lower in _game_data.fold_han(s.get("labelZh") or "").lower():
            return s["name"]

    raise ValueError(f"Equipment slot '{slot_query}' not found.")


def _normalize_keep_list(keep: Iterable[str] | str | None) -> list[str]:
    if keep is None:
        return []
    if isinstance(keep, str):
        return [x.strip() for x in keep.split(",") if x.strip()]
    out: list[str] = []
    for item in keep:
        if item is None:
            continue
        for part in str(item).split(","):
            s = part.strip()
            if s:
                out.append(s)
    return out


class _BatchCache:
    """Tab-level read cache scoped to a single `run_batch` / `run_sweep` execution."""

    def __init__(self, worker: BridgeWorker) -> None:
        self.worker = worker
        self.config: dict[str, Any] | None = None
        self.known_config_vars: dict[str, str] | None = None
        self.known_config_items: dict[str, dict[str, Any]] | None = None
        self.items: dict[str, Any] | None = None
        self.skills: dict[str, Any] | None = None
        self.tree: dict[str, Any] | None = None
        self.classes: dict[str, Any] | None = None
        self.specs: dict[str, Any] | None = None

    def get_config(self) -> dict[str, Any]:
        if self.config is None:
            self.config = self.worker.call("list_config", {})
            if self.known_config_items is None:
                self._index_config_vars(self.config)
        return self.config

    def _index_config_vars(self, cfg_res: dict[str, Any]) -> None:
        known: dict[str, str] = {}
        items: dict[str, dict[str, Any]] = {}
        for sec in cfg_res.get("sections", []):
            for item in sec.get("items", []):
                vname = item.get("var")
                if vname:
                    key = str(vname).lower()
                    known[key] = str(vname)
                    items[key] = item
        self.known_config_vars = known
        self.known_config_items = items

    def get_known_config_vars(self) -> dict[str, str]:
        if self.known_config_vars is None:
            self.get_config()
        return self.known_config_vars or {}

    def get_known_config_items(self) -> dict[str, dict[str, Any]]:
        if self.known_config_items is None:
            self.get_config()
        return self.known_config_items or {}

    def get_items(self) -> dict[str, Any]:
        if self.items is None:
            self.items = self.worker.call("list_items", {})
        return self.items

    def get_skills(self) -> dict[str, Any]:
        if self.skills is None:
            self.skills = self.worker.call("list_skills", {})
        return self.skills

    def get_tree(self) -> dict[str, Any]:
        if self.tree is None:
            self.tree = self.worker.call("get_tree_state", {})
        return self.tree

    def get_classes(self) -> dict[str, Any]:
        if self.classes is None:
            self.classes = self.worker.call("list_classes", {})
        return self.classes

    def get_specs(self) -> dict[str, Any]:
        if self.specs is None:
            self.specs = self.worker.call("list_specs", {})
        return self.specs

    def invalidate(self, *tabs: str) -> None:
        for tab in tabs:
            if tab == "config":
                self.config = None
            elif tab == "config_schema":
                self.config = None
                self.known_config_vars = None
                self.known_config_items = None
            elif tab == "items":
                self.items = None
            elif tab == "skills":
                self.skills = None
            elif tab == "tree":
                self.tree = None
            elif tab == "classes":
                self.classes = None
            elif tab == "specs":
                self.specs = None

    def invalidate_all(self) -> None:
        self.config = None
        self.known_config_vars = None
        self.known_config_items = None
        self.items = None
        self.skills = None
        self.tree = None
        self.classes = None
        self.specs = None


def _is_jewel_slot_name(slot_name: str | None) -> bool:
    if not slot_name:
        return False
    s = slot_name.strip().lower()
    return "jewel" in s or s.startswith("#") or s.isdigit()
