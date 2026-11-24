"""Offline integration tests for `pob hook` (Gates 1-3, 3 protocols, CLI receipt tap, auto-remediation)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from click.testing import CliRunner

import pob_cli.cli as cli_mod
from pob_cli.cli import main
from pob_cli.cli_hook.gates import handle_pre_prompt, handle_pre_tool, handle_verify_stop
from pob_cli.cli_hook.protocols import extract_latest_assistant_from_transcript
from pob_cli.cli_hook.remediate import MAX_CORRECTION_CHARS, build_correction_prompt
from pob_cli.turn_receipts import append_receipt, load_turn_state, read_turn_receipts


def _run_hook(
    runner: CliRunner,
    subcmd: str,
    payload: dict[str, Any] | str,
    *,
    protocol: str = "raw",
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    stdin_str = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    res = runner.invoke(
        main,
        ["--offline", "hook", subcmd, "--protocol", protocol],
        input=stdin_str,
        env=env,
    )
    out = res.output.strip()
    assert "\n" not in out, f"Hook output must be single-line compact JSON, got: {out!r}"
    return res.exit_code, json.loads(out) if out else {}


def test_gate1_pre_prompt_cursor_stability_and_grounding(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    r1 = handle_pre_prompt(
        {
            "sessionId": "sess-1",
            "userText": "帮我看下 @build.xml 能不能连觉醒元素集中和堆精魂？",
            "step": 1,
        },
        runtime_dir=rt,
    )
    assert r1["reusedTurn"] is False
    assert r1["hasBuildSource"] is True
    assert "<pob-grounding" in r1["injectText"]
    t0 = r1["turnStartTs"]

    # Simulate stopRetries incrementing mid-turn, then multi-step PreInvocation re-entry
    state = load_turn_state("sess-1", runtime_dir=rt)
    assert state is not None
    state["stopRetries"] = 1
    from pob_cli.turn_receipts import save_turn_state

    save_turn_state("sess-1", state, runtime_dir=rt)

    r2 = handle_pre_prompt(
        {
            "sessionId": "sess-1",
            "userText": "帮我看下 @build.xml 能不能连觉醒元素集中和堆精魂？",
            "step": 2,
        },
        runtime_dir=rt,
    )
    assert r2["reusedTurn"] is True
    assert r2["injectText"] == ""
    assert r2["turnStartTs"] == t0
    after_state = load_turn_state("sess-1", runtime_dir=rt)
    assert after_state is not None
    assert after_state["stopRetries"] == 1

    # New turn resets stopRetries
    r3 = handle_pre_prompt(
        {
            "sessionId": "sess-1",
            "turnId": "turn-2",
            "userText": "换一个问题：圣宗开憎恨怎么样？",
            "step": 1,
        },
        runtime_dir=rt,
    )
    assert r3["reusedTurn"] is False
    new_state = load_turn_state("sess-1", runtime_dir=rt)
    assert new_state is not None
    assert new_state["stopRetries"] == 0


def test_gate2_pre_tool_all_five_rules_and_exemptions(tmp_path: Path) -> None:
    rt = tmp_path / "rt"

    # Non-shell tool always allowed
    assert handle_pre_tool({"toolName": "read_file", "commandLine": "pob --help"}, runtime_dir=rt)["decision"] == "allow"

    # Rule 1: NO_RAW_ENGINE_LUA
    r1 = handle_pre_tool(
        {"toolName": "bash", "commandLine": "rg Awakened .runtime/engine/Data/Gems.lua"},
        runtime_dir=rt,
    )
    assert r1["decision"] == "deny" and r1["ruleId"] == "NO_RAW_ENGINE_LUA"

    # Rule 2: NO_MANUAL_XML_CALC
    r2 = handle_pre_tool(
        {"toolName": "powershell", "commandLine": 'python -c "import xml.etree.ElementTree as ET; ET.parse(\'build.xml\')"'},
        runtime_dir=rt,
    )
    assert r2["decision"] == "deny" and r2["ruleId"] == "NO_MANUAL_XML_CALC"

    # Rule 3: NO_POB_HELP
    r3 = handle_pre_tool({"toolName": "run_command", "commandLine": "pob diagnose --help"}, runtime_dir=rt)
    assert r3["decision"] == "deny" and r3["ruleId"] == "NO_POB_HELP"
    assert "pob diagnose" in (r3["reason"] or "")

    # Rule 4: NO_BARE_JSON (deny without overwrite, rewrite with overwrite, exempt hook/--out/redirect)
    r4_deny = handle_pre_tool(
        {"toolName": "bash", "commandLine": "pob summary --json", "supportsOverwrite": False},
        runtime_dir=rt,
    )
    assert r4_deny["decision"] == "deny" and r4_deny["ruleId"] == "NO_BARE_JSON"

    r4_rw = handle_pre_tool(
        {"toolName": "run_command", "commandLine": "pob summary --json --compact --target main", "supportsOverwrite": True},
        runtime_dir=rt,
    )
    assert r4_rw["decision"] == "allow" and r4_rw["ruleId"] == "NO_BARE_JSON"
    assert r4_rw["rewrittenCommand"] == "pob summary --target main"

    assert handle_pre_tool({"toolName": "bash", "commandLine": "pob summary --json --out out.json"}, runtime_dir=rt)["decision"] == "allow"
    assert handle_pre_tool({"toolName": "bash", "commandLine": "pob summary --json > out.json"}, runtime_dir=rt)["decision"] == "allow"
    assert handle_pre_tool({"toolName": "bash", "commandLine": "pob --offline hook pre-tool --json"}, runtime_dir=rt)["decision"] == "allow"

    # Rule 5: NO_REDUNDANT_POST_DIAGNOSE
    handle_pre_prompt({"sessionId": "s5", "turnId": "t1", "userText": "诊断 @a.xml"}, runtime_dir=rt)
    append_receipt(
        {
            "ts": time.time(),
            "cmd": "diagnose @a.xml",
            "subcommand": "diagnose",
            "category": "diagnose",
            "isCompute": True,
            "hasTickCheck": True,
            "slot": "main",
            "rev": 1,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    for bare_cmd in ("pob summary", "pob skills", "pob items"):
        r5 = handle_pre_tool({"sessionId": "s5", "toolName": "bash", "commandLine": bare_cmd}, runtime_dir=rt)
        assert r5["decision"] == "deny" and r5["ruleId"] == "NO_REDUNDANT_POST_DIAGNOSE", bare_cmd

    # Filtered queries still allowed
    assert handle_pre_tool({"sessionId": "s5", "toolName": "bash", "commandLine": 'pob items --slot "Ring 1"'}, runtime_dir=rt)["decision"] == "allow"
    assert handle_pre_tool({"sessionId": "s5", "toolName": "bash", "commandLine": "pob skills --group 1"}, runtime_dir=rt)["decision"] == "allow"

    # After an --apply receipt, pob summary is allowed again
    append_receipt(
        {
            "ts": time.time() + 0.01,
            "cmd": "item edit --slot Ring 1 --apply",
            "subcommand": "item edit",
            "category": "mutate",
            "isCompute": True,
            "hasTickCheck": False,
            "slot": "main",
            "rev": 2,
            "applied": True,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    assert handle_pre_tool({"sessionId": "s5", "toolName": "bash", "commandLine": "pob summary"}, runtime_dir=rt)["decision"] == "allow"


def test_gate3_verify_stop_v1_v2_v3_v4_and_antiloop(tmp_path: Path, monkeypatch: Any) -> None:
    rt = tmp_path / "rt"

    # V1: hallucination & mechanic fallacy caught
    handle_pre_prompt({"sessionId": "v1s", "turnId": "t1", "userText": "辅助石怎么选"}, runtime_dir=rt)
    res_v1 = handle_verify_stop(
        {
            "sessionId": "v1s",
            "turnId": "t1",
            "userText": "辅助石怎么选",
            "assistantText": "建议连觉醒元素集中，并把卓越辅助升到20级，同时在装备上堆精魂，CwC 靠堆 CDR 缩短间隔。",
            "continuationActive": False,
            "terminationReason": "NO_TOOL_CALL",
        },
        runtime_dir=rt,
    )
    assert res_v1["decision"] == "continue"
    assert "V1" in res_v1["failedChecks"]
    assert res_v1["stopRetries"] == 1
    assert '<pob-hook-correction retry="1/1">' in res_v1["correctionPrompt"]
    assert len(res_v1["correctionPrompt"]) <= MAX_CORRECTION_CHARS

    # Anti-loop: second call in same turn MUST allow_stop
    res_v1_retry = handle_verify_stop(
        {
            "sessionId": "v1s",
            "turnId": "t1",
            "userText": "辅助石怎么选",
            "assistantText": "还是建议堆精魂。",
            "continuationActive": False,
            "terminationReason": "NO_TOOL_CALL",
        },
        runtime_dir=rt,
    )
    assert res_v1_retry["decision"] == "allow_stop"
    assert res_v1_retry.get("advisoryWarning")

    # Anti-loop: terminationReason != NO_TOOL_CALL & continuationActive=True & empty text
    assert handle_verify_stop({"sessionId": "x", "assistantText": "建议堆精魂", "terminationReason": "USER_CANCELED"}, runtime_dir=rt)["decision"] == "allow_stop"
    assert handle_verify_stop({"sessionId": "x", "assistantText": "建议堆精魂", "continuationActive": True}, runtime_dir=rt)["decision"] == "allow_stop"
    assert handle_verify_stop({"sessionId": "x", "assistantText": "   "}, runtime_dir=rt)["decision"] == "allow_stop"

    # Zero-false-positive compliant reply (negation + legal 3.30 terms)
    handle_pre_prompt({"sessionId": "ok1", "turnId": "t1", "userText": "机制问答"}, runtime_dir=rt)
    res_ok = handle_verify_stop(
        {
            "sessionId": "ok1",
            "turnId": "t1",
            "userText": "机制问答",
            "assistantText": "3.28 已移除觉醒元素集中，PoE1 没有精魂机制。圣宗开憎恨配合启迪是合法的；不确定 1 号戒指有没有空后缀，建议先检查。",
            "terminationReason": "NO_TOOL_CALL",
        },
        runtime_dir=rt,
    )
    assert res_ok["decision"] == "allow_stop"
    assert res_ok["stopRetries"] == 0

    # V2a: status-only receipt cannot satisfy DPS claim
    handle_pre_prompt({"sessionId": "v2a", "turnId": "t1", "userText": "看下构筑伤害"}, runtime_dir=rt)
    append_receipt(
        {
            "ts": time.time(),
            "cmd": "status",
            "subcommand": "status",
            "category": "status",
            "isCompute": False,
            "hasTickCheck": False,
            "slot": "main",
            "rev": 1,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    res_v2a = handle_verify_stop(
        {
            "sessionId": "v2a",
            "turnId": "t1",
            "userText": "看下构筑伤害",
            "assistantText": "你这套构筑目前大约有 12.5M DPS，生存很稳。",
        },
        runtime_dir=rt,
    )
    assert res_v2a["decision"] == "continue" and "V2" in res_v2a["failedChecks"]

    # V2b: stale rev after --apply triggers V2 + mocked daemon summary remediation
    handle_pre_prompt({"sessionId": "v2b", "turnId": "t1", "userText": "改完后伤害多少"}, runtime_dir=rt)
    now = time.time()
    append_receipt(
        {
            "ts": now,
            "cmd": "summary",
            "subcommand": "summary",
            "category": "inspect",
            "isCompute": True,
            "hasTickCheck": False,
            "slot": "main",
            "rev": 3,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    append_receipt(
        {
            "ts": now + 0.01,
            "cmd": "load @new.xml --apply",
            "subcommand": "load",
            "category": "lifecycle",
            "isCompute": False,
            "hasTickCheck": False,
            "slot": "main",
            "rev": 4,
            "applied": True,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    import pob_cli.cli_hook.remediate as rem_mod

    monkeypatch.setattr(rem_mod, "read_daemon_state", lambda _rt=None: {"pid": os.getpid(), "port": 1, "token": "t"})
    monkeypatch.setattr(
        rem_mod,
        "call_daemon",
        lambda action, params=None, **kw: {
            "rev": 4,
            "buildInfo": {"level": 95, "ascendClassNameZh": "圣宗"},
            "offence": {"FullDPS": 8450000, "TotalDPS": 8450000, "Speed": 4.2},
            "defence": {"TotalEHP": 92000, "Life": 4500, "EnergyShield": 3000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": 75},
        },
    )
    res_v2b = handle_verify_stop(
        {
            "sessionId": "v2b",
            "turnId": "t1",
            "userText": "改完后伤害多少",
            "assistantText": "目前面板 9.1M DPS。",
        },
        runtime_dir=rt,
    )
    assert res_v2b["decision"] == "continue" and "V2" in res_v2b["failedChecks"]
    assert "8.45M" in res_v2b["correctionPrompt"] or "8,450,000" in res_v2b["correctionPrompt"]

    # V3: open affix claim without item inspect -> V3 + mocked items/inspect_item remediation
    monkeypatch.setattr(rem_mod, "read_daemon_state", lambda _rt=None: None)
    handle_pre_prompt({"sessionId": "v3s", "turnId": "t1", "userText": "戒指怎么补抗性"}, runtime_dir=rt)
    res_v3 = handle_verify_stop(
        {
            "sessionId": "v3s",
            "turnId": "t1",
            "userText": "戒指怎么补抗性",
            "assistantText": "你的1号戒指还有空后缀，可以直接工艺台补冰抗。",
        },
        runtime_dir=rt,
    )
    assert res_v3["decision"] == "continue" and "V3" in res_v3["failedChecks"]
    assert "Ring 1" in res_v3["correctionPrompt"]

    # V3 passes once Ring 1 is in inspectedSlots
    handle_pre_prompt({"sessionId": "v3ok", "turnId": "t1", "userText": "戒指怎么补抗性"}, runtime_dir=rt)
    append_receipt(
        {
            "ts": time.time(),
            "cmd": "item inspect --slot Ring 1",
            "subcommand": "item inspect",
            "category": "inspect_slot",
            "isCompute": False,
            "hasTickCheck": False,
            "slot": "main",
            "rev": 1,
            "applied": False,
            "inspectedSlots": ["Ring 1"],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    assert (
        handle_verify_stop(
            {
                "sessionId": "v3ok",
                "turnId": "t1",
                "userText": "戒指怎么补抗性",
                "assistantText": "你的1号戒指还有空后缀，可以直接工艺台补冰抗。",
            },
            runtime_dir=rt,
        )["decision"]
        == "allow_stop"
    )

    # V4: trigger breakpoint claim without tick check -> V4 + 0ms tick table
    handle_pre_prompt({"sessionId": "v4s", "turnId": "t1", "userText": "CoC 攻速上限多少"}, runtime_dir=rt)
    res_v4 = handle_verify_stop(
        {
            "sessionId": "v4s",
            "turnId": "t1",
            "userText": "CoC 攻速上限多少",
            "assistantText": "暴击时施放 14% CDR 断点对应攻速上限 7.57 APS。",
        },
        runtime_dir=rt,
    )
    assert res_v4["decision"] == "continue" and "V4" in res_v4["failedChecks"]
    assert "0.033s" in res_v4["correctionPrompt"] and "14%" in res_v4["correctionPrompt"]


def test_protocols_agy_claude_transcript_and_fail_open(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {"POB_RUNTIME_DIR": str(tmp_path / "rt")}

    # Fail-open on invalid JSON stdin
    code, out = _run_hook(runner, "pre-tool", "{invalid-json", protocol="agy", env=env)
    assert code == 0 and out == {"decision": "allow"}

    # AGY PreInvocation -> injectSteps on step 1, {} on step 2 (same lastUserInput)
    code1, out1 = _run_hook(
        runner,
        "pre-prompt",
        {"conversationId": "conv-1", "invocationNum": 1, "lastUserInput": "可以连觉醒元素集中吗"},
        protocol="agy",
        env=env,
    )
    assert code1 == 0
    assert "injectSteps" in out1
    assert "systemMessage" in out1["injectSteps"][0]["systemMessage"]

    code2, out2 = _run_hook(
        runner,
        "pre-prompt",
        {"conversationId": "conv-1", "invocationNum": 2, "lastUserInput": "可以连觉醒元素集中吗"},
        protocol="agy",
        env=env,
    )
    assert code2 == 0 and out2 == {}

    # AGY PreToolUse preserves original arg key (CommandLine vs command)
    c_cl, o_cl = _run_hook(
        runner,
        "pre-tool",
        {"conversationId": "conv-1", "toolCall": {"name": "run_command", "args": {"CommandLine": "pob summary --json"}}},
        protocol="agy",
        env=env,
    )
    assert c_cl == 0 and o_cl["decision"] == "allow" and o_cl["overwrite"] == {"CommandLine": "pob summary"}

    c_cmd, o_cmd = _run_hook(
        runner,
        "pre-tool",
        {"conversationId": "conv-1", "toolCall": {"name": "bash", "args": {"command": "pob summary --json --compact"}}},
        protocol="agy",
        env=env,
    )
    assert c_cmd == 0 and o_cmd["decision"] == "allow" and o_cmd["overwrite"] == {"command": "pob summary"}

    # AGY Stop extracts assistant text from transcriptPath JSONL
    tr = tmp_path / "transcript.jsonl"
    tr.write_text(
        json.dumps({"type": "user", "content": "hi"}) + "\n"
        + json.dumps({"plannerResponse": {"response": "建议直接堆精魂来开光环。"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert "精魂" in extract_latest_assistant_from_transcript(tr)
    c_stop, o_stop = _run_hook(
        runner,
        "verify-stop",
        {
            "conversationId": "conv-1",
            "lastUserInput": "可以连觉醒元素集中吗",
            "transcriptPath": str(tr),
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
        },
        protocol="agy",
        env=env,
    )
    assert c_stop == 0 and o_stop["decision"] == "continue"
    assert '<pob-hook-correction retry="1/1">' in o_stop["reason"]

    # Claude protocol adapters
    cc_pre, co_pre = _run_hook(
        runner,
        "pre-prompt",
        {"session_id": "cl-1", "prompt": "PoE1 能堆精魂吗"},
        protocol="claude",
        env=env,
    )
    assert cc_pre == 0 and co_pre["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    cc_tool, co_tool = _run_hook(
        runner,
        "pre-tool",
        {"session_id": "cl-1", "tool_name": "Bash", "tool_input": {"command": "pob diagnose --help"}},
        protocol="claude",
        env=env,
    )
    assert cc_tool == 0 and co_tool["hookSpecificOutput"]["permissionDecision"] == "deny"

    tr_cl = tmp_path / "claude_tr.jsonl"
    tr_cl.write_text(
        json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "建议连觉醒元素集中。"}]}},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    cc_stop, co_stop = _run_hook(
        runner,
        "verify-stop",
        {"session_id": "cl-1", "prompt": "PoE1 能堆精魂吗", "transcript_path": str(tr_cl), "stop_hook_active": False},
        protocol="claude",
        env=env,
    )
    assert cc_stop == 0 and co_stop["decision"] == "block"


def test_cli_receipt_tap_and_correction_cap(tmp_path: Path, monkeypatch: Any) -> None:
    rt = tmp_path / "rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.delenv("POB_HOOK_DISABLE_RECEIPTS", raising=False)
    monkeypatch.setattr(
        cli_mod,
        "_daemon_call",
        lambda action, params=None, **kw: {
            "rev": 7,
            "slot": "main",
            "buildInfo": {"level": 90, "className": "Templar", "ascendClassName": "Hierophant", "points": {}},
            "offence": {"FullDPS": 1000000},
            "defence": {"TotalEHP": 50000},
        },
    )
    runner = CliRunner()
    t0 = time.time() - 1.0
    r = runner.invoke(main, ["--offline", "summary"])
    assert r.exit_code == 0
    recs = read_turn_receipts(t0, runtime_dir=rt)
    assert len(recs) == 1
    assert recs[0]["isCompute"] is True
    assert recs[0]["rev"] == 7
    assert recs[0]["exitCode"] == 0

    # `pob hook` must NOT record a receipt
    r_hook = runner.invoke(main, ["--offline", "hook", "pre-tool"], input="{}")
    assert r_hook.exit_code == 0
    assert len(read_turn_receipts(t0, runtime_dir=rt)) == 1

    # POB_HOOK_DISABLE_RECEIPTS=1 suppresses receipt tap
    monkeypatch.setenv("POB_HOOK_DISABLE_RECEIPTS", "1")
    r2 = runner.invoke(main, ["--offline", "summary"])
    assert r2.exit_code == 0
    assert len(read_turn_receipts(t0, runtime_dir=rt)) == 1

    # Correction formatter strictly caps at <=2500 chars
    huge = build_correction_prompt(["V1 fail"], ["A" * 5000])
    assert len(huge) <= MAX_CORRECTION_CHARS
    assert huge.startswith('<pob-hook-correction retry="1/1">')
    assert huge.endswith("</pob-hook-correction>")


def test_concurrent_sessions_do_not_cross_satisfy_gate3_v2(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    handle_pre_prompt({"sessionId": "agent-A", "turnId": "1", "userText": "诊断 @a.xml"}, runtime_dir=rt)
    handle_pre_prompt({"sessionId": "agent-B", "turnId": "1", "userText": "诊断 @b.xml"}, runtime_dir=rt)
    append_receipt(
        {
            "ts": time.time(),
            "cmd": "diagnose @a.xml",
            "subcommand": "diagnose",
            "category": "diagnose",
            "isCompute": True,
            "hasTickCheck": True,
            "slot": "slot_a",
            "rev": 1,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 0,
            "sessionId": "agent-A",
        },
        runtime_dir=rt,
    )
    # Agent A passes V2; Agent B must NOT inherit Agent A's diagnose receipt
    res_a = handle_verify_stop(
        {"sessionId": "agent-A", "turnId": "1", "userText": "诊断 @a.xml", "assistantText": "当前面板 5.2M DPS。"},
        runtime_dir=rt,
    )
    res_b = handle_verify_stop(
        {"sessionId": "agent-B", "turnId": "1", "userText": "诊断 @b.xml", "assistantText": "当前面板 8.8M DPS。"},
        runtime_dir=rt,
    )
    assert res_a["decision"] == "allow_stop"
    assert res_b["decision"] == "continue" and "V2" in res_b["failedChecks"]
