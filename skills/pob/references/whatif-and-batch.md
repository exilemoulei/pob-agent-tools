# What-If Mutations, Batch, Sweep & Matrix (`pob`)

All mutators run in **0-residue what-if mode** by default (snapshot -> mutate -> PoB calc -> rollback + `verify_restored`). Pass `--apply` only when the user wants to persist changes (`pob undo` / `pob redo` supported).

## Multi-Slot Parallel Comparison
Target isolated build slots with `--target <slug>` (default `main`, reference `ref`):
```powershell
pob load "https://pobb.in/..." --target main
pob load "https://pobb.in/..." --target ref
pob compare                                              # Original / Env-Aligned / Pinnacle Dewatered
pob borrow --item-slot "Weapon 1,Amulet" [--apply]       # Transplant ref items into main
```

## Scenario Matrix (`pob matrix`)
```powershell
pob matrix Original Pinnacle Uber FlasksOff "BossWithFrenzy:Pinnacle,useFrenzyCharges=true"
```

## Atomic Multi-Op Transaction (`pob batch`)
Supported op types (19): `config`, `flask`, `item`, `item_db`, `item_edit`, `unequip`, `anoint`, `tree`, `tree_reset`, `import_tree_url`, `skill`, `add_group`, `delete_group`, `set_group`, `replace_group`, `new_build`, `set_level`, `set_class`, `switch_main`.
- `"config"` takes `"set": {"useFrenzyCharges": true, "enemyIsBoss": "Pinnacle"}` (one or many variables), `"reset"`, and `"customMods"` / `"appendCustomMods"`; a bare `"var"`/`"value"` pair is refused with the `"set"` form to use.
- `"skill"` supports `"skill_part"` (1-based sub-skill part index for multi-part gems such as `Ice Spear`, `Blade Vortex`, or transfigured variants).
```powershell
pob batch --json-ops '[{"op":"config","set":{"usePowerCharges":true,"enemyIsBoss":"Pinnacle"}},{"op":"anoint","slot":"Amulet","notable":"Whispers of Doom"},{"op":"skill","group":"main","gem":1,"skill_part":2}]' --return summary,audit
```

## Scoped `calc`, Repeatable `tree change` & PowerShell 5.1 `--out`
```powershell
pob calc "CombinedDPS" "ManaCost" --group 2 --skill 1                     # Inspect a specific socket group / active skill without switching main
pob tree change --allocate 26725 --allocate 41263 --deallocate 36634      # Repeatable --allocate / --deallocate flags (or comma-separated IDs)
pob diagnose @build.xml --json --out report.json                          # Writes clean UTF-8 (no BOM); prefer --out <file> on Windows PowerShell 5.1 instead of > / pipe
```

## Parallel Variant Sweep (`pob sweep`)
Evaluate multiple candidate setups under one scenario (`Pinnacle` etc.) in a single daemon call:
```powershell
pob sweep --scenario Pinnacle --json-variants '[{"name":"Echo","ops":[{"op":"skill","group":"main","gem":2,"new_gem":"Spell Echo"}]},{"name":"Unleash","ops":[{"op":"skill","group":"main","gem":2,"new_gem":"Unleash"}]}]'
```

## Tolerant Slot & Flag Aliases
- Slots accept EN & ZH shortcuts: `ring1`/`左戒指` -> `Ring 1`, `ring2`/`右戒指` -> `Ring 2`, `chest`/`body`/`衣服`/`胸甲` -> `Body Armour`, `helm`/`头盔` -> `Helmet`, `gloves`/`手套` -> `Gloves`, `boots`/`鞋子` -> `Boots`, `amulet`/`项链` -> `Amulet`, `belt`/`腰带` -> `Belt`, `weapon1`/`主手` -> `Weapon 1`, `weapon2`/`offhand`/`副手` -> `Weapon 2`, `flask1..5` -> `Flask 1..5`, `#1` -> Jewel socket #1. Traditional spellings (`頭盔`, `項鍊`) resolve the same way.
- Flag aliases: `item anoint --oil` (`--notable`), `skill add/set/rm --gem-name` (`--gem`).
