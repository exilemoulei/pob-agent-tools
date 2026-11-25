"""Offline unit tests for L0 reply_scanner and L1 turn_receipts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pob_cli.guardrails import (
    ReplyIssue,
    detect_build_source_in_text,
    detect_panel_or_delta_claims,
    detect_trigger_breakpoint_claims,
    extract_claimed_open_affix_slots,
    scan_assistant_reply,
)
from pob_cli.turn_receipts import (
    MAX_RECEIPT_LINES,
    RECEIPTS_FILENAME,
    append_receipt,
    classify_cli_args,
    load_turn_state,
    read_turn_receipts,
    receipt_context,
    record_daemon_receipt_hint,
    save_turn_state,
    session_hash,
    summarize_turn_receipts,
)


class TestReplyScannerV1:
    def test_catches_poe2_terms_urls_ascendancies_and_jewels(self) -> None:
        samples = [
            ("建议胸甲多堆点精魂开光环。", "POE2_SPIRIT_RESERVATION"),
            ("You should stack Spirit on your chest for extra auras.", "POE2_SPIRIT_RESERVATION"),
            ("项链用涂灌上核心天赋。", "POE2_DISTILLED_EMOTIONS"),
            ("Farm T15 Waystones for endgame progression.", "POE2_WAYSTONE"),
            ("在头盔里镶嵌灵魂核心补抗性。", "POE2_SOUL_CORES_AND_RUNE_MODS"),
            ("腰带多一个护符槽可以带防冻。", "POE2_CHARM_BELT_SLOTS"),
            ("Check https://poe2db.tw/us/Skill_Gems for details.", "URL_POE2_POE2DB"),
            ("升华选择风暴编织者打电弧。", "POE2_CLASS_IN_POB1"),
            ("Put Against the Darkness in your jewel socket.", "POE2_TIMELESS_JEWEL_IN_POB1"),
            ("点异能魔力把能量护盾转化为最大魔力。", "POE2_EB_MANA_CONVERSION"),
            ("Eldritch Battery converts all Mana into ES.", "POE2_EB_MANA_CONVERSION"),
            ("这个构筑不能重复使用同一个辅助宝石。", "POE2_SINGLE_SUPPORT_LIMIT"),
        ]
        for text, expected_rule in samples:
            issues = scan_assistant_reply(text)
            rule_ids = [i.rule_id for i in issues]
            assert expected_rule in rule_ids, f"{text!r} -> {rule_ids}"
            assert all(isinstance(i, ReplyIssue) and i.category == "poe2" for i in issues if i.rule_id == expected_rule)

    def test_catches_legacy_removed_items(self) -> None:
        samples = [
            ("主技能推荐连觉醒·元素集中提升秒伤。", "PATCH_328_AWAKENED_REMOVED"),
            ("Link Awakened Elemental Focus for more damage.", "PATCH_328_AWAKENED_REMOVED"),
            ("光环用神圣祝福开迅捷爆发。", "PATCH_325_DIVINE_BLESSING_REMOVED"),
            ("近战记得放先祖战士长和先祖卫士增伤。", "PATCH_325_ANCESTRAL_TOTEMS_REMOVED"),
            ("头盔去跑迷宫附魔拿额外投射物。", "PATCH_323_LAB_ENCHANTS_REMOVED"),
            ("换一颗分歧品质的宝石提高范围。", "PATCH_323_ALT_QUALITY_REMOVED"),
            ("把熔岩护盾放左键瞬发或者连武装召唤。", "PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS"),
            ("刷图记得在虚空石上打觉醒六分仪。", "PATCH_324_SEXTANTS_REMOVED"),
        ]
        for text, expected_rule in samples:
            issues = scan_assistant_reply(text)
            assert any(i.rule_id == expected_rule and i.category == "legacy_removed" for i in issues), (text, issues)

    def test_catches_exceptional_level_and_hard_fallacies(self) -> None:
        samples = [
            ("把卓越宝石升到20级伤害最高。", "EXCEPTIONAL_SUPPORT_LEVEL_CAP", "exceptional_level"),
            ("Level Greater Spell Echo to Lv5 for max DPS.", "EXCEPTIONAL_SUPPORT_LEVEL_CAP", "exceptional_level"),
            ("PoE1 服务器帧间隔是 33.33ms，按此计算断点。", "FALLACY_SERVER_TICK_33_33MS", "mechanic_fallacy"),
            ("吟唱时施放堆 14% 冷却回复速度可以提速。", "FALLACY_CWC_SCALES_WITH_CDR", "mechanic_fallacy"),
            ("Stack CDR on Cast while Channelling to trigger faster.", "FALLACY_CWC_SCALES_WITH_CDR", "mechanic_fallacy"),
            ("酋长点了 Ramako 建议挂易燃和火焰曝露减火抗提高点燃伤害。", "FALLACY_RAMAKO_RES_LOWERING", "mechanic_fallacy"),
            ("寒霜爆会占用一个诅咒上限。", "FALLACY_FROST_BOMB_AS_CURSE", "mechanic_fallacy"),
        ]
        for text, expected_rule, expected_cat in samples:
            issues = scan_assistant_reply(text)
            assert any(i.rule_id == expected_rule and i.category == expected_cat for i in issues), (text, issues)

    def test_zero_false_positives_on_legal_poe1_330_terms(self) -> None:
        legal_reply = (
            "圣宗(Hierophant)开憎恨(Hatred)配启迪(Inspiration)打冰伤。\n"
            "副手拿精魂之盾(Spirit Shield)，召唤怒灵(Summon Raging Spirit)配灵魂奉献(Spirit Offering)。\n"
            "主技能连元素集中(Elemental Focus)与觉醒赋予(Awakened Empower)和觉醒启蒙。\n"
            "防御面考虑艾许之面(Esh's Visage)在非低血时防混沌穿透，酋长点了雷默科(Ramako)。\n"
            "异能魔力(Eldritch Battery)用护盾支付蓝耗，卓越辅助自然上限3级、瓦尔4级。\n"
            "药水可用 Distilled Perfection，图鉴可用 Ancient Waystones。\n"
        )
        assert scan_assistant_reply(legal_reply) == []

    def test_blockquote_and_negation_debunking_exemptions(self) -> None:
        debunk_reply = (
            "> 用户原话：我能不能连觉醒·元素集中和神圣祝福，再堆点精魂？\n"
            "3.28 已移除觉醒·元素集中，请改用普通元素集中。\n"
            "PoE1 没有精魂机制，也不存在涂灌。\n"
            "PoE1 允许重复使用相同辅助宝石，没有单辅助限制。\n"
            "PoE1 服务器帧是 33ms 而不是 33.33ms。\n"
            "CwC 不吃冷却回复(CDR)，堆 CDR 无效。\n"
            "Ramako 站立火 DoT 抗性改为固定 -20%，易燃和曝露无效。\n"
            "寒霜爆不是诅咒，不占诅咒上限。\n"
            "Divine Blessing was removed in 3.25 and is no longer available.\n"
        )
        assert scan_assistant_reply(debunk_reply) == []

    def test_ds41f_redteam_zero_fp_and_mixed_cn_en_exceptional_catch(self) -> None:
        # All 7 real DS V4.1 Flash compliant/debunk lines that previously caused false positives
        real_compliant_lines = (
            "不同主技能对「元素集中封异常」的容忍度差别很大。\n"
            "## 二、PoE1 有精魂机制吗\n"
            "**没有。**\n"
            "本地 DB 里这批宝石全部带 `legacy_removed_328 = true`（Awakened Fork、Chain、Brutality）。\n"
            "**1) 33.33ms 是错的**，虽然误差看起来只有 1%，但会让取整结果偏移：\n"
            "- 神圣祝福那条路已经没了，别在 PoB 里挂 legacy 宝石自欺欺人。\n"
            "## 4) CwC 提速：14% CDR = 0 收益\n"
            "| 把「卓越辅助」升到 20 级 | 卓越辅助（Exceptional）**天生上限 3 级，腐化 4 级**，永远到不了 20 级 |\n"
            "② Standard 老区仓库里已有的觉醒石仍然存在（legacy 物品），但新赛季无法再获取。\n"
            "3.28 后只剩 觉醒·赋能/启蒙/增强（Empower、Enhance、Enlighten）。\n"
            "**3. 神圣祝福 + 憎恨：两个都已失效**\n"
            "**4. “精魂”是 PoE2 的资源**\n"
            "精魂 / Spirit 是 **PoE2** 的保留池。\n"
            "**5. CwC 与 CDR：这是机制误解**\n"
            "**2) 33.33ms 会算错的实例**\n"
            "**2）用 1/30 秒换算会出错的实例**\n"
            "若按 33.33ms 则算成 3t=100ms（10/s）→ **凭空多出 32% DPS**。\n"
            "若按 1/30 秒换算则得 3t = 100ms（10/s）→ **凭空多出约 32% DPS**。\n"
            "例外：觉醒·赋予、觉醒·增幅、觉醒·启蒙 仍存在（卓越石形态），自然上限 Lv4、腐化 Lv5。\n"
            "老觉醒石在永久区属于遗产（legacy），赛季区既买不到也刷不到。\n"
            "以及 冷却回复（对 CwC 无用）。\n"
            "如果按 33.33ms 计算，刚好是 3.00，这就错了。\n"
            "⚠️ 卓越/强辅别按 Lv20 规划。\n"
            "只能靠宝石等级（强辅 0.25s，但强辅同样已是遗产）改变。\n"
            "得靠启迪/减耗或偷蓝，别在胸甲上找“精魂”。\n"
            "| 项 | 正确（33ms） | 错误（33.33ms） | 偏差 |\n"
            "| 项 | 正确（33ms tick） | 按 1/30 s 算 |\n"
            "- 按 33.33ms 算出来的假阈值：CoC 4t 只要 12.5%。\n"
            "- 按 1/30 s 反推的假阈值：分别是 12.5% 和 15.4%。\n"
        )
        assert scan_assistant_reply(real_compliant_lines) == []

        # Catch Mainland CN slang prefix "强辅<removed gem>" (R4 Round 3 leak)
        assert any(
            i.rule_id == "PATCH_328_AWAKENED_REMOVED"
            for i in scan_assistant_reply("堆 52% CDR 靠强辅暴击时施放 Lv20 自带 52% 即可。")
        )

        # Still catch Lv6+/Lv20 even when mentioning Empower/Enhance/Enlighten
        assert any(
            i.rule_id == "EXCEPTIONAL_SUPPORT_LEVEL_CAP"
            for i in scan_assistant_reply("把卓越辅助觉醒赋予升到 20 级收益最高。")
        )

        # Real R4 Draft #2 leak: pivot clause after "，所以" with mixed-language "Exceptional 辅助（...Lv20）"
        r4_leak = (
            "3.28 之后**普通觉醒辅助已被移除**（仅 Awakened Empower / Enhance / Enlighten 保留到 Lv4，腐化 Lv5），"
            "所以堆 CDR 只能靠 3.28+ 的 **Exceptional 辅助（如 Cooldown Recovery，Lv20 给 +30% 冷却回复速度）**。"
        )
        issues = scan_assistant_reply(r4_leak)
        assert any(i.rule_id == "EXCEPTIONAL_SUPPORT_LEVEL_CAP" for i in issues), issues


class TestReplyScannerV2V3V4:
    def test_extract_claimed_open_affix_slots(self) -> None:
        text = (
            "你的 1号戒指 还有空后缀可以直接工艺台补火抗。\n"
            "Your Gloves have an open prefix for life craft.\n"
            "头盔不确定有没有空后缀，先检查一下再决定。\n"
            "If Ring 2 has an open suffix, craft chaos res.\n"
            "鞋子已满词缀，没有空后缀。\n"
        )
        slots = extract_claimed_open_affix_slots(text)
        assert [s for s, _ in slots] == ["Ring 1", "Gloves"]

    def test_detect_panel_or_delta_claims(self) -> None:
        assert detect_panel_or_delta_claims("30% more 和 20% more 叠乘等于 56% more。") == []
        claims = detect_panel_or_delta_claims("当前构筑 12.5M DPS，EHP 约 8.4万，换装后 DPS +18.5%。")
        assert len(claims) >= 2
        zh_claims = detect_panel_or_delta_claims("换完装备后 DPS 提升 18%，EHP 提高 25%，秒伤降低 10%。")
        assert len(zh_claims) == 3

    def test_detect_trigger_breakpoint_claims(self) -> None:
        assert detect_trigger_breakpoint_claims("玩 CoC 注意攻速别超过触发冷却上限。") == []
        hits = detect_trigger_breakpoint_claims("CoC 在 14% CDR 档位下攻速上限为 7.57 APS。")
        assert len(hits) == 1
        assert "CoC" in hits[0]

    def test_detect_build_source_in_text(self) -> None:
        assert detect_build_source_in_text("帮我看下 @C:/pob/build.xml 的防御") == (True, "@C:/pob/build.xml")
        assert detect_build_source_in_text("看看 https://pobb.in/abc123XYZ 这个构筑") == (
            True,
            "https://pobb.in/abc123XYZ",
        )
        assert detect_build_source_in_text("https://poe.ninja/poe1/builds/foo/bar") == (
            True,
            "https://poe.ninja/poe1/builds/foo/bar",
        )
        b64 = "eN" + "A" * 90
        assert detect_build_source_in_text(f"导出码：{b64}") == (True, None)
        assert detect_build_source_in_text("请问正火怎么堆抗性？") == (False, None)


class TestTurnReceiptsL1:
    def test_classify_cli_args_matrix(self) -> None:
        c_diag = classify_cli_args(["--offline", "diagnose", "@build.xml", "--target", "Alt1"])
        assert c_diag["shouldRecord"] is True
        assert c_diag["isCompute"] is True
        assert c_diag["hasTickCheck"] is True
        assert c_diag["slot"] == "alt1"
        assert c_diag["category"] == "diagnose"

        # A proxy password must not reach the ledger file, in either option spelling.
        for proxy_args in (
            ["--proxy", "socks5://me:s3cret@10.0.0.1:1080"],
            ["--proxy=http://me:s3cret@host"],
            ["--proxy", "http://me@example.com:s3cret@host"],
            ["--proxy", "http://me:s3cret phrase@host"],
        ):
            c_proxy = classify_cli_args([*proxy_args, "price", "Mageblood"])
            assert "s3cret" not in c_proxy["cmd"] and "://***@" in c_proxy["cmd"]
            assert "phrase" not in c_proxy["cmd"] and "example.com" not in c_proxy["cmd"]
            plain = ["wiki", "https://example.test?email=a@b.test"]
            assert classify_cli_args(plain)["cmd"] == " ".join(plain)
            assert c_proxy["cmd"].endswith("price Mageblood")
        # ... nor through any other field, when the URL lands where a command word belongs.
        assert "s3cret" not in json.dumps(classify_cli_args(["trade", "http://me:s3cret@host"]))

        c_status = classify_cli_args(["status"])
        assert c_status["shouldRecord"] is True
        assert c_status["isCompute"] is False
        assert c_status["hasTickCheck"] is False

        for cmd in (["weights", "--preset", "jewels"], ["respec"], ["rank-nodes", "--refund"]):
            assert classify_cli_args(cmd)["isCompute"] is True, cmd

        c_inspect = classify_cli_args(["item", "inspect", "--slot", "左戒指"])
        assert c_inspect["isCompute"] is False
        assert c_inspect["inspectedSlots"] == ["Ring 1"]
        assert c_inspect["category"] == "inspect_slot"

        c_items_all = classify_cli_args(["items", "--all"])
        assert c_items_all["inspectedSlots"] == ["*"]

        c_trade_on = classify_cli_args(["trade", "search", "--slot", "ring2", "--bench-craft", "fire"])
        assert c_trade_on["isCompute"] is True
        assert c_trade_on["inspectedSlots"] == ["Ring 2"]

        c_trade_off = classify_cli_args(["trade", "search", "--pob", "off", "--slot", "ring2"])
        assert c_trade_off["isCompute"] is False

        c_db_equip = classify_cli_args(["item", "db", "Mageblood", "--equip"])
        assert c_db_equip["isCompute"] is True
        assert classify_cli_args(["item", "db", "Mageblood"])["isCompute"] is False

        c_tick = classify_cli_args(["data", "tick", "0.15", "--cdr", "14"])
        assert c_tick["hasTickCheck"] is True
        assert c_tick["isCompute"] is False

        assert classify_cli_args(["hook", "verify-stop"])["shouldRecord"] is False
        assert classify_cli_args(["serve"])["shouldRecord"] is False
        assert classify_cli_args(["diagnose", "--help"])["shouldRecord"] is False

    def test_receipt_context_daemon_hint_and_summary(self, tmp_path: Path) -> None:
        t0 = time.time() - 1.0
        with receipt_context(["item", "edit", "--slot", "Ring 1", "--apply"], runtime_dir=tmp_path):
            record_daemon_receipt_hint("item_edit", {"target": "Main", "apply": True}, {"rev": 4, "applied": True})

        with receipt_context(["summary", "--target", "main"], runtime_dir=tmp_path):
            record_daemon_receipt_hint("summary", {"target": "main"}, {"rev": 4, "slot": "main"})

        with receipt_context(["item", "inspect", "--slot", "ring1"], runtime_dir=tmp_path):
            record_daemon_receipt_hint("items", {"target": "main", "slot": "ring1"}, {"rev": 4})

        with pytest.raises(SystemExit), receipt_context(["diagnose", "@bad.xml"], runtime_dir=tmp_path):
            raise SystemExit(2)

        receipts = read_turn_receipts(t0, runtime_dir=tmp_path)
        assert len(receipts) == 4
        summary = summarize_turn_receipts(receipts)
        assert summary["hasCompute"] is True
        assert summary["hasDiagnose"] is False  # exitCode=2 ignored
        assert summary["hasTickCheck"] is False
        assert summary["inspectedSlots"] == ["Ring 1"]
        assert summary["maxAppliedRevBySlot"] == {"main": 4}
        assert summary["maxComputeRevBySlot"] == {"main": 4}
        assert summary["lastSlot"] == "main"
        assert summary["count"] == 4

    def test_disable_env_and_truncation_and_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POB_HOOK_DISABLE_RECEIPTS", "1")
        with receipt_context(["summary"], runtime_dir=tmp_path):
            pass
        assert not (tmp_path / RECEIPTS_FILENAME).exists()

        monkeypatch.delenv("POB_HOOK_DISABLE_RECEIPTS", raising=False)
        for i in range(MAX_RECEIPT_LINES + 15):
            append_receipt({"ts": float(i), "cmd": f"summary {i}", "exitCode": 0}, runtime_dir=tmp_path)
        lines = [ln for ln in (tmp_path / RECEIPTS_FILENAME).read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == MAX_RECEIPT_LINES
        assert json.loads(lines[0])["cmd"] == "summary 15"

        # Fail-open when runtime_dir is unwritable (point to a regular file)
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        with receipt_context(["summary"], runtime_dir=blocker):
            pass

    def test_turn_state_roundtrip_and_session_hash(self, tmp_path: Path) -> None:
        h1 = session_hash("sess-abc")
        assert len(h1) == 16
        assert h1 == session_hash("sess-abc")
        assert load_turn_state("sess-abc", runtime_dir=tmp_path) is None

        state = {"sessionId": "sess-abc", "turnKey": "t1", "turnStartTs": 1234.5, "stopRetries": 0}
        save_turn_state("sess-abc", state, runtime_dir=tmp_path)
        assert load_turn_state("sess-abc", runtime_dir=tmp_path) == state

    def test_click_exit_pob_slot_allraw_and_fresh_diagnose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeClickExit(RuntimeError):
            def __init__(self, exit_code: int) -> None:
                super().__init__()
                self.exit_code = exit_code

        monkeypatch.setenv("POB_SLOT", "LeagueStart")
        assert classify_cli_args(["summary"])["slot"] == "leaguestart"
        assert classify_cli_args(["summary", "--slot", "Bossing"])["slot"] == "bossing"
        assert classify_cli_args(["diagnose", "--slot", "Alt2"])["slot"] == "alt2"

        t0 = time.time() - 1.0
        with pytest.raises(FakeClickExit), receipt_context(["diagnose", "@b.xml"], runtime_dir=tmp_path):
            raise FakeClickExit(0)
        with pytest.raises(FakeClickExit), receipt_context(["summary"], runtime_dir=tmp_path):
            raise FakeClickExit(5)
        with receipt_context(["items"], runtime_dir=tmp_path):
            record_daemon_receipt_hint("items", {"allRaw": True}, {"rev": 2})

        r1 = read_turn_receipts(t0, runtime_dir=tmp_path)
        assert [r["exitCode"] for r in r1] == [0, 5, 0]
        s1 = summarize_turn_receipts(r1)
        assert s1["hasDiagnose"] is True
        assert s1["hasFreshDiagnose"] is True
        assert s1["inspectedSlots"] == ["*"]

        # Applying a mutation or switching/loading resets hasFreshDiagnose
        with receipt_context(["item", "edit", "--slot", "Ring 1", "--apply"], runtime_dir=tmp_path):
            record_daemon_receipt_hint("item_edit", {"target": "leaguestart", "apply": True}, {"rev": 3, "applied": True})
        s2 = summarize_turn_receipts(read_turn_receipts(t0, runtime_dir=tmp_path))
        assert s2["hasDiagnose"] is True
        assert s2["hasFreshDiagnose"] is False

    def test_session_id_isolation_for_concurrent_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        t0 = time.time() - 1.0
        monkeypatch.setenv("POB_HOOK_SESSION_ID", "sess-A")
        with receipt_context(["diagnose", "@a.xml"], runtime_dir=tmp_path):
            pass
        monkeypatch.setenv("POB_HOOK_SESSION_ID", "sess-B")
        with receipt_context(["status"], runtime_dir=tmp_path):
            pass
        monkeypatch.delenv("POB_HOOK_SESSION_ID", raising=False)
        with receipt_context(["data", "tick", "0.15"], runtime_dir=tmp_path):
            pass

        rec_a = read_turn_receipts(t0, session_id="sess-A", runtime_dir=tmp_path)
        rec_b = read_turn_receipts(t0, session_id="sess-B", runtime_dir=tmp_path)
        # Session A sees its own diagnose + untagged CLI tick, never Session B's status
        assert [r["subcommand"] for r in rec_a] == ["diagnose", "data tick"]
        # Session B does NOT inherit Session A's diagnose
        assert [r["subcommand"] for r in rec_b] == ["status", "data tick"]
        assert summarize_turn_receipts(rec_b)["hasCompute"] is False
