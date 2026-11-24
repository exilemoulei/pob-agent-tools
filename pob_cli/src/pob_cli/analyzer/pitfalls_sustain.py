"""Seven-link, ascendancy flask sustain, Ramako, Frost Bomb, and Spectre mana synergy detectors."""

from __future__ import annotations

from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from .pitfalls_defence import _gem_matches
from .pitfalls_offence import _item_names_low
from .summary import _active_item_lines, _normalize_support_base_name


def _check_seven_link_and_class_synergies(
    ctx: dict[str, Any],
    core_synergies: list[str],
    amulet_7th_link: list[str],
) -> None:
    main_group = ctx["main_group"]
    groups = ctx["groups"]
    equipped_names_all = ctx["equipped_names_all"]
    all_active_non_swap_low = ctx["all_active_non_swap_low"]
    asc_names_lower = ctx["asc_names_lower"]
    asc_ids = ctx["asc_ids"]

    amulet_item = ctx["active_items_by_slot"].get("Amulet") or {}
    amulet_names_low = _item_names_low(amulet_item)
    has_uul_netols_vow = any(
        tok in amulet_names_low for tok in ("uul-netol's vow", "uul-netols vow", "乌尔尼多之誓", "烏爾尼多之誓")
    )
    amulet_7th_link_gems: list[dict[str, Any]] = []
    main_slot_low = str((main_group or {}).get("slot") or "").strip().lower()
    if has_uul_netols_vow and main_group and main_group.get("enabled", True) and main_slot_low == "body armour":
        for g in groups:
            if not isinstance(g, dict) or not g.get("enabled", True):
                continue
            if str(g.get("slot") or "").strip().lower() != "amulet":
                continue
            for gm in g.get("gems") or []:
                if not isinstance(gm, dict) or not gm.get("enabled", True) or not gm.get("support"):
                    continue
                amulet_7th_link_gems.append(gm)
                nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
                nm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip()
                lv = gm.get("level")
                ql = gm.get("quality")
                meta = f" ({lv}/{ql}%)" if (lv is not None and ql is not None) else (f" (Lv.{lv})" if lv is not None else "")
                disp = f"{nm_zh} ({nm}){meta}" if (nm_zh and nm and nm_zh != nm) else f"{nm or nm_zh}{meta}"
                if disp:
                    amulet_7th_link.append(disp)

    if main_group and main_group.get("enabled", True):
        mg_enabled_gems = [gm for gm in (main_group.get("gems") or []) if isinstance(gm, dict) and gm.get("enabled", True)]
        effective_main_supports = [gm for gm in mg_enabled_gems if gm.get("support")] + amulet_7th_link_gems
        has_returning_proj = any(
            _gem_matches(
                gm,
                skill_ids=("SupportReturningProjectiles",),
                tokens=("returning projectiles", "投射物归返", "投射物返回"),
            )
            for gm in effective_main_supports
        )

        def _is_pierce_support(gm: dict[str, Any]) -> bool:
            gsid = str(gm.get("skillId") or "")
            if gsid in ("SupportPierce", "SupportAwakenedPierce", "SupportGreaterPierce"):
                return True
            en_nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            if _normalize_support_base_name(en_nm) in ("pierce", "greater pierce", "awakened pierce"):
                return True
            zh_nm = strip_pob_escapes(str(gm.get("nameZh") or "")).strip().lower()
            return "投射物穿透" in zh_nm or zh_nm in ("穿透", "穿透(辅)", "穿透（辅）", "穿透辅助", "穿透輔助")

        if has_returning_proj and any(_is_pierce_support(gm) for gm in effective_main_supports):
            core_synergies.append(
                "[7L SYNERGY] Returning Projectiles + Pierce: Pierce is required so projectiles pass through "
                "targets and hit again on return — do NOT drop Pierce based purely on raw 1-hit PoB tooltip DPS!"
            )

        mg_active_gems = [gm for gm in mg_enabled_gems if not gm.get("support")]
        mg_trigger_supports = [
            gm
            for gm in mg_enabled_gems
            if gm.get("support")
            and _gem_matches(
                gm,
                skill_ids=(
                    "SupportCastWhenStunned",
                    "SupportCastOnCriticalStrike",
                    "SupportAwakenedCastOnCriticalStrike",
                    "SupportCastWhileChannelling",
                    "SupportAwakenedCastWhileChannelling",
                ),
                tokens=(
                    "cast when stunned", "晕眩时施放", "暈眩時施放",
                    "cast on critical strike", "暴击时施放", "暴擊時施放",
                    "cast while channelling", "吟唱时施放", "引導時施放",
                ),
            )
        ]
        has_multi_active_trigger = len(mg_active_gems) >= 2 and bool(mg_trigger_supports)
    else:
        has_multi_active_trigger = False

    has_hinekora = 1731 in asc_ids or any(
        any(tok in nm for tok in ("hinekora", "欣嫩", "海妮可拉", "死神之怒"))
        for nm in asc_names_lower
    )
    has_penance_mark = any(
        any(tok in nm for tok in ("penance mark", "忏悔印记", "懺悔印記"))
        for nm in all_active_non_swap_low
    ) or any(
        "penance mark" in ln.lower()
        for raw_txt in ctx["raw_by_slot"].values()
        for ln in _active_item_lines(raw_txt)
    )
    has_hateful_accuser = any(
        any(tok in nl for tok in ("the hateful accuser", "憎恨控诉", "憎恨控訴", "憎惡控告者"))
        for nl in equipped_names_all.values()
    )
    has_detonate_dead = any(
        any(tok in nm for tok in ("detonate dead", "爆灵术", "爆靈術", "尸体爆破", "屍體爆破"))
        for nm in all_active_non_swap_low
    )
    if has_multi_active_trigger or (has_hinekora and (has_penance_mark or has_hateful_accuser or has_detonate_dead)):
        core_synergies.append(
            "[MULTI-ACTIVE / TRIGGER ARCHITECTURE] PoB's single-skill CombinedDPS only evaluates ONE active skill "
            "at a time — secondary active skills showing 0 isolated DPS delta are often essential "
            "multi-hit/phantasm-kill/corpse-explosion/burning-ground enablers (NOT dead links!)."
        )

    _check_ascendancy_and_utility_synergies(ctx, core_synergies)


def _check_ascendancy_and_utility_synergies(ctx: dict[str, Any], core_synergies: list[str]) -> None:
    build_info = ctx["build_info"]
    asc_names_lower = ctx["asc_names_lower"]
    asc_ids = ctx["asc_ids"]
    equipped_names_all = ctx["equipped_names_all"]
    all_active_non_swap_low = ctx["all_active_non_swap_low"]
    main_group = ctx["main_group"]

    asc_cls_en = strip_pob_escapes(str((build_info or {}).get("ascendClassName") or "")).strip().lower()
    asc_cls_zh = strip_pob_escapes(str((build_info or {}).get("ascendClassNameZh") or "")).strip().lower()
    is_pathfinder = (
        "pathfinder" in asc_cls_en
        or "追猎者" in asc_cls_zh
        or "追獵者" in asc_cls_zh
        or "pathfinder" in ctx["asc_classes_lower"]
    )
    if is_pathfinder:
        has_natures_adrenaline = any(
            tok in nm for nm in asc_names_lower for tok in ("nature's adrenaline", "自然肾上腺素", "自然腎上腺素")
        )
        has_master_surgeon = any(
            tok in nm for nm in asc_names_lower for tok in ("master surgeon", "外科大师", "外科大師")
        )
        has_tides_of_time = any(
            any(tok in nl for tok in ("the tides of time", "时光之潮", "時間之潮", "時光浪潮"))
            for nl in equipped_names_all.values()
        )
        has_petrified_blood = any(
            any(tok in nm for tok in ("petrified blood", "石化之血"))
            for nm in all_active_non_swap_low
        )
        has_progenesis = any(
            any(tok in nl for tok in ("progenesis", "创生", "創生", "先祖之源"))
            for nl in equipped_names_all.values()
        )
        pf_tags: list[str] = []
        if has_natures_adrenaline:
            pf_tags.append("Nature's Adrenaline")
        if has_tides_of_time:
            pf_tags.append("The Tides of Time")
        if has_master_surgeon:
            pf_tags.append("Master Surgeon")
        if has_petrified_blood:
            pf_tags.append("Petrified Blood")
        if has_progenesis:
            pf_tags.append("Progenesis")
        pf_detail = f" ({', '.join(pf_tags)})" if pf_tags else ""
        stripped_flasks = ctx["stripped_flasks"]
        if stripped_flasks:
            core_synergies.append(
                f"[PATHFINDER PERMANENT FLASKS] Dewater stripped {len(stripped_flasks)} active flask(s), "
                f"but Pathfinder sustains permanent flasks in boss fights{pf_detail} — re-run with `--keep flasks` "
                "for realistic boss EHP. Note Master Surgeon + Petrified Blood + Progenesis synergy: "
                "do NOT cut Petrified Blood solely because of its 50% non-flask life cap!"
            )
        elif has_master_surgeon and (has_petrified_blood or has_progenesis):
            core_synergies.append(
                "[PATHFINDER FLASK SYNERGY] Master Surgeon + Petrified Blood + Progenesis forms a core Pathfinder "
                "defensive loop (Life Flask recovers above 50% life) — do NOT cut Petrified Blood solely because "
                "of its 50% non-flask life cap!"
            )

    has_ramako = (61355 in asc_ids) or any(
        tok in nm for nm in asc_names_lower for tok in ("ramako", "雷默科", "烈阳之光", "烈陽之光", "太阳之光", "太陽之光")
    )
    if has_ramako:
        core_synergies.append(
            "[RAMAKO FIXED -20% DOT RES] Chieftain 'Ramako, Sun's Light' sets nearby enemies' Fire Resistance "
            "against DoT to a FIXED -20% while stationary — Flammability, Fire Exposure, and Scorch have ZERO effect "
            "on Fire DoT while standing still (this is why Punishment is preferred over Flammability!)."
        )

    mg_active_low = " ".join(
        strip_pob_escapes(str(gm.get(k) or ""))
        for gm in ((main_group.get("gems") or []) if isinstance(main_group, dict) else [])
        if isinstance(gm, dict) and gm.get("enabled", True) and not gm.get("support")
        for k in ("name", "nameSpec", "nameZh")
    ).lower()
    has_frost_bomb = any(
        tok in nm for nm in all_active_non_swap_low for tok in ("frost bomb", "寒霜爆")
    )
    main_is_cold = any(
        tok in mg_active_low for tok in ("ice ", "frost", "cold", "vortex", "creeping", "winter", "glacial", "冰", "寒", "冻")
    )
    if has_frost_bomb and not main_is_cold:
        core_synergies.append(
            "[UTILITY SKILL: FROST BOMB] Frost Bomb is NOT a curse (does not compete with curse limit) — "
            "in non-Cold builds it provides -75% Enemy Life Regeneration against Maven/Essence/Harvest/regen rares."
        )

    main_is_spectre = any(tok in mg_active_low for tok in ("raise spectre", "召唤灵体", "喚醒幽魂"))
    mana_unres_val = _finite_float(ctx["stats"].get("ManaUnreserved"), default=0.0)
    lifetap_group_count = 0
    for grp in ctx["groups"]:
        if not isinstance(grp, dict) or not grp.get("enabled", True):
            continue
        if any(
            any(
                tok in " ".join(strip_pob_escapes(str(gm.get(k) or "")) for k in ("name", "nameSpec", "nameZh")).lower()
                for tok in ("lifetap", "赤炼", "赤煉", "活栓")
            )
            for gm in (grp.get("gems") or [])
            if isinstance(gm, dict) and gm.get("enabled", True)
        ):
            lifetap_group_count += 1
    if main_is_spectre and ctx["main_mana_cost"] > 0 and mana_unres_val < ctx["main_mana_cost"] and lifetap_group_count >= 2:
        core_synergies.append(
            f"[SPECTRE HIDEOUT CAST] Raise Spectre ({round(ctx['main_mana_cost'])} Mana) is only cast once in hideout "
            f"(temporarily toggling off an aura) — {lifetap_group_count} utility groups use Lifetap (Life cost), "
            "so do NOT require Raise Spectre's cast cost to fit inside active in-combat unreserved Mana."
        )
