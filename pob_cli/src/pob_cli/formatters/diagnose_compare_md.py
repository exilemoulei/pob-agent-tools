"""Markdown formatters for `pob diagnose --compare`, fact cards/upgrades, and `pob ground`."""

from __future__ import annotations

from typing import Any

from ..game_data.fact_cards import normalize_stat_lines
from .build_md import format_compare_markdown
from .primitives import (
    _parse_numeric_or_none,
    fmt_num,
    render_table,
    render_update_warning,
    strip_pob_escapes,
)


def _fmt_ask_grounding_line(res: dict[str, Any]) -> str | None:
    ask_g = res.get("askGrounding")
    if not (isinstance(ask_g, dict) and ask_g.get("query")):
        return None
    slang_list = ask_g.get("resolvedSlang") or []
    added_ent = ask_g.get("addedEntities") or []
    if not slang_list and not added_ent:
        return None
    slang_note = (
        " | Slang: " + ", ".join(f"{s['slang']}={s['canonical']}" for s in slang_list[:6])
        if slang_list
        else ""
    )
    ent_note = f" | Grounded: {', '.join(added_ent[:6])}" if added_ent else ""
    return f"> **[Question Grounding]** `{str(ask_g['query'])[:120]}`{slang_note}{ent_note}"


def _filter_duplicate_md_advisories(res: dict[str, Any], advisories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    core_syn = (
        res.get("core_synergies")
        or res.get("coreSynergies")
        or (res.get("buildMechanics") or {}).get("coreSynergies")
        or []
    )
    syn_blob = " ".join(str(x) for x in core_syn).lower()
    gap_blob = " ".join(
        str(g.get("message") or "") for g in (res.get("healthGaps") or []) if isinstance(g, dict)
    ).lower()
    bm = res.get("buildMechanics") or {}
    has_soa = isinstance(bm.get("swordOfAges"), dict)
    has_cwc = any(
        isinstance(t, dict) and t.get("kind") == "fixed_interval_channelling"
        for t in (bm.get("triggers") or [])
    )

    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for adv in advisories:
        if not isinstance(adv, dict):
            continue
        rid = str(adv.get("ruleId") or "").strip().upper()
        trig = str(adv.get("trigger") or "").strip().lower()
        k = rid or f"{adv.get('tag')}:{trig}"
        if k in seen_keys:
            continue
        if (rid == "PATCH_330_AGNOSTIC_CWS_SYNERGY" or "the agnostic" in trig or "valyrium" in trig) and (
            "[do not unspec]" in syn_blob or "the agnostic" in syn_blob
        ):
            continue
        if (rid == "PATCH_330_RAMAKO_FIXED_DOT_RES" or "ramako" in trig or "雷默科" in trig) and "ramako" in syn_blob:
            continue
        if (rid == "PATCH_330_DORYANI_PROTOTYPE_NEG_RES" or "doryani's prototype" in trig) and (
            "doryani's prototype" in syn_blob or "doryani's prototype" in gap_blob
        ):
            continue
        if rid == "PATCH_330_SWORD_OF_AGES_TEMPORAL_MIRAGE" and has_soa:
            continue
        if rid == "PATCH_330_CWC_NOT_COOLDOWN" and has_cwc:
            continue
        if rid == "PATCH_330_LOW_LIFE_ESH_VISAGE" and "esh's visage" in gap_blob:
            continue
        if ("frost bomb" in trig or "寒霜爆" in trig or "FROST_BOMB" in rid) and "frost bomb" in syn_blob:
            continue
        seen_keys.add(k)
        out.append(adv)
    return out


def _fmt_diagnose_fact_cards_and_upgrades(res: dict[str, Any], *, compact: bool) -> list[str]:
    lines: list[str] = []
    env = res.get("env") or {}
    fc = res.get("factCards") or {}
    gem_cards = fc.get("gems") or []
    uniq_cards = fc.get("uniques") or []
    advisories = _filter_duplicate_md_advisories(res, list(res.get("advisories") or []))
    if gem_cards or uniq_cards or advisories:
        lines.append(f"\n#### 3. Live PoE1 {env.get('patch') or '3.30'} Fact Cards & Patch Advisories")
        for adv in advisories:
            lines.append(f"> {adv.get('tag', '[POE1-TRAP-WARN]')} `{adv.get('trigger')}`: {adv.get('message')}")
        for gc in gem_cards[: (4 if compact else 8)]:
            zh_p = f" ({gc['zhName']})" if gc.get("zhName") else ""
            is_sup_card = bool(gc.get("isSupport") or gc.get("is_support"))
            bits: list[str] = ["Support" if is_sup_card else "Active"]
            if gc.get("castTime") is not None:
                bits.append(f"Cast={gc['castTime']}s")
            cd_card = _parse_numeric_or_none(gc.get("cooldown"))
            if cd_card is not None and cd_card > 0:
                su_raw = gc.get("stored_uses") if gc.get("stored_uses") is not None else gc.get("storedUses")
                try:
                    su_int = int(su_raw) if su_raw is not None else 0
                except (TypeError, ValueError):
                    su_int = 0
                cd_lbl = f"CD={cd_card:g}s" + (f" ({su_int} uses)" if su_int > 1 else "")
                bits.append(cd_lbl)
            if gc.get("tags"):
                bits.append(f"Tags={gc['tags']}")
            if gc.get("legacy_removed_328") or gc.get("legacyRemoved328"):
                bits.append("**[LEGACY REMOVED 3.28+]**")
            elif gc.get("is_exceptional") or gc.get("isExceptional"):
                nat_lv = gc.get("natural_max_level") or gc.get("naturalMaxLevel")
                cor_lv = gc.get("corrupted_max_level") or gc.get("corruptedMaxLevel")
                if nat_lv is not None and cor_lv is not None:
                    bits.append(f"[Exceptional | MaxLv {nat_lv}/{cor_lv}]")
                else:
                    bits.append("[Exceptional]")
            lv_stats = list((gc.get("lv20Stats") or {}).items())[:4]
            if lv_stats:
                bits.append("Lv20: " + ", ".join(f"{k}={v}" for k, v in lv_stats))
            lines.append(f"- **[Gem] {gc.get('name')}{zh_p}**: " + " | ".join(bits))
        for uc in uniq_cards[: (5 if compact else 10)]:
            zh_p = f" ({uc['zhName']})" if uc.get("zhName") else ""
            mods_preview = "; ".join(normalize_stat_lines(uc.get("explicits") or [])[: (5 if compact else 8)])
            lines.append(f"- **[Unique] {uc.get('name')}{zh_p}** (`{uc.get('baseType') or '-'}`): {mods_preview}")

    top_nodes = res.get("topNodes")
    top_gems = res.get("topGems")
    if top_nodes or top_gems:
        lines.append("\n#### 4. Top ROI Upgrades")
        if isinstance(top_nodes, dict) and (top_nodes.get("byPerPoint") or []):
            n_rows = [
                [
                    idx,
                    n.get("allocSpec")
                    or (f"{n.get('id')}:{n.get('effectId')}" if n.get("effectId") is not None else n.get("id")),
                    " ".join(
                        part.strip()
                        for part in strip_pob_escapes(n.get("nameZh") or n.get("name") or "-").splitlines()
                        if part.strip()
                    ),
                    n.get("pathDist"),
                    strip_pob_escapes(n.get("pathPowerStr") or fmt_num(n.get("pathPower"), 2)),
                    strip_pob_escapes(n.get("powerStr") or fmt_num(n.get("power"), 2)),
                ]
                for idx, n in enumerate(top_nodes.get("byPerPoint") or [], start=1)
            ]
            n_base = f" @ {top_nodes['baselineLabel']}" if top_nodes.get("baselineLabel") else ""
            lines.append(
                f"\n**Top Passive Nodes (`{top_nodes.get('stat')}`{n_base}, max dist {top_nodes.get('maxDist')})**:"
            )
            lines.append(render_table(["#", "Node ID", "Name", "Dist", "Gain/Pt", "Total Gain"], n_rows))
        if isinstance(top_gems, dict) and (
            (top_gems.get("candidates") or []) or (top_gems.get("excludedSupports") or [])
        ):
            cands = top_gems.get("candidates") or []
            g_base = f" @ {top_gems['baselineLabel']}" if top_gems.get("baselineLabel") else ""
            lines.append(
                f"\n**Top Candidate Support Gems (`{top_gems.get('dpsField')}`{g_base}, {top_gems.get('mode')})**:"
            )
            if cands:
                has_caveats = any(gm.get("caveat") for gm in cands)
                g_rows = []
                for idx, gm in enumerate(cands, start=1):
                    row_g: list[Any] = [
                        idx,
                        strip_pob_escapes(gm.get("nameZh") or gm.get("name") or "-"),
                        strip_pob_escapes(gm.get("name") or "-"),
                        fmt_num(gm.get("delta")),
                        f"{float(gm['pct']):+.1f}%" if gm.get("pct") is not None else "-",
                    ]
                    if has_caveats:
                        row_g.append(str(gm.get("caveat") or "-"))
                    g_rows.append(row_g)
                headers_g = ["#", "Gem (ZH)", "Gem (EN)", "Delta", "Gain %"] + (["Caveat"] if has_caveats else [])
                lines.append(render_table(headers_g, g_rows))
            excluded = top_gems.get("excludedSupports") or []
            if excluded:
                excl_str = "; ".join(
                    f"{strip_pob_escapes(str(ex.get('name') or '-'))} ({ex.get('reason') or 'conflict'})"
                    for ex in excluded[:6]
                )
                lines.append(f"_Excluded conflicting supports: {excl_str}_")
            skipped = top_gems.get("filteredArchetypeSwaps") or []
            if skipped:
                lines.append(f"_Filtered archetype-changing / invalid supports: {', '.join(skipped[:6])}_")

    rank_errs = res.get("rankErrors") or []
    if rank_errs:
        lines.append("\n_Ranking notes_: " + "; ".join(rank_errs))
    return lines


def format_diagnose_compare_markdown(res: dict[str, Any]) -> str:
    """Format `pob diagnose --compare` into a unified, compact (<8KB) lossless Markdown report."""
    from .diagnose_md import format_diagnose_markdown

    target = str(res.get("target") or "main")
    ref_target = str(res.get("refTarget") or "ref")
    build_a = res.get("buildA") or {}
    build_b = res.get("buildB") or {}
    cmp_res = res.get("comparison") or {}
    env = res.get("env") or build_a.get("env") or {}

    rev_a = (build_a.get("env") or {}).get("rev") if isinstance(build_a, dict) else None
    rev_b = (build_b.get("env") or {}).get("rev") if isinstance(build_b, dict) else None
    slot_a_lbl = f"{target} (rev={rev_a})" if isinstance(rev_a, int) and not isinstance(rev_a, bool) else target
    slot_b_lbl = f"{ref_target} (rev={rev_b})" if isinstance(rev_b, int) and not isinstance(rev_b, bool) else ref_target
    parts: list[str] = [
        f"[Env: PoE1 {env.get('patch') or '3.30'} (NOT PoE2) | PoB {env.get('pobVersion') or '-'} | "
        f"Data {env.get('dataVersion') or '-'} | Slots: {slot_a_lbl} vs {slot_b_lbl}]"
    ]
    u_warn = render_update_warning(res.get("updateWarning"))
    if u_warn:
        parts.append(u_warn.strip())

    parts.append("\n## Dual-Build 3-Baseline Comparison")
    parts.append(format_compare_markdown(cmp_res, target=target, ref_target=ref_target, compact=True))

    if build_a:
        parts.append(f"\n## Build A Diagnosis (`{target}`)")
        parts.append(format_diagnose_markdown(build_a, compact=True))
    if build_b:
        parts.append(f"\n## Build B Diagnosis (`{ref_target}`)")
        parts.append(format_diagnose_markdown(build_b, compact=True))
    return "\n".join(parts)


def format_ground_markdown(res: dict[str, Any]) -> str:
    """Format `pob ground` entity & trap grounding output cleanly."""
    block = str(res.get("contextBlock") or "").strip()
    elapsed = res.get("elapsedMs")
    if elapsed is not None:
        return f"{block}\n_(grounded in {elapsed:.1f}ms)_" if block else f"_(no entities or traps matched; {elapsed:.1f}ms)_"
    return block or "_(no entities or traps matched)_"
