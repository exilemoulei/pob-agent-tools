# pob4agents (`pob`)

**简体中文** | [English](README_EN.md)

`pob4agents` 是给 AI Agent 和《流放之路》1 玩家用的 Path of Building（PoB）命令行工具集，命令名为 `pob`。它能在后台运行 PoB，导入构筑、试配装备、比较天赋与技能，并查询游戏数据和交易价格。

伤害（DPS）、有效生命池（TotalEHP）、最大承伤（MaxHit），以及换装、改点、换宝石和永恒珠宝种子的收益，都交给实际运行的 PoB 引擎计算；工具不会只凭构筑 XML 里的词缀自行估算。

---

## 目录

- [主要功能](#主要功能)
- [安装与跨平台配置](#安装与跨平台配置)
  - [Windows x64 安装](#1-windows-x64-安装)
  - [macOS 与 Linux 安装](#2-macos-与-linux-安装)
  - [首次设置 PoB 引擎](#3-首次设置-pob-引擎)
- [命令选项与构筑管理](#命令选项与构筑管理)
- [功能与使用示例](#功能与使用示例)
  - [1. 检查构筑与 PoB 配置](#1-检查构筑与-pob-配置)
  - [2. 比较不同战斗场景 (`pob matrix`)](#2-比较不同战斗场景-pob-matrix)
  - [3. 试配装备、药剂、涂油、技能和天赋](#3-试配装备药剂涂油技能和天赋)
  - [4. 批量试算多套方案 (`pob batch` / `pob sweep`)](#4-批量试算多套方案-pob-batch--pob-sweep)
  - [5. 寻找天赋与宝石升级，比较两份构筑](#5-寻找天赋与宝石升级比较两份构筑)
  - [6. 搜索永恒珠宝种子 (`pob timeless`)](#6-搜索永恒珠宝种子-pob-timeless)
  - [7. 查询游戏数据、物价与构筑分布](#7-查询游戏数据物价与构筑分布)
  - [8. 搜索交易装备与工艺台词缀](#8-搜索交易装备与工艺台词缀)
- [退出码说明](#退出码说明)
- [本地开发、测试与发版](#本地开发测试与发版)
- [致谢与开源协议](#致谢与开源协议)

---

## 主要功能

- **安装与更新 PoB 引擎**（`pob bootstrap` / `pob update`）：Windows 使用 PobTools-zh；macOS 和 Linux 使用本机编译的 LuaJIT 运行时。工具会定期检查 PobTools-zh 和 PoB 的更新，更新失败时保留可用的旧版本。GitHub 暂时连不上，也可以继续使用本地引擎。
  - Windows x64 使用 `pob-zh.exe --engine-headless`，支持中英双语；macOS（Apple Silicon / Intel）和 Linux 使用原生编译的 `LuaJIT 2.1 + lua-utf8`，通过 `headless_runner.lua` 运行官方 PoB 与 `bridge.lua`。
  - 启动时及之后每 30 分钟检查一次更新：从 PobTools-zh 的 `302` 重定向获取版本，不调用 `api.github.com`；再按官方 `manifest.xml` 的 SHA1 增量下载 PoB 文件。更新会先通过 `gate_result` 检查，再刷新已加载的构筑槽位；失败则从 `.bak/` 回滚。
  - GitHub 无法访问时，连接会在 3 秒内失败并转用本地引擎；`stderr`、JSON `updateWarning` 和 Markdown 告警会说明更新状态。
- **同时管理多份构筑**：用 `--target <slug>` 给构筑命名，例如 `main`、`ref` 或 `mageblood_test`。每份构筑都有独立的撤销与重做记录；`pob serve` 可让引擎保持运行，省去反复启动的时间。
  - 槽位数量不再固定为两个，例如可同时使用 `main`、`ref`、`pinnacle_test`、`mageblood_test`；也可用环境变量 `POB_SLOT` 设置默认槽位。每个槽位保存 30 步 `undo` / `redo` 历史。
  - 引擎调用支持 `call_batch` 批处理；CLI 按需加载模块，并在发现失效进程锁时快速清理（目标 `<1ms`）。热查询通常以毫秒计。普通命令会自动启动后台进程；容器或 Agent 环境可改用 `pob serve` 前台常驻。
- **检查 PoB 面板有没有“水分”**（`pob audit` / `pob matrix`）：分别检查配置项、药剂、临时或瓦尔技能、组队增益对面板的影响，并用 PoB 重新计算每项带来的 DPS 和有效生命变化。法师之血提供的魔法药剂效果、基础技能和常驻光环会按实际情况保留。确定能稳定触发的条件可用 `--keep` 留下；检查结束后会核对构筑是否恢复原样。
  - 审计会逐项关闭 PoB Config 配置、非常驻药剂、临时防御或爆发技能、瓦尔爆发和组队增益，再单独恢复每一项，测出它对 DPS / EHP 的影响。瓦尔技能会关闭爆发版本 `enableGlobal1`，保留基础技能 `enableGlobal2`，必要时切换 `mainActiveSkill`。
  - 自定义词缀 `customMods` 按分组禁用，不改动原有标题和文本；内部使用哨兵分组维持配置结构。每次重算前会回读配置，确认改动生效。`--keep` 可指定单个条件、药剂、技能或 `customMods:<分组名>`；条件拼错时会在 `keepRejected` 中提示。最后用 `verify_restored` 核对原构筑。
- **试配和批量比较**（`pob batch` / `pob sweep`）：换装备、改技能、调整天赋与配置默认只做试算，结束后恢复原构筑。加 `--apply` 才保存修改。也可以把多项改动组合起来，或一次比较多套配装和技能连法。
  - 单步和批量改动都在内存中的构筑快照上运行。`pob batch` 可组合 19 类操作并返回指定分析视图；`pob sweep` 在同一个已启动的引擎中比较多套配装、天赋或技能连法，单套方案出错不会中断其他结果。
- **搜索永恒珠宝种子**（`pob timeless list` / `pob timeless search`）：按已点亮的珠宝孔和天赋，用 PoB 逐个计算候选种子的收益；支持中文和英文珠宝名。需要遍历所有候选时可加 `--exact`。
  - 从 PoB 的 `Data/Uniques/jewel.lua` 读取 11 种永恒珠宝数据。对致命的骄傲、残酷的约束、军团的信仰、优雅的狂妄、英雄的悲剧等珠宝，先按已分配及涂油取得的核心天赋去重，再预筛候选种子；`--exact` 跳过预筛。搜索可用多个临时引擎进程并行运行，并处理其他珠宝孔中冲突的 `Historic` 珠宝。
- **查询游戏数据和物价**（`pob data` / `pob price` / `pob meta` / `pob poedb` / `pob wiki`）：本地中英双语索引可查宝石、传奇装备、词缀和底材；联网时还能查 poe.ninja 物价与构筑分布、流亡编年史和 PoE Wiki。
  - 本地数据库由 PoB 的 Lua 数据建立 SQLite/FTS5 索引；无需启动 PoB，查询通常在 15ms 内完成。覆盖秽生传奇变体、工艺台/古灵/腐化词缀和中英名称互查，一条命令可同时查多个词。
- **找装备、补词缀**（`pob trade` / `pob bench`）：搜索官方交易市集的在售装备，用 PoB 比较换装收益；还能查看工艺台可补的词缀，并试算丰收同阶元素抗性转换。交易功能只负责搜索和计算，不会代替玩家购买。
  - `TradeSiteStats.lua` 在本地匹配中英文交易词缀，并区分装备局部词缀与全局词缀；交易搜索遵守 GGG 响应头给出的请求频率限制，支持 3.30 一口价搜索和 `chaos_equiv` 折算。
  - `pob item inspect` 识别稀有装备的前后缀、复合词缀和现有大师工艺；占用不确定时不会假定存在空位。交易搜索可用 `--bench-craft` 对可添加或替换的工艺按最高数值试算，并比较加工后的 PoB 收益。
  - `--allow-conversion` 只在满足条件时考虑丰收同阶转抗，并检查词缀组冲突；`--sum` 支持官方市集的加权和（Weighted Sum）词缀分组与按总和降序排序；候选装备的 DPS / EHP 变化由 PoB 批量计算。`pob bench` 结合 `ModMaster.lua` 与本地费用/解锁数据给出可验证的工艺台词缀建议。
- **一键构筑诊断、防幻觉校验与通用 Agent Skill**（`pob diagnose` / `pob ground` / `skills/pob`）：面向快速/轻量 AI 模型设计，单条命令输出去水面板、升华与血脉词条、全技能组、0% 失效或互斥辅助石、`0.033s` 触发冷却断点与超速（Overspeed）检查、低血防混沌穿透失效等构筑隐患，并拦截《流放之路 2》及 3.23–3.25 已移除机制的跨版本误用。
  - `pob diagnose` 支持 `-c/--compare` 单命令双构筑去水横评与 `-q/--ask` 问题伴随核对；每次构筑输出附带 `[Env: PoE1 3.30 (NOT PoE2) | ... (rev=N)]` 状态标记，避免多轮长对话混淆历史面板。仓库内置跨宿主通用的 [`skills/pob/SKILL.md`](skills/pob/SKILL.md)，可在 DeepSeek Harness、Claude Code、Codex、OpenCode、Cursor 等环境中直接加载。

---

## 安装与跨平台配置

### 1. Windows x64 安装

Windows 独立打包版内置运行环境，**无需安装 Python**。

#### 方式一：PowerShell 一键安装 / 升级（推荐）

```powershell
irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
```

若访问 GitHub 需要代理：

```powershell
$env:POB_PROXY = "your_proxy_here"
irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
```

以后要升级，重新运行这条命令即可。安装脚本会校验 SHA256，并停止旧版后台进程。

#### 方式二：Scoop 包管理器

仓库提供 Scoop 清单（`bucket/pob.json`），发布新版本时会自动更新：

```powershell
scoop bucket add pob https://github.com/exilemoulei/pob4agents
scoop install pob

# 后续升级：
scoop update pob
```

#### 方式三：GitHub Releases 便携压缩包

前往 [GitHub Releases](https://github.com/exilemoulei/pob4agents/releases/latest) 下载 `pob-v0.6.0-windows-x64.zip`，解压后将包含 `pob.exe` 的目录加入系统 `PATH`。

#### 方式四：Python 包安装（Python 3.11+，`uv` / `pipx` / `pip`）

```powershell
# 使用 uv（推荐）
uv tool install "git+https://github.com/exilemoulei/pob4agents.git@v0.6.0#subdirectory=pob_cli"

# 或直接安装 Release Wheel
pip install "https://github.com/exilemoulei/pob4agents/releases/download/v0.6.0/pob_cli-0.6.0-py3-none-any.whl"
```

> **为 AI Agent 挂载通用 Skill（可选，推荐）**：将仓库内的 [`skills/pob`](skills/pob) 目录复制到你的 Agent 技能目录（如 `~/.agents/skills/pob`、`~/.claude/skills/pob` 或 DeepSeek Harness 的 `skills` 目录），即可让快速模型（如 DeepSeek Flash / Hydrogen / Haiku）按精简路由调用 `pob` 并规避跨版本幻觉。

---

### 2. macOS 与 Linux 安装

在 macOS（Apple Silicon `arm64` / Intel `x86_64`）或 Linux（`x86_64` / `aarch64`）上，先安装 Python 3.11+、`cc`、`make`、`git` 和系统 `zlib` 头文件，再编译一次 `LuaJIT 2.1 + lua-utf8`：

```bash
git clone https://github.com/exilemoulei/pob4agents.git
cd pob4agents

# 1. 安装 pob CLI
pip install "./pob_cli"

# 2. 编译原生 LuaJIT 2.1 + lua-utf8 运行时并放入托管引擎目录
# macOS 默认运行态目录为 ~/.local/share/pob4agents（或自定义 POB_RUNTIME_DIR）
export POB_RUNTIME_DIR="$HOME/.local/share/pob4agents"
chmod +x pob_cli/packaging/build_posix_runtime.sh
./pob_cli/packaging/build_posix_runtime.sh ./runtime-dist
mkdir -p "$POB_RUNTIME_DIR/engine"
cp -R ./runtime-dist/bin "$POB_RUNTIME_DIR/engine/bin"

# 3. 下载 PoB 数据与 bridge.lua，并检查引擎能否正常运行
pob bootstrap --no-zh
pob status
```

---

### 3. 首次设置 PoB 引擎

安装 `pob` 后，运行一次 `pob bootstrap` 下载 PoB 引擎。已有本地 `PobTools` 的话，也可以从该目录导入：

```powershell
pob --version
pob bootstrap                                   # 直连 GitHub 下载 PobTools-zh + 官方 PoB
pob bootstrap --proxy http://127.0.0.1:7890     # 通过 HTTP / SOCKS5 代理下载
pob bootstrap --from-dir "D:\PoE\PobTools"      # 从本地已有 PobTools 目录离线导入种子
pob update --check                              # 检查上游是否有新版本
pob update --proxy http://127.0.0.1:7890        # 更新引擎，并刷新已加载的构筑槽位
```

常用环境变量一览：

| 环境变量 | 说明 |
|---|---|
| `POB_ROOT` | 显式指定无头引擎根目录（默认自动查找 `<POB_RUNTIME_DIR>/engine`） |
| `POB_RUNTIME_DIR` | 守护进程状态、引擎与缓存根目录（安装版默认 `%LOCALAPPDATA%\pob4agents`，自动兼容复用旧版 `pob-agent-tools` 数据；源码检出默认 `<repo>/.runtime`） |
| `POB_CACHE_DIR` | 指定本地游戏数据库及联网查询的缓存目录 |
| `POB_SLOT` | 默认目标构筑槽位名（未传 `--target` 时生效，默认为 `main`） |
| `POB_PROXY` | 网络请求代理（支持 `http://`、`https://`、`socks5://`；亦兼容 `HTTPS_PROXY` / `ALL_PROXY`） |
| `POB_GITHUB_MIRROR` | GitHub 下载镜像前缀（加速 `bootstrap` / `update`） |
| `POB_OFFLINE` | 设为 `1` 时跳过启动期与定时 GitHub 更新检查（等价于全局 `--offline`） |
| `POESESSID` / `POE_COOKIE` | 官方交易接口返回 401/403 时可按需设置；请只通过环境变量提供，不要写进命令参数或日志 |

---

## 命令选项与构筑管理

### 输出格式与构筑槽位

命令可以输出 JSON、压缩为单行，或直接写入 UTF-8 无 BOM 文件。在 Windows 上，直接写文件也能避开 PowerShell 重定向可能造成的编码问题：

```powershell
# 全局参数放在 pob 之后；多数子命令也支持同名选项
pob --offline --compact --out result.json summary --json

# 查看守护进程 PID、PoB / PobTools 版本、更新状态及当前所有已加载槽位
pob status
pob status --json
```

### 让 PoB 引擎保持运行 (`pob serve`)

一般情况下，运行构筑命令时会自动启动后台进程。如果你的 Agent 环境会在命令结束后清理后台进程，可在独立会话中运行 `pob serve`，让引擎保持在线。服务只监听本机 `127.0.0.1`，并使用 `X-PoB-Token` 验证请求：

```powershell
# 在独立会话中启动，让引擎持续运行
pob serve --offline

# 停止后台进程，释放引擎占用的内存
pob stop
```

### 导入、创建和导出构筑

`pob load` 支持本地 `.xml` / `.txt` 文件（直接传路径或 `@path`）、原始 Base64 分享码，以及 `pobb.in`、`poedb.tw/pob`、`poe.ninja/pob`、`maxroll.gg/poe/pob`、`pob.codes`、`pastebin.com`、`pastebinp.com`、`rentry.co` 这 8 个分享站。`poe.ninja` 角色主页链接也能直接导入：

```powershell
# 1. 将构筑导入默认的 main 槽位
pob load "https://poedb.tw/pob/AWqOLSSmyG"
pob load "@D:\builds\my_chieftain.xml"
pob load "https://poe.ninja/poe1/builds/mirage/character/AccountName-1234/CharName"

# 2. 用 --target 管理其他构筑，例如用于对比的 ref、用于试配的 exp1
pob load "https://pobb.in/xxxxxx" --target ref
pob new --class Marauder --asc Chieftain --level 95 --target exp1

# 3. 切换同一构筑中的天赋树、装备集或技能集
pob switch-main --loadout "Endgame Mageblood"
pob switch-main --spec 2 --item-set 2 --skill-group 1

# 4. 导出 XML 文件或 PoB 分享码
pob save "D:\builds\optimized.xml"
pob export-code --target exp1
```

---

## 功能与使用示例

### 1. 检查构筑与 PoB 配置

拿到别人的 PoB，推荐直接用 `pob diagnose` 做一键完整体检（同时输出原面板与去水后的巅峰首领基准、升华与血脉词条、全技能组与失效辅助检查、`0.033s` 触发断点及候选提升点），或用 `--compare` 一次对比两套构筑；也可以分别运行 `pob summary` 和 `pob audit`（这里把暂时关掉临时条件、重算常态表现称为“去水”）：

```powershell
# 一键加载并诊断单套构筑（-q 可附带玩家问题做版本陷阱与实体词条核对）
pob diagnose "@D:\builds\my_build.xml" -q "为什么打Boss伤害低而且容易被秒？"

# 一键对比两套构筑在三组基准（原面板 / 对齐环境 / Pinnacle 去水）下的表现与隐患
pob diagnose "@D:\builds\build_a.xml" --compare "@D:\builds\build_b.xml"

# 基础攻防与召唤物摘要 + PoB 原生告警
pob summary

# 逐项关闭临时条件，查看各项对 DPS / 有效生命的影响
pob audit

# 只看检查结果，跳过逐项重算
pob audit --no-attribution
```

如果构筑确实能稳定满足某些条件，例如站定、持续获得耐力球，或让敌人保持点燃，可以用 `--keep` 保留。PoB 的自定义词缀分组也能单独保留：

```powershell
# 保留多个配置项、指定药剂槽、整套药剂、特定技能或单个 customMods 分组
pob audit --keep conditionStationary,useEnduranceCharges,conditionEnemyIgnited
pob audit --keep "Flask 1,Vaal Haste,customMods:Boss Setup"

# 条件名拼错时，keepRejected 会给出建议
pob audit --keep conditionStationery --no-attribution
```

想进一步查看装备、技能、天赋、配置项或计算过程，可以用这些只读命令：

```powershell
# 查看当前配置，或搜索配置项有哪些可选值
pob config list
pob config list --all --search "boss" --with-options

# 查看全身装备、单件装备，或导出装备与珠宝的原始文本
pob items
pob items --slot "Weapon 1" --raw
pob items --all --raw

# 诊断装备前后缀占用，以及可添加或替换的大师工艺
pob item inspect --slot "Ring 2" --want "Cold Resistance"
pob item inspect --file copied-item.txt --slot gloves --want "maximum Life"

# 查看全部技能组，或主技能组中每颗宝石的详细信息
pob skills
pob skills --group main

# 搜索天赋节点、查看专精和珠宝孔附近的天赋
pob tree --search "最大生命" "法术压制" --unallocated --max-cost 4 --show-path
pob tree --type Mastery --allocated --masteries
pob tree --socket-radius 61419

# 查看 PoB「计算」页中的数值和来源；可同时查多个关键词
pob calc "Average Hit" "Spell Suppression" "Mana Regen"
```

---

### 2. 比较不同战斗场景 (`pob matrix`)

一次运行就能比较同一份构筑在**当前配置 (`Original`)**、**去水后的 84 级巅峰首领 (`Pinnacle`)**、**去水后的 85 级超级首领 (`Uber`)**、**关闭药剂 (`FlasksOff`)**等场景下的表现，也可以添加自定义场景。比较结束后会核对构筑是否恢复原样。默认显示常用指标；`--full` 可输出 200 多项原始统计：

```powershell
# 默认四场景对比（Original / Pinnacle / Uber / FlasksOff）
pob matrix

# 在 Pinnacle/Uber 场景中保留站定与药剂，并追加自定义满层枯萎与关熔岩护盾场景
pob matrix Original Pinnacle Uber `
  -s "Wither15:Pinnacle,MultiplierWitheredStackCount=15" `
  -s "NoGuard:Pinnacle,conditionMoltenShell=false" `
  --keep conditionStationary,flasks

# 输出完整统计数据；默认只显示常用指标
pob matrix Original Pinnacle --full --json --out matrix_full.json
```

---

### 3. 试配装备、药剂、涂油、技能和天赋

换装备、改天赋、换宝石等命令默认只做试算：显示改动前后的差值，随后恢复原构筑。想保留修改，再加 `--apply`；保存后可用 `pob undo` / `pob redo` 撤销或重做。项链涂油使用 PoB 自己的涂油流程，并校验核心天赋是否合法；改天赋时也可指定专精的 `effectId`。配置命令兼容旧首领别名、下拉菜单的 `None` 选项，以及向现有 `customMods` 追加词缀。

```powershell
# --- 试穿装备、调整词缀、切换药剂与项链涂油 ---
# 粘贴游戏内 Ctrl+C 的物品文本试穿对比
pob item try --slot "Amulet" --file .\candidate_amulet.txt

# 在现有装备上试着替换、新增或删除词缀
pob item edit --slot "Ring 1" `
  --replace-mod "Cold Resistance" "+48% to Lightning Resistance" `
  --add-mod "+55 to maximum Life"

# 搜索 PoB 内置传奇（暗金）装备并试穿，支持中英文名称
pob item db "法师之血" --slot Belt --equip
pob item flask all off

# 试算项链涂油
pob item anoint --slot Amulet --notable "Whispers of Doom"

# --- 调整技能宝石或替换整组技能 ---
# 新增宝石；瓦尔宝石默认只启用基础技能，不计入瓦尔爆发
pob skill add --group main --gem "Awakened Elemental Focus Support" --level 5 --quality 20
pob skill set --group main --gem 1 --level 21 --quality 20
pob skill new-group --label "Aura" --slot "Helmet" `
  --gem "Determination:21:0" --gem "Grace:21:0" --gem "Enlighten Support:4:0"
pob skill replace-group --group main `
  --gem "Ice Nova of Frostbolts:21:20" `
  --gem "Archmage Support:20:20" `
  --gem "Spell Echo Support:20:20"

# --- 改天赋、专精与升华 ---
pob tree change --allocate "26725,49080:64315" --deallocate "12345"
pob tree set-class Witch Occultist --secondary-asc None

# --- 调整 PoB 配置；--append-mods 会追加自定义词缀，不覆盖原有内容 ---
pob config set enemyIsBoss=Pinnacle conditionStationary=true --append-mods "+15% more Damage"

# 保存修改，再撤销或重做
pob item anoint --slot Amulet --notable "Charisma" --apply
pob undo
pob redo
```

---

### 4. 批量试算多套方案 (`pob batch` / `pob sweep`)

#### `pob batch`：把多项改动放在一起试算

一次 `pob batch` 可以组合下面 **19 类操作**，并返回所需的面板或检查结果：`config`、`flask`、`item`、`item_db`、`item_edit`、`unequip`、`anoint`、`tree`、`tree_reset`、`import_tree_url`、`skill`、`add_group`、`delete_group`、`set_group`、`replace_group`、`new_build`、`set_level`、`set_class`、`switch_main`。

```powershell
# 一次试算换上猎首、涂油魅力并调整配置，返回面板、检查结果和场景对比
pob batch --json-ops '[
  {"op": "item_db", "query": "Headhunter", "slot": "Belt"},
  {"op": "anoint", "slot": "Amulet", "notable": "Charisma"},
  {"op": "config", "set": {"enemyIsBoss": "Pinnacle", "conditionStationary": true}}
]' --keep conditionStationary --return "summary,audit,matrix,calc:DPS;Mana"
```

想看每项临时条件分别影响多少数值，可把 `--return` 中的 `audit` 改为 `audit:full`。批量生成构筑时，也可以用 `--apply --no-undo` 保存结果并跳过撤销快照。

#### `pob sweep`：一次比较多套方案

可以比较不同配装、涂油、辅助宝石或升华路线打巅峰首领时的表现。某套方案出错，不会影响其他方案的结果：

```powershell
pob sweep --scenario Pinnacle --keep conditionStationary --calc "Mana" --json-variants '[
  {"name": "Baseline", "ops": []},
  {"name": "Anoint_Whispers", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Whispers of Doom"}]},
  {"name": "Anoint_Sovereignty", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Sovereignty"}]},
  {"name": "Mageblood_Swap", "ops": [{"op": "item_db", "query": "Mageblood", "slot": "Belt"}]}
]'
```

---

### 5. 寻找天赋与宝石升级，比较两份构筑

```powershell
# 1. 按需要花费的天赋点，查找值得点的天赋（DPS / 有效生命 / 生命）
pob rank-nodes --stat CombinedDPS --max-dist 5 --top 15
pob rank-nodes --stat TotalEHP --max-dist 4 --top 10

# 2. 比较替换辅助宝石或新增一连（--add-link）的收益
pob rank-gems --group main --slot 5 --field FullDPS --top 10
pob rank-gems --group main --add-link --field CombinedDPS --top 10

# 逐条试加词缀（默认 16 条，--mod 可自订），查看每条带来的 DPS / 有效生命收益
pob weights --scenario Pinnacle
pob weights --mod "+1 to Level of all Skill Gems" --mod "20% increased Area of Effect"

# 3. 比较原面板、统一战斗条件后的面板，以及去水后的巅峰首领面板
pob compare "https://pobb.in/ReferenceBuildCode"

# 4. 试穿 ref 构筑的装备，或试用它的主技能组
pob borrow --item-slot "Weapon 1,Amulet,Ring 1" --skill-group main
```

---

### 6. 搜索永恒珠宝种子 (`pob timeless`)

想知道某颗永恒珠宝种子对当前构筑究竟提升多少，可以用 `pob timeless` 搜索。它会把候选种子放进 PoB 重新计算，计入天赋被替换后失去的原有收益，而不是只把新词缀的数值相加。支持 PoB 数据中的 11 种永恒珠宝：

```powershell
# 1. 查看支持的珠宝名称和当前构筑已点亮的珠宝孔 ID
pob timeless list

# 2. 在指定珠宝孔搜索种子；珠宝名可用中文或英文
pob timeless search `
  --jewel "致命的骄傲" `
  --socket 61419 `
  --conqueror Kaom `
  --stat CombinedDPS `
  --top 15 `
  --workers 4 `
  --idle

# 3. 按指定属性搜索，也可以附加需要的珠宝词缀
pob timeless search `
  --jewel "Heroic Tragedy" `
  --socket 26196 `
  --conqueror Vorana `
  --stat Ward `
  --mod "+10 to all Attributes" `
  --top 10

# 4. 用 --exact 遍历全部候选种子
pob timeless search --jewel "Militant Faith" --socket 41263 --stat TotalEHP --exact --workers 4
```

> **性能与内存**：`pob timeless list` 通常在 100ms 内返回。每个并行运行的 PoB 进程约占 1.3–1.7 GB 内存。`--workers` 默认取 CPU 核心数的一半，最多 4 个进程；后台搜索时可加 `--idle`，降低 CPU 占用优先级。

---

### 7. 查询游戏数据、物价与构筑分布

这些查询命令一次可接收多个关键词。本地游戏数据无需启动 PoB；poe.ninja 物价覆盖 52 个分类并缓存 15 分钟，流亡编年史查询可查看词缀池、权重和化石标签；百科查询也会在本地缓存结果：

```powershell
# --- 本地游戏数据库：宝石、品质阈值、传奇词缀反查、天赋树、帧率断点与中英名称 ---
pob ground "纯冰法术用神圣祝福开憎恨还有收益吗？"
pob data gem "Ice Nova of Frostbolts" "Archmage Support"
pob data gem --quality-breakpoints --active-only --limit 20
pob data unique "Mageblood" "Headhunter"
pob data unique --mod "Chaos Damage taken does not bypass"
pob data tree "Polymath" "Oath of Summer" --type Ascendancy
pob data tick coc --cdr 14 --aps 8.28
pob data mod "Spell Suppression" --slot Helmet --ilvl 85
pob data base "Spirit Shield" --limit 10
pob data translate "喋血之刃" "Whispers of Doom"

# --- 流亡编年史（poedb.tw）：查词缀池和权重 ---
pob poedb "傲视头冠" --mods --lang cn
pob poedb " Hubris Circlet" --mods --lang us

# --- poe.ninja：查物价和热门构筑分布 ---
pob price "Mageblood" "Progenesis" "Nimis"
pob price "Forbidden Flame" --variant "Hierophant" --limit 30
pob price "Awakened Spell Echo Support" --detailed
pob meta --skill "Ice Nova of Frostbolts" --top 15

# --- PoE Wiki：查机制说明 ---
pob wiki "Eldritch Battery" "Leech"
```

---

### 8. 搜索交易装备与工艺台词缀

换装前，可以先用 `pob audit` 看缺哪些抗性或属性，再用 `pob bench` 查工艺台能否补上。需要买装备时，用 `pob trade search` 搜官方交易市集的一口价物品，再让 PoB 比较候选装备的 DPS 和有效生命。交易搜索只读，不会自动购买。详见 [docs/TRADE.md](docs/TRADE.md)。

```powershell
# 1. 查询中文词缀在官方交易市集对应的筛选项
pob trade stats "攻击速度" --slot weapon1
pob trade stats "法术压制率" --slot gloves

# 2. 搜索在售手套；--allow-conversion 同时考虑丰收同阶转抗
pob trade search --slot gloves --budget 25 --currency chaos_equiv `
  --stat "冰霜抗性=40" `
  --stat "最大生命=70" `
  --want "Attack Speed" `
  --allow-conversion `
  --sort pob --pob auto

# 3. 用加权和（Weighted Sum）搜索补抗戒指并按抗性总和排序，或搜索有空余后缀的装备配合工艺台
pob trade search --slot ring1 --budget 5 --currency divine --stat "最大生命>=60" --sum "火抗|冰抗|电抗>=80" --search-sort sum
pob trade search --slot ring1 --budget 15 --stat "最大生命=65" --open-suffix --pob auto
pob bench list --slot ring1 --affix suffix --search "抗性"
pob bench suggest --slot gloves --stat "冰霜抗性"

# 同时比较原商品与添加／替换大师工艺后的装备（最高数值）
pob trade search --slot gloves --budget 20 --currency divine --stat "to Dexterity=55" --bench-craft "Area Damage" --pob on

# 4. 预览丰收转换元素抗性后的物品文本；转换后的数值仍需重骰
pob trade convert --file .\dropped_gloves.txt --to lightning
```

`--bench-craft` 可重复指定工艺方向，默认按 PoB 收益排序；`--pob-limit` 限制原商品数量，每件商品可生成多个加工方案。`--budget` 仅限制挂牌价，工艺、移除与重复加工费用另计；`--stat` 硬性条件筛选加工前的装备。JSON 的 `bench_craft` 包含加工词条、移除词条、费用、最高数值标记和原商品文本。

`pob item inspect` 只读，不会实际加工装备。无法匹配、存在歧义或未知底材时，空词缀数量会标为未知；工艺建议须核对装备。粘贴文本无法推断装备类型时，请提供 `--slot`。交易加工试算会跳过无法确认占用、腐化、镜像及不支持的特殊加工情况。

---

## 退出码说明

写脚本或让 Agent 调用时，可以根据退出码判断下一步。命令报错时会显示简短提示，不会直接打印 Python Traceback：

| 命令范围 | 退出码 | 含义与处理方式 |
|---|---|---|
| **常规与构筑命令** | `0` | 执行成功 |
| | `1` | 参数或当前构筑状态不符合要求，例如尚未初始化引擎、槽位没有装备或分享码损坏；检查参数或先加载构筑 |
| | `5` | 后台进程、PoB 引擎或网络出错；按报错提示处理，必要时查看 `daemon.log` |
| **`pob trade` 系列** | `0` | 搜索或转换成功 |
| | `1` | 参数或词缀过滤条件无法映射（附相近词缀建议） |
| | `2` | 没有符合条件的在售装备；可放宽词缀数值或预算 |
| | `3` | 官方交易接口返回 401/403，或被 Cloudflare 拦截；按需设置环境变量 `POESESSID` |
| | `4` | 触发交易接口的请求频率限制；按 `Retry-After` 提示等待后重试 |
| | `5` | 官方交易接口出错或网络超时 |
| | `6` | 显式指定 `--pob on` 时本地无头 PoB 实测失败 |

---

## 本地开发、测试与发版

```powershell
# 本地可编辑模式安装开发依赖
pip install -e "./pob_cli[dev]"

# 运行全量单元与无头集成测试（离线模式可设 $env:POB_OFFLINE="1"）
python -m pytest pob_cli/tests -v

# 本地构建 Windows x64 独立单目录可执行包（dist/pob/pob.exe）
pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec
```

**按版本标签发布**（`.github/workflows/release.yml`）：

1. 修改 `pob_cli/src/pob_cli/__init__.py` 中的 `__version__`，并合入 `main`。
2. 创建并推送版本标签：`git tag -s v0.5.0 -m "pob4agents v0.5.0" && git push origin main v0.5.0`。
3. GitHub Actions 检查版本号、运行测试、构建并冒烟测试 `pob-vX.Y.Z-windows-x64.zip`、Wheel / sdist 和 `SHA256SUMS.txt`，随后发布 GitHub Release，并更新 Scoop 清单 `bucket/pob.json`。

Agent 命令速查见 [AGENTS.md](AGENTS.md)。

---

## 致谢与开源协议

### 上游项目与灵感来源

- 架构灵感源自 [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp)（GPL-3.0）与 [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP)。
- [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh) 提供中文引擎与无界面桥接（`pob-zh.exe --engine-headless` + `bridge.lua`）；构筑计算与游戏数据来自 [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding)。

### 社区贡献致谢

- **[@ChangMenQAQ](https://github.com/ChangMenQAQ)**（[PR #1](https://github.com/exilemoulei/pob4agents/pull/1)）：官方交易搜索、丰收同阶转抗与工艺台词缀方案。
- **[@restartgr](https://github.com/restartgr)**（[PR #2](https://github.com/exilemoulei/pob4agents/pull/2)、[PR #4](https://github.com/exilemoulei/pob4agents/pull/4)、[PR #6](https://github.com/exilemoulei/pob4agents/pull/6)）：面板“去水”分析、`--keep` 条件保留、构筑恢复校验、错误退出码与中英文文档润色。
- **[@Capoouo](https://github.com/Capoouo)**（[PR #3](https://github.com/exilemoulei/pob4agents/pull/3)）：永恒珠宝种子搜索与候选种子筛选。

### 开源协议

本项目采用 **GNU General Public License v3.0（GPL-3.0）**，详见 [LICENSE](LICENSE)。
