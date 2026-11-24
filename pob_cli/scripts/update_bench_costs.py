"""Generate/refresh `pob_cli/src/pob_cli/data/bench_costs.json`.

Authoritative mod text, Prefix/Suffix affix classification, mod group, level,
applicable PoB item types, and zh-rCN translations are ALWAYS read from local
PoB `ModMaster.lua` (via `game_data.ensure_gamedata_index()`).

Crafting currency costs and unlock locations are overlaid from either:
- `--from-local-poedb <path>`: an existing local poedb JSON dump (offline)
- default online mode: fetches `https://poedb.tw/us/Crafting_Bench` via
  `importers._make_httpx_client` (honoring `--proxy` / `POB_PROXY`).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Allow running directly as `python pob_cli/scripts/update_bench_costs.py`
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from pob_cli import bench, game_data, importers  # noqa: E402

DEFAULT_OUTPUT = _SRC_DIR / "pob_cli" / "data" / "bench_costs.json"
POEDB_BENCH_URL = "https://poedb.tw/us/Crafting_Bench"

_DASH_RE = re.compile(r"[\u2013\u2014\u2212]")
_GRANT_FOCUS_RE = re.compile(r"\s*grant focus skill \[1\]\s*", re.IGNORECASE)
_GRANTS_SKILL_RE = re.compile(r"\s*Grants Level \d+ [A-Za-z ']+$")
_COST_PATTERN = re.compile(r"(\d+)\s*[×xX]\s*([A-Za-z][A-Za-z' -]+)")

_COARSE_SLOTS: frozenset[str] = frozenset(
    {
        "helmet",
        "body_armour",
        "gloves",
        "boots",
        "shield",
        "quiver",
        "ring1",
        "ring2",
        "amulet",
        "belt",
        "weapon1",
        "weapon2",
        "flask",
    }
)

_POEDB_CLASS_TO_COARSE: dict[str, tuple[str, ...]] = {
    "helmet": ("helmet",),
    "body armour": ("body_armour",),
    "gloves": ("gloves",),
    "boots": ("boots",),
    "shield": ("shield", "weapon2"),
    "quiver": ("quiver", "weapon2"),
    "ring": ("ring1", "ring2"),
    "amulet": ("amulet",),
    "belt": ("belt",),
    "flask": ("flask",),
    "one hand melee": ("weapon1", "weapon2"),
    "one hand ranged": ("weapon1", "weapon2"),
    "one handed": ("weapon1", "weapon2"),
    "two hand melee": ("weapon1",),
    "two hand ranged": ("weapon1",),
    "two handed": ("weapon1",),
    "bow": ("weapon1",),
    "wand": ("weapon1", "weapon2"),
    "claw": ("weapon1", "weapon2"),
    "dagger": ("weapon1", "weapon2"),
    "sceptre": ("weapon1", "weapon2"),
    "staff": ("weapon1",),
}


def _clean_poedb_mod_text(text: str) -> str:
    s = _DASH_RE.sub("-", text)
    s = _GRANT_FOCUS_RE.sub(" ", s)
    s = _GRANTS_SKILL_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _coarse_slots_from_record(rec: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for s in rec.get("slots") or []:
        k = bench.slot_key(str(s))
        if k in _COARSE_SLOTS:
            out.add(k)
    if out:
        return out
    for cls in rec.get("classes") or []:
        mapped = _POEDB_CLASS_TO_COARSE.get(str(cls).strip().lower())
        if mapped:
            out.update(mapped)
        else:
            k = bench.slot_key(str(cls))
            if k in _COARSE_SLOTS:
                out.add(k)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class _PoedbBenchTableParser(HTMLParser):
    """Minimal stdlib HTML parser for poedb.tw/us/Crafting_Bench table rows."""

    def __init__(self) -> None:
        super().__init__()
        self._in_tr = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "tr":
            self._in_tr = True
            self._current_row = []
        elif t in ("td", "th") and self._in_tr:
            self._in_cell = True
            self._cell_parts = []
        elif t == "br" and self._in_cell:
            self._cell_parts.append(" ")
        elif t == "img" and self._in_cell:
            attr_map = dict(attrs)
            alt = (attr_map.get("alt") or attr_map.get("title") or "").strip()
            if alt:
                self._cell_parts.append(f" {alt} ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            cell_text = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
            self._current_row.append(cell_text)
        elif t == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _extract_entries_from_local_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for rec in raw.get("recipes", []):
        cost = str(rec.get("cost") or "").strip()
        unlock = str(rec.get("unlock") or "").strip()
        text = str(rec.get("text") or "").strip()
        if not text or not cost:
            continue
        entries.append(
            {
                "text": text,
                "cost": cost,
                "unlock": unlock,
                "coarse_slots": _coarse_slots_from_record(rec),
            }
        )
    src_meta = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    return entries, {
        "costSource": src_meta.get("en") or POEDB_BENCH_URL,
        "costFetched": src_meta.get("fetched") or date.today().isoformat(),
        "gameVersion": src_meta.get("gameVersion") or "",
    }


def _extract_entries_from_poedb_online(*, proxy: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with importers._make_httpx_client(timeout=30.0, proxy=proxy) as client:
        resp = client.get(POEDB_BENCH_URL)
        resp.raise_for_status()
        html_text = resp.text

    parser = _PoedbBenchTableParser()
    parser.feed(html_text)

    entries: list[dict[str, Any]] = []
    for row in parser.rows:
        if len(row) < 3:
            continue
        # Detect cost column matching "Nx Currency"
        cost_idx = -1
        cost_val = ""
        for idx, cell in enumerate(row):
            m = _COST_PATTERN.search(cell)
            if m and any(w in cell for w in ("Orb", "Bauble", "Shard", "Prism", "Scouring", "Alchemy", "Chaos", "Divine", "Exalted", "Alteration", "Augmentation", "Transmutation", "Regal", "Vaal", "Chromatic", "Jeweller", "Fusing")):
                cost_idx = idx
                cost_val = f"{m.group(1)}x {m.group(2).strip()}"
                break
        if cost_idx < 0:
            continue

        # Mod text is typically in the first or second column before cost
        mod_candidates = [c for c in row[:cost_idx] if c and not c.isdigit()]
        if not mod_candidates:
            continue
        mod_text = max(mod_candidates, key=len)
        unlock = ""
        coarse: set[str] = set()
        for cell in row[cost_idx + 1 :]:
            if not cell:
                continue
            low = cell.lower()
            matched_class = False
            for cls_name, slots in _POEDB_CLASS_TO_COARSE.items():
                if cls_name in low:
                    coarse.update(slots)
                    matched_class = True
            if not matched_class and not unlock:
                unlock = cell.strip()

        entries.append(
            {
                "text": mod_text,
                "cost": cost_val,
                "unlock": unlock,
                "coarse_slots": coarse,
            }
        )

    return entries, {
        "costSource": POEDB_BENCH_URL,
        "costFetched": date.today().isoformat(),
    }


def _build_poedb_index(entries: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_tpl_vals: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_tpl: dict[str, list[dict[str, Any]]] = {}
    for item in entries:
        cleaned = _clean_poedb_mod_text(item["text"])
        tpl = bench.normalize_template(cleaned)
        vals_sig = bench.values_signature(bench.parse_values([cleaned]))
        by_tpl_vals.setdefault((tpl, vals_sig), []).append(item)
        by_tpl.setdefault(tpl, []).append(item)
    return by_tpl_vals, by_tpl


def _pick_best_candidate(
    candidates: list[dict[str, Any]],
    mod_coarse_slots: set[str],
) -> dict[str, Any]:
    if len(candidates) == 1:
        return candidates[0]
    scored = [(_jaccard(mod_coarse_slots, c["coarse_slots"]), idx, c) for idx, c in enumerate(candidates)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]


def _load_modmaster_rows(pob_dir: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    db_path = game_data.ensure_gamedata_index(pob_dir=pob_dir)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        rows = conn.execute("SELECT data_json FROM mods WHERE source = 'Bench' ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows], meta


def generate_bench_costs(
    *,
    from_local_poedb: Path | None = None,
    proxy: str | None = None,
    pob_dir: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    mod_rows, gd_meta = _load_modmaster_rows(pob_dir=pob_dir)

    if from_local_poedb is not None:
        poedb_entries, cost_meta = _extract_entries_from_local_json(from_local_poedb)
    else:
        poedb_entries, cost_meta = _extract_entries_from_poedb_online(proxy=proxy)

    by_tpl_vals, by_tpl = _build_poedb_index(poedb_entries)

    type_table = sorted(
        {str(c) for rec in mod_rows for c in (rec.get("applicable_types") or []) if str(c).strip()}
    )
    type_to_idx = {name: i for i, name in enumerate(type_table)}

    snapshot: list[dict[str, Any]] = []
    matched_exact = 0
    matched_tpl = 0
    unmatched = 0

    for rec in mod_rows:
        lines = [bench.normalize_line(str(s)) for s in rec.get("stats", []) if isinstance(s, str) and s.strip()]
        if not lines:
            continue
        lines_zh = [bench.normalize_line(str(s)) for s in (rec.get("stats_zh") or []) if isinstance(s, str) and s.strip()]
        affix = bench._normalize_affix(str(rec.get("generation_type") or ""))
        affix_code = "p" if affix == "prefix" else "s"
        classes = [str(c) for c in (rec.get("applicable_types") or []) if str(c).strip()]
        mod_slots = set(bench.classes_to_slots(classes)) & _COARSE_SLOTS

        flat = " ".join(lines)
        tpl = bench.normalize_template(flat)
        vals_sig = bench.values_signature(bench.parse_values(lines))

        hit: dict[str, Any] | None = None
        if (tpl, vals_sig) in by_tpl_vals:
            hit = _pick_best_candidate(by_tpl_vals[(tpl, vals_sig)], mod_slots)
            matched_exact += 1
        elif tpl in by_tpl:
            hit = _pick_best_candidate(by_tpl[tpl], mod_slots)
            matched_tpl += 1
        else:
            unmatched += 1

        entry: dict[str, Any] = {
            "id": str(rec["id"]),
            "a": affix_code,
            "g": str(rec.get("group") or ""),
            "lv": int(rec.get("level") or 0),
            "l": lines,
            "t": [type_to_idx[c] for c in classes],
        }
        if lines_zh:
            entry["z"] = lines_zh
        if hit is not None:
            entry["c"] = hit["cost"]
            if hit["unlock"]:
                entry["u"] = hit["unlock"]
        snapshot.append(entry)

    meta = {
        "schemaVersion": 1,
        "authoritativeSource": "PoB PoE1/Data/ModMaster.lua",
        "pobVersion": gd_meta.get("pob_version", "unknown"),
        "dataVersion": gd_meta.get("data_version", "unknown"),
        "generated": date.today().isoformat(),
        "count": len(snapshot),
        "costMatched": matched_exact + matched_tpl,
        "costMatchedExact": matched_exact,
        "costMatchedTemplateFallback": matched_tpl,
        "costUnmatched": unmatched,
        **cost_meta,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Compact single-line-per-recipe JSON formatting (~150KB, <800 lines)
    lines_out = [
        "{",
        f'"meta":{json.dumps(meta, ensure_ascii=False, separators=(",", ":"))},',
        f'"types":{json.dumps(type_table, ensure_ascii=False, separators=(",", ":"))},',
        '"snapshot":[',
    ]
    for idx, item in enumerate(snapshot):
        comma = "," if idx < len(snapshot) - 1 else ""
        lines_out.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + comma)
    lines_out.append("]")
    lines_out.append("}")
    content = "\n".join(lines_out) + "\n"
    output_path.write_text(content, encoding="utf-8")

    # Sanity-check output parses cleanly and has zero unknown affixes
    parsed = json.loads(content)
    bad_affix = [x for x in parsed["snapshot"] if x.get("a") not in ("p", "s")]
    if bad_affix:
        raise RuntimeError(f"Found {len(bad_affix)} entries with invalid affix codes!")

    return {
        "output": str(output_path),
        "bytes": len(content.encode("utf-8")),
        "lines": content.count("\n"),
        **meta,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate clean bench_costs.json from local PoB ModMaster.lua + poedb cost/unlock overlay."
    )
    parser.add_argument(
        "--from-local-poedb",
        type=Path,
        default=None,
        help="Offline mode: extract cost/unlock strings from an existing local poedb JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="HTTP/SOCKS proxy URL for online poedb fetch (overrides POB_PROXY).",
    )
    parser.add_argument(
        "--pob-dir",
        type=Path,
        default=None,
        help="Optional explicit PoB engine directory.",
    )
    args = parser.parse_args(argv)

    summary = generate_bench_costs(
        from_local_poedb=args.from_local_poedb,
        proxy=args.proxy,
        pob_dir=args.pob_dir,
        output_path=args.output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
