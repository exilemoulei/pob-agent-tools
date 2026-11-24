"""PoB Lua parsers for Special/*.lua generated unique item blocks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import LUA_SKIP_KEYS_MOD
from .lua_parser import (
    MOD_TRADEHASH_STRIP_RE,
    _as_seq,
    _LuaParser,
    _parse_lua_return,
    _tokenize_lua,
)

_KNOWN_GENERATED_UNIQUE_VARS: tuple[str, ...] = (
    "megalomaniac",
    "forbiddenShako",
    "replicaForbiddenShako",
    "precursorsEmblem",
    "balanceOfTerror",
    "watchersEye",
    "sublimeVision",
    "voranasMarch",
    "boundByDestiny",
    "skinOfTheLords",
    "impossibleEscape",
    "replicaDragonfangsFlight",
    "pearl",
)


def _load_special_mod_pools(special_dir: Path) -> dict[str, list[str]]:
    pools: dict[str, list[str]] = {
        "watchers_eye": [],
        "sublime_vision": [],
        "voranas_march": [],
        "bound_by_destiny": [],
    }
    we_path = special_dir / "WatchersEye.lua"
    if we_path.is_file():
        parsed = _parse_lua_return(
            we_path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )
        if isinstance(parsed, dict):
            for mod_id, entry in parsed.items():
                if not isinstance(mod_id, str) or not isinstance(entry, dict):
                    continue
                stat_lines = [str(s).strip() for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
                if not stat_lines:
                    continue
                if mod_id.startswith("SublimeVision"):
                    pools["sublime_vision"].extend(stat_lines)
                elif mod_id.startswith("SummonArbalist"):
                    pools["voranas_march"].extend(stat_lines)
                else:
                    pools["watchers_eye"].extend(stat_lines)

    bbd_path = special_dir / "BoundByDestiny.lua"
    if bbd_path.is_file():
        parsed = _parse_lua_return(
            bbd_path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )
        if isinstance(parsed, dict):
            for entry in parsed.values():
                if isinstance(entry, dict):
                    stat_lines = [str(s).strip() for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
                    if stat_lines:
                        pools["bound_by_destiny"].append(" ".join(stat_lines))
    return pools


def _synthesize_forbidden_jewel_blocks() -> list[tuple[str, str]]:
    return [
        (
            "ForbiddenFlame",
            "\n".join(
                [
                    "Rarity: UNIQUE",
                    "Forbidden Flame",
                    "Crimson Jewel",
                    "Source: Drops from unique{The Searing Exarch}",
                    "Limited to: 1",
                    "Item Level: 83",
                    "Implicits: 0",
                    "Allocates <Ascendancy Notable> if you have the matching modifier on Forbidden Flesh",
                    "Corrupted",
                ]
            ),
        ),
        (
            "ForbiddenFlesh",
            "\n".join(
                [
                    "Rarity: UNIQUE",
                    "Forbidden Flesh",
                    "Cobalt Jewel",
                    "Source: Drops from unique{The Eater of Worlds}",
                    "Limited to: 1",
                    "Item Level: 83",
                    "Implicits: 0",
                    "Allocates <Ascendancy Notable> if you have the matching modifier on Forbidden Flame",
                    "Corrupted",
                ]
            ),
        ),
    ]


def _parse_generated_lua(gen_path: Path, pools: dict[str, list[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    gen_txt = gen_path.read_text(encoding="utf-8", errors="ignore")
    parser = _LuaParser(_tokenize_lua(gen_txt))
    local_tables: dict[str, Any] = {}
    local_inserts: dict[str, list[str]] = {}
    generated_vars: list[str] = []

    while parser.pos < parser.n:
        tok = parser.peek()
        if tok == ("IDENT", "local"):
            nxt = parser.peek(1)
            if nxt is not None and nxt[0] == "IDENT" and parser.peek(2) == ("OP", "=") and parser.peek(3) == ("OP", "{"):
                var_name = nxt[1]
                parser.pos += 3
                local_tables[var_name] = parser.parse_table()
                continue
        if (
            tok == ("IDENT", "table")
            and parser.peek(1) == ("OP", ".")
            and parser.peek(2) == ("IDENT", "insert")
            and parser.peek(3) == ("OP", "(")
        ):
            parser.pos += 4
            target_tok = parser.peek()
            if target_tok is not None and target_tok[0] == "IDENT":
                target_var = target_tok[1]
                if (
                    target_var == "data"
                    and parser.peek(1) == ("OP", ".")
                    and parser.peek(2) == ("IDENT", "uniques")
                    and parser.peek(3) == ("OP", ".")
                    and parser.peek(4) == ("IDENT", "generated")
                    and parser.peek(5) == ("OP", ",")
                    and parser.peek(6) == ("IDENT", "table")
                    and parser.peek(7) == ("OP", ".")
                    and parser.peek(8) == ("IDENT", "concat")
                    and parser.peek(9) == ("OP", "(")
                ):
                    src_tok = parser.peek(10)
                    if src_tok is not None and src_tok[0] == "IDENT" and src_tok[1] not in generated_vars:
                        generated_vars.append(src_tok[1])
                elif parser.peek(1) == ("OP", ",") and parser.peek(2) is not None and parser.peek(2)[0] == "STR":
                    if parser.peek(3) == ("OP", ")"):
                        local_inserts.setdefault(target_var, []).append(parser.peek(2)[1])
            continue
        parser.advance()

    charge_mod_lines: list[str] = []
    for cname in ("enduranceChargeMods", "frenzyChargeMods", "powerChargeMods"):
        tbl = local_tables.get(cname)
        if isinstance(tbl, dict):
            for tier_tbl in tbl.values():
                if isinstance(tier_tbl, dict):
                    for v in tier_tbl.values():
                        if isinstance(v, str) and v.strip():
                            charge_mod_lines.append(v.strip())
    balance_mod_lines: list[str] = []
    bot_tbl = local_tables.get("balanceOfTerrorMods")
    if isinstance(bot_tbl, dict):
        for v in bot_tbl.values():
            if isinstance(v, str) and v.strip():
                balance_mod_lines.append(v.strip())

    extra_by_var: dict[str, list[str]] = {
        "precursorsEmblem": charge_mod_lines,
        "balanceOfTerror": balance_mod_lines,
        "watchersEye": pools["watchers_eye"],
        "sublimeVision": pools["sublime_vision"],
        "voranasMarch": pools["voranas_march"],
        "boundByDestiny": pools["bound_by_destiny"],
        "megalomaniac": ["1 Added Passive Skill is <Cluster Notable>"],
        "forbiddenShako": [
            "Socketed Gems are Supported by Level (1-10) <Support Gem>",
            "Socketed Gems are Supported by Level (25-35) <Support Gem>",
        ],
        "replicaForbiddenShako": [
            "Socketed Gems are Supported by Level (1-10) <Support Gem>",
            "Socketed Gems are Supported by Level (25-35) <Support Gem>",
        ],
        "skinOfTheLords": ["Allocates a random Keystone Passive Skill"],
        "impossibleEscape": [
            "Passive Skills in radius of <Keystone> can be allocated without being connected to your tree"
        ],
        "replicaDragonfangsFlight": ["+3 to Level of all <Active Skill> Gems"],
        "pearl": [
            "+(8-10)% to all Elemental Resistances",
            "Socketed/Random Support Gem (Helmet / Gloves / Boots / Passive Tree)",
        ],
    }

    ordered_vars = list(generated_vars)
    for known in _KNOWN_GENERATED_UNIQUE_VARS:
        if known in local_tables and known not in ordered_vars:
            ordered_vars.append(known)

    for var_name in ordered_vars:
        tbl = local_tables.get(var_name)
        parts: list[str] = [str(x) for x in _as_seq(tbl) if isinstance(x, str) and x.strip()]
        parts.extend(s for s in local_inserts.get(var_name, []) if s.strip())
        parts.extend(extra_by_var.get(var_name, []))
        if parts:
            out.append((var_name, "\n".join(parts)))

    if "buildForbidden" in gen_txt:
        out.extend(_synthesize_forbidden_jewel_blocks())
    return out


def _synthesize_minimal_special_blocks(pools: dict[str, list[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if pools["watchers_eye"]:
        out.append(
            (
                "WatchersEye",
                "\n".join(
                    [
                        "Watcher's Eye",
                        "Prismatic Jewel",
                        "Limited to: 1",
                        "Implicits: 0",
                        "(4-6)% increased maximum Energy Shield",
                        "(4-6)% increased maximum Life",
                        "(4-6)% increased maximum Mana",
                        *pools["watchers_eye"],
                    ]
                ),
            )
        )
    if pools["bound_by_destiny"]:
        out.append(
            (
                "BoundByDestiny",
                "\n".join(
                    [
                        "Bound by Destiny",
                        "Prismatic Jewel",
                        "Limited to: 1",
                        "Implicits: 0",
                        *pools["bound_by_destiny"],
                    ]
                ),
            )
        )
    return out


def _extract_other_special_files(special_dir: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    handled_special = {"Generated.lua", "WatchersEye.lua", "BoundByDestiny.lua"}
    for p in sorted(special_dir.glob("*.lua")):
        if p.name in handled_special:
            continue
        blocks = _parse_lua_return(p)
        seq = [b for b in _as_seq(blocks) if isinstance(b, str) and b.strip()]
        if seq:
            for b in seq:
                out.append((p.stem, b))
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        p_sub = _LuaParser(_tokenize_lua(txt))
        while p_sub.pos < p_sub.n:
            if p_sub.peek() == ("OP", "{"):
                tbl = p_sub.parse_table()
                for b in _as_seq(tbl):
                    if isinstance(b, str) and b.strip():
                        out.append((p.stem, b))
            else:
                p_sub.advance()
    return out


def _extract_special_lua_blocks(special_dir: Path, data_dir: Path) -> list[tuple[str, str]]:
    """Safely extract unique item text blocks from all ``Data/Uniques/Special/*.lua`` files."""
    if not special_dir.is_dir():
        return []
    pools = _load_special_mod_pools(special_dir)
    gen_path = special_dir / "Generated.lua"
    out = _parse_generated_lua(gen_path, pools) if gen_path.is_file() else _synthesize_minimal_special_blocks(pools)
    out.extend(_extract_other_special_files(special_dir))
    return out
