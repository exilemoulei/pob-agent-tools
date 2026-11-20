# Daemon errors printed as Python tracebacks

- **Date**: 2026-11-21
- **Base**: `feat/macos-support` @ `56d79cb`

## Problem

When the daemon could not serve a request, every `pob` command printed a raw Python traceback:

```text
$ pob switch-main --target ref --loadout "Late Upgrades"
Traceback (most recent call last):
  ...
  File ".../pob_cli/daemon.py", line 937, in call_daemon
    raise RuntimeError(data.get("error") or "Daemon error")
RuntimeError: Build has no synced loadouts available.
```

Found while comparing a build against a guide PoB (`https://pobb.in/_uGIUuCogLLN`) that has
tree/item/skill sets but no synced loadouts.

## Root cause

`cli._daemon_call` only translated `BootstrapMissingError`. `call_daemon` raised plain
`RuntimeError` for two different situations, and both escaped uncaught:

1. **Request rejected**: the daemon is healthy and answered `ok: false` (no synced loadouts,
   nothing loaded in a slot, unknown spec, …). That is the caller's argument/state problem.
2. **Daemon failure**: not running, failed to boot, exited during startup, startup timeout,
   state file vanished, or a transport error talking to it. That is not the caller's fault.

## Change

- `pob_cli/src/pob_cli/daemon.py`: new `DaemonRequestError(RuntimeError)`, raised only for
  `ok: false` responses. It subclasses `RuntimeError`, so `pob update` / `pob stop`, which call
  `call_daemon` directly and catch `RuntimeError`, behave as before. Failure paths are unchanged.
- `pob_cli/src/pob_cli/cli.py`, `_daemon_call`:
  - `BootstrapMissingError` → bootstrap/update guidance, exit 1 (unchanged).
  - `DaemonRequestError` → `Error: <daemon message>`, exit 1 (arg/state error in the AGENTS.md
    exit-code contract). An empty message becomes `Daemon rejected '<action>'`.
  - Any other `RuntimeError`, `httpx.HTTPError` or `ValueError` (bad JSON) →
    `Error: PoB daemon failure during '<action>': <reason>` plus a hint to run `pob stop` and check
    the daemon log, exit 5 (`DAEMON_FAILURE_EXIT_CODE`, same code the contract uses for upstream/API
    failures).
- Messages may span more than one line (the daemon failure hint, `load`'s update warnings); the
  point is "no traceback", not "one line".

## Verification

- `pob_cli/tests/test_cli_errors.py` (13 cases): rejections for `switch-main --loadout`,
  `summary`, `skills`, `calc`, `switch-main --spec`, empty message (exit 1); daemon failures:
  startup timeout, exit during startup, vanished state file, `httpx.ConnectError`,
  `httpx.ReadTimeout` (exit 5, distinct message); bootstrap-missing path unchanged; and
  `call_daemon` itself raising `DaemonRequestError` on an `ok: false` response (mocked HTTP).
- Full suite: 177 passed, 22 skipped (the skipped ones need a real engine).
- Live check on macOS (Apple Silicon, PoB 2.68.1, LuaJIT runner from `feat/macos-support`):

```text
$ pob switch-main --target ref --loadout "Late Upgrades"
Error: Build has no synced loadouts available.
exit=1
```

The daemon-failure path was only exercised by the mocked tests, not against a real crashing daemon.

## Files

- `pob_cli/src/pob_cli/daemon.py`
- `pob_cli/src/pob_cli/cli.py`
- `pob_cli/tests/test_cli_errors.py`
