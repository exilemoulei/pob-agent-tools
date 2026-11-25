# `pob trade upgrade` 設計

## 目的與動機

Agent 想回答「預算 N chaos，下一步該往哪裡投資」時，現在要自己串 `audit`、`trade search --auto-query`、`trade plan`、`respec`、`rank-gems --optimize` 五、六條命令，步驟多又容易出錯。`pob trade upgrade --budget N` 把這些串成一條命令，輸出一份**參考用**的升級清單：每項都經 PoB 實算，依每 chaos 的分數提升排序。

清單的用途是「方向參考」，不是「照單購買」。因此**速度與涵蓋面優先於精確度**。

## 目標

- 一條命令輸出：預算內的最佳組合（每 slot 至多一件）＋每 chaos 效益排行＋零 chaos 項建議。
- 每筆交易候選都經 PoB 驗證（沿用 `trade search` 的驗證流程）。
- 一般執行時間約 1–2 分鐘（K=3 slot、每 slot 驗證 5 筆）。
- 人類輸出為 Markdown，`--json` 欄位為英文，與現有命令一致。

## 非目標

- 不保證「買了就達到加總的提升」：各 slot 的提升分別驗證，加總只是近似（與 `trade plan` 相同的已知限制，輸出中註明）。
- 不做全身掃描（方案 C）、不做離線詞綴估價（方案 B）。
- 不自動購買、不寫入 build（不提供 `--apply`）。
- 不涵蓋 cluster jewel、天賦珠寶、藥劑。

## 反目標（做了也算失敗）

- 撞到 GGG 速率限制而整條命令失敗：必須沿用 `trade_client` 的動態限速，單一 slot 失敗只跳過該 slot 並註記，不可讓整體失敗。
- 執行完 build 有殘留改動：所有 what-if 一律 rollback（沿用 `BuildTransaction`／既有 daemon action）。
- 削弱 guardrails 或在參數／log 中暴露 `POESESSID` 等 cookie。

## 限制

- 官方 trade API 速率限制，每多一個 slot 約多數十秒。
- 層級：新命令位於 L4（`cli_trade/`），只往下呼叫既有的 L1–L3 函式；檔案 ≤650 行／24,000 bytes，函式 ≤200 行。
- 交易唯讀；結束碼沿用 trade 的 `0..6` 合約。
- PoE1 only。

## 方案比較

| 方案 | 作法 | 取捨 |
|------|------|------|
| **A（已選）** | 挑最弱 K 個 slot 即時搜交易，結果交給 `plan_budget`，另附零 chaos 項 | 價格真實且經 PoB 驗證，約 1–2 分鐘；沒搜的 slot 看不到 |
| B | `weights` 詞綴價值 × poe.ninja unique 價格，全離線 | 數秒完成；稀有裝無價，缺了最常見的升級來源 |
| C | 全身 slot 都搜、每 slot 驗證 10+ 筆 | 最完整；5–10 分鐘以上且易撞限速，對參考用途太重 |

使用者選 A，理由：清單是參考用途，速度與涵蓋面比精確度重要，A 也最能沿用現有零件。

## 設計

### 命令

```
pob trade upgrade --budget N [--slot S]... [--top-slots 3] [--per-slot 5]
            [--score dps=W,ehp=W] [--league L] [--proxy U]
            [--target <slug>] [--json] [--compact] [--out f]
```

- `--budget`：必填，chaos。
- `--slot`：可重複，指定要搜的 slot；有給就不做自動挑選。
- `--top-slots`：自動挑選的 slot 數，預設 3。
- `--per-slot`：每個 slot 送 PoB 驗證的候選數（傳給 `--pob-limit`），預設 5。
- `--score`：沿用 `trade plan` 的 `dps`／`ehp` 權重，預設 `dps=1,ehp=0.25`。

### 流程

1. **挑 slot**（未給 `--slot` 時）：
   - 空的主要 slot（`audit` 已列為 hard gap 的 `Equipment slot '...' is empty`）優先。
   - 其餘主要 slot（`Weapon 1, Body Armour, Helmet, Gloves, Boots, Amulet, Ring 1, Ring 2, Belt`）各做一次 what-if `unequip`，以 `--score` 計算「拿掉後掉多少分」；掉分最少＝貢獻最低＝最該升級。取前 K 個。
   - 判斷：用卸下探針而非另做「弱度」模型，因為它就是 PoB 實算、成本只有 9 次 calc，且與後續評分用同一把尺。（我的決定）
2. **搜交易**：對每個選中的 slot 呼叫 `trade search --auto-query` 的同一套內部流程（`_try_auto_trade_query` → `_execute_trade_api_query` → `_verify_candidates_with_pob`），`pob_limit = --per-slot`。單一 slot 出錯（限速、無結果、auto-query 失敗）只記入 `skippedSlots` 並附原因。
3. **背包**：把各 slot 結果組成與 `trade search --json` 同形的 payload，交給 `trade_eval.budget.plan_budget`，取得 `picks`、`bestPerChaos`。
4. **零 chaos 項**：以 1 輪 `respec`（`rounds=1`）與 1 輪 `rank-gems --optimize`（`passes=1`）跑主技能群組，只列出有正分的建議，不放進背包。（我的決定：只跑 1 輪以控制時間；要更深入可另跑原命令）
5. **輸出**：
   - `### Upgrade Plan`：與 `trade plan` 相同的 picks 表與 bestPerChaos 表（直接沿用其格式化程式碼）。
   - `### Free moves`：respec 與 gem 建議各一小段。
   - 註記：所選 slot 與挑選理由、跳過的 slot 與原因、「加總為近似值」提示。
   - `--json`：`{budget, score, slots:[{slot, reason}], plan:<plan_budget 結果>, free:{respec, gems}, skippedSlots:[{slot, error}]}`。

### 程式碼位置

- 新檔 `pob_cli/src/pob_cli/cli_trade/upgrade_cmd.py`（L4），在 `cli_trade/__init__.py` 註冊為 `pob trade` 的子命令 `pob trade upgrade`（避免與 `pob update`／`pob self-update` 混淆）。
- 需要把 `cmd_trade_search` 中「給定 slot 與參數 → 回傳候選 payload」的部分抽成可呼叫的函式，讓 `trade search` 與 `upgrade` 共用；不得複製流程。
- `trade plan` 的 Markdown 表格產生部分抽成共用函式供 `upgrade` 使用。

### 測試

- 單元測試（離線、fake）：slot 挑選排序（空 slot 優先、掉分少者優先、`--slot` 覆蓋）；單一 slot 失敗時其餘照常且列入 `skippedSlots`；JSON 形狀；預算 0 時 picks 為空。
- 不新增真實引擎測試（交易需網路，CI 跑不了）；故 `engine.yml` 門檻不變。

### 文件

同步 `docs/guides/commands.md`、`README.md`、`README_EN.md`、`skills/pob/SKILL.md`（優化流程那段把 `pob trade upgrade` 列為第一步的捷徑）。

## 留給實作者決定

- 共用函式的切法與命名，只要 `trade search` 行為不變。
- 卸下探針使用既有 daemon action 或 `batch` what-if，取較簡單者。
- 零 chaos 項與交易搜尋的先後順序（建議先跑零 chaos 項，避免交易逾時後什麼都沒有）。
