"""Author Notes passthrough and PoB color-escape cleaning (`get_notes`)."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_protocol import BridgeRejectedError
from ..errors import DaemonRequestError

# Matches PoB's Lua `StripEscapes(text)`:
#   text:gsub("%^%d", ""):gsub("%^x%x%x%x%x%x%x", "")
_POB_NOTE_ESCAPE_RE = re.compile(r"\^\d|\^x[0-9A-Fa-f]{6}")
# Strip untrusted ANSI CSI sequences and C0/DEL control bytes (preserving \n and \t).
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_pob_note_escapes(text: str | None) -> str:
    """Strip PoB color codes (`^xRRGGBB` and `^0`..`^9`) matching PoB Lua `StripEscapes`."""
    if not text:
        return ""
    return _POB_NOTE_ESCAPE_RE.sub("", str(text))


def clean_notes_text(raw: str | None) -> str:
    """Clean raw PoB NotesTab text while preserving paragraphs, URLs, and bullet lists."""
    if not raw or not isinstance(raw, str):
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    no_ansi = _CONTROL_CHAR_RE.sub("", _ANSI_CSI_RE.sub("", normalized))
    stripped = strip_pob_note_escapes(no_ansi)
    lines = [line.rstrip() for line in stripped.split("\n")]
    return "\n".join(lines).strip("\n")


def build_notes_payload(raw_text: str | None) -> dict[str, Any]:
    """Assemble full untruncated cleaned `notes` and `notesMeta` for `--json` and formatters."""
    cleaned = clean_notes_text(raw_text)
    has_notes = bool(cleaned)
    line_count = len(cleaned.splitlines()) if has_notes else 0
    return {
        "notes": cleaned,
        "notesMeta": {
            "hasNotes": has_notes,
            "lineCount": line_count,
            "charCount": len(cleaned),
        },
    }


def notes_payload_from_rpc(res: Any) -> dict[str, Any]:
    """Convert a raw `get_notes` RPC result (or error dict) into `{notes, notesMeta}`."""
    raw_text = ""
    if isinstance(res, dict) and not res.get("_error") and res.get("ok") is not False:
        val = res.get("text")
        if isinstance(val, str):
            raw_text = val
    return build_notes_payload(raw_text)


def fetch_build_notes(worker: Any) -> dict[str, Any]:
    """Fetch author notes via `get_notes`, gracefully falling back on unsupported mocks/engines."""
    try:
        get_notes_fn = getattr(worker, "get_notes", None)
        res = get_notes_fn() if callable(get_notes_fn) else worker.call("get_notes", {})
        return notes_payload_from_rpc(res)
    except (AttributeError, KeyError, TypeError, AssertionError, DaemonRequestError, BridgeRejectedError):
        return build_notes_payload("")
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "get_notes" in msg or "unknown" in msg or "unsupported" in msg:
            return build_notes_payload("")
        raise
