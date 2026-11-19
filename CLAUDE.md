# CLAUDE.md

@AGENTS.md

This repository ships an AI playbook for gear upgrades. When the user asks what to
upgrade, how to spend a budget, whether an item is worth it, or reports a resistance
gap, follow `.claude/skills/pob-trade-upgrade/SKILL.md` together with the default
behavior in `AGENTS.md`.

Key entry point:

```bash
pob trade search --slot gloves --budget 20 --stat "to Cold Resistance=40" --pob auto
```

If no listing fits, fall back to an open affix plus a bench craft:

```bash
pob trade search --slot gloves --budget 10 --stat "to maximum Life=60" --open-suffix --pob auto
pob bench suggest --slot gloves --stat "to Cold Resistance"
```

Bench modifiers are lower tier than natural rolls; quote the minimum, and verify the
crafted item by appending the `{crafted}` line and running `pob item try`.

Details: [docs/TRADE.md](docs/TRADE.md).
