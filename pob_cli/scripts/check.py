#!/usr/bin/env python3
"""Deterministic lint, architecture-DAG, and offline test gate for `pob_cli`.

Modes:
  python pob_cli/scripts/check.py             # ruff + arch-lint + isolated offline pytest (>=905 passed, <=25 skipped)
  python pob_cli/scripts/check.py --min-passed 1331 --max-skipped 8  # CI ratchet for a POSIX real-engine run
  python pob_cli/scripts/check.py --fast      # ruff + AST arch-lint only (<3s)
  python pob_cli/scripts/check.py --self-test # mutation checks in %TEMP% proving every rule fires
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

MIN_PASSED_TESTS = 905
# Skip budgets by what this machine can run. A gate that is red for everyone without an engine is
# no gate: the defaults follow the tier this machine is actually in, and `engine.yml` still passes
# explicit counts so CI keeps an exact ratchet.
MAX_SKIPPED_TESTS = 25  # engine plus the legacy PobTools seed: the strict tier
_MAX_SKIPPED_BOOTSTRAPPED = 34  # engine from `pob bootstrap`: Windows keeps POSIX-only + slim LUT + Py<3.12 skips
_MAX_SKIPPED_NO_ENGINE = 120  # no engine at all: every real-engine test skips


def _engine_tier(repo_root: Path) -> tuple[str, int]:
    """(tier name, max skipped tests) for this checkout: what the offline gate can really run."""
    candidates = []
    env_root = os.environ.get("POB_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(repo_root / ".runtime" / "engine")
    candidates.append(repo_root / "PobTools[20261102]")
    for cand in candidates:
        if (cand / "PoE1" / "Data" / "Gems.lua").is_file():
            legacy = (repo_root / "PobTools[20261102]" / "PoE1" / "manifest.xml").is_file()
            if legacy:
                return "engine + legacy seed", MAX_SKIPPED_TESTS
            return "bootstrapped engine", _MAX_SKIPPED_BOOTSTRAPPED
    return "no engine", _MAX_SKIPPED_NO_ENGINE


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


def run_ruff(pob_cli_root: Path) -> tuple[bool, str]:
    targets = [
        str(pob_cli_root / sub)
        for sub in ("src", "scripts", "packaging", "tests")
        if (pob_cli_root / sub).is_dir()
    ]
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--config",
        str(pob_cli_root / "pyproject.toml"),
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
    pob_cli_root: Path,
    *,
    min_passed: int = MIN_PASSED_TESTS,
    max_skipped: int = MAX_SKIPPED_TESTS,
) -> tuple[bool, str]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pob_check_pytest_"))
    try:
        rt_dir = tmp_dir / "runtime"
        cache_dir = tmp_dir / "cache"
        rt_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["POB_RUNTIME_DIR"] = str(rt_dir)
        env["POB_CACHE_DIR"] = str(cache_dir)
        env["POB_OFFLINE"] = "1"
        env["PYTHONUTF8"] = "1"
        src_str = str(pob_cli_root / "src")
        prev_pp = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_str}{os.pathsep}{prev_pp}" if prev_pp else src_str

        cmd = [sys.executable, "-m", "pytest", str(pob_cli_root / "tests"), "-q"]
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


def run_self_test(pob_cli_root: Path) -> int:
    """Copy source to %TEMP%, inject mutations for each rule, assert each fails with [rule] & Fix:."""
    tmp_root = Path(tempfile.mkdtemp(prefix="pob_check_selftest_"))
    try:
        staged = tmp_root / "pob_cli"
        for sub in ("src", "packaging", "scripts", "tests"):
            if (pob_cli_root / sub).is_dir():
                _copy_tree_filtered(pob_cli_root / sub, staged / sub)
        (staged / "pyproject.toml").write_bytes((pob_cli_root / "pyproject.toml").read_bytes())

        pkg = staged / "src" / "pob_cli"
        mutations: list[tuple[str, str, Path, str]] = [
            (
                "file-budget",
                "file-budget (>650L)",
                pkg / "errors.py",
                "\n" + "\n".join(f"# pad {i}" for i in range(660)) + "\n",
            ),
            (
                "fn-budget",
                "fn-budget (>200L)",
                pkg / "ticks.py",
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
                "layer-dag (lazy function-body L2->L3)",
                pkg / "trade_eval" / "evaluator.py",
                "\n\ndef _selftest_lazy_upward() -> object:\n"
                "    from ..daemon import call_daemon\n\n"
                "    return call_daemon\n",
            ),
            (
                "blocked-rpc",
                "blocked-rpc (save_build_as)",
                pkg / "snapshot" / "__init__.py",
                '\n\ndef _selftest_blocked_rpc(worker: object) -> None:\n'
                '    worker.call("save_build_as", {})  # type: ignore[attr-defined]\n',
            ),
            (
                "spec-sync",
                "spec-sync (lazy subcommand without a module)",
                pkg / "cli.py",
                '\n_LAZY_SUBCOMMANDS = {"selftest": (".cli_selftest_missing", "cmd_selftest", "Missing.")}\n',
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
                    print(
                        f"SELF-TEST FAILED: mutation `{label}` did not emit expected tag/Fix:\n{combined}"
                    )
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
            print(f"SELF-TEST FAILED: clean staged copy failed --fast:\n{clean_proc.stdout}\n{clean_proc.stderr}")
            return 1
        print(f"SELF-TEST PASSED: all {len(mutations)} mutation checks + clean baseline verified (0 residue).")
        return 0
    finally:
        _remove_tree_bottom_up(tmp_root)
        if tmp_root.exists():
            print(f"SELF-TEST FAILED: leftover temp directory {tmp_root}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="pob_cli lint, architecture DAG, and offline test gate.")
    parser.add_argument("--fast", action="store_true", help="Run ruff + AST arch-lint only (<3s).")
    parser.add_argument("--self-test", action="store_true", help="Run mutation self-test in %%TEMP%%.")
    parser.add_argument("--root", type=Path, default=None, help="Override pob_cli root directory.")
    # Skips depend on the machine (legacy snapshot, Windows-only, pob-zh engine), so CI pins its own counts.
    parser.add_argument(
        "--min-passed",
        type=int,
        default=None,
        help=f"Minimum passed tests (default: {MIN_PASSED_TESTS}).",
    )
    parser.add_argument(
        "--max-skipped",
        type=int,
        default=None,
        help="Maximum skipped tests (default: the budget of the tier this machine is in, but only "
        f"when neither flag is passed: {MAX_SKIPPED_TESTS} with the legacy seed, "
        f"{_MAX_SKIPPED_BOOTSTRAPPED} with a bootstrapped engine, {_MAX_SKIPPED_NO_ENGINE} with no "
        "engine; passing either flag pins both).",
    )
    args = parser.parse_args(argv)

    pob_cli_root = (args.root or Path(__file__).resolve().parents[1]).resolve()

    if args.self_test:
        return run_self_test(pob_cli_root)

    t0 = time.perf_counter()
    ruff_ok, ruff_out = run_ruff(pob_cli_root)
    violations, stats = run_arch_lint(pob_cli_root)

    failed = False
    if not ruff_ok:
        failed = True
        print("FAIL [ruff] ruff check reported lint violations | Fix: run `python -m ruff check pob_cli/src pob_cli/scripts pob_cli/packaging pob_cli/tests`")
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

    # The engine tier is a convenience for a bare run only. Passing either ratchet flag means the
    # caller is pinning the numbers (engine.yml does), so the other one keeps its plain default
    # instead of being quietly widened to the tier budget.
    explicit_ratchet = args.min_passed is not None or args.max_skipped is not None
    min_passed = args.min_passed if args.min_passed is not None else MIN_PASSED_TESTS
    max_skipped = args.max_skipped if args.max_skipped is not None else MAX_SKIPPED_TESTS
    if not explicit_ratchet:
        tier, max_skipped = _engine_tier(pob_cli_root.parent)
        print(
            f"INFO: {tier}: offline gate allows up to {max_skipped} skipped tests"
            + ("" if tier != "no engine" else " (run `pob bootstrap` to exercise the real-engine tests)")
            + "."
        )
    pytest_ok, pytest_msg = run_isolated_pytest(pob_cli_root, min_passed=min_passed, max_skipped=max_skipped)
    total_elapsed = time.perf_counter() - t0
    if not pytest_ok:
        print(f"FAIL [pytest] isolated offline test gate failed in {total_elapsed:.2f}s | Fix: inspect failures below.")
        print(pytest_msg)
        return 1

    print(f"FULL GATE OK in {total_elapsed:.2f}s | {pytest_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
