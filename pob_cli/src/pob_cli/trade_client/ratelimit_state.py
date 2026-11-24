"""Rate-limit state lockfile, header parsing, pruning, merging, and disk persistence."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .utils import (
    _MAX_TRACKED_TIMESTAMPS,
    _POLICY_ALIASES,
    _RULE_STATE_HEADER_RE,
    DEFAULT_RETRY_AFTER_SEC,
    _atomic_write_json,
    _is_lockfile_stale,
    _parse_buckets,
    _parse_retry_after,
    _release_lockfile,
    _try_break_stale_lockfile,
)


@contextlib.contextmanager
def state_lock(
    lock_path: Path,
    *,
    timeout_sec: float = 5.0,
    stale_sec: float = 15.0,
) -> Iterator[None]:
    """Short cross-process atomic lockfile around trade_ratelimit.json read-modify-write."""
    owner_token: str | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.05, float(timeout_sec))
        deadline_break_attempted = False
        stale_breaks = 0
        while True:
            token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(
                        fd,
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "tid": threading.get_ident(),
                                "token": token,
                                "ts": time.time(),
                            }
                        ).encode("utf-8"),
                    )
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                else:
                    try:
                        os.close(fd)
                    except BaseException:
                        try:
                            lock_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                owner_token = token
                break
            except FileExistsError:
                now_mono = time.monotonic()
                if _is_lockfile_stale(lock_path, stale_sec=stale_sec):
                    if _try_break_stale_lockfile(lock_path):
                        stale_breaks += 1
                        if now_mono < deadline:
                            if stale_breaks > 1:
                                time.sleep(0.01)
                            continue
                        if not deadline_break_attempted:
                            deadline_break_attempted = True
                            continue
                    elif now_mono < deadline:
                        time.sleep(0.01)
                        continue
                    else:
                        break
                if now_mono >= deadline:
                    if not deadline_break_attempted:
                        deadline_break_attempted = True
                        if _try_break_stale_lockfile(lock_path):
                            continue
                    break
                time.sleep(0.01)
            except OSError:
                # Read-only or inaccessible cache directory: fall back to in-memory state.
                break
    except OSError:
        owner_token = None
    try:
        yield
    finally:
        if owner_token is not None:
            _release_lockfile(lock_path, owner_token)


def canonical_policy(policy: str) -> str:
    return _POLICY_ALIASES.get(policy, policy)


def resolve_policy(state: dict[str, Any], policy: str) -> str:
    canonical = canonical_policy(policy)
    aliases = state.get("aliases")
    if isinstance(aliases, dict):
        mapped = aliases.get(canonical)
        if isinstance(mapped, str) and mapped:
            return mapped
    return canonical


def read_disk_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def policy_freshness(entry: dict[str, Any]) -> float:
    return float(entry.get("updated_at") or 0.0)


def prune_state(state: dict[str, Any], now: float, *, max_wait_sec: float) -> dict[str, Any]:
    pruned_policies: dict[str, Any] = {}
    policies = state.get("policies")
    if isinstance(policies, dict):
        for policy_name, raw_entry in policies.items():
            if not isinstance(raw_entry, dict):
                continue
            entry: dict[str, Any] = {}
            retry_after = raw_entry.get("retry_after")
            if isinstance(retry_after, (int, float)) and float(retry_after) > now:
                entry["retry_after"] = float(retry_after)

            max_period = 0
            pruned_rules: dict[str, dict[str, dict[str, Any]]] = {}
            raw_rules = raw_entry.get("rules")
            if isinstance(raw_rules, dict):
                for rule_name, raw_buckets in raw_rules.items():
                    if not isinstance(raw_buckets, dict):
                        continue
                    kept_buckets: dict[str, dict[str, Any]] = {}
                    for period_key, bucket in raw_buckets.items():
                        if not isinstance(bucket, dict):
                            continue
                        try:
                            period_sec = int(bucket.get("period_sec") or int(period_key))
                            max_hits = int(bucket.get("max_hits") or 0)
                            penalty_sec = int(bucket.get("penalty_sec") or 0)
                            cur_hits = int(bucket.get("cur_hits") or 0)
                            active_timeout_sec = int(bucket.get("active_timeout_sec") or 0)
                            updated_at = float(bucket.get("updated_at") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if period_sec <= 0:
                            continue
                        window_active = (updated_at + period_sec) > now
                        timeout_active = (
                            active_timeout_sec > 0
                            and (updated_at + active_timeout_sec) > now
                        )
                        if not window_active and not timeout_active:
                            continue
                        max_period = max(max_period, period_sec)
                        kept_buckets[str(period_sec)] = {
                            "max_hits": max_hits,
                            "period_sec": period_sec,
                            "penalty_sec": penalty_sec,
                            "cur_hits": cur_hits,
                            "active_timeout_sec": active_timeout_sec,
                            "updated_at": updated_at,
                        }
                    if kept_buckets:
                        pruned_rules[str(rule_name)] = kept_buckets

            if pruned_rules:
                entry["rules"] = pruned_rules

            raw_ts = raw_entry.get("timestamps")
            ts_upper = now + max_wait_sec + 5.0
            if isinstance(raw_ts, list) and max_period > 0:
                cutoff = now - max_period
                kept_ts = sorted(
                    float(t)
                    for t in raw_ts
                    if isinstance(t, (int, float)) and float(t) > cutoff and float(t) <= ts_upper
                )[-_MAX_TRACKED_TIMESTAMPS:]
                if kept_ts:
                    entry["timestamps"] = kept_ts

            raw_in_flight = raw_entry.get("in_flight")
            if isinstance(raw_in_flight, list) and max_period > 0:
                inf_cutoff = now - min(float(max_period), 30.0)
                kept_inf = sorted(
                    float(t)
                    for t in raw_in_flight
                    if isinstance(t, (int, float)) and float(t) > inf_cutoff and float(t) <= ts_upper
                )[-_MAX_TRACKED_TIMESTAMPS:]
                if kept_inf:
                    entry["in_flight"] = kept_inf

            if entry:
                entry["updated_at"] = float(raw_entry.get("updated_at") or 0.0)
                pruned_policies[str(policy_name)] = entry

    aliases = state.get("aliases")
    kept_aliases = (
        {str(k): str(v) for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(aliases, dict)
        else {}
    )
    return {"version": 1, "aliases": kept_aliases, "policies": pruned_policies}


def merge_timestamps(a: Any, b: Any) -> list[float]:
    ca: Counter[float] = Counter(float(t) for t in (a or []) if isinstance(t, (int, float)))
    cb: Counter[float] = Counter(float(t) for t in (b or []) if isinstance(t, (int, float)))
    merged: list[float] = []
    for ts in sorted(set(ca) | set(cb)):
        merged.extend([ts] * max(ca.get(ts, 0), cb.get(ts, 0)))
    return merged[-_MAX_TRACKED_TIMESTAMPS:]


def merge_policy_entry(mem_val: dict[str, Any], disk_val: dict[str, Any]) -> dict[str, Any]:
    disk_fresh = policy_freshness(disk_val)
    mem_fresh = policy_freshness(mem_val)
    primary, secondary = (disk_val, mem_val) if disk_fresh >= mem_fresh else (mem_val, disk_val)
    merged: dict[str, Any] = dict(primary)

    # Preserve union of rules, keeping the fresher snapshot per rule
    merged_rules: dict[str, Any] = {}
    sec_rules = secondary.get("rules")
    if isinstance(sec_rules, dict):
        merged_rules.update(sec_rules)
    pri_rules = primary.get("rules")
    if isinstance(pri_rules, dict):
        merged_rules.update(pri_rules)
    if merged_rules:
        merged["rules"] = merged_rules

    # Never drop an active 429 retry_after from a concurrent worker
    mem_retry = mem_val.get("retry_after")
    disk_retry = disk_val.get("retry_after")
    retry_vals = [float(v) for v in (mem_retry, disk_retry) if isinstance(v, (int, float))]
    if retry_vals:
        merged["retry_after"] = max(retry_vals)

    # Union request timestamps across workers without duplicating identical copies
    merged_ts = merge_timestamps(mem_val.get("timestamps"), disk_val.get("timestamps"))
    if merged_ts:
        merged["timestamps"] = merged_ts

    merged["updated_at"] = max(disk_fresh, mem_fresh)
    return merged


def merge_states(
    mem_state: dict[str, Any],
    disk: dict[str, Any] | None,
    now: float,
    *,
    max_wait_sec: float,
) -> dict[str, Any]:
    merged_aliases: dict[str, str] = {}
    merged_policies: dict[str, Any] = {}

    mem_aliases = mem_state.get("aliases")
    if isinstance(mem_aliases, dict):
        merged_aliases.update(mem_aliases)
    mem_policies = mem_state.get("policies")
    if isinstance(mem_policies, dict):
        for key, val in mem_policies.items():
            if isinstance(val, dict):
                merged_policies[key] = val

    if disk is not None:
        disk_aliases = disk.get("aliases")
        if isinstance(disk_aliases, dict):
            merged_aliases.update(disk_aliases)
        disk_policies = disk.get("policies")
        if isinstance(disk_policies, dict):
            for key, disk_val in disk_policies.items():
                if not isinstance(disk_val, dict):
                    continue
                mem_val = merged_policies.get(key)
                if mem_val is None:
                    merged_policies[key] = disk_val
                else:
                    merged_policies[key] = merge_policy_entry(mem_val, disk_val)

    return prune_state(
        {"version": 1, "aliases": merged_aliases, "policies": merged_policies},
        now,
        max_wait_sec=max_wait_sec,
    )


def save_disk_state(state_path: Path, state: dict[str, Any]) -> None:
    _atomic_write_json(state_path, state)


def parse_rate_limit_headers(
    policy: str,
    headers: Mapping[str, str],
    now: float,
    *,
    is_429: bool = False,
    default_retry_after: int = DEFAULT_RETRY_AFTER_SEC,
) -> tuple[str, str, str, dict[str, dict[str, dict[str, Any]]], int | None]:
    """Parse GGG ``X-Rate-Limit-*`` and ``Retry-After`` response headers."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}

    canonical = canonical_policy(policy)
    header_policy = lowered.get("x-rate-limit-policy", "").strip()
    target_policy = header_policy or canonical

    # Identify rules from X-Rate-Limit-Rules (or infer from *-state headers).
    rules_header = lowered.get("x-rate-limit-rules", "").strip()
    rule_names: list[str] = []
    if rules_header:
        for item in rules_header.split(","):
            name = item.strip().lower()
            if name and name not in rule_names:
                rule_names.append(name)
    else:
        for key in lowered:
            match = _RULE_STATE_HEADER_RE.match(key)
            if match:
                name = match.group(1)
                if name not in rule_names:
                    rule_names.append(name)

    parsed_rules: dict[str, dict[str, dict[str, Any]]] = {}
    for rule_name in rule_names:
        limits_map = _parse_buckets(lowered.get(f"x-rate-limit-{rule_name}"))
        state_map = _parse_buckets(lowered.get(f"x-rate-limit-{rule_name}-state"))
        if not limits_map and not state_map:
            continue
        buckets: dict[str, dict[str, Any]] = {}
        all_periods = set(limits_map) | set(state_map)
        for period_sec in sorted(all_periods):
            max_hits, _, penalty_sec = limits_map.get(period_sec, (0, period_sec, 0))
            cur_hits, _, active_timeout_sec = state_map.get(period_sec, (0, period_sec, 0))
            buckets[str(period_sec)] = {
                "max_hits": max_hits,
                "period_sec": period_sec,
                "penalty_sec": penalty_sec,
                "cur_hits": cur_hits,
                "active_timeout_sec": active_timeout_sec,
                "updated_at": now,
            }
        if buckets:
            parsed_rules[rule_name] = buckets

    retry_after_sec = _parse_retry_after(
        lowered.get("retry-after"),
        now,
        default=default_retry_after if is_429 else None,
    )
    return canonical, header_policy, target_policy, parsed_rules, retry_after_sec
