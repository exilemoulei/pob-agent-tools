"""Gate 1 (`pre-prompt`), Gate 2 (`pre-tool`), and Gate 3 (`verify-stop`) deterministic handlers."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

from ..game_data import ground_text_entities
from ..guardrails import (
    detect_build_source_in_text,
    detect_hesitation_claims,
    detect_mechanics_research_need,
    detect_panel_or_delta_claims,
    detect_trigger_breakpoint_claims,
    extract_claimed_open_affix_slots,
    scan_assistant_reply,
)
from ..turn_receipts import (
    load_turn_state,
    read_turn_receipts,
    save_turn_state,
    summarize_turn_receipts,
)
from .remediate import run_remediation

_SHELL_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "shell",
        "powershell",
        "pwsh",
        "run_command",
        "exec_command",
        "terminal",
        "cmd",
        "command",
    }
)

_FILE_INSPECT_TOOLS: frozenset[str] = frozenset(
    {
        "grep",
        "grep_search",
        "glob",
        "find_by_name",
        "find",
        "search",
        "read",
        "read_file",
        "file_read",
        "view_file",
        "list_dir",
        "ls",
        "rg",
        "cat",
    }
)

_RAW_LUA_READER_RE = re.compile(
    r"\b(?:cat|get-content|gc|type|rg|ripgrep|select-string|findstr|grep|head|tail|more|less|ls|dir|get-childitem|gci|python3?|py|node|pwsh|powershell)\b",
    re.IGNORECASE,
)
_RAW_LUA_PATH_RE = re.compile(
    r"(?:\.runtime[/\\]engine|PobTools[^/\s\"'`]*)[/\\][^\s\"'`]*\.lua\b",
    re.IGNORECASE,
)
_RAW_ENGINE_DIR_RE = re.compile(
    r"(?:\.runtime[/\\]engine|PobTools[^/\s\"'`]*[/\\]PoE1)(?:[/\\]|$)",
    re.IGNORECASE,
)
_MANUAL_XML_EXEC_RE = re.compile(
    r"(?:\b(?:python3?|py)\s+-(?:c|-)\b|\bnode\s+-e\b|\b(?:pwsh|powershell)\s+(?:-c|-command)\b|\[xml\]|\bselect-xml\b|\belementtree\b|\bxml\.etree\b|\bminidom\b|\blxml\b|\bdomparser\b)",
    re.IGNORECASE,
)
_MANUAL_XML_TARGET_RE = re.compile(
    r"(?:\.xml\b|<PathOfBuilding\b|<PlayerStat\b|<Build\b|<Items\b|<Skills\b)",
    re.IGNORECASE,
)
_POB_CMD_RE = re.compile(r"\bpob(?:\.exe)?\b", re.IGNORECASE)
_POB_HELP_RE = re.compile(r"\bpob(?:\.exe)?\b[^\n;|&]*\s(?:--help|-h)(?:\s|$)", re.IGNORECASE)
_POB_HOOK_RE = re.compile(r"\bpob(?:\.exe)?\b(?:\s+--offline)?\s+hook\b", re.IGNORECASE)
_BARE_JSON_FLAG_RE = re.compile(r"(?:^|\s)--json(?=\s|$)")
_OUT_OR_REDIRECT_RE = re.compile(
    r"(?:(?:^|\s)--out(?:\s|=)|(?<![2&])>{1,2}\s*\S+|\b(?:out-file|set-content|tee)\b)",
    re.IGNORECASE,
)
_STRIP_JSON_FLAGS_RE = re.compile(r"(?:^|\s)--(?:json|compact)(?=\s|$)")

_POB_HELP_QUICK_REF = (
    "禁止运行 pob --help 浪费步数（NO_POB_HELP）。核心命令速查：\n"
    "1. 构筑诊断/对比: pob diagnose [@build.xml|url] [--compare @b2.xml] [--ask \"...\"] [--keep ...]\n"
    "2. 数据/断点查表: pob data <gem|unique|mod|base|tree|tick|quality> \"<query>\" | pob calc \"<kw>\"\n"
    "3. 装备/验视/改词: pob item inspect --slot \"<Slot>\" | pob item edit/try/anoint/flask [--apply]\n"
    "4. 技能/天赋/优化: pob skill set/add/rm | pob tree change | pob rank-nodes | pob rank-gems"
)


def _compute_turn_key(turn_id: str, user_text: str) -> str:
    tid = turn_id.strip()
    if tid:
        return tid
    return hashlib.sha1(user_text.strip().encode("utf-8")).hexdigest()[:16]


def handle_pre_prompt(
    req: dict[str, Any],
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Gate 1: anchor turn cursor stably (no reset on multi-step re-entry) and ground entities offline."""
    session_id = str(req.get("sessionId") or "default").strip() or "default"
    turn_id = str(req.get("turnId") or "")
    user_text = str(req.get("userText") or "")
    turn_key = _compute_turn_key(turn_id, user_text)

    existing = load_turn_state(session_id, runtime_dir=runtime_dir)
    if existing and existing.get("turnKey") == turn_key:
        return {
            "injectText": "",
            "turnKey": turn_key,
            "turnStartTs": float(existing.get("turnStartTs") or time.time()),
            "hasBuildSource": bool(existing.get("hasBuildSource", False)),
            "needsWebResearch": bool(existing.get("needsWebResearch", False)),
            "suggestedResearchQueries": list(existing.get("suggestedResearchQueries") or []),
            "reusedTurn": True,
        }

    now_ts = int(time.time() * 1000) / 1000.0
    has_build_src, source_hint = detect_build_source_in_text(user_text)
    mech_info = detect_mechanics_research_need(user_text)
    needs_web = bool(mech_info["needsResearch"])
    suggested_queries = list(mech_info["suggestedQueries"])
    state = {
        "sessionId": session_id,
        "turnKey": turn_key,
        "turnStartTs": now_ts,
        "hasBuildSource": has_build_src,
        "buildSourceHint": source_hint,
        "needsWebResearch": needs_web,
        "suggestedResearchQueries": suggested_queries,
        "userText": user_text[:2000],
        "stopRetries": 0,
    }
    save_turn_state(session_id, state, runtime_dir=runtime_dir)

    ground_block = ""
    if user_text.strip():
        try:
            grounded = ground_text_entities(user_text, limit=6, runtime_dir=runtime_dir)
            if grounded.get("entities") or grounded.get("warnings") or grounded.get("resolvedSlang"):
                ground_block = str(grounded.get("contextBlock") or "").strip()
        except Exception:
            ground_block = ""

    parts: list[str] = []
    if needs_web:
        first_q = suggested_queries[0] if suggested_queries else "<词条>"
        q_hint = f"（建议查词：{', '.join(suggested_queries[:3])}）" if suggested_queries else ""
        parts.append(
            "[MECHANICS-RESEARCH-REQUIRED] 本题涉及流派机制运作/交互边界（PoB 引擎可能未完整模拟）。"
            f'严禁凭记忆猜或写“大概/应该/不确定”，请先调用 `pob research "{first_q}"`（并发查 poedb.tw + poewiki.net）'
            f"或 `pob wiki` / `pob poedb` 核验真值。{q_hint}"
        )
    if ground_block:
        parts.append(ground_block)

    inject_text = ""
    if parts:
        combined = "\n".join(parts)[:3500]
        inject_text = f'<pob-grounding patch="3.30">\n{combined}\n</pob-grounding>'

    return {
        "injectText": inject_text,
        "turnKey": turn_key,
        "turnStartTs": now_ts,
        "hasBuildSource": has_build_src,
        "needsWebResearch": needs_web,
        "suggestedResearchQueries": suggested_queries,
        "reusedTurn": False,
    }


def _extract_pob_tokens(command_line: str) -> list[str]:
    """Extract argv tokens starting right after `pob`/`pob.exe` for the first pob invocation."""
    try:
        tokens = shlex.split(command_line, posix=True)
    except ValueError:
        tokens = command_line.split()
    for idx, tok in enumerate(tokens):
        base = Path(tok).name.lower()
        if base in ("pob", "pob.exe"):
            rest: list[str] = []
            for nxt in tokens[idx + 1 :]:
                if nxt in ("|", "||", "&&", ";"):
                    break
                rest.append(nxt)
            return rest
    return []


def _is_redundant_post_diagnose_cmd(command_line: str) -> bool:
    tokens = _extract_pob_tokens(command_line)
    if not tokens:
        return False
    # Skip root global flags before subcommand
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("--offline", "--compact"):
            idx += 1
        elif tok == "--out":
            idx += 2
        elif tok.startswith("--out="):
            idx += 1
        else:
            break
    if idx >= len(tokens):
        return False
    sub = tokens[idx].lower()
    sub_args = tokens[idx + 1 :]
    if sub == "summary":
        return True
    if sub == "skills":
        return not any(a == "--group" or a.startswith("--group=") for a in sub_args)
    if sub == "items":
        return not any(
            a in ("--slot", "--raw", "--all") or a.startswith("--slot=")
            for a in sub_args
        )
    return False


def _has_active_diagnose_without_apply(session_id: str, *, runtime_dir: Path | None) -> bool:
    state = load_turn_state(session_id, runtime_dir=runtime_dir)
    if not state:
        return False
    turn_start = float(state.get("turnStartTs") or 0.0)
    receipts = read_turn_receipts(turn_start, session_id=session_id, runtime_dir=runtime_dir)
    summary = summarize_turn_receipts(receipts)
    return bool(summary.get("hasFreshDiagnose", False))


def _allow_tool() -> dict[str, Any]:
    return {"decision": "allow", "ruleId": None, "reason": None, "rewrittenCommand": None}


def _deny_tool(rule_id: str, reason: str) -> dict[str, Any]:
    return {"decision": "deny", "ruleId": rule_id, "reason": reason, "rewrittenCommand": None}


def handle_pre_tool(
    req: dict[str, Any],
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Gate 2: enforce deterministic pre-tool rules (shell + file/grep engine-Lua guards)."""
    tool_name = str(req.get("toolName") or "").strip().lower()
    if tool_name in _FILE_INSPECT_TOOLS:
        cand_paths: list[str] = []
        raw_tp = req.get("targetPaths")
        if isinstance(raw_tp, list):
            cand_paths.extend(str(p) for p in raw_tp if p)
        for k in ("filePath", "path", "pattern", "commandLine"):
            v = req.get(k)
            if isinstance(v, str) and v.strip():
                cand_paths.append(v.strip())
        if any(_RAW_LUA_PATH_RE.search(p) or _RAW_ENGINE_DIR_RE.search(p) for p in cand_paths):
            return _deny_tool(
                "NO_RAW_ENGINE_LUA",
                '禁止用 grep/glob/read 扫描 PoB 引擎 Lua 源码目录（NO_RAW_ENGINE_LUA）；请直接使用 pob data <gem|unique|mod|tree>、pob calc "<kw>" 或 pob research "<词条>"。',
            )
        return _allow_tool()

    if tool_name and tool_name not in _SHELL_TOOLS:
        return _allow_tool()

    cmd = str(req.get("commandLine") or "").strip()
    if not cmd:
        return _allow_tool()

    # Rule 1: NO_RAW_ENGINE_LUA
    if _RAW_LUA_READER_RE.search(cmd) and (_RAW_LUA_PATH_RE.search(cmd) or _RAW_ENGINE_DIR_RE.search(cmd)):
        return _deny_tool(
            "NO_RAW_ENGINE_LUA",
            '禁止手撕引擎 Lua 源码（NO_RAW_ENGINE_LUA）；请直接使用 pob data <gem|unique|mod|tree> 或 pob calc "<kw>"（0ms 索引查询）。',
        )

    # Rule 2: NO_MANUAL_XML_CALC
    if _MANUAL_XML_EXEC_RE.search(cmd) and _MANUAL_XML_TARGET_RE.search(cmd):
        return _deny_tool(
            "NO_MANUAL_XML_CALC",
            "严禁手算 XML（R5 / NO_MANUAL_XML_CALC）；请直接执行 pob diagnose @build.xml。",
        )

    # Rule 3: NO_POB_HELP
    if _POB_HELP_RE.search(cmd):
        return _deny_tool("NO_POB_HELP", _POB_HELP_QUICK_REF)

    # Rule 4: NO_BARE_JSON
    if (
        _POB_CMD_RE.search(cmd)
        and _BARE_JSON_FLAG_RE.search(cmd)
        and not _POB_HOOK_RE.search(cmd)
        and not _OUT_OR_REDIRECT_RE.search(cmd)
    ):
        if bool(req.get("supportsOverwrite", False)):
            rewritten = re.sub(r"\s{2,}", " ", _STRIP_JSON_FLAGS_RE.sub("", cmd)).strip()
            return {
                "decision": "allow",
                "ruleId": "NO_BARE_JSON",
                "reason": "已自动移除裸 --json/--compact 以避免上下文膨胀（默认紧凑 Markdown 已含完整信息）。",
                "rewrittenCommand": rewritten,
            }
        return _deny_tool(
            "NO_BARE_JSON",
            "禁止裸跑 --json 灌爆上下文（NO_BARE_JSON）；默认紧凑 Markdown 已含完整信息，如需机器解析请配合 --out <file> 或重定向至文件。",
        )

    # Rule 5: NO_REDUNDANT_POST_DIAGNOSE
    if _POB_CMD_RE.search(cmd) and not _POB_HOOK_RE.search(cmd) and _is_redundant_post_diagnose_cmd(cmd):
        session_id = str(req.get("sessionId") or "default").strip() or "default"
        if _has_active_diagnose_without_apply(session_id, runtime_dir=runtime_dir):
            return _deny_tool(
                "NO_REDUNDANT_POST_DIAGNOSE",
                '当前轮次 pob diagnose 输出已包含完整去水面板、全技能组与装备摘要；仅在需要查看单件黄装原文本时运行 pob item inspect --slot "<Slot>"。',
            )

    return _allow_tool()


def _allow_stop(stop_retries: int, *, advisory: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "decision": "allow_stop",
        "failedChecks": [],
        "correctionPrompt": None,
        "stopRetries": stop_retries,
    }
    if advisory:
        out["advisoryWarning"] = advisory
    return out


def handle_verify_stop(
    req: dict[str, Any],
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Gate 3: verify assistant reply against V1..V4 and auto-remediate once (maxRetries=1)."""
    termination_reason = str(req.get("terminationReason") or "").strip()
    if termination_reason and termination_reason != "NO_TOOL_CALL":
        return _allow_stop(0)

    assistant_text = str(req.get("assistantText") or "")
    if not assistant_text.strip():
        return _allow_stop(0)

    session_id = str(req.get("sessionId") or "default").strip() or "default"
    turn_id = str(req.get("turnId") or "")
    user_text = str(req.get("userText") or "")
    continuation_active = bool(req.get("continuationActive", False))

    state = load_turn_state(session_id, runtime_dir=runtime_dir) or {}
    turn_key = (
        _compute_turn_key(turn_id, user_text)
        if (turn_id.strip() or user_text.strip())
        else str(state.get("turnKey") or "")
    )
    same_turn = bool(state and turn_key and state.get("turnKey") == turn_key)
    stop_retries = int(state.get("stopRetries") or 0) if same_turn else 0
    turn_start_ts = float(state.get("turnStartTs") or 0.0) if same_turn else 0.0

    if continuation_active or stop_retries >= 1:
        residual = scan_assistant_reply(assistant_text)
        advisory = (
            f"Residual V1 alerts after retry budget exhausted: {', '.join(i.rule_id for i in residual[:3])}"
            if residual
            else None
        )
        return _allow_stop(stop_retries, advisory=advisory)

    receipts = read_turn_receipts(turn_start_ts, session_id=session_id, runtime_dir=runtime_dir)
    summary = summarize_turn_receipts(receipts)
    failed_checks: list[str] = []

    # V1: reply hallucination & fallacy scanner
    v1_issues = scan_assistant_reply(assistant_text)
    if v1_issues:
        failed_checks.append("V1")

    # V2: compute receipt & post-apply rev freshness
    has_src_user, src_hint_user = detect_build_source_in_text(user_text)
    has_build_src = bool((state.get("hasBuildSource") if same_turn else False) or has_src_user)
    build_source_hint = src_hint_user or (state.get("buildSourceHint") if same_turn else None)
    panel_claims = detect_panel_or_delta_claims(assistant_text)
    v2_info: dict[str, Any] | None = None
    target_slot = str(summary.get("lastSlot") or "main")

    if has_build_src or panel_claims:
        if not summary.get("hasCompute"):
            failed_checks.append("V2")
            v2_info = {
                "reasonKind": "missing_compute",
                "targetSlot": target_slot,
                "buildSourceHint": build_source_hint,
            }
        else:
            applied_revs: dict[str, int] = summary.get("maxAppliedRevBySlot") or {}
            compute_revs: dict[str, int] = summary.get("maxComputeRevBySlot") or {}
            for slot_name, max_app_rev in applied_revs.items():
                comp_rev = compute_revs.get(slot_name)
                if comp_rev is None or comp_rev < max_app_rev:
                    failed_checks.append("V2")
                    target_slot = slot_name
                    v2_info = {
                        "reasonKind": "stale_rev",
                        "targetSlot": slot_name,
                        "buildSourceHint": build_source_hint,
                    }
                    break

    # V3: open-affix slot inspection receipt
    claimed_slots = extract_claimed_open_affix_slots(assistant_text)
    inspected = set(summary.get("inspectedSlots") or ())
    v3_missing: list[str] = []
    if claimed_slots and "*" not in inspected:
        for norm_slot, _ in claimed_slots:
            if norm_slot not in inspected and norm_slot not in v3_missing:
                v3_missing.append(norm_slot)
        if v3_missing:
            failed_checks.append("V3")

    # V4: trigger breakpoint tick check receipt
    v4_claims = detect_trigger_breakpoint_claims(assistant_text)
    v4_failed_claims: list[str] = []
    if v4_claims and not summary.get("hasTickCheck"):
        failed_checks.append("V4")
        v4_failed_claims = v4_claims

    # V5: web-research mechanics verification & anti-hesitation gate
    # NEVER infer offline from POB_OFFLINE=1 (set by `pob --offline hook ...` to skip GitHub update checks).
    v5_enabled = (
        req.get("disableV5") is not True
        and os.environ.get("POB_HOOK_DISABLE_V5", "").strip() != "1"
    )
    web_ok = bool(
        summary.get("hasWebResearch")
        or summary.get("attemptedWebResearch")
        or req.get("hostWebResearched") is True
    )
    state_needs_web = bool(state.get("needsWebResearch")) if same_turn else False
    state_queries = list(state.get("suggestedResearchQueries") or []) if same_turn else []
    v5_info: dict[str, Any] | None = None
    if v5_enabled and not web_ok:
        hesitations = detect_hesitation_claims(assistant_text)
        mech_check = detect_mechanics_research_need(user_text, assistant_text)
        mech_need = state_needs_web or bool(mech_check["needsResearch"])
        if hesitations or mech_need:
            failed_checks.append("V5")
            merged_q = state_queries or list(mech_check.get("suggestedQueries") or [])
            v5_info = {
                "hesitations": hesitations[:3],
                "mechNeed": mech_need,
                "reasons": list(mech_check.get("reasons") or []),
                "suggestedQueries": merged_q[:3],
            }

    if not failed_checks:
        return _allow_stop(stop_retries)

    next_retries = stop_retries + 1
    updated_state = {
        "sessionId": session_id,
        "turnKey": turn_key or _compute_turn_key("", user_text),
        "turnStartTs": turn_start_ts or time.time(),
        "hasBuildSource": has_build_src,
        "buildSourceHint": build_source_hint,
        "needsWebResearch": state_needs_web or bool(v5_info and v5_info.get("mechNeed")),
        "suggestedResearchQueries": (v5_info.get("suggestedQueries") if v5_info else state_queries) or [],
        "userText": (user_text or str(state.get("userText") or ""))[:2000],
        "stopRetries": next_retries,
    }
    save_turn_state(session_id, updated_state, runtime_dir=runtime_dir)

    correction = run_remediation(
        v1_issues=v1_issues,
        v2_info=v2_info,
        v3_slots=v3_missing,
        v4_claims=v4_failed_claims,
        v5_info=v5_info,
        assistant_text=assistant_text,
        target_slot=target_slot,
        runtime_dir=runtime_dir,
    )
    return {
        "decision": "continue",
        "failedChecks": failed_checks,
        "correctionPrompt": correction,
        "stopRetries": next_retries,
    }
