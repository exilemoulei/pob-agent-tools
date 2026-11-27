# Official PoE1 Trade Search & Crafting Bench (`pob trade` / `pob bench`)

**Hard Safety Rules**
- Read-only official PoE1 `/api/trade` only (never whispers, buys, or sends game input; NEVER call `/trade2/`).
- DPS/EHP columns come strictly from headless PoB (`-` = unverified; never claim `0` upgrade).
- `POESESSID` / `POE_COOKIE` are env-only if GGG returns 401/403 (never print or pass on CLI).

## 1. Resolve Trade Stat IDs & Verify Slot Affix Existence (Official Trade = Ground Truth)
```powershell
pob trade stats "法压" --slot boots
pob trade stats "受到【坚定】影响时" --json   # Strips 【】 brackets, resolves Watcher's Eye/pseudo offline, exposes `better` (`1`/`-1`/`0`, `betterSource: "apt"`)
pob trade stats "to Cold Resistance" --slot ring1

# MANDATORY Ground-Truth check before claiming any affix can/cannot exist on a slot
# (PoB `pob data mod` has 0 spawn weights & incomplete slot tags for Delve/Incursion/league drop-only explicits!)
pob trade search --slot gloves --stat "+# to Level of Socketed Minion Gems>=2" --fetch-limit 5 --pob off
```
> **Hard Affix-Existence Rule**: Never conclude an affix cannot exist on `<slot>` solely from `pob data mod` (empty `Weights` / `0` `--slot` hits) or web search — drop-only explicits (Delve `Subterranean`, Incursion `Citaqualotl's`/`Tacati's`/`Matatl's`, Essence, Recombinator, current-league drops) have `0` currency roll weight in PoB `Mods.dat`. Always verify against the live official trade market with `--fetch-limit 5 --pob off` (explicit `>=<min>` when checking specific tiers like `+2`, or `--pob auto --pob-limit 5` when ranking DPS upgrades). When offline (`POB_OFFLINE=1` / no network / `401`/`403`), explicitly report `unverified (offline)` / `[待验证]` and never issue a negative verdict.

## 2. Search & Verify Live Listings in PoB
```powershell
# Basic rare upgrade search with budget & PoB verification
pob trade search --slot boots --stat "生命=70" --stat "移速=30" --stat "法压=12" --budget 5 --currency divine --pob auto

# Weighted Sum (--sum): ALWAYS use `>=min` at the end for total sum threshold (`=min` sets per-entry min!)
pob trade search --slot ring1 --sum "火抗|冰抗|电抗>=100" --stat "生命>=60" --search-sort sum --sort pob --budget 3 --currency divine

# Harvest single-element resistance swap (--allow-conversion)
# Applies ONLY to identified rare/magic explicit single-element mods; always keeps tier but rerolls value!
pob trade search --slot ring2 --stat "冰霜抗性=42" --allow-conversion --convert-to cold --open-prefix --budget 2 --currency divine

# Bench-craft max-roll PoB simulation (--bench-craft; keeps originals + legal craft variants, PoB-ranked by default)
pob trade search --slot gloves --budget 20 --currency divine --stat "to Dexterity=55" --bench-craft "Area Damage" --pob on

# Fresh instant-buyout listings only (`--securable` = Faustus Merchant's Tab Gold buyout; `--max-age` hours; `--bargain` percent below 1-seller-1-vote going price; `--max-per-seller 2` dedupes spammer listings before PoB eval; `--normalize-quality auto` evaluates modifiable <20%Q gear at +20%Q `[+20%Q]`)
pob trade search --slot ring1 --stat "生命>=80" --stat "冰抗>=40" --securable --max-age 15 --bargain 30

# Preview Harvest swap on a pasted item
pob trade convert --file item.txt --to fire

# Exact item primitives, native armour panel filters, 3.30 Foulborn/Viscera, gems & enumerated option stats:
# --base "<BaseType>" (alias --type), --links <1..6>, --foulborn (defaults --rarity to unique when omitted),
# --es-min/--es-max, --ar-min/--ar-max, --ev-min/--ev-max, --ward-min/--ward-max -> official `armour_filters.filters.*`
#   (total/panel ES/AR/EV/Ward phrases on armour slots route here automatically — NEVER to `pseudo.pseudo_total_energy_shield`, which ignores base * quality * local %ES!).
# 3.30 Viscera/Graft slots (`viscera:head|body|arm|leg`, `graft:*`) & automatic `reduced`/`less` polarity inversion (`better: -1`) are built in.
# --gem-level <1..40>, --gem-quality <0..40>, enumerated option templates (Forbidden Flame/Flesh, Thread of Hope, Impossible Escape).
# Bare-aura ambiguity guard: `--stat "受坚定影响时"` or `--stat "while affected by vitality"` fails with candidate IDs/clauses;
# pass a specific effect clause instead (e.g. `--stat "受【坚定】影响时，获得额外物理伤害减免"`).
pob trade search --slot body_armour --es-min 800 --securable --budget 10 --currency divine --pob auto
pob trade search --name "Cloak of Flame" --links 6 --foulborn --securable --pob off
pob trade search --base "Awakened Multistrike Support" --gem-level 5 --gem-quality 20 --securable --pob off
pob trade search --name "Watcher's Eye" --stat "受【坚定】影响时，获得额外物理伤害减免" --securable --pob off
```

## 2b. 8p2s Large Cluster Jewel Solver (`trade cluster`) & Offline Budget Planner (`trade plan`)
```powershell
# 8-passive 2-socket Front-2 Back-1 Large Cluster Jewel topology solver (zero-HTTP URL by default; add --live for buyouts):
pob trade cluster --enchant "12% increased Lightning Damage" --notables "Sadist,Snowstorm" [--passives 8] [--sockets 2] [--ilvl-max 83] [--live --budget 15 --currency divine --status securable --max-results 8] [--realm global|tw] [--league L]

# Offline multi-slot budget knapsack over saved `pob trade search --json --out <slot>.json` files (0 HTTP calls):
# Picks at most one PoB-verified listing per slot maximizing weighted DPS/EHP within total chaos --budget.
pob trade plan ring1.json boots.json amulet.json --budget 1500 [--score dps=1,ehp=0.25] [--top 10] [--divine-rate 150] [--json]
```

## 3. Rare Item Affix Inspection & Hideout Crafting Bench (`ModMaster.lua` Authoritative)
```powershell
pob item inspect --slot "Ring 2" --want "Cold Resistance"
pob item inspect --file item.txt --slot gloves --want "maximum Life"
pob bench list --slot ring --search "生命"
pob bench suggest --slot gloves --stat "攻速" --affix suffix
```

## 4. Full-Build Buyout Links & 3-Tier Build Appraisal (`shopping-list` / `appraise`)
```powershell
# L0 zero-HTTP full-build buyout links (<0.5s, 0 HTTP requests; default --status securable):
# Covers all equipped gear, flasks, active weapon set, Foulborn uniques (strips replaced mod + sets foulborn_item=true),
# and special jewels (Watcher's Eye, Forbidden Flame/Flesh, Thread of Hope, Impossible Escape, Timeless Mazcotl -> ahuana).
# Surfaces `warnings` (unmappedTwFields, CJK residue, non-English raw item skip). Add `--live` to upgrade to L1 appraisal.
pob trade shopping-list [--target SLOT] [--league L] [--realm global|tw] [--status securable] [--include-gems|--no-gems] [--live [--max-live-queries 8]] [--json]

# L1 3-tier build valuation:
# - Tier A: poe.ninja uniques/gems/anoint oils (strict 5/20 Awakened gem variant safety)
# - Tier B: live P25 across >=5 distinct sellers scanning up to 30 rows
# - Tier C: crafted rares counted in `unpricedCount`
# Exposes `divineRateSource: ninja|env|static_fallback`. On `--realm tw`, warns poe.ninja is unavailable and promotes
# core Tier A items into live queries. `--max-live-queries 0` runs offline poe.ninja-only.
pob trade appraise [--target SLOT] [--league L] [--realm global|tw] [--status securable] [--max-live-queries 8] [--include-gems|--no-gems] [--json]

# Bilingual & slang poe.ninja economy lookup (supports --detailed for gem level/quality, corrupted, links, cluster passives; 3.30 Greater Viscera prefix strip & wombgift/viscera categoryFallback):
pob price "<EN|中文>" [--league L] [--detailed] [--variant V] [--category C]
```

## Exit Codes (`pob trade`)
`0` OK | `1` bad arg/stat | `2` no results | `3` cookie/CF required | `4` rate-limited | `5` API error | `6` `--pob on` failed.
