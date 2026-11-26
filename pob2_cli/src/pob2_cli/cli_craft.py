# SPDX-License-Identifier: GPL-3.0-only
"""CLI commands for `pob2 craft <inspect|odds|plan|next>` and `pob2 item inspect` (L4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from .compat import ensure_utf8
from .craft.item_inspector import inspect_item_text
from .craft.models import RemovedLeverError, UnknownBaseError, UnknownTargetError
from .errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError

_TARGET_HELP = (
    "目标词缀（可重复指定；示例: 'T1 Spell Damage', 'T2 Cast Speed', "
    "'+30% Fire Resistance', '最大生命', 'SpellDamageOnWeapon8_'）。"
)


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _emit_json(ctx: click.Context, payload: dict[str, Any]) -> None:
    root_obj = (ctx.find_root().obj or {}) if ctx else {}
    compact = bool(root_obj.get("compact"))
    out_path = root_obj.get("out")
    text = json.dumps(
        payload,
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


def _read_item_source(item_text: str | None, file_path: str | None) -> str:
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    if item_text:
        return item_text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise click.UsageError("请提供 --text、--file 或通过 stdin 粘贴 Ctrl+C 装备文本。")


def _render_inspect_md(rep: Any) -> str:
    lines = [
        f"### 装备词缀体检 [PoE2 {rep.patch}] — {rep.item_name} ({rep.base_type}, ilvl {rep.ilvl})",
        f"- 稀有度: `{rep.rarity.value}` | 类别槽位: `{rep.base_slug}` | "
        f"前缀 `{len(rep.prefixes)}/{rep.max_prefixes}` (空 {rep.open_prefixes}) | "
        f"后缀 `{len(rep.suffixes)}/{rep.max_suffixes}` (空 {rep.open_suffixes})",
        f"- `0.5.0+ Crafted` 槽位: `{rep.crafted_mod_count}/1`"
        + (" [破裂锁死]" if rep.fractured_crafted_locked else "")
        + f" | `Desecrated` 亵渎槽位: `{rep.desecrated_mod_count}/1` | 合法性: "
        + ("PASS" if rep.is_legal_0_5_5 else "FAIL"),
    ]
    rows = []
    for side_label, mlist in (("前缀", rep.prefixes), ("后缀", rep.suffixes)):
        for m in mlist:
            tags = []
            if m.fractured:
                tags.append("破裂")
            if m.is_crafted:
                tags.append("Crafted")
            if m.is_desecrated:
                tags.append("亵渎")
            rows.append([
                side_label,
                f"T{m.display_tier}" if m.resolved else "?",
                m.required_level if m.resolved else "?",
                m.source_pool,
                ",".join(tags) or "-",
                m.weight_confidence.value,
                m.raw_text.replace("\n", " / "),
            ])
    if rows:
        from tabulate import tabulate

        lines.append("")
        lines.append(
            tabulate(
                rows,
                headers=["位", "阶", "req_lvl", "词缀池", "标记", "权重置信度", "词缀文本"],
                tablefmt="github",
            )
        )
    if rep.whittling_preview:
        lines.append(f"\n- 削切预兆（Omen of Whittling）预览: {rep.whittling_preview.summary}")
    for iss in rep.legality_issues:
        lines.append(f"- [违例] {iss}")
    for w in rep.warnings:
        lines.append(f"- [提示] {w}")
    return "\n".join(lines)


def _render_plan_md(plan: Any, *, title: str) -> str:
    from tabulate import tabulate

    div_str = f" (~{plan.expected_cost_div:.2f} Div)" if plan.expected_cost_div is not None else ""
    lines = [
        f"### {title} [PoE2 {plan.patch}] — {plan.base_type} (`{plan.base_slug}`, ilvl {plan.ilvl})",
        f"- 目标词缀: {', '.join(plan.targets)}",
        f"- 建议裁决: `{plan.recommendation}` | 权重置信度: `{plan.weight_confidence.value}`",
        f"- 动态最优期望造价: **{plan.expected_cost_ex:.1f} Ex**{div_str} | "
        f"预算分位 `P50={plan.p50_cost_ex:.1f} Ex` / `P80={plan.p80_cost_ex:.1f} Ex` / `P95={plan.p95_cost_ex:.1f} Ex` "
        f"| 重开率 `{plan.restart_rate * 100:.1f}%` (MDP {plan.mdp_states_solved} 状态 / {plan.mdp_iterations} 轮收敛)",
    ]
    if plan.policy_steps:
        rows = [
            [
                s.step_index,
                s.stage,
                s.action + (f" + {' + '.join(s.omens)}" if s.omens else ""),
                f"{s.step_cost_ex:.2f} Ex",
                f"{s.hit_probability * 100:.2f}%",
                s.goal,
                s.on_miss,
                s.weight_confidence.value,
            ]
            for s in plan.policy_steps
        ]
        lines.append("")
        lines.append(
            tabulate(
                rows,
                headers=["步", "阶段", "动作与预兆", "单步成本", "命中率", "目标", "歪词缀抢救/止损", "置信度"],
                tablefmt="github",
            )
        )
    if plan.candidate_actions:
        crows = [
            [
                a.lever + (f" + {' + '.join(a.omens)}" if a.omens else ""),
                f"{a.step_cost_ex:.2f} Ex",
                f"{a.hit_probability * 100:.2f}%",
                f"{a.expected_cost_per_hit_ex:.1f} Ex" if a.expected_cost_per_hit_ex else "-",
                f"{a.collateral_risk * 100:.1f}%",
                a.weight_confidence.value,
                a.notes,
            ]
            for a in plan.candidate_actions[:8]
        ]
        lines.append("\n#### 候选单步动作对比")
        lines.append(
            tabulate(
                crows,
                headers=["通货 + 预兆", "单次成本", "命中率", "期望成本/命中", "误伤率", "置信度", "说明"],
                tablefmt="github",
            )
        )
    for w in plan.warnings:
        lines.append(f"- [提示] {w}")
    return "\n".join(lines)


def _raise_removed_as_trap(exc: RemovedLeverError) -> None:
    raise LegacyPatchTrapError(
        trigger=exc.lever_name,
        patch_version="0.4/0.5",
        error=exc.reason,
        remediation=exc.alternatives,
        equivalent_command="pob2 craft plan --base <base> --target <mod>",
        rule_id="CRAFT_REMOVED_LEVER",
    ) from exc


def _handle_craft_error(ctx: click.Context, exc: Exception, *, as_json: bool) -> None:
    if isinstance(exc, (CrossGameHallucinationError, LegacyPatchTrapError)):
        raise exc
    if isinstance(exc, RemovedLeverError):
        _raise_removed_as_trap(exc)
    if isinstance(exc, (UnknownBaseError, UnknownTargetError)):
        _echo(str(exc), err=True)
        if as_json:
            _emit_json(ctx, exc.to_dict())
        ctx.exit(1)
    if isinstance(exc, ValueError):
        _echo(f"Error: {exc}", err=True)
        if as_json:
            _emit_json(ctx, {"game": "poe2", "ok": False, "errorKind": "invalid_input", "error": str(exc)})
        ctx.exit(1)
    raise DaemonRequestError(str(exc)) from exc


def _run_inspect_command(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    as_json: bool,
) -> None:
    """Unified execution pipeline for `craft inspect` and `item inspect` (`--text` & `--file`)."""
    raw = _read_item_source(item_text, file_path)
    try:
        rep = inspect_item_text(raw, base_override=base_override, ilvl_override=ilvl_override)
    except Exception as exc:
        _handle_craft_error(ctx, exc, as_json=as_json)
        return
    if as_json:
        _emit_json(ctx, rep.to_dict())
        return
    _echo(_render_inspect_md(rep))


@click.group("craft")
def craft_group() -> None:
    """PoE2 0.5.5 做装求解器（词缀体检、单步概率、MDP 路线规划、半成品接力）。"""


@craft_group.command("inspect")
@click.option("--text", "item_text", default=None, help="直接传入 Ctrl+C 装备文本。")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), default=None, help="从文件读取装备文本。")
@click.option("--base", "base_override", default=None, help="手动指定底材名或类别 slug。")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="手动覆盖物品等级。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出机器可读 JSON。")
@click.pass_context
def cmd_craft_inspect(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    as_json: bool,
) -> None:
    """拆解稀有/魔法装备前/后缀、词缀池来源、空槽与 0.5.5 合法性体检。"""
    _run_inspect_command(ctx, item_text, file_path, base_override, ilvl_override, as_json)


@craft_group.command("odds")
@click.option("--base", required=True, help="底材名或类别 slug（如 Attuned Wand / Vile Robe / 项链）。")
@click.option("--ilvl", type=int, default=82, show_default=True, help="物品等级。")
@click.option("--target", "targets", multiple=True, required=False, help=_TARGET_HELP)
@click.option("--outcomes", "show_outcomes", is_flag=True, default=False, help="输出下一步通货完整词缀族(ModFamily)权重与命中概率分布（无需 --target）。")
@click.option("--lever", default=None, help="指定单个通货（如 Perfect Orb of Transmutation）。")
@click.option("--omen", "omens", multiple=True, help="附加预兆（可重复指定）。")
@click.option("--text", "item_text", default=None, help="可选当前装备 Ctrl+C 文本。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="使用离线正数基准物价。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽特定通货或预兆（如 SSF）。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出 JSON。")
@click.pass_context
def cmd_craft_odds(
    ctx: click.Context,
    base: str,
    ilvl: int,
    targets: tuple[str, ...],
    show_outcomes: bool,
    lever: str | None,
    omens: tuple[str, ...],
    item_text: str | None,
    offline: bool,
    excluded: tuple[str, ...],
    as_json: bool,
) -> None:
    """计算单步通货 + 预兆精确命中率、误伤率、置信度，或完整词缀族(`--outcomes`)概率分布。"""
    from .craft import CraftMdpSolver, ModPoolEngine, fetch_live_price_book
    from .craft.pool_outcomes import (
        compute_next_step_outcomes,
        default_rarity_for_lever,
        render_outcomes_md,
    )

    eng = ModPoolEngine()
    try:
        if not targets and not show_outcomes:
            raise ValueError("Specify at least one `--target <mod>` or pass `--outcomes` for the full ModFamily probability distribution.")
        if item_text:
            rep = inspect_item_text(item_text, eng, base_override=base, ilvl_override=ilvl)
            state = rep.state
            assert state is not None
        else:
            b = eng.resolve_base(base)
            from .craft.models import CraftingState, ItemRarity

            lv_entry = eng.resolve_lever(lever) if lever else None
            def_rar = default_rarity_for_lever(lv_entry) if lv_entry is not None else ItemRarity.NORMAL
            state = CraftingState(base=b, ilvl=ilvl, rarity=def_rar)

        outcomes_payload = (
            compute_next_step_outcomes(eng, state, lever_name=lever, omens=list(omens))
            if show_outcomes
            else None
        )
        if show_outcomes and not targets:
            assert outcomes_payload is not None
            if as_json:
                _emit_json(ctx, outcomes_payload)
                return
            _echo(render_outcomes_md(outcomes_payload))
            return

        t_specs = tuple(eng.resolve_target(state.base.poe2db_slug, t, ilvl=state.ilvl) for t in targets)
        t_ids = {mid for ts in t_specs for mid in ts.matching_mod_ids}
        if lever:
            res = eng.evaluate_action(state, t_ids, lever, list(omens))
            payload: dict[str, Any] = {
                "game": "poe2",
                "patch": eng.patch,
                "base": state.base.name,
                "ilvl": state.ilvl,
                "evaluation": res.to_dict(),
            }
            if outcomes_payload is not None:
                payload["totalPoolWeight"] = outcomes_payload["totalPoolWeight"]
                payload["familyCount"] = outcomes_payload["familyCount"]
                payload["outcomes"] = outcomes_payload["outcomes"]
            if as_json:
                _emit_json(ctx, payload)
                return
            msg = (
                f"[{eng.patch}] {res.lever.name}"
                + (f" + {', '.join(o.name for o in res.omens)}" if res.omens else "")
                + f" -> 命中率 **{res.hit_probability * 100:.4f}%** ({res.target_weight}/{res.eligible_pool_weight}) | "
                + f"误伤率 `{res.collateral_remove_probability * 100:.2f}%` | 置信度 `{res.weight_confidence.value}` | {res.notes}"
            )
            if outcomes_payload is not None:
                msg += "\n\n" + render_outcomes_md(outcomes_payload)
            _echo(msg)
            return

        solver = CraftMdpSolver(eng)
        pb = fetch_live_price_book(offline=offline, excluded=excluded)
        actions = solver.evaluate_next_actions(state, t_specs, pb)
    except Exception as exc:
        _handle_craft_error(ctx, exc, as_json=as_json)
        return
    if as_json:
        out_json: dict[str, Any] = {
            "game": "poe2",
            "patch": eng.patch,
            "base": state.base.name,
            "ilvl": state.ilvl,
            "actions": [a.to_dict() for a in actions],
        }
        if outcomes_payload is not None:
            out_json["totalPoolWeight"] = outcomes_payload["totalPoolWeight"]
            out_json["familyCount"] = outcomes_payload["familyCount"]
            out_json["outcomes"] = outcomes_payload["outcomes"]
        _emit_json(ctx, out_json)
        return
    from tabulate import tabulate

    rows = [
        [a.lever + (f" + {' + '.join(a.omens)}" if a.omens else ""), f"{a.step_cost_ex:.2f}", f"{a.hit_probability * 100:.3f}%", f"{a.collateral_risk * 100:.1f}%", a.weight_confidence.value, a.notes]
        for a in actions[:15]
    ]
    tbl = tabulate(rows, headers=["通货 + 预兆", "成本(Ex)", "命中率", "误伤率", "置信度", "说明"], tablefmt="github")
    if outcomes_payload is not None:
        tbl += "\n\n" + render_outcomes_md(outcomes_payload)
    _echo(tbl)


@craft_group.command("plan")
@click.option("--base", required=True, help="底材名或类别 slug（如 Attuned Wand / Vile Robe / Breach Ring）。")
@click.option("--ilvl", type=int, default=82, show_default=True, help="物品等级。")
@click.option("--target", "targets", multiple=True, required=True, help=_TARGET_HELP)
@click.option("--base-cost", "base_cost_ex", type=float, default=2.0, show_default=True, help="白底单价(Ex)。")
@click.option("--buy-price", "buy_price_ex", type=float, default=None, help="可选市集现货价(Ex)，触发 Buy vs Craft 裁决。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽通货/预兆（适配独狼 SSF）。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="离线使用正数基准物价。")
@click.option("--sims", "n_sims", type=int, default=600, show_default=True, help="具体物品回放次数。")
@click.option("--seed", type=int, default=42, show_default=True, help="回放随机种子。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出 JSON。")
@click.pass_context
def cmd_craft_plan(
    ctx: click.Context,
    base: str,
    ilvl: int,
    targets: tuple[str, ...],
    base_cost_ex: float,
    buy_price_ex: float | None,
    excluded: tuple[str, ...],
    offline: bool,
    n_sims: int,
    seed: int,
    as_json: bool,
) -> None:
    """运行压缩状态 MDP 策略迭代 + 具体物品回放，输出分步中文剧本与 P50/P80/P95 造价。"""
    from .craft import CraftMdpSolver, fetch_live_price_book

    solver = CraftMdpSolver()
    pb = fetch_live_price_book(offline=offline, excluded=excluded)
    try:
        plan = solver.plan_craft(
            base=base, ilvl=ilvl, target_mods=list(targets), price_book=pb,
            base_cost_ex=base_cost_ex, buy_price_ex=buy_price_ex, n_sims=n_sims, seed=seed,
        )
    except Exception as exc:
        _handle_craft_error(ctx, exc, as_json=as_json)
        return
    if as_json:
        _emit_json(ctx, plan.to_dict())
        return
    _echo(_render_plan_md(plan, title="PoE2 最优制作工艺路线（MDP 动态策略）"))


@craft_group.command("next")
@click.option("--text", "item_text", default=None, help="半成品 Ctrl+C 文本。")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), default=None, help="半成品文本文件。")
@click.option("--base", "base_override", default=None, help="可选底材覆盖。")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="可选物品等级覆盖。")
@click.option("--target", "targets", multiple=True, required=True, help=_TARGET_HELP)
@click.option("--protect-existing", is_flag=True, default=False, help="锁定当前装备全部已有词缀并计算误伤率。")
@click.option("--base-cost", "base_cost_ex", type=float, default=2.0, show_default=True, help="重开白底单价(Ex)。")
@click.option("--buy-price", "buy_price_ex", type=float, default=None, help="可选成品现货价(Ex)。")
@click.option("--exclude", "excluded", multiple=True, help="屏蔽通货/预兆。")
@click.option("--offline", is_flag=True, default=True, show_default=True, help="离线使用正数基准物价。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出 JSON。")
@click.pass_context
def cmd_craft_next(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    targets: tuple[str, ...],
    protect_existing: bool,
    base_cost_ex: float,
    buy_price_ex: float | None,
    excluded: tuple[str, ...],
    offline: bool,
    as_json: bool,
) -> None:
    """半成品接力：评估当前半成品下一手最优操作、削切误伤风险与止损判定。"""
    from .craft import CraftMdpSolver, ModPoolEngine, fetch_live_price_book

    raw = _read_item_source(item_text, file_path)
    eng = ModPoolEngine()
    solver = CraftMdpSolver(eng)
    pb = fetch_live_price_book(offline=offline, excluded=excluded)
    try:
        rep = inspect_item_text(raw, eng, base_override=base_override, ilvl_override=ilvl_override)
        assert rep.state is not None
        plan = solver.plan_craft(
            base=rep.state.base, ilvl=rep.state.ilvl, target_mods=list(targets),
            starting_state=rep.state, protect_existing=protect_existing, price_book=pb,
            base_cost_ex=base_cost_ex, buy_price_ex=buy_price_ex, n_sims=400, seed=42,
        )
    except Exception as exc:
        _handle_craft_error(ctx, exc, as_json=as_json)
        return
    if as_json:
        out = plan.to_dict()
        out["inspection"] = rep.to_dict()
        _emit_json(ctx, out)
        return
    _echo(_render_inspect_md(rep) + "\n\n" + _render_plan_md(plan, title="半成品下一手接力与止损评估"))


@click.group("item")
def item_group() -> None:
    """PoE2 装备文本拆解与词缀体检命令。"""


@item_group.command("inspect")
@click.option("--text", "item_text", default=None, help="直接传入 Ctrl+C 装备文本。")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), default=None, help="从文件读取装备文本。")
@click.option("--base", "base_override", default=None, help="手动指定底材名或类别 slug。")
@click.option("--ilvl", "ilvl_override", type=int, default=None, help="手动覆盖物品等级。")
@click.option("--json", "as_json", is_flag=True, default=False, help="输出 JSON。")
@click.pass_context
def cmd_item_inspect(
    ctx: click.Context,
    item_text: str | None,
    file_path: str | None,
    base_override: str | None,
    ilvl_override: int | None,
    as_json: bool,
) -> None:
    """拆解中/繁/英 Ctrl+C 稀有装前/后缀、空槽与 0.5.5 合法性体检。"""
    _run_inspect_command(ctx, item_text, file_path, base_override, ilvl_override, as_json)


__all__ = ["craft_group", "item_group"]
