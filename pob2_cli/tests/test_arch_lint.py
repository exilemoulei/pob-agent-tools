"""Unit tests for pob2_cli architecture, budget, isolation, and local-path leak lint gates."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

_POB2_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _POB2_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import arch_lint  # noqa: E402


def _make_minimal_pkg(tmp_path: Path) -> Path:
    root = tmp_path / "pob2_cli"
    pkg = root / "src" / "pob2_cli"
    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pob24agents"\nlicense = { text = "GPL-3.0-only" }\n'
        'classifiers = ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]\n'
        'dependencies = ["click>=8.1.0", "httpx[socks]>=0.25.0", "tabulate>=0.9.0"]\n',
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    return root


def test_real_pob2_cli_passes_arch_lint() -> None:
    violations, stats = arch_lint.run_arch_lint(_POB2_ROOT)
    assert not violations, "\n".join(violations)
    assert int(stats["files"]) >= 7


def test_agents_md_under_60_lines_and_uv_lock_valid() -> None:
    agents_lines = (_POB2_ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert len(agents_lines) < 60, f"pob2_cli/AGENTS.md has {len(agents_lines)} lines (must be <60)"
    lock = tomllib.loads((_POB2_ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = [pkg.get("name") for pkg in lock.get("package", [])]
    assert "pob24agents" in names
    assert "pob4agents" not in names


def test_arch_lint_blocks_cross_pkg_detached_leak_and_ticks(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    pkg = root / "src" / "pob2_cli"

    cross_pkg = "pob" + "_cli"
    detached = "DETACHED" + "_PROCESS"
    leak = "C:" + "/Users/" + "mou" + "le" + "/work"
    (pkg / "compat.py").write_text(
        f"import {cross_pkg}\n"
        f"import subprocess\n"
        f"FLAG = getattr(subprocess, '{detached}', 0)\n"
        f'PATH = "{leak}"\n',
        encoding="utf-8",
    )
    (pkg / "ticks.py").write_text("TICK = 0.033\n", encoding="utf-8")

    violations, _ = arch_lint.run_arch_lint(root)
    joined = "\n".join(violations)
    assert "[cross-pkg-import]" in joined
    assert "[win-subprocess]" in joined
    assert "[local-path-leak]" in joined
    assert "[no-poe1-ticks]" in joined


def test_arch_lint_allows_public_repo_handle_without_false_positive(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    pkg = root / "src" / "pob2_cli"
    (pkg / "compat.py").write_text(
        'REPO = "https://github.com/exilemoulei/pob4agents"\n',
        encoding="utf-8",
    )
    violations, _ = arch_lint.run_arch_lint(root)
    assert not violations, "\n".join(violations)


def test_arch_lint_rejects_gpl_or_later_and_forbidden_deps(tmp_path: Path) -> None:
    root = _make_minimal_pkg(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pob24agents"\nlicense = { text = "GPL-3.0-or-later" }\n'
        'classifiers = ["License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)"]\n'
        'dependencies = ["click>=8.1.0", "pydantic>=2.0"]\n',
        encoding="utf-8",
    )
    violations, _ = arch_lint.run_arch_lint(root)
    joined = "\n".join(violations)
    assert joined.count("[pkg-policy]") >= 3
