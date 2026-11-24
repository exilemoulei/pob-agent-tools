# `pob` command reference

Every subcommand with its flags, in one place. For when to use which, start with the recipes in [skills/pob/SKILL.md](../../skills/pob/SKILL.md) and the examples in the [README](../../README_EN.md); `pob <command> --help` is authoritative when this list and the CLI disagree.

```powershell
# Optimization flow: pob audit (dewater & gaps) -> rank-nodes / rank-gems / anoint / bench -> pob trade search (verified via PoB)
# Global output & slot targeting: --target <slug> (alias --slot where non-conflicting; default POB_SLOT or main); --compact (1-line JSON); --out <file> (UTF-8 no-BOM).

# Lifecycle, Foreground Daemon, Engine Bootstrap/Update & Build I/O (127.0.0.1 + X-PoB-Token)
pob serve [--offline] [--proxy <url>] [--port N] [--runtime-dir <dir>]                 # Foreground persistent daemon (use IsDaemon:true in job-reaped shells)
pob bootstrap [--proxy <url>] [--mirror <url>] [--no-zh] [--from-dir <dir>] [--json]   # Install/seed managed engine (PobTools-zh + PoB master)
pob update [--check] [--force] [--proxy <url>] [--mirror <url>] [--offline] [--json]   # Incremental SHA1 update (hot-reloads live daemon slots)
pob status [--json]                                                                    # Daemon PID, PoB/PobTools/Data versions, boolean Auth env status, updateStatus, all named slots
pob self-update [--check] [--version X.Y.Z] [--proxy <url>] [--mirror <url>] [--json]  # Update pob itself (standalone builds; signed release only); --check reports latest + the update command for this install; network/disk/swap failure exits 5
pob profile [ACCOUNT_OR_URL] [--realm pc|xbox|sony|cn] [--league L] [--proxy U] [--json] # 0-engine official profile character list (pathofexile.com public no-cookie, or poe.game.qq.com via env POE_CN_SESSID)
pob new [--class C] [--asc A] [--secondary-asc S] [--level N] [--target <slug>] [--json]
pob load [SOURCE] [--char "<name>"] [--league L] [--realm pc|xbox|sony|cn] [--target <slug>] [--json] # XML, Base64, 8 share sites, poe.ninja char URL, or official Intl/CN profile URL
pob save <out.xml> | pob export-code [--target <slug>] [--json]                        # Export XML (CLI disk write, R11) or Base64 code
pob switch-main [--spec S] [--skill-group G] [--skill N] [--part N] [--item-set I] [--skill-set S] [--config-set C] [--loadout L]
pob undo | pob redo | pob stop [--json]

# Read-Only Inspection, Grounding, Hook Guardrails & One-Shot Diagnosis
pob ground "<prompt_text>" [--limit 8] [--json]                                        # <15ms daemon-free 3.30 entity cards + PoE2/patch trap alerts
pob hook <pre-prompt|pre-tool|verify-stop> [--protocol raw|agy|claude]                 # Stdin/stdout JSON lifecycle guardrails (grounding + [MECHANICS-RESEARCH-REQUIRED], shell pre-tool deny/rewrite, stop V1..V5 + auto-remediation)
pob diagnose [SOURCE] [-c|--compare REF] [-q|--ask "<text>"] [--char "<name>"] [--league L] [--realm pc|xbox|sony|cn] [--keep <spec>]... [--top 5] [--no-rank] [--target <slug>] [--ref-target <slug>] [--json] # One-shot: [Env (NOT PoE2) | rev=N] + Pinnacle audit + gaps/bricks/dead-supports/triggers + 3.30 cards + upgrades (or dual-build 3-baseline compare)
pob summary [--target <slug>] [--json]                                                 # [Env | rev=N] + Offence (Hit/DoT/FullDPS/Minion*), Defence (EHP/MaxHit/Res), Warnings
pob audit [--keep <spec>]... [--no-attribution] [--target <slug>] [--json]             # [Env | rev=N] + Hard gaps + 4-dim water audit -> Pinnacle (per-item PoB attribution; --keep var|Flask N|flasks|gem|customMods|party)
pob config list [--all] [--search "<sub >"] [--with-options] [--target <slug>] [--json] # Active/filtered ConfigTab vars + raw customMods
pob items [--slot "Weapon 1"|"#1"] [--all] [--raw] [--target <slug>] [--json]          # Equipped table, single slot, or all equipped + allocated jewels raw
pob item inspect (--slot S | --raw "<item text>" | --file f) [--want "Cold Resistance"]... [--target <slug>] [--json] # Rare-item affix occupancy and bench candidates; uncertain matches never claim free slots
pob skills [--group main|1|"label"] [--target <slug>] [--json]                         # Skill groups or per-gem DPS tooltips
pob tree [--search "<kw>" ...] [--type Keystone|Notable|Mastery|Socket|Ascendancy ...] [--allocated|--unallocated] [--max-cost N] [--show-path] [--masteries] [--socket-radius <id|#1>] [--json]
pob calc "<kw1>" ["<kw2>" ...] [--limit 20] [--no-breakdown] [--target <slug>] [--json] # Variadic EN/ZH get_calcs + calcs_breakdown formulas/tables; rows are for the skill PoB's Calcs tab selects (named in the output as `calcsSkill`, with a warning when it is not the main skill)

# What-If Simulation (0-residue rollback by default) & Persistent Mutation (--apply)
pob item try [--slot S] (--raw "<text>" | --file f) [--tooltip-only] [--apply]
pob item edit --slot S [--replace-mod <old> <new>]... [--add-mod <ln>]... [--remove-mod <sub >]... [--apply]
pob item unequip --slot S [--apply]
pob item db "<query>" [--type UNIQUE|RARE] [--slot S] [--index N] [--equip] [--with-tooltips] [--with-raw] [--apply] [--limit 15]
pob item flask <1..5|all> <on|off> [--apply]
pob item anoint [--slot Amulet] --notable "<name>" [--index 1..4] [--apply]
pob skill set --group main [--gem <idx|name>] [--new-gem "<name>"] [--level N] [--quality N] [--enabled true|false] [--count N] [--full-dps true|false] [--apply]
pob skill add --group main --gem "<name>" [--level 20] [--quality 20] [--apply]
pob skill rm --group main --gem <idx|name> [--apply]
pob skill new-group [--label L] [--slot S] [--gem "Name[:lvl[:qual]]"]... [--full-dps true|false] [--apply]
pob skill replace-group --group main (--gems '<json>' | --gem "Name[:lvl[:qual]]"...) [--label L] [--slot S] [--full-dps true|false] [--apply]
pob tree change [--allocate id1,id2:effectId] [--deallocate id3] [--allow-cascade|--no-allow-cascade] [--apply]
pob tree set-class [Class] [Ascendancy] [--secondary-asc <Bloodline|None>] [--apply]
pob config set [<var>=<val|reset> ...] [--custom-mods "<text|clear>"] [--append-mods "<text>"] [--apply]
pob batch (--file <ops.json|-> | --json-ops '[...]') [--apply] [--no-undo] [--keep <spec>]... [--return summary,audit,audit:full,matrix,calc:DPS;Mana,export] [--compact] [--out f]
# Batch ops: config, flask, item, item_db, item_edit, unequip, anoint, tree, tree_reset, import_tree_url, skill, add_group, delete_group, set_group, replace_group, new_build, set_level, set_class, switch_main
pob sweep (--file <vars.json|-> | --json-variants '[...]') [--base-ops '[...]'] [--calc K]... [--scenario Pinnacle] [--keep <spec>]... [--compact] [--out f] [--json]
pob matrix [Original Pinnacle Uber FlasksOff "Label:Pinnacle,k=v"] [-s <spec>]... [--keep <spec>]... [--full] [--json]  # Compact default; --full for raw 200-key stats

# ROI Ranking, 3-Baseline A/B Comparison & Transplant
pob rank-nodes [--stat CombinedDPS|TotalEHP|Life] [--max-dist 5] [--top 15] [--json]
pob rank-gems [--group main] [--slot N | --add-link] [--field CombinedDPS|FullDPS] [--top 15] [--json]
pob compare [<ref_source>] [--json]                                                    # 3 baselines: Original / Target Env Aligned / Pinnacle Dewatered
pob borrow [--item-slot "Weapon 1,Amulet"] [--skill-group main] [--apply] [--json]

# Local 0ms Game Data (Daemon-Free, Variadic) & External Economy/PoEDB/Wiki (Cached, Variadic)
pob data <gem|unique|mod|base|translate|tree|tick> ["<q1>" ...] [--slot S] [--ilvl N] [--mod "<stat>"] [--type T] [--asc A] [--cdr N] [--aps R] [--spells N] [--quality-breakpoints] [--search-quality Q] [--active-only] [--limit 25] [--rebuild] [--json]
pob poedb "<q1>" ["<q2>" ...] [--mods] [--filter "<kw>"] [--lang cn|us] [--proxy <url>] [--limit 60] [--json]
pob price "<q1>" ["<q2>" ...] [--league L] [--category all|unique|gem|imbuedgem|...] [--variant S] [--limit 20] [--detailed] [--proxy <url>] [--json]
pob meta [--league L] [--skill S] [--class C] [--top 15] [--proxy <url>] [--json]
pob research "<q1>" ["<q2>" ...] [--mods] [--filter "<kw>"] [--lang cn|us] [--proxy <url>] [--limit 40] [--json] # Concurrent poedb.tw + poewiki.net dual-source research (short entity names; auto-splits long NL sentences)
pob wiki "<q1>" ["<q2>" ...] [--proxy <url>] [--json]                                  # Local PoEDB bilingual index exact match + Mechanics/Interactions priority extraction

# Official PoE1 Trade (Read-Only, PoB-Verified) & Crafting Bench
pob trade stats "<text|中文>" [--slot S] [--limit 5] [--proxy U] [--json]              # Local TradeSiteStats.lua (0ms) or API fallback
pob trade search --slot S [--stat "text|中文=min"]... [--sum "a[*w]|b[*w]>=min"]... [--want T]... [--bench-craft "<stat>"]... [--budget N] [--currency chaos_equiv|chaos|divine] [--status available|securable|online|any] [--max-age HOURS] [--bargain PCT] [--search-sort asc|desc|sum] [--sort score|price|pob|sum] [--allow-conversion [--convert-to E]] [--open-prefix|--open-suffix|--open-affix] [--pob auto|on|off] [--target <slug>] [--dump-query] [--proxy U] [--json] # --bench-craft keeps originals + max-roll bench variants (PoB-ranked; budget=listing only; hard --stat pre-craft)
pob trade convert (--text T | --file f) --to fire|cold|lightning [--out f] [--json]    # Harvest 3.30 swap preview (same tier, rerolled)
pob bench list --slot S [--search "<text|中文>"] [--affix prefix|suffix] [--limit 25] [--json]
pob bench suggest --slot S --stat "<text|中文>" [--affix prefix|suffix] [--limit 5] [--json]

# Timeless Jewel Seed Search (PoB-verified on parallel throwaway engines; other Historic jewels removed)
pob timeless list [--target <slug>] [--json]                                           # 11 timeless jewels (EN/ZH) + build's allocated sockets
pob timeless search --jewel "<name|中文>" --socket <nodeId> [--conqueror C] [--stat CombinedDPS|FullDPS|TotalEHP|...] [--mod "<ln>"]... [--top 15] [--workers N] [--exact] [--idle] [--target <slug>] [--json]
```
