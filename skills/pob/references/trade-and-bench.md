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
```

## 3. Rare Item Affix Inspection & Hideout Crafting Bench (`ModMaster.lua` Authoritative)
```powershell
pob item inspect --slot "Ring 2" --want "Cold Resistance"
pob item inspect --file item.txt --slot gloves --want "maximum Life"
pob bench list --slot ring --search "生命"
pob bench suggest --slot gloves --stat "攻速" --affix suffix
```

## Exit Codes (`pob trade`)
`0` OK | `1` bad arg/stat | `2` no results | `3` cookie/CF required | `4` rate-limited | `5` API error | `6` `--pob on` failed.
