"""Protocol adapters (`raw`, `agy`, `claude`) and JSONL transcript tail extractor for `pob hook`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_CMD_ARG_KEYS: tuple[str, ...] = ("CommandLine", "command", "cmd", "script")
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path", "file_path", "filePath", "Path", "TargetFile", "SearchPath",
    "DirectoryPath", "directory", "dir", "cwd", "glob", "pattern", "include",
)
_TRANSCRIPT_TAIL_BYTES = 131_072


def _extract_target_paths(args: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
        elif isinstance(val, list):
            out.extend(str(v).strip() for v in val if isinstance(v, str) and v.strip())
    return out


def read_stdin_json() -> dict[str, Any]:
    """Read UTF-8 JSON object from stdin; fail-open to `{}` on empty or invalid JSON."""
    try:
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
        raw = sys.stdin.read()
    except Exception:
        return {}
    cleaned = raw.lstrip("\ufeff").strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "output_text"):
                    txt = item.get("text") or item.get("content")
                    if isinstance(txt, str) and txt.strip():
                        parts.append(txt.strip())
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        for key in ("modifiedResponse", "response", "text", "content"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _extract_assistant_text_from_record(rec: dict[str, Any]) -> str:
    planner = rec.get("plannerResponse")
    if isinstance(planner, dict):
        txt = _extract_text_from_content(planner)
        if txt:
            return txt

    role = str(rec.get("role") or "").lower()
    rec_type = str(rec.get("type") or "").lower()
    kind = str(rec.get("kind") or "").lower()

    if role == "assistant" or kind == "assistant/message" or rec_type == "assistant_message":
        for key in ("content", "text", "message"):
            txt = _extract_text_from_content(rec.get(key))
            if txt:
                return txt

    if rec_type == "assistant":
        msg = rec.get("message")
        if isinstance(msg, dict):
            txt = _extract_text_from_content(msg.get("content") or msg.get("text"))
            if txt:
                return txt
        txt = _extract_text_from_content(rec.get("content") or rec.get("text"))
        if txt:
            return txt

    return ""


def extract_latest_assistant_from_transcript(
    transcript_path: str | Path | None,
    *,
    max_bytes: int = _TRANSCRIPT_TAIL_BYTES,
) -> str:
    """Extract the latest assistant message text from a JSONL transcript tail safely."""
    if not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            seeked = size > max_bytes
            if seeked:
                fh.seek(size - max_bytes)
            chunk = fh.read()
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if seeked and len(lines) > 1:
            lines = lines[1:]
    except OSError:
        return ""

    for line in reversed(lines):
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            rec = json.loads(s)
        except Exception:
            continue
        if isinstance(rec, dict):
            txt = _extract_assistant_text_from_record(rec)
            if txt:
                return txt
    return ""


def _extract_cmd_from_args(args: dict[str, Any], default_key: str) -> tuple[str, str]:
    for key in _CMD_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val, key
    for key in _CMD_ARG_KEYS:
        if key in args and isinstance(args[key], str):
            return args[key], key
    return "", default_key


def decode_input(
    gate: str,
    protocol: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize host-specific stdin payload into canonical `raw` request + adapter metadata."""
    proto = protocol.lower()
    meta: dict[str, Any] = {"argKey": "CommandLine"}

    if proto == "raw":
        if gate == "pre-tool" and isinstance(payload.get("argKey"), str):
            meta["argKey"] = payload["argKey"]
        return dict(payload), meta

    if proto == "agy":
        session_id = str(payload.get("conversationId") or payload.get("sessionId") or "default")
        if gate == "pre-prompt":
            return (
                {
                    "sessionId": session_id,
                    "turnId": str(payload.get("turnId") or ""),
                    "userText": str(payload.get("lastUserInput") or payload.get("userText") or ""),
                    "step": int(payload.get("invocationNum") or payload.get("step") or 1),
                },
                meta,
            )
        if gate == "pre-tool":
            tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            cmd_line, arg_key = _extract_cmd_from_args(args, "CommandLine")
            if not cmd_line and isinstance(payload.get("commandLine"), str):
                cmd_line = payload["commandLine"]
            meta["argKey"] = arg_key
            return (
                {
                    "sessionId": session_id,
                    "toolName": str(tc.get("name") or payload.get("toolName") or ""),
                    "commandLine": cmd_line,
                    "targetPaths": _extract_target_paths(args),
                    "supportsOverwrite": True,
                },
                meta,
            )
        # verify-stop
        assistant_text = str(payload.get("assistantText") or "")
        if not assistant_text and payload.get("transcriptPath"):
            assistant_text = extract_latest_assistant_from_transcript(payload.get("transcriptPath"))
        return (
            {
                "sessionId": session_id,
                "turnId": str(payload.get("turnId") or ""),
                "userText": str(payload.get("lastUserInput") or payload.get("userText") or ""),
                "assistantText": assistant_text,
                "continuationActive": bool(payload.get("continuationActive", False)),
                "terminationReason": str(payload.get("terminationReason") or ""),
            },
            meta,
        )

    if proto == "claude":
        session_id = str(payload.get("session_id") or payload.get("sessionId") or "default")
        if gate == "pre-prompt":
            return (
                {
                    "sessionId": session_id,
                    "turnId": str(payload.get("turn_id") or payload.get("turnId") or ""),
                    "userText": str(
                        payload.get("prompt") or payload.get("user_prompt") or payload.get("userText") or ""
                    ),
                    "step": int(payload.get("step") or 1),
                },
                meta,
            )
        if gate == "pre-tool":
            tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            cmd_line, arg_key = _extract_cmd_from_args(tool_input, "command")
            if not cmd_line and isinstance(payload.get("commandLine"), str):
                cmd_line = payload["commandLine"]
            meta["argKey"] = arg_key
            return (
                {
                    "sessionId": session_id,
                    "toolName": str(payload.get("tool_name") or payload.get("toolName") or ""),
                    "commandLine": cmd_line,
                    "targetPaths": _extract_target_paths(tool_input),
                    "supportsOverwrite": False,
                },
                meta,
            )
        # verify-stop
        assistant_text = str(payload.get("last_assistant_message") or payload.get("assistantText") or "")
        if not assistant_text and payload.get("transcript_path"):
            assistant_text = extract_latest_assistant_from_transcript(payload.get("transcript_path"))
        return (
            {
                "sessionId": session_id,
                "turnId": str(payload.get("turn_id") or payload.get("turnId") or ""),
                "userText": str(
                    payload.get("last_user_input") or payload.get("prompt") or payload.get("userText") or ""
                ),
                "assistantText": assistant_text,
                "continuationActive": bool(payload.get("stop_hook_active") or payload.get("continuationActive")),
                "terminationReason": str(
                    payload.get("termination_reason") or payload.get("terminationReason") or ""
                ),
            },
            meta,
        )

    return dict(payload), meta


def encode_output(
    gate: str,
    protocol: str,
    canonical: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Encode canonical gate decision into the host-specific stdout JSON contract."""
    proto = protocol.lower()
    if proto == "raw":
        return canonical

    arg_key = str((meta or {}).get("argKey") or "CommandLine")

    if proto == "agy":
        if gate == "pre-prompt":
            inject = str(canonical.get("injectText") or "").strip()
            if not inject:
                return {}
            return {"injectSteps": [{"systemMessage": {"systemMessage": inject}}]}
        if gate == "pre-tool":
            if canonical.get("decision") == "deny":
                return {
                    "decision": "deny",
                    "reason": str(canonical.get("reason") or "Blocked by pob hook guardrail."),
                }
            rewritten = canonical.get("rewrittenCommand")
            if isinstance(rewritten, str) and rewritten.strip():
                out: dict[str, Any] = {
                    "decision": "allow",
                    "overwrite": {arg_key: rewritten},
                }
                if canonical.get("reason"):
                    out["reason"] = str(canonical["reason"])
                return out
            return {"decision": "allow"}
        # verify-stop
        if canonical.get("decision") == "continue" and canonical.get("correctionPrompt"):
            return {
                "decision": "continue",
                "reason": str(canonical["correctionPrompt"]),
            }
        return {}

    if proto == "claude":
        if gate == "pre-prompt":
            inject = str(canonical.get("injectText") or "").strip()
            if not inject:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": inject,
                }
            }
        if gate == "pre-tool":
            if canonical.get("decision") == "deny":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": str(
                            canonical.get("reason") or "Blocked by pob hook guardrail."
                        ),
                    }
                }
            return {}
        # verify-stop
        if canonical.get("decision") == "continue" and canonical.get("correctionPrompt"):
            return {
                "decision": "block",
                "reason": str(canonical["correctionPrompt"]),
            }
        return {}

    return canonical
