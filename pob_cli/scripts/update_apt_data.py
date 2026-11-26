#!/usr/bin/env python3
"""Deterministic offline generator for trimmed awakened-poe-trade (MIT) data supplements.

Reads `renderer/public/data/` from a local `SnosMe/awakened-poe-trade` checkout and emits
deterministic `mtime=0`, `compresslevel=9` `.json.gz` artifacts under `pob_cli/src/pob_cli/data/`:
- `apt_item_drops.json.gz`: boss/invitation/fragment/map drop groups with bilingual EN/ZH names.
- `apt_non_gear_items.json.gz`: non-gear bilingual dictionary (divination cards, currencies,
  fragments, scarabs, oils, catalysts, fossils, essences, maps) absent from PoB SQLite.
- `apt_trade_supplements.json.gz`: trade stat polarity (`better` in `{-1, 0}`) and trimmed
  folded-Chinese trade stat templates (`pseudo.*`, `option: true`, Watcher's Eye, crafted/implicit/fractured/enchant).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from pob_cli.game_data.tw_norm import fold_han  # noqa: E402

DEFAULT_OUT_DIR = _SRC_DIR / "pob_cli" / "data"

# Mainland CN wording overrides where zh-CN differs meaningfully from folded zh-TW.
# Tuple form: (primary_cn, *extra_cn_aliases).
CN_OVERRIDES: dict[str, tuple[str, ...]] = {
    # High-frequency currencies
    "Orb of Annulment": ("剥离石",),
    "Awakener's Orb": ("觉醒者之石", "觉醒石"),
    "Hinekora's Lock": ("欣妮库拉的发丝", "希涅科拉的发丝"),
    "Veiled Exalted Orb": ("隐匿崇高石", "隐匿石"),
    "Veiled Chaos Orb": ("隐匿混沌石",),
    "Orb of Dominance": ("支配之石", "统御宝珠"),
    "Orb of Conflict": ("冲突石", "冲突宝珠"),
    "Sacred Orb": ("圣洁石", "神圣玉"),
    "Orb of Unmaking": ("撤回石",),
    "Orb of Fusing": ("链接石",),
    "Ancient Orb": ("远古石",),
    "Orb of Scouring": ("重铸石",),
    "Orb of Regret": ("后悔石",),
    "Orb of Alchemy": ("点金石",),
    "Orb of Alteration": ("改造石",),
    "Orb of Chance": ("机会石",),
    "Orb of Horizons": ("地平石",),
    "Orb of Binding": ("高阶点金石",),
    "Harbinger's Orb": ("先驱石",),
    "Tailoring Orb": ("裁剪石",),
    "Tempering Orb": ("回火石",),
    "Tainted Mythic Orb": ("污秽神话石",),
    "Tainted Chromatic Orb": ("污秽幻色石",),
    "Tainted Jeweller's Orb": ("污秽工匠石",),
    "Tainted Orb of Fusing": ("污秽链接石",),
    "Eldritch Chaos Orb": ("古灵混沌石",),
    "Eldritch Exalted Orb": ("古灵崇高石",),
    "Eldritch Orb of Annulment": ("古灵剥离石",),
    "Mirror of Kalandra": ("卡兰德的魔镜", "魔镜"),
    "Mirror Shard": ("魔镜碎片",),
    "Fracturing Shard": ("破裂碎片",),
    # High-frequency divination cards
    "The Apothecary": ("药剂师",),
    "The Doctor": ("医者",),
    "House of Mirrors": ("明镜",),
    "The Fiend": ("恶魔",),
    "The Nurse": ("护士",),
    "Unrequited Love": ("痴恋",),
    "The Insane Cat": ("狂猫", "疯猫"),
    "Seven Years Bad Luck": ("七年厄运", "七年霉运"),
    "The Price of Devotion": ("奉献的代价",),
    "Brother's Gift": ("兄弟的赠礼",),
    "The Immortal": ("不朽",),
    "The Demon": ("魔神",),
    "The Fortunate": ("幸运",),
    "Divine Beauty": ("神圣之美",),
    "The Sephirot": ("生命之树",),
    # Boss fragments / invitations / echoes / iconic drop uniques
    "Echo of Loneliness": ("孤寂回响", "孤独的回响"),
    "Memory of Loneliness": ("孤寂记忆", "孤独的记忆"),
    "Syndicate Medallion": ("辛迪加勋章",),
    "Fragment of the Minotaur": ("牛头人碎片",),
    "Fragment of the Chimera": ("奇美拉碎片",),
    "Fragment of the Phoenix": ("凤凰碎片",),
    "Fragment of the Hydra": ("九头蛇碎片",),
    "Legacy of the Rose": ("玫瑰遗产", "玫瑰杖"),
    "Yoke of Suffering": ("苦难之轭",),
    "Mageblood": ("法师之血", "法血"),
    "Headhunter": ("猎首",),
    "Ashes of the Stars": ("群星之灰",),
    "Crystallised Omniscience": ("结晶全知",),
    "Progenesis": ("新生",),
    "Nimis": ("尼米斯",),
    "Original Sin": ("原罪",),
    "Forbidden Flesh": ("禁断之肉",),
    "Forbidden Flame": ("禁断之火",),
    "The Devouring Diadem": ("吞噬者王冠",),
}

_CRAFTABLE_NON_GEAR_PREFIXES: tuple[str, ...] = (
    "Contract:",
    "Blueprint:",
    "Expedition Logbook",
    "Chronicle of Atzoatl",
    "Inscribed Ultimatum",
    "Forbidden Tome",
)
_CRAFTABLE_NON_GEAR_KEYWORDS: tuple[str, ...] = (
    "Map",
    "Invitation",
    "Memory",
    "Logbook",
    "Idol",
    "Relic",
    "Tome",
)


def _resolve_data_root(apt_dir: Path) -> Path:
    cand = apt_dir / "renderer" / "public" / "data"
    if cand.is_dir():
        return cand
    if (apt_dir / "item-drop.json").is_file():
        return apt_dir
    raise FileNotFoundError(f"Could not find awakened-poe-trade data directory under {apt_dir}")


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s:
            rows.append(json.loads(s))
    return rows


def _write_deterministic_gz(path: Path, payload: Any) -> int:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compressed)
    return len(compressed)


def _bilingual_tuple(ns_code: str, en_name: str, tw_name: str) -> list[str]:
    tw_folded = fold_han(tw_name).strip() if tw_name else ""
    overrides = CN_OVERRIDES.get(en_name)
    if overrides:
        primary_cn = overrides[0]
        extras = [a for a in overrides[1:] if a and a != primary_cn]
        if tw_folded and tw_folded != primary_cn and tw_folded not in extras:
            extras.append(tw_folded)
    else:
        primary_cn = tw_folded
        extras = []
    row = [ns_code, en_name, primary_cn]
    if extras:
        row.append("|".join(dict.fromkeys(extras)))
    return row


def _build_tw_item_map(data_root: Path) -> dict[tuple[str, str], str]:
    tw_items = _read_ndjson(data_root / "cmn-Hant" / "items.ndjson")
    tw_map: dict[tuple[str, str], str] = {}
    for it in tw_items:
        ns = str(it.get("namespace") or "")
        ref = str(it.get("refName") or "")
        nm = str(it.get("name") or "")
        if ns and ref and nm:
            tw_map.setdefault((ns, ref), nm)
    return tw_map


def build_item_drops(data_root: Path, tw_map: dict[tuple[str, str], str]) -> dict[str, Any]:
    drops = json.loads((data_root / "item-drop.json").read_text(encoding="utf-8"))
    groups: list[dict[str, Any]] = []
    for grp in drops:
        q_rows: list[list[str]] = []
        seen_q: set[tuple[str, str]] = set()
        for raw in grp.get("query") or []:
            if "::" not in raw:
                continue
            ns, rest = raw.split("::", 1)
            en_name = rest.split(" // ", 1)[0].strip()
            if not en_name or (ns, en_name) in seen_q:
                continue
            seen_q.add((ns, en_name))
            tw = tw_map.get((ns, en_name), "")
            q_rows.append(_bilingual_tuple(ns[0], en_name, tw))
        i_rows: list[list[str]] = []
        seen_i: set[tuple[str, str]] = set()
        for raw in grp.get("items") or []:
            if "::" not in raw:
                continue
            ns, rest = raw.split("::", 1)
            en_name = rest.split(" // ", 1)[0].strip()
            if not en_name or (ns, en_name) in seen_i:
                continue
            seen_i.add((ns, en_name))
            tw = tw_map.get((ns, en_name), "")
            i_rows.append(_bilingual_tuple(ns[0], en_name, tw))
        if q_rows and i_rows:
            groups.append({"q": q_rows, "i": i_rows})
    return {"v": 1, "g": groups}


def _is_non_gear_item(it: dict[str, Any]) -> bool:
    ns = str(it.get("namespace") or "")
    ref = str(it.get("refName") or it.get("name") or "")
    if not ref:
        return False
    if ns in ("DIVINATION_CARD", "AREA"):
        return True
    if ns != "ITEM":
        return False
    if not it.get("craftable"):
        return True
    if "armour" in it:
        return False
    if any(ref.startswith(p) for p in _CRAFTABLE_NON_GEAR_PREFIXES):
        return True
    return any(kw in ref for kw in _CRAFTABLE_NON_GEAR_KEYWORDS)


def build_non_gear_items(data_root: Path, tw_map: dict[tuple[str, str], str]) -> dict[str, Any]:
    en_items = _read_ndjson(data_root / "en" / "items.ndjson")
    rows: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for it in en_items:
        if not _is_non_gear_item(it):
            continue
        ns = str(it.get("namespace") or "")
        en_name = str(it.get("refName") or it.get("name") or "").strip()
        if not en_name or (ns, en_name) in seen:
            continue
        seen.add((ns, en_name))
        tw = tw_map.get((ns, en_name), "")
        rows.append(_bilingual_tuple(ns[0], en_name, tw))
    rows.sort(key=lambda r: (r[0], r[1]))
    return {"v": 1, "items": rows}


_CJK_BRACKET_RE = re.compile(r"[【】「」『』《》]")
_STAT_SPACE_RE = re.compile(r"\s+")


def _iter_flat_stats(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for row in rows:
        sub = row.get("stats")
        if isinstance(sub, list):
            for child in sub:
                if isinstance(child, dict):
                    yield child
        else:
            yield row


def _normalize_zh_template(raw: str) -> str:
    s = _CJK_BRACKET_RE.sub("", fold_han(raw)).strip()
    return _STAT_SPACE_RE.sub(" ", s)


def build_trade_supplements(data_root: Path) -> dict[str, Any]:
    en_stats = _read_ndjson(data_root / "en" / "stats.ndjson")
    tw_stats = _read_ndjson(data_root / "cmn-Hant" / "stats.ndjson")

    polarity: dict[str, int] = {}
    for entry in _iter_flat_stats(en_stats):
        better = entry.get("better")
        if better not in (-1, 0):
            continue
        ids_map = (entry.get("trade") or {}).get("ids") or {}
        if not isinstance(ids_map, dict):
            continue
        for id_list in ids_map.values():
            if isinstance(id_list, list):
                for stat_id in id_list:
                    if isinstance(stat_id, str) and stat_id:
                        polarity[stat_id] = int(better)

    zh_templates: dict[str, str] = {}
    for entry in _iter_flat_stats(tw_stats):
        if entry.get("mercenary") or entry.get("fromAreaMods") or entry.get("anointments"):
            continue
        trade = entry.get("trade") or {}
        ids_map = trade.get("ids") or {}
        if not isinstance(ids_map, dict):
            continue
        ref = str(entry.get("ref") or "").strip()
        if not ref:
            continue
        keep = (
            "pseudo" in ids_map
            or bool(trade.get("option"))
            or "while affected by" in ref.lower()
            or "crafted" in ids_map
            or "implicit" in ids_map
            or "fractured" in ids_map
            or "enchant" in ids_map
            or bool(entry.get("modFamily"))
            or bool(entry.get("jewelleryQuality"))
        )
        if not keep:
            continue
        for matcher in entry.get("matchers") or []:
            if not isinstance(matcher, dict):
                continue
            ms = str(matcher.get("string") or "").strip()
            if not ms:
                continue
            key = _normalize_zh_template(ms)
            if key:
                zh_templates.setdefault(key, ref)

    return {"v": 1, "polarity": polarity, "zh": zh_templates}


def generate_apt_data(apt_dir: Path, *, out_dir: Path = DEFAULT_OUT_DIR) -> dict[str, int]:
    data_root = _resolve_data_root(apt_dir)
    tw_map = _build_tw_item_map(data_root)
    sizes: dict[str, int] = {}
    drops_payload = build_item_drops(data_root, tw_map)
    sizes["apt_item_drops.json.gz"] = _write_deterministic_gz(out_dir / "apt_item_drops.json.gz", drops_payload)
    non_gear_payload = build_non_gear_items(data_root, tw_map)
    sizes["apt_non_gear_items.json.gz"] = _write_deterministic_gz(
        out_dir / "apt_non_gear_items.json.gz", non_gear_payload
    )
    if (data_root / "en" / "stats.ndjson").is_file() and (data_root / "cmn-Hant" / "stats.ndjson").is_file():
        trade_payload = build_trade_supplements(data_root)
        sizes["apt_trade_supplements.json.gz"] = _write_deterministic_gz(
            out_dir / "apt_trade_supplements.json.gz", trade_payload
        )
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate trimmed APT data supplements (.json.gz).")
    parser.add_argument("--apt-dir", type=Path, required=True, help="Path to awakened-poe-trade checkout or data dir.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory for .json.gz files.")
    args = parser.parse_args(argv)
    sizes = generate_apt_data(args.apt_dir, out_dir=args.out_dir)
    for name, sz in sorted(sizes.items()):
        print(f"Wrote {name}: {sz:,} bytes")
    print(f"Total: {sum(sizes.values()):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
