# Official PoE1 Trade Search & Crafting Bench (`pob trade` / `pob bench`)

**Hard Safety Rules**
- Read-only official PoE1 `/api/trade` only (never whispers, buys, or sends game input; NEVER call `/trade2/`).
- DPS/EHP columns come strictly from headless PoB (`-` = unverified; never claim `0` upgrade).
- `POESESSID` / `POE_COOKIE` are env-only if GGG returns 401/403 (never print or pass on CLI).

## 1. Resolve Trade Stat IDs (0ms Local `TradeSiteStats.lua`)
```powershell
pob trade stats "法压" --slot boots
pob trade stats "to Cold Resistance" --slot ring1
```

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

# Fresh instant-buyout listings only (--max-age hours; if none is that fresh, older ones are shown and the report's `Listed:` line says so), underpriced ones flagged BARGAIN and listed first (--bargain percent below the going price, price only; a search matching 10,000+ listings flags nothing, so narrow it)
pob trade search --slot ring1 --stat "生命>=80" --stat "冰抗>=40" --status securable --max-age 15 --bargain 30

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
