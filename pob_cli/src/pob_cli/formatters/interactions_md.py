"""Markdown formatter for `pob interactions` (ModParser interactions, flag mismatches, and red mods)."""

from __future__ import annotations

from typing import Any

from .primitives import render_table, strip_pob_escapes


def _fmt_main_skill_line(ms: dict[str, Any]) -> str:
    name = strip_pob_escapes(str(ms.get("skillName") or "-"))
    mod_flags = ", ".join(ms.get("mainModFlagNames") or []) or "none"
    kw_flags = ", ".join(ms.get("mainKeywordFlagNames") or []) or "none"
    dmgs = ", ".join(ms.get("damageTypes") or []) or "none"
    return (
        f"- **Active Main Skill**: `{name}` | **ModFlags**: `{mod_flags}` | "
        f"**KeywordFlags**: `{kw_flags}` | **DamageTypes**: `{dmgs}`"
    )


def _fmt_mod_summary(entry: dict[str, Any]) -> str:
    mods = entry.get("mods") or []
    if not mods:
        return "-"
    parts: list[str] = []
    for m in mods[:3]:
        nm = str(m.get("name") or "?")
        tp = str(m.get("type") or "?")
        val = m.get("value")
        val_s = str(val) if not isinstance(val, dict) else "{...}"
        parts.append(f"{nm}:{tp}={val_s}")
    if len(mods) > 3:
        parts.append(f"(+{len(mods) - 3})")
    return "; ".join(parts)


def _fmt_details(entry: dict[str, Any]) -> str:
    bits: list[str] = []
    if entry.get("mismatchedFlags"):
        bits.append("Mismatch: " + ", ".join(str(x) for x in entry["mismatchedFlags"]))
    if entry.get("partialMismatches"):
        bits.append("Partial mismatch: " + ", ".join(str(x) for x in entry["partialMismatches"]))
    if entry.get("premises"):
        bits.append("Premise: " + ", ".join(str(x) for x in entry["premises"]))
    if entry.get("unsupported") and entry.get("extra"):
        bits.append("Unparsed: " + strip_pob_escapes(str(entry["extra"])))
    return " | ".join(bits) if bits else "-"


def _fmt_slot_label(it: dict[str, Any]) -> str:
    slot = str(it.get("slot") or "-")
    return f"{slot} [INACTIVE]" if it.get("active") is False else slot


def format_interactions_markdown(res: dict[str, Any]) -> str:
    """Render concise Markdown report for `pob interactions`."""
    if not isinstance(res, dict):
        return ""
    mode = str(res.get("mode") or "build_wide")
    lines: list[str] = ["### PoB Mechanics Interactions (机制联动分析)"]
    ms = res.get("mainSkill")
    if isinstance(ms, dict) and ms:
        lines.append(_fmt_main_skill_line(ms))

    if mode == "unsupported_only":
        unsup = res.get("unsupportedMods") or {}
        total = int(unsup.get("totalCount") or 0)
        lines.append(f"- **Uncomputed Red Mods (`^xF05050`)**: `{total}`")
        rows: list[list[Any]] = []
        for cm in unsup.get("customMods") or []:
            rows.append(["ConfigTab", cm.get("blockTitle") or "Default", strip_pob_escapes(str(cm.get("rawLine") or ""))])
        for it in unsup.get("items") or []:
            rows.append([_fmt_slot_label(it), it.get("itemName") or "-", strip_pob_escapes(str(it.get("rawLine") or ""))])
        if rows:
            lines.append(render_table(["Slot", "Source", "Unparsed Mod Line (0 PoB Effect)"], rows))
        else:
            lines.append("- None (all equipped gear and customMods parsed cleanly by PoB).")
        return "\n".join(lines)

    if mode == "targeted":
        if res.get("slot"):
            inm = strip_pob_escapes(str(res.get("itemName") or "-"))
            lines.append(f"\n#### Slot `{res['slot']}` — {inm}")
            s_rows = [
                [
                    c.get("section") or "-",
                    strip_pob_escapes(str(c.get("rawLine") or "")),
                    c.get("verdict") or "-",
                    _fmt_mod_summary(c),
                    _fmt_details(c),
                ]
                for c in (res.get("slotLines") or [])
            ]
            if s_rows:
                lines.append(render_table(["Section", "Mod Line", "Verdict", "Parsed Mods", "Flags / Premise"], s_rows))
            else:
                lines.append("- _(No matching mod lines in this slot.)_")
        if "lines" in res:
            lines.append("\n#### Candidate Mod Lines")
            c_rows = [
                [
                    strip_pob_escapes(str(c.get("rawLine") or "")),
                    c.get("verdict") or "-",
                    _fmt_mod_summary(c),
                    _fmt_details(c),
                ]
                for c in (res.get("lines") or [])
            ]
            if c_rows:
                lines.append(render_table(["Mod Line", "Verdict", "Parsed Mods", "Flags / Premise"], c_rows))
            else:
                lines.append("- _(No matching candidate mod lines.)_")
        return "\n".join(lines)

    # build_wide mode
    unsup = res.get("unsupportedMods") or {}
    u_total = int(unsup.get("totalCount") or 0)
    lines.append(f"\n#### 1. Uncomputed Red Mods (`UNSUPPORTED_BY_POB`): `{u_total}`")
    u_rows: list[list[Any]] = []
    for cm in unsup.get("customMods") or []:
        u_rows.append(["ConfigTab", cm.get("blockTitle") or "Default", strip_pob_escapes(str(cm.get("rawLine") or ""))])
    for it in unsup.get("items") or []:
        u_rows.append([_fmt_slot_label(it), it.get("itemName") or "-", strip_pob_escapes(str(it.get("rawLine") or ""))])
    if u_rows:
        lines.append(render_table(["Slot", "Item / Block", "Red Unparsed Mod (0 PoB Effect)"], u_rows))
    else:
        lines.append("- None (0 unparsed red mods across active gear and customMods).")

    mismatches = res.get("flagMismatches") or []
    lines.append(f"\n#### 2. Main-Skill Flag Mismatches (`FLAG_MISMATCH`): `{len(mismatches)}`")
    if mismatches:
        m_rows = [
            [
                m.get("slot") or "-",
                m.get("itemName") or "-",
                strip_pob_escapes(str(m.get("rawLine") or "")),
                ", ".join(str(x) for x in (m.get("mismatchedFlags") or [])),
            ]
            for m in mismatches
        ]
        lines.append(render_table(["Slot", "Item", "Dead Offensive Mod", "Mismatched Flags"], m_rows))
    else:
        lines.append("- None (no offensive flag mismatches detected on active gear).")

    keystones = res.get("keystones") or []
    overrides = res.get("overrides") or []
    if keystones or overrides:
        lines.append("\n#### 3. Keystones & Flag/Override Mechanics")
        if keystones:
            ks_txt = ", ".join(str(k.get("name") or "") for k in keystones if k.get("name"))
            lines.append(f"- **Allocated Keystones**: {ks_txt}")
        if overrides:
            o_rows = [
                [o.get("slot") or "-", o.get("modType") or "-", o.get("modName") or "-", strip_pob_escapes(str(o.get("rawLine") or ""))]
                for o in overrides[:20]
            ]
            lines.append(render_table(["Slot", "Type", "Mod", "Source Line"], o_rows))
    return "\n".join(lines)
