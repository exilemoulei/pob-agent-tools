# pob4agents (`pob`)

**简体中文** | [English](README_EN.md)

`pob4agents` 是专为AI打造的 [**PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding) 工具

玩家只要通过与AI agents对话导入构筑，就能让agent真的帮你跑一遍pob

碰上不懂的还会去[编年史](https://poedb.tw)和[wiki](https://www.poewiki.net)，而不是和往常一样空口瞎编

它会给你诸多优化建议，还能找找对你最有提升的装备，直接提供市集购买链接

甚至还能帮你找永恒珠宝！

---

## 该怎么安装？

直接告诉你的agent：

> 我要安装 exilemoulei/pob4agents ，请帮我装好所有依赖

以后出新版本不用重装，让agent帮你跑一下 `pob self-update` 就行

## 什么是agent？

最常见的客户端有Deepseek Harness (DSH)、Codex (ChatGPT)、Claude Code

如果你不知道这是什么，去b站搜搜教程吧

## 我该怎么用？

安装完成之后，新开对话窗，给它提供你的 poe.ninja / 国际服或国服官网角色链接 / pob导出链接，它都能认

（国服看下面导入 Cookie 的方法。另外官网接口拿不到盗贼和万神殿，记得确认下）

尽量选择聪明点的模型（推荐Neon/Opus/Sol），不过你只有大肥鱼也没关系

可以多告诉它你的角色是什么bd、有什么特别容易搞错的核心机制

让它多问问你，实测会让准确性大幅提升！

它会自动挤掉bd水分，还原真实数据

但有时候也会做得太过火，请务必确认一下

对话不宜跑太长，差不多到四五轮就建议新开一个窗口

跑不出来也不用急，poe是个很复杂的游戏，请多多反馈给我们

我们会尽量多做支持，争取让agent能理解大部分的bd

## 我想让它帮我找装备，还有你说的永恒珠宝

那你需要导入一下 POESESSID，这样才能让它功能全开（国服玩家想直接导官网角色，或者国际服角色设了私密，也一样需要）

### 导入方法：

在官网或交易网页上打开 F12，点击“应用”标签页，找到 储存 → Cookie，点进去找到 `POESESSID`，把那串值复制出来

- **国际服玩家**：复制完直接发给你的 agent，让它帮你配进本机环境变量 `POESESSID` 就行
- **国服玩家（`poe.game.qq.com`）**：同理复制完发给 agent，**顺口说一句是国服**，让它帮你配进本机环境变量 `POE_CN_SESSID`

> [!CAUTION]
> 千万不要把 POESESSID 发给别人，也别在直播或截屏里露出来！这相当于你的账号凭证，请谨慎保管。

---

## 致谢与开源协议

- **上游与灵感**：[**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp)（GPL-3.0）、[**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP)、[**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh)（中文无头引擎）、[**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding)。
- **社区贡献**：
  - [**@ChangMenQAQ**](https://github.com/ChangMenQAQ)（[PR #1](https://github.com/exilemoulei/pob4agents/pull/1)）：官方交易搜索、丰收同阶转抗与工艺台词缀方案。
  - [**@restartgr**](https://github.com/restartgr)（[PR #2](https://github.com/exilemoulei/pob4agents/pull/2)、[PR #4](https://github.com/exilemoulei/pob4agents/pull/4)、[PR #6](https://github.com/exilemoulei/pob4agents/pull/6)）：面板“去水”分析、`--keep` 条件保留、构筑恢复校验、错误退出码与中英文文档润色。
  - [**@Capoouo**](https://github.com/Capoouo)（[PR #3](https://github.com/exilemoulei/pob4agents/pull/3)）：永恒珠宝种子搜索与候选种子筛选。
- **参与贡献**：请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。
- **开源协议**：**GNU General Public License v3.0（GPL-3.0）**，详见 [LICENSE](LICENSE)。

---

## 给 AI Agent 的安装与配置指引（必读，人类用户可跳过）

当用户要求你安装 `exilemoulei/pob4agents` 时，**以下 5 步必须全部完成**，不要只装完 CLI 就结束：

### 1. 安装 `pob4agents`（命令 `pob`，无需 Python）

- **Windows x64**：
  ```powershell
  irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
  ```
- **macOS / Linux**：
  ```bash
  curl -fsSL https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.sh | bash
  ```

两个安装脚本都会自动校验发布的 Ed25519 签名与 SHA256。若直连 GitHub 失败，先询问用户本地代理地址并设置 `POB_PROXY`（如 `http://127.0.0.1:<端口>`）或 `POB_GITHUB_MIRROR` 后重试。后续升级直接运行 `pob self-update`。

### 2. 初始化本地 PoB 引擎

```powershell
pob bootstrap
pob status
```

- 需代理时加 `--proxy http://127.0.0.1:<端口>`；若本机已有 `PobTools` 目录，可用 `pob bootstrap --from-dir "<本地路径>"` 离线导入。
- Windows 采用精简原生 LuaJIT 托管引擎（无需 `pob-zh.exe` 或数百 MB 永恒珠宝 LUT 压缩包），macOS / Linux 独立版会自动使用内置或经 Ed25519 校验下载的预编译 LuaJIT 运行时，无需手动编译。
- 默认显示语言为简体中文（`zh-CN`），国际服/台服繁体玩家可运行 `pob lang zh-TW`（或设置环境变量 `POB_LANG=zh-TW`）切换为固定繁体中文（支持简繁双词库离线秒切与互查；引擎未带的语言会被拒绝）。
- 确认 `pob status` 返回版本与引擎就绪状态后再进入下一步。

### 3. 挂载 Skill（必做，不可跳过）

`install.ps1` / `install.sh` 只安装 `pob` 可执行文件，**不会**自动复制 Skill 和 Hook。请先拉取本仓库（例如 `git clone --depth 1 https://github.com/exilemoulei/pob4agents.git` 到临时目录），将整个 [`skills/pob`](skills/pob) 目录（含 `SKILL.md` 及 `references/`）完整复制到当前客户端的技能目录：

| 客户端 | 目标路径 |
| :--- | :--- |
| Claude Code | `~/.claude/skills/pob` |
| Codex / Antigravity / OpenCode / 通用 | `~/.agents/skills/pob` |
| DeepSeek Harness (DSH) | 对应客户端的 `skills/pob` 目录 |

后续所有《流放之路》1 构筑对话，必须先读取并遵守 [`skills/pob/SKILL.md`](skills/pob/SKILL.md)。

### 4. 接入防幻觉护栏 Hook（必做，与 Skill 配套使用）

从仓库 [`hooks/`](hooks) 目录安装当前客户端对应的钩子，防止跳过 `pob` 瞎编数值、翻查敏感环境变量或混入 PoE2 / 旧版本规则：

| 客户端 | 安装方式 |
| :--- | :--- |
| Antigravity | 将 [`hooks/hooks.json`](hooks/hooks.json) 复制或合并到 `.agents/hooks.json` |
| Claude Code | 将 [`hooks/claude-hooks.json`](hooks/claude-hooks.json) 合并到 `.claude/settings.json` |
| DeepSeek Harness (DSH) | 启用 `dsh-pob-assistant` 插件 |

若当前客户端不支持上述 Hook 机制，明确告知用户，但必须保证第 3 步的 Skill 已安装到位。

### 5. 收尾验收与用户 Cookie 安全规则

- **完成验收**：确认 `pob status` 正常、`skills/pob` 已落盘、对应 Hook 已接入后，**提醒用户新开一个对话窗口**，让新装的 Skill 与 Hook 生效。
- **收到用户发来的 Cookie 时**：
  - 国际服交易 / 国际服私密角色 → 写入本机用户级环境变量 **`POESESSID`**（Windows PowerShell：`[Environment]::SetEnvironmentVariable("POESESSID", "<值>", "User")`；macOS/Linux 写入 shell 配置文件）。
  - 国服官网（`poe.game.qq.com`）角色导入 → 写入本机用户级环境变量 **`POE_CN_SESSID`**（或完整 `POE_CN_COOKIE`）。
  - **严禁**写入命令行参数、仓库文件或日志；**严禁**执行 `Get-ChildItem Env:` / `printenv` 等命令读取或打印环境变量；回复中不要复读明文。确认凭证是否生效只查 `pob status`。

<details>
<summary>备用安装方式（Scoop / PyPI / Releases 便携包）与文档索引（主流程卡住时展开）</summary>

### 备用安装方式

- **Windows Scoop**：
  ```powershell
  scoop bucket add pob https://github.com/exilemoulei/pob4agents
  scoop install pob
  ```
- **PyPI 安装（Python 3.11+）**：
  ```powershell
  uv tool install pob4agents
  # 或 pip install pob4agents
  ```
- **GitHub Releases 便携包**：前往 [Releases 最新正式版](https://github.com/exilemoulei/pob4agents/releases/latest) 下载对应平台压缩包（附 `SHA256SUMS.txt` 与 Ed25519 签名 `SHA256SUMS.txt.sig`），解压后将含 `pob` 的目录加入 `PATH`。
- **macOS / Linux 源码编译兜底**：若预编译运行时不适配当前系统，可用 [`pob_cli/packaging/build_posix_runtime.sh`](pob_cli/packaging/build_posix_runtime.sh) 编译。

### 常用排障环境变量与仓库文档

- 常用环境变量：`POB_PROXY`（网络代理）、`POB_GITHUB_MIRROR`（GitHub 镜像前缀，替换文件时仍校验哈希/签名）、`POB_OFFLINE=1`（跳过更新检查）、`POESESSID`（国际服交易/私密资料）、`POE_CN_SESSID`（国服官网角色）。
- 文档总目录：[`docs/README.md`](docs/README.md)
- 全部子命令与参数：[`docs/guides/commands.md`](docs/guides/commands.md)
- 交易搜索与工艺台细则：[`docs/guides/trade.md`](docs/guides/trade.md)
- 架构与贡献指南：[`CONTRIBUTING.md`](CONTRIBUTING.md)、[`AGENTS.md`](AGENTS.md)

</details>
