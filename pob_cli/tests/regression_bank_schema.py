"""Cross-gen (PoE1 / PoE2 C2) shared agent regression bank manifest schema & deterministic validator."""

from __future__ import annotations

import math
import re
from typing import Any

SCHEMA_VERSION = "1.0"
ALLOWED_GAMES: frozenset[str] = frozenset({"poe1", "poe2"})
ALLOWED_BUILD_SOURCE_KINDS: frozenset[str] = frozenset({"xml", "code", "url", "fixture"})
ALLOWED_CHECK_TYPES: frozenset[str] = frozenset(
    {"contains", "regex", "numeric_range", "json_path", "command_exit"}
)
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_LOCAL_PATH_RE = re.compile(r"(?:[A-Za-z]:[/\\]|/(?:Users|home)/)")


def _validate_build_source(val: Any, prefix: str) -> list[str]:
    errs: list[str] = []
    if isinstance(val, str):
        s = val.strip()
        if not s:
            errs.append(f"{prefix}.build_source must be a non-empty string or object")
        elif _LOCAL_PATH_RE.search(s):
            errs.append(f"{prefix}.build_source must not contain machine-local filesystem paths")
        return errs
    if not isinstance(val, dict):
        return [f"{prefix}.build_source must be a non-empty string or object"]
    kind = val.get("kind")
    if kind not in ALLOWED_BUILD_SOURCE_KINDS:
        errs.append(f"{prefix}.build_source.kind must be one of {sorted(ALLOWED_BUILD_SOURCE_KINDS)}")
    value = val.get("value")
    if not isinstance(value, str) or not value.strip():
        errs.append(f"{prefix}.build_source.value must be a non-empty string")
    elif _LOCAL_PATH_RE.search(value):
        errs.append(f"{prefix}.build_source.value must not contain machine-local filesystem paths")
    return errs


def _validate_check(chk: Any, prefix: str) -> list[str]:
    if not isinstance(chk, dict):
        return [f"{prefix} must be an object"]
    errs: list[str] = []
    ctype = chk.get("type")
    if ctype not in ALLOWED_CHECK_TYPES:
        errs.append(f"{prefix}.type must be one of {sorted(ALLOWED_CHECK_TYPES)}")
        return errs
    if ctype in ("contains", "regex"):
        pat = chk.get("pattern") or chk.get("value")
        if not isinstance(pat, str) or not pat.strip():
            errs.append(f"{prefix} ({ctype}) requires non-empty 'pattern' or 'value'")
        elif ctype == "regex":
            try:
                re.compile(pat)
            except re.error as exc:
                errs.append(f"{prefix} invalid regex: {exc}")
    elif ctype == "numeric_range":
        if not isinstance(chk.get("path"), str) or not chk["path"].strip():
            errs.append(f"{prefix} (numeric_range) requires non-empty 'path'")
        lo, hi = chk.get("min"), chk.get("max")
        if lo is None and hi is None:
            errs.append(f"{prefix} (numeric_range) requires 'min' and/or 'max'")
        lo_num: float | None = None
        hi_num: float | None = None
        for bound_name, raw_val in (("min", lo), ("max", hi)):
            if raw_val is None:
                continue
            if isinstance(raw_val, bool):
                errs.append(f"{prefix} (numeric_range) '{bound_name}' must be a finite number, not boolean")
                continue
            try:
                fval = float(raw_val)
            except (TypeError, ValueError):
                errs.append(f"{prefix} (numeric_range) '{bound_name}' must be a finite number")
                continue
            if not math.isfinite(fval):
                errs.append(f"{prefix} (numeric_range) '{bound_name}' must be finite")
                continue
            if bound_name == "min":
                lo_num = fval
            else:
                hi_num = fval
        if lo_num is not None and hi_num is not None and lo_num > hi_num:
            errs.append(f"{prefix} (numeric_range) has min > max")
    elif ctype == "json_path":
        if not isinstance(chk.get("path"), str) or not chk["path"].strip():
            errs.append(f"{prefix} (json_path) requires non-empty 'path'")
        if "expected" not in chk:
            errs.append(f"{prefix} (json_path) requires 'expected'")
    elif ctype == "command_exit":
        code_val = chk.get("code")
        if isinstance(code_val, bool) or not isinstance(code_val, int):
            errs.append(f"{prefix} (command_exit) requires integer 'code'")
    return errs


def validate_regression_case(case: Any, *, index: int = 0) -> list[str]:
    """Validate a single PoE1/PoE2 regression bank case; returns a list of error strings (empty = valid)."""
    prefix = f"cases[{index}]"
    if not isinstance(case, dict):
        return [f"{prefix} must be an object"]

    errs: list[str] = []
    cid = case.get("id")
    if not isinstance(cid, str) or not _CASE_ID_RE.match(cid):
        errs.append(f"{prefix}.id must match {_CASE_ID_RE.pattern}")
    else:
        prefix = f"case '{cid}'"

    game = case.get("game")
    if game not in ALLOWED_GAMES:
        errs.append(f"{prefix}.game must be one of {sorted(ALLOWED_GAMES)}")

    for req_str in ("title", "prompt"):
        v = case.get(req_str)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{prefix}.{req_str} must be a non-empty string")

    errs.extend(_validate_build_source(case.get("build_source"), prefix))

    checks = case.get("ground_truth_checks")
    if not isinstance(checks, list) or not checks:
        errs.append(f"{prefix}.ground_truth_checks must be a non-empty list")
    else:
        for i, chk in enumerate(checks):
            errs.extend(_validate_check(chk, f"{prefix}.ground_truth_checks[{i}]"))

    forb = case.get("forbidden_patterns")
    if not isinstance(forb, list):
        errs.append(f"{prefix}.forbidden_patterns must be a list")
    else:
        for i, pat in enumerate(forb):
            if not isinstance(pat, str) or not pat.strip():
                errs.append(f"{prefix}.forbidden_patterns[{i}] must be a non-empty string")

    tags = case.get("tags")
    if not isinstance(tags, list) or not tags:
        errs.append(f"{prefix}.tags must be a non-empty list of lowercase tags")
    else:
        for i, tag in enumerate(tags):
            if not isinstance(tag, str) or not _TAG_RE.match(tag):
                errs.append(f"{prefix}.tags[{i}] must be a lowercase slug")

    return errs


def validate_regression_manifest(manifest: Any) -> list[str]:
    """Validate a full PoE1/PoE2 shared regression manifest (`schema_version` + unique `cases`)."""
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    errs: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"manifest.schema_version must equal {SCHEMA_VERSION!r}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errs.append("manifest.cases must be a non-empty list")
        return errs
    seen_ids: set[str] = set()
    for idx, case in enumerate(cases):
        errs.extend(validate_regression_case(case, index=idx))
        if isinstance(case, dict) and isinstance(case.get("id"), str):
            cid = case["id"]
            if cid in seen_ids:
                errs.append(f"duplicate case id: {cid!r}")
            seen_ids.add(cid)
    return errs
