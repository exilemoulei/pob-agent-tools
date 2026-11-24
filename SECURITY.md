# Security Policy

## Supported versions

Only the latest release of `pob4agents` (the `pob` CLI) receives security fixes.
Upgrade with `scoop update pob`, the `install.ps1` one-liner, or by reinstalling the latest wheel.

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub private vulnerability reporting). Include:

- the affected version (`pob --version`) and platform
- steps or a minimal input that reproduces the issue
- the impact you expect (for example token leak, loopback daemon exposure, unsafe file write)

We aim to acknowledge reports within 7 days and to ship a fix or mitigation in the next release.

## Scope

In scope: the `pob` CLI and daemon in `pob_cli/`, the install scripts (`install.ps1`, `install.sh`),
the Scoop manifest in `bucket/`, the bundled hooks and skills, and the release workflows.

Out of scope: Path of Building itself, PobTools, poe.ninja, poedb, the official trade site, and other
third-party services the CLI talks to. Report those to their maintainers.
