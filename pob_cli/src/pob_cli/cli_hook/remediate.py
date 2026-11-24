"""Bounded (<=2.5s) auto-remediation runner and `<pob-hook-correction>` formatter for Gate 3."""

from __future__ import annotations

import contextlib
import html as _html
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..cli_common import _prepare_load_source
from ..daemon import call_daemon, read_daemon_state
from ..formatters import fmt_num, format_diagnose_markdown
from ..game_data import ground_text_entities
from ..item_inspect import format_inspection, inspect_item
from ..ticks import build_server_tick_table
from ..updater.paths import get_shared_cache_dir

MAX_CORRECTION_CHARS = 2500
DEFAULT_BUDGET_SEC = 2.5


@contextlib.contextmanager
def _disable_receipts() -> Iterator[None]:
    prev = os.environ.get("POB_HOOK_DISABLE_RECEIPTS")
    os.environ["POB_HOOK_DISABLE_RECEIPTS"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("POB_HOOK_DISABLE_RECEIPTS", None)
        else:
            os.environ["POB_HOOK_DISABLE_RECEIPTS"] = prev


def _detect_trigger_base_cd(claims: list[str], fallback_text: str) -> tuple[float, str]:
    blob = (" ".join(claims) + " " + fallback_text).lower()
    if any(k in blob for k in ("sword of ages", "万古之剑", "soa")):
        return 0.50, "Sword of Ages (0.50s)"
    if any(k in blob for k in ("cospri", "冰剑", "mjolner", "mjölner", "雷锤", "poet", "诗人", "cwdt", "受伤时施放")):
        return 0.25, "0.25s Trigger (Cospri/Mjolner/Poet/CWDT)"
    return 0.15, "Cast On Critical Strike (0.15s)"


def _remediate_v1(v1_issues: list[Any], *, runtime_dir: Path | None) -> str:
    lines: list[str] = []
    terms: list[str] = []
    seen_terms: set[str] = set()
    for iss in v1_issues[:5]:
        lines.append(f"- [{iss.rule_id}] `{iss.trigger}`: {iss.reason} -> {iss.remediation}")
        for term in (iss.ground_terms or (iss.trigger,)):
            t = str(term or "").strip()
            if t and t.lower() not in seen_terms:
                seen_terms.add(t.lower())
                terms.append(t)
    if terms:
        try:
            grounded = ground_text_entities(" ".join(terms[:8]), limit=4, runtime_dir=runtime_dir)
            block = str(grounded.get("contextBlock") or "").strip()
            if block:
                lines.append(block)
        except Exception:
            pass
    return "\n".join(lines).strip()


def _format_compact_summary(res: dict[str, Any], slot: str) -> str:
    info = res.get("buildInfo") or {}
    off = res.get("offence") or {}
    dfn = res.get("defence") or {}
    rev = res.get("rev")
    dps = off.get("FullDPS") or off.get("CombinedDPS") or off.get("TotalDPS")
    asc = info.get("ascendClassNameZh") or info.get("ascendClassName") or info.get("className") or "-"
    rev_str = f" | rev={rev}" if rev is not None else ""
    return (
        f"[Summary `{slot}`{rev_str}] Lv.{info.get('level', '-')} {asc}\n"
        f"- Offence: Full/Combined DPS={fmt_num(dps)} | Hit={fmt_num(off.get('TotalDPS'))} | "
        f"DoT={fmt_num(off.get('TotalDotDPS') or off.get('TotalDot'))} | Speed={fmt_num(off.get('Speed'), 2)}/s\n"
        f"- Defence: TotalEHP={fmt_num(dfn.get('TotalEHP'))} | Life={fmt_num(dfn.get('Life'))} | "
        f"ES={fmt_num(dfn.get('EnergyShield'))} | PhysMaxHit={fmt_num(dfn.get('PhysicalMaximumHitTaken'))} | "
        f"Res F/C/L/Ch={fmt_num(dfn.get('FireResist'), 0)}/{fmt_num(dfn.get('ColdResist'), 0)}/"
        f"{fmt_num(dfn.get('LightningResist'), 0)}/{fmt_num(dfn.get('ChaosResist'), 0)}%"
    )


def _is_local_build_hint(hint: str | None) -> bool:
    if not hint:
        return False
    s = hint.strip()
    if s.startswith("@"):
        s = s[1:].strip()
    if not s or s.startswith(("http://", "https://")) or len(s) > 260:
        return False
    try:
        return Path(s).is_file()
    except OSError:
        return False


def _remediate_v2(
    v2_info: dict[str, Any],
    *,
    runtime_dir: Path | None,
    deadline: float,
) -> str:
    target_slot = str(v2_info.get("targetSlot") or "main")
    source_hint = v2_info.get("buildSourceHint")
    reason_kind = str(v2_info.get("reasonKind") or "missing_compute")
    remaining = deadline - time.monotonic()

    if read_daemon_state(runtime_dir) is not None and remaining > 0.25:
        try:
            if reason_kind == "missing_compute" and _is_local_build_hint(source_hint):
                src, xml_code = _prepare_load_source(str(source_hint))
                params: dict[str, Any] = {"target": target_slot, "no_rank": True}
                if xml_code is not None:
                    params["xml_code"] = xml_code
                else:
                    params["source"] = src
                res = call_daemon(
                    "diagnose",
                    params,
                    auto_start=False,
                    timeout=min(2.2, remaining),
                    runtime_dir=runtime_dir,
                    emit_cold_start_alert=False,
                )
                return format_diagnose_markdown(res)[:1200]
            if reason_kind == "missing_compute" and source_hint and not _is_local_build_hint(source_hint):
                # Never query an existing/unrelated daemon slot when the user supplied a remote URL not yet loaded.
                return (
                    f"目标构筑链接（`{source_hint}`）尚未在槽位 `{target_slot}` 加载成功；"
                    f"请先执行 `pob diagnose \"{source_hint}\" --target {target_slot}` 或 `pob profile` 核验，"
                    "切勿借用其他槽位或凭空估算 DPS/EHP。"
                )
            res = call_daemon(
                "summary",
                {"target": target_slot},
                auto_start=False,
                timeout=min(1.5, remaining),
                runtime_dir=runtime_dir,
                emit_cold_start_alert=False,
            )
            return _format_compact_summary(res, target_slot)
        except Exception:
            pass

    if reason_kind == "stale_rev":
        return f"当前 daemon 未响应或超时，请先执行 `pob summary --target {target_slot}` 获取 `--apply` 后的最新面板真值再重写结论。"
    src_cmd = f"pob diagnose {source_hint}" if source_hint else "pob diagnose <build_source>"
    return f"当前 daemon 未运行或超时，请先执行 `{src_cmd}` 获取 PoB 引擎实算面板后再给出数字结论，严禁凭空估算 DPS/EHP。"


def _remediate_v3(
    missing_slots: list[str],
    *,
    target_slot: str,
    runtime_dir: Path | None,
    deadline: float,
) -> str:
    blocks: list[str] = []
    daemon_up = read_daemon_state(runtime_dir) is not None
    for eq_slot in missing_slots[:3]:
        remaining = deadline - time.monotonic()
        inspected_md = ""
        if daemon_up and remaining > 0.2:
            try:
                loaded = call_daemon(
                    "items",
                    {"target": target_slot, "slot": eq_slot, "raw": True},
                    auto_start=False,
                    timeout=min(1.2, remaining),
                    runtime_dir=runtime_dir,
                    emit_cold_start_alert=False,
                )
                raw_text = loaded.get("raw") if isinstance(loaded, dict) else None
                if isinstance(raw_text, str) and raw_text.strip():
                    slot_name = (
                        (loaded.get("slot") or {}).get("name")
                        if isinstance(loaded.get("slot"), dict)
                        else None
                    ) or eq_slot
                    report = inspect_item(raw_text, slot=slot_name, limit=4, runtime_dir=runtime_dir)
                    inspected_md = format_inspection(report)
            except Exception:
                inspected_md = ""
        if inspected_md:
            blocks.append(inspected_md[:700])
        else:
            blocks.append(
                f"未验视 `{eq_slot}`（daemon 未运行或该槽位为空/超时）：请先运行 `pob item inspect --slot \"{eq_slot}\"` 核验真实前后缀空位，切勿盲猜空词缀。"
            )
    return "\n\n".join(blocks).strip()


def _remediate_v4(trigger_claims: list[str], assistant_text: str) -> str:
    base_cd, label = _detect_trigger_base_cd(trigger_claims, assistant_text)
    tbl = build_server_tick_table(base_cd)
    rows = tbl.get("breakpoints") or []
    lines = [
        f"[PoE1 0.033s (33ms) 服务端帧率断点表 — {label}]",
        "| Ticks | Min CDR | Eff CD | Max APS/Triggers |",
        "|---:|---:|---:|---:|",
    ]
    for r in rows[:5]:
        lines.append(
            f"| {r['ticks']}t | {r['minCdrPct']}% | {r['effectiveCdSec']:.3f}s ({r['effectiveCdMs']}ms) | {r['maxApsCap']:.2f}/s |"
        )
    lines.append(f"- {tbl['cwcNote']}")
    return "\n".join(lines)


def _strip_tags_compact(raw_html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", _html.unescape(txt)).strip()


def _scan_local_web_cache(queries: list[str], *, runtime_dir: Path | None) -> list[str]:
    """0ms local disk cache scan for poewiki_*.json and poedb_*.json (never makes HTTP calls)."""
    needles = [q.strip().lower() for q in queries if isinstance(q, str) and len(q.strip()) >= 2]
    if not needles:
        return []
    cache_dirs: list[Path] = []
    if runtime_dir is not None:
        local_c = Path(runtime_dir) / "cache"
        if local_c.is_dir():
            cache_dirs.append(local_c)
    try:
        shared_c = get_shared_cache_dir(runtime_dir=runtime_dir)
        if shared_c.is_dir() and shared_c not in cache_dirs:
            cache_dirs.append(shared_c)
    except Exception:
        pass

    hits: list[str] = []
    seen_titles: set[str] = set()
    for cdir in cache_dirs:
        candidates: list[Path] = []
        with contextlib.suppress(OSError):
            candidates.extend(cdir.glob("poewiki_*.json"))
            candidates.extend(cdir.glob("poedb_*.json"))
        candidates = [p for p in candidates if p.name != "poedb_autocomplete_index.json"]
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
        for path in candidates[:60]:
            if len(hits) >= 2:
                return hits
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            src = "poewiki" if path.name.startswith("poewiki_") else "poedb"
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            title = str(data.get("title") or data.get("resolvedQuery") or data.get("query") or "").strip()
            url = str(data.get("url") or obj.get("url") or "").strip()
            summary = str(data.get("summary") or "").strip()
            if not summary and isinstance(obj.get("html"), str):
                h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", obj["html"], flags=re.DOTALL | re.IGNORECASE)
                t_m = re.search(r"<title[^>]*>(.*?)</title>", obj["html"], flags=re.DOTALL | re.IGNORECASE)
                if not title:
                    title = _strip_tags_compact(h1_m.group(1) if h1_m else (t_m.group(1) if t_m else ""))
                summary = _strip_tags_compact(obj["html"][:6000])
            haystack = f"{title} {data.get('query') or ''} {data.get('resolvedQuery') or ''} {url}".lower()
            if not any(n in haystack or n.replace(" ", "_") in haystack for n in needles):
                continue
            key = f"{src}:{(title or url).lower()}"
            if key in seen_titles or not summary:
                continue
            seen_titles.add(key)
            label = title or path.stem
            url_part = f" ({url})" if url else ""
            hits.append(f"[Cached {src}] {label}{url_part}: {summary[:360]}")
    return hits


def _remediate_v5(v5_info: dict[str, Any], *, runtime_dir: Path | None) -> str:
    queries = [str(q).strip() for q in (v5_info.get("suggestedQueries") or []) if str(q).strip()]
    hesitations = [str(h).strip() for h in (v5_info.get("hesitations") or []) if str(h).strip()]
    lines: list[str] = []
    if hesitations:
        lines.append(f"- 检出犹豫/猜测表述：`{hesitations[0][:100]}`")
    cached_hits = _scan_local_web_cache(queries, runtime_dir=runtime_dir)
    if cached_hits:
        lines.append("- 本地磁盘缓存命中（0ms）：")
        lines.extend(cached_hits)
    first_q = queries[0] if queries else "<词条>"
    q_args = " ".join(f'"{q}"' for q in queries[:3]) if queries else '"<词条>"'
    lines.append(
        f'- 必做查证命令：`pob research {q_args}`（或 `pob_lookup(kind="research", query="{first_q}")` / `pob wiki "{first_q}"` / `pob poedb "{first_q}"`）。'
    )
    lines.append(
        "- 要求：请先执行上述联网查证（或依据已命中的本地缓存真值）核实机制交互与版本事实，再用明确权威结论重写回复，严禁凭记忆猜测或写“大概/应该/理论上/不确定/建议进游戏实测”。"
    )
    return "\n".join(lines).strip()


def build_correction_prompt(
    reasons: list[str],
    evidence_blocks: list[str],
    *,
    max_chars: int = MAX_CORRECTION_CHARS,
) -> str:
    """Format `<pob-hook-correction retry="1/1">` strictly capped at `<= max_chars`."""
    header = (
        '<pob-hook-correction retry="1/1">\n'
        "[硬护栏拦截] 你的上一版回复未通过确定性核验：\n"
        "- 原因：" + "；".join(reasons) + "\n"
        "- 实测真值（已自动为你补跑）：\n"
    )
    footer = (
        "\n\n请严格依据上述实测真值直接重写给用户的最终回复"
        "（修正违规内容与数值，不要向用户暴露内部拦截日志，无需重复调用已代跑的命令）。\n"
        "</pob-hook-correction>"
    )
    body = "\n\n".join(b.strip() for b in evidence_blocks if b and b.strip())
    avail = max(120, max_chars - len(header) - len(footer))
    if len(body) > avail:
        suffix = "\n...(truncated)"
        body = body[: max(0, avail - len(suffix))].rstrip() + suffix
    out = f"{header}{body}{footer}"
    if len(out) > max_chars:
        # Extreme fallback if reasons themselves were unusually long
        keep = max_chars - len("\n</pob-hook-correction>") - 16
        out = out[: max(0, keep)].rstrip() + "\n...(truncated)\n</pob-hook-correction>"
    return out


def run_remediation(
    *,
    v1_issues: list[Any],
    v2_info: dict[str, Any] | None,
    v3_slots: list[str],
    v4_claims: list[str],
    v5_info: dict[str, Any] | None = None,
    assistant_text: str,
    target_slot: str = "main",
    runtime_dir: Path | None = None,
    budget_sec: float = DEFAULT_BUDGET_SEC,
) -> str:
    """Execute bounded in-process auto-remediation (never cold-starts daemon) and format correction XML."""
    deadline = time.monotonic() + max(0.2, float(budget_sec))
    reasons: list[str] = []
    evidence: list[str] = []

    with _disable_receipts():
        if v1_issues:
            triggers = ", ".join(dict.fromkeys(str(i.trigger) for i in v1_issues[:4]))
            reasons.append(f"V1 终稿命中违禁词/谬误（{triggers}）")
            evidence.append(_remediate_v1(v1_issues, runtime_dir=runtime_dir))
        if v2_info:
            if v2_info.get("reasonKind") == "stale_rev":
                reasons.append(f"V2 槽位 `{v2_info.get('targetSlot', target_slot)}` 在 `--apply` 变更后未重新读取最新 rev 面板")
            else:
                reasons.append("V2 本轮无有效 PoB 算力回执即输出构筑/面板数字结论")
            evidence.append(_remediate_v2(v2_info, runtime_dir=runtime_dir, deadline=deadline))
        if v3_slots:
            reasons.append(f"V3 未执行 item inspect 即断言装备空词缀（{', '.join(v3_slots[:3])}）")
            evidence.append(
                _remediate_v3(
                    v3_slots,
                    target_slot=target_slot,
                    runtime_dir=runtime_dir,
                    deadline=deadline,
                )
            )
        if v4_claims:
            reasons.append("V4 未执行 `pob diagnose` 或 `pob data tick` 即断言触发器 APS/CDR 断点")
            evidence.append(_remediate_v4(v4_claims, assistant_text))
        if v5_info:
            has_hes = bool(v5_info.get("hesitations"))
            has_mech = bool(v5_info.get("mechNeed"))
            if has_hes and has_mech:
                reasons.append("V5 本轮未联网核验即对流派机制运作做出犹豫/猜测表述")
            elif has_hes:
                reasons.append("V5 终稿含犹豫/凭记忆猜测措辞且本轮未联网查证")
            else:
                reasons.append("V5 涉及流派机制运作/交互边界问题但本轮尚未调用 poedb/poewiki 核验")
            evidence.append(_remediate_v5(v5_info, runtime_dir=runtime_dir))

    return build_correction_prompt(reasons, evidence)
