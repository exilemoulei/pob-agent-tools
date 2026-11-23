# `pob4agents` Hook 硬护栏与防「假执行」施工方案 (`docs/HOOK_GUARDRAILS_PLAN.md`)

> **当前状态**：已完成并经 4 轮实机验收闭环（Completed & Verified）—— 三阶段施工、4 轮 DeepSeek V4.1 Flash 红队实测闭环（R1–R5 安全硬防线全过、R3 提速 -43.2%、R4 现场真拦强辅 CoC 幻觉并纠偏、R5 零误伤 PASS、并发 `POB_HOOK_SESSION_ID` 100% 隔离）、29/29 实测草稿离线回放（0 误伤 / 5/5 真幻觉命中）与全量门禁全绿：`check.py --fast`（1.44s, `ruff=0, arch=0`）、`check.py --self-test`（5/5 PASS）、全量 `check.py`（944 passed, 0 failed, 1 skipped）与 `verify-plugin.mjs`（`VERIFY_PLUGIN_OK`）。

---

## 1. 背景与核心问题：为什么快速模型会「假执行」？

终端用户主力环境包含大量 **DeepSeek Harness (`DSH`) + DeepSeek 4.1 Flash / Hydrogen** 等轻量快速模型。仅靠 `skills/pob/SKILL.md` 或 System Prompt 软约束，或仅靠简单的“检查本轮有没有调过 `pob`”布尔判断，弱模型在实战中会出现 4 类典型逃逸（假执行）：

1. **打卡式凑数调用**：若验收只看“是否调用过 CLI”，模型随便调一次 `pob status` 或无关的 `pob data`，骗过检查后继续手撕 XML 或凭预训练记忆编造 DPS。
2. **绕过 CLI 在最终正文裸奔幻觉（当前最大盲区）**：现有 `pob_cli/src/pob_cli/guardrails/` 只拦截 **CLI 入参**。若模型根本不查某颗辅助石，直接在最终回复正文里推荐“连觉醒·元素集中 / 把卓越宝石升到 20 级 / 开神圣祝福 / 堆精魂”，CLI 护栏完全无机会触发。
3. **局部细节盲猜**：只跑了宏观 `pob diagnose`，未执行 `pob item inspect --slot "Ring 1"` 就断言“1 号戒指还有空后缀可以工艺补抗性”。
4. **长对话旧面板串台**：本轮执行 `--apply` 修改构筑使槽位 `rev` 递增后，最终回复仍照抄上一轮旧快照面板。

### 设计铁律（Design Invariants）
- **零自证**：绝不询问模型“你是否已验证”，验收只认**模型不可篡改的进程外执行回执（Out-of-Band Receipt）+ 终稿文本确定性扫描**。
- **零 LLM 裁判**：不引入额外 LLM-as-a-Judge 二次评审（高延迟、费 Token、弱模型互评不可靠）；纯常规命中路径额外开销 **< 25ms**。
- **代跑塞真值优于骂它重跑**：一旦收尾拦截抓到漏查或违禁词，Hook 当场同步代跑对应的毫秒级查询（`ground_prompt` / 已启动 daemon 的 `items`+`item_inspect` / `build_server_tick_table` / `summary`），把真实输出直接塞进打回消息，让快速模型做最擅长的“看眼前真值重写结论”。**绝不在 Hook 中冷启动耗时十余秒的 daemon**（daemon 未运行时仅退化为确定性规则提示，严禁卡死宿主）。
- **单轮最多打回 1 次（`maxRetries = 1`）**：彻底杜绝无限续写死循环。
- **Fail-Open 容错**：回执写入失败、JSON 解析异常或子命令不可用时绝不破坏主 CLI 或宿主正常对话。

---

## 2. 5 层架构落点（严格对齐 `pob_cli/scripts/arch_lint.py`）

所有核心判定逻辑统一收敛在 `pob_cli` Python 内核中，各宿主（DSH、Antigravity、Claude Code、Cursor）只保留薄胶水，避免多处维护同一套规则。

> **强制体积红线（`arch_lint.py`）**：每个源码文件 **`<=650L` 且最坏 CRLF 字节 `<=24,000B`**，每个函数 **`<=200L`**（测试文件 `<=1000L / <=38,000B`），严格单向 DAG `L0 -> L1 -> L2 -> L3 -> L4`（含函数内懒导入）。

| 层级 | 模块路径 | 职责与依赖边界 |
|---|---|---|
| **L0** | `pob_cli/src/pob_cli/guardrails/reply_scanner.py`（及 `guardrails/__init__.py` 导出） | 终稿文本扫描器：复用 `rules_poe2.py`、`rules_legacy.py`、`rules_legacy_gems.py`、`rules_false_friends.py`、`build_gate.py`、`warnings.py`。内置**引用剥离 + 分句否定/辟谣语境豁免器**，严格区分“硬移除/跨代项”与“仅机制科普项”，外加硬机制谬误正则（`33.33ms`、`CwC` 吃 `CDR`、酋长 `Ramako` 站立火 DoT 减抗、`Frost Bomb` 当诅咒、卓越辅助 `Lv5+/20级`、PoE2 `EB`）。仅依赖 L0。 |
| **L1** | `pob_cli/src/pob_cli/turn_receipts.py` | 进程外执行回执账本与轮次状态读写（`<POB_RUNTIME_DIR>/turn_receipts.jsonl` 与 `hook_turn_<hash>.json`）：线程本地 `receipt_context()`、argv 命令分类、原子追加与滚动截断（`256KB` / 最近 `200` 条）、按 `turnStartTs` 聚合本轮算力/验视/帧率回执。注册 `LAYER_MAP["turn_receipts"] = 1`，仅依赖 L0 `guardrails` 与 L1 `updater.get_runtime_dir`。 |
| **L4** | `pob_cli/src/pob_cli/cli_hook/` 子包（`__init__.py`、`gates.py`、`remediate.py`、`protocols.py`） | 拆薄子包暴露 `pob hook <pre-prompt \| pre-tool \| verify-stop> [--protocol raw\|agy\|claude]`，读取 `stdin` UTF-8 JSON，输出紧凑 JSON 决策；Gate 3 不通过时在 `<=2.5s` 预算内调用 L0/L1/L2/L3 接口代跑缺失取证。注册 `LAYER_MAP["cli_hook"] = 4` 与 `cli.py` `_LAZY_SUBCOMMANDS["hook"]`（满足 `[spec-sync]` 与 `pob.spec` `collect_submodules("pob_cli")`）。 |
| **L4 埋点** | `pob_cli/src/pob_cli/cli_common.py`（`GuardrailGroup.invoke` + `invoke_daemon`） | 在 `GuardrailGroup.invoke` 统一包一层 `receipt_context(raw_args)` 并在 `finally` 落盘 1 行轻量回执（耗时 `<1ms`）；在 `invoke_daemon` 返回后把 `action`/`params`/`res`（`rev`、`target`、`applied`、`slot`）回填进当前 `receipt_context`。排除 `hook`/`serve`/`_daemon`/`--help`/`--version`，且支持 `POB_HOOK_DISABLE_RECEIPTS=1`。 |
| **宿主适配** | `<dsh>/plugins/dsh-pob-assistant/{hook-bridge.mjs,index.mjs,verify-plugin.mjs}` + `hooks/{hooks.json,claude-hooks.json}` | DSH 侧薄胶水接入 `agent/pre-step`、`tools/pre-execute`、`agent/turn-stopping`；跨宿主侧提供 Antigravity (`hooks/hooks.json`) 与 Claude Code (`hooks/claude-hooks.json`) 模板。 |

---

## 3. 进程外回执账本与模块接口契约

### 3.1 回执账本格式（`<POB_RUNTIME_DIR>/turn_receipts.jsonl`）

每次 `pob` 命令（排除 `hook`、`serve`、`_daemon`、`--help`、`--version`）结束时追加单行 JSON（单行紧凑、UTF-8、超过 `256KB` 或 `200` 条时保留最近 `200` 条原子替换）：

```json
{
  "ts": 1795372000.125,
  "cmd": "item inspect --slot Ring 1",
  "category": "inspect_slot",
  "isCompute": false,
  "hasTickCheck": false,
  "slot": "main",
  "rev": 3,
  "applied": false,
  "inspectedSlots": ["Ring 1"],
  "exitCode": 0
}
```

### 3.2 命令分类枚举（防凑数白名单）
- **`isCompute = true`（算力/引擎真值命令）**：
  - `diagnose`、`summary`、`audit`、`matrix`、`compare`、`calc`
  - `item try`、`item edit`、`item unequip`、`item db --equip`、`item flask`、`item anoint`
  - `skill set`、`skill add`、`skill rm`、`skill new-group`、`skill replace-group`
  - `tree change`、`tree set-class`、`config set`、`batch`、`sweep`、`borrow`
  - `rank-nodes`、`rank-gems`、`timeless search`、`trade search`（当 `--pob != off` 时）
- **`inspectedSlots` 写入规则**（经 L0 `normalize_equipment_slot` 归一化）：
  - `item inspect --slot <S>` / `items --slot <S>` / `trade search --slot <S> --bench-craft ...` -> `[normalize_equipment_slot(S)]`
  - `items --all`（含或不含 `--raw`，因 `cmd_items` 对 `all_items` 默认 `raw=True`） -> `["*"]`
- **`hasTickCheck = true`**：
  - `diagnose`（已内置全触发器 `0.033s` 帧率审计）或 `data tick`。
- **`applied = true` 与 `rev` 捕获**：
  - 从 `invoke_daemon` 返回的 `res` 中优先提取 `rev`（整数）与 `slot`/`target`（小写 slug），以及 `--apply` / `res.get("applied") == True` 标志。
- **明确不计入算力回执的命令**（防打卡假执行）：
  - `status`、`ground`、`data gem/unique/mod/base/translate/tree`、`price`、`meta`、`poedb`、`wiki`、`bench list/suggest`、`timeless list`、任何 `exitCode != 0` 的失败调用。

### 3.3 L0 / L1 公开 Python 契约（供 L4 `cli_hook/` 稳定调用）

#### L0 `guardrails/reply_scanner.py`
- `ReplyIssue`（frozen dataclass）：`rule_id: str`, `category: str` (`"poe2" | "legacy_removed" | "mechanic_fallacy" | "exceptional_level"`), `trigger: str`, `sentence: str`, `reason: str`, `remediation: str`, `ground_terms: tuple[str, ...] = ()`
- `scan_assistant_reply(text: str) -> list[ReplyIssue]`
- `extract_claimed_open_affix_slots(text: str) -> list[tuple[str, str]]` — 返回 `[(normalized_slot, sentence), ...]`（已豁免条件/不确定语态）
- `detect_panel_or_delta_claims(text: str) -> list[str]` — 返回匹配到的具体面板/模拟收益断言片段
- `detect_trigger_breakpoint_claims(text: str) -> list[str]` — 返回匹配到的具体触发器 APS/CDR 断点断言片段（含检测出的技能提示词）
- `detect_build_source_in_text(text: str) -> tuple[bool, str | None]` — 返回 `(has_build_source, local_file_or_source_hint)`

#### L1 `turn_receipts.py`
- `classify_cli_args(raw_args: Sequence[str]) -> dict[str, Any]` — 纯函数解析 argv，返回 `cmd`, `subcommand`, `category`, `isCompute`, `hasTickCheck`, `slot`, `applied`, `inspectedSlots`, `shouldRecord`
- `receipt_context(raw_args: Sequence[str], *, runtime_dir: Path | None = None)` — 上下文管理器；退出时自动落盘（捕获 `exitCode`，静默容错）
- `record_daemon_receipt_hint(action: str, params: dict[str, Any] | None, result: dict[str, Any] | None) -> None` — 将 daemon 返回的 `rev`、`slot`、`applied` 等写入当前线程上下文
- `append_receipt(entry: dict[str, Any], *, runtime_dir: Path | None = None) -> None`
- `read_turn_receipts(since_ts: float, *, runtime_dir: Path | None = None) -> list[dict[str, Any]]`
- `summarize_turn_receipts(receipts: Sequence[dict[str, Any]]) -> dict[str, Any]` — 聚合 `hasCompute`, `hasTickCheck`, `hasDiagnose`, `inspectedSlots`, `maxAppliedRevBySlot`, `maxComputeRevBySlot`, `lastSlot`, `count`
- `load_turn_state(session_id: str, *, runtime_dir: Path | None = None) -> dict[str, Any] | None`
- `save_turn_state(session_id: str, state: dict[str, Any], *, runtime_dir: Path | None = None) -> None`
- `session_hash(session_id: str) -> str` — 稳定 16 位 sha256 hex，用于 `hook_turn_<hash>.json`

---

## 4. 三道生命周期闸门详细规格

### Gate 1：`pob hook pre-prompt [--protocol raw|agy|claude]`（前置免调与游标锚定）
- **宿主挂载点**：DSH `agent/pre-step` / AGY `PreInvocation` / Claude Code `UserPromptSubmit`
- **canonical `raw` 输入（`stdin` JSON）**：`{"sessionId": "...", "turnId": "...", "userText": "...", "step": 1}`
- **多步游标稳定铁律（防 `PreInvocation` 误重置游标）**：
  - 计算 `turnKey = str(turnId)`（若有明确 `turnId`）否则 `sha1(userText.strip())[:16]`。
  - 读取已有 `hook_turn_<hash>.json`：若 `state["turnKey"] == turnKey`（同一轮的第 2+ 步再次进入 `PreInvocation`），**严禁重置 `turnStartTs` 或清零 `stopRetries`**，也不重复注入 `<pob-grounding>`，直接返回空注入放行。
  - 仅当 `turnKey` 变化（新的一轮开始）时：
    1. 写入 `hook_turn_<hash>.json`：`{"sessionId", "turnKey", "turnStartTs": now(), "hasBuildSource": bool, "buildSourceHint": str|None, "userText": userText[:2000], "stopRetries": 0}`。
    2. 离线调用 `ground_prompt(userText)`（`<15ms`，不启动 daemon）；若命中 3.30 实体卡或 PoE2/旧补丁陷阱词，返回紧凑 `<pob-grounding>` 注入文本。
- **canonical `raw` 输出**：`{"injectText": "<pob-grounding ...>..." | "", "turnKey": "...", "turnStartTs": 1795372000.12, "hasBuildSource": false, "reusedTurn": false}`

---

### Gate 2：`pob hook pre-tool [--protocol raw|agy|claude]`（调用前低效与违规动作硬拒止）
- **宿主挂载点**：DSH `tools/pre-execute` / AGY `PreToolUse` / Claude Code `PreToolUse`
- **canonical `raw` 输入（`stdin` JSON）**：`{"sessionId": "...", "toolName": "...", "commandLine": "...", "supportsOverwrite": false}`
- **硬拦截 / 重写规则**（仅针对 Shell 执行命令；非 Shell 工具或空命令一律 `allow`）：
  1. **禁止手撕引擎 Lua 源码**（`ruleId: "NO_RAW_ENGINE_LUA"`）：命令尝试用 `cat`/`Get-Content`/`type`/`rg`/`Select-String`/`findstr`/`python`/`node` 读取 `.runtime/engine/**/*.lua` 或 `PobTools*/**/*.lua` -> `deny`，提示：`请直接使用 pob data <gem|unique|mod|tree> 或 pob calc "<kw>"（0ms 索引查询）。`
  2. **禁止脚本手算构筑 XML（违反 `R5`）**（`ruleId: "NO_MANUAL_XML_CALC"`）：命令用 `python -c` / `python -` / `node -e` / `pwsh` XML DOM 解析构筑 `.xml` 计算词缀或面板 -> `deny`，提示：`严禁手算 XML（R5）；请直接执行 pob diagnose @build.xml。`
  3. **禁止跑 `--help` 浪费步数与上下文**（`ruleId: "NO_POB_HELP"`）：匹配 `\bpob(?:\.exe)?\b[^\n;|&]*\s(?:--help|-h)(?:\s|$)` -> `deny` 并返回 4 行核心命令速查签名。
  4. **禁止裸 `--json` 灌爆上下文**（`ruleId: "NO_BARE_JSON"`）：匹配 `\bpob(?:\.exe)?\b` 含 `--json` 且排除 `pob hook`、不含 `--out`、且未重定向至文件（`>`）-> 若 `supportsOverwrite == true`（如 AGY）返回 `decision: "allow"` + `rewrittenCommand` 自动剥离 `--json`（及附带 `--compact`）；否则返回 `decision: "deny"` 提示默认紧凑 Markdown 已含完整信息。
  5. **防 `diagnose` 后机械重复拉取**（`ruleId: "NO_REDUNDANT_POST_DIAGNOSE"`）：若本轮回执已有成功的 `diagnose`（且无后续 `--apply` 修改），紧接着无过滤参数裸跑 `pob summary`、`pob skills`（无 `--group`）或 `pob items`（无 `--slot`/`--raw`/`--all`）-> `deny` 并提示：`当前轮次 pob diagnose 输出已包含完整去水面板、全技能组与装备摘要；仅在需要查看单件黄装原文本时运行 pob item inspect --slot "<Slot>"。`
- **canonical `raw` 输出**：`{"decision": "allow" | "deny", "ruleId": "..." | null, "reason": "..." | null, "rewrittenCommand": "..." | null}`

---

### Gate 3：`pob hook verify-stop [--protocol raw|agy|claude]`（收尾防假执行核验 + 自动代跑补真值）
- **宿主挂载点**：DSH `agent/turn-stopping` / AGY `Stop` / Claude Code `Stop`
- **canonical `raw` 输入（`stdin` JSON）**：
  ```json
  {
    "sessionId": "...",
    "turnId": "...",
    "userText": "...",
    "assistantText": "...",
    "continuationActive": false,
    "terminationReason": "NO_TOOL_CALL"
  }
  ```
- **防死循环前置短路**：
  - 若 `terminationReason` 非空且不等于 `"NO_TOOL_CALL"`（如 `USER_CANCELED`、`ERROR`、`MAX_INVOCATIONS`）：立即返回 `allow_stop`。
  - 若 `continuationActive == true`，或 `hook_turn_<hash>.json` 记录当前 `turnKey` 已触发 `stopRetries >= 1`：**立即返回 `allow_stop`**（若仍有残余违规，仅在 `advisoryWarning` 字段记录，绝不再打回）。
  - 若 `assistantText` 为空：直接返回 `allow_stop`。

#### 4 项机械验收判据（任一 FAIL 即触发单次打回 `stopRetries += 1`）

| 编号 | 核验项 | 触发条件 | 通过标准 | 防误伤豁免设计 |
|---|---|---|---|---|
| **V1** | **终稿文本幻觉与谬误扫描** | 所有非空 `assistantText` | 正向陈述句中**零命中**：<br>1. **PoE2 跨代词/URL/升华/珠宝**：`精魂`/`Spirit`（保留 `Spirit Shield`/`Summon Raging Spirit`/`Spirit Offering`）、`涂灌`/`Distilled`（保留 `Distilled Perfection`）、`引路石`/`Waystone`（保留 `Ancient Waystones`）、`Charm`、`Soul Core`、单辅助限制、PoE2 `EB`（把魔力转护盾）、PoE2 URL/升华/永恒珠宝；<br>2. **3.23–3.28 硬移除项**：非赋予/增幅/启蒙的常规 `Awakened *` 强辅、`神圣祝福`（`Divine Blessing`）、`先祖卫士/战士长`、迷宫头/鞋/手附魔、`异常/分歧/魅影`替代品质、`武装召唤`/左键瞬发战吼护体、`六分仪`；<br>3. **卓越辅助等级幻觉**：3.28+ 卓越辅助（Exceptional Support，自然上限 Lv3 / 瓦尔 Lv4）被声称升到 `Lv5+` 或 `20级`；<br>4. **硬机制谬误**：`33.33ms` 帧率、`CwC`/`吟唱时施放`吃`CDR`/冷却回复、酋长 `Ramako`/`雷默科` 站立火 DoT 靠易燃/元素要害/曝露减抗、`Frost Bomb`/`寒霜爆`当诅咒/占诅咒上限。 | **1. 引用与代码块剥离**：忽略 `>` 引用块（复述用户原话）。<br>**2. 分句否定/辟谣豁免**：按 `[。！？；\n!?]` 和 `;` 切句；同句含否定/对比/辟谣词（`移除\|删了\|删除\|绝版\|没有\|不存在\|不可用\|不是\|并非\|不能\|切勿\|别用\|不要\|无需\|无效\|不吃\|不受\|不会\|取代\|替代\|改为\|仅限\|已改为\|改为固定\|removed\|no longer\|not in poe1\|zero effect\|does not\|doesn't\|don't\|never\|instead of`）则豁免放行。<br>**3. 严禁把合法 3.30 实体名当违禁词**：普通提及 `Hatred`/`憎恨`、`Hierophant`/`圣宗`、`Inspiration`/`启迪`、`Ramako`/`雷默科`、`Esh's Visage` 等**不触发** V1（仅在明确作出错误机制断言时触发）。 |
| **V2** | **构筑/面板数字算力回执核对** | 满足任一：<br>1. `hasBuildSource == true`（本轮 `hook_turn` 或 `userText` 含 `@*.xml`/`@*.code`/`pobb.in`/`poe.ninja/poe1`/PoB Base64 串）<br>2. `assistantText` 断言了具体构筑面板或模拟收益（正则匹配 `\d+(?:\.\d+)?\s*(?:M\|k\|万\|亿)\s*(?:DPS\|EHP\|秒伤\|有效生命)` 或 `(?:DPS\|EHP\|总伤\|秒伤\|满血有效生命)[^\n。；]{0,16}[+-]\d+(?:\.\d+)?%`） | 1. 本轮回执（`ts >= turnStartTs`）至少有 1 条 `exitCode == 0` 且 `isCompute == true`；<br>2. 若本轮某条回执含 `applied == true` 将槽位 `S` 推至 `maxRev`，则存在一条 `isCompute == true` 且 `slot == S`、`rev >= maxRev` 的回执（防改后不看新面板）。 | 纯机制数学举例（无构筑源且无 `DPS/EHP` 面板断言，如“30% more 和 20% more 叠乘是多少”）不触发；若 `userText` 有构筑源但回复明确只是报错/要求用户补全文件而非给构筑结论且无面板数字，视情况仍需先跑一次 `diagnose`（除非已尝试且非 0 退出提示环境故障）。 |
| **V3** | **黄装空词缀/工艺位验视回执核对** | `assistantText` 对具体部位正向断言了空词缀/可上工艺（匹配 `<部位词>[^\n。；]{0,25}(?:空前缀\|空后缀\|空词缀\|open prefix\|open suffix\|open affix\|还有空位\|有空余词缀\|可(?:以)?直接工艺\|工艺台补)`，部位词复用 `EQUIPMENT_SLOT_ALIASES` + `1号戒指`/`2号戒指` 等常见别名） | 本轮成功回执 `inspectedSlots` 含该归一化槽位名或 `"*"`。 | 同句含条件/不确定语态（`先检查\|先看\|不确定\|确认是否有\|如果有空\|若有空\|若满词缀\|如果还有空\|前提是有空\|check if\|if .* open\|assuming .* open\|whether .* open`）豁免放行。 |
| **V4** | **触发器冷却/攻速断点验算回执核对** | `assistantText` 在触发器语境（`CoC\|暴击时施放\|Cospri\|冰剑\|Mjolner\|雷锤\|Poet\|诗人\|CWDT\|受伤时施放\|Sword of Ages\|万古之剑\|触发`）下给出具体攻速上限（`\d+\.\d+\s*(?:APS\|次/秒)` 或 `攻速上限`）或具体 `CDR`/冷却回复断点百分比（如 `14%`、`52%`、`128%` 等配断点/档位词） | 本轮成功回执至少有 1 条 `hasTickCheck == true`（`diagnose` 或 `data tick`）。 | 仅定性提及“注意攻速别超过触发冷却上限”而无具体数字阈值时不触发。 |

#### 自动代跑塞真值（Auto-Remediation，总预算 `<= 2.5s`）
当 `V1..V4` 触发拦截时，`cli_hook/remediate.py` 在返回前自动在进程内补跑对应取证（全程跳过回执写入，且绝不冷启动未运行的 daemon）：
- **V1 失败** -> 调用 L1 `game_data.ground_prompt(triggered_terms)`，拼装 3.30 真实替代品卡片与规则说明（`<20ms`）。
- **V2 失败** -> 若 daemon 已在运行（检查 `daemon.json` 存在）：
  - 若有本地 `@file.xml`/`@file.code` 且尚未跑过计算，调用 `call_daemon("diagnose", ..., auto_start=False, timeout=2.2)`；
  - 否则若槽位已加载（如 `applied` 后未读新面板），调用 `call_daemon("summary", {"target": slot}, auto_start=False, timeout=1.5)`；
  - 若 daemon 未运行或超时，则附上明确指令提示（如 `请执行 pob diagnose <source>`）而不阻塞。
- **V3 失败** -> 若 daemon 已在运行，对每个漏查槽位调用 `call_daemon("items", {"target": slot, "slot": eq_slot, "raw": True}, auto_start=False, timeout=1.2)` + L2 `item_inspect.inspect_item` + `format_inspection`，直接输出真实前后缀占用与可用工艺位。
- **V4 失败** -> 0ms 调用 L0 `ticks.build_server_tick_table(base_cd)`（按上下文自动识别 `coc`=0.15、`cospri`/`mjolner`/`poet`/`cwdt`=0.25、`soa`/`万古之剑`=0.50），直接产出 `0.033s` 断点表。

最终输出统一矫正指令块（截断控制在 `<= 2500` 字符）：
```xml
<pob-hook-correction retry="1/1">
[硬护栏拦截] 你的上一版回复未通过确定性核验：
- 原因：<具体违规项说明>
- 实测真值（已自动为你补跑）：
<代跑产出的紧凑 Markdown 真值>

请严格依据上述实测真值直接重写给用户的最终回复（修正违规内容与数值，不要向用户暴露内部拦截日志，无需重复调用已代跑的命令）。
</pob-hook-correction>
```

---

## 5. 各宿主薄胶水接线方式

### 5.1 DeepSeek Harness (`<dsh>/plugins/dsh-pob-assistant/`)
将 Hook 桥接抽离至独立小模块 `hook-bridge.mjs`，由 `index.mjs` 注册 3 个 Cordis 钩子（默认均可通过 config 开关控制，异常一律 Fail-Open）：
1. **`agent/pre-step`**（waterfall）：按 `(agent.id, turn, userText)` 去重，调用 `pob --offline hook pre-prompt --protocol raw` 锚定 `turnStartTs` 并按需注入 `<pob-grounding>`（若 hook 不可用回退旧版 `pob ground`）。
2. **`tools/pre-execute`**（waterfall）：拦截 shell 工具（`bash`/`shell`/`powershell`/`run_command`/`exec_command`/`terminal`），从 `exec.arguments` 提取命令传给 `pob --offline hook pre-tool --protocol raw`（`supportsOverwrite: false`）；若返回 `decision === "deny"`，返回 `{ kind: 'deny', reason: res.reason }`（遵循 DSH `PreToolDecision` 契约，不做参数重写），否则 `return next()`。
3. **`agent/turn-stopping`**（serial）：从 `agent.session` 提取本轮最新用户文本与最后一条 `assistant/message` 文本，调用 `pob --offline hook verify-stop --protocol raw`；若返回 `decision === "continue"` 且 `correctionPrompt` 非空，调用 `agent.steer(createFrozenUserMessage(res.correctionPrompt, { kind: 'pob-hook-correction', form: 'instructions' }))`，DSH 循环自动再走一步完成重写。

### 5.2 Antigravity (`hooks/hooks.json`) 与 Claude Code (`hooks/claude-hooks.json`)
- **`hooks/hooks.json`**（严格对齐 `agy-customizations/docs/hooks.md`）：
  - `PreInvocation`（flat array）-> `pob --offline hook pre-prompt --protocol agy`
  - `PreToolUse`（grouped `{matcher: "run_command|Bash|bash|powershell", hooks: [...]}`）-> `pob --offline hook pre-tool --protocol agy`
  - `Stop`（flat array）-> `pob --offline hook verify-stop --protocol agy`
- **`hooks/claude-hooks.json`**（Claude Code 标准格式模板）：
  - `UserPromptSubmit` -> `pob --offline hook pre-prompt --protocol claude`
  - `PreToolUse` (`matcher: "Bash"`) -> `pob --offline hook pre-tool --protocol claude`
  - `Stop` -> `pob --offline hook verify-stop --protocol claude`

---

## 6. 分阶段施工与验收门禁

1. **Step 1 — L0/L1/L4 内核、埋点与离线单测**：
   - 落地 `guardrails/reply_scanner.py`（+ `guardrails/__init__.py` 导出）、`turn_receipts.py`、`cli_hook/{__init__,gates,remediate,protocols}.py`、`cli.py` 与 `cli_common.py` 埋点、`scripts/arch_lint.py` `LAYER_MAP`。
   - 新增离线测试 `tests/test_hook_scanner_receipts.py` 与 `tests/test_hook_guardrails.py`，覆盖：
     - 4 类假执行正例拦截（只调 `status` 瞎报 DPS / `--apply` 后不查新 `rev`、正文推 `Awakened Elemental Focus` / `Lv20` 卓越宝石 / `精魂` / `33.33ms` / `CwC吃CDR` / `Ramako用易燃` / `Frost Bomb当诅咒`、未 `inspect` 断言 `Ring 1` 空后缀、未查 `tick` 瞎报断点）；
     - 否定科普语境与合法 3.30 词汇零误报（“3.28 已移除觉醒元素集中”“PoE1 没有精魂”“不确定戒指有没有空后缀”“圣宗开憎恨配合启迪”等全 PASS）；
     - Gate 2 全部 5 条拒止与 AGY `--json` 自动剥离重写；
     - `continuationActive` / `stopRetries >= 1` / `terminationReason != NO_TOOL_CALL` 防死循环必放行；
     - 多步 `PreInvocation` 游标不误重置与 AGY/Claude/raw 三协议编解码 + transcript 提取；
     - 离线自动代跑塞真值（V1 `ground`、V4 `ticks`、Mock daemon 下 V2 `summary` / V3 `item_inspect`）。
   - 门禁必过：`python pob_cli/scripts/check.py --fast`、`--self-test`、`check.py` 全量全绿（所有文件 `<=650L / <=24,000B` CRLF，函数 `<=200L`）。
2. **Step 2 — DSH 插件联调与对抗实测**：
   - 落地 `<dsh>/plugins/dsh-pob-assistant/hook-bridge.mjs`、接线 `index.mjs` 并升级 `verify-plugin.mjs` 实测 `agent/pre-step`、`tools/pre-execute`、`agent/turn-stopping` 三钩子。
   - 运行 `node <dsh>/plugins/dsh-pob-assistant/verify-plugin.mjs` 全绿，并执行 4 道红队对抗场景验证（确认违规回复被自动代跑塞真值单次纠偏，正常合规回复 `stopRetries == 0`）。
3. **Step 3 — 跨宿主模板与文档收口**：
   - 落盘 `hooks/hooks.json` 与 `hooks/claude-hooks.json`，同步更新 `AGENTS.md`、`README.md`、`README_EN.md` 与 `PROGRESS.md`。
