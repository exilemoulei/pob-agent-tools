# `pob trade upgrade` 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目標：** 新增 `pob trade upgrade --budget N`，一條命令輸出「預算內最佳升級組合＋每 chaos 效益排行＋零 chaos 項」的參考清單，每項都經 PoB 實算。

**架構：** 純邏輯（挑最弱 slot、組裝結果）放在 L2 的 `trade_eval/upgrade.py`；L4 的 `cli_trade/upgrade_cmd.py` 負責呼叫 daemon（`items`、`item_unequip`、`respec`、`rank_gems`），並以 `ctx.invoke(cmd_trade_search, …)` 逐 slot 重用既有的交易搜尋，最後交給 `trade_eval.budget.plan_budget`。不複製 `trade search` 流程。

**技術：** Python 3.11+、click、pytest（離線、fake daemon）。

**與規格的差異（實作者決定）：** 規格建議把 `cmd_trade_search` 拆成可呼叫函式；該函式與 click 綁很深（`ctx.exit`、直接輸出），拆開風險大。改用 `ctx.invoke` 加 `--json --out <暫存檔>` 重用，`trade search` 程式碼零改動，同樣滿足「不複製流程」。

**規格：** `docs/superpowers/specs/2026-11-25-pob-upgrade-design.md`

## 全域限制

- 命令名稱：`pob trade upgrade`（`trade_group` 子命令）。
- 選項與預設：`--budget`（必填，chaos）、`--slot`（可重複）、`--top-slots 3`、`--per-slot 5`、`--score` 預設 `dps=1,ehp=0.25`、`--league`、`--proxy`、`--target`、`--json`、`--compact`、`--out`。
- 主要 slot（PoB 名稱 → trade `--slot` 名稱）：`Weapon 1→weapon1`、`Body Armour→body_armour`、`Helmet→helmet`、`Gloves→gloves`、`Boots→boots`、`Amulet→amulet`、`Ring 1→ring1`、`Ring 2→ring2`、`Belt→belt`。
- 零 chaos 項：`respec` 用 `rounds=1, max_dist=5`；`rank_gems` 用 `group="main", field="CombinedDPS", optimize=True, passes=1, include_legacy=False`。
- 不提供 `--apply`；所有探針皆 what-if（rollback）。
- `--json` 欄位英文；結束碼：參數錯誤 `1`，daemon／引擎故障沿用 `invoke_daemon` 既有行為（`5`）。
- 層級：`trade_eval/upgrade.py` 只 import L0–L2；檔案 ≤650 行／24,000 bytes，函式 ≤200 行。
- PoE1 only；cookie 只在環境變數。

## Review Focus

1. **某個 slot 的交易搜尋失敗**（限速 `RateLimited`、無結果、auto-query 失敗、`ctx.exit(N)`）：其餘 slot 照常，失敗者列入 `skippedSlots` 並附原因，整體結束碼 `0`。→ Task 2 測試 `test_upgrade_skips_failed_slot`。
2. **空 slot**（`selItemId` 為空）：不做卸下探針，視為最弱、排在最前，理由 `empty`。→ Task 1 測試 `test_rank_weak_slots_empty_first`。
3. **零 chaos 項 daemon 出錯**（`respec`／`rank_gems` 拋 `click.ClickException`）：該項改為 `{"error": "..."}`，交易部分照常輸出。→ Task 2 測試 `test_upgrade_free_move_error_is_reported`。
4. **`--budget 0` 或負數**：0 時 picks 為空但仍輸出；負數結束碼 `1`。→ Task 2 測試 `test_upgrade_budget_zero_and_negative`。
5. **使用者給 `--slot`**：不做自動挑選、不呼叫 `item_unequip`；不認得的 slot 名稱結束碼 `1`。→ Task 2 測試 `test_upgrade_explicit_slots_skip_probe`。

---

### Task 1：挑最弱 slot 的純邏輯

**Files:**
- Create: `pob_cli/src/pob_cli/trade_eval/upgrade.py`（`arch_lint.LAYER_MAP` 已以套件為單位列 `trade_eval: 2`，不需改）
- Test: `pob_cli/tests/test_trade_upgrade.py`

**Interfaces:**
- Produces:
  - `UPGRADE_SLOTS: dict[str, str]`：上方主要 slot 對照表（PoB 名稱 → trade 名稱），保持上列順序。
  - `unequip_loss(res: dict[str, Any], score: dict[str, float]) -> float | None`：`res` 為 `item_unequip` what-if 結果（含 `before`／`after`，鍵 `primaryDps`、`totalEhp`）；`score` 鍵為 `dps`／`ehp`，分別對應 `primaryDps`／`totalEhp`。回傳 `-weighted_score(pcts, score)`（掉分為正）；任一需要的值缺漏回傳 `None`。用 `pob_cli.scoring.pct_change` 與 `weighted_score`。
  - `rank_weak_slots(items_res: dict[str, Any], losses: dict[str, float | None], top: int) -> list[dict[str, Any]]`：`items_res` 為 daemon `items` 結果（`slots` 內每項有 `name`、`selItemId`、`inactive`）。只看 `UPGRADE_SLOTS` 內且非 `inactive` 的 slot。回傳 `[{"slot": <PoB 名稱>, "tradeSlot": <trade 名稱>, "reason": "empty" | "lowest contribution", "loss": float | None}]`，空 slot 先、其餘依 `loss` 由小到大，`loss is None` 排最後，取前 `top` 個。

- [ ] **Step 1：寫失敗測試**

```python
def test_unequip_loss_weights_dps_and_ehp() -> None:
    res = {"before": {"primaryDps": 100.0, "totalEhp": 1000.0}, "after": {"primaryDps": 90.0, "totalEhp": 800.0}}
    assert unequip_loss(res, {"dps": 1.0, "ehp": 0.25}) == pytest.approx(10.0 + 0.25 * 20.0)
    assert unequip_loss({"before": {}, "after": {}}, {"dps": 1.0}) is None

def test_rank_weak_slots_empty_first() -> None:
    items = {"slots": [
        {"name": "Helmet", "selItemId": 3}, {"name": "Boots", "selItemId": 0},
        {"name": "Amulet", "selItemId": 5}, {"name": "Weapon 2", "selItemId": 9},
        {"name": "Belt", "selItemId": 7, "inactive": True},
    ]}
    ranked = rank_weak_slots(items, {"Helmet": 4.0, "Amulet": 1.5}, top=2)
    assert [(r["slot"], r["tradeSlot"], r["reason"]) for r in ranked] == [
        ("Boots", "boots", "empty"), ("Amulet", "amulet", "lowest contribution")]

def test_rank_weak_slots_unknown_loss_last() -> None:
    items = {"slots": [{"name": "Helmet", "selItemId": 3}, {"name": "Gloves", "selItemId": 4}]}
    assert [r["slot"] for r in rank_weak_slots(items, {"Helmet": None, "Gloves": 2.0}, top=3)] == ["Gloves", "Helmet"]
```

- [ ] **Step 2：執行確認失敗**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py -q`
Expected: FAIL（`ModuleNotFoundError: pob_cli.trade_eval.upgrade`）

- [ ] **Step 3：實作 `trade_eval/upgrade.py` 的三個介面**（空 slot 判斷：`not s.get("selItemId")`，與 `analyzer/audit.py:433` 一致）

- [ ] **Step 4：執行確認通過**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py -q` → PASS；`python pob_cli/scripts/check.py --fast` → 0 錯誤

- [ ] **Step 5：Commit**

```bash
git add pob_cli/src/pob_cli/trade_eval/upgrade.py pob_cli/tests/test_trade_upgrade.py
git commit -m "feat(trade): rank the weakest gear slots for pob trade upgrade"
```

---

### Task 2：`pob trade upgrade` 命令（JSON 輸出）

**Files:**
- Create: `pob_cli/src/pob_cli/cli_trade/upgrade_cmd.py`
- Modify: `pob_cli/src/pob_cli/cli_trade/__init__.py`（`from .upgrade_cmd import cmd_trade_upgrade`，並加入 `__all__`）
- Modify: `pob_cli/src/pob_cli/turn_receipts.py`（`_COMPUTE_PAIR_CMDS` 加 `("trade", "upgrade")`）
- Test: `pob_cli/tests/test_trade_upgrade.py`、`pob_cli/tests/test_hook_scanner_receipts.py`

**Interfaces:**
- Consumes: Task 1 的 `UPGRADE_SLOTS`、`unequip_loss`、`rank_weak_slots`；`trade_eval.budget.plan_budget(payloads, budget=, score=, top=)`；`cli_common.invoke_daemon`；`cli_trade.search_cmd.cmd_trade_search`。
- Produces:
  - `_search_slot(ctx: click.Context, trade_slot: str, *, per_slot: int, league: str | None, proxy: str | None, target: str) -> dict[str, Any]`：以 `ctx.invoke(cmd_trade_search, slot=trade_slot, auto_query=True, pob_mode="on", pob_limit=per_slot, league=league, proxy=proxy, target=target, as_json=True, out_path=<暫存檔>)` 執行，讀回 JSON。`click.exceptions.Exit`（非 0）、`click.ClickException`、暫存檔不存在或無 `candidates` 一律拋 `RuntimeError(<原因>)`。暫存檔放 `tempfile.TemporaryDirectory()`。測試以 monkeypatch 取代此函式。
  - `cmd_trade_upgrade`：`@trade_group.command("upgrade")`，選項見全域限制。`--json` 輸出：
    `{"budget", "score", "slots": [rank_weak_slots 的項目，或 --slot 時 {"slot","tradeSlot","reason":"requested","loss":None}], "plan": <plan_budget 結果>, "free": {"respec": <結果或 {"error"}>, "gems": <結果或 {"error"}>}, "skippedSlots": [{"slot": <trade 名稱>, "error": str}]}`
  - 執行順序：參數驗證（`--budget` 負數、`--score` 只接受 `dps`／`ehp`、未知 `--slot` → `click.BadParameter`，結束碼 1）→ 零 chaos 項 → 挑 slot（`invoke_daemon("items", {"target"})` 後逐 slot `invoke_daemon("item_unequip", {"target", "slot", "apply": False})`；空 slot 不探）→ 逐 slot `_search_slot` → `plan_budget`。
  - `--slot` 接受 trade 名稱（如 `ring1`）或 PoB 名稱（如 `Ring 1`），以 `UPGRADE_SLOTS` 正反查。
  - 零 chaos 項的 `respec` score 參數：把 `dps`→`primaryDps`、`ehp`→`totalEhp` 同權重轉換後傳入；`timeout=900.0`。`rank_gems` `timeout=600.0`。

- [ ] **Step 1：寫失敗測試**（`_fake_call` 依 action 回傳：`items`→三 slot；`item_unequip`→固定 before/after；`respec`→`{"rounds": [], "score": {...}}`；`rank_gems`→`{"swaps": [], "groupIndex": 1}`；monkeypatch `pob_cli.cli._daemon_call` 與 `upgrade_cmd._search_slot`。`_patch(monkeypatch, search=..., respec_error=None)` 回傳被搜尋的 trade slot 清單，並帶 `.actions` 記錄 daemon action；`search` 值為 payload 或 `RuntimeError` 時由 fake 拋出。`_AMU`／`_HELM`／`_RING` 與 `test_trade_plan.py` 的 `_AMU` 同形（`{"slot", "candidates": [{"name","price":{"chaos"},"pob":{"dps_pct","ehp_pct"},"url"}]}`）。`_run` 用 `CliRunner().invoke(main, args)`）

```python
def test_upgrade_json_end_to_end(monkeypatch, tmp_path) -> None:
    searched = _patch(monkeypatch, search={"amulet": _AMU, "helmet": _HELM})
    out = _run(["trade", "upgrade", "--budget", "100", "--top-slots", "2", "--json"])
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert [s["tradeSlot"] for s in data["slots"]] == searched
    assert data["plan"]["picks"] and data["skippedSlots"] == []
    assert set(data["free"]) == {"respec", "gems"}

def test_upgrade_skips_failed_slot(monkeypatch) -> None:
    _patch(monkeypatch, search={"amulet": _AMU, "helmet": RuntimeError("rate limited")})
    data = json.loads(_run(["trade", "upgrade", "--budget", "100", "--top-slots", "2", "--json"]).output)
    assert data["skippedSlots"] == [{"slot": "helmet", "error": "rate limited"}]
    assert [p["slot"] for p in data["plan"]["picks"]] == ["amulet"]

def test_upgrade_free_move_error_is_reported(monkeypatch) -> None:
    _patch(monkeypatch, search={"amulet": _AMU}, respec_error="engine busy")
    data = json.loads(_run(["trade", "upgrade", "--budget", "50", "--slot", "amulet", "--json"]).output)
    assert "engine busy" in data["free"]["respec"]["error"] and data["plan"]["picks"]

def test_upgrade_budget_zero_and_negative(monkeypatch) -> None:
    _patch(monkeypatch, search={"amulet": _AMU})
    assert json.loads(_run(["trade", "upgrade", "--budget", "0", "--slot", "amulet", "--json"]).output)["plan"]["picks"] == []
    assert _run(["trade", "upgrade", "--budget", "-5", "--slot", "amulet"]).exit_code == 1

def test_upgrade_explicit_slots_skip_probe(monkeypatch) -> None:
    calls = _patch(monkeypatch, search={"ring1": _RING})
    data = json.loads(_run(["trade", "upgrade", "--budget", "50", "--slot", "Ring 1", "--json"]).output)
    assert data["slots"][0]["reason"] == "requested" and "item_unequip" not in calls.actions
    assert _run(["trade", "upgrade", "--budget", "50", "--slot", "flask9"]).exit_code == 1
```

`test_hook_scanner_receipts.py` 加：`assert classify_cli_args(["trade", "upgrade", "--budget", "10"])["isCompute"] is True`

- [ ] **Step 2：執行確認失敗**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py pob_cli/tests/test_hook_scanner_receipts.py -q`
Expected: FAIL（`No such command 'upgrade'`）

- [ ] **Step 3：實作 `upgrade_cmd.py` 與註冊、receipts 一行**

- [ ] **Step 4：執行確認通過**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py pob_cli/tests/test_hook_scanner_receipts.py pob_cli/tests/test_trade_plan.py -q` → PASS；`python pob_cli/scripts/check.py --fast` → 0 錯誤

- [ ] **Step 5：Commit**

```bash
git add pob_cli/src/pob_cli/cli_trade/ pob_cli/src/pob_cli/turn_receipts.py pob_cli/tests/test_trade_upgrade.py pob_cli/tests/test_hook_scanner_receipts.py
git commit -m "feat(trade): add pob trade upgrade budget planner"
```

---

### Task 3：Markdown 輸出（重用既有格式化）

**Files:**
- Modify: `pob_cli/src/pob_cli/cli_trade/plan_cmd.py`（把 `cmd_trade_plan` 內 picks／bestPerChaos 表格那段抽成 `format_plan_markdown(res: dict[str, Any], *, budget: float) -> str`，`cmd_trade_plan` 改呼叫它，輸出逐字不變）
- Modify: `pob_cli/src/pob_cli/cli_trade/upgrade_cmd.py`
- Test: `pob_cli/tests/test_trade_upgrade.py`

**Interfaces:**
- Consumes: Task 2 的結果 dict；`cli_rank_nodes.format_respec_markdown(res, target=)`；`cli_optimize._format_link_plan(res)`。
- Produces: `format_upgrade_markdown(res: dict[str, Any], *, target: str) -> str`，段落順序：
  1. `format_plan_markdown(res["plan"], budget=res["budget"])`
  2. `- Slots searched: Amulet (lowest contribution), Boots (empty)`
  3. 若有 `skippedSlots`：`- Skipped: helmet (rate limited)`
  4. `> Each listing was PoB-verified alone; the plan's total is an estimate.`
  5. `### Free moves`，接 `format_respec_markdown` 與 `_format_link_plan`；某項為 `{"error"}` 時改印 `- respec: <error>`／`- gems: <error>`。

- [ ] **Step 1：寫失敗測試**

```python
def test_upgrade_markdown_sections(monkeypatch) -> None:
    _patch(monkeypatch, search={"amulet": _AMU, "helmet": RuntimeError("rate limited")})
    text = _run(["trade", "upgrade", "--budget", "100", "--top-slots", "2"]).output
    assert "### Upgrade Plan" in text and "- Skipped: helmet (rate limited)" in text
    assert "is an estimate" in text and "### Free moves" in text and "### Respec Plan" in text
```

並確認 `test_trade_plan.py` 既有的文字輸出測試不需修改即通過。

- [ ] **Step 2：執行確認失敗**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py::test_upgrade_markdown_sections -q`
Expected: FAIL（缺 `### Free moves`）

- [ ] **Step 3：抽出 `format_plan_markdown`、實作 `format_upgrade_markdown`，非 `--json` 時用 `_emit_text` 輸出**

- [ ] **Step 4：執行確認通過**

Run: `python -m pytest pob_cli/tests/test_trade_upgrade.py pob_cli/tests/test_trade_plan.py -q` → PASS；`python pob_cli/scripts/check.py --fast` → 0 錯誤

- [ ] **Step 5：Commit**

```bash
git add pob_cli/src/pob_cli/cli_trade/plan_cmd.py pob_cli/src/pob_cli/cli_trade/upgrade_cmd.py pob_cli/tests/test_trade_upgrade.py
git commit -m "feat(trade): render pob trade upgrade as markdown"
```

---

### Task 4：文件與全量門檻

**Files:**
- Modify: `docs/guides/commands.md`（在 `pob trade plan` 那行之後加一行 `pob trade upgrade …`，含選項與預設、「參考用、加總為近似」註記）
- Modify: `docs/guides/trade.md`（新增一小節：用途、流程四步、耗時約 1–2 分鐘、限速時 slot 會被跳過）
- Modify: `skills/pob/SKILL.md`（`pob trade plan` 那行之前加 `pob trade upgrade --budget 100   # one-shot: weakest slots -> live trade -> plan + free moves`）
- README：兩份 README 沒有逐命令清單，不改（AGENTS.md 要求只在觸及其內容時同步）。

- [ ] **Step 1：更新上述三份文件**

- [ ] **Step 2：跑全量門檻**

Run: `python pob_cli/scripts/check.py`
Expected: ruff／arch 0 錯誤，pytest 通過數比基準多出本計畫新增的測試數，skip 數不變（印出的 INFO 行符合本機基準）

- [ ] **Step 3：Commit**

```bash
git add docs/guides/commands.md docs/guides/trade.md skills/pob/SKILL.md
git commit -m "docs(trade): document pob trade upgrade"
```

- [ ] **Step 4：（需網路與引擎，手動）實機冒煙**

Run: `pob load <build code>` 後 `pob trade upgrade --budget 50`
Expected: 1–3 分鐘內輸出 `### Upgrade Plan` 與 `### Free moves`；之後 `pob summary` 數值與執行前相同（無殘留）。把實際耗時記在 PR 描述。
