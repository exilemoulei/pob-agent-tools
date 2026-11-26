"""Markdown renderer for `pob timeless search --trade` in-stock buyout matches and batch URLs."""

from __future__ import annotations

import math
from typing import Any

from ..locale_cfg import current_lang
from .primitives import fmt_num, render_table


def _esc(val: Any) -> str:
    return str(val if val is not None else "-").replace("|", "\\|").replace("\n", " ").strip() or "-"


def _buyout_badge(trade_mode: str | None, gold_fee: int | None, lang: str) -> str:
    if trade_mode == "securable":
        label = "一口价" if lang == "zh-CN" else ("一口價" if lang == "zh-TW" else "Instant")
        if isinstance(gold_fee, int) and gold_fee > 0:
            return f"[{label} · {gold_fee:,}G]"
        return f"[{label}]"
    label = "私聊" if lang in ("zh-CN", "zh-TW") else "Whisper"
    return f"[{label}]"


def _fmt_price(item: dict[str, Any], lang: str) -> str:
    amt = item.get("priceAmount")
    cur = str(item.get("priceCurrency") or "").strip().lower()
    if not isinstance(amt, (int, float)) or not math.isfinite(amt) or not cur:
        base = "no price"
    elif cur == "chaos":
        base = f"{amt:g}c"
    elif cur == "divine":
        base = f"{amt:g}d"
    else:
        base = f"{amt:g} {cur}"
    return f"{base} {_buyout_badge(item.get('tradeMode'), item.get('goldFee'), lang)}"


def _fmt_delta(delta: Any, pct: Any) -> str:
    has_d = isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(delta)
    has_p = isinstance(pct, (int, float)) and not isinstance(pct, bool) and math.isfinite(pct)
    if not has_d and not has_p:
        return "-"
    parts: list[str] = []
    if has_d:
        sign = "+" if float(delta) > 0 else ""
        parts.append(f"{sign}{fmt_num(delta)}")
    if has_p:
        parts.append(f"({float(pct):+.1f}%)")
    return " ".join(parts)


def _fmt_socket(item: dict[str, Any]) -> str:
    sock = item.get("socket")
    if sock is None:
        return "-"
    label = str(item.get("socketLabel") or "").strip()
    return f"#{sock} ({label})" if label else f"#{sock}"


def format_timeless_trade_markdown(trade_res: dict[str, Any], *, stat: str = "CombinedDPS") -> str:
    """Render `### In-Stock Trade Matches (市集一口价现货)` table and pre-sharded batch URLs."""
    lang = current_lang()
    matches = list(trade_res.get("matches") or [])
    batches = list(trade_res.get("batches") or [])
    mode = str(trade_res.get("mode") or "live")
    budget = trade_res.get("budget")
    currency = str(trade_res.get("currency") or "divine")
    budget_txt = f"<= {budget:g} {currency}" if isinstance(budget, (int, float)) and budget > 0 else "unlimited"

    lines: list[str] = ["### In-Stock Trade Matches (市集一口价现货)"]
    summary_parts = [
        f"League: `{trade_res.get('league')}` (`{trade_res.get('realm')}`)",
        f"Status: `{trade_res.get('status')}`",
        f"Budget: `{budget_txt}`",
    ]
    if mode == "urls_only":
        reason = "POB_OFFLINE=1 fallback" if trade_res.get("offlineFallback") else "0 HTTP"
        summary_parts.append(f"Mode: `urls_only` ({reason}; {len(batches)} pre-sharded batch URLs)")
    else:
        exec_n = int(trade_res.get("batchesExecuted") or 0)
        tot_n = int(trade_res.get("totalShards") or len(batches))
        batch_note = f"Batches: {exec_n}/{tot_n}"
        if trade_res.get("earlyStopped"):
            batch_note += f" (Early-Stop: {trade_res.get('earlyStopReason')})"
        summary_parts.append(batch_note)
    if int(trade_res.get("sellerCappedDropped") or 0) > 0:
        summary_parts.append(f"Seller cap dropped: {int(trade_res['sellerCappedDropped'])}")
    lines.append("> " + " | ".join(summary_parts))

    if trade_res.get("offlineFallback"):
        lines.append("> [WARN] `POB_OFFLINE=1` active: skipped live Trade API calls and emitted pre-sharded URLs only.")
    if trade_res.get("leagueWarning"):
        lines.append(f"> [WARN] {trade_res['leagueWarning']}")
    err = trade_res.get("error")
    if isinstance(err, dict) and err.get("message"):
        lines.append(
            f"> [WARN] Trade API stopped early ({err.get('type') or 'TradeError'}, exit {err.get('exitCode', 5)}): "
            f"{err['message']} — PoB seed results and pre-sharded fallback URLs are preserved below."
        )

    extra_stat = stat not in ("CombinedDPS", "TotalEHP")
    if matches:
        headers = ["#", "Price", "Socket", "Seed & Conqueror"]
        if extra_stat:
            headers.append(f"PoB Δ{stat}")
        headers.extend(["PoB ΔDPS", "PoB ΔEHP", "DPS/Div", "Key Notables", "Seller", "Trade Link"])
        rows: list[list[Any]] = []
        for idx, m in enumerate(matches, start=1):
            notables = list(m.get("keyNotables") or [])
            notable_txt = ", ".join(notables[:3]) + (" ..." if len(notables) > 3 else "") if notables else "-"
            seller = str(m.get("seller") or "-")
            dups = int(m.get("sellerDuplicatesCollapsed") or 0)
            if dups > 0:
                seller = f"{seller} (+{dups} dup)"
            dps_div = fmt_num(m.get("dpsPerDivine")) if m.get("dpsPerDivine") is not None else "-"
            seed_txt = f"`{m.get('seed')}` ({m.get('conqueror') or '-'})"
            row: list[Any] = [idx, _esc(_fmt_price(m, lang)), _esc(_fmt_socket(m)), _esc(seed_txt)]
            if extra_stat:
                row.append(_esc(_fmt_delta(m.get("deltaStat"), m.get("deltaStatPct"))))
            row.extend(
                [
                    _esc(_fmt_delta(m.get("deltaDps"), m.get("deltaDpsPct"))),
                    _esc(_fmt_delta(m.get("deltaEhp"), m.get("deltaEhpPct"))),
                    dps_div, _esc(notable_txt), _esc(seller),
                    f"[Open]({m.get('url')})" if m.get("url") else "-",
                ]
            )
            rows.append(row)
        lines.append(render_table(headers, rows))
    elif mode == "urls_only":
        lines.append("\n> Live Trade API lookup skipped; open the pre-sharded batch URLs below.")
    elif not err:
        lines.append("\n> No in-stock listings matched the searched top seed batches; widen `--max-trade-batches`, `--budget`, or `--status`.")

    unfetched_total = sum(int(b.get("unfetchedCount") or 0) for b in batches if b.get("executed"))
    rem_shards = int(trade_res.get("remainingShardCount") or 0)
    if unfetched_total > 0 or rem_shards > 0:
        notes: list[str] = []
        if unfetched_total > 0:
            notes.append(f"{unfetched_total} additional listings in executed batches were not fetched (fetch cap)")
        if rem_shards > 0:
            notes.append(f"{rem_shards} pre-sharded batch(es) were not executed")
        lines.append(f"\n> Note: {'; '.join(notes)}.")

    if batches:
        lines.append("\n#### Pre-sharded Batch URLs")
        shown_batches = batches[:10]
        for b in shown_batches:
            seeds = b.get("seeds") or []
            preview = ", ".join(str(s) for s in seeds[:5]) + (" ..." if len(seeds) > 5 else "")
            status_tag = (
                f"executed: {b.get('total', 0)} hits / {b.get('matched', 0)} matched"
                if b.get("executed")
                else "not executed"
            )
            lines.append(
                f"- Batch {b.get('batch')} ({b.get('seedCount')} seeds, {b.get('filterCount')} filters, "
                f"complexity {b.get('complexity')}, {status_tag}) [{preview}]: {b.get('url')}"
            )
        if len(batches) > len(shown_batches):
            lines.append(f"- ... and {len(batches) - len(shown_batches)} more pre-sharded batches (available in `--json`).")
    return "\n".join(lines)
