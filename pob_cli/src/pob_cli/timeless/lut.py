"""Timeless jewel metadata, lookup tables, seed grouping, and additive estimation."""

from __future__ import annotations

import math
import os
import random
import re
import sys
import zlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..errors import CrossGameHallucinationError
from ..formatters import _parse_numeric_or_none
from ..game_data import fold_han
from ..guardrails import validate_engine_root
from ..locale_cfg import current_lang
from ..updater import get_shared_cache_dir, resolve_engine_root
from .xml_helpers import (
    _ALLOCATES_RE as _ALLOCATES_RE,
)
from .xml_helpers import (
    _active_tree_spec as _active_tree_spec,
)
from .xml_helpers import (
    _extract_allocated_notable_names as _extract_allocated_notable_names,
)
from .xml_helpers import (
    _extract_xml_allocated_nodes as _extract_xml_allocated_nodes,
)
from .xml_helpers import (
    _is_historic_socket_jewel as _is_historic_socket_jewel,
)

# Jewels stored as one byte per (notable, seed) in `Data/TimelessJewelData/<stem>.zip`.
# The others (Glorious Vanity, the Abyss family) use variable-length records: every seed is calculated.
FLAT_TABLES = {
    "Lethal Pride": "LethalPride",
    "Brutal Restraint": "BrutalRestraint",
    "Militant Faith": "MilitantFaith",
    "Elegant Hubris": "ElegantHubris",
    "Heroic Tragedy": "HeroicTragedy",
}
JEWEL_TYPE_IDS: dict[str, int] = {
    "Glorious Vanity": 1, "Lethal Pride": 2, "Brutal Restraint": 3,
    "Militant Faith": 4, "Elegant Hubris": 5, "Heroic Tragedy": 6,
    "Festering Vengeance": 7, "Extinguishing Grasp": 8,
    "Baleful Dominion": 9, "Destructive Aspiration": 10, "Reclaimed Malevolence": 11,
}
LUT_STEMS: dict[str, str] = {
    **FLAT_TABLES,
    "Glorious Vanity": "GloriousVanity",
    "Festering Vengeance": "AbyssTecrod", "Extinguishing Grasp": "AbyssUlaman",
    "Baleful Dominion": "AbyssKurgal", "Destructive Aspiration": "AbyssAmanamu",
    "Reclaimed Malevolence": "AbyssZorath",
}
SEED_STEP = {"Elegant Hubris": 20}  # PoB Modules/Data.lua: this one only exists in multiples of 20
STAT_KEYS = (
    "CombinedDPS", "FullDPS", "TotalDPS", "TotalDotDPS", "MinionCombinedDPS",
    "TotalEHP", "Life", "EnergyShield", "Ward", "Mana",
    "Armour", "Evasion", "PhysicalMaximumHitTaken",
    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
    "EffectiveBlockChance", "EffectiveSpellBlockChance", "EffectiveSpellSuppressionChance",
    "Str", "Dex", "Int",
)
# Static Simplified & Traditional Chinese display names and aliases for the 11 Historic jewels.
JEWEL_ZH_NAMES: dict[str, str] = {
    "Brutal Restraint": "残酷的约束", "Elegant Hubris": "优雅的狂妄", "Glorious Vanity": "光彩夺目",
    "Lethal Pride": "致命的骄傲", "Militant Faith": "好战的信仰", "Heroic Tragedy": "英勇悲剧",
    "Festering Vengeance": "溃烂复仇", "Extinguishing Grasp": "熄灭之握", "Baleful Dominion": "噩兆统御",
    "Destructive Aspiration": "毁灭渴望", "Reclaimed Malevolence": "再造恶意",
}
JEWEL_ZH_TW_NAMES: dict[str, str] = {
    "Brutal Restraint": "殘酷的紀律", "Elegant Hubris": "優雅的高傲", "Glorious Vanity": "輝煌的虛榮",
    "Lethal Pride": "致命的驕傲", "Militant Faith": "激進的信仰", "Heroic Tragedy": "英勇的悲劇",
    "Festering Vengeance": "潰爛復仇", "Extinguishing Grasp": "撲滅之握", "Baleful Dominion": "邪惡統治",
    "Destructive Aspiration": "滅亡之願", "Reclaimed Malevolence": "重奪惡意",
}
_JEWEL_ZH_ALIASES: dict[str, str] = {
    **{zh: en for en, zh in JEWEL_ZH_NAMES.items()},
    **{zh: en for en, zh in JEWEL_ZH_TW_NAMES.items()},
    **{fold_han(zh): en for en, zh in JEWEL_ZH_TW_NAMES.items()},
    "优雅的傲慢": "Elegant Hubris", "军团的荣耀": "Glorious Vanity",
    "军团的信仰": "Militant Faith", "英雄悲剧": "Heroic Tragedy",
}
# PoE 3.30 renamed Glorious Vanity's Ahuana -> Mazcotl; accept either name against either PoB version.
_CONQUEROR_ALIASES: dict[str, str] = {
    "ahuana": "Mazcotl",
    "mazcotl": "Ahuana",
}
RESET_EVERY = 150  # each calculated seed leaves one more item in the build; reload before that slows PoB down
MIN_SAMPLE = 200  # groups calculated before the first estimate
MIN_SEEN = 2  # ... holding every (node, table byte) at least this often
VERIFY_BATCH = 50  # estimated-best groups calculated per round
SELF_CHECK = 10  # groups whose second seed is recalculated at the end to confirm the grouping
_SEED_LINE = re.compile(r"^(?:\{variant:[\d,]+\})?(.*\((\d+)-(\d+)\).* (\w+))$")


def default_workers() -> int:
    # An engine grows to roughly 1.3 GB during a search, so stay modest unless the user asks for more.
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def set_idle_priority() -> None:
    """Lowest CPU priority for this process; the PoB engines started afterwards inherit it."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit handle; the default int return truncates it
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x40):  # IDLE_PRIORITY_CLASS
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        try:
            os.nice(19)
        except OSError:
            pass


def _resolve_game_dir(
    worker: Any = None,
    *,
    pob_dir: Path | str | None = None,
    runtime_dir: Path | str | None = None,
) -> Path:
    """Resolve the PoE1 game data directory from an explicit `pob_dir`, an active worker, or engine lookup."""
    if pob_dir:
        p = Path(pob_dir)
        validate_engine_root(p, expected_game="poe1", source_label="pob_dir")
        if p.name.lower() == "poe2" and not (p / "PoE1").is_dir():
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger=f"pob_dir={p}",
                error=f"Timeless engine directory `{p}` points at a PoE2 directory (`PoE2`).",
                remediation="Point `POB_ROOT` / `pob_dir` to a PoE1 PobTools directory or use `pob2`.",
                equivalent_command="pob bootstrap",
                rule_id="ENV_POE2_ENGINE_IN_POB1",
            )
        if (p / "PoE1").is_dir() or not (p / "Data").is_dir():
            return p / "PoE1"
        return p
    if worker is not None and getattr(worker, "game_dir", None) is not None:
        return Path(worker.game_dir)
    rt = Path(runtime_dir) if runtime_dir else None
    return resolve_engine_root(runtime_dir=rt) / "PoE1"


def _worker_batch(
    worker: Any,
    calls: list[tuple[str, dict[str, Any] | None]],
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Pipeline RPCs through `worker.call_batch` when available, else fall back to sequential `worker.call`."""
    if not calls:
        return []
    batch_fn = getattr(worker, "call_batch", None)
    if callable(batch_fn):
        return batch_fn(calls, timeout=timeout)
    return [worker.call(method, params or {}, timeout=timeout) for method, params in calls]


def _parse_stat(val: Any) -> float | None:
    """Parse a raw PoB stat value into float (including ±inf), returning None for missing/NaN/non-numeric."""
    if val is None or isinstance(val, bool) or (isinstance(val, str) and not val.strip()):
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None or math.isnan(parsed):
        return None
    return parsed


def _json_safe_stat(val: Any) -> int | float | str | None:
    """Normalize a PoB stat value for RFC-8259 JSON and hashable outcome keys (`nan` -> None, ±inf -> str)."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val):
            return None
        if math.isinf(val):
            return "inf" if val > 0 else "-inf"
        return val
    parsed = _parse_stat(val)
    if parsed is None:
        return None
    if math.isinf(parsed):
        return "inf" if parsed > 0 else "-inf"
    return parsed


def _stat_rank_key(val: Any) -> tuple[int, float]:
    """Total-order ranking key (higher is better; safe against None, NaN, 'inf', '-inf', strings)."""
    parsed = _parse_stat(val)
    if parsed is None:
        return (0, 0.0)
    if math.isinf(parsed):
        return (2, 0.0) if parsed > 0 else (-1, 0.0)
    return (1, parsed)


def load_jewels(game_dir: Path) -> dict[str, dict[str, Any]]:
    """Timeless jewel definitions from PoB's unique item database, keyed by English name."""
    text = (Path(game_dir) / "Data" / "Uniques" / "jewel.lua").read_text(encoding="utf-8")
    jewels: dict[str, dict[str, Any]] = {}
    for entry in re.split(r"\]\],[^\[]*\[\[", text):  # entries may have a `-- comment` line between them
        lines = [x.strip() for x in entry.strip().splitlines() if x.strip()]
        if "Historic" not in lines or "Implicits: 0" not in lines or not any(_SEED_LINE.match(x) for x in lines):
            continue
        name = lines[0]
        conquerors: dict[str, str] = {}
        tail: list[str] = []
        lo = hi = 0
        for line in lines[lines.index("Implicits: 0") + 1:]:
            m = _SEED_LINE.match(line)
            if m:
                lo, hi = int(m.group(2)), int(m.group(3))
                conquerors[m.group(4)] = m.group(1).replace(f"({lo}-{hi})", "{seed}")
            elif not line.startswith("{variant:"):  # variant lines are optional mods (Militant Faith devotion)
                tail.append(line)
        jewels[name] = {
            "base": lines[1],
            "head": [x for x in lines[2:] if x.startswith(("Limited to:", "Radius:"))],
            "conquerors": conquerors,
            "tail": tail,
            "seeds": range(lo, hi + 1, SEED_STEP.get(name, 1)),
        }
    return jewels


def _resolve_conqueror(conqueror: str | None, jewel: dict[str, Any], name: str) -> str:
    """Match a conqueror name case-insensitively, including the 3.30 Ahuana <-> Mazcotl rename."""
    conquerors: dict[str, str] = jewel["conquerors"]
    if not conqueror:
        return next(iter(conquerors))
    query = conqueror.strip()
    by_lower = {k.lower(): k for k in conquerors}
    matched = by_lower.get(query.lower())
    if matched is not None:
        return matched
    alias = _CONQUEROR_ALIASES.get(query.lower())
    if alias is not None:
        matched = by_lower.get(alias.lower())
        if matched is not None:
            return matched
    raise ValueError(f"{name} conquerors: {', '.join(conquerors)}")


def jewel_text(jewel: dict[str, Any], name: str, conqueror: str | None = None, mods: Sequence[str] = ()) -> str:
    """PoB item text for the jewel with the seed left as `{seed}`."""
    resolved = _resolve_conqueror(conqueror, jewel, name)
    return "\n".join([
        "Rarity: UNIQUE", name, jewel["base"], "Item Level: 84", *jewel["head"], "Implicits: 0",
        jewel["conquerors"][resolved], *mods, *jewel["tail"],
    ])


def load_node_index(game_dir: Path) -> dict[int, int]:
    """Node id -> row in the flat tables (only notables have rows)."""
    mapping_path = Path(game_dir) / "Data" / "TimelessJewelData" / "NodeIndexMapping.lua"
    text = mapping_path.read_text(encoding="utf-8")
    match = re.search(r'nodeIDList\["sizeNotable"\] = (\d+)', text)
    if match is None:
        raise RuntimeError(f"Malformed NodeIndexMapping.lua at {mapping_path}: missing sizeNotable")
    n_notable = int(match.group(1))
    rows = {int(a): int(b) for a, b in re.findall(r"nodeIDList\[(\d+)\] = \{ index = (\d+),", text)}
    return {node: row for node, row in rows.items() if row < n_notable}


def _read_lut_zip_or_parts(base_dir: Path, stem: str) -> tuple[bytes | None, Path]:
    whole = base_dir / f"{stem}.zip"
    if whole.is_file():
        return whole.read_bytes(), whole
    part0 = base_dir / f"{stem}.zip.part0"
    if part0.is_file():
        parts: list[tuple[int, Path]] = []
        for p in base_dir.glob(f"{stem}.zip.part*"):
            suf = p.name.rsplit(".part", 1)[-1]
            if suf.isdigit():
                parts.append((int(suf), p))
        if parts:
            parts.sort(key=lambda x: x[0])
            return b"".join(p.read_bytes() for _, p in parts), part0
    return None, whole


def load_table(
    game_dir: Path,
    stem: str,
    *,
    runtime_dir: Path | str | None = None,
    cache_dir: Path | None = None,
) -> bytes:
    """Decompressed lookup table. Reads PoB's file (or shared timeless cache); never writes next to PoB."""
    g_data = Path(game_dir) / "Data" / "TimelessJewelData"
    raw, table_path = _read_lut_zip_or_parts(g_data, stem)
    if raw is None and cache_dir is not None:
        raw, table_path = _read_lut_zip_or_parts(Path(cache_dir), stem)
    if raw is None:
        rt = Path(runtime_dir) if runtime_dir else None
        c_dir = get_shared_cache_dir(rt, pob_root=Path(game_dir).parent) / "timeless_lut"
        raw, _ = _read_lut_zip_or_parts(c_dir, stem)
        if raw is not None:
            table_path = c_dir / f"{stem}.zip"
    if raw is None:
        raw = (g_data / f"{stem}.zip").read_bytes()
    try:
        return zlib.decompress(raw)
    except zlib.error as exc:
        raise RuntimeError(f"Failed to decompress timeless jewel table {table_path}: {exc}") from exc


def group_seeds(seeds: Sequence[int], table: bytes, index: dict[int, int], nodes: Sequence[int]) -> dict[bytes, list[int]]:
    """Table bytes on `nodes` (the signature) -> the seeds that share them."""
    n = len(seeds)
    groups: dict[bytes, list[int]] = defaultdict(list)
    for offset, seed in enumerate(seeds):
        groups[bytes(table[index[node] * n + offset] for node in nodes)].append(seed)
    return dict(groups)


def fit_pieces(samples: Sequence[tuple[bytes, float]], rounds: int = 25) -> Callable[[bytes], float]:
    """Additive estimate from calculated samples: value(signature) = mean + one effect per (position, byte).

    Backfitting: each position's effects are the average of what the other positions leave unexplained.
    Non-finite sample values are ignored safely so NaN/Inf never poisons predictions.
    """
    finite: list[tuple[bytes, float]] = []
    for sig, raw_v in samples:
        v = _parse_stat(raw_v)
        if v is not None and math.isfinite(v):
            finite.append((sig, v))
    if not finite:
        return lambda _sig: 0.0
    k = len(finite[0][0])
    mean = sum(value for _, value in finite) / len(finite)
    effects: list[dict[int, float]] = [defaultdict(float) for _ in range(k)]
    for _ in range(rounds):
        for pos in range(k):
            left: dict[int, list[float]] = defaultdict(list)
            for sig, value in finite:
                left[sig[pos]].append(value - mean - sum(effects[p][sig[p]] for p in range(k) if p != pos))
            for byte, values in left.items():
                effects[pos][byte] = sum(values) / len(values)
    return lambda sig: mean + sum(effects[p].get(sig[p], 0.0) for p in range(k))


def narrow_down(
    all_sigs: Sequence[bytes],
    results: dict[bytes, dict[str, Any]],
    calculate: Callable[[Sequence[bytes]], None],
    stat: str,
    top: int,
    rng: random.Random,
    say: Callable[[str], None],
) -> dict[str, Any] | None:
    """Calculate a sample, estimate the other groups from it, and keep calculating the estimated-best ones until
    none of the rest is estimated to reach the `top` best. `calculate(sigs)` adds those groups to `results`.

    Returns the estimate's summary, or None when every group ended up calculated.
    """
    k = len(all_sigs[0])
    by_byte: list[dict[int, list[bytes]]] = [defaultdict(list) for _ in range(k)]
    for sig in all_sigs:
        for pos in range(k):
            by_byte[pos][sig[pos]].append(sig)
    sample_size = max(MIN_SAMPLE, 2 * sum(len(d) for d in by_byte))
    if len(all_sigs) <= 3 * sample_size:
        calculate(all_sigs)
        return None
    # The effect of a byte that was never calculated is a blind guess: a purely random sample was seen to leave
    # seeds out of the top list. So take every (node, byte) MIN_SEEN times first, then fill up at random.
    sample = dict.fromkeys(
        sig for d in by_byte for members in d.values() for sig in rng.sample(members, min(MIN_SEEN, len(members)))
    )
    rest = [sig for sig in all_sigs if sig not in sample]
    sample.update(dict.fromkeys(rng.sample(rest, max(0, sample_size - len(sample)))))
    calculate(list(sample))
    say(f"{len(results)} sampled of {len(all_sigs)} groups; estimating the rest")

    while True:
        parsed_map: dict[bytes, float] = {}
        has_nonfinite = False
        for sig, stats in results.items():
            parsed = _parse_stat(stats.get(stat))
            if parsed is None or not math.isfinite(parsed):
                has_nonfinite = True
                break
            parsed_map[sig] = parsed

        if has_nonfinite or not parsed_map:
            # Missing/NaN/±inf stats cannot be bounded safely by an additive model (and mapping None -> 0.0
            # would corrupt negative-stat cutoffs such as ChaosResist); fall back to exact evaluation.
            remaining = [s for s in all_sigs if s not in results]
            if remaining:
                calculate(remaining)
            return None

        use_log = all(v > 0.0 for v in parsed_map.values())
        done = [
            (sig, math.log(max(v, 1e-9)) if use_log else v)
            for sig, v in parsed_map.items()
        ]
        predict = fit_pieces(done)
        # doubled: the fit has seen these samples, so its error on unseen groups runs higher
        error = 2.0 * max(abs(predict(sig) - v) for sig, v in done)
        if not math.isfinite(error) or (use_log and error > 700.0):
            remaining = [s for s in all_sigs if s not in results]
            if remaining:
                calculate(remaining)
            return None
        cutoff = sorted((v for _, v in done), reverse=True)[min(top, len(done)) - 1]
        open_sigs = sorted(
            (s for s in all_sigs if s not in results and predict(s) + error >= cutoff),
            key=predict,
            reverse=True,
        )
        if not open_sigs:
            return {
                "calculated": len(results),
                "maxError": (math.exp(error) - 1.0) if use_log else error,
                "relative": use_log,
            }
        if len(results) > 0.4 * len(all_sigs):  # the estimate is not narrowing things down: finish exactly
            calculate([s for s in all_sigs if s not in results])
            return None
        calculate(open_sigs[:VERIFY_BATCH])
        say(f"{len(results)} calculated; {max(0, len(open_sigs) - VERIFY_BATCH)} could still reach the top {top}")


def _local_jewel_name(name: str, dialog_local: dict[str, str]) -> str:
    lang = current_lang()
    if lang == "en":
        return name
    local = dialog_local.get(name)
    if local and local != name:
        return local
    if lang == "zh-TW":
        return JEWEL_ZH_TW_NAMES.get(name, JEWEL_ZH_NAMES.get(name, name))
    return JEWEL_ZH_NAMES.get(name, name)


def _resolve_jewel_static(name: str, jewels: dict[str, Any]) -> str | None:
    """Resolve English name or static Chinese display name/alias without starting an engine."""
    query = name.strip()
    q_low = query.lower()
    for key in jewels:
        if key.lower() == q_low:
            return key
    # The names are Simplified, with a Traditional spelling for some: fold so the rest match too.
    zh_mapped = _JEWEL_ZH_ALIASES.get(query) or _JEWEL_ZH_ALIASES.get(fold_han(query))
    if zh_mapped and zh_mapped in jewels:
        return zh_mapped
    return None
