# Daemon errors printed as Python tracebacks

- **Date**: 2026-11-21
- **Base**: `feat/macos-support` @ `56d79cb`
- **Branch / commit**: `fix/daemon-error-traceback` @ `fb61199`
- **Status**: PR [#4](https://github.com/exilemoulei/pob-agent-tools/pull/4) open against `feat/macos-support`

## Problem

Loading a build with no synced loadouts and switching loadout crashed with a raw traceback:

```text
$ pob switch-main --target ref --loadout "Late Upgrades"
Traceback (most recent call last):
  ...
  File ".../pob_cli/daemon.py", line 937, in call_daemon
    raise RuntimeError(data.get("error") or "Daemon error")
RuntimeError: Build has no synced loadouts available.
```

Found while comparing a user build against a guide PoB (`https://pobb.in/_uGIUuCogLLN`) that has
tree/item/skill sets but no synced loadouts.

## Root cause

`cli._daemon_call` only translated `BootstrapMissingError`. Any other `RuntimeError` the daemon
returns for a rejected request (no loadouts, nothing loaded in a slot, unknown spec, …) escaped
uncaught, so **every** command printed a traceback on such errors, not just `--loadout`.

## Change

`pob_cli/src/pob_cli/cli.py` — `_daemon_call` now also catches `RuntimeError` and raises
`click.ClickException(message)`:

- Output becomes `Error: <daemon message>`, exit code `1` (arg/state error in the AGENTS.md
  exit-code contract).
- An empty message becomes `Daemon error during '<action>'`.
- `BootstrapMissingError` (a `RuntimeError` subclass) is still handled first, so the
  bootstrap/update guidance is unchanged.
- Callers that use `call_daemon` directly and rely on `RuntimeError` (`pob update`, `pob stop`) are
  not affected.

## Verification

- New `pob_cli/tests/test_cli_errors.py` (7 cases): `--loadout` without loadouts; `summary`,
  `skills`, `calc`, `switch-main --spec` with daemon errors; empty error message; bootstrap-missing
  path unchanged.
- The new tests fail before the fix (6 of 7) and pass after.
- Full suite on the committed tree, run in an isolated worktree: 171 passed, 22 skipped.
- Live check on macOS (PoB 2.68.1, LuaJIT runner):

```text
$ pob switch-main --target ref --loadout "Late Upgrades"
Error: Build has no synced loadouts available.
exit=1
```

## Files

- `pob_cli/src/pob_cli/cli.py` (+8 / −1, `_daemon_call` only)
- `pob_cli/tests/test_cli_errors.py` (new)

Uncommitted edits to `analyzer.py`, `comparator.py`, `daemon.py`, other parts of `cli.py`, and
`tests/test_dewater.py` in the same working tree belong to a different session and are not part
of this change.
