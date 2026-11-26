---
name: pob-poe1
description: Path of Exile 1 (PoE1 3.30, 流放之路1) build diagnosis, headless PoB calculation, DPS/EHP Pinnacle audit, passive tree/gem/item what-if optimization, Atlas map stash regex (map-regex), official PoE1 trade search, crafting bench, and timeless jewel seed lookup via local `pob` CLI. Trigger on PoE1, PoB, pobb.in, poe.ninja, 配装, 天赋, 宝石, 技能连法, 伤害计算, 脱水, 跑PoB, 洗图正则, 词缀正则, 市集, 工艺台, 永恒珠宝, 法血, 猎首, 尼米斯, 大法师, 正火, 闪打, 龙卷, 漩涡, 憎恨, 耐力球, 异灵之体, 异能魔力, 万古之剑, 圣宗, 喋血之刃, 暴击时施放, 吟唱时施放, 艾许之面, 残存, 内脏, 符文之结.
---

# PoE1 3.30 Headless PoB Skill (`pob4agents`, command `pob`)

**Three Provenance & Workflow Rules**
1. **Every Number Comes From `pob` Output** — calculate all DPS, EHP, max hits, resistances, CDR breakpoints, and upgrade deltas via `pob` (`diagnose`, `summary`, `audit`, `calc`, `batch`, `sweep`, `matrix`, `data tick`). Never estimate build stats from raw XML or training memory, and never apply PoE2 mechanics (`Spirit`/`精魂`, `Charm 1..3`, `Waystone`, `Soul Core`/`{rune}`, `Distilled`/`涂灌`, 1-support limit, final-type-only conversion, PoE2 `EB`). After `--apply` or loading a new build, read updated figures from the latest `[Env: ... (rev=N)]` output.
2. **Research Uncertain Mechanics & Tag Unmodeled Ones Explicitly** — verify post-3.23 gem/unique/ascendancy changes and interactions via `pob ground`, `pob data`, or `pob research "<entity>"` (`poedb.tw` + `poewiki.net`; also `pob wiki` / `pob poedb`). If a mechanic or interaction is still unmodeled by PoB or unverified after checking, **explicitly tag the statement `[PoB未模拟]` or `[待验证]`** — never fabricate certainty.
3. **Fast Subcommands (`<0.3s`) for Follow-Ups & CoW `batch`/`sweep`/`matrix` for Variants** — use `pob diagnose` once to load and baseline a build; for follow-up questions on an already loaded slot, prefer `<0.3s` targeted subcommands (`pob summary`, `pob items`, `pob skills`, `pob calc`, `pob tree`) instead of re-running heavy ranking. All mutators (`skill`/`item`/`tree`/`config`/`batch`/`sweep`/`matrix`) use in-memory shallow Lua CoW snapshots (0-residue rollback by default; pass `--apply` only to persist). Prefer `pob batch`, `pob sweep`, or `pob matrix` to compare multiple variants in one call. Markdown is default; pass `--json` (`--compact` / `--out <file>`) whenever structured fields help, and run `pob help --brief` or `pob <cmd> --help` whenever checking flags. Never print or inspect `POESESSID` / `POE_CN_SESSID` in shell commands (`pob status` shows boolean auth state).

## 1. Canonical Fast Recipes
- **A. Initial build diagnosis, cold-start skeleton & official profile import**:
  ```powershell
  pob diagnose @build.xml -q "<player question>" [--keep SPEC]... [--top 5] [--json]
  pob new --class Witch --asc Necromancer --level 90; pob batch --json-ops '[...]' --apply # Cold-start theorycraft from scratch (sequential)
  pob profile "https://www.pathofexile.com/account/view-profile/<Acc-1234>" [--realm pc|cn] [--league L] # 0-engine char list (public intl needs no cookie; CN uses env POE_CN_SESSID)
  pob diagnose "https://www.pathofexile.com/account/view-profile/<Acc-1234>" --char "<name>" -q "<question>"
  ```
- **B. Dual-build A/B comparison**:
  ```powershell
  pob diagnose @buildA.xml --compare @buildB.xml -q "<player question>"
  ```
- **C. Mechanics, local 0ms game data, `0.033s` CDR math & dual-source web research (daemon-free)**:
  ```powershell
  pob ground "<player question>"                                      # <15ms: 3.30 cards + PoE2/patch traps
  pob research "<q1>" ["<q2>" ...] [--mods] [--filter "<kw>"] [--lang cn|us] # Concurrent poedb.tw + poewiki.net (1-3 short entity names)
  pob wiki "<q1>" ["<q2>" ...] | pob poedb "<q1>" ["<q2>" ...] [--mods] [--filter "<kw>"] [--lang cn|us]
  pob data gem "<q1>" ["<q2>" ...] | pob data gem --quality-breakpoints --active-only [--limit 25]
  pob data unique [QUERY] [--mod "<stat_kw>"] [--slot S] | pob data mod "<q>" [--slot S] [--ilvl N] | pob data translate "<EN|中文>"
  pob data tree [Q...] [--type Keystone|Notable|Ascendancy|Bloodline] [--asc "<Asc>"] | pob data runegraft <query> | pob data tattoo <query> [--kind tattoo|runegraft]
  pob data tick <base_cd|coc|cospri|mjolner|poet|cwdt|soa> [--cdr N] [--aps R]        # Exact 0.033s tick & overspeed math
  pob meta [--skill "<EN|中文>"] [--class "<EN|中文>"] [--league L]                   # Compliant poe.ninja league meta share (EN/ZH/slang)
  ```
- **D. Trade, Crafting Bench & `<3.5s` Timeless upgrades**:
  Run `pob diagnose @build.xml --no-rank` (or `pob timeless list`) for gaps & jewel socket IDs, then run `pob trade search`, `pob bench suggest`, or `pob timeless search` (hybrid in-engine PRNG+mmap synthesis for Legion & 3.30 Abyss jewels).

## 2. Fast `<0.3s` Follow-Ups & CoW Zero-Residue What-If (`--apply` to Persist)
```powershell
pob help --brief | pob summary [--json] | pob audit [--keep SPEC] | pob calc "<kw1>" ["<kw2>" ...] [--group G] [--skill S] [--no-breakdown]
pob items [--slot "Ring 1"|"#1"] [--all --raw] | pob skills [--group main] | pob tree [--search "<kw>"|--socket-radius <id>] | pob config list [--all]
pob item inspect (--slot S | --raw "<txt>" | --file f) [--want "<stat>"]... | pob item try --slot S --raw "<txt>" [--apply] | pob item edit --slot S --add-mod "<ln>" [--apply] | pob item anoint --notable "<N>"
pob skill add|set|rm --group main ... [--apply] | pob tree change --allocate <id>[,<id>...] [--deallocate <id>[,<id>...]] [--apply] | pob config set <k>=<v> [--custom-mods "<txt>"] [--apply]
pob batch --json-ops '[...]' [--return "skills,summary"] | pob sweep --scenario Pinnacle --json-variants '[...]' | pob matrix Original Pinnacle Uber FlasksOff
pob rank-nodes [--stat CombinedDPS|TotalEHP|Life] [--score STAT=W,...] [--floor "STAT>=N"]... [--max-dist 5] [--refund] | pob respec [--score ...] [--floor ...] [--rounds 3] | pob rank-gems [--group main] [--slot N|--add-link|--optimize]
pob weights [--mod "<ln>"]... [--preset bandit|pantheon-major|pantheon-minor|jewels] [--scenario Pinnacle] [--keep SPEC] [--score STAT=W,...] [--floor "STAT>=N"]...   # PoB-verified ΔDPS/ΔEHP per probe mod (16 defaults; --mod replaces); --score ranks by Σ W×Δ% (trade DPS vs EHP), --floor drops results under a hard limit
pob bench suggest --slot boots --stat "抗性" | pob trade search --slot ring1 [--securable] [--auto-query | --stat "生命>=60" --sum "火抗|冰抗|电抗>=80"] [--bench-craft "最大生命"] [--max-per-seller 2] [--normalize-quality auto|on|off] --budget 5 --currency divine
pob trade upgrade --budget 100 [--slot ring1] [--top-slots 3]           # one-shot reference: free moves + weakest slots -> live PoB-verified trade -> best combo within budget
pob trade plan ring.json amulet.json --budget 100 [--score dps=1,ehp=0.5]   # offline: best one-listing-per-slot combo within budget from saved `trade search --pob on --json --out` files
pob timeless search --jewel "Lethal Pride" --socket <nodeId|id1,id2|all> --stat CombinedDPS [--exact] | pob lang [zh-CN|zh-TW|en]
pob map-regex [@build.xml] [--tier both|t16|t17] [--mode safe|brick] [--avoid "<id|kw>"] [--allow "<id|kw>"] [--min-quant 80] [--client-lang auto|zh-CN|zh-TW|en] # <=250-char 0-FP stash regex
```

## 3. High-Risk PoE1 3.23–3.30 Domain Facts
- **3.28 Awakened -> Exceptional Gems**: Regular **Awakened Support Gems** (`Awakened Elemental Focus`, `Awakened Spell Echo`, `Awakened CwC`, `Awakened CoC`) were **drop-disabled in 3.28 (Mirage)** and replaced by **Exceptional Support Gems** (max natural **Lv3**, corrupted **Lv4** — `Greater Spell Echo`, `Cooldown Recovery`, `Eldritch Blasphemy`, `Hexpass`, `Congregation`). Only `Awakened Empower`, `Awakened Enhance`, `Awakened Enlighten` remain as Exceptional gems (max natural **Lv4**, corrupted **Lv5**).
- **3.25–3.30 Removals & Reworks**: Removed `Divine Blessing` (`神圣祝福`), `Ancestral Protector`/`Warchief` (`先祖卫士`/`战士长`), Lab Enchants, Alt-Quality (`Anomalous`/`Divergent`/`Phantasmal`), Sextants, Left-Click instant skills (use `Automation`/`Autoexertion`); `Vortex` = `0.6s` cast; `Hatred` (`憎恨`) = Phys-as-Cold only (no `% more Cold`); `Endurance Charge` (`耐力球`) = **4% Phys & Ele DR** (no `+4% Ele Res`); `Taste of Hate` (`恨意`) = Fire/Light-as-Cold; Hierophant `Sanctuary of Thought` = no 50% less Mana Cost; `Cloak of Defiance` = standard 40% MoM; Trickster `Polymath` = **2% more** per Mastery; Ranger `Warden` (`守望者`) replaced Raider.
- **Triggers & Ticks**: PoE1 server tick = **`0.033s` (`33ms`, NOT `33.33ms`)** (`pob data tick`); CoC overspeed (`APS > cap`) **halves** effective triggers; **`Cast while Channelling` (`CwC`) is a fixed interval (`320ms` Lv20), NOT a cooldown — CDR has 0% effect**.
- **Build Bricks & Archetype Traps**: `Esh's Visage` (`艾许之面`) **breaks on Low Life (`<=50%` unres Life)** so Chaos bypasses ES (use `Shavronne's Wrappings`/`Solaris Lorica`/`The Ivory Tower`/`Coruscating Elixir`); `+% to Quality of all Skill Gems` applies **only to Active Skill Gems**; **Tinctures != Flasks**; `EB + CI` = 1 Life / 0 ES on Life; `The Agnostic + Valyrium + Bloodnotch/CWS` requires **`ES = 0`** (>0 ES adds 50% Stun Avoidance & raises Valyrium stun threshold — keep `The Agnostic` and `Defiance of Destiny` on CWS); Chieftain `Ramako, Sun's Light` locks stationary enemy Fire Res vs DoT to **fixed `-20%`** (`Flammability`/Exposure/Scorch add 0 while stationary — use `Punishment`); `Inspiration` gives 0 charges at 0 Mana and resets at **800 Mana spent** Lv20 (`922` Lv21); `Sadism` reduces Ailment Duration (pads PoB DPS while cutting ignite uptime); `Doryani's Prototype` `-200%` Lightning Res is intentional (Armour mitigates hits, not Lightning DoT); `Uul-Netol's Vow` adds a 7th link; `Returning Projectiles` requires `Pierce`.
- **Vestigial (`残存`/`殘存`) uniques, Wickerman Viscera (`内脏`/`臟器`) & Runegrafts (`符文之结`/`符文之結`)**:
  - A `{vestigial}` implicit on a unique `Helmet / Body Armour / Gloves / Boots / Shield` is legal: it is another unique's mod, and PoB places it only on a unique of the donor's own item type that has no `Source:` line (plus `Stasis Prison`), e.g. `+200 to maximum Life` from `Kaom's Heart` on `Cloak of Flame`; never on a quiver, a Replica or the donor itself. `pob data mod "<stat>"` shows `unique <type>, from <donor>`, and `pob data unique "<name>"` says whether the item takes one; check before calling one illegal.
  - PoB 2.69 has the **Wickerman Viscera** as items (the Wickerman slots in `pob items`) and the **Runegrafts** as passives (`pob data runegraft <query>`), and counts what it can parse of them itself: e.g. the `bonuses gained from Equipped Gloves/Boots` of `Greater Scholar's Leg` or `Runegraft of the Bound`. A `customMods` line that repeats such a stat counts twice (`pob diagnose` points such lines out): keep only what the build does not already carry. Keep **`Convocation` (`号召`) manual** when it alone feeds `"used a Minion Skill Recently"` (e.g. `Greater Overseer's Whip`; a triggered skill is not "used").
- **Affix Slot-Existence Ground Truth (`pob trade search`, NEVER `pob data mod` Alone) & Rare Gear Check**:
  - Whenever verifying whether an affix/mod can legally exist on a specific gear slot (especially Delve `Subterranean`, Incursion `Citaqualotl's`/`Tacati's`/`Matatl's`, Essence, Veiled, Recombinator, or league drop-only explicits such as Gloves `+2 to Level of Socketed Minion Gems`), **NEVER issue a negative verdict from `pob data mod` or web search alone** (PoB `Mods.dat` stores `0` spawn weights and incomplete slot tags for drop-only mods) — **always run `pob trade search --slot <slot> --stat "<stat_template>=<min>" --fetch-limit 5 --pob off`** (e.g. `--stat "+# to Level of Socketed Minion Gems>=2"`) to verify against live official market Ground Truth; if offline (`POB_OFFLINE=1` / network unavailable / `401`/`403`), mark `unverified (offline)` / `[待验证]` and never conclude the affix cannot exist.
  - Run `pob items --slot <S>` or `pob item inspect --slot <S>` before claiming open affixes, tiers, or `{crafted}` bench slots on rare gear.

## 4. Deep Reference Guides
- Triggers, `0.033s` CoC/CDR ticks, CwC, Low-Life & build bricks: `references/triggers-and-mechanics.md`
- Multi-step what-if, `batch`, `sweep`, `matrix`, `borrow`: `references/whatif-and-batch.md`
- Official PoE1 trade search, Weighted Sum, Harvest swap, crafting bench: `references/trade-and-bench.md`
- Timeless / Historic jewel seed search (`pob timeless`): `references/timeless.md`
- Complete PoE1 3.30 anti-hallucination & patch-drift table: `references/poe1-330-anti-hallucination.md`
