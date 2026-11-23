"""Comprehensive offline unit & integration tests for V5 web-research guardrails."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pob_cli.cli_hook.gates import handle_pre_prompt, handle_verify_stop
from pob_cli.cli_hook.remediate import MAX_CORRECTION_CHARS
from pob_cli.guardrails import (
    detect_hesitation_claims,
    detect_mechanics_research_need,
)
from pob_cli.turn_receipts import (
    append_receipt,
    classify_cli_args,
    load_turn_state,
    read_turn_receipts,
    receipt_context,
    summarize_turn_receipts,
)


class TestV5DetectorsL0:
    def test_detect_hesitation_claims_catches_zh_and_en(self) -> None:
        samples = [
            "如果没记错，返回投射物大概可以二次击中同一个 Boss。",
            "理论上可以触发两次，但不太确定建议进游戏实测。",
            "触发机器人应该吃 30% less，PoB 可能不准。",
            "印象中同类曝露可能不叠加，以游戏内实测为准。",
            "I think Returning Projectiles probably triggers twice, not 100% sure.",
            "Theoretically it might stack, presumably you should test in-game.",
            "As far as I know from memory, this is unverified in PoB.",
        ]
        for text in samples:
            hits = detect_hesitation_claims(text)
            assert hits, f"Expected hesitation hit for: {text!r}"

    def test_detect_hesitation_claims_exempts_v3_and_citations_and_blockquotes(self) -> None:
        safe_samples = [
            "不确定 1 号戒指有没有空后缀，建议先检查。",
            "不确定 1号戒指是否有空后缀，建议先检查装备词缀后再决定工艺台附魔。",
            "头盔不确定有没有空后缀，先检查一下再决定。",
            "Not sure if Ring 2 has an open suffix; check if Ring 2 has an open suffix first.",
            "据 poewiki 机制说明，PoB 未模拟该二次返回击中，实战生效。",
            "根据 poedb 3.30 词缀表，实测数据表明该词缀权重为 250。",
            "> 用户原话：我不太确定这个机制大概能不能触发？\n返回投射物在穿透、分裂、连锁全部耗尽后才会返回。",
        ]
        for text in safe_samples:
            assert detect_hesitation_claims(text) == [], f"Unexpected false positive for: {text!r}"

    def test_detect_mechanics_research_need_triggers_on_q1_to_q4_and_en(self) -> None:
        prompts = [
            "PoE1 3.30 里，「返回投射物(Returning Projectiles)」配合「穿透(Pierce) / 连锁(Chain) / 分裂(Fork)」的触发先后顺序是什么？",
            "构筑里「万古之剑 / 时空幻影」配合破坏者触发机器人(Triggerbots) 到底吃不吃 30% less，PoB 算得准不准？",
            "在 poedb.tw 上 T1 能量护盾百分比和固定护盾各自需要多少物品等级(iLvl)？权重高不高？",
            "圣宗玩「法术图腾(Spell Totem)」连「启迪(Inspiration)」，图腾消耗魔力算不算触发秘能波动？同类曝露能叠加吗？",
            "How does Eldritch Battery work with Mind over Matter in PoE1?",
            "Does Triggerbots apply to Sword of Ages or share cooldown?",
        ]
        for p in prompts:
            res = detect_mechanics_research_need(p)
            assert res["needsResearch"] is True, f"Expected needsResearch=True for: {p!r}"
            assert res["reasons"]
            assert res["suggestedQueries"]

    def test_detect_mechanics_research_need_zero_false_positives_on_controls(self) -> None:
        controls = [
            "帮我查一下 @build.xml 的去水面板和 Ring 1 空后缀",
            "帮我查一下 @C:/pob/builds/saboteur_vd_lv85_starter.xml 的去水面板，并看看 Ring 1 和 Gloves 哪件还有空后缀可以工艺台补抗性。",
            "圣宗开憎恨配启迪要注意什么？",
            "3.28 还能用觉醒元素集中吗？",
            "辅助石怎么选",
            "机制问答",
            "CoC 攻速上限多少",
            "帮我看下 @build.xml 能不能连觉醒元素集中和堆精魂？",
        ]
        for p in controls:
            res = detect_mechanics_research_need(p)
            assert res["needsResearch"] is False, f"Unexpected false positive for control: {p!r} -> {res}"


class TestTurnReceiptsWebResearchL1:
    def test_classify_and_summarize_web_research_success_and_failure(self, tmp_path: Path) -> None:
        for cmd_argv, expected_sub in (
            (["poedb", "Twilight Regalia", "--mods"], "poedb"),
            (["wiki", "Returning Projectiles"], "wiki"),
            (["research", "Triggerbots", "Sword of Ages"], "research"),
        ):
            meta = classify_cli_args(cmd_argv)
            assert meta["category"] == "web_research"
            assert meta["isWebResearch"] is True
            assert meta["subcommand"] == expected_sub
            assert meta["shouldRecord"] is True

        assert classify_cli_args(["summary"])["isWebResearch"] is False
        assert classify_cli_args(["hook", "verify-stop"])["isWebResearch"] is False

        # Failed web research (e.g. offline/proxy timeout, exitCode=1) sets attemptedWebResearch=True, hasWebResearch=False
        t0 = time.time() - 1.0
        with pytest.raises(SystemExit), receipt_context(["research", "Triggerbots"], runtime_dir=tmp_path):
            raise SystemExit(1)

        r1 = read_turn_receipts(t0, runtime_dir=tmp_path)
        s1 = summarize_turn_receipts(r1)
        assert s1["attemptedWebResearch"] is True
        assert s1["hasWebResearch"] is False

        # Successful web research sets both True
        with receipt_context(["wiki", "Returning Projectiles"], runtime_dir=tmp_path):
            pass
        s2 = summarize_turn_receipts(read_turn_receipts(t0, runtime_dir=tmp_path))
        assert s2["attemptedWebResearch"] is True
        assert s2["hasWebResearch"] is True


class TestHookV5GatesAndRemediationL4:
    def test_gate1_injects_mechanics_directive_even_with_zero_grounding_cards(self, tmp_path: Path) -> None:
        rt = tmp_path / "rt"
        prompt = "How does Returning Projectiles work with Sniper's Mark?"
        res = handle_pre_prompt({"sessionId": "g1", "turnId": "t1", "userText": prompt}, runtime_dir=rt)
        assert res["needsWebResearch"] is True
        assert "[MECHANICS-RESEARCH-REQUIRED]" in res["injectText"]
        assert "pob research" in res["injectText"]

        state = load_turn_state("g1", runtime_dir=rt)
        assert state is not None
        assert state["needsWebResearch"] is True
        assert state["suggestedResearchQueries"]

    def test_v5_stays_active_under_pob_offline_and_respects_explicit_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = tmp_path / "rt"
        monkeypatch.setenv("POB_OFFLINE", "1")
        monkeypatch.delenv("POB_HOOK_DISABLE_V5", raising=False)

        # V5 MUST fire even when POB_OFFLINE=1!
        handle_pre_prompt({"sessionId": "off1", "turnId": "t1", "userText": "返回投射物怎么运作？"}, runtime_dir=rt)
        res = handle_verify_stop(
            {
                "sessionId": "off1",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "大概可以二次击中同一个 Boss，不太确定建议进游戏实测。",
                "terminationReason": "NO_TOOL_CALL",
            },
            runtime_dir=rt,
        )
        assert res["decision"] == "continue"
        assert "V5" in res["failedChecks"]
        assert "pob research" in res["correctionPrompt"]
        assert len(res["correctionPrompt"]) <= MAX_CORRECTION_CHARS

        # Explicit disableV5=True skips V5
        handle_pre_prompt({"sessionId": "off2", "turnId": "t1", "userText": "返回投射物怎么运作？"}, runtime_dir=rt)
        res_dis_req = handle_verify_stop(
            {
                "sessionId": "off2",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "大概可以二次击中。",
                "disableV5": True,
            },
            runtime_dir=rt,
        )
        assert res_dis_req["decision"] == "allow_stop"

        # Explicit POB_HOOK_DISABLE_V5=1 skips V5
        monkeypatch.setenv("POB_HOOK_DISABLE_V5", "1")
        handle_pre_prompt({"sessionId": "off3", "turnId": "t1", "userText": "返回投射物怎么运作？"}, runtime_dir=rt)
        res_dis_env = handle_verify_stop(
            {
                "sessionId": "off3",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "大概可以二次击中。",
            },
            runtime_dir=rt,
        )
        assert res_dis_env["decision"] == "allow_stop"

    def test_v5_passes_on_web_receipt_attempted_failure_or_host_researched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = tmp_path / "rt"
        monkeypatch.delenv("POB_HOOK_DISABLE_V5", raising=False)

        # 1) hasWebResearch (exitCode=0) -> allow_stop
        handle_pre_prompt({"sessionId": "ok-web", "turnId": "t1", "userText": "触发机器人吃不吃 30% less？"}, runtime_dir=rt)
        append_receipt(
            {
                "ts": time.time(),
                "cmd": "research Triggerbots",
                "subcommand": "research",
                "category": "web_research",
                "isCompute": False,
                "hasTickCheck": False,
                "isWebResearch": True,
                "slot": "main",
                "rev": None,
                "applied": False,
                "inspectedSlots": [],
                "exitCode": 0,
                "sessionId": "ok-web",
            },
            runtime_dir=rt,
        )
        r1 = handle_verify_stop(
            {
                "sessionId": "ok-web",
                "turnId": "t1",
                "userText": "触发机器人吃不吃 30% less？",
                "assistantText": "据 poewiki，触发机器人会分别触发两次，并施加 35% less 伤害。",
            },
            runtime_dir=rt,
        )
        assert r1["decision"] == "allow_stop"

        # 2) attemptedWebResearch (network failure exitCode=1) -> fail-open allow_stop (no deadlock!)
        handle_pre_prompt({"sessionId": "fail-web", "turnId": "t1", "userText": "触发机器人吃不吃 30% less？"}, runtime_dir=rt)
        append_receipt(
            {
                "ts": time.time(),
                "cmd": "wiki Triggerbots",
                "subcommand": "wiki",
                "category": "web_research",
                "isCompute": False,
                "hasTickCheck": False,
                "isWebResearch": True,
                "slot": "main",
                "rev": None,
                "applied": False,
                "inspectedSlots": [],
                "exitCode": 1,
                "sessionId": "fail-web",
            },
            runtime_dir=rt,
        )
        r2 = handle_verify_stop(
            {
                "sessionId": "fail-web",
                "turnId": "t1",
                "userText": "触发机器人吃不吃 30% less？",
                "assistantText": "当前离线无法联网核验，理论上应该生效。",
            },
            runtime_dir=rt,
        )
        assert r2["decision"] == "allow_stop"

        # 3) hostWebResearched=True -> allow_stop
        handle_pre_prompt({"sessionId": "host-web", "turnId": "t1", "userText": "返回投射物怎么运作？"}, runtime_dir=rt)
        r3 = handle_verify_stop(
            {
                "sessionId": "host-web",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "返回投射物在穿透、分裂、连锁全部耗尽后触发返回。",
                "hostWebResearched": True,
            },
            runtime_dir=rt,
        )
        assert r3["decision"] == "allow_stop"

    def test_v5_remediation_uses_local_disk_cache_0ms_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = tmp_path / "rt"
        cache_dir = rt / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("POB_CACHE_DIR", str(cache_dir))
        monkeypatch.delenv("POB_HOOK_DISABLE_V5", raising=False)

        (cache_dir / "poewiki_abc123456789.json").write_text(
            json.dumps(
                {
                    "_ts": time.time(),
                    "data": {
                        "query": "Returning Projectiles",
                        "title": "Returning Projectiles Support",
                        "url": "https://www.poewiki.net/wiki/Returning_Projectiles_Support",
                        "summary": "Projectiles return only after Pierce, Fork, and Chain are exhausted.",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (cache_dir / "poedb_page_cn_deadbeef0001.json").write_text(
            json.dumps(
                {
                    "_ts": time.time(),
                    "url": "https://poedb.tw/cn/Returning_Projectiles_Support",
                    "html": "<html><head><title>Returning Projectiles Support</title></head><body><h1>Returning Projectiles Support</h1><p>Supported Skills have Returning Projectiles deal 61% less Damage.</p></body></html>",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        prompt = "「返回投射物(Returning Projectiles)」怎么运作？"
        handle_pre_prompt({"sessionId": "cache-hit", "turnId": "t1", "userText": prompt}, runtime_dir=rt)
        res = handle_verify_stop(
            {
                "sessionId": "cache-hit",
                "turnId": "t1",
                "userText": prompt,
                "assistantText": "大概会先返回再穿透。",
            },
            runtime_dir=rt,
        )
        assert res["decision"] == "continue"
        assert "V5" in res["failedChecks"]
        corr = res["correctionPrompt"]
        assert "[Cached poewiki]" in corr
        assert "Pierce, Fork, and Chain" in corr
        assert "[Cached poedb]" in corr
        assert len(corr) <= MAX_CORRECTION_CHARS

    def test_v5_zero_fp_on_q6_control_and_max_retries_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = tmp_path / "rt"
        monkeypatch.delenv("POB_HOOK_DISABLE_V5", raising=False)

        # Q6 control: pure build panel + item inspect, no mechanics question, no hesitation -> allow_stop
        q6 = "帮我查一下 @C:/pob/builds/saboteur_vd_lv85_starter.xml 的去水面板，并看看 Ring 1 和 Gloves 哪件还有空后缀可以工艺台补抗性。"
        g1 = handle_pre_prompt({"sessionId": "q6", "turnId": "t1", "userText": q6}, runtime_dir=rt)
        assert g1["needsWebResearch"] is False
        assert "[MECHANICS-RESEARCH-REQUIRED]" not in g1["injectText"]

        append_receipt(
            {
                "ts": time.time(),
                "cmd": "diagnose @C:/pob/builds/saboteur_vd_lv85_starter.xml",
                "subcommand": "diagnose",
                "category": "diagnose",
                "isCompute": True,
                "hasTickCheck": True,
                "isWebResearch": False,
                "slot": "main",
                "rev": 1,
                "applied": False,
                "inspectedSlots": ["Ring 1", "Gloves"],
                "exitCode": 0,
                "sessionId": "q6",
            },
            runtime_dir=rt,
        )
        res_q6 = handle_verify_stop(
            {
                "sessionId": "q6",
                "turnId": "t1",
                "userText": q6,
                "assistantText": "当前去水面板 4.2M DPS，EHP 6.5万。Ring 1 有 1 个空后缀可工艺台补火抗，Gloves 词缀已满。",
            },
            runtime_dir=rt,
        )
        assert res_q6["decision"] == "allow_stop"
        assert res_q6["stopRetries"] == 0

        # Anti-loop maxRetries=1 for V5
        handle_pre_prompt({"sessionId": "loop5", "turnId": "t1", "userText": "返回投射物怎么运作？"}, runtime_dir=rt)
        r_first = handle_verify_stop(
            {
                "sessionId": "loop5",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "大概可以二次击中。",
            },
            runtime_dir=rt,
        )
        assert r_first["decision"] == "continue" and r_first["stopRetries"] == 1
        r_second = handle_verify_stop(
            {
                "sessionId": "loop5",
                "turnId": "t1",
                "userText": "返回投射物怎么运作？",
                "assistantText": "大概还是可以二次击中。",
            },
            runtime_dir=rt,
        )
        assert r_second["decision"] == "allow_stop" and r_second["stopRetries"] == 1

    def test_gate2_blocks_grep_glob_read_on_engine_lua_dirs_and_allows_normal_paths(
        self, tmp_path: Path
    ) -> None:
        from pob_cli.cli_hook.gates import handle_pre_tool
        from pob_cli.cli_hook.protocols import decode_input

        rt = tmp_path / "rt"
        # Direct raw request: grep / read on PobTools/PoE1/Data and .runtime/engine
        d_grep = handle_pre_tool(
            {"toolName": "grep", "targetPaths": [r"C:\pob\PobTools[20261102]\PoE1\Data"]},
            runtime_dir=rt,
        )
        assert d_grep["decision"] == "deny" and d_grep["ruleId"] == "NO_RAW_ENGINE_LUA"

        d_read = handle_pre_tool(
            {"toolName": "read", "targetPaths": [r"C:\pob\.runtime\engine\PoE1\Data\Skills\other.lua"]},
            runtime_dir=rt,
        )
        assert d_read["decision"] == "deny" and d_read["ruleId"] == "NO_RAW_ENGINE_LUA"

        # Claude & AGY protocol adapters extract targetPaths from tool_input / toolCall.args
        req_claude, _ = decode_input(
            "pre-tool",
            "claude",
            {"tool_name": "Grep", "tool_input": {"pattern": "Temporal", "path": r"C:\pob\PobTools[20261102]\PoE1\Data"}},
        )
        assert handle_pre_tool(req_claude, runtime_dir=rt)["decision"] == "deny"

        req_agy, _ = decode_input(
            "pre-tool",
            "agy",
            {"toolCall": {"name": "read_file", "args": {"Path": r"C:\pob\.runtime\engine\PoE1\Modules\CalcTriggers.lua"}}},
        )
        assert handle_pre_tool(req_agy, runtime_dir=rt)["decision"] == "deny"

        # Normal non-engine grep/read stays allowed
        assert handle_pre_tool({"toolName": "grep", "targetPaths": [r"C:\work\notes.md"]}, runtime_dir=rt)["decision"] == "allow"

    def test_gate1_build_path_cn_colon_and_q5_assertion_verification(self, tmp_path: Path) -> None:
        rt = tmp_path / "rt"
        q2 = "帮我看下 @C:/pob/builds/saboteur_vd_lv98_endgame.xml：先算出真实去水 Pinnacle DPS/EHP，再帮我核实触发机器人吃不吃 30% less？"
        g_q2 = handle_pre_prompt({"sessionId": "q2-cn", "turnId": "t1", "userText": q2}, runtime_dir=rt)
        assert g_q2["hasBuildSource"] is True
        assert g_q2["needsWebResearch"] is True

        q5 = "听说 PoE1 3.30 酋长点了雷默科(Ramako)站立不动时，再挂易燃(Flammability)和火曝露可以把怪物火抗降到 -60%，这套思路对吗？"
        g_q5 = handle_pre_prompt({"sessionId": "q5-assert", "turnId": "t1", "userText": q5}, runtime_dir=rt)
        assert g_q5["needsWebResearch"] is True
        assert "[MECHANICS-RESEARCH-REQUIRED]" in g_q5["injectText"]
