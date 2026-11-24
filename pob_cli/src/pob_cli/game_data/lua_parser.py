"""Single-pass Lua lexer and recursive-descent parser for PoB data tables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Single-pass Lua lexer regex (whitespace, comments, and flat metadata tables skipped in C)
LUA_TOKEN_RE = re.compile(
    r"--\[(?P<BEQ>=*)\[[\s\S]*?\](?P=BEQ)\]|--[^\n]*|"
    r"(?:(?:statOrder|skillTypes|minionSkillTypes|notMinionStat|requireSkillTypes|addSkillTypes|excludeSkillTypes)\s*=\s*\{[^{}]*\})|"
    r"(\[(?P<LEQ>=*)\[[\s\S]*?\](?P=LEQ)\])|"
    r'("(?:\\.|[^"\\])*")|'
    r"('(?:\\.|[^'\\])*')|"
    r"(0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"([A-Za-z_][A-Za-z0-9_]*)|"
    r"(\.\.\.|\.\.|==|~=|<=|>=|[\{\}\[\]\(\)=,;:\.\+\-\*/%\^#<>])"
)

LUA_ESCAPE_RE = re.compile(r'\\(["\'\\abfnrtv0-9])')
LUA_ESCAPE_MAP: dict[str, str] = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}

MOD_TRADEHASH_STRIP_RE = re.compile(r",\s*tradeHashes\s*=\s*\{[^\n]*\}")
SKILL_HIGH_LEVEL_STRIP_RE = re.compile(
    r"^\t\t\[(?:[89]|1\d|2[2-9]|[34]\d)\]\s*=\s*\{[^\n]*\},\r?\n", re.MULTILINE
)


def _unescape_lua_quoted(raw: str) -> str:
    body = raw[1:-1]
    if "\\" not in body:
        return body

    def _repl(match: re.Match[str]) -> str:
        ch = match.group(1)
        if ch in LUA_ESCAPE_MAP:
            return LUA_ESCAPE_MAP[ch]
        if ch.isdigit():
            return chr(int(ch))
        return ch

    return LUA_ESCAPE_RE.sub(_repl, body)


def _strip_long_bracket(raw: str) -> str:
    eq_end = raw.index("[", 1)
    content = raw[eq_end + 1 : -(eq_end + 1)]
    if content.startswith("\r\n"):
        content = content[2:]
    elif content.startswith("\n"):
        content = content[1:]
    return content


def _tokenize_lua(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    append = tokens.append
    for m in LUA_TOKEN_RE.finditer(text):
        idx = m.lastindex
        if idx is None:
            continue
        val = m.group(idx)
        if idx == 8:
            append(("OP", val))
        elif idx == 7:
            if val in ("true", "false", "nil"):
                append(("BOOL_NIL", val))
            else:
                append(("IDENT", val))
        elif idx == 4 or idx == 5:
            append(("STR", _unescape_lua_quoted(val)))
        elif idx == 6:
            append(("NUM", val))
        elif idx == 2:
            append(("STR", _strip_long_bracket(val)))
    return tokens


def _parse_num(s: str) -> int | float:
    if s.startswith(("0x", "0X")):
        return int(s, 16)
    if "." in s or "e" in s or "E" in s:
        return float(s)
    return int(s)


class _LuaParser:
    """Recursive-descent parser over Lua data tables and top-level table assignments."""

    def __init__(
        self,
        tokens: list[tuple[str, str]],
        *,
        skip_keys: frozenset[str] = frozenset(),
        keep_levels: frozenset[int] | None = None,
    ) -> None:
        self.tokens = tokens
        self.n = len(tokens)
        self.pos = 0
        self.skip_keys = skip_keys
        self.keep_levels = keep_levels

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        idx = self.pos + offset
        if 0 <= idx < self.n:
            return self.tokens[idx]
        return None

    def advance(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _skip_balanced(self, open_tok: str, close_tok: str) -> None:
        depth = 0
        while self.pos < self.n:
            _, val = self.advance()
            if val == open_tok:
                depth += 1
            elif val == close_tok:
                depth -= 1
                if depth <= 0:
                    return

    def _skip_function_block(self) -> None:
        # Called positioned on 'function' keyword; in Lua, for/while always pair with 'do'.
        self.advance()
        depth = 1
        while self.pos < self.n:
            kind, val = self.advance()
            if kind == "IDENT":
                if val in ("function", "do", "if", "repeat"):
                    depth += 1
                elif val in ("end", "until"):
                    depth -= 1
                    if depth <= 0:
                        return

    def _parse_expr(self) -> Any:
        tok = self.peek()
        if tok is None:
            return None
        kind, val = tok

        if kind == "IDENT" and val == "function":
            self._skip_function_block()
            return None

        # Unary minus / plus / not / #
        if kind == "OP" and val in ("-", "+", "#"):
            op = self.advance()[1]
            nxt = self.peek()
            if op in ("-", "+") and nxt is not None and nxt[0] == "NUM":
                num = _parse_num(self.advance()[1])
                v = -num if op == "-" else num
                return self._maybe_consume_binary_tail(v)
            sub = self._parse_expr()
            if op == "-" and isinstance(sub, (int, float)):
                return -sub
            return sub

        if kind == "IDENT" and val == "not":
            self.advance()
            self._parse_expr()
            return None

        if kind == "OP" and val == "{":
            return self.parse_table()

        if kind == "STR":
            self.advance()
            return self._maybe_consume_binary_tail(val)

        if kind == "NUM":
            self.advance()
            return self._maybe_consume_binary_tail(_parse_num(val))

        if kind == "BOOL_NIL":
            self.advance()
            if val == "true":
                return True
            if val == "false":
                return False
            return None

        if kind == "OP" and val == "(":
            self._skip_balanced("(", ")")
            return None

        if kind == "IDENT":
            parts = [self.advance()[1]]
            while self.pos < self.n:
                nxt = self.peek()
                if nxt is not None and nxt[0] == "OP" and nxt[1] in (".", ":"):
                    sep = self.advance()[1]
                    nxt2 = self.peek()
                    if nxt2 is not None and nxt2[0] == "IDENT":
                        parts.append(sep + self.advance()[1])
                    continue
                if nxt is not None and nxt[0] == "OP" and nxt[1] == "(":
                    self._skip_balanced("(", ")")
                    continue
                if nxt is not None and nxt[0] == "OP" and nxt[1] == "[":
                    self._skip_balanced("[", "]")
                    continue
                break
            expr_str = "".join(parts)
            return self._maybe_consume_binary_tail(expr_str)

        # Fallback: consume unexpected token
        self.advance()
        return None

    def _maybe_consume_binary_tail(self, left: Any) -> Any:
        while self.pos < self.n:
            nxt = self.peek()
            if nxt is None or nxt[0] != "OP" or nxt[1] in (",", "}", "]", ";"):
                break
            if nxt[1] in ("+", "-", "*", "/", "%", "^", "..", "==", "~=", "<=", ">=", "<", ">"):
                op = self.advance()[1]
                right = self._parse_expr()
                if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                    if op == "+":
                        left = left + right
                    elif op == "-":
                        left = left - right
                    elif op == "*":
                        left = left * right
                    elif op == "/" and right != 0:
                        left = left / right
                elif op == ".." and isinstance(left, str) and isinstance(right, str):
                    left = left + right
                continue
            break
        return left

    def parse_table(self, *, filter_int_keys: frozenset[int] | None = None) -> dict[Any, Any] | list[Any]:
        tokens = self.tokens
        n = self.n
        self.pos += 1  # Consume '{'
        result: dict[Any, Any] = {}
        seq_idx = 1
        has_explicit_keys = False
        skip_keys = self.skip_keys
        keep_levels = self.keep_levels

        while self.pos < n:
            tok = tokens[self.pos]
            tk0, tv0 = tok
            if tk0 == "OP":
                if tv0 == "}":
                    self.pos += 1
                    break
                if tv0 == "," or tv0 == ";":
                    self.pos += 1
                    continue
                if tv0 == "[":
                    has_explicit_keys = True
                    self.pos += 1
                    key = self._parse_expr()
                    if self.pos < n and tokens[self.pos] == ("OP", "]"):
                        self.pos += 1
                    if self.pos < n and tokens[self.pos] == ("OP", "="):
                        self.pos += 1
                    if filter_int_keys is not None and isinstance(key, int) and key not in filter_int_keys:
                        if self.pos < n and tokens[self.pos] == ("OP", "{"):
                            self._skip_balanced("{", "}")
                        else:
                            self._parse_expr()
                        continue
                    val = self._parse_expr()
                    if key is not None:
                        result[key] = val
                    continue

            if tk0 == "IDENT" and self.pos + 1 < n and tokens[self.pos + 1] == ("OP", "="):
                has_explicit_keys = True
                key = tv0
                self.pos += 2  # ident, '='
                if key in skip_keys and self.pos < n and tokens[self.pos] == ("OP", "{"):
                    self._skip_balanced("{", "}")
                    continue
                if key == "levels" and keep_levels is not None and self.pos < n and tokens[self.pos] == ("OP", "{"):
                    result[key] = self.parse_table(filter_int_keys=keep_levels)
                    continue
                result[key] = self._parse_expr()
            else:
                result[seq_idx] = self._parse_expr()
                seq_idx += 1

        if not has_explicit_keys and result:
            return [result[i] for i in range(1, seq_idx)]
        return result

    def parse_return_table(self) -> dict[Any, Any] | list[Any]:
        while self.pos < self.n:
            kind, val = self.advance()
            if kind == "IDENT" and val == "return":
                nxt = self.peek()
                if nxt is not None and nxt == ("OP", "{"):
                    return self.parse_table()
        return {}

    def parse_var_assignments(self, var_name: str) -> dict[str, dict[Any, Any]]:
        out: dict[str, dict[Any, Any]] = {}
        while self.pos < self.n:
            tok = self.peek()
            if tok is None:
                break
            if tok == ("IDENT", var_name) and self.peek(1) == ("OP", "["):
                self.advance()  # var_name
                self.advance()  # '['
                key = self._parse_expr()
                if self.peek() == ("OP", "]"):
                    self.advance()
                if self.peek() == ("OP", "="):
                    self.advance()
                val = self._parse_expr()
                if isinstance(key, str) and isinstance(val, dict):
                    out[key] = val
            else:
                self.advance()
        return out


def _parse_lua_return(
    path: Path,
    *,
    skip_keys: frozenset[str] = frozenset(),
    prestrip_re: re.Pattern[str] | None = None,
    prestrip_repl: str = "",
) -> Any:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if prestrip_re is not None:
        txt = prestrip_re.sub(prestrip_repl, txt)
    return _LuaParser(_tokenize_lua(txt), skip_keys=skip_keys).parse_return_table()


def _parse_lua_assignments(
    path: Path,
    var_name: str,
    *,
    skip_keys: frozenset[str] = frozenset(),
    keep_levels: frozenset[int] | None = None,
    prestrip_re: re.Pattern[str] | None = None,
) -> dict[str, dict[Any, Any]]:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if prestrip_re is not None:
        txt = prestrip_re.sub("", txt)
    return _LuaParser(_tokenize_lua(txt), skip_keys=skip_keys, keep_levels=keep_levels).parse_var_assignments(var_name)


def _as_seq(tbl: Any) -> list[Any]:
    if isinstance(tbl, list):
        return tbl
    if isinstance(tbl, dict):
        return [tbl[k] for k in sorted(k for k in tbl if isinstance(k, int))]
    return []
