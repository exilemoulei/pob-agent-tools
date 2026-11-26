"""Dynamic GGG X-Rate-Limit-* rate limiter with cross-process atomic persistence."""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..importers import _shared_cache_dir
from .ratelimit_state import (
    canonical_policy,
    merge_policy_entry,
    merge_states,
    merge_timestamps,
    parse_rate_limit_headers,
    policy_freshness,
    prune_state,
    read_disk_state,
    resolve_policy,
    save_disk_state,
    state_lock,
)
from .utils import (
    _MAX_TRACKED_TIMESTAMPS,
    DEFAULT_LIMIT_MARGIN,
    DEFAULT_MAX_WAIT_SEC,
    DEFAULT_RETRY_AFTER_SEC,
    RateLimited,
)


class TradeRateLimiter:
    """Dynamic GGG ``X-Rate-Limit-*`` rate limiter with atomic disk persistence.

    Follows Path of Building's ``TradeQueryRateLimiter.lua`` header parsing
    semantics while persisting state across short-lived CLI invocations in
    ``<cache_dir>/trade_ratelimit.json``.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        realm: str = "global",
        limit_margin: int = DEFAULT_LIMIT_MARGIN,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _shared_cache_dir()
        self.realm = str(realm or "global").strip().lower() or "global"
        self.limit_margin = max(0, int(limit_margin))
        self.max_wait_sec = max(0.0, float(max_wait_sec))
        self._sleep = sleep
        self._now = now
        self._state: dict[str, Any] = {"version": 1, "aliases": {}, "policies": {}}

    @property
    def state_path(self) -> Path:
        fname = "trade_ratelimit.json" if self.realm == "global" else f"trade_ratelimit_{self.realm}.json"
        return self.cache_dir / fname

    @property
    def lock_path(self) -> Path:
        fname = "trade_ratelimit.json.lock" if self.realm == "global" else f"trade_ratelimit_{self.realm}.json.lock"
        return self.cache_dir / fname

    @contextlib.contextmanager
    def _state_lock(
        self,
        *,
        timeout_sec: float = 5.0,
        stale_sec: float = 15.0,
    ) -> Iterator[None]:
        with state_lock(self.lock_path, timeout_sec=timeout_sec, stale_sec=stale_sec):
            yield

    @staticmethod
    def _canonical_policy(policy: str) -> str:
        return canonical_policy(policy)

    def _resolve_policy(self, state: dict[str, Any], policy: str) -> str:
        return resolve_policy(state, policy)

    def _read_disk_state(self) -> dict[str, Any] | None:
        return read_disk_state(self.state_path)

    @staticmethod
    def _policy_freshness(entry: dict[str, Any]) -> float:
        return policy_freshness(entry)

    def _prune_state(self, state: dict[str, Any], now: float) -> dict[str, Any]:
        return prune_state(state, now, max_wait_sec=self.max_wait_sec)

    @staticmethod
    def _merge_timestamps(a: Any, b: Any) -> list[float]:
        return merge_timestamps(a, b)

    @classmethod
    def _merge_policy_entry(cls, mem_val: dict[str, Any], disk_val: dict[str, Any]) -> dict[str, Any]:
        return merge_policy_entry(mem_val, disk_val)

    def _load_state(self, now: float) -> dict[str, Any]:
        state = merge_states(
            self._state,
            self._read_disk_state(),
            now,
            max_wait_sec=self.max_wait_sec,
        )
        self._state = state
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state = state
        save_disk_state(self.state_path, state)

    def before_request(self, policy: str) -> float:
        """Enforce GGG rate limits before issuing a request.

        Returns the number of seconds slept (``0.0`` when immediate).
        Raises ``RateLimited`` when an active penalty timeout is in effect or
        the required wait exceeds ``self.max_wait_sec``.
        """
        required_wait = 0.0
        with self._state_lock():
            now = float(self._now())
            state = self._load_state(now)
            policy_key = self._resolve_policy(state, policy)
            entry = state["policies"].get(policy_key)
            if not entry:
                return 0.0

            # 1. Check active penalty timeouts first (never sleep out a penalty).
            penalty_wait = 0.0
            rules = entry.get("rules") or {}
            for buckets in rules.values():
                for bucket in buckets.values():
                    active_timeout = int(bucket.get("active_timeout_sec") or 0)
                    updated_at = float(bucket.get("updated_at") or 0.0)
                    if active_timeout > 0:
                        remaining = (updated_at + active_timeout) - now
                        if remaining > 0:
                            penalty_wait = max(penalty_wait, remaining)
            if penalty_wait > 0:
                raise RateLimited(max(1, math.ceil(penalty_wait)))

            # 2. Compute required wait across Retry-After and saturated buckets
            #    (including optimistic in-flight reservations from concurrent workers).
            retry_after = entry.get("retry_after")
            if isinstance(retry_after, (int, float)) and float(retry_after) > now:
                required_wait = max(required_wait, float(retry_after) - now)

            timestamps = [
                float(t)
                for t in (entry.get("timestamps") or [])
                if isinstance(t, (int, float))
            ]
            in_flight = [
                float(t)
                for t in (entry.get("in_flight") or [])
                if isinstance(t, (int, float))
            ]

            for buckets in rules.values():
                for bucket in buckets.values():
                    max_hits = int(bucket.get("max_hits") or 0)
                    period_sec = int(bucket.get("period_sec") or 0)
                    cur_hits = int(bucket.get("cur_hits") or 0)
                    updated_at = float(bucket.get("updated_at") or 0.0)
                    if max_hits <= 0 or period_sec <= 0:
                        continue
                    effective_limit = max(1, max_hits - self.limit_margin)
                    active_in_flight = sum(
                        1 for t in in_flight if t > (now - period_sec) and t >= updated_at
                    )
                    effective_hits = cur_hits + active_in_flight
                    if effective_hits >= effective_limit and (updated_at + period_sec) > now:
                        window_ts = [t for t in timestamps if t > (now - period_sec)]
                        if len(window_ts) >= effective_hits:
                            recent = window_ts[-effective_hits:]
                            need_expire = effective_hits - effective_limit + 1
                            idx = min(need_expire - 1, len(recent) - 1)
                            bucket_wait = (recent[idx] + period_sec) - now
                        else:
                            bucket_wait = (updated_at + period_sec) - now
                        if bucket_wait > 0:
                            required_wait = max(required_wait, bucket_wait)

            if required_wait > self.max_wait_sec:
                raise RateLimited(max(1, math.ceil(required_wait)))

            # 3. Reserve an optimistic in-flight slot before releasing lock so
            #    concurrent workers cannot burst past effective_limit.
            if rules:
                reserve_ts = now + required_wait
                updated_entry = dict(entry)
                new_in_flight = sorted([*in_flight, reserve_ts])[-_MAX_TRACKED_TIMESTAMPS:]
                new_timestamps = sorted([*timestamps, reserve_ts])[-_MAX_TRACKED_TIMESTAMPS:]
                updated_entry["in_flight"] = new_in_flight
                updated_entry["timestamps"] = new_timestamps
                prev_upd = float(updated_entry.get("updated_at") or 0.0)
                updated_entry["updated_at"] = max(now, prev_upd + 1e-6)
                state["policies"][policy_key] = updated_entry
                state = self._prune_state(state, now)
                self._save_state(state)

        if required_wait <= 0:
            return 0.0
        self._sleep(required_wait)
        return required_wait

    def _apply_headers(
        self,
        policy: str,
        headers: Mapping[str, str],
        *,
        is_429: bool = False,
        default_retry_after: int = DEFAULT_RETRY_AFTER_SEC,
    ) -> int | None:
        now = float(self._now())
        canonical, header_policy, target_policy, parsed_rules, retry_after_sec = (
            parse_rate_limit_headers(
                policy,
                headers,
                now,
                is_429=is_429,
                default_retry_after=default_retry_after,
            )
        )

        with self._state_lock():
            state = self._load_state(now)
            if not parsed_rules and retry_after_sec is None:
                # Even without headers, release one in-flight reservation if present.
                resolved_key = self._resolve_policy(state, policy)
                existing = state["policies"].get(resolved_key)
                if isinstance(existing, dict) and existing.get("in_flight"):
                    updated = dict(existing)
                    in_flight = list(updated.get("in_flight") or [])
                    if in_flight:
                        in_flight.pop(0)
                    if in_flight:
                        updated["in_flight"] = in_flight
                    else:
                        updated.pop("in_flight", None)
                    prev_upd = float(updated.get("updated_at") or 0.0)
                    updated["updated_at"] = max(now, prev_upd + 1e-6)
                    state["policies"][resolved_key] = updated
                    state = self._prune_state(state, now)
                    self._save_state(state)
                return None

            if header_policy and canonical != header_policy:
                state["aliases"][canonical] = header_policy

            entry = dict(state["policies"].get(target_policy) or {})
            if parsed_rules:
                existing_rules = dict(entry.get("rules") or {})
                for rule_name, buckets in parsed_rules.items():
                    existing_rules[rule_name] = buckets
                entry["rules"] = existing_rules

            if retry_after_sec is not None:
                prev_retry = float(entry.get("retry_after") or 0.0)
                entry["retry_after"] = max(prev_retry, now + retry_after_sec)

            in_flight = [
                float(t)
                for t in (entry.get("in_flight") or [])
                if isinstance(t, (int, float))
            ]
            reserved = bool(in_flight)
            if in_flight:
                in_flight.pop(0)
            if in_flight:
                entry["in_flight"] = in_flight
            else:
                entry.pop("in_flight", None)

            timestamps = [
                float(t)
                for t in (entry.get("timestamps") or [])
                if isinstance(t, (int, float))
            ]
            if not reserved:
                # before_request already recorded a reserved request. Recording it again made the
                # window look twice as full, so the wait was measured from a later request.
                timestamps.append(now)
            entry["timestamps"] = sorted(timestamps)[-_MAX_TRACKED_TIMESTAMPS:]
            prev_upd = float(entry.get("updated_at") or 0.0)
            entry["updated_at"] = max(now, prev_upd + 1e-6)

            state["policies"][target_policy] = entry
            state = self._prune_state(state, now)
            self._save_state(state)
            return retry_after_sec

    def record_response(self, policy: str, headers: Mapping[str, str]) -> None:
        """Update and persist rate-limit state from a non-429 HTTP response."""
        self._apply_headers(policy, headers, is_429=False)

    def record_429(
        self,
        policy: str,
        headers: Mapping[str, str],
        default_retry_after: int = DEFAULT_RETRY_AFTER_SEC,
    ) -> int:
        """Record a 429 response's ``Retry-After`` and buckets before raising."""
        retry_after_sec = self._apply_headers(
            policy,
            headers,
            is_429=True,
            default_retry_after=default_retry_after,
        )
        return retry_after_sec if retry_after_sec is not None else default_retry_after
