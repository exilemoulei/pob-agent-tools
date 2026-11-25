"""Per-(slot, rev) read-only RPC cache, soft 3rd-call loop-guard, and idempotent sourceHash tracking."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any

READ_CACHE_ACTIONS: frozenset[str] = frozenset(
    {"summary", "items", "skills", "audit", "calc", "diagnose"}
)

LOOP_GUARD_WARNING = (
    "[loop-guard] Query repeated 3+ times at unchanged rev; synthesize from existing output."
)

_COMMON_SKIP_KEYS: frozenset[str] = frozenset({"target", "main_target", "skip_periodic_check"})
_DIAGNOSE_LOAD_KEYS: frozenset[str] = frozenset(
    {"source", "xml_code", "char_name", "league", "realm", "source_hash", "sourceHash"}
)
_PARAM_KEY_ALIASES: dict[str, str] = {
    "all_raw": "allRaw",
    "noRank": "no_rank",
    "noBreakdown": "no_breakdown",
    "maxBreakdowns": "max_breakdowns",
}


def compute_source_hash(params: dict[str, Any]) -> str | None:
    """Compute a deterministic 16-hex content hash for a slot load / `diagnose <source>` request."""
    explicit = params.get("source_hash") or params.get("sourceHash")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    xml_code = params.get("xml_code")
    src = params.get("source")
    realm = str(params.get("realm") or "").strip().lower()

    canonical: str | None = None
    if isinstance(xml_code, str) and xml_code.strip():
        canonical = "code:" + xml_code.strip()
    elif isinstance(src, str) and src.strip():
        s = src.strip()
        if s.startswith(("http://", "https://")) or realm:
            char_nm = str(params.get("char_name") or "").strip()
            lg = str(params.get("league") or "").strip()
            canonical = f"src:{s}|char={char_nm}|league={lg}|realm={realm}"
        elif s.lstrip().startswith("<"):
            try:
                from ..importers import xml_to_share_code

                canonical = "code:" + xml_to_share_code(s).strip()
            except Exception:
                canonical = "xml:" + s
        else:
            canonical = "code:" + s
    elif realm == "cn":
        char_nm = str(params.get("char_name") or "").strip()
        lg = str(params.get("league") or "").strip()
        canonical = f"cn:|char={char_nm}|league={lg}"

    if not canonical:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _canonicalize_params(action: str, params: dict[str, Any], *, include_section: bool) -> str:
    norm: dict[str, Any] = {}
    for k, v in params.items():
        if k in _COMMON_SKIP_KEYS:
            continue
        if action == "diagnose" and k in _DIAGNOSE_LOAD_KEYS:
            continue
        if action == "diagnose" and k == "section":
            if include_section:
                sec = str(v or "all").strip().lower()
                if sec and sec != "all":
                    norm["section"] = sec
            continue
        canon_k = _PARAM_KEY_ALIASES.get(k, k)
        if v is None:
            continue
        if canon_k in ("raw", "allRaw", "no_rank", "no_breakdown", "attribution"):
            norm[canon_k] = bool(v)
        elif isinstance(v, tuple):
            norm[canon_k] = list(v)
        else:
            norm[canon_k] = v

    if action == "items":
        norm.setdefault("raw", False)
        norm.setdefault("allRaw", False)
    elif action == "audit":
        norm.setdefault("attribution", True)
    elif action == "calc":
        kws = norm.get("keywords")
        kw = norm.get("keyword")
        if kws is None and kw is not None:
            norm["keywords"] = [str(kw)]
        if isinstance(norm.get("keywords"), list) and norm["keywords"] and "keyword" not in norm:
            norm["keyword"] = str(norm["keywords"][0])
        norm.setdefault("limit", 20)
        norm.setdefault("max_breakdowns", 30)
        norm.setdefault("no_breakdown", False)
    elif action == "diagnose":
        norm.setdefault("top", 5)
        norm.setdefault("no_rank", False)

    return json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


class SlotReadCache:
    """Thread-safe per-(slot, rev) read cache with 3rd-call soft `[loop-guard]` & `sourceHash` tracking."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, int, str, str, str], dict[str, Any]] = {}
        self._hits: dict[tuple[str, int, str, str, str], int] = {}
        self._slot_revs: dict[str, int] = {}
        self._slot_sources: dict[str, dict[str, Any]] = {}

    def _sync_slot_rev_unlocked(self, slot: str, rev: int) -> None:
        prev = self._slot_revs.get(slot)
        if prev is not None and prev != rev:
            for k in [k for k in self._entries if k[0] == slot and k[1] != rev]:
                self._entries.pop(k, None)
            for k in [k for k in self._hits if k[0] == slot and k[1] != rev]:
                self._hits.pop(k, None)
        self._slot_revs[slot] = rev

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits.clear()
            self._slot_revs.clear()

    def record_slot_load(
        self,
        slot: str,
        rev: int,
        source_hash: str | None,
        load_meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            if not source_hash:
                self._slot_sources.pop(slot, None)
                return
            kept_meta: dict[str, Any] = {}
            if isinstance(load_meta, dict):
                for key in ("importNotes", "translateWarnings", "character"):
                    if load_meta.get(key):
                        kept_meta[key] = copy.deepcopy(load_meta[key])
            self._slot_sources[slot] = {
                "sourceHash": source_hash,
                "rev": rev,
                "loadMeta": kept_meta,
            }

    def matching_slot_load(
        self,
        slot: str,
        rev: int,
        has_loaded_build: bool,
        source_hash: str | None,
    ) -> dict[str, Any] | None:
        if not has_loaded_build or not source_hash:
            return None
        with self._lock:
            rec = self._slot_sources.get(slot)
            if rec is None or rec.get("sourceHash") != source_hash or rec.get("rev") != rev:
                return None
            return copy.deepcopy(rec.get("loadMeta") or {})

    def lookup(
        self,
        slot: str,
        rev: int,
        lang: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if action not in READ_CACHE_ACTIONS:
            return None
        comp_key = _canonicalize_params(action, params, include_section=False)
        loop_key = _canonicalize_params(action, params, include_section=True)
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            stored = self._entries.get((slot, rev, lang, action, comp_key))
            if stored is None:
                return None
            hit_tuple = (slot, rev, lang, action, loop_key)
            count = self._hits.get(hit_tuple, 0) + 1
            self._hits[hit_tuple] = count
            out = copy.deepcopy(stored)
        out["cached"] = True
        if count >= 3:
            out["loopGuardWarning"] = LOOP_GUARD_WARNING
        return out

    def store(
        self,
        slot: str,
        rev: int,
        lang: str,
        action: str,
        params: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if action not in READ_CACHE_ACTIONS or not isinstance(payload, dict):
            return
        comp_key = _canonicalize_params(action, params, include_section=False)
        loop_key = _canonicalize_params(action, params, include_section=True)
        clean_copy = copy.deepcopy(payload)
        clean_copy.pop("cached", None)
        clean_copy.pop("loopGuardWarning", None)
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            self._entries[(slot, rev, lang, action, comp_key)] = clean_copy
            hit_tuple = (slot, rev, lang, action, loop_key)
            self._hits[hit_tuple] = max(1, self._hits.get(hit_tuple, 0) + 1)
