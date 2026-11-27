# What-If Mutations, Batch, Sweep & Matrix (`pob`)

All mutators run in **0-residue what-if mode** by default (snapshot -> mutate -> PoB calc -> rollback + `verify_restored`). Pass `--apply` only when the user wants to persist changes (`pob undo` / `pob redo` supported).

## Multi-Slot Parallel Comparison & Staged A->B Upgrade Recipe
Target isolated build slots with `--target <slug>` (default `main`, reference `ref`):
```powershell
# 1. Load current (main) & target (ref) builds and inspect full structural diff:
pob load "https://pobb.in/..." --target main
pob load "https://pobb.in/..." --target ref
pob compare                                              # 3 baselines (Original / Env-Aligned / Pinnacle Dewatered) + 15 gear/flask slots + 3.30 Wickerman Viscera (`Head/Body/Arm/Leg`) + `jewelSocketDiff` paired by `nodeId`
# 2. Price the reference build for upgrade budgeting:
pob trade appraise --target ref [--league L] [--max-live-queries 8]
# 3. Run warm 0-residue staged upgrade deltas in one call (or single-shot `pob borrow`):
pob sweep --scenario Pinnacle --ref-target ref --json-variants '[{"name":"S1_Viscera","ops":[{"op":"borrow","items":["Body","Arm","node:28475"]}]},{"name":"S2_TreeMain","ops":[{"op":"borrow","skill_group":"main","tree":true}]}]'
pob borrow --item-slot "Body,Arm,node:28475" [--skill-group main] [--tree [--no-tree-jewels]] [--scenario Pinnacle [--keep SPEC]] [--apply]
```
Two-slot commands (`pob compare`, `pob borrow`, `pob tree diff`) require distinct main and reference slots; naming the same slot twice fails with exit code `1`.

## Scenario Matrix & Pinnacle Dewater V2 (`pob audit` / `pob matrix`)
- **Dewater V2 Three-State Engine**: Pinnacle/Uber dewatering classifies config/flask/skill entries into **`AutoKeep`** (build-guaranteed mechanisms such as Mageblood leftmost/rightmost Magic Utility flasks, sustained flasks, or proxy-safe on-hit/crit triggers), **`AutoClamp`** (boss-tier ailment/shock clamping for Pinnacle vs Uber), and **`Strip`** (inflated padding, unverified customMods, guard/CD burst skills).
- **Cycle / Burst Ref**: Sustained main panels keep guard/CD burst skills OFF while emitting a 1-line **`Cycle / Burst Ref`** duty-cycle envelope (`pob audit`, `pob diagnose`, `pob matrix`, `pob sweep`, `pob compare`).
```powershell
pob audit [--boss pinnacle|uber] [--keep SPEC] [--no-auto-keep] [--no-attribution]
pob matrix Original Pinnacle Uber FlasksOff "BossWithFrenzy:Pinnacle,useFrenzyCharges=true"
```

## Stat Weights, Mod Interactions & Pairwise Synergy (`weights` / `interactions` / `--ablate --synergy`)
```powershell
# PoB-verified 0-residue stat sensitivity probes & build-choice presets:
pob weights [--mod "<mod line>"]... [--preset bandit|jewels|pantheon-major|pantheon-minor] [--scenario Pinnacle] [--keep SPEC] [--score CombinedDPS=1,TotalEHP=0.5] [--floor "SpellSuppressionChance>=100"]

# Inspect ModParser interactions, main-skill flag mismatches, and unparsed red mods (^xF05050, 0 calc effect):
pob interactions [--slot S] [--mod "<candidate mod>"]... [--unsupported-only]

# Single-mod + Top-K pairwise joint toggle-off ablation (detects super-additive synergies & joint req collapse):
pob item inspect "<slot>" --ablate [--synergy [--synergy-top 5]] [--json]
```

## Validated Passive Tattoos (`pob tree tattoo`) & 5-Tier Auto-Rebalance (`pob rebalance`)
- **`pob tree tattoo`** enforces 4 hard safety guards before applying: (1) slot attribute/notable type match, (2) `MinimumConnected`/`MaximumConnected` counting **allocated** adjacent passives (stricter than PoB static `linkedId`), (3) `Limited to N <group>` cap across the tree, and (4) `[REQ-DEFICIT]` Str/Dex/Int/Omni requirement check (exit `1` when `--strict-req` is passed).
- **`pob rebalance`** solves attribute, elemental resistance, optional Chaos (`--include-chaos` -> `0%`), and Spell Suppression (`--include-suppression` -> `100%`) deficits via CoW-verified 5-tier search: **Tier 1** Crafting Bench recraft, **Tier 2** Harvest single-element swap (worst-case tier min-roll floor), **Tier 3** Jewelry `+20%` Intrinsic/Prismatic Catalyst, **Tier 4** free small-node Tattoos on surplus-attribute passives, **Tier 5** Trade fallback when zero-buy tiers cannot close the gap.
```powershell
pob data tattoo <query> [--kind tattoo|runegraft] [--slot small_dex|notable_attr] [--for-build [--target SLUG]]
pob tree tattoo --node <id|name> [--tattoo "<EN|ZH|clear>"] [--show-legacy] [--strict-req] [--apply]
pob rebalance [--try-item <file|raw|-> --slot <S>] [--max-steps 3] [--max-evals 24] [--include-chaos] [--include-suppression] [--apply-plan N]
```

## Atomic Multi-Op Transaction (`pob batch`)
Supported op types (24, aliases in parentheses; 41 names in all): `borrow` (`transplant`), `config` (`config_set`), `flask` (`item_flask`), `item` (`item_try`), `item_db`, `item_edit`, `unequip` (`item_unequip`, `unequip_slot`), `anoint` (`item_anoint`), `tree` (`tree_change`), `tattoo` (`tree_tattoo`), `tree_reset` (`reset_tree`), `import_tree_url`, `new_build`, `set_level`, `set_class` (`tree_set_class`), `switch_main`, `add_group` (`new_group`, `skill_new_group`), `delete_group` (`rm_group`, `remove_group`), `set_group`, `replace_group` (`skill_replace_group`), `skill`, `skill_set`, `skill_add`, `skill_rm`.
- `"tattoo"` (`"tree_tattoo"`) applies or clears a validated passive Tattoo/Runegraft (`"node"`, `"tattoo"` / `"clear"`, optional `"show_legacy"`, `"strict_req"`).
- `"borrow"` (`"transplant"`) requires `--ref-target <slug>` (default `ref`, captured once in memory across the whole `batch`/`sweep`), and accepts `"items"` / `"item_slots"` / `"itemSlots"` / `"slots"` / `"slot"` / `"item_slot"` (list or CSV of gear slots, 3.30 Wickerman Viscera `Head`/`Body`/`Arm`/`Leg` or `viscera:<part>`, and active passive-tree sockets `node:<nodeId>` / `socket:<nodeId>`), `"skill_group"` / `"skillGroup"` (label, index, or `"main"` to transplant and activate the reference main skill), `"tree"` / `"borrow_tree"` / `"borrowTree"` (bool), and `"tree_jewels"` / `"treeJewels"` (bool, default `true`).
- `"add_group"` takes `"gems"` as `"Name[:level[:quality]]"` strings (or gem objects), plus `label`, `slot`, `makeMain`; `"tree"` takes `"allocate"` / `"deallocate"` lists of node ids or names (`pob tree --search "<kw>" --show-path`).
- `"skill_add"` / `"skill_set"` / `"skill_rm"` are `"skill"` with that action as the default (an explicit `"action"` in the step still wins, so leave it out): `{"op":"skill_add","group":"main","gem":"Controlled Destruction","level":20,"quality":0}`, `{"op":"skill_rm","group":"main","gem":"Spell Echo"}`.
- `"config"` takes `"set": {"useFrenzyCharges": true, "enemyIsBoss": "Pinnacle"}` (one or many variables), `"reset"`, and `"customMods"` / `"appendCustomMods"`; a bare `"var"`/`"value"` pair is refused with the `"set"` form to use.
- `"flask"` (`"item_flask"`) takes `"slot"` or `"flask"` (`1`–`5`) and `"active"` / `"state"` / `"enabled"` (`true`/`false`/`on`/`off`/`active`/`inactive`; conflicting aliases fail pre-run validation).
- `"skill"` supports `"skill_part"` (1-based sub-skill part index for multi-part gems such as `Ice Spear`, `Blade Vortex`, or transfigured variants).
- **Strict pre-run validation & exit codes:** every batch step is validated before the first step runs with strict field types (integers reject booleans, lists, objects, and fractions like `12.5`; booleans accept `true`/`false`/`0`/`1`/`on`/`off`/`yes`/`no`; `allocate`/`deallocate`/`addMods`/`removeMods`/`gems` take lists, and `replaceMods` takes `[old, new]` pairs). Invalid batch/trade arguments or same-slot `compare`/`borrow`/`tree diff` exit `1` (`pob trade` reserves exit `2` for "no results").
```powershell
pob batch --json-ops '[{"op":"config","set":{"usePowerCharges":true,"enemyIsBoss":"Pinnacle"}},{"op":"anoint","slot":"Amulet","notable":"Whispers of Doom"},{"op":"skill","group":"main","gem":1,"skill_part":2}]' --return summary,audit
```

## Scoped `calc`, Repeatable `tree change` & PowerShell 5.1 `--out`
```powershell
pob calc "CombinedDPS" "ManaCost" --group 2 --skill 1                     # Inspect a specific socket group / active skill without switching main
pob calc "Life" "FireResist" "LifeRegen" --group <N>                      # Pure minion groups (e.g. Animate Guardian) auto-switch to minion actor; @minion / @player suffixes override explicitly without mutating main skill or player ItemSet
pob tree change --allocate 26725 --allocate 41263 --deallocate 36634      # Repeatable --allocate / --deallocate flags (or comma-separated IDs)
pob diagnose '@build.xml' --json --out report.json                        # Writes clean UTF-8 (no BOM); prefer --out <file> on Windows PowerShell 5.1 instead of > / pipe
```

## Parallel Variant Sweep (`pob sweep`)
Evaluate multiple candidate setups under one scenario (`Pinnacle` etc.) in a single daemon call. Every variant must wrap its steps inside `"ops": [...]` (misplaced top-level `"op"` or step keys fail pre-run validation with exit `1`):
```powershell
pob sweep --scenario Pinnacle [--no-auto-keep] --json-variants '[{"name":"Echo","ops":[{"op":"skill","group":"main","gem":2,"new_gem":"Spell Echo"}]},{"name":"Unleash","ops":[{"op":"skill","group":"main","gem":2,"new_gem":"Unleash"}]}]'
pob sweep --scenario Pinnacle --ref-target ref --json-variants '[{"name":"S1_BodyArm","ops":[{"op":"borrow","items":["Body","Arm","node:28475"]}]},{"name":"S2_FullTree","ops":[{"op":"borrow","skill_group":"main","tree":true}]}]'
```

## Tolerant Slot & Flag Aliases
- Slots accept EN & ZH shortcuts: `ring1`/`左戒指` -> `Ring 1`, `ring2`/`右戒指` -> `Ring 2`, `chest`/`body armour`/`衣服`/`胸甲` -> `Body Armour`, `helm`/`头盔` -> `Helmet`, `gloves`/`手套` -> `Gloves`, `boots`/`鞋子` -> `Boots`, `amulet`/`项链` -> `Amulet`, `belt`/`腰带` -> `Belt`, `weapon1`/`主手` -> `Weapon 1`, `weapon2`/`offhand`/`副手` -> `Weapon 2`, `flask1..5` -> `Flask 1..5`. Traditional spellings (`頭盔`, `項鍊`) resolve the same way.
- **3.30 Wickerman Viscera vs Body Armour**: `Head` / `Body` / `Arm` / `Leg` (or `viscera:head|body|arm|leg`, `graft:head|body|arm|leg`) target the 4 Wickerman Viscera slots (`Body` resolves to Wickerman `Body` when present, while `Body Armour` / `chest` / `胸甲` always target the chest armour slot).
- **Passive-tree jewel sockets**: `node:<nodeId>` / `socket:<nodeId>` pair active passive-tree jewel sockets by stable tree `nodeId` across builds (preferred over `#1` build-local socket index shortcuts when comparing or borrowing between builds).
- Flag aliases: `item anoint --oil` (`--notable`), `skill add/set/rm --gem-name` (`--gem`).
