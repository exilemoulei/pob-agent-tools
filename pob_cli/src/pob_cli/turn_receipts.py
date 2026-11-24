"""L1 out-of-band CLI execution receipt ledger and per-turn hook state storage."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .guardrails import normalize_equipment_slot, redact_secrets
from .updater.paths import get_runtime_dir

RECEIPTS_FILENAME = "turn_receipts.jsonl"
MAX_RECEIPT_LINES = 200
MAX_RECEIPT_BYTES = 256 * 1024

_EXCLUDED_TOP_COMMANDS = frozenset({"hook", "serve", "_daemon"})
_HELP_VERSION_FLAGS = frozenset({"--help", "-h", "--version"})
_GLOBAL_VALUE_FLAGS = frozenset({"--target", "--runtime-dir", "--pob-root", "--cache-dir", "--proxy"})
# The `user:password@` part of a URL inside one argument: everything up to the last `@` of the
# authority, so an e-mail user name or a space in the password is covered too.
_URL_USERINFO_RE = re.compile(r"(://)[^/?#]*@")

_COMPUTE_SINGLE_CMDS = frozenset(
    {
        "diagnose",
        "summary",
        "audit",
        "matrix",
        "compare",
        "calc",
        "batch",
        "sweep",
        "borrow",
        "rank-nodes",
        "rank-gems",
    }
)
_COMPUTE_PAIR_CMDS = frozenset(
    {
        ("item", "try"),
        ("item", "edit"),
        ("item", "unequip"),
        ("item", "flask"),
        ("item", "anoint"),
        ("skill", "set"),
        ("skill", "add"),
        ("skill", "rm"),
        ("skill", "new-group"),
        ("skill", "replace-group"),
        ("tree", "change"),
        ("tree", "set-class"),
        ("config", "set"),
        ("timeless", "search"),
    }
)
_TWO_WORD_PARENTS = frozenset({"item", "skill", "tree", "config", "data", "timeless", "trade", "bench"})
_WEB_RESEARCH_CMDS = frozenset({"poedb", "wiki", "research"})
_BUILD_SLOT_ALIAS_CMDS = frozenset({"summary", "diagnose", "audit", "skills", "config", "matrix", "compare", "calc"})
_FRESH_DIAGNOSE_RESET_CMDS = frozenset({"load", "new", "undo", "redo", "switch-main"})

_RECEIPT_TLS = threading.local()


def _tls_stack() -> list[dict[str, Any]]:
    stack = getattr(_RECEIPT_TLS, "stack", None)
    if stack is None:
        stack = []
        _RECEIPT_TLS.stack = stack
    return stack


def _receipts_disabled() -> bool:
    return os.environ.get("POB_HOOK_DISABLE_RECEIPTS", "").strip() == "1"


def session_hash(session_id: str) -> str:
    """Return stable 16-char sha256 hex digest for `hook_turn_<hash>.json`."""
    raw = str(session_id or "default").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _extract_flag_value(args: Sequence[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for idx, tok in enumerate(args):
        if tok == flag and idx + 1 < len(args):
            return str(args[idx + 1])
        if tok.startswith(prefix):
            return tok[len(prefix) :]
    return None


def _has_flag(args: Sequence[str], flag: str) -> bool:
    prefix = f"{flag}="
    return any(tok == flag or tok.startswith(prefix) for tok in args)


def _extract_command_tokens(args: Sequence[str]) -> list[str]:
    positionals: list[str] = []
    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "--":
            break
        if tok.startswith("-"):
            if "=" not in tok and tok in _GLOBAL_VALUE_FLAGS:
                idx += 2
                continue
            if "=" not in tok and tok in ("--slot", "--pob", "--out", "--group", "--league"):
                idx += 2
                continue
            idx += 1
            continue
        positionals.append(tok)
        idx += 1
    return positionals


def _receipt_slot(raw: str) -> str | None:
    """Canonical slot for a receipt. Folded first, so `頭盔` records as Helmet just like `头盔`."""
    from .game_data import fold_han

    return normalize_equipment_slot(fold_han(raw))


def classify_cli_args(raw_args: Sequence[str]) -> dict[str, Any]:
    """Classify CLI argv into receipt metadata without side effects."""
    # The ledger is a plain file: a proxy or mirror URL can carry a password (http://user:pass@host),
    # and CLI args can accidentally contain cookies/tokens. Scrub before deriving any receipt fields.
    args = [redact_secrets(_URL_USERINFO_RE.sub(r"\g<1>***@", str(a))) for a in raw_args if a is not None]
    cmd_str = redact_secrets(" ".join(args))[:240]

    if not args or any(tok in _HELP_VERSION_FLAGS for tok in args):
        return {
            "cmd": cmd_str,
            "subcommand": "",
            "category": "excluded",
            "isCompute": False,
            "hasTickCheck": False,
            "isWebResearch": False,
            "slot": "main",
            "applied": False,
            "inspectedSlots": [],
            "shouldRecord": False,
        }

    pos = _extract_command_tokens(args)
    top = pos[0].lower() if pos else ""
    second = pos[1].lower() if len(pos) >= 2 and top in _TWO_WORD_PARENTS else ""
    subcommand = f"{top} {second}".strip() if second else top

    if not top or top in _EXCLUDED_TOP_COMMANDS:
        return {
            "cmd": cmd_str,
            "subcommand": subcommand,
            "category": "excluded",
            "isCompute": False,
            "hasTickCheck": False,
            "isWebResearch": False,
            "slot": "main",
            "applied": False,
            "inspectedSlots": [],
            "shouldRecord": False,
        }

    target_raw = _extract_flag_value(args, "--target")
    eq_slot_raw = _extract_flag_value(args, "--slot")
    if (not target_raw or not target_raw.strip()) and top in _BUILD_SLOT_ALIAS_CMDS:
        target_raw = eq_slot_raw
    default_slot = (os.environ.get("POB_SLOT") or "main").strip().lower() or "main"
    build_slot = target_raw.strip().lower() if target_raw and target_raw.strip() else default_slot
    applied = _has_flag(args, "--apply")

    is_compute = False
    if top in _COMPUTE_SINGLE_CMDS:
        is_compute = True
    elif (top, second) in _COMPUTE_PAIR_CMDS:
        is_compute = True
    elif top == "item" and second == "db" and _has_flag(args, "--equip"):
        is_compute = True
    elif top == "trade" and second == "search":
        pob_mode = (_extract_flag_value(args, "--pob") or "").strip().lower()
        is_compute = pob_mode != "off"

    has_tick_check = subcommand in ("diagnose", "data tick")
    is_web_research = top in _WEB_RESEARCH_CMDS

    inspected_slots: list[str] = []
    if subcommand in ("item inspect", "items"):
        if eq_slot_raw and eq_slot_raw.strip():
            norm_eq = _receipt_slot(eq_slot_raw)
            if norm_eq:
                inspected_slots.append(norm_eq)
        elif subcommand == "items" and _has_flag(args, "--all"):
            inspected_slots.append("*")
    elif subcommand == "trade search" and _has_flag(args, "--bench-craft") and eq_slot_raw and eq_slot_raw.strip():
        norm_eq = _receipt_slot(eq_slot_raw)
        if norm_eq:
            inspected_slots.append(norm_eq)

    if subcommand == "diagnose":
        category = "diagnose"
    elif subcommand == "data tick":
        category = "tick_check"
    elif is_web_research:
        category = "web_research"
    elif inspected_slots and not is_compute:
        category = "inspect_slot"
    elif is_compute:
        category = "compute"
    else:
        category = "other"

    return {
        "cmd": cmd_str,
        "subcommand": subcommand,
        "category": category,
        "isCompute": is_compute,
        "hasTickCheck": has_tick_check,
        "isWebResearch": is_web_research,
        "slot": build_slot,
        "applied": applied,
        "inspectedSlots": inspected_slots,
        "shouldRecord": True,
    }


def record_daemon_receipt_hint(
    action: str,
    params: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> None:
    """Enrich the active thread-local receipt context with daemon response metadata."""
    stack = _tls_stack()
    if not stack:
        return
    frame = stack[-1]

    if isinstance(params, dict):
        p_target = params.get("target")
        if isinstance(p_target, str) and p_target.strip():
            frame["slot"] = p_target.strip().lower()
        if params.get("apply") is True:
            frame["applied"] = True
        if action in ("items", "item_inspect"):
            if params.get("allRaw") is True or params.get("all_items") is True or params.get("all") is True:
                if "*" not in frame["inspectedSlots"]:
                    frame["inspectedSlots"].append("*")
            p_slot = params.get("slot")
            if isinstance(p_slot, str) and p_slot.strip():
                norm_eq = _receipt_slot(p_slot)
                if norm_eq and norm_eq not in frame["inspectedSlots"]:
                    frame["inspectedSlots"].append(norm_eq)

    if isinstance(result, dict):
        r_target = result.get("target") or result.get("slot")
        if isinstance(r_target, str) and r_target.strip():
            frame["slot"] = r_target.strip().lower()
        rev = result.get("rev")
        if not (isinstance(rev, int) and not isinstance(rev, bool)):
            after = result.get("after")
            if isinstance(after, dict):
                rev = after.get("rev")
        if isinstance(rev, int) and not isinstance(rev, bool):
            frame["rev"] = rev
        if result.get("applied") is True:
            frame["applied"] = True


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def append_receipt(entry: dict[str, Any], *, runtime_dir: Path | None = None) -> None:
    """Append a compact receipt line and atomically truncate to last 200 entries if over budget."""
    if _receipts_disabled():
        return
    rt = get_runtime_dir(runtime_dir)
    rt.mkdir(parents=True, exist_ok=True)
    path = rt / RECEIPTS_FILENAME
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)

    try:
        size = path.stat().st_size
        raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return

    if size > MAX_RECEIPT_BYTES or len(raw_lines) > MAX_RECEIPT_LINES:
        kept = raw_lines[-MAX_RECEIPT_LINES:]
        _atomic_write_text(path, "\n".join(kept) + "\n")


@contextlib.contextmanager
def receipt_context(
    raw_args: Sequence[str],
    *,
    runtime_dir: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Track one CLI invocation and persist an out-of-band receipt on exit (fail-open)."""
    meta = classify_cli_args(raw_args)
    frame: dict[str, Any] = {
        **meta,
        "inspectedSlots": list(meta["inspectedSlots"]),
        "rev": None,
    }
    stack = _tls_stack()
    stack.append(frame)
    exit_code = 0
    try:
        yield frame
    except SystemExit as exc:
        if exc.code is None:
            exit_code = 0
        elif isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 1
        raise
    except BaseException as exc:
        code = getattr(exc, "exit_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            exit_code = code
        else:
            exit_code = 1
        raise
    finally:
        if stack and stack[-1] is frame:
            stack.pop()
        elif frame in stack:
            stack.remove(frame)
        if frame.get("shouldRecord") and not _receipts_disabled():
            entry: dict[str, Any] = {
                "ts": round(time.time(), 3),
                "cmd": frame["cmd"],
                "subcommand": frame["subcommand"],
                "category": frame["category"],
                "isCompute": bool(frame["isCompute"]),
                "hasTickCheck": bool(frame["hasTickCheck"]),
                "isWebResearch": bool(frame.get("isWebResearch", False)),
                "slot": frame["slot"],
                "rev": frame.get("rev"),
                "applied": bool(frame["applied"]),
                "inspectedSlots": list(frame["inspectedSlots"]),
                "exitCode": exit_code,
            }
            sess_id = os.environ.get("POB_HOOK_SESSION_ID", "").strip()
            if sess_id:
                entry["sessionId"] = sess_id
            with contextlib.suppress(Exception):
                append_receipt(entry, runtime_dir=runtime_dir)


def read_turn_receipts(
    since_ts: float,
    *,
    session_id: str | None = None,
    runtime_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read receipt entries with `ts >= since_ts`, scoped by `session_id` when tagged (fail-open)."""
    path = get_runtime_dir(runtime_dir) / RECEIPTS_FILENAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    threshold = float(since_ts)
    want_sess = str(session_id or "").strip()
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ts_val = obj.get("ts")
        if not (isinstance(ts_val, (int, float)) and float(ts_val) >= threshold):
            continue
        rec_sess = str(obj.get("sessionId") or "").strip()
        if want_sess and rec_sess and rec_sess != want_sess:
            continue
        out.append(obj)
    return out


def summarize_turn_receipts(receipts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate turn receipt list into gate verification summary."""
    has_compute = False
    has_tick_check = False
    has_diagnose = False
    has_fresh_diagnose = False
    has_web_research = False
    attempted_web_research = False
    inspected_slots: list[str] = []
    inspected_seen: set[str] = set()
    max_applied_rev: dict[str, int] = {}
    max_compute_rev: dict[str, int] = {}
    last_slot: str | None = None

    for rec in receipts:
        if not isinstance(rec, dict):
            continue
        subcmd = str(rec.get("subcommand") or "")
        is_web = (
            bool(rec.get("isWebResearch"))
            or rec.get("category") == "web_research"
            or subcmd in _WEB_RESEARCH_CMDS
        )
        if is_web:
            attempted_web_research = True
        if rec.get("exitCode", 0) != 0:
            continue
        if is_web:
            has_web_research = True
        is_compute = bool(rec.get("isCompute"))
        if is_compute:
            has_compute = True
        if rec.get("hasTickCheck"):
            has_tick_check = True
        if rec.get("category") == "diagnose" or subcmd == "diagnose":
            has_diagnose = True
            has_fresh_diagnose = True
        elif rec.get("applied") is True or subcmd in _FRESH_DIAGNOSE_RESET_CMDS:
            has_fresh_diagnose = False

        for sl in rec.get("inspectedSlots") or ():
            if isinstance(sl, str) and sl and sl not in inspected_seen:
                inspected_seen.add(sl)
                inspected_slots.append(sl)

        slot_raw = rec.get("slot")
        slot = str(slot_raw).strip().lower() if isinstance(slot_raw, str) and slot_raw.strip() else "main"
        last_slot = slot

        rev = rec.get("rev")
        if isinstance(rev, int) and not isinstance(rev, bool):
            if rec.get("applied"):
                prev_a = max_applied_rev.get(slot)
                if prev_a is None or rev > prev_a:
                    max_applied_rev[slot] = rev
            if is_compute:
                prev_c = max_compute_rev.get(slot)
                if prev_c is None or rev > prev_c:
                    max_compute_rev[slot] = rev

    return {
        "hasCompute": has_compute,
        "hasTickCheck": has_tick_check,
        "hasDiagnose": has_diagnose,
        "hasFreshDiagnose": has_fresh_diagnose,
        "hasWebResearch": has_web_research,
        "attemptedWebResearch": attempted_web_research,
        "inspectedSlots": inspected_slots,
        "maxAppliedRevBySlot": max_applied_rev,
        "maxComputeRevBySlot": max_compute_rev,
        "lastSlot": last_slot,
        "count": len(receipts),
    }


def _turn_state_path(session_id: str, runtime_dir: Path | None = None) -> Path:
    return get_runtime_dir(runtime_dir) / f"hook_turn_{session_hash(session_id)}.json"


def load_turn_state(session_id: str, *, runtime_dir: Path | None = None) -> dict[str, Any] | None:
    """Load persisted per-session turn state dict, or None if missing/corrupt."""
    path = _turn_state_path(session_id, runtime_dir=runtime_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_turn_state(
    session_id: str,
    state: dict[str, Any],
    *,
    runtime_dir: Path | None = None,
) -> None:
    """Atomically save per-session turn state JSON."""
    path = _turn_state_path(session_id, runtime_dir=runtime_dir)
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    _atomic_write_text(path, payload)
