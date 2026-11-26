#!/usr/bin/env python3
"""Deterministic lint, architecture-DAG, isolation, and offline test gate for ``pob2_cli``.

Modes:
  python pob2_cli/scripts/check.py             # ruff + arch-lint + isolated offline pytest
  python pob2_cli/scripts/check.py --fast      # ruff + AST arch/isolation/leak lint (<3s)
  python pob2_cli/scripts/check.py --self-test # mutation proof in %TEMP% that every rule fires
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from arch_lint import SKIP_COPY_DIRS, run_arch_lint  # noqa: E402

MIN_PASSED_TESTS = 1
MAX_SKIPPED_TESTS = 50


def _remove_tree_bottom_up(path: Path) -> None:
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        root_p = Path(root)
        for fn in files:
            os.unlink(root_p / fn)
        for dn in dirs:
            os.rmdir(root_p / dn)
    os.rmdir(path)


def _copy_tree_filtered(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src):
        if entry.is_dir(follow_symlinks=False):
            if entry.name in SKIP_COPY_DIRS or entry.name.endswith(".egg-info"):
                continue
            _copy_tree_filtered(Path(entry.path), dst / entry.name)
        elif entry.is_file(follow_symlinks=False):
            if entry.name.endswith((".pyc", ".pyo")):
                continue
            (dst / entry.name).write_bytes(Path(entry.path).read_bytes())


def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_ruff(pob2_root: Path) -> tuple[bool, str]:
    targets = [
        str(pob2_root / sub)
        for sub in ("src", "scripts", "packaging", "tests")
        if (pob2_root / sub).is_dir()
    ]
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--config",
        str(pob2_root / "pyproject.toml"),
        *targets,
    ]
    proc = subprocess.run(
        cmd,
        env=_utf8_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, combined


def run_isolated_pytest(
    pob2_root: Path,
    *,
    min_passed: int = MIN_PASSED_TESTS,
    max_skipped: int = MAX_SKIPPED_TESTS,
) -> tuple[bool, str]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pob2_check_pytest_"))
    try:
        rt_dir = tmp_dir / "runtime"
        cache_dir = tmp_dir / "cache"
        rt_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        env = _utf8_env()
        env["POB2_RUNTIME_DIR"] = str(rt_dir)
        env["POB2_CACHE_DIR"] = str(cache_dir)
        env["POB2_OFFLINE"] = "1"
        src_str = str(pob2_root / "src")
        prev_pp = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_str}{os.pathsep}{prev_pp}" if prev_pp else src_str

        cmd = [sys.executable, "-m", "pytest", str(pob2_root / "tests"), "-q"]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (proc.stdout + "\n" + proc.stderr).strip()
        passed_m = re.search(r"(\d+)\s+passed", out)
        failed_m = re.search(r"(\d+)\s+failed", out)
        skipped_m = re.search(r"(\d+)\s+skipped", out)
        errors_m = re.search(r"(\d+)\s+error", out)
        passed = int(passed_m.group(1)) if passed_m else 0
        failed = int(failed_m.group(1)) if failed_m else 0
        skipped = int(skipped_m.group(1)) if skipped_m else 0
        errors = int(errors_m.group(1)) if errors_m else 0

        ok = (
            proc.returncode == 0
            and failed == 0
            and errors == 0
            and passed >= min_passed
            and skipped <= max_skipped
        )
        summary = (
            f"pytest: passed={passed}, failed={failed}, skipped={skipped}, errors={errors} "
            f"(ratchet>={min_passed}, max_skipped<={max_skipped})"
        )
        if not ok:
            return False, f"{summary}\n{out}"
        return True, summary
    finally:
        _remove_tree_bottom_up(tmp_dir)


def run_self_test(pob2_root: Path) -> int:
    """Copy source to %TEMP%, inject mutations for every lint rule, assert each fails with [rule] & Fix:."""
    tmp_root = Path(tempfile.mkdtemp(prefix="pob2_check_selftest_"))
    try:
        staged = tmp_root / "pob2_cli"
        for sub in ("src", "packaging", "scripts", "tests"):
            if (pob2_root / sub).is_dir():
                _copy_tree_filtered(pob2_root / sub, staged / sub)
        for root_file in ("pyproject.toml", "AGENTS.md", "ATTRIBUTION.md"):
            src_f = pob2_root / root_file
            if src_f.is_file():
                (staged / root_file).write_bytes(src_f.read_bytes())

        pkg = staged / "src" / "pob2_cli"
        cross_pkg_name = "pob" + "_cli"
        detached_flag = "DETACHED" + "_PROCESS"
        leak_path = "C:" + "/Users/" + "mou" + "le" + "/secret"
        mutations: list[tuple[str, str, Path, str]] = [
            (
                "file-budget",
                "file-budget (>650L)",
                pkg / "compat.py",
                "\n" + "\n".join(f"# pad {i}" for i in range(660)) + "\n",
            ),
            (
                "fn-budget",
                "fn-budget (>200L)",
                pkg / "compat.py",
                "\n\ndef _selftest_huge_fn() -> int:\n"
                + "".join(f"    _x = {i}\n" for i in range(210))
                + "    return _x\n",
            ),
            (
                "layer-dag",
                "layer-dag (top-level L0->L4)",
                pkg / "errors.py",
                "\nfrom . import cli as _illegal_cli  # noqa: F401\n",
            ),
            (
                "layer-dag",
                "layer-dag (lazy function-body L0->L4)",
                pkg / "compat.py",
                "\n\ndef _selftest_lazy_upward() -> object:\n"
                "    from .cli import main\n\n"
                "    return main\n",
            ),
            (
                "cross-pkg-import",
                "cross-pkg-import (import PoE1 pkg)",
                pkg / "compat.py",
                f"\nimport {cross_pkg_name}  # noqa: F401\n",
            ),
            (
                "win-subprocess",
                "win-subprocess (forbidden creation flag)",
                pkg / "compat.py",
                f"\nimport subprocess\n_BAD_FLAG = getattr(subprocess, '{detached_flag}', 0)\n",
            ),
            (
                "blocked-rpc",
                "blocked-rpc (tj_open)",
                pkg / "compat.py",
                '\n\ndef _selftest_rpc(worker: object) -> None:\n'
                '    worker.call("tj_open", {})  # type: ignore[attr-defined]\n',
            ),
            (
                "local-path-leak",
                "local-path-leak (hardcoded local path)",
                pkg / "compat.py",
                f'\n_LEAK = "{leak_path}"\n',
            ),
        ]

        check_script = staged / "scripts" / "check.py"
        for expected_rule, label, target_file, snippet in mutations:
            original = target_file.read_bytes()
            try:
                target_file.write_bytes(original + snippet.encode("utf-8"))
                proc = subprocess.run(
                    [sys.executable, str(check_script), "--fast", "--root", str(staged)],
                    env=_utf8_env(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                combined = proc.stdout + "\n" + proc.stderr
                if proc.returncode == 0:
                    print(f"SELF-TEST FAILED: mutation `{label}` unexpectedly passed!")
                    return 1
                if f"[{expected_rule}]" not in combined or "Fix:" not in combined:
                    print(f"SELF-TEST FAILED: mutation `{label}` missing [{expected_rule}]/Fix:\n{combined}")
                    return 1
                print(f"SELF-TEST OK [{label}]: caught mutation as expected.")
            finally:
                target_file.write_bytes(original)

        clean_proc = subprocess.run(
            [sys.executable, str(check_script), "--fast", "--root", str(staged)],
            env=_utf8_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if clean_proc.returncode != 0:
            print(f"SELF-TEST FAILED: clean copy failed --fast:\n{clean_proc.stdout}\n{clean_proc.stderr}")
            return 1
        print(f"SELF-TEST PASSED: all {len(mutations)} mutation checks + clean baseline verified (0 residue).")
        return 0
    finally:
        _remove_tree_bottom_up(tmp_root)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="pob2_cli lint, architecture DAG, and offline test gate.")
    parser.add_argument("--fast", action="store_true", help="Run ruff + AST arch-lint only (<3s).")
    parser.add_argument("--self-test", action="store_true", help="Run mutation self-test in %%TEMP%%.")
    parser.add_argument("--root", type=Path, default=None, help="Override pob2_cli root directory.")
    parser.add_argument("--min-passed", type=int, default=MIN_PASSED_TESTS, help="Minimum passed tests.")
    parser.add_argument("--max-skipped", type=int, default=MAX_SKIPPED_TESTS, help="Maximum skipped tests.")
    args = parser.parse_args(argv)

    pob2_root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    if args.self_test:
        return run_self_test(pob2_root)

    t0 = time.perf_counter()
    ruff_ok, ruff_out = run_ruff(pob2_root)
    violations, stats = run_arch_lint(pob2_root)

    failed = False
    if not ruff_ok:
        failed = True
        print("FAIL [ruff] ruff check reported lint violations | Fix: run `python -m ruff check --config pob2_cli/pyproject.toml pob2_cli/src pob2_cli/scripts pob2_cli/tests`")
        if ruff_out:
            print(ruff_out)
    if violations:
        failed = True
        for v in violations:
            print(v)

    elapsed_fast = time.perf_counter() - t0
    if failed:
        print(f"check.py FAILED in {elapsed_fast:.2f}s ({len(violations)} arch violations, ruff_ok={ruff_ok}).")
        return 1

    ml, ml_f = stats["maxLines"]  # type: ignore[misc]
    mb, mb_f = stats["maxBytes"]  # type: ignore[misc]
    mf, mf_f = stats["maxFn"]  # type: ignore[misc]
    print(
        f"FAST GATE OK in {elapsed_fast:.2f}s | pkg_files={stats['files']} checked={stats['checkedFiles']} | "
        f"max={ml}L ({ml_f}), {mb}B ({mb_f}), fn={mf}L ({mf_f}) | ruff=0 | arch=0"
    )
    if args.fast:
        return 0

    pytest_ok, pytest_msg = run_isolated_pytest(
        pob2_root,
        min_passed=args.min_passed,
        max_skipped=args.max_skipped,
    )
    total_elapsed = time.perf_counter() - t0
    if not pytest_ok:
        print(f"FAIL [pytest] isolated offline test gate failed in {total_elapsed:.2f}s | Fix: inspect failures below.")
        print(pytest_msg)
        return 1

    print(f"FULL GATE OK in {total_elapsed:.2f}s | {pytest_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
