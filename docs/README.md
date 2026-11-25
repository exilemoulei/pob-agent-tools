# Documentation

Everything about pob4agents beyond the top-level README lives here. When a doc and the code disagree, the code wins.

## Architecture

[architecture.md](architecture.md): how a command reaches the engine, module layers, engine paths, environment variables, Chinese locale matching, the `R1`..`R13` constraints, the checklist for adding a command, verification and CI. Read it before changing code.

## Guides

How to use a feature end to end.

| Doc | What it covers |
|---|---|
| [guides/commands.md](guides/commands.md) | Every `pob` subcommand with its flags |
| [guides/trade.md](guides/trade.md) | Read-only official PoE1 trade search and the crafting bench (`pob trade`, `pob bench`), with PoB-verified DPS/EHP for every candidate |

## Design notes

Plans written before or during a piece of work, in Chinese. They record why things were built the way they were; they are snapshots and are not kept in sync with later changes.

| Doc | What it covers |
|---|---|
| [design/v0.1-architecture.zh-CN.md](design/v0.1-architecture.zh-CN.md) | Original architecture: product goals, the zero-formula rule (all numbers come from the real PoB engine), RPC contracts and the `R1`..`R12` red lines |
| [design/v0.2-swarm-plan.zh-CN.md](design/v0.2-swarm-plan.zh-CN.md) | v0.2: engine bootstrap from upstream PoB and PobTools-zh, update checks with network warnings, the local game-data DB, and the work split into parallel waves |
| [design/hook-guardrails.zh-CN.md](design/hook-guardrails.zh-CN.md) | `pob hook` lifecycle guardrails against "fake execution" by agents: reply scanning, turn receipts, the stop gate |
| [design/web-research-guardrails.zh-CN.md](design/web-research-guardrails.zh-CN.md) | `pob research` / `pob wiki` and the V5 gate that makes agents look up mechanics instead of guessing |

## Elsewhere in the repo

These stay outside `docs/` because tools expect them at fixed paths.

| File | Purpose |
|---|---|
| [../README.md](../README.md) / [../README_EN.md](../README_EN.md) | User docs: install, engine setup, every command with examples |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Where a new contributor starts: points to AGENTS.md, the architecture guide, the PR template and SECURITY.md |
| [../AGENTS.md](../AGENTS.md) | Contributor guide for agents and people: commands, layout, hard rules, conventions, commit scopes. `CLAUDE.md` imports it |
| [../skills/pob/SKILL.md](../skills/pob/SKILL.md) | Cross-harness agent skill, with topic references in [../skills/pob/references/](../skills/pob/references/) |
| [../.github/pull_request_template.md](../.github/pull_request_template.md) | What every pull request states: summary, testing (platforms and engine), AI disclosure |
| [../SECURITY.md](../SECURITY.md) | How to report a vulnerability |

## Adding a doc

- User guides go in `guides/`, plans and design write-ups in `design/`. Add a row to the table above.
- File names are lower-case kebab-case. Chinese docs end in `.zh-CN.md`.
