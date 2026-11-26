# pob24agents (`pob2_cli`)

`pob24agents` (`pob2`) is the Path of Exile 2 (`0.5.5+`) headless Path of Building CLI, daemon, and crafting solver.
It is physically isolated from `pob_cli/` (`pob`, PoE1) and licensed under **`GPL-3.0-only`** (see `ATTRIBUTION.md`).

## Commands

```bash
python pob2_cli/scripts/check.py --fast          # ruff + AST arch/isolation/leak lint (<3s)
python pob2_cli/scripts/check.py --self-test     # mutation proof that every gate rule fires
python pob2_cli/scripts/check.py                 # fast gate + isolated offline pytest
POB2_OFFLINE=1 python -m pytest pob2_cli/tests -q
```

## Architecture & Isolation Hard Rules

- **Strict 5-layer DAG (`L0 -> L4`)**: imports point downward only, zero top-level cycles.
  - `L0`: `__init__`, `game_profile`, `os_util`, `compat`, `json_io`, `errors`, `han_fold`, `locale_cfg`, `guardrails`
  - `L1`: `updater`, `bridge_*`, `importers`, `profile_import`, `game_data`, `external_data`, `poedb_client`
  - `L2`: `formatters`, `snapshot`, `analyzer`, `mutators`, `comparator`, `craft`, `rune`, `timeless`, `waystone`, `rebalance`, `item_inspect`, `trade_*`
  - `L3`: `daemon`
  - `L4`: `__main__`, `cli*`
- **Zero cross-package coupling**: NEVER `import pob_cli` or read `POB_*` env vars. Use `POB2_*` env vars (`POB2_RUNTIME_DIR`, `POB2_ENGINE_DIR`, `POB2_DATA_DIR`, `POB2_LANG`, `POB2_SLOT`, `POB2_OFFLINE`), `X-PoB2-Token`, `%LOCALAPPDATA%/pob24agents`, and `"game": "poe2"`.
- **Code budgets (no ignore pragmas)**: hand-written `.py`/`.lua` `<=650L` & `<=24,000B` (worst-case CRLF), functions `<=200L`; test files `<=1,000L` & `<=38,000B`.
- **Runtime dependencies**: strictly `click`, `httpx[socks]`, `tabulate` (NO `pydantic`, `typer`, `rich`, `mcp`).
- **Windows & POSIX runtime**: Windows child spawns MUST use `os_util` (`CREATE_NO_WINDOW` + hidden `STARTUPINFO`; NEVER `DETACHED_PROCESS`). POSIX MUST use pinned LuaJIT `2460b3f` + `luautf8 0.1.6` (stock LuaJIT breaks on PoB2 `+=`).
- **Zero repo pollution**: never commit local paths, bot working notes (`PROGRESS.md`, `SPEC.md`, `*_PLAN.md`), engine binaries, or real player share codes.

## PoE2 `0.5.5+` Mechanics Red Lines

- **8 non-contiguous class IDs**: `1` Witch, `2` Ranger, `6` Warrior, `7` Sorceress, `8` Huntress, `9` Mercenary, `10` Monk, `11` Druid.
- **Dual Weapon Sets**: node-level `allocMode (0|1|2)`, per-skill `set1`/`set2` (`weaponSetEnvs`), and `+5 Str/Dex/Int` attribute overrides must snapshot & roll back idempotently.
- **Core rule flips vs PoE1**: Spirit pool (`Spirit - SpiritUnreserved`); 50% base block cap; Deflection; Armour applies BEFORE Ele Res; two-step final-only conversion; Quality = 1% MORE; Chaos 2x vs ES & Bleed/Poison bypass ES; NO `ticks.py` (Meta Energy replaces 0.033s CoC breakpoints); NO `tj_open`/`tattoo_open`.
- **0.3+/0.5.5 Support & Crafting rules**: normal supports stack across skills — only `isLineage` & global reservation supports are 1-copy (same-skill excludes by `gemFamily`). Max 1 `Crafted` mod per item (fractured still occupies slot; `Desecrated` max 1 independently). Scouring/Alteration/Homogenising Omens/Recombinator are removed.
- **Engine shields**: guard `#2529` (`mainOutput == nil` crash) and `#2582` (missing `0.5.5` base silent weapon drop).
- **Exit codes & JSON**: `0` OK, `1` request/guardrail error, `5` daemon/engine fault; JSON keys stay English.
