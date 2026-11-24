# pob4agents (`pob`)

[简体中文](README.md) | **English**

`pob4agents` is a Path of Building (PoB) CLI toolkit for AI agents and Path of Exile 1 players. Its command is `pob`. It runs PoB in the background so you can import builds, try gear, compare passives and gems, and look up game data and trade prices.

DPS, effective hit pool (TotalEHP), maximum hit taken (MaxHit), and the effects of gear, passive, gem, or Timeless Jewel changes are calculated by a running PoB engine. The tool does not estimate them from modifiers in a build XML file.

---

## Contents

- [What it does](#what-it-does)
- [Installation and setup](#installation-and-setup)
  - [Windows x64](#1-windows-x64)
  - [macOS and Linux](#2-macos-and-linux)
  - [Set up the PoB engine](#3-set-up-the-pob-engine)
- [Options and build management](#options-and-build-management)
- [Features and examples](#features-and-examples)
  - [1. Inspect a build and its PoB configuration](#1-inspect-a-build-and-its-pob-configuration)
  - [2. Compare combat scenarios](#2-compare-combat-scenarios)
  - [3. Try gear, flasks, anoints, gems, and passives](#3-try-gear-flasks-anoints-gems-and-passives)
  - [4. Evaluate several changes or variants](#4-evaluate-several-changes-or-variants)
  - [5. Find passive and gem upgrades; compare builds](#5-find-passive-and-gem-upgrades-compare-builds)
  - [6. Search Timeless Jewel seeds](#6-search-timeless-jewel-seeds)
  - [7. Look up game data, prices, and build trends](#7-look-up-game-data-prices-and-build-trends)
  - [8. Search trade listings and bench crafts](#8-search-trade-listings-and-bench-crafts)
- [AI Agent lifecycle hook guardrails (`pob hook`)](#ai-agent-lifecycle-hook-guardrails-pob-hook)
- [Exit codes](#exit-codes)
- [Development, tests, and releases](#development-tests-and-releases)
- [Credits and license](#credits-and-license)

---

## What it does

- **Install and update the PoB engine** (`pob bootstrap` / `pob update`): Windows uses PobTools-zh; macOS and Linux use a locally compiled LuaJIT runtime. The tool checks for PobTools-zh and PoB updates, keeps the previous working version if an update fails, and can use the local engine while GitHub is unreachable.
  - Windows x64 runs `pob-zh.exe --engine-headless` with English and Chinese support. macOS (Apple Silicon / Intel) and Linux compile `LuaJIT 2.1 + lua-utf8` locally and run official PoB and `bridge.lua` through `headless_runner.lua`.
  - Updates are checked at startup and every 30 minutes. The PobTools-zh version comes from a `302` redirect rather than `api.github.com`; PoB files are downloaded incrementally using SHA1 values from the official `manifest.xml`. The update must pass a `gate_result` check before loaded slots are refreshed, and failures roll back from `.bak/`.
  - If GitHub cannot be reached, the connection fails within three seconds and the local engine remains available. `stderr`, JSON `updateWarning`, and Markdown warnings report the update state.
- **Manage several builds at once**: Name a build slot with `--target <slug>`, such as `main`, `ref`, or `mageblood_test`. Each slot has its own undo and redo history. `pob serve` keeps the engine running between commands.
  - Slots are no longer limited to two: for example, `main`, `ref`, `pinnacle_test`, and `mageblood_test` can all be active. `POB_SLOT` can set the default slot; each slot keeps 30 steps of `undo` / `redo` history.
  - Engine calls support `call_batch` pipelining. CLI modules load on demand, and stale process locks are cleaned up quickly (target `<1ms`). Warm queries usually take milliseconds. Normal commands start the daemon automatically; containers and agent environments can use foreground `pob serve` instead.
- **Check inflated PoB numbers** (`pob audit` / `pob matrix`): See how configuration, flasks, temporary or Vaal skills, and party buffs affect DPS and effective hit pool. PoB recalculates the effect of each condition. Magic flasks sustained by Mageblood, the regular version of a Vaal skill, and persistent auras are handled separately. Keep conditions your build can reliably sustain with `--keep`; the tool checks that the original build is restored afterward.
  - The audit disables PoB Config settings, nonpersistent flasks, temporary guard or burst skills, Vaal bursts, and party buffs one category at a time, then restores each item separately to measure its DPS / EHP effect. For Vaal gems it disables burst `enableGlobal1` while retaining the base skill `enableGlobal2`, switching `mainActiveSkill` when needed.
  - `customMods` groups are disabled without changing their titles or text; an internal sentinel group preserves the configuration structure. Configuration is read back before recalculation to confirm the change took effect. `--keep` accepts individual conditions, flasks, skills, or `customMods:<group name>`; `keepRejected` suggests corrections to misspelled conditions. `verify_restored` checks the original build at the end.
- **Try and compare changes** (`pob batch` / `pob sweep`): Gear, gems, passives, and configuration changes are simulations by default; the original build is restored afterward. Add `--apply` to save a change. Combine several edits or compare multiple gear and gem setups in one run.
  - Single edits and batches run on an in-memory build snapshot. `pob batch` combines 19 operation types and returns selected analysis views. `pob sweep` compares multiple gear, passive, or gem setups in the same running engine; one failed variant does not stop the others.
- **Search Timeless Jewel seeds** (`pob timeless list` / `pob timeless search`): Evaluate candidate seeds in PoB using your allocated socket and passives. English and Chinese jewel names are accepted. Use `--exact` to evaluate all candidates.
  - Jewel data comes from PoB's `Data/Uniques/jewel.lua` and covers 11 Timeless Jewel variants. For jewels such as Lethal Pride, Brutal Restraint, Militant Faith, Elegant Hubris, and Heroic Tragedy, the search deduplicates allocated and anointed notables before prefiltering seeds; `--exact` skips that prefilter. Multiple temporary engine workers can search in parallel, while conflicting `Historic` jewels in other sockets are handled automatically.
- **Look up game data, mechanics, and prices** (`pob data` / `pob research` / `pob poedb` / `pob wiki` / `pob price` / `pob meta`): The local bilingual index covers gems, unique items, modifiers, and bases. Online queries support concurrent `pob research` (`poedb.tw` + `poewiki.net`), exact local PoEDB bilingual index matching for `pob wiki` with prioritized `Mechanics` / `Skill functions and interactions` extraction, and poe.ninja prices and build trends.
  - A SQLite/FTS5 index is built from PoB's Lua data and can be searched without starting PoB, usually within 15 ms. It includes Foulborn unique variants, bench/Eldritch/corrupted modifiers, and English↔Chinese names; one command can search multiple terms.
- **Find gear and fill modifier gaps** (`pob trade` / `pob bench`): Search official trade listings, compare upgrades in PoB, inspect available bench crafts, and preview same-tier Harvest resistance conversions. Trade commands only search and calculate; they do not buy items for you.
  - `pob item inspect` classifies rare-item prefixes, suffixes, hybrid modifiers, and existing bench crafts; uncertain matches never imply a free slot. Trade search uses `--bench-craft` to evaluate legal additions or replacements at maximum rolls and compare the resulting PoB gains.
  - Local `TradeSiteStats.lua` resolves English and Chinese trade stats and distinguishes local item modifiers from global ones. Searches respect rate limits reported in GGG response headers and support 3.30 instant buyout listings and `chaos_equiv` prices.
  - `--allow-conversion` considers eligible same-tier Harvest resistance swaps and checks modifier-group conflicts; `--sum` builds official trade Weighted Sum groups and sorts by sum. Candidate DPS / EHP changes are calculated by PoB in batches. `pob bench` combines `ModMaster.lua` with local cost and unlock data to suggest verifiable bench crafts.
- **One-shot build diagnosis, anti-hallucination guardrails, and universal Agent Skill** (`pob diagnose` / `pob ground` / `skills/pob`): Built for fast/lightweight AI models as well as players, a single command reports dewatered Pinnacle numbers, Ascendancy and Bloodline node stats, all skill groups, zero-effect or conflicting supports, `0.033s` trigger cooldown breakpoints and overspeed checks, and Low-Life Chaos bypass bricks while blocking Path of Exile 2 and removed 3.23–3.25 mechanics from leaking into PoE 1 answers.
  - `pob diagnose` supports `-c/--compare` for one-shot dual-build comparison and `-q/--ask` to ground terms in the user's prompt; build outputs carry an `[Env: PoE1 3.30 (NOT PoE2) | ... (rev=N)]` stamp so long multi-turn chats do not mix up stale numbers. The bundled [`skills/pob/SKILL.md`](skills/pob/SKILL.md) skill works out of the box in DeepSeek Harness, Claude Code, Codex, OpenCode, Cursor, and other skill-aware agents.

---

## Installation and setup

### 1. Windows x64

The standalone Windows package includes its runtime; Python is not required.

#### Option 1: PowerShell installer (recommended)

```powershell
irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
```

If your network needs a proxy to reach GitHub:

```powershell
$env:POB_PROXY = "http://127.0.0.1:7890"
irm https://raw.githubusercontent.com/exilemoulei/pob4agents/main/install.ps1 | iex
```

Run the same command again to upgrade. The installer checks the release's Ed25519 signature and SHA256 and stops the old background process.

#### Option 2: Scoop

The repository includes a Scoop manifest (`bucket/pob.json`) that is updated with each release:

```powershell
scoop bucket add pob https://github.com/exilemoulei/pob4agents
scoop install pob

# Update later:
scoop update pob
```

#### Option 3: Portable GitHub release (permanent link)

```powershell
# Permanent link: always the latest stable release, no version string needed
irm https://github.com/exilemoulei/pob4agents/releases/latest/download/pob-windows-x64.zip -OutFile "$env:TEMP\pob-windows-x64.zip"
```

Extract it and add the folder containing `pob.exe` to your `PATH` (you can also just open the link above in a browser). Checksums live in [`SHA256SUMS.txt`](https://github.com/exilemoulei/pob4agents/releases/latest/download/SHA256SUMS.txt), which covers both the versioned filenames and the permanent alias.

#### Option 4: Python package (Python 3.11+, `uv` / `pipx` / `pip`)

```powershell
# Use uv (recommended; tracks the latest commit on the main branch)
uv tool install "git+https://github.com/exilemoulei/pob4agents.git#subdirectory=pob_cli"

# Pin an exact version when you need reproducibility (replace vX.Y.Z with a real tag from the Releases page)
uv tool install "git+https://github.com/exilemoulei/pob4agents.git@vX.Y.Z#subdirectory=pob_cli"

# pip / pipx work the same way
pip install "git+https://github.com/exilemoulei/pob4agents.git#subdirectory=pob_cli"
```

> **Load the bundled AI Agent Skill (optional, recommended)**: Copy [`skills/pob`](skills/pob) into your agent's skill folder (such as `~/.agents/skills/pob`, `~/.claude/skills/pob`, or DeepSeek Harness's `skills` directory) so fast models follow concise `pob` workflows and avoid cross-patch hallucinations.

---

### 2. macOS and Linux

On macOS (Apple Silicon `arm64` / Intel `x86_64`) or Linux (`x86_64` / `aarch64`), install Python 3.11+, `cc`, `make`, `git`, and the system `zlib` headers. Then compile `LuaJIT 2.1 + lua-utf8` once:

```bash
git clone https://github.com/exilemoulei/pob4agents.git
cd pob4agents

# 1. Install pob4agents (the pob command)
pip install "./pob_cli"

# 2. Build LuaJIT 2.1 + lua-utf8 and place it in the managed engine directory
# On macOS the default runtime directory is ~/.local/share/pob4agents; override it with POB_RUNTIME_DIR
export POB_RUNTIME_DIR="$HOME/.local/share/pob4agents"
chmod +x pob_cli/packaging/build_posix_runtime.sh
./pob_cli/packaging/build_posix_runtime.sh ./runtime-dist
mkdir -p "$POB_RUNTIME_DIR/engine"
cp -R ./runtime-dist/bin "$POB_RUNTIME_DIR/engine/bin"

# 3. Download PoB data and bridge.lua, then check that the engine starts
pob bootstrap --no-zh
pob status
```

---

### 3. Set up the PoB engine

After installing pob4agents, run `pob bootstrap` once to download the PoB engine. You can also import an existing local `PobTools` directory:

```powershell
pob --version
pob bootstrap # Download PobTools-zh and official PoB directly from GitHub
pob bootstrap --proxy http://127.0.0.1:7890 # Download through an HTTP or SOCKS5 proxy
pob bootstrap --from-dir "<path-to-PobTools>" # Seed the engine from an existing local PobTools directory
pob update --check # Check for upstream updates
pob update --proxy http://127.0.0.1:7890 # Update the engine and refresh loaded build slots
```

Common environment variables:

| Variable | Purpose |
|---|---|
| `POB_ROOT` | Explicit engine root; otherwise `<POB_RUNTIME_DIR>/engine` is discovered automatically |
| `POB_RUNTIME_DIR` | Engine, daemon state, and cache root; installed Windows default: `%LOCALAPPDATA%\pob4agents` (auto-reuses existing legacy `pob-agent-tools` data), source checkout default: `<repo>/.runtime` |
| `POB_CACHE_DIR` | Override the local game database and online query cache directory |
| `POB_SLOT` | Default build slot if `--target` is omitted; defaults to `main` |
| `POB_PROXY` | HTTP, HTTPS, or SOCKS5 proxy; `HTTPS_PROXY` and `ALL_PROXY` are also supported |
| `POB_GITHUB_MIRROR` | Mirror prefix for GitHub downloads during `bootstrap` and `update` |
| `POB_OFFLINE` | Set to `1` to skip startup and scheduled GitHub update checks; equivalent to global `--offline` |
| `POB_HOOK_DISABLE_V5` | Set to `1` to disable the Gate 3 V5 web-research verification gate (independent of `POB_OFFLINE`) |
| `POESESSID` / `POE_COOKIE` | Optional when the official trade API returns 401/403; provide them only as environment variables, never as CLI arguments or log entries |

---

## Options and build management

### Output and build slots

Commands can return JSON, compact output on one line, or write UTF-8 without a BOM directly to a file. Direct file output also avoids PowerShell redirection encoding issues on Windows:

```powershell
# Put global options after pob; most subcommands also accept equivalent local options
pob --offline --compact --out result.json summary --json

# Show the daemon PID, PoB and PobTools versions, update status, and loaded slots
pob status
pob status --json
```

### Keep the PoB engine running (`pob serve`)

Build commands normally start the daemon in the background. If your agent environment removes child processes after each command, run `pob serve` in a separate session. It listens only on local `127.0.0.1` and authenticates requests with `X-PoB-Token`:

```powershell
# Start the persistent engine in a separate session
pob serve --offline

# Stop the daemon and release engine memory
pob stop
```

### Import, create, and export builds

`pob load` accepts local `.xml` / `.txt` files (direct paths or `@path`), raw Base64 share codes, and links from eight share sites: `pobb.in`, `poedb.tw/pob`, `poe.ninja/pob`, `maxroll.gg/poe/pob`, `pob.codes`, `pastebin.com`, `pastebinp.com`, and `rentry.co`. It can also import a `poe.ninja` character page URL:

```powershell
# 1. Import a build into the default main slot
pob load "https://poedb.tw/pob/AWqOLSSmyG"
pob load "@builds/my_chieftain.xml"
pob load "https://poe.ninja/poe1/builds/mirage/character/AccountName-1234/CharName"

# 2. Use --target for other builds, such as ref for comparison or exp1 for experiments
pob load "https://pobb.in/xxxxxx" --target ref
pob new --class Marauder --asc Chieftain --level 95 --target exp1

# 3. Switch the passive tree, item set, or skill set within a build
pob switch-main --loadout "Endgame Mageblood"
pob switch-main --spec 2 --item-set 2 --skill-group 1

# 4. Export an XML file or PoB share code
pob save "builds/optimized.xml"
pob export-code --target exp1
```

---

## Features and examples

### 1. Inspect a build and its PoB configuration

When you receive a PoB, run `pob diagnose` for a one-shot report covering original vs. dewatered Pinnacle numbers, Ascendancy and Bloodline stats, all skill groups and dead-support checks, `0.033s` trigger breakpoints, and top upgrades — or `--compare` to compare two builds at once. You can also run `pob summary` and `pob audit` individually:

```powershell
# One-shot build diagnosis (-q grounds mechanics/gems mentioned in a question)
pob diagnose "@builds/my_build.xml" -q "Why is my boss DPS low and why am I getting one-shot?"

# One-shot 3-baseline comparison and Pinnacle audit across two builds
pob diagnose "@builds/build_a.xml" --compare "@builds/build_b.xml"

# Offence, defence, minion summary, and native PoB warnings
pob summary

# Disable temporary conditions one by one to see their DPS and EHP effects
pob audit

# Show the audit without recalculating each contribution
pob audit --no-attribution
```

If a build reliably meets a condition, such as standing still, sustaining Endurance Charges, or keeping an enemy ignited, retain it with `--keep`. Individual custom modifier groups can also be retained:

```powershell
# Keep selected conditions, flask slots, all flasks, skills, or one customMods group
pob audit --keep conditionStationary,useEnduranceCharges,conditionEnemyIgnited
pob audit --keep "Flask 1,Vaal Haste,customMods:Boss Setup"

# keepRejected suggests corrections for misspelled conditions
pob audit --keep conditionStationery --no-attribution
```

Use these read-only commands to inspect items, gems, passives, configuration, and calculation details:

```powershell
# Show current configuration or search available values
pob config list
pob config list --all --search "boss" --with-options

# Show all equipped items, one item, or raw item and jewel text
pob items
pob items --slot "Weapon 1" --raw
pob items --all --raw

# Inspect affix occupancy and compatible crafts on equipped or pasted items
pob item inspect --slot "Ring 2" --want "Cold Resistance"
pob item inspect --file copied-item.txt --slot gloves --want "maximum Life"

# Show skill groups or details for each gem in the main group
pob skills
pob skills --group main

# Search passive nodes, Masteries, and nodes near a jewel socket
pob tree --search "最大生命" "法术压制" --unallocated --max-cost 4 --show-path
pob tree --type Mastery --allocated --masteries
pob tree --socket-radius 61419

# Inspect values and sources on PoB's Calcs tab; multiple terms are accepted
pob calc "Average Hit" "Spell Suppression" "Mana Regen"
```

---

### 2. Compare combat scenarios

`pob matrix` compares the current configuration (`Original`), a dewatered level 84 Pinnacle boss (`Pinnacle`), a dewatered level 85 Uber boss (`Uber`), flasks off (`FlasksOff`), and optional custom scenarios in one run. It checks that the build is restored afterward. The default output contains common metrics; `--full` returns more than 200 raw stats:

```powershell
# Compare the four default scenarios: Original, Pinnacle, Uber, FlasksOff
pob matrix

# Keep stationary and flasks in Pinnacle/Uber; add full Wither and no-guard scenarios
pob matrix Original Pinnacle Uber `
  -s "Wither15:Pinnacle,MultiplierWitheredStackCount=15" `
  -s "NoGuard:Pinnacle,conditionMoltenShell=false" `
  --keep conditionStationary,flasks

# Return all raw stats; the default output shows common metrics
pob matrix Original Pinnacle --full --json --out matrix_full.json
```

---

### 3. Try gear, flasks, anoints, gems, and passives

Changes to gear, passives, gems, and configuration are simulations by default: you see the before/after difference, then the original build is restored. Add `--apply` to keep a change, and use `pob undo` / `pob redo` afterward if needed. Amulet anoints use PoB's native flow and validate the notable; passive changes can specify a Mastery `effectId`. Configuration commands accept older boss names, the `None` dropdown option, and appending to existing `customMods`.

```powershell
# --- Try gear, edit modifiers, toggle flasks, and anoint an amulet ---
# Try an item copied from the game with Ctrl+C
pob item try --slot "Amulet" --file .\candidate_amulet.txt

# Try replacing, adding, or removing modifiers on equipped gear
pob item edit --slot "Ring 1" `
  --replace-mod "Cold Resistance" "+48% to Lightning Resistance" `
  --add-mod "+55 to maximum Life"

# Find and equip a PoB unique item; English and Chinese names work
pob item db "法师之血" --slot Belt --equip
pob item flask all off

# Try an amulet anoint
pob item anoint --slot Amulet --notable "Whispers of Doom"

# --- Edit gems or replace a whole skill group ---
# For Vaal gems, the base skill starts enabled and the Vaal burst disabled
pob skill add --group main --gem "Awakened Elemental Focus Support" --level 5 --quality 20
pob skill set --group main --gem 1 --level 21 --quality 20
pob skill new-group --label "Aura" --slot "Helmet" `
  --gem "Determination:21:0" --gem "Grace:21:0" --gem "Enlighten Support:4:0"
pob skill replace-group --group main `
  --gem "Ice Nova of Frostbolts:21:20" `
  --gem "Archmage Support:20:20" `
  --gem "Spell Echo Support:20:20"

# --- Change passives, Masteries, and Ascendancy ---
pob tree change --allocate "26725,49080:64315" --deallocate "12345"
pob tree set-class Witch Occultist --secondary-asc None

# --- Change PoB configuration; --append-mods preserves existing custom modifiers ---
pob config set enemyIsBoss=Pinnacle conditionStationary=true --append-mods "+15% more Damage"

# Save a change, then undo or redo it
pob item anoint --slot Amulet --notable "Charisma" --apply
pob undo
pob redo
```

---

### 4. Evaluate several changes or variants

#### `pob batch`: combine edits in one simulation

`pob batch` supports **19 operation types** and can return the chosen build panels or audit results: `config`, `flask`, `item`, `item_db`, `item_edit`, `unequip`, `anoint`, `tree`, `tree_reset`, `import_tree_url`, `skill`, `add_group`, `delete_group`, `set_group`, `replace_group`, `new_build`, `set_level`, `set_class`, `switch_main`.

```powershell
# Try Headhunter, anoint Charisma, and change configuration; return panels, audit, and scenarios
pob batch --json-ops '[
  {"op": "item_db", "query": "Headhunter", "slot": "Belt"},
  {"op": "anoint", "slot": "Amulet", "notable": "Charisma"},
  {"op": "config", "set": {"enemyIsBoss": "Pinnacle", "conditionStationary": true}}
]' --keep conditionStationary --return "summary,audit,matrix,calc:DPS;Mana"
```

To see the effect of each temporary condition, change `audit` in `--return` to `audit:full`. For bulk generation, `--apply --no-undo` saves the build without creating undo snapshots.

#### `pob sweep`: compare several setups

Compare gear, anoints, support gems, or Ascendancy paths against a Pinnacle boss. An error in one variant does not stop the others:

```powershell
pob sweep --scenario Pinnacle --keep conditionStationary --calc "Mana" --json-variants '[
  {"name": "Baseline", "ops": []},
  {"name": "Anoint_Whispers", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Whispers of Doom"}]},
  {"name": "Anoint_Sovereignty", "ops": [{"op": "anoint", "slot": "Amulet", "notable": "Sovereignty"}]},
  {"name": "Mageblood_Swap", "ops": [{"op": "item_db", "query": "Mageblood", "slot": "Belt"}]}
]'
```

---

### 5. Find passive and gem upgrades; compare builds

Rank unallocated passives by point cost, compare support gem swaps or adding a link, compare two builds under three baselines, and try items or skills from a reference build:

```powershell
# 1. Rank passives by point cost and gain in DPS, EHP, or Life
pob rank-nodes --stat CombinedDPS --max-dist 5 --top 15
pob rank-nodes --stat TotalEHP --max-dist 4 --top 10
# Multi-objective: weight each stat's % gain (+1% EHP counts half of +1% DPS) and drop nodes that leave EHP under 8000
pob rank-nodes --score CombinedDPS=1,TotalEHP=0.5 --floor "TotalEHP>=8000"
# Refund value of allocated passives: lowest loss per refunded point first (dead points on top)
pob rank-nodes --refund --score CombinedDPS=1,TotalEHP=0.5
# Plan a respec: refund the cheapest point, allocate the best one, each round PoB-verified; prints a ready pob batch command
pob respec --score CombinedDPS=1,TotalEHP=0.5 --floor "TotalEHP>=8000" --rounds 3

# 2. Compare support gem replacements or adding one link with --add-link
pob rank-gems --group main --slot 5 --field FullDPS --top 10
pob rank-gems --group main --add-link --field CombinedDPS --top 10
# Optimise the whole support set: swap each slot to its best gem given the others until nothing helps (each swap PoB-verified)
pob rank-gems --group main --optimize --field CombinedDPS

# Probe each mod line alone (16 defaults; --mod replaces) for its PoB-verified DPS / EHP gain
pob weights --scenario Pinnacle
pob weights --mod "+1 to Level of all Skill Gems" --mod "20% increased Area of Effect"
pob weights --score CombinedDPS=1,TotalEHP=0.5 --floor "Life>=4000"
# Compare every bandit / major / minor Pantheon option
pob weights --preset bandit
pob weights --preset pantheon-major --scenario Pinnacle
# What each equipped jewel (abyss included) is worth: remove one at a time; replace the weakest first
pob weights --preset jewels --score CombinedDPS=1,TotalEHP=0.5

# 3. Compare original panels, aligned combat conditions, and dewatered Pinnacle results
pob compare "https://pobb.in/ReferenceBuildCode"

# 4. Try equipment or the main skill group from the ref build
pob borrow --item-slot "Weapon 1,Amulet,Ring 1" --skill-group main
```

---

### 6. Search Timeless Jewel seeds

`pob timeless` evaluates candidate seeds inside PoB, including the value lost when an existing notable is replaced. It does not simply add the new modifiers' values. It supports the 11 Timeless Jewel variants in PoB's data:

```powershell
# 1. List supported jewels and allocated jewel socket IDs
pob timeless list

# 2. Search a socket; Chinese and English jewel names work
pob timeless search `
  --jewel "致命的骄傲" `
  --socket 61419 `
  --conqueror Kaom `
  --stat CombinedDPS `
  --top 15 `
  --workers 4 `
  --idle

# 3. Search by a chosen stat and optionally add a desired jewel modifier
pob timeless search `
  --jewel "Heroic Tragedy" `
  --socket 26196 `
  --conqueror Vorana `
  --stat Ward `
  --mod "+10 to all Attributes" `
  --top 10

# 4. Use --exact to evaluate every candidate seed
pob timeless search --jewel "Militant Faith" --socket 41263 --stat TotalEHP --exact --workers 4
```

> **Performance and memory:** `pob timeless list` usually returns in under 100 ms. Each parallel PoB worker uses about 1.3–1.7 GB. By default, `--workers` uses half the CPU core count, capped at four workers. Add `--idle` to lower their CPU priority during background searches.

---

### 7. Look up game data, prices, and build trends

These commands accept multiple search terms. The local game data index works without starting PoB. poe.ninja prices cover 52 categories and are cached for 15 minutes; PoEDB queries can show modifier pools, weights, and fossil tags. Wiki results are also cached locally:

```powershell
# --- Local game data: gems, quality breakpoints, unique mod search, passive tree, ticks, and bilingual names ---
pob ground "Does Divine Blessing with Hatred still grant more Cold Damage on spells?"
pob data gem "Ice Nova of Frostbolts" "Archmage Support"
pob data gem --quality-breakpoints --active-only --limit 20
pob data unique "Mageblood" "Headhunter"
pob data unique --mod "Chaos Damage taken does not bypass"
pob data tree "Polymath" "Oath of Summer" --type Ascendancy
pob data tick coc --cdr 14 --aps 8.28
pob data mod "Spell Suppression" --slot Helmet --ilvl 85
pob data base "Spirit Shield" --limit 10
pob data translate "喋血之刃" "Whispers of Doom"

# --- PoEDB: modifier pools and weights ---
pob poedb "傲视头冠" --mods --lang cn
pob poedb " Hubris Circlet" --mods --lang us

# --- poe.ninja: prices and popular build trends ---
pob price "Mageblood" "Progenesis" "Nimis"
pob price "Forbidden Flame" --variant "Hierophant" --limit 30
pob price "Awakened Spell Echo Support" --detailed
pob meta --skill "Ice Nova of Frostbolts" --top 15

# --- Concurrent dual-source research (poedb.tw + poewiki.net) & PoE Wiki (local PoEDB bilingual exact match + Mechanics priority) ---
pob research "Returning Projectiles" "Ice Spear"
pob research "Twilight Regalia" --mods --lang us
pob wiki "异能魔力" "Eldritch Battery" "Leech"
```

---

### 8. Search trade listings and bench crafts

Before buying an upgrade, use `pob audit` to find resistance or attribute gaps and `pob bench` to check whether a bench craft can fill them. Then search official trade listings with `pob trade search` and have PoB compare candidates' DPS and effective hit pool. Trade searches are read-only and never buy an item. See [docs/TRADE.md](docs/TRADE.md).

```powershell
# 1. Resolve Chinese stat text to official trade filters
pob trade stats "攻击速度" --slot weapon1
pob trade stats "法术压制率" --slot gloves

# 2. Search listed gloves; --allow-conversion also considers same-tier Harvest resistance swaps
pob trade search --slot gloves --budget 25 --currency chaos_equiv `
  --stat "冰霜抗性=40" `
  --stat "最大生命=70" `
  --want "Attack Speed" `
  --allow-conversion `
  --sort pob --pob auto

# 3. Search rings using a Weighted Sum resistance group, or require an open suffix for bench crafting
pob trade search --slot ring1 --budget 5 --currency divine --stat "maximum Life>=60" --sum "Fire Resistance|Cold Resistance|Lightning Resistance>=80" --search-sort sum
pob trade search --slot ring1 --budget 15 --stat "最大生命=65" --open-suffix --pob auto
pob bench list --slot ring1 --affix suffix --search "抗性"
pob bench suggest --slot gloves --stat "冰霜抗性"

# Compare listed items with maximum-roll added/replaced bench crafts
pob trade search --slot gloves --budget 20 --currency divine --stat "to Dexterity=55" --bench-craft "Area Damage" --pob on

# 4. Preview a Harvest resistance swap; the resulting value must be rerolled
pob trade convert --file .\dropped_gloves.txt --to lightning

# 5. Plan one budget across slots: save each slot's search as JSON, then pick at most one listing per slot for the best total gain (offline, no further trade API calls)
pob trade search --slot ring1 --budget 50 --pob on --json --out ring.json
pob trade search --slot amulet --budget 80 --pob on --json --out amulet.json
pob trade plan ring.json amulet.json --budget 100 --score dps=1,ehp=0.5
```

Repeat `--bench-craft` for multiple craft targets. Results default to PoB ranking; `--pob-limit` counts source listings, each of which may produce multiple craft variants. `--budget` caps listing prices only: crafting, removal, and repeated crafting costs are additional. Hard `--stat` filters apply before crafting. JSON `bench_craft` includes crafted lines, removed lines, costs, the maximum-roll marker, and original listing text.

`pob item inspect` is read-only and does not apply a craft. Unknown, ambiguous, or unresolved-base matches produce unknown open-slot counts, and suggestions require checking the item. Supply `--slot` when pasted text has no inferable item type. Trade craft simulation skips uncertain occupancy, corrupted/mirrored items, and unsupported special crafting cases.

---

## AI Agent lifecycle hook guardrails (`pob hook`)

To prevent fast/lightweight models from bypassing `pob` (hand-parsing Lua/XML, hallucinating PoE2 or removed legacy mechanics in final answers, claiming open affixes without inspecting items, or inventing CoC breakpoints), `pob4agents` ships a deterministic, zero-LLM-judge hook kernel (`pob --offline hook <pre-prompt|pre-tool|verify-stop> [--protocol raw|agy|claude]`) backed by an out-of-band execution receipt ledger:

- **Pre-prompt grounding & turn cursor (`pre-prompt`)**: anchors `turnStartTs` once per turn and injects compact `<pob-grounding>` PoE1 3.30 entity cards and trap alerts offline (`<15ms`), plus `[MECHANICS-RESEARCH-REQUIRED]` when the user asks about build mechanics or skill interactions.
- **Pre-tool hard deny / rewrite (`pre-tool`)**: blocks raw engine `.lua` reads, script-based XML calculations (`R5`), redundant `pob --help`, bare `--json` without `--out` (auto-stripped on hosts that support argument overwrite), and redundant post-`diagnose` fetches.
- **Stop verification & auto-remediation (`verify-stop`)**: checks `turn_receipts.jsonl` and scans the final assistant text (`guardrails/reply_patterns.py` + `reply_scanner.py`: V1 PoE2/legacy/mechanic fallacies, V2 compute receipts for DPS/EHP claims, V3 slot inspection receipts for open-affix claims, V4 `0.033s` tick receipts for trigger breakpoints, V5 web-research verification for hedging or mechanics-interaction answers — disable independently with `POB_HOOK_DISABLE_V5=1`). On failure, it runs fast in-process remediation or cached research lookup and injects a single correction pass (`maxRetries = 1`, fail-open on errors).

Ready-to-use host templates and plugin integration:

| Host | Template / Plugin | Protocol |
|---|---|---|
| **Antigravity** | [`hooks/hooks.json`](hooks/hooks.json) (copy to `.agents/hooks.json`) | `--protocol agy` |
| **Claude Code** | [`hooks/claude-hooks.json`](hooks/claude-hooks.json) (merge into `.claude/settings.json`) | `--protocol claude` |
| **DeepSeek Harness (DSH)** | `dsh-pob-assistant` plugin (`agent/pre-step`, `tools/pre-execute`, `agent/turn-stopping`) | `--protocol raw` |

---

## Exit codes

Scripts and agents can use exit codes to decide what to do next. Errors are shown as short messages, without a raw Python traceback:

| Command | Code | Meaning and suggested action |
|---|---|---|
| General/build commands | `0` | Success |
| | `1` | Invalid arguments or build state, such as an uninitialized engine, empty slot, or damaged share code; fix the request or load a build first |
| | `5` | Daemon, PoB engine, or network failure; follow the error message and check `daemon.log` if needed |
| `pob trade` commands | `0` | Search or conversion succeeded |
| | `1` | Invalid argument or unmappable trade stat; similar stats are suggested |
| | `2` | No matching active listings; relax modifier thresholds or budget |
| | `3` | Official trade API returned 401/403, or Cloudflare blocked the request; set `POESESSID` in the environment if needed |
| | `4` | GGG rate limit reached; wait for the reported `Retry-After` period |
| | `5` | Official trade API error or network timeout |
| | `6` | Local PoB evaluation failed when `--pob on` was requested |

---

## Development, tests, and releases

```powershell
# Install the locked development environment into pob_cli/.venv (same versions as CI)
uv sync --project pob_cli --extra dev
# or, without uv: pip install -e "./pob_cli[dev]"

# Run unit and headless integration tests; set POB_OFFLINE=1 for offline mode
python -m pytest pob_cli/tests -v

# Build the standalone Windows x64 directory package at dist/pob/pob.exe
pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec
```

**Tag-driven releases** (`.github/workflows/release.yml`):

1. Update `__version__` in `pob_cli/src/pob_cli/__init__.py` and merge it into `main`.
2. Create and push a version tag: `git tag -s v0.5.0 -m "pob4agents v0.5.0" && git push origin main v0.5.0`.
3. GitHub Actions checks the version, runs the real-engine gate on Windows, macOS and Linux (`engine.yml`), runs tests, builds and smoke-tests `pob-vX.Y.Z-windows-x64.zip`, the wheel / sdist, and `SHA256SUMS.txt`, publishes the GitHub Release, and updates the Scoop manifest at `bucket/pob.json`. Every release also carries a byte-identical stable alias `pob-windows-x64.zip` so `releases/latest/download/pob-windows-x64.zip` always downloads the newest build without editing the version (`latest` skips prereleases).

To contribute, start with [CONTRIBUTING.md](CONTRIBUTING.md). For a compact command reference for agents, see [AGENTS.md](AGENTS.md).

---

## Credits and license

### Upstream projects and inspiration

- The architecture was inspired by [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp) (GPL-3.0) and [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP).
- [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh) provides the localized engine and headless bridge (`pob-zh.exe --engine-headless` + `bridge.lua`). Core build calculations and game data come from [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding).

### Community contributors

- **[@ChangMenQAQ](https://github.com/ChangMenQAQ)** ([PR #1](https://github.com/exilemoulei/pob4agents/pull/1)): official trade search, same-tier Harvest resistance conversion, and crafting bench suggestions.
- **[@restartgr](https://github.com/restartgr)** ([PR #2](https://github.com/exilemoulei/pob4agents/pull/2), [PR #4](https://github.com/exilemoulei/pob4agents/pull/4), [PR #6](https://github.com/exilemoulei/pob4agents/pull/6)): inflated-panel audit, `--keep`, build restoration checks, CLI exit codes, and bilingual README polish.
- **[@Capoouo](https://github.com/Capoouo)** ([PR #3](https://github.com/exilemoulei/pob4agents/pull/3)): PoB-calculated Timeless Jewel seed search and candidate filtering.

### License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](LICENSE).
