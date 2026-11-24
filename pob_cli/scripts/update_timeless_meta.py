"""Generate compact gzipped Lua metadata (`timeless_meta_3_30.lua.gz`) for `lua/timeless_prng.lua`.

Reads GGG datc64 JSON exports (`alternate_tree_versions`, `alternate_passive_skills`,
`alternate_passive_additions`, plain `.json` or `.json.gz`) and PoB's
`TreeData/<tree_version>/tree.lua` (`["root"]["out"]`) to emit the minimal numeric
table (~3.74 KiB gzipped) required by the pure-LuaJIT TinyMT32 + Abyss Walk engine.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from pob_cli.updater import resolve_engine_root  # noqa: E402

DEFAULT_TREE_VERSION = "3_30"
DEFAULT_OUTPUT = _SRC_DIR / "pob_cli" / "data" / "timeless_meta_3_30.lua.gz"
MAX_JEWEL_TYPE = 11


def extract_root_out(game_dir: Path, tree_version: str = DEFAULT_TREE_VERSION) -> list[int]:
    """Read raw `['root']['out']` edge list from PoB's `TreeData/<tree_version>/tree.lua`."""
    tree_lua = Path(game_dir) / "TreeData" / tree_version / "tree.lua"
    if not tree_lua.is_file():
        raise FileNotFoundError(f"Missing PoB tree file: {tree_lua}")
    with tree_lua.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 16384))
        tail = fh.read().decode("utf-8", errors="replace")
    match = re.search(r'\["root"\]\s*=\s*\{.*?\["out"\]\s*=\s*\{(.*?)\}', tail, re.S)
    if not match:
        full_text = tree_lua.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'\["root"\]\s*=\s*\{.*?\["out"\]\s*=\s*\{(.*?)\}', full_text, re.S)
    if not match:
        raise RuntimeError(f"Could not locate ['root']['out'] in {tree_lua}")
    return [int(tok) for tok in re.findall(r'"(\d+)"', match.group(1))]


def _load_json_table(raw_dir: Path, stem: str) -> list[dict[str, Any]]:
    gz_path = raw_dir / f"{stem}.json.gz"
    if gz_path.is_file():
        with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    plain_path = raw_dir / f"{stem}.json"
    if plain_path.is_file():
        return json.loads(plain_path.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"Missing {stem}.json.gz (or {stem}.json) in {raw_dir}. "
        "Export AlternateTreeVersions, AlternatePassiveSkills, and AlternatePassiveAdditions first."
    )


def render_meta_lua(
    versions_raw: list[dict[str, Any]],
    skills_raw: list[dict[str, Any]],
    adds_raw: list[dict[str, Any]],
    root_out: list[int],
    *,
    tree_version: str = DEFAULT_TREE_VERSION,
) -> tuple[str, dict[str, int]]:
    """Render compact Lua module text and return `(lua_text, counts)`."""
    ver_lines: list[str] = []
    for row in versions_raw:
        k = int(row["_key"])
        if 1 <= k <= MAX_JEWEL_TYPE:
            sa_rep = "true" if row["Var1"] else "false"
            sn_rep = "true" if row["Var2"] else "false"
            msvc = "true" if k >= 7 else "false"
            ver_lines.append(
                f"  [{k}]={{"
                f"sa_rep={sa_rep},sn_rep={sn_rep},"
                f"min_sa={int(row['Var3'])},max_sa={int(row['Var4'])},"
                f"min_sn={int(row['Var5'])},max_sn={int(row['Var6'])},"
                f"min_no={int(row['Var7'])},max_no={int(row['Var8'])},"
                f"not_w={int(row['Var9'])},msvc={msvc}}},"
            )

    skills_by: dict[int, dict[int, list[dict[str, Any]]]] = {}
    for row in skills_raw:
        jt = int(row["AlternateTreeVersionsKey"])
        if not (1 <= jt <= MAX_JEWEL_TYPE):
            continue
        for pt in row.get("PassiveType", []):
            skills_by.setdefault(jt, {}).setdefault(int(pt), []).append(row)

    adds_by: dict[int, dict[int, list[dict[str, Any]]]] = {}
    for row in adds_raw:
        jt = int(row["AlternateTreeVersionsKey"])
        if not (1 <= jt <= MAX_JEWEL_TYPE):
            continue
        for pt in row.get("PassiveType", []):
            adds_by.setdefault(jt, {}).setdefault(int(pt), []).append(row)

    lines = [
        f"-- Auto-generated compact timeless jewel weight metadata ({tree_version}).",
        "local M = {}",
        f'M.tree_version = "{tree_version}"',
        f"M.additions_count = {len(adds_raw)}",
        "M.root_out = {" + ",".join(str(x) for x in root_out) + "}",
        "M.versions = {",
        *ver_lines,
        "}",
        "M.skills = {",
    ]
    for jt in sorted(skills_by):
        lines.append(f"  [{jt}] = {{")
        for pt in sorted(skills_by[jt]):
            entries: list[str] = []
            for row in skills_by[jt][pt]:
                nstats = min(len(row.get("StatsKeys", [])), 4)
                entries.append(
                    f"{{{int(row['_key'])},{int(row['SpawnWeight'])},{nstats},"
                    f"{int(row['Stat1Min'])},{int(row['Stat1Max'])},"
                    f"{int(row['Stat2Min'])},{int(row['Stat2Max'])},"
                    f"{int(row['Var9'])},{int(row['Var10'])},"
                    f"{int(row['Var11'])},{int(row['Var12'])},"
                    f"{int(row['RandomMin'])},{int(row['RandomMax'])}}}"
                )
            lines.append(f"    [{pt}] = {{{','.join(entries)}}},")
        lines.append("  },")
    lines.append("}")

    lines.append("M.additions = {")
    lines.append("  -- tuple: {idx, weight, nstats, s1min, s1max, s2min, s2max}")
    for jt in sorted(adds_by):
        lines.append(f"  [{jt}] = {{")
        for pt in sorted(adds_by[jt]):
            entries = []
            total_w = 0
            for row in adds_by[jt][pt]:
                w = int(row["SpawnWeight"])
                total_w += w
                nstats = min(len(row.get("StatsKeys", [])), 2)
                entries.append(
                    f"{{{int(row['_key'])},{w},{nstats},"
                    f"{int(row['Stat1Min'])},{int(row['Stat1Max'])},"
                    f"{int(row['Var6'])},{int(row['Var7'])}}}"
                )
            lines.append(f"    [{pt}] = {{total_w={total_w},list={{{','.join(entries)}}}}},")
        lines.append("  },")
    lines.append("}")
    lines.append("return M")

    lua_text = "\n".join(lines) + "\n"
    counts = {
        "versions": len(ver_lines),
        "skills": len(skills_raw),
        "additions": len(adds_raw),
        "root_out_len": len(root_out),
    }
    return lua_text, counts


def _resolve_game_dir(pob_dir: Path | None) -> Path:
    root = Path(pob_dir) if pob_dir is not None else resolve_engine_root()
    if (root / "TreeData").is_dir():
        return root
    if (root / "PoE1" / "TreeData").is_dir():
        return root / "PoE1"
    raise FileNotFoundError(f"Could not find TreeData directory under {root}")


def generate_timeless_meta(
    raw_meta_dir: Path,
    *,
    pob_dir: Path | None = None,
    tree_version: str = DEFAULT_TREE_VERSION,
    output_path: Path = DEFAULT_OUTPUT,
    out_lua: Path | None = None,
) -> dict[str, Any]:
    """Build and write `timeless_meta_<ver>.lua.gz` from raw GGG datc64 JSONs + PoB tree.lua."""
    game_dir = _resolve_game_dir(pob_dir)
    versions_raw = _load_json_table(raw_meta_dir, "alternate_tree_versions")
    skills_raw = _load_json_table(raw_meta_dir, "alternate_passive_skills")
    adds_raw = _load_json_table(raw_meta_dir, "alternate_passive_additions")
    root_out = extract_root_out(game_dir, tree_version=tree_version)

    lua_text, counts = render_meta_lua(
        versions_raw, skills_raw, adds_raw, root_out, tree_version=tree_version
    )
    lua_bytes = lua_text.encode("utf-8")
    gz_bytes = gzip.compress(lua_bytes, compresslevel=9, mtime=0)
    if gzip.decompress(gz_bytes) != lua_bytes:
        raise RuntimeError("Gzip round-trip verification failed")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(gz_bytes)
    if out_lua is not None:
        out_lua.parent.mkdir(parents=True, exist_ok=True)
        out_lua.write_bytes(lua_bytes)

    return {
        "output": output_path.name,
        "tree_version": tree_version,
        "lua_bytes": len(lua_bytes),
        "lua_gz_bytes": len(gz_bytes),
        **counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate compact timeless_meta_<ver>.lua.gz for lua/timeless_prng.lua."
    )
    parser.add_argument(
        "--raw-meta-dir",
        type=Path,
        required=True,
        help="Directory with alternate_tree_versions/skills/additions .json(.gz) files.",
    )
    parser.add_argument(
        "--pob-dir",
        type=Path,
        default=None,
        help="Path to PoB root or PoE1 directory containing TreeData/<ver>/tree.lua.",
    )
    parser.add_argument(
        "--tree-version",
        type=str,
        default=DEFAULT_TREE_VERSION,
        help=f"Passive tree version folder name (default: {DEFAULT_TREE_VERSION}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .lua.gz path (default: pob_cli/src/pob_cli/data/timeless_meta_3_30.lua.gz).",
    )
    parser.add_argument(
        "--out-lua",
        type=Path,
        default=None,
        help="Optional path to also write uncompressed .lua output.",
    )
    args = parser.parse_args(argv)
    summary = generate_timeless_meta(
        args.raw_meta_dir,
        pob_dir=args.pob_dir,
        tree_version=args.tree_version,
        output_path=args.output,
        out_lua=args.out_lua,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
