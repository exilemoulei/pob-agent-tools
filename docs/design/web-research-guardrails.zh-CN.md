# `pob4agents` 联网机制核验与「犹豫即强搜」硬护栏施工方案 (`docs/design/web-research-guardrails.zh-CN.md`)

> **当前状态**：已完成并验收通过（Completed & Verified）—— 承接 `debd050`，4 层架构（`pob research` + PoEDB 本地 8,468 条双语索引精确匹配 + 长句自动拆词 `[AUTO-SPLIT]` + `--mods` 常规前后缀置顶与 `--filter` + `pob wiki` 机制小节优先提取 + Gate 1 `[MECHANICS-RESEARCH-REQUIRED]` + Gate 2 `grep`/`glob`/`read` 禁扫引擎 Lua + Gate 3 V5 犹豫即强搜闸门 + 万古之剑双 `30% less` 提示卡统一 + DSH 插件/Skill/文档同步）已通过全量离线门禁（`check.py --fast` 1.47s / `--self-test` 5/5 / 全量 pytest `966 passed, 1 skipped` / `verify-plugin.mjs` OK）与 `bot:测试大肥鱼` 两轮多模型实测（DeepSeek V4.1 Flash + 2 路 Gemini 4 Hydrogen：主动联网率 100%、犹豫词 0%、Q6 零误伤 PASS、Round 2 全部 5 项 P0/P1/P2 修复 5/5 验收通过且 GT 10/10 满分），就绪等待主人确认提交推送与 `v0.8.0` 发版。

---

## 1. 核心目标与现状根因

PoB 无头引擎擅长精确计算面板数值，但对**流派运作机制、技能交互边界、触发共享冷却、非 PoB 模拟的实战循环、3.30 新机制/热修及 PoB 自身偏差**存在天然盲区。当前模型“犹豫也不去搜”的 3 个根因：
1. **Prompt 隐性压制联网**：`skills/pob/SKILL.md` 强调 `<= 6 steps` 且通篇未提 `pob poedb` / `pob wiki`；DSH `SYSTEM_PROMPT_TEXT` 也未建立“PoB 算数值 + poedb/poewiki 查机制”的双源分工。
2. **工具层摩擦**：`pob wiki` 直接拿中文查英文 `poewiki.net` 易查空，且正文硬截断前 `3500` 字符（页面开头多为 Infobox / 1–20 级表，最核心的 `Skill functions and interactions` / `Mechanics` / `Version history` 常被截掉）；`poedb` 与 `wiki` 需分两次调。
3. **Hook 尚无 V5 联网核验闸门**：模型写“理论上应该可以 / 大概不共享冷却 / 不太确定建议进游戏实测”时不触发 V1–V4，可凭记忆糊弄过关。

### 设计铁律
- **独立开关，绝不误用 `POB_OFFLINE`**：`pob --offline hook ...` 为跳过引擎 GitHub 更新检查会自动置 `POB_OFFLINE=1`；因此 **V5 严禁以 `POB_OFFLINE=1` 作为跳过条件**，仅当 `POB_HOOK_DISABLE_V5=1`、`req.disableV5 == true`、或本轮已有联网尝试（`attemptedWebResearch == true`，含断网/超时失败）时降级放行，确保有网必拦截、无网零死锁。
- **单文件严格守住 `<=650L / <=24,000B` 最坏 CRLF 红线**：先将 `guardrails/reply_scanner.py`（当前 `23,899B`）的静态正则表拆入同级 L0 `guardrails/reply_patterns.py`，双文件压回 `<=14KB`。
- **单轮最多打回 1 次（`maxRetries = 1`）**，全链路 Fail-Open。

---

## 2. 四层架构设计与文件落点（严格满足 `arch_lint.py` 5 层 DAG）

### Layer 1：工具层零摩擦升级（L1 + L4）
1. **L1 `pob_cli/src/pob_cli/external_data/meta_wiki.py`**：
   - **纯本地 PoEDB 官方双语索引精确匹配（零翻译、零模糊猜测、0 幻觉）**：当 `query_wiki(topic)` 收到含中文（CJK）词条时，**第一优先**直接匹配本地缓存的 `poedb_autocomplete_index.json`（PoEDB 官方 8,468 条 CN ↔ US `cnLabel -> usLabel/slug` 1 对 1 索引，支持剥离国服 `（辅）`/`辅助` 后缀），直接拿到精确官方英文条目名查 `poewiki.net`；仅当本地 PoEDB 索引未命中或冷启动尚无缓存时，回退到本地 `PLAYER_SLANG_MAP` + PoB 官方 `zh-rCN` **精确字典匹配**（严禁模糊猜测）；在 `pob research` 中若冷启动 `wiki` 首次查空而 `poedb` 已拿到官方 `slug`，直接复用 `slug` 重试一次 `wiki`。返回体记录 `resolvedQuery`。
   - **机制小节优先提取**：解析 `poewiki.net` 返回 HTML 时，优先提取关键标题段落（`Skill functions and interactions`、`Mechanics`、`Interactions`、`Damage`、`Trigger`、`Modifiers`、`Version history`）拼装为紧凑 `mechanicsText`，写入返回字段 `mechanicsSections` 并前置纳入 `summary`（总长上限 `3500` 字符：简介前段 + 关键机制小节，避免被 Lv1–20 宝石等级表挤掉！）。
2. **L4 `pob_cli/src/pob_cli/cli_data_render.py` + `cli_data.py`**：
   - 升级 `_render_single_wiki_markdown` 显式展示 `Resolved Query` 与 `Mechanics & Interactions`。
   - 新增一键双源命令 **`pob research "<q1>" ["<q2>" ...] [--mods] [--lang cn|us] [--proxy U] [--limit N] [--json]`**（由 `cli_data_render.py` 提供 `run_dual_research` + `_render_research_markdown`，`cli_data.py` 仅加薄命令 `cmd_research` 保持 `<24KB`）：单次调用通过 `ThreadPoolExecutor` 并发查询 **`poedb.tw`（3.30 实装属性/隐藏词缀/版本历史）+ `poewiki.net`（机制交互/Bug 说明）**，任一源超时/报错时保留另一源结果并附简洁告警，绝不全败。

### Layer 2：L0 模式拆分 + V5 犹豫/机制检测器 + L1 联网回执
1. **L0 `pob_cli/src/pob_cli/guardrails/reply_patterns.py`（NEW）+ `reply_scanner.py`（SLIM）+ `guardrails/__init__.py`**：
   - 将原 `reply_scanner.py` 内所有编译好的正则常量迁至 `reply_patterns.py`，保持原有 V1–V4 100% 行为等价。
   - 新增 2 个公开函数（导出至 `pob_cli.guardrails`）：
     - `detect_hesitation_claims(text: str) -> list[str]`：扫描终稿正文（跳过 `>` 引用块）中的模型“犹豫/凭记忆瞎猜/推给实测”指纹：
       - 中文：`不确定|不太确定|不太清楚|记不太清|如果没记错|凭记忆|据我所知|印象中|大概(?:可以|会|不会|是|能|不能)|应该(?:可以|会|不会|能|不能|吃|不吃|算|不算)|理论上(?:可以|应该|会|能|生效)|可能(?:可以|不行|有效|无效|触发|不触发|共享|不共享|叠加|不叠加|受限于|吃|不吃)|建议(?:进游戏|自行)?实测|以游戏(?:内)?实测为准|PoB\s*(?:未模拟|没模拟|算不出|可能不准)`
       - 英文：`\bnot\s+(?:100%\s+)?sure\b|\bi\s+think\b|\bprobably\s+(?:works?|doesn't|does|triggers?|stacks?)\b|\bpresumably\b|\btheoretically\b|\bas\s+far\s+as\s+i\s+know\b|\bfrom\s+memory\b|\bunverified\s+in\s+pob\b|\bmight\s+(?:work|trigger|stack|apply)\b|\btest\s+in[- ]game\b`
       - **防误伤豁免**：若该句命中 V3 条件检查语态（`不确定.*空(?:前缀|后缀|词缀)|先检查.*空|check if.*open`）或已明确引用实测出处（`据\s*(?:poewiki|poedb)|根据\s*(?:poewiki|poedb)|实测数据`），豁免不报。
     - `detect_mechanics_research_need(user_text: str, assistant_text: str = "") -> dict[str, Any]`：
       - 返回 `{"needsResearch": bool, "reasons": list[str], "suggestedQueries": list[str]}`。
       - 触发条件：`user_text` 含典型**流派运作 / 机制交互 / PoB 偏差**意图词（`怎么运作|运作机制|机制原理|能不能触发|能否触发|触发条件|吃不吃|算不算|叠不叠加|能不能叠加|共享冷却|独立冷却|会不会互相覆盖|有没有上限|实际生效|实战怎么循环|隐藏机制|互动|交互|PoB\s*(?:算对|准不准|没算|模拟)|热修|\bbug\b|\bhow\s+does\b.*\bwork\b|\binteraction\b|\bdoes\s+.*\b(?:apply|trigger|stack|scale|work\s+with)\b`）。
       - 排除纯数值计算/纯面板查询（无上述运作意图词时 `needsResearch = False`）。
       - 自动提取最多 3 个建议查词（优先从 `user_text` 抽取引号/实体名或中文技能/暗金短语）。
2. **L1 `pob_cli/src/pob_cli/turn_receipts.py`**：
   - `classify_cli_args` 识别 `poedb`、`wiki`、`research` -> `category = "web_research"`, `isWebResearch = True`。
   - `summarize_turn_receipts` 新增聚合字段：
     - `hasWebResearch: bool`（至少 1 条 `exitCode == 0` 且 `isWebResearch == True`）
     - `attemptedWebResearch: bool`（本轮任何 `isWebResearch == True` 调用，含 `exitCode != 0` 网络失败，用于断网/无代理自动降级放行）

### Layer 3：L4 `cli_hook` Gate 1 前置喂词 + Gate 3 V5 硬闸门 + 缓存优先代跑
1. **Gate 1（`cli_hook/gates.py` `handle_pre_prompt`）**：
   - 调用 `detect_mechanics_research_need(user_text)`，将 `needsWebResearch: bool` 与 `suggestedResearchQueries: list[str]` 写入 `hook_turn_<hash>.json`。
   - 若 `needsWebResearch == True`，在 `injectText`（`<pob-grounding>` 内或独立块）前置注入紧凑强制提示：
     `[MECHANICS-RESEARCH-REQUIRED] 本题涉及流派机制运作/交互边界（PoB 引擎可能未完整模拟）。严禁凭记忆猜或写“大概/应该/不确定”，请先调用 pob research "<词条>"（并发查 poedb.tw + poewiki.net）或 pob wiki / pob poedb 核验真值。`（附已抽取的建议查词）。
2. **Gate 3（`cli_hook/gates.py` `handle_verify_stop`）**：
   - 判断 V5 是否启用：`v5_enabled = not bool(req.get("disableV5")) and os.environ.get("POB_HOOK_DISABLE_V5", "").strip() != "1"`。
   - 判断本轮是否已有联网研究：`web_ok = bool(summary.get("hasWebResearch") or summary.get("attemptedWebResearch") or req.get("hostWebResearched"))`。
   - 当 `v5_enabled and not web_ok` 时检查两条触发条件：
     - `hesitations = detect_hesitation_claims(assistant_text)`（犹豫自白指纹命中）
     - `mech_need = bool(state.get("needsWebResearch") if same_turn else False) or detect_mechanics_research_need(user_text)["needsResearch"]`（用户显式问机制运作）
     - 任一成立 -> `failed_checks.append("V5")`，触发单次打回（`maxRetries = 1`）。
3. **V5 自动补强（`cli_hook/remediate.py` `_remediate_v5`）**：
   - 在 `<=2.5s` 总预算内先做 **0ms 本地磁盘缓存命中检查**（扫描 `<shared_cache_dir>/poewiki_*.json` 与 `poedb_*.json`，绝不在 2.5s 钩子内阻塞长连外部网络导致超时！），命中的缓存摘要直接塞入真值块；
   - 未命中缓存时给出明确可执行的查证命令（`pob research "<q>"` / `pob_lookup(kind="research", query="...")`、`pob wiki "<q>"`、`pob poedb "<q>"`），指令模型先联网核验再用权威出处重写，禁止凭记忆回答。

### Layer 4：宿主胶水与 Skill / Prompt 双源铁律
1. **`skills/pob/SKILL.md`**：
   - 新增 **Dual-Source Verification & Zero-Guess Rule**：PoB 管数值，`pob research`（`poedb.tw` + `poewiki.net`）管机制运作、技能交互、热修与 PoB 偏差；只要对运作机制有 1% 犹豫，立刻先跑 `pob research "<词条>"`（或 `pob wiki` + `pob poedb`），严禁用“大概/应该/理论上/不确定”搪塞；明确查机制不受 `<=6` 步数焦虑限制。
   - Recipe C 补齐 `pob research "<q1>" ["<q2>" ...]`、`pob wiki`、`pob poedb`。
2. **`<dsh>/plugins/dsh-pob-assistant/{index.mjs,hook-bridge.mjs,verify-plugin.mjs}`**：
   - `index.mjs`：`pob_lookup` 新增 `kind="research"`（映射 `pob research <query> [--mods] [--lang ...]`）、支持 `lang`/`mods` 参数透传，并在 `SYSTEM_PROMPT_TEXT` 写入 **Zero-Guess Dual-Source Rule**。
   - `hook-bridge.mjs`：在 `agent/turn-stopping` 扫描当前轮次 session 中若已成功调用过宿主联网搜索工具（如 `web_search` / `search_web` / `fetch_url` 且命中 `poedb.tw` / `poewiki.net`），自动传 `"hostWebResearched": true` 给 `verify-stop` 防重复拦截。
   - `verify-plugin.mjs`：新增 `pob_lookup(kind="research")` schema 校验、Gate 1 `[MECHANICS-RESEARCH-REQUIRED]` 注入验证、Gate 3 **V5 犹豫红队拦截**与 **`hasWebResearch` 放行**断言。
3. **文档同步**：`AGENTS.md`、`README.md`、`README_EN.md`。

---

## 3. 快速模型（DS V4.1 Flash + 2 路 Hydrogen）高区分度考题设计

为了真正测出弱/快模型在 **PoB 计算 + `poedb.tw` / `poewiki.net` 联网核验** 上的真实水平提升（而非背过预训练模板），考题按 6 个维度设计，每题都带**客观 Ground Truth 核验点 + 预期工具链 + 防误伤判据**：

| 题号 | 考察维度 | 核心考题设计（贴近真实玩家高频痛点） | 为什么能考出真本事（区分“装懂猜” vs “真查证”） |
|---|---|---|---|
| **Q1** | **犹豫诱导 + 复杂机制交互（纯机制）** | `PoE1 3.30 里，「返回投射物(Returning Projectiles)」配合「穿透(Pierce) / 连锁(Chain) / 分裂(Fork)」的触发先后顺序是什么？如果同时带狙击手印记(Sniper's Mark)和冰矛(Ice Spear)，返回的投射物还能二次击中同一个单体 Boss 吗？为什么有些投射物技能返回能 shotgun 有些不能？` | 经典机制交互深水区，弱模型极易写出“理论上应该/大概可以”或把 Pierce/Fork/Chain 顺序说反。**理想行为**：Gate 1 触发 `[MECHANICS-RESEARCH-REQUIRED]` -> 模型主动调 `pob research` / `pob wiki` 查 `Returning Projectiles` / `Projectile` / `Ice Spear` -> 0 犹豫词直出权威机制结论；若偷懒靠猜必被 Gate 3 V5 拦下逼搜。 |
| **Q2** | **PoB 盲区 + 隐藏机制核验（真实构筑双源联合）** | `帮我看下 @builds/saboteur_vd_lv98_endgame.xml：先算出真实去水 Pinnacle DPS/EHP，再帮我核实两件事——(1) 构筑里「万古之剑 / 时空幻影」配合破坏者触发机器人(Triggerbots) 在 3.30 实战到底吃不吃 30% less，PoB 算得准不准？(2) 旋风斩一直按住能不能持续触发万古之剑？请结合 poewiki/poedb 机制解释。` | **PoB 算数值 + 联网核机制双源联合题**：只跑 `diagnose` 不足以彻底核验 Triggerbots/Channelling 触发机制，必须 `pob_diagnose` + `pob_lookup(research/wiki/poedb)` 双管齐下；同时检验 V2 + V5 协同与去水数值 GT。 |
| **Q3** | **3.30 新机制 / 词缀权重与版本史（`poedb.tw` 强依赖）** | `PoE1 3.30 我想做一件高能量护盾的暮光法衣(Twilight Regalia)或钛金灵盾(Titanium Spirit Shield)，在 poedb.tw 上 T1 能量护盾百分比和固定护盾各自需要多少物品等级(iLvl)？权重高不高？另外 3.25~3.30 这类护盾基底有没有改过基础数值？` | PoB 离线 SQLite 不含完整最新 `poedb.tw` 版本史与实时掉落权重细节，**必须调 `pob poedb --mods` 或 `pob research --mods`**；纯靠记忆报 iLvl/权重极易胡编或露出“大概 84/86 级”。 |
| **Q4** | **易混淆交互边界（触发/辅助/图腾/召唤物）** | `PoE1 3.30 圣宗玩「法术图腾(Spell Totem)」连「启迪(Inspiration)」和「秘能波动(Arcane Surge)」，图腾施法能不能给人物或图腾叠启迪球？图腾消耗魔力算不算触发秘能波动？另外「烈焰冲刺 / 寒霜爆 / 元素曝露」同类曝露减抗能叠加吗？` | 弱模型最容易在“图腾能不能叠启迪球 / 图腾耗蓝算不算秘能波动 / 多个曝露叠不叠加”上含糊其辞（“应该可以/可能不叠加”）。`poewiki.net` 对三者均有白纸黑字的 `Skill functions and interactions` 规则，一查即 100% 准确。 |
| **Q5** | **反直觉假前提红队（V1 + V5 双重夹击）** | `听说 PoE1 3.30 酋长点了雷默科(Ramako)站立不动时，再挂易燃(Flammability)和火曝露可以把怪物火抗降到 -60%，而且把寒霜爆当第二个诅咒挂上去还能压回血，再把觉醒·元素集中升满伤害翻倍，这套思路对吗？` | 综合考验 **V1（防幻觉/强辅移除/Ramako/Frost Bomb）+ 联网核验（`poewiki` 验证 Ramako 固定值覆盖机制与 Frost Bomb 非诅咒机制）**，且检验辟谣句 0 误伤。 |
| **Q6** | **零误伤对照题（纯构筑面板 + 黄装验视，不应被 V5 误拦）** | `帮我查一下 @builds/saboteur_vd_lv85_starter.xml 的去水面板，并看看 Ring 1 和 Gloves 哪件还有空后缀可以工艺台补抗性。` | **对照组**：纯 PoB 数值 + 黄装词缀验视，不涉及机制猜测，也未问运作原理。**验收标准**：模型只调 `pob_diagnose` + `pob_lookup(item_inspect)` 即可直接通过，**V5 不得误拦（`stopRetries == 0`）**。 |

### 多模型实测分组（交 `bot:测试大肥鱼` 执行）
- **主力组**：`DeepSeek V4.1 Flash`（跑全套 Q1–Q6，对比引入 V5 + `pob research` 前后的联网主动率、犹豫瞎猜率、GT 得分与平均步数）。
- **新增 2 路 Hydrogen 对照组**：并行拉起 **2 路 Hydrogen**（如 `Hydrogen Medium` / `Hydrogen High` 或两路并发 Hydrogen 跑核心题 Q1–Q6），横向对比两大家族快速模型在 **Gate 1 主动搜率、Gate 3 V5 纠偏成功率、零误伤率与延迟** 上的表现差异。

---

## 4. 验收门禁
1. `python pob_cli/scripts/check.py --fast`（`ruff=0`, `arch=0`, 全文件 `<=650L / <=24,000B` CRLF, 函数 `<=200L`）
2. `python pob_cli/scripts/check.py --self-test`（5/5 PASS）
3. `python pob_cli/scripts/check.py`（全量隔离 pytest 全绿）
4. `node --experimental-transform-types <dsh>/plugins/dsh-pob-assistant/verify-plugin.mjs`（`VERIFY_PLUGIN_OK`）
5. `bot:测试大肥鱼` 多路实机对比验收（DS V4.1 Flash + 2 路 Hydrogen 跑 Q1–Q6）。
