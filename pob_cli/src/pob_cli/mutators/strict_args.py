"""Strict parsers for JSON mutation arguments (batch steps and daemon RPC params) (L2).

Each raises `ValueError` (exit 1) on a wrong type instead of coercing it: `bool("false")` is True,
`int(True)` is 1, `int([2])` is a TypeError (exit 5), and iterating a string walks its characters.
"""

from __future__ import annotations

import re
from typing import Any

_TRUE_WORDS = frozenset({"true", "on", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "off", "no", "0"})
# Node ids, each optionally `id:masteryEffect`, separated by commas.
_NODE_ID_CSV_RE = re.compile(r"\d+(?::\d+)?(?:\s*,\s*\d+(?::\d+)?)+")


def parse_bool(value: Any, field: str) -> bool:
    """A JSON bool, 0/1, or one of true/false/on/off/yes/no (any case)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise ValueError(f"'{field}' must be a boolean (true/false), got {value!r}.")


def opt_bool(src: dict[str, Any], *keys: str, default: bool) -> bool:
    """The first of `keys` present in `src`, parsed strictly; `default` when none is.

    A key sent as null reads as false, as `bool(None)` always did here: `{"apply": null}` on a
    handler that applies by default must stay a what-if.
    """
    for key in keys:
        if key in src:
            return False if src[key] is None else parse_bool(src[key], key)
    return default


def parse_int(value: Any, field: str) -> int:
    """A JSON integer (or a string of one); bools, floats with a fraction, lists and objects are rejected."""
    if isinstance(value, bool):
        raise ValueError(f"'{field}' must be an integer, got boolean {value!r}.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    raise ValueError(f"'{field}' must be an integer, got {value!r}.")


def check_int_or_str(value: Any, field: str) -> None:
    """A selector that may be an index or a name: an integer or a non-empty string, never a bool/list/object."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"'{field}' must be an integer or a string, got {value!r}.")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"'{field}' must not be empty.")


def parse_token_list(value: Any, field: str) -> list[str]:
    """A list of node ids/names (a single string or integer is one token); never iterated character by character."""
    if value is None:
        return []
    if isinstance(value, str) and _NODE_ID_CSV_RE.fullmatch(value.strip()):
        # `pob tree prune` / `rank-wheels` print ids as CSV for `pob tree change`; a name is never split.
        value = [part.strip() for part in value.split(",")]
    items = [value] if isinstance(value, (str, int)) and not isinstance(value, bool) else value
    if not isinstance(items, list):
        raise ValueError(f"'{field}' must be a list of node ids or names, got {value!r}.")
    tokens: list[str] = []
    for tok in items:
        if isinstance(tok, bool) or not isinstance(tok, (str, int)):
            raise ValueError(f"'{field}' entries must be node ids or names, got {tok!r}.")
        text = str(tok).strip()
        if not text:
            raise ValueError(f"'{field}' contains an empty node token.")
        tokens.append(text)
    return tokens


def parse_replace_mods(value: Any, field: str = "replaceMods") -> list[tuple[str, str]]:
    """`[[old, new], ...]`; a malformed entry is an error, not silently dropped."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be a list of [old, new] pairs, got {value!r}.")
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2 and all(isinstance(x, str) for x in entry)):
            raise ValueError(f"'{field}' entries must be [old, new] string pairs, got {entry!r}.")
        pairs.append((entry[0], entry[1]))
    return pairs


def parse_str_list(value: Any, field: str) -> list[str] | None:
    """A list of text lines (a single string is one line, never its characters); None when absent."""
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ValueError(f"'{field}' must be a string or a list of strings, got {value!r}.")
    return list(items)


def parse_gem_list(value: Any, field: str = "gems") -> list[Any] | None:
    """A list of gem specs (`"Name[:level[:quality]]"` or an object); a single spec is a list of one."""
    if value is None:
        return None
    items = [value] if isinstance(value, (str, dict)) else value
    if not isinstance(items, list) or not all(isinstance(x, (str, dict)) for x in items):
        raise ValueError(f"'{field}' must be a list of gem names or gem objects, got {value!r}.")
    return list(items)
