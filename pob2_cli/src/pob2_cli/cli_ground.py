# SPDX-License-Identifier: GPL-3.0-only
"""CLI entry point for `<15ms` daemon-free `pob2 ground` mechanics grounding (L4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .compat import ensure_utf8
from .game_data import ground_query


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


@click.command("ground")
@click.argument("query", nargs=-1, required=True)
@click.option("--category", default=None, help="按类别过滤（combat/defence/gems/keystone/ailment/atlas/crafting）。")
@click.option("--limit", type=int, default=5, show_default=True, help="最多返回卡片数。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出机器可读 JSON。")
@click.pass_context
def cmd_ground(
    ctx: click.Context,
    query: tuple[str, ...],
    category: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """快速（<15ms，免引擎）核查 PoE2 0.5.5 机制知识卡、出处与防幻觉护栏。"""
    q_text = " ".join(query).strip()
    res = ground_query(q_text, category=category, limit=limit, emit_stderr=(not as_json))
    if as_json:
        obj = (ctx.find_root().obj or {}) if ctx else {}
        compact = bool(obj.get("compact"))
        out_path = obj.get("out")
        text = json.dumps(
            res.to_dict(),
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
        if out_path:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8", newline="\n")
            return
        _echo(text)
        return

    lines = [f"### PoE2 {res.patch} 机制核查 (`{res.query}`, {res.elapsed_ms:.2f} ms, 命中 {len(res.cards)} 条)"]
    for w in res.warnings:
        lines.append(f"> [WARN/{w['ruleId']}] {w['warning']} (Source: {w.get('sourceRef', '-')})")
    if not res.cards:
        lines.append("- 未命中已核查机制卡片；未证实规则隔离见 `pob2_cli/docs/unverified-mechanics.md`。")
    for c in res.cards:
        lines.append(f"\n#### {c.title} (`{c.slug}` · {c.category})")
        lines.append(f"- **0.5.5 现行机制**: {c.poe2_live_rule}")
        if c.formula_expr:
            lines.append(f"- **公式**: `{c.formula_expr}`")
        lines.append(f"- **旧版/一代误区**: {c.poe1_or_stale_trap}")
        lines.append(f"- **核查出处**: `{c.source_ref}` (patch `{c.patch_verified}`)")
    _echo("\n".join(lines))


__all__ = ["cmd_ground"]
