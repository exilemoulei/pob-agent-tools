"""AST architecture, file/function budget, layer-DAG, blocked-RPC, and spec-sync linter for `pob_cli`."""

from __future__ import annotations

import ast
from pathlib import Path

MAX_FILE_LINES = 650
MAX_FILE_BYTES = 24000
MAX_FN_LINES = 200

# 5-layer unidirectional architecture (L0 -> L1 -> L2 -> L3 -> L4)
LAYER_MAP: dict[str, int] = {
    # L0: foundation & static rules
    "__init__": 0,
    "errors": 0,
    "ticks": 0,
    "os_util": 0,
    "compat": 0,
    "json_io": 0,
    "locale_cfg": 0,
    "guardrails": 0,
    # L1: engine runtime, bridge, importers, and data sources
    "updater": 1,
    "bridge_protocol": 1,
    "bridge_client": 1,
    "bridge_boot": 1,
    "bridge_trees": 1,
    "importers": 1,
    "importers_codec": 1,
    "importers_stages": 1,
    "profile_import": 1,
    "game_data": 1,
    "external_data": 1,
    "poedb_client": 1,
    # L2: domain core
    "formatters": 2,
    "snapshot": 2,
    "analyzer": 2,
    "mutators": 2,
    "comparator": 2,
    "bench": 2,
    "item_inspect": 2,
    "item_ablate": 2,
    "trade_craft": 2,
    "trade_item": 2,
    "trade_client": 2,
    "trade_query": 2,
    "trade_eval": 2,
    "timeless": 2,
    "map_regex": 2,
    # L3: daemon server & client
    "daemon": 3,
    # L4: CLI entry points
    "__main__": 4,
    "cli_common": 4,
    "cli": 4,
    "cli_help": 4,
    "cli_lifecycle": 4,
    "cli_lang": 4,
    "cli_build_io": 4,
    "cli_inspect": 4,
    "cli_inspect_parts": 4,
    "cli_item_inspect": 4,
    "cli_mutate_items": 4,
    "cli_mutate_skills": 4,
    "cli_mutate": 4,
    "cli_tree": 4,
    "cli_tree_wheels": 4,
    "cli_optimize": 4,
    "cli_self_update": 4,
    "cli_data_render": 4,
    "cli_data": 4,
    "cli_research": 4,
    "cli_trade": 4,
    "cli_timeless": 4,
    "cli_map_regex": 4,
}

BLOCKED_RPC_EXACT: frozenset[str] = frozenset(
    {
        "config_mod_search",
        "config_mod_add",
        "set_pob_options",
        "oauth_logout",
        "import_from_uri",
        "save_build_as",
    }
)
RPC_ALLOWLIST_FILES: frozenset[str] = frozenset({"bridge_client.py", "bridge_protocol.py"})
TIMELESS_CLI_FORBIDDEN_IMPORTS: tuple[str, ...] = ("httpx", "cli_trade", "bench")
SKIP_COPY_DIRS: frozenset[str] = frozenset(
    {"__pycache__", "build", "dist", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)


def _fmt_fail(rule: str, path: str, line: int, problem: str, fix: str) -> str:
    return f"FAIL [{rule}] {path}:{line} -- {problem} | Fix: {fix}"


def _top_unit_for_rel(rel: Path) -> str:
    return rel.stem if len(rel.parts) == 1 else rel.parts[0]


def _resolve_import_target(rel: Path, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    """Return list of `(top_unit, full_module)` for imports targeting `pob_cli` (or external names)."""
    out: list[tuple[str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == "pob_cli":
                out.append(("__init__", "pob_cli"))
            elif name.startswith("pob_cli."):
                out.append((name.split(".", 2)[1], name))
            else:
                out.append((f"ext:{name.split('.')[0]}", name))
        return out

    if node.level == 0:
        mod = node.module or ""
        if mod == "pob_cli":
            for alias in node.names:
                out.append((alias.name, f"pob_cli.{alias.name}"))
        elif mod.startswith("pob_cli."):
            out.append((mod.split(".", 2)[1], mod))
        elif mod:
            out.append((f"ext:{mod.split('.')[0]}", mod))
        return out

    pkg_parts = list(rel.parts[:-1])
    up = node.level - 1
    if up > len(pkg_parts):
        return out
    base = pkg_parts[: len(pkg_parts) - up]
    mod_parts = [p for p in (node.module or "").split(".") if p]
    resolved = base + mod_parts
    if not resolved:
        for alias in node.names:
            if alias.name != "*":
                out.append((alias.name, f"pob_cli.{alias.name}"))
    else:
        out.append((resolved[0], "pob_cli." + ".".join(resolved)))
    return out


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _iter_runtime_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Collect all runtime imports (top-level and inside functions/methods), excluding `if TYPE_CHECKING:` blocks."""
    imports: list[ast.Import | ast.ImportFrom] = []

    def _walk(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for stmt in node.orelse:
                _walk(stmt)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            return
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return imports


def _iter_top_level_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []

    def _collect(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                imports.append(stmt)
            elif isinstance(stmt, ast.If):
                if not _is_type_checking_guard(stmt):
                    _collect(stmt.body)
                    _collect(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                _collect(stmt.body)
                for h in stmt.handlers:
                    _collect(h.body)
                _collect(stmt.orelse)
                _collect(stmt.finalbody)

    _collect(tree.body)
    return imports


def _is_blocked_rpc_name(name: str) -> bool:
    return name in BLOCKED_RPC_EXACT or name.startswith("compare_")


def _find_blocked_rpc_calls(tree: ast.AST, rel_posix: str) -> list[str]:
    if rel_posix in RPC_ALLOWLIST_FILES:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else "")
        )
        if func_name == "call" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and _is_blocked_rpc_name(first.value):
                violations.append(
                    _fmt_fail(
                        "blocked-rpc",
                        rel_posix,
                        node.lineno,
                        f"Forbidden PoB RPC `{first.value}` called outside bridge allowlist",
                        "Use the R1-R11 compliant helper instead of invoking blocked RPCs directly.",
                    )
                )
        elif func_name in ("call_batch", "_call_batch"):
            for arg in node.args:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    for elt in arg.elts:
                        if isinstance(elt, (ast.List, ast.Tuple)) and elt.elts:
                            head = elt.elts[0]
                            if (
                                isinstance(head, ast.Constant)
                                and isinstance(head.value, str)
                                and _is_blocked_rpc_name(head.value)
                            ):
                                violations.append(
                                    _fmt_fail(
                                        "blocked-rpc",
                                        rel_posix,
                                        head.lineno,
                                        f"Forbidden PoB RPC `{head.value}` queued in batch call",
                                        "Remove blocked RPC per AGENTS.md R1-R4/R7/R11.",
                                    )
                                )
    return violations


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    visited: set[str] = set()
    in_stack: list[str] = []
    in_stack_set: set[str] = set()
    cycles: list[list[str]] = []

    def _dfs(node: str) -> None:
        visited.add(node)
        in_stack.append(node)
        in_stack_set.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in in_stack_set:
                idx = in_stack.index(nxt)
                cycles.append([*in_stack[idx:], nxt])
            elif nxt not in visited:
                _dfs(nxt)
        in_stack.pop()
        in_stack_set.remove(node)

    for node in sorted(graph):
        if node not in visited:
            _dfs(node)
    return cycles


def _check_spec_sync(pob_cli_root: Path, pkg_root: Path) -> list[str]:
    violations: list[str] = []
    spec_path = pob_cli_root / "packaging" / "pob.spec"
    cli_path = pkg_root / "cli.py"
    if not spec_path.is_file():
        return [
            _fmt_fail(
                "spec-sync",
                "packaging/pob.spec",
                1,
                "PyInstaller spec file is missing",
                "Restore pob_cli/packaging/pob.spec.",
            )
        ]

    spec_text = spec_path.read_text(encoding="utf-8")
    uses_collect = (
        'collect_submodules("pob_cli")' in spec_text
        or "collect_submodules('pob_cli')" in spec_text
    )

    lazy_targets: list[tuple[str, int]] = []
    if cli_path.is_file():
        cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename="cli.py")
        for stmt in cli_tree.body:
            target_id = None
            value_node = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target_id, value_node = stmt.target.id, stmt.value
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target_id, value_node = stmt.targets[0].id, stmt.value
            if target_id == "_LAZY_SUBCOMMANDS" and isinstance(value_node, ast.Dict):
                for val in value_node.values:
                    if (
                        isinstance(val, ast.Tuple)
                        and val.elts
                        and isinstance(val.elts[0], ast.Constant)
                        and isinstance(val.elts[0].value, str)
                    ):
                        lazy_targets.append((val.elts[0].value, val.lineno))

    for mod_spec, lineno in lazy_targets:
        mod_rel = mod_spec.lstrip(".")
        exists = (pkg_root / f"{mod_rel}.py").is_file() or (pkg_root / mod_rel / "__init__.py").is_file()
        if not exists:
            violations.append(
                _fmt_fail(
                    "spec-sync",
                    "src/pob_cli/cli.py",
                    lineno,
                    f"Lazy subcommand module `{mod_spec}` does not exist under src/pob_cli/",
                    "Create the module/package or update _LAZY_SUBCOMMANDS.",
                )
            )
        full_mod = f"pob_cli.{mod_rel}"
        if not uses_collect and f'"{full_mod}"' not in spec_text and f"'{full_mod}'" not in spec_text:
            violations.append(
                _fmt_fail(
                    "spec-sync",
                    "packaging/pob.spec",
                    1,
                    f"Lazy subcommand `{full_mod}` is not bundled in pob.spec hiddenimports",
                    'Use `collect_submodules("pob_cli")` in pob.spec hiddenimports.',
                )
            )

    subpackages: list[str] = []
    for entry in sorted(pkg_root.iterdir()):
        if not entry.is_dir() or entry.name in SKIP_COPY_DIRS or entry.name in ("data", "lua"):
            continue
        if not any(entry.rglob("*.py")):
            continue
        if not (entry / "__init__.py").is_file():
            violations.append(
                _fmt_fail(
                    "spec-sync",
                    f"src/pob_cli/{entry.name}",
                    1,
                    f"Subpackage `{entry.name}` is missing __init__.py (collect_submodules skips namespace dirs)",
                    f"Add src/pob_cli/{entry.name}/__init__.py.",
                )
            )
        else:
            subpackages.append(f"pob_cli.{entry.name}")

    if not uses_collect:
        for sp in subpackages:
            if f'"{sp}"' not in spec_text and f"'{sp}'" not in spec_text:
                violations.append(
                    _fmt_fail(
                        "spec-sync",
                        "packaging/pob.spec",
                        1,
                        f"Subpackage `{sp}` missing from pob.spec hiddenimports",
                        'Use `collect_submodules("pob_cli")` in pob.spec hiddenimports.',
                    )
                )

    return violations


def _check_lua_file_budget(
    fpath: Path,
    disp_path: str,
    violations: list[str],
    extremes: dict[str, tuple[int, str]],
) -> None:
    raw = fpath.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    line_count = len(text.splitlines())
    lf_bytes = len(text.encode("utf-8"))
    byte_count = max(len(raw), lf_bytes + line_count)

    if line_count > extremes["maxLines"][0]:
        extremes["maxLines"] = (line_count, disp_path)
    if byte_count > extremes["maxBytes"][0]:
        extremes["maxBytes"] = (byte_count, disp_path)

    if line_count > MAX_FILE_LINES or byte_count > MAX_FILE_BYTES:
        violations.append(
            _fmt_fail(
                "file-budget",
                disp_path,
                1,
                f"File has {line_count} lines / {byte_count} bytes (limit: {MAX_FILE_LINES}L / {MAX_FILE_BYTES}B)",
                "Split cohesive responsibilities into smaller submodules under <=650L and <=24000B.",
            )
        )


def _check_budget_for_file(
    fpath: Path,
    disp_path: str,
    violations: list[str],
    extremes: dict[str, tuple[int, str]],
) -> ast.Module:
    raw = fpath.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    line_count = len(text.splitlines())
    lf_bytes = len(text.encode("utf-8"))
    # Enforce worst-case CRLF byte ceiling across all OS line-ending configs
    byte_count = max(len(raw), lf_bytes + line_count)

    if line_count > extremes["maxLines"][0]:
        extremes["maxLines"] = (line_count, disp_path)
    if byte_count > extremes["maxBytes"][0]:
        extremes["maxBytes"] = (byte_count, disp_path)

    if line_count > MAX_FILE_LINES or byte_count > MAX_FILE_BYTES:
        violations.append(
            _fmt_fail(
                "file-budget",
                disp_path,
                1,
                f"File has {line_count} lines / {byte_count} bytes (limit: {MAX_FILE_LINES}L / {MAX_FILE_BYTES}B)",
                "Split cohesive responsibilities into smaller submodules under <=650L and <=24000B.",
            )
        )

    tree = ast.parse(text, filename=disp_path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > extremes["maxFn"][0]:
                extremes["maxFn"] = (span, f"{disp_path}:{node.name}")
            if span > MAX_FN_LINES:
                violations.append(
                    _fmt_fail(
                        "fn-budget",
                        disp_path,
                        node.lineno,
                        f"Function `{node.name}` spans {span} lines (limit: {MAX_FN_LINES}L)",
                        "Decompose into focused helper functions or handler dispatch tables.",
                    )
                )
    return tree


def _check_pkg_file_layers(
    rel: Path,
    rel_posix: str,
    tree: ast.Module,
    violations: list[str],
    top_edges: dict[str, set[str]],
    edge_sites: dict[tuple[str, str], tuple[str, int]],
) -> None:
    src_unit = _top_unit_for_rel(rel)
    src_layer = LAYER_MAP.get(src_unit)
    if src_layer is None:
        violations.append(
            _fmt_fail(
                "layer-dag",
                rel_posix,
                1,
                f"Top-level unit `{src_unit}` is not registered in LAYER_MAP",
                f"Assign `{src_unit}` to its intended layer (L0..L4) in scripts/arch_lint.py.",
            )
        )
    else:
        top_edges.setdefault(src_unit, set())

    # Strict layer enforcement on ALL runtime imports (top-level AND function-body lazy imports)
    for node in _iter_runtime_imports(tree):
        for dst_unit, full_mod in _resolve_import_target(rel, node):
            if src_unit == "cli_timeless":
                bare = dst_unit.removeprefix("ext:")
                if bare in TIMELESS_CLI_FORBIDDEN_IMPORTS or any(
                    full_mod.startswith(p) for p in ("httpx", "pob_cli.cli_trade", "pob_cli.bench")
                ):
                    violations.append(
                        _fmt_fail(
                            "layer-dag",
                            rel_posix,
                            node.lineno,
                            f"`cli_timeless` must not import `{full_mod}`",
                            "Keep cli_timeless isolated from httpx, cli_trade, and bench.",
                        )
                    )
            if dst_unit.startswith("ext:") or dst_unit == src_unit or src_layer is None:
                continue
            dst_layer = LAYER_MAP.get(dst_unit)
            if dst_layer is None:
                continue
            if dst_layer > src_layer:
                violations.append(
                    _fmt_fail(
                        "layer-dag",
                        rel_posix,
                        node.lineno,
                        f"Upward import: L{src_layer} `{src_unit}` -> L{dst_layer} `{dst_unit}`",
                        "Invert the dependency or extract shared types/helpers into a lower layer.",
                    )
                )

    for node in _iter_top_level_imports(tree):
        for dst_unit, _full_mod in _resolve_import_target(rel, node):
            if dst_unit.startswith("ext:") or dst_unit == src_unit or src_layer is None:
                continue
            if dst_unit in LAYER_MAP:
                top_edges.setdefault(src_unit, set()).add(dst_unit)
                edge_sites.setdefault((src_unit, dst_unit), (rel_posix, node.lineno))


def run_arch_lint(pob_cli_root: Path) -> tuple[list[str], dict[str, object]]:
    pkg_root = pob_cli_root / "src" / "pob_cli"
    py_files = sorted(pkg_root.rglob("*.py"))
    violations: list[str] = []
    top_edges: dict[str, set[str]] = {}
    edge_sites: dict[tuple[str, str], tuple[str, int]] = {}
    extremes: dict[str, tuple[int, str]] = {
        "maxLines": (0, ""),
        "maxBytes": (0, ""),
        "maxFn": (0, ""),
    }

    for fpath in py_files:
        rel = fpath.relative_to(pkg_root)
        rel_posix = rel.as_posix()
        tree = _check_budget_for_file(fpath, rel_posix, violations, extremes)
        _check_pkg_file_layers(rel, rel_posix, tree, violations, top_edges, edge_sites)
        violations.extend(_find_blocked_rpc_calls(tree, rel_posix))

    extra_count = 0
    lua_dir = pkg_root / "lua"
    if lua_dir.is_dir():
        for fpath in sorted(lua_dir.glob("*.lua")):
            disp = fpath.relative_to(pkg_root).as_posix()
            _check_lua_file_budget(fpath, disp, violations, extremes)
            extra_count += 1
    for extra_dir_name in ("scripts", "packaging"):
        extra_dir = pob_cli_root / extra_dir_name
        if not extra_dir.is_dir():
            continue
        for fpath in sorted(extra_dir.rglob("*.py")):
            if any(part in SKIP_COPY_DIRS for part in fpath.parts):
                continue
            disp = fpath.relative_to(pob_cli_root).as_posix()
            _check_budget_for_file(fpath, disp, violations, extremes)
            extra_count += 1

    for cycle in _find_cycles(top_edges):
        site_file, site_line = edge_sites.get((cycle[0], cycle[1]), ("src/pob_cli", 1))
        violations.append(
            _fmt_fail(
                "layer-dag",
                site_file,
                site_line,
                f"Top-level circular import cycle detected: {' -> '.join(cycle)}",
                "Break the cycle by extracting shared helpers to a lower layer.",
            )
        )

    violations.extend(_check_spec_sync(pob_cli_root, pkg_root))

    stats: dict[str, object] = {
        "files": len(py_files),
        "checkedFiles": len(py_files) + extra_count,
        "maxLines": extremes["maxLines"],
        "maxBytes": extremes["maxBytes"],
        "maxFn": extremes["maxFn"],
    }
    return violations, stats
