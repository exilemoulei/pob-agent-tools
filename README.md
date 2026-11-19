# pob-agent-tools (`pob`)

High-signal CLI, managed dual-upstream headless engine, offline Lua game-data SQLite index, and persistent dual-slot daemon for **Path of Building (PoE 1)**, purpose-built for AI Agents and power users.

> **Acknowledgements & Lineage**  
> Inspired by [**ianderse/pob-mcp**](https://github.com/ianderse/pob-mcp) (GPL-3.0) and [**shalayiding/POEMCP**](https://github.com/shalayiding/POEMCP), powered by [**Hsiung-Shao/PobTools-zh**](https://github.com/Hsiung-Shao/PobTools-zh) (`pob-zh.exe --engine-headless` + `bridge.lua`) and official [**PathOfBuildingCommunity/PathOfBuilding**](https://github.com/PathOfBuildingCommunity/PathOfBuilding).

---

## Key Features (v0.2)

- **Managed Dual-Upstream Engine & Auto-Update (`pob bootstrap` / `pob update`)**:
  - Automatically resolves `POB_ROOT` -> `<POB_RUNTIME_DIR>/engine` (defaults to `%LOCALAPPDATA%\pob-agent-tools\engine` when installed, or `<repo>/.runtime/engine` in a source checkout).
  - Checks upstream on daemon startup (and every 30m with zero-loss slot hot-reload) via `PobTools-zh` `302` release tags (never `api.github.com`) + official PoB `manifest.xml` SHA1 incremental diff with `gate_result` smoke probe & `.bak/` rollback.
  - Fast-fails (`connect=3s`) when GitHub is unreachable, degrades cleanly to local engine, and emits dual-channel alerts (`stderr` + JSON `updateWarning` / Markdown `> [WARN]`).
- **Zero-Residue `what-if` Simulation & 30-Step Undo/Redo**: Every mutation (`item try/edit/db/flask/anoint`, `skill set/add/rm`, `tree change/set-class`, `config set [--append-mods]`, `batch`, `borrow`) runs inside an in-memory `export_code`/`import_code` transaction and rolls back unless `--apply` is passed.
- **Multi-Scenario Matrix (`pob matrix`) & 4-Dim Inflation Audit (`pob audit`)**: Compare `Original`, `Pinnacle`, `Uber`, `FlasksOff`, and custom `Label:Pinnacle,key=val` scenarios in one atomic pass, or strip Config/Flask/Skill/Party water automatically.
- **3-Baseline A/B Comparison (`pob compare`) & Transplant (`pob borrow`)**: Compare `main` vs `ref` across Original, Target-Env-Aligned, and Dewatered Pinnacle baselines with tree/skill/gear diffs.
- **0ms Local Offline Game Data (`pob data`)**: Pure-Python PoB Lua parser + SQLite/FTS5 index (`gem`, `unique` incl. Foulborn, `mod` incl. Bench/Eldritch/Corrupted, `base`, `translate` EN<->ZH) running daemon-free in `<15ms`.
- **External Economy, Meta, PoEDB & Wiki (`pob price` / `pob meta` / `pob poedb` / `pob wiki`)**:
  - `poe.ninja`: 52 overview categories, `--variant`, `--limit`, `--detailed` (corrupted/21-20 gems/links/listings via stash API), `pob meta` (`build-index-state`), and fixed character URL import (`Name-1234`).
  - `poedb.tw`: 24h CN/US autocomplete index + 12h cleaned pages with `DropChance` weights, `ModFamilyList`, and `fossil_no` tags.
- **AI Gear Upgrade Workflow (`pob trade`)**: official PoE1 trade search, comparison against the equipped item, Harvest same-tier element conversion, and headless PoB DPS/EHP verification. See [docs/TRADE.md](docs/TRADE.md) and the default behavior in [AGENTS.md](AGENTS.md).
- **Crafting Bench Lookup (`pob bench`)**: offline poedb/wiki snapshot of bench modifiers per slot (prefix/suffix, roll range, cost, unlock), used for the "buy an open-affix base and craft" fallback.

---

## For AI agents

This repository is designed to be driven by an AI agent; [AGENTS.md](AGENTS.md) is the
contract it reads first (and `CLAUDE.md` imports it for Claude Code). The default
gear-upgrade loop is:

```powershell
pob load "<build source>"
pob summary
pob trade search --slot gloves --budget 20 --stat "to Cold Resistance=40" --pob auto
```

`pob trade` searches the official trade site (read-only), compares listings with the
equipped item, expands Harvest-convertible elements when asked (`--allow-conversion`),
and verifies the top candidates through the local headless PoB. DPS/EHP only ever comes
from PoB; `-` means "not verified". When no listing fits, `--open-suffix` / `--open-prefix`
plus `pob bench suggest` plan a crafting-bench fallback (bench mods are lower tier, so
quote the minimum). The `pob-trade-upgrade` skill in `.claude/skills/` describes the full
workflow.

---

## Installation (Windows x64)

No Python installation is required for the standalone Windows binary.

### Option 1: One-Click PowerShell Installer (Recommended, No Python Needed)

```powershell
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

If GitHub requires a proxy on your network:

```powershell
$env:POB_PROXY = "http://127.0.0.1:7890"
irm https://raw.githubusercontent.com/exilemoulei/pob-agent-tools/main/install.ps1 | iex
```

Re-run the same command anytime to upgrade to the latest release (verifies SHA256 and stops the old daemon automatically).

### Option 2: Scoop Package Manager (Easy Updates)

This repository doubles as a Scoop bucket (`bucket/pob.json`, auto-updated on every tagged release):

```powershell
scoop bucket add pob https://github.com/exilemoulei/pob-agent-tools
scoop install pob

# Update to new releases anytime:
scoop update pob
```

### Option 3: Portable Zip from GitHub Releases

Download `pob-vX.Y.Z-windows-x64.zip` from [GitHub Releases](https://github.com/exilemoulei/pob-agent-tools/releases/latest), unzip it, and add the folder containing `pob.exe` to your `PATH`.

### Option 4: Python Package (`uv` / `pipx` / `pip`, Python 3.11+)

```powershell
# With uv (recommended for Python users)
uv tool install "git+https://github.com/exilemoulei/pob-agent-tools.git#subdirectory=pob_cli"

# Or with pipx / pip (non-editable, from a release wheel or git tag)
pipx install "git+https://github.com/exilemoulei/pob-agent-tools.git@v0.2.0#subdirectory=pob_cli"
pip install "https://github.com/exilemoulei/pob-agent-tools/releases/download/v0.2.0/pob_cli-0.2.0-py3-none-any.whl"
```

### First-Time Engine Setup

After installing `pob`, download the headless PoB engine once (or import an existing local `PobTools` directory):

```powershell
pob --version
pob bootstrap                                  # Direct GitHub download
pob bootstrap --proxy http://127.0.0.1:7890    # Via HTTP/SOCKS5 proxy
pob bootstrap --from-dir "D:\path\to\PobTools" # Offline seed from local PobTools
```

Supported environment variables: `POB_ROOT`, `POB_RUNTIME_DIR` (defaults to `%LOCALAPPDATA%\pob-agent-tools`), `POB_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, `POB_GITHUB_MIRROR`, `POB_OFFLINE=1`.

---

## Quick Start

```powershell
# 1. Check engine versions, update state, and load builds (URL, poe.ninja char, .xml, Base64)
pob status
pob update --check
pob load "https://poedb.tw/pob/AWqOLSSmyG"

# 2. Inspect summary, water audit, config, all equipped items/jewels, skills, tree, and calcs
pob summary
pob audit
pob config list
pob items --all --raw
pob skills --group main
pob calc "Average Hit"

# 3. What-if simulations (add --apply to persist; pob undo / pob redo to revert)
pob item edit --slot "Ring 1" --replace-mod "Cold Resistance" "+45% to Lightning Resistance"
pob item db "Mageblood" --equip
pob config set --append-mods "+100 to maximum Life"
pob tree set-class Witch Necromancer
pob matrix Original Pinnacle Uber FlasksOff

# 4. Rankings, 3-baseline comparison, and gear/skill transplant
pob rank-nodes --stat CombinedDPS --max-dist 4 --top 10
pob rank-gems --group main --top 10
pob compare "https://poedb.tw/pob/AWqOLSSmyG"
pob borrow --item-slot "Weapon 1,Amulet" --skill-group main

# 5. Local 0ms game data & external poedb / poe.ninja / wiki queries
pob data mod "Life Converted to Energy Shield"
pob data gem "Dark Bargain"
pob data translate "喋血之刃"
pob poedb "傲视头冠" --mods
pob price "Forbidden Flame" --variant "Necromancer" --limit 50
pob price "Empower Support" --detailed
pob meta --top 15
pob wiki "Eldritch Battery"
```

Pass `--json` to any command for structured, color-stripped JSON output. Pass `--offline` before a subcommand (or set `POB_OFFLINE=1`) to skip GitHub update checks. See [AGENTS.md](AGENTS.md) for the compact agent cheat-sheet.

---

## Development & Releasing

```powershell
# Local editable dev install
pip install -e "./pob_cli[dev]"

# Run tests
python -m pytest pob_cli/tests -v

# Build standalone Windows binary locally
pyinstaller --noconfirm --clean pob_cli/packaging/pob.spec
```

**Tag-driven releases** (`.github/workflows/release.yml`):
1. Bump `__version__` in `pob_cli/src/pob_cli/__init__.py` and commit to `main`.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. GitHub Actions (`windows-latest`) validates the tag, runs tests, builds & smoke-tests `pob-v0.2.0-windows-x64.zip` + wheel/sdist + `SHA256SUMS.txt`, publishes the GitHub Release, and auto-updates `bucket/pob.json` for Scoop.

## License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)** — see [LICENSE](LICENSE) for details.
