"""Markdown formatters for local game data queries (`pob data <gem|unique|mod|base|translate>`)."""

from __future__ import annotations

from typing import Any

from ..game_data.mod_guidance import DROP_ONLY_SOURCES
from .primitives import _parse_numeric_or_none, render_table


def _fmt_game_data_gem(results: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for r in results:
        gem_name = r.get("display_name") or r.get("name") or "-"
        zh_nm = r.get("zh_name") or r.get("zhName")
        zh_part = f" ({zh_nm})" if zh_nm else ""
        is_sup = bool(r.get("is_support") or r.get("isSupport"))
        kind_lbl = "Support" if is_sup else "Active"
        is_legacy_328 = bool(r.get("legacy_removed_328") or r.get("legacyRemoved328"))
        is_exceptional = bool(r.get("is_exceptional") or r.get("isExceptional"))
        badges: list[str] = []
        if is_legacy_328:
            badges.append("**[LEGACY REMOVED 3.28+]**")
        elif r.get("legacyOnly"):
            badges.append(f"**[LEGACY / REMOVED IN {r.get('removedInPatch') or '3.25'}]**")
        if is_exceptional:
            nat_lv = r.get("natural_max_level") if r.get("natural_max_level") is not None else r.get("naturalMaxLevel")
            cor_lv = r.get("corrupted_max_level") if r.get("corrupted_max_level") is not None else r.get("corruptedMaxLevel")
            if nat_lv is not None and cor_lv is not None:
                badges.append(f"[Exceptional | MaxLv {nat_lv}/{cor_lv}]")
            else:
                badges.append("[Exceptional]")
        badge_str = (" " + " ".join(badges)) if badges else ""
        lines.append(f"\n#### {gem_name}{zh_part} — `{r.get('color') or '-'}` {kind_lbl}{badge_str}")
        if r.get("legacyNote"):
            lines.append(f"> [POE1-PATCH-WARN] {r['legacyNote']}")
        elif is_legacy_328:
            lines.append(
                f"> [POE1-PATCH-WARN] '{gem_name}' was drop-disabled in PoE1 3.28 (Mirage) "
                "and is unavailable in current leagues."
            )

        meta_bits: list[str] = []
        if r.get("tag_string"):
            meta_bits.append(f"Tags: {r['tag_string']}")
        if r.get("cast_time") is not None:
            meta_bits.append(f"Cast: {r['cast_time']}s")
        cd_val = _parse_numeric_or_none(r.get("cooldown"))
        if (cd_val is None or cd_val <= 0.0) and isinstance(r.get("levels"), dict):
            lvls_map = r["levels"]
            lvl_ref = lvls_map.get("20") or lvls_map.get(20) or (next(iter(lvls_map.values())) if lvls_map else {})
            if isinstance(lvl_ref, dict):
                cd_val = _parse_numeric_or_none(lvl_ref.get("cooldown"))
        if cd_val is not None and cd_val > 0.0:
            su_raw = r.get("stored_uses") if r.get("stored_uses") is not None else r.get("storedUses")
            try:
                su_int = int(su_raw) if su_raw is not None else 0
            except (TypeError, ValueError):
                su_int = 0
            cd_lbl = f"CD: {cd_val:g}s" + (f" ({su_int} uses)" if su_int > 1 else "")
            meta_bits.append(cd_lbl)
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
            q_parts = []
            for q in q_stats[:4]:
                pq = q.get("per_quality")
                at20 = q.get("at_20_quality")
                at40 = q.get("at_40_quality")
                lbl = q.get("label")
                if lbl:
                    q_parts.append(f"{lbl} [`{q['stat']}`, {pq}/1%, @20%={at20}, @40%={at40}]")
                else:
                    q_parts.append(f"{q['stat']}={at20} (@20%, {pq}/1%, @40%={at40})")
            lines.append(f"- **Quality**: {'; '.join(q_parts)}")
        variants = r.get("variants") or []
        if variants:
            v_parts = []
            for v in variants[:8]:
                v_zh = f" ({v['zh_name']})" if v.get("zh_name") else ""
                v_parts.append(f"{v.get('name')}{v_zh} [{v.get('relation')}]")
            lines.append(f"- **Variants**: {', '.join(v_parts)}")
    return lines


def _fmt_game_data_unique(results: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for r in results:
        zh_part = f" ({r['zh_name']})" if r.get("zh_name") else ""
        base_zh = f" ({r['base_type_zh']})" if r.get("base_type_zh") else ""
        lines.append(f"\n#### {r.get('name')}{zh_part} — {r.get('base_type')}{base_zh} (`{r.get('item_class')}`)")
        meta = [x for x in [f"ReqLv {r['req_level']}" if r.get("req_level") else "", r.get("league") or "", r.get("source") or ""] if x]
        if meta:
            lines.append("- " + " | ".join(meta))
        matched_mods = r.get("matched_mods") or []
        if matched_mods:
            for mm in matched_mods[:4]:
                lines.append(f"- **[Match]** {mm}")
        for imp, imp_zh in zip(r.get("implicits") or [], (r.get("implicits_zh") or []) + [None] * 10, strict=False):
            lines.append(f"- [Implicit] {imp}" + (f" / {imp_zh}" if imp_zh else ""))
        for exp, exp_zh in zip(r.get("explicits") or [], (r.get("explicits_zh") or []) + [None] * 30, strict=False):
            lines.append(f"- {exp}" + (f" / {exp_zh}" if exp_zh else ""))
        fb_mods = r.get("foulborn_mods") or []
        if fb_mods:
            lines.append("- **Foulborn Mutations**:")
            for fb in fb_mods[:8]:
                st_en = "; ".join(fb.get("stats") or [])
                st_zh = "; ".join(x for x in (fb.get("stats_zh") or []) if x)
                lines.append(f"  - {st_en}" + (f" / {st_zh}" if st_zh else ""))
        donor_en, donor_zh = r.get("vestigial_donor_mods") or [], r.get("vestigial_donor_mods_zh") or []
        if donor_en:
            pairs = [
                f"{en} / {donor_zh[i]}" if (i < len(donor_zh) and donor_zh[i] and donor_zh[i] != en) else str(en)
                for i, en in enumerate(donor_en)
            ]
            lines.append(f"- **Gives as Vestigial implicit**: {'; '.join(pairs)}")
        if r.get("takes_vestigial"):
            lines.append(f"- **Takes a Vestigial implicit** from another unique `{r.get('item_class')}`")
    return lines


def _fmt_game_data_mod(results: list[dict[str, Any]]) -> list[str]:
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
        elif weights:
            w_disp = ", ".join(f"{k}:{v}" for k, v in list(weights.items())[:5])
        elif r.get("source") == "Vestigial":
            donors, types_s = r.get("vestigialDonors") or [], ",".join(r.get("applicable_types") or [])
            w_disp = f"unique {types_s}, from {donors[0]}" if (donors and types_s) else "not offered by PoB"
        else:
            origin = str(r.get("drop_origin") or "")
            app_types = r.get("applicable_types") or []
            if app_types:
                types_s = ",".join(str(x) for x in app_types[:4])
                w_disp = f"{origin or 'Drop'}[{types_s}] (drop-only)"
            elif origin:
                w_disp = f"{origin} (drop-only; no roll weight)"
            elif str(r.get("source") or "Explicit") in DROP_ONLY_SOURCES:
                w_disp = "drop-only (no roll weight)"
            else:  # a legacy corrupted, Eldritch, Foulborn or jewel implicit: not a dropped explicit
                w_disp = f"{r.get('source')} (no roll weight)"
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
    return [render_table(["Source", "Type", "Affix (Group)", "iLvl", "Stats", "Weights / Bench"], rows)]


def _fmt_game_data_base(results: list[dict[str, Any]]) -> list[str]:
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
    return [render_table(["Base", "Class", "Req (Lv/S/D/I)", "Properties", "Implicits"], rows)]


def _fmt_game_data_tattoo(results: list[dict[str, Any]]) -> list[str]:
    rows = []
    for r in results:
        en, zh = str(r.get("name") or "-"), str(r.get("zh_name") or "")
        kind = ("Runegraft" if r.get("kind") == "runegraft" else "Tattoo") + (" [Legacy]" if r.get("legacy") else "")
        target, target_value = str(r.get("target_type") or "-"), str(r.get("target_value") or "")
        s_en, s_zh = r.get("stats") or [], r.get("stats_zh") or []
        stats = [f"{s} / {s_zh[i]}" if (i < len(s_zh) and s_zh[i] and s_zh[i] != s) else str(s) for i, s in enumerate(s_en)]
        rows.append(
            [
                kind,
                f"{zh} ({en})" if (zh and zh != en) else en,
                f"{target} ({target_value})" if target_value else target,
                "; ".join(stats) or "-",
                r.get("limit_text") or "-",
            ]
        )
    return [render_table(["Kind", "Name", "Replaces", "Stats", "Limit"], rows)]


def format_game_data(res: dict[str, Any], kind: str | None = None, query: str | None = None) -> str:
    """Format `pob data <gem|unique|mod|base|translate|tattoo|runegraft>` local game data results into Markdown."""
    kind_norm = str(res.get("kind") or kind or "gem").lower()
    q_disp = str(query if query is not None else (res.get("query") or ""))
    results = res.get("results") or []
    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")
    alias = res.get("resolvedAlias")
    alias_note = f" (alias `{alias['from']}` -> `{alias['to']}`)" if isinstance(alias, dict) and alias.get("to") else ""
    lines.append(
        f"### Local Game Data (`{kind_norm}`): `{q_disp}`{alias_note} — `{res.get('count', len(results))}` hits "
        f"(PoB `{res.get('pobVersion')}` / Data `{res.get('dataVersion')}`)"
    )
    if not results:
        lines.append("_(no matches)_")
        sugs = res.get("suggestions") or []
        if sugs:
            s_rows = [
                [s.get("name") or "-", s.get("zh_name") or "-", f"{float(s.get('score') or 0):.2f}"]
                for s in sugs
            ]
            lines.append("\n#### Did You Mean")
            lines.append(render_table(["Name (EN)", "Name (ZH)", "Score"], s_rows))
        if res.get("hint"):
            lines.append(f"\n> {res['hint']}")
        return "\n".join(lines)

    if kind_norm == "translate":
        rows = [[r.get("en"), r.get("zh") or "-", r.get("category") or "-", r.get("match_type") or "-"] for r in results]
        lines.append(render_table(["English", "Chinese", "Category", "Match"], rows))
    elif kind_norm == "gem":
        lines.extend(_fmt_game_data_gem(results))
    elif kind_norm == "unique":
        lines.extend(_fmt_game_data_unique(results))
    elif kind_norm == "mod":
        lines.extend(_fmt_game_data_mod(results))
        if res.get("dropOnlyNote"):
            lines.append(f"\n> {res['dropOnlyNote']}")
    elif kind_norm == "base":
        lines.extend(_fmt_game_data_base(results))
    elif kind_norm in ("tattoo", "runegraft"):
        lines.extend(_fmt_game_data_tattoo(results))

    return "\n".join(lines)
