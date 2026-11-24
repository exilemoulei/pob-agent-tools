# Timeless / Historic Jewel Seed Search (`pob timeless`)

Searches PoE1 Historic timeless jewels (`Lethal Pride`, `Brutal Restraint`, `Glorious Vanity`, `Militant Faith`, `Elegant Hubris`, `Heroic Tragedy`, etc.) using parallel throwaway headless PoB workers (other Historic jewels removed cleanly during simulation).

> Never pass PoE2 timeless/radius jewels (`Undying Hate`/`不朽之恨`, `Against the Darkness`/`抗衡黑暗`, `Controlled Metamorphosis`, `Prism of Belief`, `From Nothing`, `英雄的悲歌`) — those are hard-blocked (`pob2 timeless`).

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

# Exact full evaluation mode (slower, checks every seed in PoB)
pob timeless search --jewel "Brutal Restraint" --socket 26725 --conqueror Balbala --stat TotalEHP --exact
```
