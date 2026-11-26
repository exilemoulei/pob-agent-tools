# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI commands `pob2 fix-gaps` and `pob2 spirit` (L4)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from . import bridge_protocol, updater
from .bridge_client import BridgeWorker
from .compat import ensure_utf8
from .errors import DaemonRequestError
from .game_profile import stamp_game
from .guardrails import validate_build_xml
from .rebalance import (
    SpiritKnapsackPlan,
    build_offline_pack_catalog_options,
    solve_fix_gaps_live,
    solve_forward_spirit_knapsack,
    solve_live_spirit_deficit,
)


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _emit_json(ctx: click.Context | None, payload: dict[str, Any]) -> None:
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    compact = bool(root_obj.get("compact"))
    text = json.dumps(
        stamp_game(payload),
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )
    _echo(text)


def _resolve_engine_dir(engine_dir: Path | None) -> tuple[Path, Path | None]:
    raw_eng = str(engine_dir) if engine_dir else (os.environ.get("POB2_ENGINE_DIR", "").strip() or os.environ.get("POB2_ROOT", "").strip())
    raw_data = os.environ.get("POB2_DATA_DIR", "").strip()
    if not raw_eng:
        raise DaemonRequestError("Live PoB2 engine required: set `POB2_ENGINE_DIR` or pass `--engine-dir`.")
    eng = Path(raw_eng)
    data = Path(raw_data) if raw_data else None
    if not updater.is_valid_engine_dir(eng, data_dir=data):
        raise DaemonRequestError(f"Invalid PoB2 engine directory: {eng}")
    return eng, data


def _load_build_xml_into_worker(worker: BridgeWorker, build_xml_path: Path) -> None:
    xml_text = build_xml_path.read_text(encoding="utf-8")
    validate_build_xml(xml_text, expected_game="poe2", source_hint=str(build_xml_path))
    code = bridge_protocol.encode_xml_share_code(xml_text)
    worker.call("import_code", {"code": code, "mode": "replace"})


@click.command("fix-gaps")
@click.option(
    "--build",
    "build_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="PoB2 XML build file to audit and solve across Stages 1-5.",
)
@click.option(
    "--overcap",
    "overcap_target",
    type=int,
    default=20,
    show_default=True,
    help="Soft overcap buffer target above each element's MaxRes (never equalizes healthy overcap).",
)
@click.option(
    "--chaos-target",
    "chaos_target",
    type=int,
    default=0,
    show_default=True,
    help="Hard floor target for Chaos Resistance.",
)
@click.option(
    "--engine-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional PoB2 engine root directory (defaults to POB2_ENGINE_DIR).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_fix_gaps(
    ctx: click.Context,
    build_path: Path,
    overcap_target: int,
    chaos_target: int,
    engine_dir: Path | None,
    as_json: bool,
) -> None:
    """5-stage zero-buy PoE2 attribute, resistance cap+overcap, and Spirit deficit solver."""
    eng, data = _resolve_engine_dir(engine_dir)
    worker = BridgeWorker(slot_name="fix_gaps_cli", pob_dir=eng, data_dir=data)
    try:
        worker.start()
        _load_build_xml_into_worker(worker, build_path)
        report = solve_fix_gaps_live(
            worker,
            overcap_target=overcap_target,
            chaos_target=chaos_target,
            apply=False,
        )
    finally:
        worker.stop()

    data_out = report.to_dict()
    if as_json:
        _emit_json(ctx, data_out)
        return
    _echo(_format_fix_gaps_human(data_out))


@click.command("spirit")
@click.option(
    "--budget",
    type=int,
    default=None,
    help="Forward packing mode: total Spirit budget (e.g. --budget 220) to pack optimal buffs/supports.",
)
@click.option(
    "--build",
    "build_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional PoB2 XML build file for live CoW Spirit deficit rescue.",
)
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP).")
@click.option(
    "--engine-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional PoB2 engine root directory (defaults to POB2_ENGINE_DIR).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_spirit(
    ctx: click.Context,
    budget: int | None,
    build_path: Path | None,
    metric: str,
    engine_dir: Path | None,
    as_json: bool,
) -> None:
    """PoE2 discrete Spirit knapsack solver: rescue negative Spirit or pack optimal buffs within `--budget`."""
    if budget is not None:
        if budget <= 0:
            raise DaemonRequestError("`--budget` must be a positive integer (e.g. `--budget 220`).")
        opts = build_offline_pack_catalog_options(metric=metric)
        chosen, used = solve_forward_spirit_knapsack(opts, budget)
        plan = SpiritKnapsackPlan(
            mode="pack",
            target_spirit=budget,
            achieved_spirit=used,
            remaining_deficit=max(0, budget - used),
            selected=tuple(chosen),
            all_evaluated=tuple(opts),
        )
    elif build_path is not None:
        eng, data = _resolve_engine_dir(engine_dir)
        worker = BridgeWorker(slot_name="spirit_cli", pob_dir=eng, data_dir=data)
        try:
            worker.start()
            _load_build_xml_into_worker(worker, build_path)
            plan = solve_live_spirit_deficit(worker)
        finally:
            worker.stop()
    else:
        raise DaemonRequestError("Provide `--budget <N>` for forward Spirit packing or `--build <file.xml>` for live deficit rescue.")

    out = plan.to_dict()
    if as_json:
        _emit_json(ctx, out)
        return
    _echo(_format_spirit_human(out))


def _format_fix_gaps_human(data: dict[str, Any]) -> str:
    after_r = data["afterVerified"]["resistances"]
    after_a = data["afterVerified"]["attributes"]
    after_s = data["afterVerified"]["spirit"]
    lines = [
        f"PoE2 {data['patch']} 5-Stage Fix-Gaps | HardFloorsMet={data['allHardFloorsMet']} | "
        f"Gold={data['totalGoldCost']} | EstCost={data['totalExCost']:.1f} Ex | OvercapTarget=+{data['overcapTarget']}%",
        f"  After Attrs: Str {after_a['str']}/{after_a['reqStr']}, Dex {after_a['dex']}/{after_a['reqDex']}, Int {after_a['int']}/{after_a['reqInt']}",
        f"  After Res: Fire {after_r['fire']}/{after_r['fireMax']} (+{after_r['fireOvercap']}), "
        f"Cold {after_r['cold']}/{after_r['coldMax']} (+{after_r['coldOvercap']}), "
        f"Light {after_r['lightning']}/{after_r['lightningMax']} (+{after_r['lightningOvercap']}), Chaos {after_r['chaos']}",
        f"  After Spirit: {after_s['reserved']}/{after_s['total']} (Unreserved={after_s['unreserved']})",
    ]
    s1 = data["stages"]["stage1Attributes"]
    if s1["swapCount"]:
        lines.append(f"  [Stage 1 Gold Respec] {s1['swapCount']} swaps ({s1['goldCost']} Gold)")
    s2 = data["stages"]["stage2Runes"]
    if s2["assignmentCount"]:
        lines.append(f"  [Stage 2 Runes] {s2['assignmentCount']} socket augments (~{s2['totalCostEx']:.1f} Ex)")
    s3 = data["stages"]["stage3Catalysts"]
    if s3["actionCount"]:
        lines.append(f"  [Stage 3 Catalysts] {s3['actionCount']} jewellery upgrades (~{s3['totalCostEx']:.1f} Ex)")
    s4 = data["stages"]["stage4Spirit"]
    if s4["selected"]:
        lines.append(f"  [Stage 4 Spirit] freed {s4['achievedSpirit']} Spirit via {len(s4['selected'])} change(s)")
    s5 = data["stages"]["stage5Fallback"]
    if s5["needed"]:
        lines.append(f"  [Stage 5 Fallback] replace `{s5['targetSlot']}` for remaining hard gaps: {s5['requiredStats']}")
    return "\n".join(lines)


def _format_spirit_human(data: dict[str, Any]) -> str:
    lines = [
        f"PoE2 Spirit Knapsack ({data['mode']}) | target={data['targetSpirit']} | achieved={data['achievedSpirit']} | remaining={data['remainingDeficit']}"
    ]
    for item in data["selected"]:
        lines.append(f"  - {item['label']} (dDPS={item['deltaDps']:+.1f}, dEHP={item['deltaEhp']:+.1f})")
    return "\n".join(lines)


__all__ = [
    "cmd_fix_gaps",
    "cmd_spirit",
]
