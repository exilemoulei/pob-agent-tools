# ai/ — AI-assisted change log

Records of changes made with AI coding sessions (Claude Code, Codex, ...), one file per change, so
whoever maintains this project next — a person or an AI — can see *why* each change was made, how it
was verified, and what it touched, without reverse-engineering the code.

- Not shipped with the package and has no effect at runtime; safe to delete.
- Records describe the change as merged. They hold no live status (PR open/merged, CI): that lives on
  GitHub and would go stale here.

## Conventions

- File name: `YYYY-MM-DD-<short-slug>.md`
- Sections: Problem · Root cause · Change · Verification · Files
- Add one line to the index below for every record.

## Index

| Date | Record | Area |
|------|--------|------|
| 2026-11-21 | [Daemon errors printed as Python tracebacks](2026-11-21-daemon-error-traceback.md) | `cli._daemon_call`, `daemon.call_daemon` |
