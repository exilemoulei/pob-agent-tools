# Timeless / Historic Jewel Seed Search (`pob timeless`)

Searches PoE1 Historic timeless jewels (`Lethal Pride`, `Brutal Restraint`, `Glorious Vanity`, `Militant Faith`, `Elegant Hubris`, `Heroic Tragedy`, etc.) using parallel throwaway headless PoB workers (other Historic jewels removed cleanly during simulation).

> Never pass PoE2 timeless/radius jewels (`Undying Hate`/`不朽之恨`, `Against the Darkness`/`抗衡黑暗`, `Controlled Metamorphosis`, `Prism of Belief`, `From Nothing`) — those are hard-blocked (`pob2 timeless`).
> `Heroic Tragedy` is a PoE1 jewel and works by its PoE1 Chinese names `英勇悲剧` (zh-CN) / `英勇的悲劇` (zh-TW); only the PoE2 localization `英雄的悲歌` is blocked.

## 1. List Supported PoE1 Timeless Jewels & Allocated Sockets
```powershell
pob timeless list [--target main] [--json]
```

## 2. Search Optimal Seeds Verified by PoB
```powershell
# Find best Lethal Pride seeds for DPS in socket nodeId 26725
pob timeless search --jewel "Lethal Pride" --socket 26725 --conqueror Kaom --stat CombinedDPS --top 15

# Chinese jewel name + filter specific notable mod lines
pob timeless search --jewel "致命的骄傲" --socket 26725 --mod "5% chance to deal Double Damage" --stat FullDPS --top 10

# Scan multiple sockets (or `--socket all` for all allocated jewel sockets) in one shared worker pool
pob timeless search --jewel "Festering Vengeance" --socket all --stat CombinedDPS --top 5

# Exact full evaluation mode (checks every PRNG group in PoB; only pass when explicitly requested)
pob timeless search --jewel "Brutal Restraint" --socket 26725 --conqueror Balbala --stat TotalEHP --exact
```

Default search uses non-exhaustive fast modes — additive fitting (`narrow_down`, which may stop once remaining PRNG groups are bounded) for Legion jewels and a conditional `max(top * 4, 200)` weight shortlist for `Glorious Vanity` and 3.30 Abyss jewels (`Festering Vengeance` / `溃烂复仇`, etc.) — reporting both currently allocated notables (`0` extra points) and unallocated notables in radius; either fast mode can miss a higher seed that `--exact` (full evaluation of every resolved group) finds. Note: `Putrid Seed` (`腐朽之种`) is the 3.30 outer cluster passive fork, whereas the 3.30 Abyss timeless jewel is `Festering Vengeance`.

## 3. Conquered Radius Boundaries & `Elegant Hubris` `0`-Stat Passives
- **`Elegant Hubris` (`优雅的狂妄`) `Price of Glory`**: Conquers every small passive in radius into **`Price of Glory`** (zh-TW `榮耀代價`), which grants **nothing**; the conquered notables and keystone carry the jewel's stats and a mastery keeps its own, so a small passive in radius is worth its point only as the path to one of those or to a jewel socket.
- **`TreeWaste` Dead-End Audit (`pob diagnose`)**: Flags an allocated conquered `0`-stat `Price of Glory` small passive when taking it out would disconnect nothing but other such passives: points that lead nowhere. It is not a general dead-end check; a `Price of Glory` node on the way to anything with stats is left alone.
- **Inspect Active Socket Radius (`pob tree --socket-radius <id>`)**:
  ```powershell
  pob tree --socket-radius 26725
  ```
  Lists the nodes around jewel socket `<id>` under a `Radius <name>` heading per ring; a ring beyond the equipped jewel's own radius is marked `outside equipped ... radius (not affected by this jewel)`, so conquered and unconquered passives are not mixed up.
