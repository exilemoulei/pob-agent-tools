"""Per-(slot, rev) read-only RPC cache, soft 3rd-call loop-guard, and idempotent sourceHash tracking."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from ..snapshot import history_of

READ_CACHE_ACTIONS: frozenset[str] = frozenset(
    {"summary", "items", "skills", "audit", "calc", "diagnose", "map_regex"}
)

LOOP_GUARD_WARNING = (
    "[loop-guard] Query repeated 3+ times at unchanged rev; synthesize from existing output."
)

_COMMON_SKIP_KEYS: frozenset[str] = frozenset({"target", "main_target", "skip_periodic_check"})
_DIAGNOSE_LOAD_KEYS: frozenset[str] = frozenset(
    {"source", "xml_code", "char_name", "league", "realm", "source_hash", "sourceHash"}
)
_COMPARE_SKIP_KEYS: frozenset[str] = frozenset(
    {"ref_target", "source", "xml_code", "ref_source", "ref_xml_code"}
)
_PARAM_KEY_ALIASES: dict[str, str] = {
    "all_raw": "allRaw",
    "noRank": "no_rank",
    "noBreakdown": "no_breakdown",
    "maxBreakdowns": "max_breakdowns",
    "noAutoKeep": "no_auto_keep",
}
_BOOL_CANON_KEYS: frozenset[str] = frozenset(
    {"raw", "allRaw", "no_rank", "no_breakdown", "attribution", "no_auto_keep"}
)

# A URL or profile source is hashed by its address, not by the build behind it, and that build can
# change (a character swaps gear, a paste is edited). A repeat within this window is taken for an
# agent loop and skips the reload; after it the source is fetched again.
LIVE_SOURCE_RELOAD_AFTER_SEC = 120.0


def _is_address(src: str, params: dict[str, Any]) -> bool:
    """True when `src` says where a build lives: a URL, or an account the importer reads a character from."""
    realm = str(params.get("realm") or "").strip()
    if realm or src.startswith(("http://", "https://")):
        return True
    from ..profile_import import should_route_official_profile

    # `Name#1234 --char X` has no URL and no realm, and is still an account, not a build code.
    return should_route_official_profile(src, char_name=str(params.get("char_name") or "").strip() or None)


def is_live_source(params: dict[str, Any]) -> bool:
    """True when a load request names where the build lives (URL, account profile), not its content."""
    xml_code = params.get("xml_code")
    if isinstance(xml_code, str) and xml_code.strip():
        return False
    src = params.get("source")
    if isinstance(src, str) and src.strip():
        return _is_address(src.strip(), params)
    return bool(str(params.get("realm") or "").strip())


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
        if _is_address(s, params):
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
    stage_bits = [
        f"{k}={str(params.get(k)).strip()}"
        for k in ("stage", "spec", "item_set", "skill_set")
        if params.get(k) is not None and str(params.get(k)).strip()
    ]
    if stage_bits:
        canonical = canonical + "|" + "|".join(stage_bits)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _canonicalize_params(action: str, params: dict[str, Any], *, include_section: bool) -> str:
    norm: dict[str, Any] = {}
    for k, v in params.items():
        if k in _COMMON_SKIP_KEYS:
            continue
        if action in ("diagnose", "map_regex") and k in _DIAGNOSE_LOAD_KEYS:
            continue
        if action == "compare" and k in _COMPARE_SKIP_KEYS:
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
        if k != canon_k and params.get(canon_k) is not None:
            continue
        if canon_k in _BOOL_CANON_KEYS:
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
        norm.setdefault("no_auto_keep", False)
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
        norm.setdefault("no_auto_keep", False)
    elif action == "compare":
        norm.setdefault("no_auto_keep", False)

    return json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _is_hist_unsettled(hist: Any) -> bool:
    """Return True when a slot has an open BuildTransaction (e.g. `tx.restore(rebuild=False)`) or stale flag."""
    return bool(getattr(hist, "stale", False) or getattr(hist, "active", None) is not None)


def _key_mentions_slot(key: tuple[Any, ...], slot: str, stale_rev: int | None = None) -> bool:
    if len(key) == 5:
        return key[0] == slot and (stale_rev is None or key[1] != stale_rev)
    if len(key) == 6:
        if stale_rev is None:
            return key[0] == slot or key[2] == slot
        return (key[0] == slot and key[1] != stale_rev) or (key[2] == slot and key[3] != stale_rev)
    return False


class SlotReadCache:
    """Thread-safe bounded LRU per-(slot, rev) read cache with `compare` support & `sourceHash` tracking."""

    def __init__(self, max_entries: int = 256, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._max_hits = max(64, self.max_entries * 4)
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._hits: OrderedDict[tuple[Any, ...], int] = OrderedDict()
        self._slot_revs: dict[str, int] = {}
        self._slot_sources: dict[str, dict[str, Any]] = {}

    def _sync_slot_rev_unlocked(self, slot: str, rev: int) -> None:
        prev = self._slot_revs.get(slot)
        if prev is not None and prev != rev:
            for k in [k for k in self._entries if _key_mentions_slot(k, slot, stale_rev=rev)]:
                self._entries.pop(k, None)
            for k in [k for k in self._hits if _key_mentions_slot(k, slot, stale_rev=rev)]:
                self._hits.pop(k, None)
        self._slot_revs[slot] = rev

    def _put_entry_unlocked(
        self,
        entry_key: tuple[Any, ...],
        hit_key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        clean_copy = copy.deepcopy(payload)
        clean_copy.pop("cached", None)
        clean_copy.pop("loopGuardWarning", None)
        self._entries[entry_key] = clean_copy
        self._entries.move_to_end(entry_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        self._hits[hit_key] = max(1, self._hits.get(hit_key, 0) + 1)
        self._hits.move_to_end(hit_key)
        while len(self._hits) > self._max_hits:
            self._hits.popitem(last=False)

    def _get_entry_unlocked(
        self,
        entry_key: tuple[Any, ...],
        hit_key: tuple[Any, ...],
    ) -> tuple[dict[str, Any], int] | None:
        stored = self._entries.get(entry_key)
        if stored is None:
            return None
        self._entries.move_to_end(entry_key)
        count = self._hits.get(hit_key, 0) + 1
        self._hits[hit_key] = count
        self._hits.move_to_end(hit_key)
        while len(self._hits) > self._max_hits:
            self._hits.popitem(last=False)
        return copy.deepcopy(stored), count

    def tracked_slots(self) -> set[str]:
        with self._lock:
            slots = set(self._slot_revs) | set(self._slot_sources)
            for k in self._entries:
                if len(k) == 5:
                    slots.add(str(k[0]))
                elif len(k) == 6:
                    slots.add(str(k[0]))
                    slots.add(str(k[2]))
            return slots

    def invalidate_slot(self, slot: str) -> None:
        with self._lock:
            for k in [k for k in self._entries if _key_mentions_slot(k, slot)]:
                self._entries.pop(k, None)
            for k in [k for k in self._hits if _key_mentions_slot(k, slot)]:
                self._hits.pop(k, None)
            self._slot_revs.pop(slot, None)
            self._slot_sources.pop(slot, None)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits.clear()
            self._slot_revs.clear()
            self._slot_sources.clear()

    def record_slot_load(
        self,
        slot: str,
        rev: int,
        source_hash: str | None,
        load_meta: dict[str, Any] | None = None,
        *,
        live: bool = False,
    ) -> None:
        """Remember what `slot` was loaded from. `live` marks a source whose build can change behind
        the same hash (`is_live_source`), which `matching_slot_load` stops trusting after a while."""
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
                "liveSince": self._clock() if live else None,
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
            live_since = rec.get("liveSince")
            if live_since is not None and self._clock() - live_since > LIVE_SOURCE_RELOAD_AFTER_SEC:
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
            hit = self._get_entry_unlocked(
                (slot, rev, lang, action, comp_key),
                (slot, rev, lang, action, loop_key),
            )
            if hit is None:
                return None
            out, count = hit
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
        with self._lock:
            self._sync_slot_rev_unlocked(slot, rev)
            self._put_entry_unlocked(
                (slot, rev, lang, action, comp_key),
                (slot, rev, lang, action, loop_key),
                payload,
            )

    def lookup_compare(
        self,
        main_slot: str,
        main_rev: int,
        ref_slot: str,
        ref_rev: int,
        lang: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        comp_key = _canonicalize_params("compare", params, include_section=False)
        with self._lock:
            self._sync_slot_rev_unlocked(main_slot, main_rev)
            self._sync_slot_rev_unlocked(ref_slot, ref_rev)
            key = (main_slot, main_rev, ref_slot, ref_rev, lang, comp_key)
            hit = self._get_entry_unlocked(key, key)
            if hit is None:
                return None
            out, count = hit
        out["cached"] = True
        if count >= 3:
            out["loopGuardWarning"] = LOOP_GUARD_WARNING
        return out

    def store_compare(
        self,
        main_slot: str,
        main_rev: int,
        ref_slot: str,
        ref_rev: int,
        lang: str,
        params: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if not isinstance(payload, dict):
            return
        comp_key = _canonicalize_params("compare", params, include_section=False)
        with self._lock:
            self._sync_slot_rev_unlocked(main_slot, main_rev)
            self._sync_slot_rev_unlocked(ref_slot, ref_rev)
            key = (main_slot, main_rev, ref_slot, ref_rev, lang, comp_key)
            self._put_entry_unlocked(key, key, payload)

    def check_slot_read(
        self,
        target: str,
        worker: Any,
        eff_lang: str,
        action: str,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        """Return `(cache_lang, cached_payload_or_None)`, bypassing stale workers."""
        if action not in READ_CACHE_ACTIONS:
            return "", None
        w_loc = getattr(worker, "locale", None)
        cache_lang = f"{eff_lang}:{w_loc}" if w_loc else eff_lang
        hist = history_of(worker)
        if _is_hist_unsettled(hist):
            return cache_lang, None
        if action in ("diagnose", "map_regex"):
            has_load_arg = bool(
                params.get("source")
                or params.get("xml_code")
                or (params.get("realm") and str(params.get("realm")).lower() == "cn")
            )
            if has_load_arg:
                inc_hash = compute_source_hash(params)
                if (
                    self.matching_slot_load(
                        target,
                        hist.rev,
                        bool(getattr(worker, "last_good_code", None)),
                        inc_hash,
                    )
                    is None
                ):
                    return cache_lang, None
        return cache_lang, self.lookup(target, hist.rev, cache_lang, action, params)

    def store_slot_read(
        self,
        target: str,
        worker: Any,
        cache_lang: str,
        action: str,
        params: dict[str, Any],
        res_slot: Any,
    ) -> None:
        """Store a single-slot read response only when the worker is settled (`not stale`)."""
        if action not in READ_CACHE_ACTIONS or not isinstance(res_slot, dict):
            return
        hist = history_of(worker)
        if _is_hist_unsettled(hist):
            return
        self.store(target, hist.rev, cache_lang, action, params, res_slot)

    def run_cached_compare(
        self,
        main_slot: str,
        mw: Any,
        ref_slot: str,
        rw: Any,
        eff_lang: str,
        params: dict[str, Any],
        compute_fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Lookup or compute dual-slot `compare`, skipping stale/unloaded slots and partial attribution errors."""
        mh, rh = history_of(mw), history_of(rw)
        if (
            _is_hist_unsettled(mh)
            or _is_hist_unsettled(rh)
            or not getattr(mw, "last_good_code", None)
            or not getattr(rw, "last_good_code", None)
        ):
            return compute_fn()
        mw_loc, rw_loc = getattr(mw, "locale", None), getattr(rw, "locale", None)
        cmp_lang = f"{eff_lang}:{mw_loc or ''}:{rw_loc or ''}" if (mw_loc or rw_loc) else eff_lang
        m_rev, r_rev = mh.rev, rh.rev
        cached_cmp = self.lookup_compare(main_slot, m_rev, ref_slot, r_rev, cmp_lang, params)
        if cached_cmp is not None:
            return cached_cmp
        cmp_res = compute_fn()
        if (
            isinstance(cmp_res, dict)
            and not cmp_res.get("powerAttributionError")
            and not _is_hist_unsettled(history_of(mw))
            and not _is_hist_unsettled(history_of(rw))
        ):
            self.store_compare(main_slot, m_rev, ref_slot, r_rev, cmp_lang, params, cmp_res)
        return cmp_res
