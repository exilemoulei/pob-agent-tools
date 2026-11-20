# ai/ — AI-assisted change log

Records of changes made to this fork with AI coding sessions (Claude Code), one file per change,
so a human reviewer can see *why* each change was made, how it was verified, and what it touched.

- Not shipped with the package; drop this folder from upstream PRs if the maintainer prefers.
- Several AI sessions may work in this checkout at once. Each record lists exactly which files that
  session changed, so unrelated edits in the working tree are not mistaken for part of it.

## Conventions

- File name: `YYYY-MM-DD-<short-slug>.md`
- Sections: Problem · Root cause · Change · Verification · Files · Status
- Add one line to the index below for every record.

## Index

| Date | Record | Branch / commit | Status |
|------|--------|-----------------|--------|
| 2026-11-21 | [Daemon errors printed as Python tracebacks](2026-11-21-daemon-error-traceback.md) | `fix/daemon-error-traceback` @ `fb61199` | PR [#4](https://github.com/exilemoulei/pob-agent-tools/pull/4) open |
