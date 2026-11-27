"""L0 security guardrail: block CLI file-input reads of PoE session secrets (`NO_SECRET_ENV_DUMP`)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

NO_SECRET_ENV_DUMP_RULE_ID = "NO_SECRET_ENV_DUMP"

NO_SECRET_ENV_DUMP_REMEDIATION = (
    "Blocked by NO_SECRET_ENV_DUMP: never read credential/cookie/sessid files or print session env vars. "
    "Use `POE_CN_COOKIE` / `POE_CN_SESSID` / `POESESSID` / `POE_COOKIE` in the environment and run "
    "`pob status` (boolean set/unset) or `pob load` / `pob diagnose` directly "
    "(set `POB_ALLOW_SECRET_PATH=1` only to override intentionally)."
)

_SECRET_DIRS = frozenset({".secrets", "_secrets"})
_SECRET_EXACT_NAMES = frozenset({"oauth_token.json", "daemon.json", ".env", ".envrc"})
_BACKUP_SUFFIXES = (".bak", ".old", ".orig", ".tmp", ".swp")
_ALWAYS_SECRET_EXTS = frozenset({".cookie", ".cookies"})
_SENSITIVE_KEY_EXTS = frozenset({".key", ".token", ".secret"})
_SAFE_NON_SECRET_EXTS = frozenset({".xml", ".pob", ".lua", ".py", ".md", ".rst"})
_CREDENTIAL_EXTS = frozenset({"", ".txt", ".json", ".ini", ".cfg", ".conf", ".env"}) | _SENSITIVE_KEY_EXTS
_SESSID_TOKEN_RE = re.compile(r"(?:^|[._-])(?:poesessid|poe_?cn_?sessid|poe_?cn_?cookie|sessid)(?:$|[._-])", re.IGNORECASE)
_COOKIE_TOKEN_RE = re.compile(r"(?:^|[._-])cookies?(?:$|[._-])", re.IGNORECASE)
_COOKIE_BENIGN_RE = re.compile(r"(?:^|[._-])cookie[_-]?(?:cutter|monster|clicker)(?:$|[._-])", re.IGNORECASE)
_KEY_SECRET_STEM_RE = re.compile(r"(?:^|[._-])(?:session|secret|oauth|auth|token|poe)(?:$|[._-])", re.IGNORECASE)


@dataclass(frozen=True)
class SecretGuardDecision:
    allowed: bool
    rule_id: str | None = None
    reason: str | None = None


def _strip_backup_suffixes(base: str) -> str:
    cur = base
    while True:
        for sfx in _BACKUP_SUFFIXES:
            if cur.endswith(sfx) and len(cur) > len(sfx):
                cur = cur[: -len(sfx)]
                break
        else:
            return cur


def _check_single_path(cand: str) -> SecretGuardDecision:
    parts = [p for p in re.split(r"[/\\]+", cand) if p]
    if not parts:
        return SecretGuardDecision(allowed=True)
    dir_parts = [p.lower() for p in parts[:-1]]
    base = _strip_backup_suffixes(parts[-1].lower())

    if any(d in _SECRET_DIRS for d in dir_parts):
        return SecretGuardDecision(False, NO_SECRET_ENV_DUMP_RULE_ID, NO_SECRET_ENV_DUMP_REMEDIATION)
    if base in _SECRET_EXACT_NAMES or base.startswith((".env.", ".envrc.")):
        return SecretGuardDecision(False, NO_SECRET_ENV_DUMP_RULE_ID, NO_SECRET_ENV_DUMP_REMEDIATION)

    ext = Path(base).suffix.lower()
    if ext in _ALWAYS_SECRET_EXTS:
        return SecretGuardDecision(False, NO_SECRET_ENV_DUMP_RULE_ID, NO_SECRET_ENV_DUMP_REMEDIATION)
    if ext in _SAFE_NON_SECRET_EXTS:
        return SecretGuardDecision(allowed=True)
    if ext in _CREDENTIAL_EXTS:
        stem = (base[: -len(ext)] if ext else base).lstrip(".")
        if (
            _SESSID_TOKEN_RE.search(stem)
            or (_COOKIE_TOKEN_RE.search(stem) and not _COOKIE_BENIGN_RE.search(stem))
            or (ext in _SENSITIVE_KEY_EXTS and _KEY_SECRET_STEM_RE.search(stem))
        ):
            return SecretGuardDecision(False, NO_SECRET_ENV_DUMP_RULE_ID, NO_SECRET_ENV_DUMP_REMEDIATION)

    return SecretGuardDecision(allowed=True)


def check_secret_file_path(path: str) -> SecretGuardDecision:
    """Deny CLI file reads targeting credential/cookie/sessid files with zero false positives on build/doc files."""
    if os.environ.get("POB_ALLOW_SECRET_PATH") == "1":
        return SecretGuardDecision(allowed=True)
    cand = str(path or "").strip()
    if not cand:
        return SecretGuardDecision(allowed=True)
    decision = _check_single_path(cand)
    if not decision.allowed:
        return decision
    try:
        p = Path(cand)
        if p.is_symlink():
            resolved = str(p.resolve())
            if resolved and resolved != cand:
                return _check_single_path(resolved)
    except OSError:
        pass
    return SecretGuardDecision(allowed=True)
