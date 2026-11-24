# PoB Agent CLI (`pob`) 开发方案

本文档锁定 `pob4agents` 项目的技术路线、模块边界、RPC 契约与严禁踩坑清单。任何接手开发的 Agent 必须严格按本方案执行，不得偏离架构或调用已证实失效的接口。

---

## 1. 核心目标与四大铁律

### 1.1 产品目标（对齐三大场景）
1. **看懂构筑（Inspect & Audit）**：输入任意 PoB 分享链接（`pobb.in`、`poedb.tw/pob`、`maxroll.gg/poe/pob`、`pob.codes`、`poe.ninja/pob`、`pastebin`、`rentry`）、`poe.ninja` 角色主页链接、裸导出码或本地 `.xml`，直接驱动真实 Path of Building 引擎读取攻防面板、装备、技能连法、天赋专精、Config 配置与计算拆解，并自动审计抗性缺口、魔力收支与“PoB 战士”配置水分。
2. **试改与无损对比（What-If & Apply）**：支持单步或组合修改装备、药剂、涂油、技能石（名称/等级/品质/开关）、天赋点/专精、Config 选项，等待真实 PoB 完成重算后，输出清洗完颜色码的结构化前后数值对比表（绝对差值 + 百分比变化 + 新增告警）。默认 `what-if` 无损回滚；加 `--apply` 持久生效并入全局多标签 Undo/Redo 栈。
3. **跨构筑参考与寻优（Compare & Optimize）**：利用引擎原生算力对未点天赋（`node_power`）与备选技能石（`gem_search` 按 DPS 排序）做真实收益排行；加载他人参考构筑时，自动将双方拉到**同一套外部靶子环境与去水分基准**下做公平对比，拆解天赋/技能/装备路线差异，并支持一键“借穿”对方某件装备或技能组（`borrow`）到当前构筑实测真收益。

### 1.2 四大架构铁律
1. **零公式复刻**：严禁在 Python 侧用简化公式估算 DPS/EHP，100% 数值必须由 `PobTools[20261102]/pob-zh.exe --engine-headless` 真实计算产出。
2. **常驻守护 + 薄 CLI（非巨型 MCP）**：采用本地常驻 Daemon 管理无头引擎子进程（消除每次调用 ~2.9s 冷启动开销），对外暴露面向 Agent 高信噪比的 `pob` CLI 命令（默认输出紧凑 Markdown 表，支持 `--json`），不向模型上下文注入数十个原始 UI 级 RPC schema。
3. **内存级全量快照回滚**：所有 `what-if` 试改与跨标签 `undo/redo` 统一基于 `export_code{}` + `import_code{code, mode="replace"}` 纯内存快照（耗时 `<10ms`，覆盖天赋+装备+技能+Config+等级职业，零磁盘 I/O）。
4. **同靶子公平对比与全景去水分**：跨构筑对比必须区分**外部靶子环境白名单**（Boss 级别、怪物等级、亢奋度、自定义怪物抗性等，可跨构筑对齐）与**角色固有构筑项**（盗贼奖励 `bandit`、万神殿 `pantheon*`、自产球/低血机制等，严禁盲拷破坏构筑），同时给出“各自原始面板”、“对齐 `main` 外部靶子环境面板”与“标准 Pinnacle Boss 全景去水分面板”，主动揭露 `customMods`、敌人满感电（`conditionEnemyShocked`）、普通怪预设、非魔血虚开 5 药、常驻护体/爆发/瓦尔技能与 `PartyTab` 团队光环等注水手段。

---

## 2. Argon 防错红线清单（违反任一条即判失败）

基于 `exports/agy_7dd0b608.md` 的排障复盘与 `PobTools[20261102]/Data/bridge/bridge.lua` 源码实测，以下红线必须在代码层面硬编码拦截：

| 编号 | 严禁行为（Argon 曾踩或险踩的坑） | 根因（已实证） | 唯一正确做法 |
| :--- | :--- | :--- | :--- |
| **R1** | 调用 `compare_load`、`compare_state`、`compare_select`、`compare_tree`、`compare_items`、`compare_skills`、`compare_config` | 当前 PoE1 引擎为 `2.67.2` (`master` 分支)，`version().caps.compareTab == false`（`Build.compareTab` 仅存在于 `beta` 分支），调用必报错 | 跨构筑对比由 Daemon 双槽位（`main` + `ref`）或快照提取在 Python 侧结构化 diff 实现；同构筑内多套天赋树对比可用 `set_compare_spec` |
| **R2** | 调用 `sidebar_breakdown` 查属性来源 | `2.67.2` 上 `version().caps.sidebarBreakdown == false`，调用永远返回空 `{sections: []}` | 统一调用 `get_calcs{}` 定位格子坐标 `{si, ui, ri, ci}`，再调 `calcs_breakdown{si, ui, ri, ci}` |
| **R3** | 调用 `config_mod_search` / `config_mod_add` | `2.67.2` 上 `version().caps.configModBrowser == false` | 统一使用 `list_config`、`set_config`、`reset_config`、`set_custom_mods` |
| **R4** | 假定进程刚启动时 `buildLoaded` 恒为 `false` 或不调 `new_build{}` 直接导入，或在双 Worker 下写 `Settings.xml` | 干净 `Settings.xml` 下 `buildLoaded == false` 调 `import_*` 必抛 `"no build is loaded"`；若 `Settings.xml` 存有上次记录则自动重开旧构筑（`bridge.lua` 第 312 行），且双进程写同一 `Settings.xml` 会竞态冲突 | Worker 握手完成后**无论 `buildLoaded` 初值如何，一律先调 `new_build{}`** 建立干净空白构筑；运行期严禁调用写 `Settings.xml` 的方法 |
| **R5** | 把 `get_sidebar` 返回值当成 `result["stats"]` 读取，或从 `get_stats` 读 `warnings` | `get_sidebar` 返回 `{rows, warnings, rev, outputRevision}`（`warnings` 在 `get_sidebar` 中！）；`get_stats` 仅返回 `{stats, count, outputRevision}` | 严格按第 4 节 RPC 契约读取字段，禁止盲猜返回结构 |
| **R6** | 抓取 `poe.ninja` `.mjs`、用 `httpx` 调 `www.pathofexile.com/character-window/*`、或拼未编码字符调 `import_from_uri` | `.mjs` 哈希轮换；GGG `character-window` 被 Cloudflare HTTP 403 拦截且缺 `get-characters` 头与 `poe.ninja` 配置快照；`bridge.lua` `valid_pob_uri` 拒绝私享服括号/中文等非 ASCII 字符 | `poe.ninja/pob/*` 走 `import_from_url` 或 `/raw`；`poe.ninja` 角色页统一走 **`index-state` 查表 + `httpx.get("https://poe.ninja/poe1/pob/raw/overview/code", params=...)` + `import_code`** |
| **R7** | 每条 CLI 命令单独 `subprocess.Popen("pob-zh.exe")` 跑完即杀 | 每次冷启动需加载 Lua VM 与 118 个中英词典 JSON（实测耗时 `~2.9s`），且丢失会话修改状态 | 必须通过本地常驻 `daemon.py` 复用已启动的 headless 进程，单次 RPC 延迟压在 `10~50ms` |
| **R8** | 无锁并发读写 `stdin/stdout`、挂 `stderr=PIPE` 不消费、或直接把首行输出当响应 | 启动先吐 `gate_result`/`hello` 事件；并发无锁会协议串包；Windows `stderr` 管道写满（4~64KB）会让子进程永久死锁；网络导入挂死会拖死 Daemon | 每槽位加互斥锁 + 单次 RPC 超时强杀自愈；`stderr` 必须由独立后台线程排空（或定向至日志文件）；循环读行严格匹配 `resp.get("id") == req_id` |
| **R9** | 在 `bridge_client.py` 底层对全部返回字段一刀切剥离 `^7`/`^xRRGGBB`，或把上千键裸 JSON 直接吐给 Agent | 全局正则清洗会篡改 `save_xml`（`<Notes>` 原文）、`export_code`、`item_raw` 并把笔记中幂次如 `10^6` 误删为 `10`；裸 JSON 污染上下文 | `bridge_client.py` 保持传输无损；仅在 `analyzer.py` / `formatters.py` 展示与指标解析层执行 `strip_pob_escapes()`（跳过 `xml`/`code`/`raw`/`notes`） |
| **R10** | 在 PowerShell 中对 `PobTools[20261102]` 使用不带 `-LiteralPath` 的命令 | `[20261102]` 被 PowerShell 当作通配符字符集解析导致找不到路径；`pob-zh.ini` 含二进制/非 UTF-8 字节 | Python 内一律用 `pathlib.Path`；PowerShell 命令涉及该路径一律加 `-LiteralPath`；绝不读写 `pob-zh.ini` |
| **R11** | 用 `save_build_as` 写外部路径，或让未鉴权的本地 HTTP Daemon 直接代写任意磁盘路径 | `bridge.lua` 第 1990 行 `under_build_path` 拦截外部路径；无 Token 的本地 HTTP 端口若开放写盘存在浏览器跨站/DNS Rebinding 任意文件覆写风险 | `daemon.json` 写入随机 `token` 并强制校验 `X-PoB-Token` 请求头；`pob save` 由 Daemon 调 `save_xml{}` 返回字符串，**在 CLI 客户端进程本地落盘**；本地 `.xml` 读取编码同样在 CLI 侧完成 |
| **R12** | 对多节点天赋增删列表无脑循环调 `tree_click`，或忽略专精/职业确认返回标志 | `bridge.lua` 中 `tree_click` 是 **Toggle 翻转开关**（过路自动点亮或级联摘除会让后续点击反向抵消）；点到专精或跨职业起点时静默返回 `{needsMastery: true}` / `{needsConfirm: "class_change"}` 而不报错 | 每次 `tree_click` 前必须先查最新 `get_tree_state().allocatedNodes` 做幂等过滤（加点仅点未亮节点，删点仅摘仍亮节点），并显式校验返回标志位 |

---

## 3. 系统架构与目录规划

### 3.1 目录结构
全部新增代码集中在 `pob_cli/`（标准 Python 包，`pyproject.toml` 注册 `pob` 可执行入口），不动已有参考仓库（`POEMCP`、`pob-mcp`、`PobTools[20261102]`）：

```text
pob4agents/
├── plan.md                      # 本开发蓝图与防错契约
├── PROGRESS.md                  # 极简进度交接单（progress-log 规范）
├── AGENTS.md                    # 供所有 Agent 秒懂 pob CLI 用法的电报式规范（M4 产出）
├── pob_cli/
│   ├── pyproject.toml           # Python >= 3.11，依赖仅 httpx + click/typer（零重型框架）
│   ├── src/pob_cli/
│   │   ├── __init__.py
│   │   ├── bridge_client.py     # pob-zh.exe --engine-headless 子进程封装、双事件握手、id 匹配、互斥锁/超时自愈、stderr 排空（无损传输）
│   │   ├── daemon.py            # 127.0.0.1 本地鉴权常驻守护进程（X-PoB-Token），管理 main / ref 双槽位及快照 Undo/Redo 栈
│   │   ├── importers.py         # 统一输入路由：8 大分享站 URL / poe.ninja 角色页（raw/overview/code） / 本地 XML / 裸码
│   │   ├── snapshot.py          # export_code/import_code 内存事务包装器（what_if 上下文管理器 + apply 历史栈）
│   │   ├── analyzer.py          # 颜色码剥离、核心攻防面板提取（含召唤/FullDPS）、全景水分审计、calcs_breakdown 检索封装
│   │   ├── mutators.py          # 装备/药剂/多槽涂油/技能/幂等天赋/Config/复合 batch 原子修改器
│   │   ├── comparator.py        # 跨构筑外部靶子环境白名单对齐、全景去水分基准对比、天赋/技能/装备路线 diff、borrow 移植器
│   │   ├── external_data.py     # 可选轻量物价（poe.ninja dense/overviews）与 Wiki（poewiki.net）磁盘缓存查询
│   │   ├── formatters.py        # 颜色码剥离兜底、紧凑 Markdown 表格与 --json 双模渲染器
│   │   └── cli.py               # `pob` 命令行入口（自动拉起 daemon、携带 Token 转发请求、本地执行 XML 读写落盘）
│   └── tests/
│       ├── test_protocol.py     # 颜色码剥离、XML <-> ShareCode 编解码、URL 路由单测
│       └── test_e2e_headless.py # 基于 AWqOLSSmyG 的真实无头引擎端到端集成测试
```

### 3.2 进程与通信模型
```text
[Agent / 用户终端]
   │  pob <subcommand> (HTTP/JSON + X-PoB-Token over 127.0.0.1:<port>, 状态与密钥文件 .runtime/daemon.json)
   ▼
[pob_cli.daemon] (常驻 Python 进程，全局空闲 30 分钟自动退出)
   ├── Slot "main" ──► pob-zh.exe --engine-headless PobTools[20261102]\PoE1 (槽位互斥锁 + 单次 RPC 超时)
   │                    └── 内存维护 undo_stack / redo_stack (存 export_code 字符串，上限 30 步)
   └── Slot "ref"  ──► 按需懒启动第二个 headless worker（仅 `compare`/`borrow` 拉起，空闲 5 分钟提前回收释放 ~200MB 内存）
```

- **Worker 启动与并发安全序列（硬编码在 `BridgeWorker.start()`）**：
  1. 启动 `PobTools[20261102]\pob-zh.exe --engine-headless PobTools[20261102]\PoE1`（`stdin/stdout` UTF-8 行缓冲，`cwd` 设为 `PobTools[20261102]\PoE1`；`stderr` 由独立守护线程持续排空或写日志文件，严禁裸挂 `PIPE` 不读导致 Windows 管道缓冲区满死锁）。
  2. 为每个 `BridgeWorker` 绑定独立 `threading.Lock` 与单次调用超时（默认 `15s`，网络导入 `30s`），超时立即 `kill()` 子进程并按最新快照自动重建。
  3. 消费启动事件直到收到 `event == "gate_result"`（断言 `data.ok == true`）与 `event == "hello"`（记录 `pobVersion`）。
  4. 调 `version{}` 缓存 `caps` 字典，供后续方法做前置能力断言。
  5. 调 `set_translate{"enabled": true}` 开启双语返回（保留英文原名与 `Zh` 中文译名）。
  6. **无论 `version().buildLoaded` 初值是 `true`（`Settings.xml` 自动重开上次构筑）还是 `false`，一律立即调 `new_build{}`** 建立干净空白构筑，且运行期绝不调用任何写 `Settings.xml` 的方法。

---

## 4. 关键底层机制与 `bridge.lua` 精确契约

### 4.1 统一导入路由（`importers.py`）
输入参数 `source` 按以下优先级自动识别并导入目标槽位（导入前先确保槽位已初始化 `new_build{}`）：

1. **本地 `.xml` 文件路径**（在 CLI 端判定 `Path(source).is_file()`）：
   - CLI 本地读取 UTF-8 XML 文本 -> `zlib.compress(xml.encode("utf-8"))` -> `base64.urlsafe_b64encode(...).decode("ascii")` -> 发给 Daemon 调 `import_code{"code": code, "mode": "replace"}`。
2. **标准 PoB 分享站 URL**（匹配 `pobb.in`、`poedb.tw/pob`、`maxroll.gg/poe/pob`、`pob.codes`、`pastebin.com`、`pastebinp.com`、`rentry.co`、`poe.ninja/pob`、`poe.ninja/poe1/pob`）：
   - 主路径：调 `import_from_url{"url": url}` 拿到 `{code: "..."}` -> 再调 `import_code{"code": code, "mode": "replace"}`。
   - 兜底路径（规避 `BuildSiteTools.lua` 第 33 行 `%w+` 不匹配含 `-`/`_` ID 的边界 bug）：若 `import_from_url` 报错且域名支持 `/raw`（如 `pobb.in/<id>/raw`、`poedb.tw/pob/<id>/raw`、`poe.ninja/pob/<id>/raw`），由 Python `httpx` 拉取裸 code 后调 `import_code`。
3. **`poe.ninja` 角色主页 URL**（如 `https://poe.ninja/poe1/builds/<leagueUrl>/character/<account>/<charName>[?timemachine=...]`）：
   - **唯一标准路径（严禁调 GGG `character-window` 撞 Cloudflare 403，严禁调 `import_from_uri` 撞 `valid_pob_uri` 括号/中文正则报错）**：
     1. 读取 `https://poe.ninja/poe1/api/data/index-state`（磁盘缓存 1 小时），在 `economyLeagues` / `buildLeagues` / `snapshotVersions` 中将 `<leagueUrl>` 映射为 `snapshotName`（即 `overview`）与 `type`（`"exp"` 或 `"depthsolo"`）。
     2. 将路径中 `<account>` 末尾 `-1234` 还原为 `Account#1234`（兼容 URL 编码）。
     3. 由 Python `httpx.get("https://poe.ninja/poe1/pob/raw/overview/code", params={"overview": ov, "type": t, "account": acc, "name": char_name, ...})` 直接获取 `poe.ninja` 生成的完整导出码（自带赛季配置、药剂开关与主技能定位，且天然由 `httpx` 完成 RFC 3986 百分号编码）。
     4. 调 `import_code{"code": code.strip(), "mode": "replace"}`。
4. **裸 Base64 导出码或 `@filepath`**：
   - 直接调 `import_code{"code": raw_code.strip(), "mode": "replace"}`。

### 4.2 试算策略：原生 Tooltip 快路 + 内存事务快照（`snapshot.py`）
按改动粒度分级处理，兼顾零状态污染与全身属性/抗性联动告警：
- **单一对象只读试算（`apply == False`）**：
  - 单件装备试穿（`pob item try` 无 `--apply`）：调用 `item_tooltip{"raw": text, "slotName": slot, "compare": true}` 提取原生悬浮对比词条（若为戒指/药剂等双槽/多槽装备且未指定 `--slot`，自动对所有候选槽位如 `Ring 1`、`Ring 2` 分别生成对比）；同时利用 `<15ms` 内存快照附带检查换装后是否触发**力量/敏捷/智慧不足红字、四抗跌破上限或丢失旧装备插槽技能**（传 `--tooltip-only` 可仅跑纯 Tooltip）。
  - 单个天赋节点预览：调用 `node_info{"id": id, "diff": true}` + `node_hover{"id": id}`，直接拿原生属性增减与过路点路径成本。
  - 单颗技能石启停/候选预览：调用 `gem_tooltip{"group": g, "index": idx}` 或 `gem_candidate_tooltip{"group": g, "index": idx, "gemId": id}`。
- **多部位联动修改或持久化落盘（`batch` / 多天赋变更 / 涂油 / 药剂组 / Config / 任何带 `--apply` 的操作）**：
  统一经过内存事务执行器 `run_mutation(worker, fn, apply=False)`：
  1. 记录修改前面板：`before = capture_digest(worker)`（聚合 `get_build_info`、`get_stats` 关键标量、`get_sidebar` 展示行与 `warnings`）。
  2. 保存内存快照：`snap_code = worker.call("export_code")["code"]`。
  3. 执行一连串修改回调 `fn(worker)`（期间若抛异常，立即调 `import_code{"code": snap_code, "mode": "replace"}` 恢复现场再向上报错）。
  4. 记录修改后面板：`after = capture_digest(worker)`。
  5. 分支处理：
     - 若 `apply == False`（默认 `what-if`）：立即调 `import_code{"code": snap_code, "mode": "replace"}` 无损回滚，不留任何脏状态。
     - 若 `apply == True`：将 `snap_code` 压入该槽位 `undo_stack`，清空 `redo_stack`，保留新状态。
  6. 计算并返回 `diff_digest(before, after)`。

### 4.3 核心 RPC 方法精确速查表（均已在 `bridge.lua` 验证）

| 领域 | `bridge.lua` 方法名 | 关键入参与边界契约 | 关键返回字段（注意键名！） |
| :--- | :--- | :--- | :--- |
| 握手与切换 | `version`, `new_build`, `get_build_info`, `set_build_field`, `set_active_spec`, `set_item_set`, `set_skill_set`, `set_config_set`, `select_loadout` | `set_build_field{field="level"\|"mainSocketGroup"\|"mainSkill"\|"mainSkillPart", value=...}` 与 `set_active_spec{index}` 为 **1-based 下标**；而 `set_item_set`/`set_skill_set`/`set_config_set`/`select_loadout` 接收内部 **`id`**（非顺序下标） | `version` -> `caps`, `buildLoaded`; `get_build_info` -> `level`, `classNameZh`, `ascendClassNameZh`, `points` |
| 快照与导出 | `export_code`, `import_code`, `save_xml` | `import_code{code, mode="replace"}`；严禁对返回字段做颜色码正则替换 | `export_code` -> `{code, bytes}`; `save_xml` -> `{xml}` |
| 面板与拆解 | `get_stats`, `get_sidebar`, `get_calcs`, `calcs_breakdown` | `calcs_breakdown{si, ui, ri, ci}`；召唤构筑需同时读取 `Minion*` 键与 `actor=="minion"` 侧栏行 | `get_stats` -> **`{stats, count, outputRevision}`**; `get_sidebar` -> **`{rows: [{lhs, lhsRaw, rhs, actor?}], warnings: [{text, raw}], rev, outputRevision}`** |
| 天赋与专精 | `get_tree_state`, `tree_data`, `list_specs`, `node_info`, `node_hover`, `tree_click`, `mastery_options`, `select_mastery`, `node_power` | `tree_click{id, effect?}` 是 **Toggle 开关**，点击前必查 `allocatedNodes` 防反向抵消，点击后必检 `needsMastery` / `needsConfirm`；`node_power{stat?, maxDepth?}` | `get_tree_state` -> **`allocatedNodes`**, **`overrides`** (`why=="mastery"`), `sockets` (`socketIndex` <-> `nodeId`), `points`; `node_power` -> `report` |
| 装备与涂油 | `list_items`, `item_raw`, `item_tooltip`, `parse_item_text`, `add_item`, `equip_item`, `set_slot_active`, `item_edit_begin`, `item_edit_popup`, `item_edit_commit`, `item_edit_cancel` | `add_item{raw, equip, slotName?}` 原生支持中文物品反向翻译；天赋珠宝槽名为 `"Jewel <nodeId>"`；涂油必先检 `item.actions`（支持 `anoint`..`anoint4`），再从 `open` 返回的 `options` 按中英名反查 `nodeId`（`bridge.lua` `notableDB` `pick` 传错 ID 会**静默不改**！） | `list_items` -> `{slots, items: [{id, actions, ...}], itemSets, activeItemSetId}` |
| 技能与寻优 | `list_skills`, `add_group`, `set_group`, `add_gem`, `set_gem`, `delete_gem`, `gem_search`, `gem_tooltip`, `gem_candidate_tooltip` | `gem_search{group, index, byDps=true, supportableOnly=true}`：新增第 6 连时传 `index = len(gems) + 1`，特定异常流派可先设 `sortGemsByDPSField` | `list_skills` -> `{groups: [{source?, ...}], mainSocketGroup, skillSets}`; `gem_search` -> `{gems, baseDps, dpsField}` |
| Config 配置 | `list_config`, `set_config`, `reset_config`, `set_custom_mods` | 敌人感电为 `conditionEnemyShocked`（`conditionShocked` 是玩家自感电！）；`config_committed` 先 `BuildModList` 后 `UpdateControls`，同批改 `enemyIsBoss` + `presetBossSkills` 需二次触发重算；禁用 `customMods` 时防 `ConfigTab.lua` 回退读旧 `input.customMods` | `list_config` -> `{sections, customMods, configSets, activeConfigSetId}` |

---

## 5. CLI 命令全景设计（`pob`）

所有命令默认针对 `--slot main`，默认输出干净紧凑的中文双语 Markdown 表格，传 `--json` 输出结构化 JSON。

### 5.1 会话管理与导入导出
- `pob status` / `pob stop`：查看常驻 Daemon 与槽位状态，或关闭引擎释放内存。
- `pob load <source> [--slot main|ref]`：智能加载任意 URL / `poe.ninja` 角色链接 / 本地 `.xml` / 导出码，加载后自动输出 15 行核心构筑摘要。
- `pob save <file.xml>` / `pob export-code`：Daemon 调 `save_xml{}` 返回 XML 字符串并由 **CLI 进程本地写入 `<file.xml>`**（防本地端口越权写盘），或导出 PoB 分享码。
- `pob switch-main [--group N] [--skill N] [--part N] [--spec N] [--item-set ID_OR_TITLE] [--skill-set ID_OR_TITLE] [--config-set ID_OR_TITLE] [--loadout ID_OR_TITLE]`：切换主技能组/阶段/天赋树（1-based 下标），或按序号/标题模糊匹配解析内部 `id` 切换装备集、技能集、Config 集及三合一 Loadout。
- `pob undo` / `pob redo`：基于内存快照栈回退或重做上一次 `--apply` 的修改。

### 5.2 构筑体检与深挖（对应场景 1）
- `pob summary [--slot main]`：输出职业升华、等级、天赋点数、主技能连法、核心输出（自动适配直伤/异常 `CombinedDPS`、多技能 `FullDPS`，以及召唤构筑 `MinionCombinedDPS`/`MinionTotalDPS` 与 `actor=="minion"` 侧栏行）、核心防御（生命/魔力/护盾/守卫、EHP、物理/三元/混沌最大承伤、四抗、法压/格挡/闪避/护甲、净秒回、技能蓝耗 vs 回蓝）及引擎告警。
- `pob audit [--slot main]`：**深度排雷与全景水分审计**——自动检查：① 四抗是否满、属性/插槽是否红字告警、命中是否 `<100%`、蓝耗是否大于回蓝/吸蓝；② **四大注水重灾区审计**——Config 水分（非 Boss 靶子、手填 `customMods`、硬勾 `conditionEnemyShocked`/满球/猛攻/肾上腺素/满震慑；保留 `bandit`/`pantheon*` 等固有项不误判）、非魔血（`Mageblood`）却全开 5 瓶药剂、常驻勾选临时护体/爆发/瓦尔技能（`Molten Shell`/`Steelskin`/`Arcane Cloak`/`Berserk`/`Vaal *`）、`PartyTab` 团队光环注入；并直接给出**切回标准 Pinnacle Boss + 剥离全部虚高项后的真实保底 DPS/EHP**。
- `pob items [--slot-name "Helmet"|"#1"] [--raw]`：列出全身装备槽位、药剂开关、天赋珠宝孔（支持 `#1`、`#2` 简写映射 `"Jewel <nodeId>"` 的 `socketIndex`）；传 `--slot-name` 查看单件装备完整词缀。
- `pob skills [--group N]`：列出全部技能组、插槽位置（标注 `source` 装备赋予来源）、宝石等级/品质/启用状态及主技能标记。
- `pob tree [--search <keyword>]`：输出已点基石天赋（Keystones）、核心大点（Notables）、全部已选专精（Masteries）、珠宝孔列表；带 `--search` 可按中英文搜全图天赋节点 ID 与位置距离。
- `pob calc <keyword>`：在 `get_calcs` 中按中英文关键字搜索指标（如 `"Mana Regen"` / `"魔力回复"` / `"Maximum Hit"`），自动拉取对应的 `calcs_breakdown` 来源构成明细表。

### 5.3 试改与前后对比（对应场景 2）
> 单一行为试看（不带 `--apply`）优先走原生 Tooltip 提取词条级对比，装备试穿默认同步跑 `<15ms` 内存快照补齐属性/四抗缺口告警；多处联动改动或带 `--apply` 的操作统一走内存快照事务重算完整前后对比表。

- `pob item try <item_text_or_file> [--slot "Ring 1"|"#1"] [--tooltip-only] [--apply]`：粘贴国服中文或国际服英文物品文本试穿（未传 `--slot` 遇戒指/药剂等多槽部位时自动遍历全部候选槽对比；珠宝支持 `#1` 简写）。默认输出 `item_tooltip` 悬浮对比词条 + 快照级全身面板/四抗/属性红字变化；传 `--tooltip-only` 仅跑纯 Tooltip。
- `pob item flask <1..5|all> <on|off> [--apply]`：开关指定药剂（测断药生存/输出）。
- `pob item anoint <notable_name> [--slot "Amulet"] [--index 1..4] [--apply]`：先校验目标物品 `actions`（拦截已腐化/已镜像不可涂油装备，支持菌潮装备/扼息者 `anoint`..`anoint4` 多涂油槽），再从 `item_edit_popup(open)` 返回的 `options` 按中英名精确匹配反查 `nodeId`（未命中立即报错，规避 `bridge.lua` 静默忽略无效 ID），提交后输出前后收益对比。
- `pob skill set <group> <gem_idx> [--name <gem>] [--level N] [--quality N] [--enabled true|false] [--apply]`：替换宝石或改等级/品质/启停（仅切开关或预览候选时可用 `gem_tooltip`/`gem_candidate_tooltip`）。
- `pob skill add <group> <gem_name> [--level 20] [--quality 20] [--apply]` / `pob skill rm <group> <gem_idx> [--apply]`：增删辅助/主动宝石。
- `pob tree change [--add id1,id2...] [--rm id3,id4...] [--mastery nodeId:effectId] [--apply]`：仅传单个节点且不带 `--apply` 时走 `node_info{id, diff=true}` + `node_hover{id}`；多节点或带 `--apply` 时，**每次点击前先读最新 `allocatedNodes` 做幂等过滤**（防自动过路点或级联摘除导致 `tree_click` 反向翻转抵消），显式拦截 `needsMastery` / `needsConfirm` 标志，回显实际变动节点与净点数消耗。
- `pob config set <key=value...> [--custom-mods "..."] [--apply]`：修改 Config 选项（如切换 `enemyIsBoss=Pinnacle`、`conditionEnemyShocked=false`；同批涉及 `enemyIsBoss` 与 `presetBossSkills` 时自动二次刷新控件依赖）。
- `pob batch <spec.json|stdin> [--apply]`：**复合组合试算**——支持在一个 JSON 事务里同时包含 `items` + `skills` + `tree` + `config` 多处联动改动（如“换掉项链 + 改涂油 + 扣 2 点敏捷点 + 换一颗辅助石”），一次性算出组合总收益并无损回滚或落盘。

### 5.4 原生寻优与跨构筑对比（对应场景 3）
- `pob rank-nodes [--stat CombinedDPS|TotalEHP|Life|Mana|MinionCombinedDPS] [--max-dist 3] [--top 15]`：调用引擎原生 `node_power`，输出当前过路距离内性价比最高的未点大点/小点排行（含每点平均收益）。
- `pob rank-gems [--group main] [--slot-idx N | --add-link] [--field CombinedDPS|TotalDot|IgniteDPS|BleedDPS...] [--top 15]`：调用引擎原生 `gem_search{byDps=true, supportableOnly=true}`（`--add-link` 自动传 `index = len(gems) + 1` 测新增第 6 连；可选切换 `sortGemsByDPSField`），列出真实提升最大的备选宝石。
- `pob compare <ref_source>`：将参考构筑加载进 `ref` 槽位，输出四合一深度对比报告：
  1. **三基准面板对比**：同时展示 ①「各自原始面板」、②「**外部靶子环境白名单对齐基准**（仅将 `ref` 的 `enemyIsBoss`/`enemyLevel`/`deliriousPercentage`/`presetBossSkills`/`enemy*Resist`/`EHPUnluckyWorstOf` 对齐到 `main`，绝不盲拷 `bandit`/`pantheon*` 等角色固有构筑项）」、③「**标准 Pinnacle Boss 全景去水分基准**（双方统一设 `enemyIsBoss="Pinnacle"` + 禁用 `customMods` 及旧回退字段 + 关闭极端虚高条件、非魔血常驻药剂、临时护体/爆发/瓦尔技能与团队光环）」。
  2. **天赋路线差异**：仅 `ref` 点了而 `main` 没点的基石、大点、专精、团簇/珠宝，以及反过来的差异。
  3. **主技能与光环差异**：双方主技能辅助连法差异、光环/诅咒/位移配置差异。
  4. **核心装备差异**：双方同部位暗金选择差异或稀有装核心词缀差异。
- `pob borrow [--item-slot "Helmet,Weapon 1"] [--skill-group main] [--apply]`：将 `ref` 槽位上的装备或技能组移植到 `main` 跑一次真实 `what-if`（或 `--apply`）：自动处理双手武器与单手+副手槽位冲突；若目标技能组由装备词缀赋予（`group.source` 非空）则提示或联动借穿对应部位装备；借入主技能组时自动切换 `mainSocketGroup` 指向新组确保面板真实重算。

### 5.5 轻量物价与机制百科（可选增强，复用 `POEMCP` 经验）
- `pob price <query> [-l league] [-c category]`：走 `poe.ninja` 公开 `dense/overviews` 接口（本地 `~/.cache/pob-cli/` 磁盘缓存 15 分钟），速查暗金/通货/技能石/团簇珠宝 Chaos 与 Divine 价格，辅助评估升级性价比。
- `pob wiki <topic>`：走 `poewiki.net`（磁盘缓存 24 小时）提取词条核心机制说明与公式。

---

## 6. 分阶段实施里程碑与硬性验收标准

开发严格分 4 个里程碑推进，**每个里程碑完成后必须跑通对应实测命令才允许进入下一阶段**：

### Milestone 1：无头引擎客户端 + 常驻 Daemon + 导入与体检闭环
- **交付物**：`pyproject.toml`、`bridge_client.py`、`daemon.py`、`importers.py`（URL/XML/裸码）、`analyzer.py`、`formatters.py`、`cli.py`（实现 `status`、`stop`、`load`、`summary`、`audit`、`items`、`skills`、`tree`、`calc`）。
- **硬性验收用例**（以 `https://poedb.tw/pob/AWqOLSSmyG` 为黄金基准）：
  1. `pob load "https://poedb.tw/pob/AWqOLSSmyG"` 成功识别为 Lv94 圣宗（Templar / Hierophant），输出零 `^7`/`^x` 乱码。
  2. `pob summary` 精确对齐引擎真值：Hit DPS 约 `49,019,541`、Total DPS inc. Ignite 约 `49,247,885`、Total Life `3,501`、Total Mana `9,075`、EHP `103,199`、Mana Cost `1,031`、Mana Regen `1,344.1`。
  3. 连续执行第 2 次 `pob summary` 端到端耗时 `< 200ms`（证明常驻 Daemon 生效，无二次冷启动）。
  4. `pob calc "Mana"` 成功通过 `get_calcs` + `calcs_breakdown` 输出魔力上限/魔力回复的具体来源构成（验证 R2 替代方案跑通）。
  5. `pob audit` 准确识别当前构筑的 Config 勾选项与切换标准 Pinnacle Boss 前后的数值变化。

### Milestone 2：内存快照事务引擎 + 全维度 `what-if` / `apply` / `undo`
- **交付物**：`snapshot.py`、`mutators.py`，打通 `item try/flask/anoint`、`skill set/add/rm`、`tree change`、`config set`、`batch`、`undo`、`redo`、`save`、`export-code`。
- **硬性验收用例**：
  1. 对 `AWqOLSSmyG` 执行一次默认 `what-if`（例如 `pob item flask all off` 或摘掉主技能一颗辅助石），验证输出完整前后对比表（含差值与百分比），紧接着跑 `pob summary` 验证 DPS 100% 回到 `49,247,885` 一字不差。
  2. 执行带 `--apply` 的复合 `pob batch`（同时改天赋 + 改技能等级 + 改药剂），验证面板改变，再执行 `pob undo` 验证跨标签一次性完整还原。
  3. 传入一段**国服简体中文**装备文本执行 `pob item try`，验证 `bridge.lua` 内置反向翻译生效并算出装备替换收益。

### Milestone 3：原生收益排行 + 跨构筑三基准对比 + `poe.ninja` 角色页导入
- **交付物**：`comparator.py`、`importers.py` 补齐 `poe.ninja` 角色主页 `index-state` + `raw/overview/code` 导入，CLI 打通 `rank-nodes`、`rank-gems`、`compare`、`borrow`。
- **硬性验收用例**：
  1. 在 `AWqOLSSmyG` 上跑 `pob rank-nodes --top 10` 与 `pob rank-gems --top 10`，返回按真实 DPS 排序的前 10 候选。
  2. 传入任意第二个同流派构筑链接（或故意注入 `customMods: "+500% more Damage"` / 虚开护体与药剂的变体）跑 `pob compare`，验证工具在「外部靶子环境白名单对齐」与「Pinnacle 全景去水分」列准确还原真实差距且不破坏对方 `bandit` 抗性。
  3. 跑 `pob borrow --item-slot "Weapon 1"` 成功将参考构筑主手武器临时借穿到 `main` 上并输出真实 DPS/抗性/属性变动表。
  4. 实测一条真实 `https://poe.ninja/poe1/builds/.../character/...` 链接完成一键导入与面板计算。

### Milestone 4：物价/百科辅助命令 + 项目级 `AGENTS.md` 收尾
- **交付物**：`external_data.py`（`pob price`、`pob wiki` 带本地磁盘缓存）、自动化回归测试套件 `pytest` 全绿、根目录电报风 `AGENTS.md`（让后续任何 Agent 打开 `<repo>` 即刻掌握 `pob` 全套命令与分析工作流）。
