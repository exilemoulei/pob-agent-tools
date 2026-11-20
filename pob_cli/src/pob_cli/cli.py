"""Click CLI entry point for `pob`."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import click

from . import __version__
from .cli_trade import bench_group, trade_group
from .daemon import (
    BootstrapMissingError,
    call_daemon,
    emit_stderr_update_alert,
    read_daemon_state,
    run_daemon_server,
)
from .external_data import query_meta, query_price, query_wiki
from .formatters import (
    fmt_delta,
    fmt_num,
    render_table,
    render_update_warning,
    strip_pob_escapes,
    to_json_str,
)
from .game_data import ensure_gamedata_index, query_local_data
from .importers import xml_to_share_code
from .poedb_client import query_poedb
from .updater import (
    STATE_BOOTSTRAP_MISSING,
    bootstrap_engine,
    check_and_apply_updates,
    format_stderr_update_alert,
    get_update_warning,
    load_update_state,
)


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


def _echo_stderr_utf8(text: str) -> None:
    """Write UTF-8 safely to stderr on Windows consoles."""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text, err=True)


def _daemon_call(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Call daemon; turn known failures into clean stderr messages + exit(1) instead of tracebacks.

    - BootstrapMissingError: engine not installed -> update/bootstrap guidance.
    - RuntimeError: the daemon rejected the request (e.g. "Build has no synced loadouts
      available.") -> `Error: <message>` via click, exit code 1 (arg/state error).
    """
    try:
        return call_daemon(action, params, auto_start=auto_start, timeout=timeout)
    except BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        _echo_stderr_utf8(alert)
        sys.exit(1)
    except RuntimeError as exc:
        raise click.ClickException(str(exc) or f"Daemon error during '{action}'") from exc


def _prepend_warning(body: str, res: dict[str, Any]) -> str:
    banner = render_update_warning(res.get("updateWarning"))
    return f"{banner}{body}" if banner else body


def _safe_min_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    vals: list[float] = []
    for key in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw = stats.get(key)
        if raw is None:
            continue
        try:
            vals.append(float(raw))
        except (TypeError, ValueError):
            pass
    if not vals:
        return None
    m = min(vals)
    return "inf" if math.isinf(m) and m > 0 else m


def _render_mutation_report(title: str, res: dict[str, Any]) -> str:
    lines = [f"### {title} — `{res.get('mode', '')}`"]
    before = res.get("before", {})
    after = res.get("after", {})
    p_key = after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"
    b_pts = before.get("points", {})
    a_pts = after.get("points", {})

    summary_rows = [
        [
            f"Primary DPS ({p_key})",
            fmt_num(before.get("primaryDps")),
            fmt_num(after.get("primaryDps")),
            fmt_delta(before.get("primaryDps"), after.get("primaryDps")),
        ],
        [
            "Effective HP (TotalEHP)",
            fmt_num(before.get("totalEhp")),
            fmt_num(after.get("totalEhp")),
            fmt_delta(before.get("totalEhp"), after.get("totalEhp")),
        ],
        [
            "Maximum Life",
            fmt_num(before.get("life")),
            fmt_num(after.get("life")),
            fmt_delta(before.get("life"), after.get("life")),
        ],
        [
            "Passive Points",
            f"{b_pts.get('used', 0)}/{b_pts.get('usedMax', 0)} (Asc {b_pts.get('ascUsed', 0)}/{b_pts.get('ascMax', 0)})",
            f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
            fmt_delta(b_pts.get("used", 0), a_pts.get("used", 0), " pts"),
        ],
    ]
    lines.append(render_table(["Headline Metric", "Before", "After", "Delta"], summary_rows))

    diff = res.get("diff", {})
    deltas = diff.get("deltas", [])
    if deltas:
        lines.append("\n#### Changed Stats")
        d_rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
        lines.append(render_table(["Stat", "Before", "After", "Delta"], d_rows))
    else:
        lines.append("\n_(No stat changes detected)_")

    new_w = diff.get("newWarnings", [])
    res_w = diff.get("resolvedWarnings", [])
    if new_w:
        lines.append("\n#### New Warnings")
        for w in new_w:
            lines.append(f"- [NEW WARNING] {w}")
    if res_w:
        lines.append("\n#### Resolved Warnings")
        for w in res_w:
            lines.append(f"- [RESOLVED] {w}")

    return "\n".join(lines)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="pob")
@click.option("--offline", is_flag=True, help="Skip GitHub engine update checks (sets POB_OFFLINE=1).")
def main(offline: bool) -> None:
    """Headless Path of Building (PoB) CLI for build inspection, what-if testing, and optimization."""
    if offline:
        os.environ["POB_OFFLINE"] = "1"


main.add_command(trade_group)
main.add_command(bench_group)


@main.command("_daemon", hidden=True)
@click.option("--pob-dir", type=click.Path(path_type=Path), default=None)
@click.option("--runtime-dir", type=click.Path(path_type=Path), default=None)
@click.option("--port", type=int, default=0)
@click.option("--offline", is_flag=True, default=False)
@click.option("--proxy", type=str, default=None)
@click.option("--mirror", type=str, default=None)
def cmd_internal_daemon(
    pob_dir: Path | None,
    runtime_dir: Path | None,
    port: int,
    offline: bool,
    proxy: str | None,
    mirror: str | None,
) -> None:
    """Internal entry point for spawning the background daemon (used by standalone binaries)."""
    run_daemon_server(
        pob_dir=pob_dir,
        port=port,
        runtime_dir=runtime_dir,
        offline=True if offline else None,
        proxy=proxy,
        mirror=mirror,
    )


# --- 4.1 Lifecycle, Engine Updates & Build I/O ---


@main.command("bootstrap")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--no-zh", is_flag=True, help="Skip PobTools-zh bilingual Chinese DataV2 pack.")
@click.option(
    "--from-dir",
    "from_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Offline import from an existing local PobTools directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_bootstrap(
    proxy: str | None,
    mirror: str | None,
    no_zh: bool,
    from_dir: Path | None,
    as_json: bool,
) -> None:
    """Bootstrap or offline-import the managed headless PoB engine."""
    if read_daemon_state() is not None:
        try:
            call_daemon("stop", auto_start=False)
            deadline = time.monotonic() + 2.0
            while read_daemon_state() is not None and time.monotonic() < deadline:
                time.sleep(0.1)
        except Exception:
            pass

    res = bootstrap_engine(
        proxy=proxy,
        mirror=mirror,
        include_zh=not no_zh,
        from_dir=from_dir,
    )
    warn = get_update_warning(res)
    if warn:
        res["updateWarning"] = warn
    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
    else:
        rows = [
            ["State", state_name or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["PoB Version", res.get("localPobVersion") or "-"],
            ["PobTools-zh", res.get("pobToolsVersion") or "-"],
            ["Data Pack", res.get("dataVersion") or "-"],
            ["Updated Files", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
            ["Source", res.get("source") or "github"],
        ]
        body = "### PoB Engine Bootstrap\n" + render_table(["Field", "Value"], rows)
        _echo_utf8(_prepend_warning(body, res))

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@main.command("update")
@click.option("--check", "check_only", is_flag=True, help="Check remote versions without applying updates.")
@click.option("--force", is_flag=True, help="Force full check/apply ignoring cached ETag.")
@click.option("--proxy", help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:7890).")
@click.option("--mirror", help="Optional GitHub mirror prefix URL.")
@click.option("--offline", is_flag=True, help="Skip network check and report local versions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_update(
    check_only: bool,
    force: bool,
    proxy: str | None,
    mirror: str | None,
    offline: bool,
    as_json: bool,
) -> None:
    """Check or incrementally update managed PoB & PobTools-zh engine (preserving loaded builds if daemon is alive)."""
    eff_offline = bool(offline or os.environ.get("POB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"))
    res: dict[str, Any] | None = None
    if read_daemon_state() is not None:
        try:
            res = call_daemon(
                "update",
                {
                    "check_only": check_only,
                    "force": force,
                    "proxy": proxy,
                    "mirror": mirror,
                    "offline": eff_offline,
                },
                auto_start=False,
                timeout=180.0,
            )
        except RuntimeError as exc:
            if "not running" not in str(exc).lower():
                raise

    if res is None:
        res = check_and_apply_updates(
            check_only=check_only,
            force=force,
            proxy=proxy,
            mirror=mirror,
            offline=eff_offline,
        )
        warn = get_update_warning(res)
        if warn:
            res["updateWarning"] = warn

    state_name = str(res.get("state") or "")
    if state_name == STATE_BOOTSTRAP_MISSING or state_name.startswith("degraded_"):
        emit_stderr_update_alert(res)

    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
    else:
        rows = [
            ["State", state_name or "-"],
            ["Local PoB Version", res.get("localPobVersion") or "-"],
            ["Remote PoB Version", res.get("remotePobVersion") or "-"],
            ["PobTools-zh Version", res.get("pobToolsVersion") or "-"],
            ["Data Pack Version", res.get("dataVersion") or "-"],
            ["Engine Dir", res.get("engineDir") or "-"],
            ["Update Available", res.get("updateAvailable") if res.get("updateAvailable") is not None else "-"],
            ["Files Updated", res.get("updatedFilesCount") if res.get("updatedFilesCount") is not None else "-"],
        ]
        if res.get("networkError"):
            rows.append(["Network Error", res["networkError"]])
        body = "### PoB Engine Update Status\n" + render_table(["Field", "Value"], rows)
        _echo_utf8(_prepend_warning(body, res))

    if state_name == STATE_BOOTSTRAP_MISSING:
        sys.exit(1)


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_status(as_json: bool) -> None:
    """Show daemon status, engine versions, update state, and loaded builds in main/ref slots."""
    res = _daemon_call("status")
    upd = res.get("updateStatus") or {}
    if str(upd.get("state") or "").startswith("degraded_") or res.get("updateWarning"):
        emit_stderr_update_alert(upd)

    if as_json:
        _echo_utf8(to_json_str(res))
        return

    m = res.get("main", {})
    m_info = m.get("buildInfo") or {}
    m_hello = m.get("hello") or {}
    r = res.get("ref", {})
    r_info = r.get("buildInfo") or {}

    pob_ver = upd.get("localPobVersion") or m_hello.get("pobVersion") or "-"
    tools_ver = upd.get("pobToolsVersion") or "-"
    data_ver = upd.get("dataVersion") or "-"
    upd_state = upd.get("state") or "-"

    rows = [
        [
            "main",
            "Running" if m.get("alive") else "Stopped",
            m_info.get("classNameZh") or m_info.get("className") or "-",
            m_info.get("ascendClassNameZh") or m_info.get("ascendClassName") or "-",
            m_info.get("level") or "-",
            f"{m.get('undoDepth', 0)} / {m.get('redoDepth', 0)}",
        ],
        [
            "ref",
            "Running" if r.get("alive") else "Idle (Stopped)",
            r_info.get("classNameZh") or r_info.get("className") or "-",
            r_info.get("ascendClassNameZh") or r_info.get("ascendClassName") or "-",
            r_info.get("level") or "-",
            "-",
        ],
    ]
    header = (
        f"Daemon PID: `{res.get('pid')}` | Uptime: `{res.get('uptimeSec')}s` | "
        f"PoB: `{pob_ver}` | PobTools: `{tools_ver}` | Data: `{data_ver}` | Update: `{upd_state}`\n"
    )
    body = header + render_table(["Slot", "State", "Class", "Ascendancy", "Level", "Undo/Redo"], rows)
    _echo_utf8(_prepend_warning(body, res))


@main.command("stop")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_stop(as_json: bool) -> None:
    """Stop the background PoB daemon and release headless processes."""
    try:
        res = call_daemon("stop", auto_start=False)
    except RuntimeError:
        res = {"stopped": True, "note": "Daemon was not running"}
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8("PoB daemon stopped.")


def _prepare_load_source(source: str) -> tuple[str | None, str | None]:
    """Resolve a user build source string (`@path`, `.xml` file, share-code `.txt` file, URL, or Base64 code).

    Returns `(source_str, xml_code_str)`.
    """
    raw = source.lstrip("\ufeff").strip()
    candidate_path = raw[1:].lstrip("\ufeff").strip() if raw.startswith("@") else raw
    is_local_file = False
    if len(candidate_path) <= 260 and not candidate_path.startswith(("http://", "https://")):
        try:
            p = Path(candidate_path)
            is_local_file = p.exists() and p.is_file()
        except OSError:
            is_local_file = False

    if is_local_file:
        text = Path(candidate_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff").strip()
        if text.lstrip().startswith("<"):
            return None, xml_to_share_code(text)
        return text, None

    if raw.lstrip().startswith("<"):
        return None, xml_to_share_code(raw)

    return raw, None


@main.command("load")
@click.argument("source")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main", help="Target slot (main or ref).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_load(source: str, target: str, as_json: bool) -> None:
    """Load a build from local .xml/.txt file (or @file), share URL (pobb.in/poedb/poe.ninja/...), or Base64 code."""
    params: dict[str, Any] = {"target": target}
    resolved_src, resolved_xml_code = _prepare_load_source(source)
    if resolved_xml_code is not None:
        params["xml_code"] = resolved_xml_code
    else:
        params["source"] = resolved_src

    try:
        res = _daemon_call("load", params)
    except Exception as exc:
        upd_state = load_update_state()
        if str(upd_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _echo_utf8(to_json_str(res))
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    header = res.get("buildHeader", {})
    main_sg = header.get("mainSocketGroup", {})
    main_sk = header.get("mainSkill", {})

    def _dd_label(dd: dict[str, Any]) -> str:
        lst = dd.get("list") or []
        idx = max(0, int(dd.get("index") or 1) - 1)
        if not lst or idx >= len(lst):
            return "-"
        entry = lst[idx]
        if isinstance(entry, dict):
            return strip_pob_escapes(entry.get("labelZh") or entry.get("label") or "-")
        return strip_pob_escapes(str(entry))

    rows = [
        ["Target Slot", target],
        ["Import Method", res.get("importMethod")],
        ["Class / Ascendancy", f"{info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}"],
        ["Level", info.get("level")],
        ["Passive Points", f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})"],
        ["Tree Version", info.get("treeVersion")],
        ["Main Skill Group", f"#{main_sg.get('index')} ({_dd_label(main_sg)})"],
        ["Active Skill", _dd_label(main_sk)],
    ]
    body = render_table(["Field", "Value"], rows)
    _echo_utf8(_prepend_warning(body, res))


@main.command("save")
@click.argument("xml_path", type=click.Path(dir_okay=False, writable=True))
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_save(xml_path: str, target: str, as_json: bool) -> None:
    """Save the current build to a local .xml file (written by CLI, R11)."""
    res = _daemon_call("export", {"target": target, "include_xml": True})
    out_file = Path(xml_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    xml_content = res.get("xml") or ""
    out_file.write_text(xml_content, encoding="utf-8")
    payload = {"savedPath": str(out_file), "bytes": len(xml_content.encode("utf-8")), "target": target}
    if as_json:
        _echo_utf8(to_json_str(payload))
    else:
        _echo_utf8(f"Saved `{target}` build XML to `{out_file}` ({payload['bytes']:,} bytes).")


@main.command("export-code")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_export_code(target: str, as_json: bool) -> None:
    """Export current build as a URL-safe Base64 PoB share code."""
    res = _daemon_call("export", {"target": target, "include_xml": False})
    if as_json:
        _echo_utf8(to_json_str({"target": target, "code": res.get("code")}))
    else:
        _echo_utf8(res.get("code") or "")


@main.command("switch-main")
@click.option("--spec", "spec_index", help="1-based passive tree spec index or title.")
@click.option("--skill-group", "skill_group", help="1-based skill group index or label.")
@click.option("--skill", "main_skill_index", type=int, help="1-based active skill index in group.")
@click.option("--part", "skill_part_index", type=int, help="1-based skill part index.")
@click.option("--item-set", "item_set_id", help="Item set ID or title.")
@click.option("--skill-set", "skill_set_id", help="Skill set ID or title.")
@click.option("--config-set", "config_set_id", help="Config set ID or title.")
@click.option("--loadout", "loadout_index", help="1-based synced loadout index or title.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_switch_main(
    spec_index: str | None,
    skill_group: str | None,
    main_skill_index: int | None,
    skill_part_index: int | None,
    item_set_id: str | None,
    skill_set_id: str | None,
    config_set_id: str | None,
    loadout_index: str | None,
    target: str,
    as_json: bool,
) -> None:
    """Switch active passive tree spec, main skill group/part, item/skill/config set, or loadout."""
    res = _daemon_call(
        "switch_main",
        {
            "target": target,
            "spec_index": spec_index,
            "skill_group": skill_group,
            "main_skill_index": main_skill_index,
            "skill_part_index": skill_part_index,
            "item_set_id": item_set_id,
            "skill_set_id": skill_set_id,
            "config_set_id": config_set_id,
            "loadout_index": loadout_index,
            "apply": True,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report("Switch Active Selection", res))


@main.command("undo")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_undo(target: str, as_json: bool) -> None:
    """Undo the last `--apply` mutation."""
    res = _daemon_call("undo", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    _echo_utf8(f"### Undo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)")
    _echo_utf8(render_table(["Stat", "Before Undo", "After Undo", "Delta"], rows))


@main.command("redo")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_redo(target: str, as_json: bool) -> None:
    """Redo the last undone mutation."""
    res = _daemon_call("redo", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    _echo_utf8(f"### Redo Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)")
    _echo_utf8(render_table(["Stat", "Before Redo", "After Redo", "Delta"], rows))


# --- 4.2 Build Inspection & Audit ---


@main.command("summary")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_summary(target: str, as_json: bool) -> None:
    """Display comprehensive build identity, offence, minion offence, defence, and warnings."""
    res = _daemon_call("summary", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    mg = res.get("mainGroup") or {}
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name'))} ({g.get('level')}/{g.get('quality')}%)"
        for g in mg.get("gems", [])
        if g.get("enabled", True)
    )

    lines = [
        f"### Build Summary (`{target}`): Lv.{info.get('level')} {info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` | **Ascendancy**: `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}` | **Tree**: `{info.get('treeVersion')}`",
        f"- **Main Skill Group**: `#{mg.get('index', '-')} {strip_pob_escapes(mg.get('displayLabelZh') or mg.get('displayLabel') or '-')}` (`{mg.get('slot') or 'Unassigned'}`)",
        f"- **Linked Gems**: {gems_str or '-'}",
    ]

    off = res.get("offence", {})
    crit_mult = off.get("CritMultiplier")
    crit_mult_str = f"{float(crit_mult) * 100:.0f}% (x{float(crit_mult):.2f})" if crit_mult is not None else "-"
    dot_dps = off.get("TotalDotDPS") or off.get("TotalDot")
    off_rows = [
        ["Full DPS", fmt_num(off.get("FullDPS")), "Combined DPS", fmt_num(off.get("CombinedDPS"))],
        ["Hit DPS (TotalDPS)", fmt_num(off.get("TotalDPS")), "DoT DPS (TotalDotDPS)", fmt_num(dot_dps)],
        ["Average Damage", fmt_num(off.get("AverageDamage")), "Attack/Cast Rate", f"{fmt_num(off.get('Speed'), 2)}/s"],
        ["Crit Chance", f"{fmt_num(off.get('CritChance'), 1)}%", "Crit Multiplier", crit_mult_str],
        ["Hit Chance", f"{fmt_num(off.get('HitChance'), 1)}%", "Mana Cost", fmt_num(off.get("ManaCost"))],
    ]
    lines.append("\n#### Offence")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], off_rows))

    m_off = res.get("minionOffence")
    if m_off:
        lines.append("\n#### Minion Offence")
        m_rows = [
            ["Minion Combined DPS", fmt_num(m_off.get("MinionCombinedDPS")), "Minion Hit DPS", fmt_num(m_off.get("MinionTotalDPS"))],
            ["Minion Avg Damage", fmt_num(m_off.get("MinionAverageDamage")), "Minion Speed", f"{fmt_num(m_off.get('MinionSpeed'), 2)}/s"],
            ["Minion Crit Chance", f"{fmt_num(m_off.get('MinionCritChance'), 1)}%", "Minion Life", fmt_num(m_off.get("MinionLife"))],
        ]
        for s_row in m_off.get("sidebarRows", []):
            m_rows.append([s_row["label"], s_row["value"], s_row["stat"], "-"])
        lines.append(render_table(["Minion Metric", "Value", "Detail", "Extra"], m_rows))

    dfn = res.get("defence", {})
    def_rows = [
        ["Effective HP (TotalEHP)", fmt_num(dfn.get("TotalEHP")), "Maximum Life / Unres", f"{fmt_num(dfn.get('Life'))} / {fmt_num(dfn.get('LifeUnreserved'))} ({fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"],
        ["Energy Shield / Ward", f"{fmt_num(dfn.get('EnergyShield'))} / {fmt_num(dfn.get('Ward'))}", "Maximum Mana / Unres", f"{fmt_num(dfn.get('Mana'))} / {fmt_num(dfn.get('ManaUnreserved'))} ({fmt_num(dfn.get('ManaUnreservedPercent'), 0)}%)"],
        ["Phys / Chaos Max Hit", f"{fmt_num(dfn.get('PhysicalMaximumHitTaken'))} / {fmt_num(dfn.get('ChaosMaximumHitTaken'))}", "Fire/Cold/Light Max Hit", f"{fmt_num(dfn.get('FireMaximumHitTaken'))} / {fmt_num(dfn.get('ColdMaximumHitTaken'))} / {fmt_num(dfn.get('LightningMaximumHitTaken'))}"],
        ["Fire / Cold Resist", f"{fmt_num(dfn.get('FireResist'), 0)}% (+{fmt_num(dfn.get('FireResistOverCap'), 0)}%) / {fmt_num(dfn.get('ColdResist'), 0)}% (+{fmt_num(dfn.get('ColdResistOverCap'), 0)}%)", "Light / Chaos Resist", f"{fmt_num(dfn.get('LightningResist'), 0)}% (+{fmt_num(dfn.get('LightningResistOverCap'), 0)}%) / {fmt_num(dfn.get('ChaosResist'), 0)}% (+{fmt_num(dfn.get('ChaosResistOverCap'), 0)}%)"],
        ["Armour / Evade", f"{fmt_num(dfn.get('Armour'))} / {fmt_num(dfn.get('Evasion'))}", "Block / Spell Block / Supp", f"{fmt_num(dfn.get('EffectiveBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%"],
        ["Net Life / Mana Regen", f"{fmt_num(dfn.get('NetLifeRegen'), 1)}/s / {fmt_num(dfn.get('NetManaRegen'), 1)}/s", "Str / Dex / Int", f"{fmt_num(dfn.get('Str'))} / {fmt_num(dfn.get('Dex'))} / {fmt_num(dfn.get('Int'))}"],
    ]
    lines.append("\n#### Defence & Sustain")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], def_rows))

    warnings = res.get("warnings", [])
    if warnings:
        lines.append("\n#### Warnings")
        for w in warnings:
            lines.append(f"- {w}")

    _echo_utf8(_prepend_warning("\n".join(lines), res))


@main.command("audit")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_audit(target: str, as_json: bool) -> None:
    """Run build health check and non-destructive 4-part PoB inflation ('water') audit."""
    res = _daemon_call("audit", {"target": target})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    lines = ["### Build Health & Inflation ('Water') Audit"]
    issues = res.get("healthIssues", [])
    if issues:
        lines.append("\n#### 1. Health & Hard Gaps")
        i_rows = [[x["severity"], x["category"], x["message"]] for x in issues]
        lines.append(render_table(["Severity", "Category", "Finding"], i_rows))
    else:
        lines.append("\n#### 1. Health & Hard Gaps\n- No critical resistance, attribute, or sustain gaps detected.")

    stripped = res.get("inflationStripped", {})
    lines.append("\n#### 2. Inflation ('Water') Stripped for Pinnacle Boss Baseline")
    s_rows = []
    for dim in ("config", "flasks", "skills", "party"):
        items = stripped.get(dim) or []
        s_rows.append([dim.capitalize(), len(items), "; ".join(items) if items else "_(Clean)_"])
    lines.append(render_table(["Dimension", "Count", "Stripped Items"], s_rows))

    orig = res.get("original", {})
    dew = res.get("dewateredPinnacle", {})
    lines.append("\n#### 3. Original Panel vs True Pinnacle Boss Baseline (De-watered)")
    comp_rows = [
        [
            f"Primary DPS ({orig.get('primaryDpsKey')})",
            fmt_num(orig.get("primaryDps")),
            fmt_num(dew.get("primaryDps")),
            fmt_delta(orig.get("primaryDps"), dew.get("primaryDps")),
        ],
        [
            "Effective HP (TotalEHP)",
            fmt_num(orig.get("totalEhp")),
            fmt_num(dew.get("totalEhp")),
            fmt_delta(orig.get("totalEhp"), dew.get("totalEhp")),
        ],
        [
            "Phys Max Hit",
            fmt_num(orig.get("physMaxHit")),
            fmt_num(dew.get("physMaxHit")),
            fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit")),
        ],
        [
            "Elemental Max Hit (Min)",
            fmt_num(orig.get("eleMaxHit")),
            fmt_num(dew.get("eleMaxHit")),
            fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit")),
        ],
        [
            "Chaos Max Hit",
            fmt_num(orig.get("chaosMaxHit")),
            fmt_num(dew.get("chaosMaxHit")),
            fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit")),
        ],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))
    _echo_utf8("\n".join(lines))


@main.command("items")
@click.option("--slot", help="Inspect a specific slot (e.g. 'Weapon 1', 'Amulet', 'Flask 1', or '#1' jewel socket).")
@click.option("--all", "all_items", is_flag=True, help="Fetch all equipped items and allocated jewel sockets with full raw mods.")
@click.option("--raw", is_flag=True, help="Include raw PoE item text.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_items(slot: str | None, all_items: bool, raw: bool, target: str, as_json: bool) -> None:
    """List equipped items, inspect a single slot, or dump all equipped items & jewels (`--all --raw`)."""
    res = _daemon_call(
        "items",
        {"target": target, "slot": slot, "raw": bool(raw or all_items), "allRaw": bool(all_items)},
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if slot:
        item = res.get("item")
        if not item:
            _echo_utf8(f"Slot `{res.get('slot', {}).get('name', slot)}` is empty.")
            return
        lines = [
            f"### Slot `{res['slot']['name']}`: {strip_pob_escapes(item.get('titleZh') or item.get('title') or item.get('name'))} (`{item.get('rarity')}`)",
        ]
        tip = res.get("tooltip") or {}
        for ln in tip.get("lines", []):
            txt = strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
            if txt:
                lines.append(f"  {txt}")
        if raw and res.get("raw"):
            lines.append("\n```text\n" + res["raw"].strip() + "\n```")
        _echo_utf8("\n".join(lines))
        return

    if all_items:
        slots = res.get("equippedSlots", [])
        lines = [
            f"### All Equipped Items & Allocated Jewels (`{res.get('equippedCount', 0)}` equipped / `{len(slots)}` slots)",
        ]
        for s in slots:
            slot_disp = s["slotName"]
            if s.get("socketIndex"):
                slot_disp = f"{slot_disp} (#{s['socketIndex']})"
            it = s.get("item")
            if not it:
                lines.append(f"\n#### `{slot_disp}` — _(Empty)_")
                continue
            title = strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-")
            base = strip_pob_escapes(it.get("baseNameZh") or it.get("baseName") or "")
            status = " [OFF]" if (s.get("isFlask") and not s.get("active")) else ""
            lines.append(f"\n#### `{slot_disp}`{status}: {title} — {base} (`{it.get('rarity') or '-'}`)")
            raw_txt = s.get("raw")
            if raw_txt:
                lines.append("```text\n" + str(raw_txt).strip() + "\n```")
        _echo_utf8("\n".join(lines))
        return

    rows = []
    for s in res.get("equippedSlots", []):
        it = s.get("item") or {}
        slot_disp = s["slotName"]
        if s.get("socketIndex"):
            slot_disp = f"{slot_disp} (#{s['socketIndex']})"
        state = "Active" if s.get("active") else ("Flask Off" if s.get("isFlask") else ("Inactive" if s.get("inactive") else "Equipped"))
        rows.append(
            [
                slot_disp,
                strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-"),
                it.get("baseNameZh") or it.get("baseName") or "-",
                it.get("rarity") or "-",
                state if it else "Empty",
            ]
        )
    _echo_utf8(render_table(["Slot", "Item Name", "Base", "Rarity", "Status"], rows))


@main.command("skills")
@click.option("--group", help="Inspect a specific skill group ('main', index, or label).")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skills(group: str | None, target: str, as_json: bool) -> None:
    """List all skill groups or inspect gems and per-gem DPS tooltips in a group."""
    res = _daemon_call("skills", {"target": target, "group": group})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if group is not None:
        grp = res.get("group", {})
        rows = []
        for gm in grp.get("gems", []):
            dps_info = gm.get("tooltipDps") or {}
            rows.append(
                [
                    gm.get("index"),
                    strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec")),
                    "Support" if gm.get("support") else "Active",
                    f"{gm.get('level')}/{gm.get('quality')}%",
                    "Yes" if gm.get("enabled", True) else "No",
                    f"{dps_info.get('pct', 0):+.1f}% ({fmt_num(dps_info.get('delta'))})" if dps_info.get("pct") is not None else "-",
                ]
            )
        _echo_utf8(f"### Skill Group #{grp.get('index')}: {strip_pob_escapes(grp.get('displayLabelZh') or grp.get('displayLabel'))} (Slot: `{grp.get('slot') or '-'}`)")
        _echo_utf8(render_table(["#", "Gem", "Type", "Lv/Qual", "Enabled", "Tooltip DPS Impact"], rows))
        return

    rows = []
    for g in res.get("groups", []):
        gems_summary = ", ".join(
            f"{strip_pob_escapes(gm.get('nameZh') or gm.get('name'))}{'' if gm.get('enabled', True) else '[OFF]'}"
            for gm in g.get("gems", [])
        )
        rows.append(
            [
                f"#{g['index']}{' *MAIN*' if g.get('isMain') else ''}",
                strip_pob_escapes(g.get("displayLabelZh") or g.get("displayLabel") or g.get("label")),
                g.get("slot") or "-",
                "Yes" if g.get("enabled", True) else "No",
                "Yes" if g.get("includeInFullDPS") else "No",
                gems_summary,
            ]
        )
    _echo_utf8(render_table(["Group", "Label", "Slot", "Enabled", "FullDPS", "Gems"], rows))


def _run_tree_inspect(search: str | None, target: str, as_json: bool) -> None:
    """Summarize allocated Keystones, Ascendancy, Masteries, Notables, Jewels, or search tree nodes."""
    res = _daemon_call("tree", {"target": target, "search": search})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if search:
        rows = []
        for m in res.get("matches", []):
            stats_s = "; ".join(str(s) for s in (m.get("statsZh") or m.get("stats") or [])[:2])
            rows.append(
                [
                    m.get("id"),
                    strip_pob_escapes(m.get("nameZh") or m.get("name")),
                    m.get("type") or "Normal",
                    "Yes" if m.get("allocated") else "No",
                    m.get("cost") if m.get("cost") is not None else "-",
                    stats_s[:80],
                ]
            )
        _echo_utf8(f"### Passive Tree Search: `{search}` ({res.get('totalMatches', 0)} matches)")
        _echo_utf8(render_table(["Node ID", "Name", "Type", "Allocated", "Path Cost", "Stats"], rows))
        return

    pts = res.get("points", {})
    lines = [
        f"### Passive Tree Summary: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` points | Ascendancy `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`",
    ]
    for title, key in (
        ("Keystones", "keystones"),
        ("Ascendancy Nodes", "ascendancyNodes"),
        ("Masteries", "masteries"),
        ("Notables", "notables"),
    ):
        items = res.get(key, [])
        lines.append(f"\n#### {title} ({len(items)})")
        rows = [
            [
                n["id"],
                strip_pob_escapes(n.get("nameZh") or n.get("name")),
                "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2]),
            ]
            for n in items
        ]
        lines.append(render_table(["ID", "Name", "Stats"], rows))

    _echo_utf8("\n".join(lines))


@main.command("calc")
@click.argument("keyword")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_calc(keyword: str, target: str, as_json: bool) -> None:
    """Search PoB Calcs breakdown tables and formulas by English/Chinese keyword."""
    res = _daemon_call("calc", {"target": target, "keyword": keyword})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    matches = res.get("matches", [])
    if not matches:
        _echo_utf8(f"No calculation rows matched `{keyword}`.")
        return

    lines = [f"### Calcs Breakdown for `{keyword}` ({res.get('totalMatches')} matches)"]
    for m in matches[:10]:
        cell_texts = " | ".join(c["text"] for c in m.get("cells", []) if c.get("text"))
        lines.append(
            f"\n#### [{m.get('sectionGroup')} / {m.get('subsection')}] {m.get('rowLabelZh') or m.get('rowLabel')}: `{cell_texts}`"
        )
        for c in m.get("cells", []):
            for sec in c.get("breakdown") or []:
                if sec.get("type") == "text":
                    for ln in sec.get("lines", []):
                        lines.append(f"  - {strip_pob_escapes(ln)}")
                elif sec.get("type") == "table":
                    cols = sec.get("cols", [])
                    headers = [col.get("label") or col.get("key") or "" for col in cols]
                    keys = [col.get("key") for col in cols]
                    t_rows = [[r.get(k, "") for k in keys] for r in sec.get("rows", [])]
                    if headers and t_rows:
                        lines.append(render_table(headers, t_rows))

    _echo_utf8("\n".join(lines))


# --- 4.3 What-If & Apply Mutations ---


@main.group("item")
def grp_item() -> None:
    """Evaluate, edit, search DB, or mutate equipped items, flasks, and anoints."""
    pass


@grp_item.command("try")
@click.option("--slot", help="Target equipment slot (e.g. 'Weapon 1', 'Ring 1', '#1').")
@click.option("--raw", "raw_text", help="Raw PoE item text string.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing raw PoE item text.")
@click.option("--tooltip-only", is_flag=True, help="Fast native tooltip preview only (skip snapshot secondary checks).")
@click.option("--apply", is_flag=True, help="Persist equipped item into build and push undo snapshot.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_try(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    tooltip_only: bool,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Evaluate or equip a raw PoE item with native tooltip comparison + full snapshot diff."""
    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    if not raw_text:
        raise click.UsageError("Provide either --raw \"<item_text>\" or --file <item.txt>.")

    res = _daemon_call(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": raw_text,
            "tooltip_only": tooltip_only,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    lines = [f"### Item Evaluation (Slot: `{res.get('slotName') or 'Auto'}`)"]
    slot_tips = res.get("slotTooltips") or []
    if not slot_tips and res.get("tooltip"):
        slot_tips = [res["tooltip"]]
    for st in slot_tips:
        st_slot = st.get("slotName") or res.get("slotName") or "Default"
        t_lines = st.get("lines") or []
        if t_lines:
            lines.append(f"\n#### Native PoB Tooltip (`{st_slot}`)")
            for ln in t_lines:
                txt = (
                    strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                    if isinstance(ln, dict)
                    else strip_pob_escapes(str(ln))
                )
                if txt:
                    lines.append(f"  {txt}")

    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report("Snapshot Full Simulation", res["mutation"]))
    _echo_utf8("\n".join(lines))


@grp_item.command("edit")
@click.option("--slot", required=True, help="Equipped slot to edit (e.g. 'Ring 1', 'Weapon 1', '#1').")
@click.option(
    "--replace-mod",
    "replace_mods",
    nargs=2,
    multiple=True,
    metavar="<OLD> <NEW>",
    help="Replace matching mod line(s) with NEW text (repeatable).",
)
@click.option("--add-mod", "add_mods", multiple=True, help="Append an explicit mod line (repeatable).")
@click.option("--remove-mod", "remove_mods", multiple=True, help="Remove matching mod line(s) (repeatable).")
@click.option("--apply", is_flag=True, help="Persist edited item into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_edit(
    slot: str,
    replace_mods: tuple[tuple[str, str], ...],
    add_mods: tuple[str, ...],
    remove_mods: tuple[str, ...],
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """In-place replace, add, or remove mod lines on an equipped item (0-residue what-if by default)."""
    if not replace_mods and not add_mods and not remove_mods:
        raise click.UsageError("Provide at least one of --replace-mod, --add-mod, or --remove-mod.")

    res = _daemon_call(
        "item_edit",
        {
            "target": target,
            "slot": slot,
            "replace_mods": [list(p) for p in replace_mods],
            "add_mods": list(add_mods),
            "remove_mods": list(remove_mods),
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    lines = [f"### Item Mod Edit (`{res.get('slotName')}`)"]
    changes = res.get("changes") or []
    if changes:
        c_rows = [
            [
                c.get("action"),
                c.get("section") or "-",
                c.get("before") or "-",
                c.get("after") or "-",
            ]
            for c in changes
        ]
        lines.append(render_table(["Action", "Section", "Before", "After"], c_rows))
    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report(f"Item Edit Impact ({res.get('slotName')})", res["mutation"]))
    _echo_utf8("\n".join(lines))


@grp_item.command("db")
@click.argument("query")
@click.option(
    "--type",
    "db_type",
    type=click.Choice(["UNIQUE", "RARE"], case_sensitive=False),
    default="UNIQUE",
    show_default=True,
    help="PoB item database kind.",
)
@click.option("--slot", help="Target equipment slot when equipping/comparing.")
@click.option("--index", "item_index", type=int, help="1-based match index to select when equipping.")
@click.option("--equip", is_flag=True, help="Try equipping selected DB item (what-if unless --apply).")
@click.option("--apply", is_flag=True, help="Persist equipped DB item into build (implies --equip).")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum matches to return.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_db(
    query: str,
    db_type: str,
    slot: str | None,
    item_index: int | None,
    equip: bool,
    apply: bool,
    limit: int,
    target: str,
    as_json: bool,
) -> None:
    """Search PoB's built-in Unique/Rare item database (EN/ZH) and optionally try/equip a match."""
    res = _daemon_call(
        "item_db",
        {
            "target": target,
            "query": query,
            "db_type": db_type.upper(),
            "slot": slot,
            "index": item_index,
            "equip": bool(equip or apply),
            "apply": apply,
            "limit": limit,
        },
        timeout=60.0,
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    if not res.get("equipped"):
        rows = []
        for m in res.get("matches", []):
            title = strip_pob_escapes(m.get("titleZh") or m.get("title") or m.get("nameZh") or m.get("name") or "-")
            base = strip_pob_escapes(m.get("baseNameZh") or m.get("baseName") or m.get("type") or "-")
            rows.append([m.get("index"), title, base, m.get("primarySlot") or "-", m.get("league") or "-"])
        _echo_utf8(f"### PoB Item DB Search (`{res.get('dbType')}`): `{query}` ({res.get('total', 0)} hits)")
        _echo_utf8(render_table(["#", "Title", "Base", "Primary Slot", "League"], rows))
        return

    sel = res.get("selectedItem") or {}
    sel_name = strip_pob_escapes(sel.get("titleZh") or sel.get("title") or sel.get("name") or query)
    lines = [f"### Item DB Equip: {sel_name} (Slot: `{res.get('slotName') or 'Auto'}`)"]
    tip = res.get("tooltip") or {}
    t_lines = tip.get("lines") or []
    if t_lines:
        lines.append("\n#### Native PoB Tooltip")
        for ln in t_lines:
            txt = (
                strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                if isinstance(ln, dict)
                else strip_pob_escapes(str(ln))
            )
            if txt:
                lines.append(f"  {txt}")
    if res.get("mutation"):
        lines.append("\n" + _render_mutation_report(f"Item DB Simulation ({sel_name})", res["mutation"]))
    _echo_utf8("\n".join(lines))


@grp_item.command("flask")
@click.argument("index", type=str)
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_flask(index: str, state: str, apply: bool, target: str, as_json: bool) -> None:
    """Toggle Flask 1..5 or `all` on or off."""
    res = _daemon_call(
        "item_flask",
        {
            "target": target,
            "flask_index": index,
            "active": state.lower() == "on",
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Flask {index} -> {state.upper()}", res))


@grp_item.command("anoint")
@click.option("--slot", default="Amulet", show_default=True, help="Equipment slot to anoint.")
@click.option("--notable", required=True, help="Notable passive name (Chinese or English).")
@click.option("--index", "anoint_idx", type=int, default=1, show_default=True, help="Anoint slot index (1..4).")
@click.option("--apply", is_flag=True, help="Persist anoint into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_item_anoint(
    slot: str,
    notable: str,
    anoint_idx: int,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Anoint an equipped item via PoB's native item_edit_popup workflow."""
    res = _daemon_call(
        "item_anoint",
        {
            "target": target,
            "slot": slot,
            "notable": notable,
            "index": anoint_idx,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        notable_obj = res.get("detail", {}).get("notable", {})
        lbl = strip_pob_escapes(notable_obj.get("labelZh") or notable_obj.get("label") or notable)
        _echo_utf8(_render_mutation_report(f"Anoint {slot} (#{anoint_idx}) -> {lbl}", res))


@main.group("skill")
def grp_skill() -> None:
    """Modify skill groups and support/active gems."""
    pass


@grp_skill.command("set")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", help="Gem 1-based index or name within group.")
@click.option("--new-gem", "--name", "new_gem", help="Replace gem with a new gem name (English or Chinese).")
@click.option("--level", type=int, help="Gem level.")
@click.option("--quality", type=int, help="Gem quality.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable gem or group.")
@click.option("--count", type=int, help="Skill/minion count.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_set(
    group: str,
    gem: str | None,
    new_gem: str | None,
    level: int | None,
    quality: int | None,
    enabled: str | None,
    count: int | None,
    full_dps: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Modify gem level/quality/enabled/count/gemId or group FullDPS state."""
    res = _daemon_call(
        "skill_set",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "new_gem": new_gem,
            "level": level,
            "quality": quality,
            "enabled": (enabled.lower() == "true") if enabled is not None else None,
            "count": count,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Skill Set (Group: {group}, Gem: {gem or 'Group'})", res))


@grp_skill.command("add")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", required=True, help="Gem name (English or Chinese).")
@click.option("--level", type=int, default=20, show_default=True)
@click.option("--quality", type=int, default=20, show_default=True)
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_add(
    group: str,
    gem: str,
    level: int,
    quality: int,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Add a gem to a skill group."""
    res = _daemon_call(
        "skill_add",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "level": level,
            "quality": quality,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Add Gem '{gem}' ({level}/{quality}%) to Group '{group}'", res))


@grp_skill.command("rm")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", required=True, help="Gem 1-based index or name to remove.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_skill_rm(group: str, gem: str, apply: bool, target: str, as_json: bool) -> None:
    """Remove a gem from a skill group."""
    res = _daemon_call(
        "skill_rm",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Remove Gem '{gem}' from Group '{group}'", res))


@main.group("tree", invoke_without_command=True)
@click.option("--search", help="Search passive tree nodes by English/Chinese name, stat, ID, or '#1' jewel socket.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.pass_context
def grp_tree(ctx: click.Context, search: str | None, target: str, as_json: bool) -> None:
    """Inspect passive tree (`pob tree [--search ...]`), mutate nodes (`change`), or switch class/ascendancy (`set-class`)."""
    if ctx.invoked_subcommand is None:
        _run_tree_inspect(search=search, target=target, as_json=as_json)


@grp_tree.command("change")
@click.option("--allocate", help="Comma-separated node IDs, names, or Mastery `<nodeId>:<effectId>` to allocate.")
@click.option("--deallocate", help="Comma-separated node IDs or names to deallocate.")
@click.option("--apply", is_flag=True, help="Persist passive tree changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_tree_change(
    allocate: str | None,
    deallocate: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Allocate or deallocate passive tree nodes idempotently (R12)."""
    alloc_list = [x.strip() for x in allocate.split(",") if x.strip()] if allocate else []
    dealloc_list = [x.strip() for x in deallocate.split(",") if x.strip()] if deallocate else []
    res = _daemon_call(
        "tree_change",
        {
            "target": target,
            "allocate": alloc_list,
            "deallocate": dealloc_list,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    _echo_utf8(_render_mutation_report("Passive Tree Change", res))


@grp_tree.command("set-class")
@click.argument("class_name", required=False)
@click.argument("ascendancy", required=False)
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name (or 'None' to clear).")
@click.option("--apply", is_flag=True, help="Persist class/ascendancy change into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_tree_set_class(
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Switch character class, primary ascendancy, and/or secondary (Bloodline) ascendancy."""
    if not class_name and not ascendancy and not secondary_asc:
        raise click.UsageError("Specify class_name, ascendancy, and/or --secondary-asc.")

    res = _daemon_call(
        "tree_set_class",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy_name": ascendancy,
            "secondary_ascendancy_name": secondary_asc,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return
    detail = res.get("detail") or {}
    changes = detail.get("changes") or []
    title = f"Set Class / Ascendancy ({'; '.join(changes) or 'no-op'})"
    _echo_utf8(_render_mutation_report(title, res))


@main.group("config")
def grp_config() -> None:
    """Inspect or modify PoB ConfigTab parameters and customMods."""
    pass


@grp_config.command("list")
@click.option("--all", "include_defaults", is_flag=True, help="Include default/inactive config variables.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_config_list(include_defaults: bool, target: str, as_json: bool) -> None:
    """List active (non-default) ConfigTab variables and raw customMods."""
    res = _daemon_call("config_list", {"target": target, "includeDefaults": include_defaults})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    active_set = res.get("activeConfigSet") or {}
    set_title = active_set.get("title") or res.get("activeConfigSetId") or "Default"
    lines = [
        f"### ConfigTab (`{set_title}`) — `{res.get('activeCount', 0)}` active / `{res.get('totalCount', 0)}` total",
    ]
    rows = [
        [
            v.get("sectionZh") or v.get("section") or "-",
            v.get("var") or "-",
            fmt_num(v.get("value")),
            v.get("labelZh") or v.get("label") or "-",
        ]
        for v in res.get("variables", [])
    ]
    lines.append(render_table(["Section", "Var", "Value", "Label"], rows))

    cm_text = (res.get("customModsText") or "").strip()
    if cm_text:
        lines.append("\n#### Custom Modifiers\n```text\n" + cm_text + "\n```")
    _echo_utf8("\n".join(lines))


@grp_config.command("set")
@click.argument("pairs", nargs=-1, required=False)
@click.option("--custom-mods", "custom_mods", help="Replace custom modifiers text (or 'clear' to disable).")
@click.option("--append-mods", "append_mods", help="Append custom modifier line(s) without overwriting existing customMods.")
@click.option("--apply", is_flag=True, help="Persist config changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_config_set(
    pairs: tuple[str, ...],
    custom_mods: str | None,
    append_mods: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Set/reset ConfigTab variables (`key=value` or `key=reset`), replace `--custom-mods`, or `--append-mods`."""
    if not pairs and custom_mods is None and append_mods is None:
        raise click.UsageError("Provide at least one `key=value` assignment, `--custom-mods`, or `--append-mods`.")
    assignments: list[list[str]] = []
    for p in pairs:
        if "=" not in p:
            raise click.UsageError(f"Invalid config assignment '{p}'; expected key=value.")
        k, v = p.split("=", 1)
        assignments.append([k.strip(), v.strip()])

    res = _daemon_call(
        "config_set",
        {
            "target": target,
            "assignments": assignments,
            "custom_mods": custom_mods,
            "appendCustomMods": append_mods,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        desc_parts = list(pairs)
        if custom_mods is not None:
            desc_parts.append(f"customMods={custom_mods!r}")
        if append_mods is not None:
            desc_parts.append(f"appendMods={append_mods!r}")
        _echo_utf8(_render_mutation_report(f"Config Set ({', '.join(desc_parts)})", res))


@main.command("batch")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="JSON file containing batch operations list.")
@click.option("--json-ops", "json_ops", help="Inline JSON array of batch operations.")
@click.option("--apply", is_flag=True, help="Persist batch changes into build.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_batch(
    file_path: str | None,
    json_ops: str | None,
    apply: bool,
    target: str,
    as_json: bool,
) -> None:
    """Execute multiple item/skill/tree/config mutations in a single snapshot transaction."""
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    elif json_ops:
        raw = json_ops
    else:
        raise click.UsageError("Provide either --file <ops.json> or --json-ops '<json>'.")

    parsed = json.loads(raw)
    ops = parsed.get("ops") if isinstance(parsed, dict) else parsed
    if not isinstance(ops, list):
        raise click.UsageError("Batch input must be a JSON list of operation objects (or `{\"ops\": [...]}`).")

    res = _daemon_call("batch", {"target": target, "ops": ops, "apply": apply})
    if as_json:
        _echo_utf8(to_json_str(res))
    else:
        _echo_utf8(_render_mutation_report(f"Batch Execution ({len(ops)} steps)", res))


@main.command("matrix")
@click.argument("scenarios", nargs=-1, required=False)
@click.option(
    "-s",
    "--scenario",
    "opt_scenarios",
    multiple=True,
    help="Scenario spec (e.g. Original, Pinnacle, Uber, FlasksOff, 'Wither15:Pinnacle,MultiplierWitheredStackCount=15').",
)
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_matrix(
    scenarios: tuple[str, ...],
    opt_scenarios: tuple[str, ...],
    target: str,
    as_json: bool,
) -> None:
    """Run multi-scenario what-if comparison in one atomic 0-residue transaction (default: Original/Pinnacle/Uber/FlasksOff)."""
    all_specs = [*scenarios, *opt_scenarios] or None
    res = _daemon_call("matrix", {"target": target, "scenarios": all_specs})
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    sc_list = res.get("scenarios", [])
    if not sc_list:
        _echo_utf8("_(No matrix scenarios evaluated)_")
        return

    base_dig = sc_list[0].get("digest", {})
    base_dps = base_dig.get("primaryDps")
    base_ehp = base_dig.get("totalEhp")
    p_key = base_dig.get("primaryDpsKey") or "CombinedDPS"

    rows = []
    for idx, sc in enumerate(sc_list):
        dig = sc.get("digest", {})
        st = dig.get("stats", {})
        p_dps = dig.get("primaryDps")
        ehp = dig.get("totalEhp")
        dps_delta = "Baseline" if idx == 0 else fmt_delta(base_dps, p_dps)
        ehp_delta = "Baseline" if idx == 0 else fmt_delta(base_ehp, ehp)
        ele_min = _safe_min_ele_max_hit(st)
        max_hit_str = (
            f"{fmt_num(st.get('PhysicalMaximumHitTaken'))} / "
            f"{fmt_num(ele_min)} / "
            f"{fmt_num(st.get('ChaosMaximumHitTaken'))}"
        )
        rows.append(
            [
                sc.get("name") or "-",
                fmt_num(p_dps),
                dps_delta,
                fmt_num(st.get("AverageDamage")),
                f"{fmt_num(st.get('Speed'), 2)}/s" if st.get("Speed") is not None else "-",
                fmt_num(ehp),
                ehp_delta,
                f"{fmt_num(st.get('Life'))} / {fmt_num(st.get('EnergyShield'))}",
                max_hit_str,
            ]
        )

    _echo_utf8(f"### Scenario Matrix Comparison (`{target}`, Baseline: `{res.get('baseline')}`, Primary: `{p_key}`)")
    _echo_utf8(
        render_table(
            ["Scenario", p_key, "DPS Delta", "Avg Hit", "Speed", "TotalEHP", "EHP Delta", "Life / ES", "MaxHit (P/E/C)"],
            rows,
        )
    )


# --- 4.4 ROI Rankings & Cross-Build Comparison ---


@main.command("rank-nodes")
@click.option("--stat", default="CombinedDPS", show_default=True, help="Target stat (CombinedDPS, FullDPS, TotalEHP, Life, etc.).")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance in points.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top candidates to show.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_rank_nodes(stat: str, max_dist: int, top: int, target: str, as_json: bool) -> None:
    """Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`)."""
    res = _daemon_call(
        "rank_nodes",
        {"target": target, "stat": stat, "max_dist": max_dist, "top": top},
        timeout=180.0,
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    rows = []
    for idx, r in enumerate(res.get("byPerPoint", []), start=1):
        rows.append(
            [
                idx,
                r.get("id"),
                strip_pob_escapes(r.get("nameZh") or r.get("name")),
                r.get("type"),
                r.get("pathDist"),
                strip_pob_escapes(r.get("pathPowerStr") or fmt_num(r.get("pathPower"), 3)),
                strip_pob_escapes(r.get("powerStr") or fmt_num(r.get("power"), 3)),
            ]
        )
    _echo_utf8(f"### Passive Node ROI Ranking (Stat: `{res.get('stat')}`, Max Dist: `{max_dist}` pts)")
    _echo_utf8(render_table(["#", "Node ID", "Name", "Type", "Cost", "Per-Point Gain", "Total Gain"], rows))


@main.command("rank-gems")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--slot", "slot_idx", type=int, help="1-based gem slot index to replace.")
@click.option("--add-link", is_flag=True, help="Rank as an additional support link instead of replacing a slot.")
@click.option("--field", default="CombinedDPS", show_default=True, help="DPS field to sort by (CombinedDPS, FullDPS, TotalDPS, etc.).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top gems to return.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_rank_gems(
    group: str,
    slot_idx: int | None,
    add_link: bool,
    field: str,
    top: int,
    target: str,
    as_json: bool,
) -> None:
    """Rank support/active candidate gems for a skill group by DPS gain."""
    res = _daemon_call(
        "rank_gems",
        {
            "target": target,
            "group": group,
            "slot_idx": slot_idx,
            "add_link": add_link,
            "field": field,
            "top": top,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    rows = []
    for idx, gm in enumerate(res.get("candidates", []), start=1):
        pct = gm.get("pct")
        pct_str = f"{float(pct):+.1f}%" if pct is not None else "-"
        rows.append(
            [
                idx,
                strip_pob_escapes(gm.get("nameZh") or gm.get("name")),
                strip_pob_escapes(gm.get("name")),
                fmt_num(gm.get("dps")),
                fmt_num(gm.get("delta")),
                pct_str,
            ]
        )
    _echo_utf8(
        f"### Candidate Gem Ranking — Group #{res.get('groupIndex')} `{strip_pob_escapes(res.get('groupLabel'))}` ({res.get('mode')}, Base `{res.get('dpsField')}`: `{fmt_num(res.get('baseDps'))}`)"
    )
    _echo_utf8(render_table(["#", "Gem (ZH)", "Gem (EN)", "New DPS", "Delta", "Gain %"], rows))


@main.command("compare")
@click.argument("ref_source", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_compare(ref_source: str | None, as_json: bool) -> None:
    """Compare `main` build vs `ref` build across 3 baselines + tree/skill/equipment diffs."""
    params: dict[str, Any] = {}
    if ref_source:
        resolved_src, resolved_xml_code = _prepare_load_source(ref_source)
        if resolved_xml_code is not None:
            params["ref_xml_code"] = resolved_xml_code
        else:
            params["ref_source"] = resolved_src

    res = _daemon_call("compare", params)
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    m_info = res.get("mainBuildInfo", {})
    r_info = res.get("refBuildInfo", {})
    lines = [
        f"### A/B Build Comparison: `main` (Lv.{m_info.get('level')} {m_info.get('ascendClassNameZh') or m_info.get('ascendClassName')}) vs `ref` (Lv.{r_info.get('level')} {r_info.get('ascendClassNameZh') or r_info.get('ascendClassName')})",
    ]

    baselines = res.get("baselines", {})
    for title, rows in (
        ("Baseline 1: Original Panels (As Configured)", baselines.get("original", [])),
        ("Baseline 2: External Target Environment Aligned to `main`", baselines.get("envAlignedToMain", {}).get("rows", [])),
        ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", baselines.get("pinnacleDewatered", {}).get("rows", [])),
    ):
        lines.append(f"\n#### {title}")
        t_rows = [[r["label"], r["mainStr"], r["refStr"], r["diffRefVsMainStr"]] for r in rows]
        lines.append(render_table(["Metric", "Main", "Ref", "Ref vs Main"], t_rows))

    td = res.get("treeDiff", {})
    ks = td.get("keystones", {})
    lines.append("\n#### Passive Tree Diff")
    lines.append(
        f"- **Keystones Only in Main**: {', '.join(x['name'] for x in ks.get('onlyInMain', [])) or '-'}\n"
        f"- **Keystones Only in Ref**: {', '.join(x['name'] for x in ks.get('onlyInRef', [])) or '-'}\n"
        f"- **Notables Shared**: `{td.get('notables', {}).get('sharedCount', 0)}` | **Only in Main**: `{len(td.get('notables', {}).get('onlyInMain', []))}` | **Only in Ref**: `{len(td.get('notables', {}).get('onlyInRef', []))}`"
    )

    sd = res.get("skillDiff", {})
    m_mg = sd.get("mainGroupMain") or {}
    r_mg = sd.get("refGroupMain") or {}
    m_mg_lbl = strip_pob_escapes(m_mg.get("displayLabelZh") or m_mg.get("displayLabel") or "-")
    r_mg_lbl = strip_pob_escapes(r_mg.get("displayLabelZh") or r_mg.get("displayLabel") or "-")
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **Main Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **Ref Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in Main**: {', '.join(sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in Ref**: {', '.join(sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(sd.get('sharedActiveSkills', [])) or '-'}"
    )

    lines.append("\n#### Equipment Slot Diff")
    slot_rows = [[s["slot"], s["mainItem"], s["refItem"]] for s in res.get("itemSlotDiff", [])]
    lines.append(render_table(["Slot", "Main Item", "Ref Item"], slot_rows))

    _echo_utf8("\n".join(lines))


@main.command("borrow")
@click.option("--item-slot", "item_slot", help="Comma-separated equipment slot(s) to borrow from `ref` into `main` (e.g. 'Weapon 1,Amulet').")
@click.option("--skill-group", "skill_group", help="Skill group ('main', index, or label) to borrow from `ref` into `main`.")
@click.option("--apply", is_flag=True, help="Persist borrowed items/skills into `main` build.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_borrow(item_slot: str | None, skill_group: str | None, apply: bool, as_json: bool) -> None:
    """Borrow items and/or a skill group from `ref` into `main` and compute exact delta."""
    slots = [x.strip() for x in item_slot.split(",") if x.strip()] if item_slot else []
    res = _daemon_call(
        "borrow",
        {
            "item_slots": slots,
            "skill_group": skill_group,
            "apply": apply,
        },
    )
    if as_json:
        _echo_utf8(to_json_str(res))
        return

    notices = res.get("detail", {}).get("notices", [])
    report = _render_mutation_report("Borrow from Reference Build (`ref` -> `main`)", res)
    if notices:
        notice_block = "\n#### Transplant Notices\n" + "\n".join(f"- {n}" for n in notices)
        report += notice_block
    _echo_utf8(report)


# --- 4.5 Local Offline Game Data & External Economy/PoEDB/Wiki Queries ---


@main.command("data")
@click.argument("kind", type=click.Choice(["gem", "unique", "mod", "base", "translate"], case_sensitive=False))
@click.argument("query")
@click.option("--slot", help="Filter mods/uniques/bases by slot (e.g. 'body_armour', 'helmet', 'ring', 'wand').")
@click.option("--ilvl", type=int, help="Maximum item level / required level filter.")
@click.option("--limit", type=int, default=25, show_default=True, help="Maximum number of results.")
@click.option("--rebuild", is_flag=True, help="Force rebuild the local SQLite game data index.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_data(
    kind: str,
    query: str,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    rebuild: bool,
    as_json: bool,
) -> None:
    """Query local 0ms PoB Lua game data & ZH dictionary (`gem|unique|mod|base|translate`, daemon-free)."""
    if rebuild:
        ensure_gamedata_index(force_rebuild=True)
    res = query_local_data(kind, query, slot=slot, ilvl=ilvl, limit=limit)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    kind_norm = res.get("kind", kind.lower())
    results = res.get("results", [])
    lines = [
        f"### Local Game Data (`{kind_norm}`): `{query}` — `{res.get('count', 0)}` hits "
        f"(PoB `{res.get('pobVersion')}` / Data `{res.get('dataVersion')}`)",
    ]
    if not results:
        lines.append("_(no matches)_")
        _echo_utf8("\n".join(lines))
        return

    if kind_norm == "translate":
        rows = [[r.get("en"), r.get("zh") or "-", r.get("category") or "-", r.get("match_type") or "-"] for r in results]
        lines.append(render_table(["English", "Chinese", "Category", "Match"], rows))
    elif kind_norm == "gem":
        for r in results:
            zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
            kind_lbl = "Support" if r.get("is_support") else "Active"
            lines.append(f"\n#### {r.get('display_name') or r.get('name')}{zh_part} — `{r.get('color') or '-'}` {kind_lbl}")
            meta_bits = []
            if r.get("tag_string"):
                meta_bits.append(f"Tags: {r['tag_string']}")
            if r.get("cast_time") is not None:
                meta_bits.append(f"Cast: {r['cast_time']}s")
            if r.get("crit_chance") is not None:
                meta_bits.append(f"Crit: {r['crit_chance']}%")
            if r.get("damage_effectiveness") is not None:
                meta_bits.append(f"DmgEff: {r['damage_effectiveness']}")
            if r.get("mana_multiplier") is not None:
                meta_bits.append(f"CostMult: {r['mana_multiplier']}%")
            if meta_bits:
                lines.append("- " + " | ".join(meta_bits))

            lvls = r.get("levels") or {}
            if lvls:
                lvl_rows = []
                for lk in sorted(lvls.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
                    lv = lvls[lk]
                    cost_s = ", ".join(f"{k}:{v}" for k, v in (lv.get("cost") or {}).items()) or "-"
                    stats_s = "; ".join(f"{k}={v}" for k, v in list((lv.get("stats") or {}).items())[:6]) or "-"
                    lvl_rows.append(
                        [
                            f"Lv.{lk}",
                            lv.get("levelRequirement") if lv.get("levelRequirement") is not None else "-",
                            cost_s,
                            lv.get("critChance") if lv.get("critChance") is not None else "-",
                            lv.get("damageEffectiveness") if lv.get("damageEffectiveness") is not None else "-",
                            stats_s[:100],
                        ]
                    )
                lines.append(render_table(["Lv", "ReqLv", "Cost", "Crit", "DmgEff", "Key Stats"], lvl_rows))

            q_stats = r.get("quality_stats") or []
            if q_stats:
                q_str = "; ".join(f"{q['stat']}={q['at_20_quality']} (@20%)" for q in q_stats[:4])
                lines.append(f"- **Quality (20%)**: {q_str}")
            variants = r.get("variants") or []
            if variants:
                v_parts = []
                for v in variants[:8]:
                    v_zh = f" ({v['zh_name']})" if v.get("zh_name") else ""
                    v_parts.append(f"{v.get('name')}{v_zh} [{v.get('relation')}]")
                lines.append(f"- **Variants**: {', '.join(v_parts)}")
    elif kind_norm == "unique":
        for r in results:
            zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
            base_zh = f" ({r['base_type_zh']})" if r.get("base_type_zh") else ""
            lines.append(f"\n#### {r.get('name')}{zh_part} — {r.get('base_type')}{base_zh} (`{r.get('item_class')}`)")
            meta = [x for x in [f"ReqLv {r['req_level']}" if r.get("req_level") else "", r.get("league") or "", r.get("source") or ""] if x]
            if meta:
                lines.append("- " + " | ".join(meta))
            for imp, imp_zh in zip(r.get("implicits") or [], (r.get("implicits_zh") or []) + [None] * 10):
                lines.append(f"- [Implicit] {imp}" + (f" / {imp_zh}" if imp_zh else ""))
            for exp, exp_zh in zip(r.get("explicits") or [], (r.get("explicits_zh") or []) + [None] * 30):
                lines.append(f"- {exp}" + (f" / {exp_zh}" if exp_zh else ""))
            fb_mods = r.get("foulborn_mods") or []
            if fb_mods:
                lines.append("- **Foulborn Mutations**:")
                for fb in fb_mods[:8]:
                    st_en = "; ".join(fb.get("stats") or [])
                    st_zh = "; ".join(x for x in (fb.get("stats_zh") or []) if x)
                    lines.append(f"  - {st_en}" + (f" / {st_zh}" if st_zh else ""))
    elif kind_norm == "mod":
        rows = []
        for r in results:
            en_s = "; ".join(r.get("stats") or [])
            zh_s = "; ".join(x for x in (r.get("stats_zh") or []) if x)
            stat_disp = f"{en_s} / {zh_s}" if zh_s else en_s
            weights = r.get("weights") or {}
            bench = r.get("bench_info")
            if bench:
                cost = bench.get("cost")
                cost_s = ", ".join(f"{k}:{v}" for k, v in cost.items()) if isinstance(cost, dict) else str(cost or "")
                types_s = ",".join((bench.get("types") or [])[:5])
                w_disp = f"Bench[{types_s}]" + (f" ({cost_s})" if cost_s else "")
            else:
                w_disp = ", ".join(f"{k}:{v}" for k, v in list(weights.items())[:5]) or "-"
            affix_grp = f"{r.get('affix') or '-'} ({r.get('group') or '-'})"
            rows.append(
                [
                    r.get("source") or "-",
                    r.get("generation_type") or "-",
                    affix_grp[:40],
                    r.get("level") if r.get("level") is not None else "-",
                    stat_disp[:120],
                    w_disp[:60],
                ]
            )
        lines.append(render_table(["Source", "Type", "Affix (Group)", "iLvl", "Stats", "Weights / Bench"], rows))
    elif kind_norm == "base":
        rows = []
        for r in results:
            name_disp = f"{r.get('name')} ({r['zh_name']})" if r.get("zh_name") else r.get("name")
            req = r.get("req") or {}
            req_s = f"Lv{req.get('level', 0)}/{req.get('str', 0)}/{req.get('dex', 0)}/{req.get('int', 0)}"
            props = r.get("properties") or {}
            prop_bits = []
            for pk, pv in props.items():
                if isinstance(pv, dict):
                    prop_bits.append(f"{pk}: " + ",".join(f"{k}={v}" for k, v in list(pv.items())[:4]))
            imp_s = "; ".join(r.get("implicits") or []) or "-"
            sub_part = f" ({r['sub_type']})" if r.get("sub_type") else ""
            rows.append(
                [
                    name_disp,
                    f"{r.get('type') or '-'}{sub_part}",
                    req_s,
                    "; ".join(prop_bits)[:60] or "-",
                    imp_s[:80],
                ]
            )
        lines.append(render_table(["Base", "Class", "Req (Lv/S/D/I)", "Properties", "Implicits"], rows))

    _echo_utf8("\n".join(lines))


@main.command("poedb")
@click.argument("query")
@click.option("--mods", "include_mods", is_flag=True, help="Fetch item/base mod pool with DropChance weights & fossil tags.")
@click.option("--lang", type=click.Choice(["cn", "us", "en"], case_sensitive=False), default="cn", show_default=True, help="PoEDB language.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--limit", type=int, default=60, show_default=True, help="Maximum mod pool rows to return.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_poedb(
    query: str,
    include_mods: bool,
    lang: str,
    proxy: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Query poedb.tw CN/US autocomplete index, properties, version history, and mod pool weights (cached)."""
    res = query_poedb(query, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    lines = [
        f"### PoEDB: {res.get('title')} (`{res.get('slug')}`) — {res.get('url')}",
    ]
    if res.get("category"):
        lines.append(f"- **Category**: {res['category']}")
    for prop in res.get("properties") or []:
        lines.append(f"- {prop}")
    if res.get("implicits"):
        lines.append("\n#### Implicits")
        for ln in res["implicits"]:
            lines.append(f"- {ln}")
    if res.get("explicits"):
        lines.append("\n#### Explicits")
        for ln in res["explicits"]:
            lines.append(f"- {ln}")

    vh = res.get("versionHistory") or []
    if vh:
        lines.append("\n#### Version History")
        v_rows = [[v.get("version"), "; ".join(v.get("changes") or [])[:140]] for v in vh[:10]]
        lines.append(render_table(["Version", "Changes"], v_rows))

    mods = res.get("mods")
    if mods is not None:
        pool_slug = res.get("modPoolSlug") or res.get("slug")
        total_m = res.get("modCountTotal", len(mods))
        lines.append(f"\n#### Mod Pool (`{pool_slug}` — showing {len(mods)} of {total_m} mods)")
        m_rows = []
        for m in mods:
            fam = ", ".join(m.get("family") or [])[:35] or "-"
            tags = ", ".join(m.get("fossilNo") or m.get("tags") or [])[:40] or "-"
            m_rows.append(
                [
                    m.get("side") or "-",
                    fam,
                    m.get("level") if m.get("level") is not None else "-",
                    (m.get("text") or "-")[:90],
                    m.get("dropChance") if m.get("dropChance") is not None else "-",
                    tags,
                ]
            )
        lines.append(render_table(["Side", "Family", "iLvl", "Mod Text", "Weight", "Tags (fossil_no)"], m_rows))

    _echo_utf8("\n".join(lines))


@main.command("price")
@click.argument("query")
@click.option("--league", help="League name (defaults to current primary softcore trade league).")
@click.option("--category", default="all", show_default=True, help="Category (all, unique, gem, imbuedgem, currency, card, cluster, base, flask, scarab, runegraft, wombgift, etc.).")
@click.option("--variant", help="Filter matches by variant substring (e.g. 'Necromancer', '8 passives', '21/20').")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum number of matches to return.")
@click.option("--detailed", is_flag=True, help="Use detailed stash overview API (gem level/quality, corrupted, links, cluster passives).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_price(
    query: str,
    league: str | None,
    category: str,
    variant: str | None,
    limit: int,
    detailed: bool,
    as_json: bool,
) -> None:
    """Query poe.ninja economy prices (15m local cache, 52 overview categories, optional --detailed/--variant)."""
    res = query_price(
        query,
        league=league,
        category=category,
        limit=limit,
        variant=variant,
        detailed=detailed,
    )
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    matches = res.get("matches", [])
    is_det = bool(res.get("detailed")) or any(
        m.get("gemLevel") is not None or m.get("corrupted") is not None for m in matches
    )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    det_tag = " [Detailed]" if is_det else ""
    _echo_utf8(
        f"### poe.ninja Price Search{det_tag}: `{query}` (League: `{res.get('league')}`{rate_str}, {res.get('totalMatches', 0)} matches)"
    )

    if is_det:
        rows = []
        for m in matches:
            lv = m.get("gemLevel")
            ql = m.get("gemQuality")
            lv_q = f"{lv}/{ql or 0}" if lv is not None else "-"
            corr = "Yes" if m.get("corrupted") is True else ("No" if m.get("corrupted") is False else "-")
            links = f"{m['links']}L" if m.get("links") else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or "-",
                    lv_q,
                    corr,
                    links,
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        _echo_utf8(
            render_table(
                ["Item / Currency", "Category", "Variant", "Lvl/Qual", "Corrupted", "Links", "Chaos", "Divine", "Listings"],
                rows,
            )
        )
    else:
        rows = []
        for m in matches:
            trend = m.get("trend24hPct")
            trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    trend_str,
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        _echo_utf8(
            render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows)
        )


@main.command("meta")
@click.option("--league", help="League name or slug (defaults to current primary challenge league).")
@click.option("--skill", help="Filter by main skill substring.")
@click.option("--class", "class_name", help="Filter by ascendancy/class substring.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top entries to show.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_meta(
    league: str | None,
    skill: str | None,
    class_name: str | None,
    top: int,
    as_json: bool,
) -> None:
    """Query poe.ninja league meta snapshot (total builds, ascendancy & main skill share)."""
    res = query_meta(league=league, skill=skill, class_name=class_name, top=top)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    lines = [
        f"### poe.ninja Meta Snapshot: `{res.get('league')}` — `{res.get('totalBuilds', 0):,}` total builds",
    ]
    asc_rows = [
        [
            a.get("class"),
            f"{a.get('percentage', 0):.2f}%",
            f"{a.get('estimatedBuilds', 0):,}",
            ", ".join((a.get("skills") or [])[:4]),
        ]
        for a in res.get("ascendancies", [])
    ]
    lines.append("\n#### Top Ascendancies")
    lines.append(render_table(["Ascendancy", "Share", "Est. Builds", "Top Skills"], asc_rows))

    sk_rows = [
        [
            s.get("skill"),
            f"{s.get('percentage', 0):.2f}%",
            f"{s.get('estimatedBuilds', 0):,}",
            ", ".join((s.get("classes") or [])[:4]),
        ]
        for s in res.get("topSkills", [])
    ]
    lines.append("\n#### Top Main Skills")
    lines.append(render_table(["Skill", "Share", "Est. Builds", "Top Classes"], sk_rows))

    _echo_utf8("\n".join(lines))


@main.command("wiki")
@click.argument("topic")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_wiki(topic: str, as_json: bool) -> None:
    """Query poewiki.net MediaWiki API for mechanics, uniques, and keystones (24h local cache)."""
    res = query_wiki(topic)
    if as_json:
        _echo_utf8(to_json_str(res, sanitize=False))
        return

    _echo_utf8(f"### PoE Wiki: {res.get('title')} ({res.get('url')})\n")
    _echo_utf8(res.get("summary") or "")


if __name__ == "__main__":
    main()
