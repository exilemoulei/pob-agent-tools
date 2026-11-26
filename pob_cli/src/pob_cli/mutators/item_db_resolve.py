"""Dropdown/sort resolution, slot aliases, raw variant compaction, and baseline math for PoB `ItemDBControl`."""

from __future__ import annotations

import math
import re
from typing import Any

from .. import game_data as _game_data
from ..bridge_client import BridgeWorker
from ..bridge_protocol import BridgeRejectedError
from ..formatters import strip_pob_escapes
from ..guardrails import normalize_equipment_slot
from .common import _fold_search_text

DB_STAT_SORT_MAX = 300
ITEM_DB_TEMPLATE_NOTE = (
    "注：按 PoB 默认暗金模板试算（固定默认 Roll，未计满 Roll、瓦尔腐化词缀或原部位孔色/影响条件差异）"
)

_TAKEN_HIT_STATS: frozenset[str] = frozenset(
    {
        "PhysicalTakenHit",
        "LightningTakenHit",
        "ColdTakenHit",
        "FireTakenHit",
        "ChaosTakenHit",
    }
)

_GENERIC_MULTI_SLOT_TOKENS: frozenset[str] = frozenset(
    {"ring", "rings", "戒指", "flask", "flasks", "药剂", "药水", "jewel", "jewels", "珠宝"}
)

_DB_SLOT_ALIASES: dict[str, str] = {
    "any": "Any slot", "all": "Any slot", "any slot": "Any slot", "任意": "Any slot", "全部": "Any slot",
    "helmet": "Helmet", "helm": "Helmet", "head": "Helmet", "头盔": "Helmet", "头部": "Helmet",
    "body armour": "Body Armour", "body armor": "Body Armour", "body": "Body Armour", "chest": "Body Armour",
    "胸甲": "Body Armour", "衣服": "Body Armour", "身甲": "Body Armour", "盔甲": "Body Armour",
    "gloves": "Gloves", "glove": "Gloves", "hands": "Gloves", "手套": "Gloves",
    "boots": "Boots", "boot": "Boots", "feet": "Boots", "shoes": "Boots", "鞋子": "Boots", "靴子": "Boots",
    "amulet": "Amulet", "neck": "Amulet", "necklace": "Amulet", "项链": "Amulet", "护身符": "Amulet",
    "ring": "Ring", "rings": "Ring", "ring 1": "Ring", "ring 2": "Ring", "ring 3": "Ring",
    "left ring": "Ring", "right ring": "Ring", "戒指": "Ring", "左戒": "Ring", "右戒": "Ring",
    "左戒指": "Ring", "右戒指": "Ring",
    "belt": "Belt", "waist": "Belt", "腰带": "Belt",
    "jewel": "Jewel", "jewels": "Jewel", "珠宝": "Jewel",
    "flask": "Flask", "flasks": "Flask", "药剂": "Flask", "药水": "Flask",
    "weapon 1": "Weapon 1", "weapon1": "Weapon 1", "mainhand": "Weapon 1", "main hand": "Weapon 1",
    "mh": "Weapon 1", "主手": "Weapon 1", "主手武器": "Weapon 1", "武器1": "Weapon 1",
    "weapon 2": "Weapon 2", "weapon2": "Weapon 2", "offhand": "Weapon 2", "off hand": "Weapon 2",
    "oh": "Weapon 2", "shield": "Weapon 2", "quiver": "Weapon 2",
    "副手": "Weapon 2", "副手武器": "Weapon 2", "盾牌": "Weapon 2", "箭袋": "Weapon 2", "武器2": "Weapon 2",
    "graft 1": "Graft 1", "graft1": "Graft 1", "graft 2": "Graft 2", "graft2": "Graft 2",
    "graft head": "Head", "肢体头部": "Head", "graft body": "Body", "肢体躯干": "Body",
    "arm": "Arm", "graft arm": "Arm", "leg": "Leg", "graft leg": "Leg",
}

_OBTAINABLE_ALIASES: dict[str, str] = {
    "obtainable": "Obtainable", "avail": "Obtainable", "available": "Obtainable", "可获取": "Obtainable",
    "any": "Any source", "all": "Any source", "anysource": "Any source", "全部": "Any source", "任意": "Any source",
    "unobtainable": "Unobtainable", "legacy": "Unobtainable", "retired": "Unobtainable",
    "绝版": "Unobtainable", "不可获取": "Unobtainable",
    "vendor": "Vendor Recipe", "recipe": "Vendor Recipe", "vendorrecipe": "Vendor Recipe", "商店配方": "Vendor Recipe",
    "upgraded": "Upgraded", "upgrade": "Upgraded", "升级": "Upgraded",
    "boss": "Boss Item", "bossitem": "Boss Item", "首领": "Boss Item",
    "corruption": "Corruption", "corrupt": "Corruption", "vaal": "Corruption", "腐化": "Corruption", "瓦尔": "Corruption",
    "core": "Core Drop Pool", "coredrop": "Core Drop Pool", "coredroppool": "Core Drop Pool", "核心掉落": "Core Drop Pool",
}

_REQUIREMENT_ALIASES: dict[str, str] = {
    "any": "Any requirements", "all": "Any requirements", "anyrequirements": "Any requirements",
    "任意": "Any requirements", "无限制": "Any requirements",
    "level": "Current level", "lvl": "Current level", "currentlevel": "Current level", "等级": "Current level",
    "attr": "Current attributes", "attrs": "Current attributes", "attributes": "Current attributes",
    "currentattributes": "Current attributes", "属性": "Current attributes",
    "useable": "Current useable", "usable": "Current useable", "currentuseable": "Current useable",
    "currentusable": "Current useable", "可用": "Current useable", "可穿戴": "Current useable",
}

_SEARCH_MODE_ALIASES: dict[str, str] = {
    "anywhere": "Anywhere", "any": "Anywhere", "all": "Anywhere", "全部": "Anywhere",
    "name": "Names", "names": "Names", "title": "Names", "名称": "Names",
    "mod": "Modifiers", "mods": "Modifiers", "modifier": "Modifiers", "modifiers": "Modifiers", "词缀": "Modifiers",
}

_SORT_ALIASES: dict[str, str] = {
    "name": "Name", "sortbyname": "Name", "名称": "Name", "按名称": "Name", "按名称排序": "Name", "排序名称": "Name",
    "dps": "FullDPS", "fulldps": "FullDPS",
    "combined": "CombinedDPS", "combineddps": "CombinedDPS",
    "hit": "TotalDPS", "hitdps": "TotalDPS", "totaldps": "TotalDPS",
    "impale": "WithImpaleDPS", "withimpaledps": "WithImpaleDPS",
    "avghit": "AverageDamage", "averagehit": "AverageDamage", "averagedamage": "AverageDamage",
    "speed": "Speed", "attackspeed": "Speed", "castspeed": "Speed",
    "dot": "TotalDot", "dotdps": "TotalDot", "totaldot": "TotalDot",
    "bleed": "BleedDPS", "bleeddps": "BleedDPS", "ignite": "IgniteDPS", "ignitedps": "IgniteDPS",
    "poison": "PoisonDPS", "poisondps": "PoisonDPS",
    "ehp": "TotalEHP", "totalehp": "TotalEHP", "effectivehitpool": "TotalEHP",
    "maxhit": "SecondMinimalMaximumHitTaken", "effmaxhit": "SecondMinimalMaximumHitTaken",
    "secondminimalmaximumhittaken": "SecondMinimalMaximumHitTaken",
    "life": "Life", "hp": "Life", "liferegen": "LifeRegen", "lifeleech": "LifeLeechRate",
    "es": "EnergyShield", "energyshield": "EnergyShield", "esregen": "EnergyShieldRegen",
    "mana": "Mana", "manaregen": "ManaRegen", "ward": "Ward",
    "armour": "Armour", "armor": "Armour", "evasion": "Evasion", "ev": "Evasion",
    "str": "Str", "strength": "Str", "dex": "Dex", "dexterity": "Dex", "int": "Int", "intelligence": "Int",
    "attr": "TotalAttr", "attrs": "TotalAttr", "attributes": "TotalAttr", "totalattr": "TotalAttr",
    "crit": "CritChance", "critchance": "CritChance", "critmulti": "CritMultiplier", "critmultiplier": "CritMultiplier",
    "ms": "EffectiveMovementSpeedMod", "movespeed": "EffectiveMovementSpeedMod",
    "block": "BlockChance", "spellblock": "SpellBlockChance",
    "suppress": "SpellSuppressionChance", "suppression": "SpellSuppressionChance",
    "minionlife": "MinionLife", "minionehp": "MinionTotalEHP", "minionspeed": "MinionSpeed",
}

_SEL_VAR_RE = re.compile(r"^(\s*Selected(?:\s+Alt)?\s+Variant[^:]*:\s*)(\d+)\s*$")
_VARIANT_HDR_RE = re.compile(r"^\s*Variant:")
_VARIANT_TAG_RE = re.compile(r"\{variant:([^}]+)\}")


def _compact_key(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", _fold_search_text(text)).lower()


def _compact_active_variants_raw(raw_text: str, max_bytes: int = 60_000) -> str:
    """Compact oversized (>60KB) multi-variant PoB DB raw text (e.g. Pearl of Tsoatha) to active variants."""
    if not raw_text or len(raw_text.encode("utf-8")) <= max_bytes:
        return raw_text
    lines = raw_text.splitlines()
    active_indices: list[int] = []
    has_variant_hdr = False
    for ln in lines:
        if _VARIANT_HDR_RE.match(ln):
            has_variant_hdr = True
        m = _SEL_VAR_RE.match(ln)
        if m:
            active_indices.append(int(m.group(2)))
    if not has_variant_hdr:
        return raw_text
    if not active_indices:
        active_indices = [1]
    kept_old = sorted(set(active_indices))
    remap = {old_i: new_i for new_i, old_i in enumerate(kept_old, start=1)}

    out_lines: list[str] = []
    var_idx = 0
    for ln in lines:
        if _VARIANT_HDR_RE.match(ln):
            var_idx += 1
            if var_idx in remap:
                out_lines.append(ln)
            continue
        m_sel = _SEL_VAR_RE.match(ln)
        if m_sel:
            old_n = int(m_sel.group(2))
            out_lines.append(f"{m_sel.group(1)}{remap.get(old_n, 1)}")
            continue
        m_tag = _VARIANT_TAG_RE.search(ln)
        if m_tag:
            mapped: list[str] = []
            for tok in m_tag.group(1).split(","):
                tok_s = tok.strip()
                if tok_s.isdigit():
                    new_n = remap.get(int(tok_s))
                    if new_n is not None and str(new_n) not in mapped:
                        mapped.append(str(new_n))
            if not mapped:
                continue
            out_lines.append(_VARIANT_TAG_RE.sub(f"{{variant:{','.join(mapped)}}}", ln, count=1))
            continue
        out_lines.append(ln)
    return "\n".join(out_lines) + ("\n" if raw_text.endswith("\n") else "")


def _normalize_db_slot_label(raw_slot: str) -> str:
    """Normalize an equipped or ergonomic slot name into PoB ItemDBControl's slot category."""
    cleaned = _game_data.fold_han(raw_slot.strip())
    low_sp = re.sub(r"[\s_\-]+", " ", cleaned).strip().lower()
    if low_sp in _DB_SLOT_ALIASES:
        return _DB_SLOT_ALIASES[low_sp]
    compact = low_sp.replace(" ", "")
    if compact in _DB_SLOT_ALIASES:
        return _DB_SLOT_ALIASES[compact]
    if re.match(r"^(?:ring|戒指)\s*\d+$", low_sp):
        return "Ring"
    if re.match(r"^(?:flask|药剂|药水)\s*\d+$", low_sp):
        return "Flask"
    if re.match(r"^(?:jewel\b|#\d+|珠宝)", low_sp):
        return "Jewel"
    norm = normalize_equipment_slot(cleaned)
    if norm:
        n_low = norm.lower()
        if n_low in _DB_SLOT_ALIASES:
            return _DB_SLOT_ALIASES[n_low]
        if n_low.startswith("ring "):
            return "Ring"
        if n_low.startswith("flask "):
            return "Flask"
        return norm
    return raw_slot.strip()


def _fetch_item_db_options(worker: BridgeWorker, kind: str) -> dict[str, Any] | None:
    """Fetch & cache `item_db_options{kind}` per worker instance, returning None on legacy/fake workers."""
    cache = getattr(worker, "_item_db_opts_cache", None)
    if isinstance(cache, dict) and kind in cache:
        return cache[kind]
    try:
        opts = worker.call("item_db_options", {"kind": kind}, timeout=30.0)
    except (BridgeRejectedError, KeyError, NotImplementedError, AssertionError):
        opts = None
    result = opts if isinstance(opts, dict) and opts.get("slot") else None
    try:
        if not isinstance(cache, dict):
            cache = {}
            worker._item_db_opts_cache = cache  # type: ignore[attr-defined]
        cache[kind] = result
    except Exception:
        pass
    return result


def _match_dropdown_index(
    options: list[dict[str, Any]] | None,
    wanted: str | None,
    *,
    default_label: str,
    aliases: dict[str, str] | None = None,
    field_name: str,
) -> tuple[int, str]:
    """Match a user filter value against 1-based `item_db_options` dropdown entries."""
    target_raw = (wanted or "").strip()
    is_default = not target_raw
    if is_default:
        target = default_label
    else:
        ck = _compact_key(target_raw)
        target = aliases.get(ck, target_raw) if aliases else target_raw

    if not options:
        return 1, (default_label if is_default else target)

    t_low = target.lower()
    t_fold = _fold_search_text(target)
    t_ck = _compact_key(target)
    for idx, opt in enumerate(options, start=1):
        lbl = strip_pob_escapes(str(opt.get("label") or "")).strip()
        lbl_zh = strip_pob_escapes(str(opt.get("labelZh") or "")).strip()
        if (
            lbl.lower() == t_low
            or (t_fold and t_fold in (_fold_search_text(lbl), _fold_search_text(lbl_zh)))
            or (t_ck and t_ck in (_compact_key(lbl), _compact_key(lbl_zh)))
        ):
            return idx, lbl or target

    if not is_default:
        sub_hits: list[tuple[int, str]] = []
        for idx, opt in enumerate(options, start=1):
            lbl = strip_pob_escapes(str(opt.get("label") or "")).strip()
            lbl_zh = strip_pob_escapes(str(opt.get("labelZh") or "")).strip()
            if (t_fold and t_fold in _fold_search_text(lbl)) or (t_fold and t_fold in _fold_search_text(lbl_zh)):
                sub_hits.append((idx, lbl))
        if len(sub_hits) == 1:
            return sub_hits[0]
        avail = ", ".join(strip_pob_escapes(str(o.get("label") or "")) for o in options[:12])
        raise ValueError(f"Unknown {field_name} '{wanted}'. Available options include: {avail}")

    return 1, strip_pob_escapes(str(options[0].get("label") or default_label))


def _resolve_sort_mode(
    sort_options: list[dict[str, Any]] | None,
    raw_sort: str | None,
) -> tuple[str, str]:
    """Resolve user `--sort` token (alias or exact PoB sortMode/label) to `(sortMode, label)`."""
    tok = (raw_sort or "").strip()
    if not tok:
        return "Name", "Sort by Name"
    ck = _compact_key(tok)
    if _SORT_ALIASES.get(ck) == "Name" or ck == "name":
        return "Name", "Sort by Name"

    entries = sort_options or []
    if ck in ("minion", "miniondps"):
        for opt in entries:
            sm = str(opt.get("sortMode") or "")
            if sm.lower().startswith("minion") and "dps" in sm.lower():
                return sm, strip_pob_escapes(str(opt.get("label") or sm))
        for fallback in ("FullDPS", "CombinedDPS"):
            for opt in entries:
                if str(opt.get("sortMode") or "") == fallback:
                    return fallback, strip_pob_escapes(str(opt.get("label") or fallback))
        return "FullDPS", "Sort by Full DPS"

    canonical = _SORT_ALIASES.get(ck)
    if not entries:
        return (canonical, f"Sort by {canonical}") if canonical else (tok, f"Sort by {tok}")

    want_keys = {k.lower() for k in (canonical, tok) if k}
    want_compact = {_compact_key(k) for k in (canonical, tok) if k}
    for opt in entries:
        sm = str(opt.get("sortMode") or "").strip()
        st = str(opt.get("stat") or "").strip()
        lbl = strip_pob_escapes(str(opt.get("label") or "")).strip()
        lbl_clean = lbl.removeprefix("Sort by ").strip()
        lbl_zh = strip_pob_escapes(str(opt.get("labelZh") or "")).strip()
        cands_low = {c.lower() for c in (sm, st, lbl, lbl_clean) if c}
        cands_ck = {_compact_key(c) for c in (sm, st, lbl, lbl_clean, lbl_zh) if c}
        if (want_keys & cands_low) or (want_compact & cands_ck):
            if sm.upper() == "NAME" or not st:
                return "Name", lbl or "Sort by Name"
            return sm or st, lbl or sm

    sample = ", ".join(str(o.get("sortMode") or "") for o in entries[:14] if o.get("sortMode"))
    raise ValueError(
        f"Unknown --sort metric '{raw_sort}'. Use --list-sorts to inspect all PoB sort keys "
        f"(common: dps, combined, hit, dot, ehp, maxhit, life, es, armour, evasion; engine: {sample})."
    )


def _parse_stat_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return None if math.isnan(f) else f
    if isinstance(val, str):
        s = val.strip().lower()
        if not s or s == "nan":
            return None
        if s in ("inf", "+inf", "infinity", "+infinity"):
            return math.inf
        if s in ("-inf", "-infinity"):
            return -math.inf
        try:
            f = float(s.replace(",", ""))
            return None if math.isnan(f) else f
        except ValueError:
            return None
    return None


def _json_safe_metric(val: float | None) -> float | str | None:
    if val is None or math.isnan(val):
        return None
    if math.isinf(val):
        return "inf" if val > 0 else "-inf"
    return val


def _extract_baseline_for_sort(stats: dict[str, Any], sort_mode: str) -> float | None:
    """Read baseline metric from `get_stats` matching PoB's `data.powerStatList.GetFromOutput`."""
    if not isinstance(stats, dict) or not stats or not sort_mode or sort_mode.upper() == "NAME":
        return None
    if sort_mode == "FullDPS":
        full = _parse_stat_float(stats.get("FullDPS"))
        if full is not None and (math.isinf(full) or full != 0.0):
            return full
        comb = _parse_stat_float(stats.get("CombinedDPS"))
        m_comb = _parse_stat_float(stats.get("MinionCombinedDPS"))
        if comb is None and m_comb is None:
            return full
        return (comb or 0.0) + (m_comb or 0.0)
    if sort_mode.startswith("Minion"):
        inner = sort_mode[len("Minion") :]
        val = _parse_stat_float(stats.get(sort_mode))
        val = 0.0 if val is None else val
        return -val if inner in _TAKEN_HIT_STATS else val
    if sort_mode in ("AverageDamage", "TotalDot") or "DPS" in sort_mode:
        p_val = _parse_stat_float(stats.get(sort_mode))
        m_val = _parse_stat_float(stats.get("Minion" + sort_mode))
        if p_val is None and m_val is None:
            return 0.0
        return (p_val or 0.0) + (m_val or 0.0)
    val = _parse_stat_float(stats.get(sort_mode))
    val = 0.0 if val is None else val
    return -val if sort_mode in _TAKEN_HIT_STATS else val


def _compute_power_delta(
    measured_raw: Any,
    baseline: float | None,
) -> tuple[float | str | None, float | None, float | None]:
    """Compute `(measuredPower, delta, deltaPct)` safely across zero baselines and +/-inf EHP."""
    mp = _parse_stat_float(measured_raw)
    mp_safe = _json_safe_metric(mp)
    if mp is None or baseline is None:
        return mp_safe, None, None
    if math.isinf(mp) or math.isinf(baseline):
        return (mp_safe, 0.0, 0.0) if mp == baseline else (mp_safe, None, None)
    delta = mp - baseline
    delta_pct = (delta / abs(baseline)) * 100.0 if abs(baseline) > 1e-9 else None
    return mp_safe, delta, delta_pct


def _stat_sort_too_many_error(sort_mode: str, total: int | None = None, max_n: int = DB_STAT_SORT_MAX) -> ValueError:
    if total is not None:
        return ValueError(
            f"Too many matching uniques ({total} > {max_n}) for stat sort '{sort_mode}'. "
            f"匹配暗金数量超过上限（{total} > {max_n}），请先缩小筛选范围，例如："
            f"`--slot Helmet`、`--item-type Bow`、`--requirement useable` 或保持默认 `--obtainable obtainable`。"
        )
    return ValueError(
        f"Stat sort '{sort_mode}' requires narrowing the Unique DB below {max_n} items "
        f"(pass `--slot`, `--item-type`, or a `QUERY`). "
        f"按属性排序需先缩小范围（最多 {max_n} 件），例如：`--slot Helmet`、`--item-type Bow`、`--requirement useable`。"
    )


def _resolve_db_rpc_plan(
    worker: BridgeWorker,
    *,
    kind: str,
    slot_name: str | None,
    item_type: str | None,
    league: str | None,
    requirement: str | None,
    obtainable: str | None,
    search_mode: str | None,
    sort_mode: str | None,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """Resolve all ItemDBControl dropdown indices + sortMode via cached `item_db_options` labels."""
    has_any_filter = any((slot_name, item_type, league, requirement, obtainable, search_mode, sort_mode))
    if kind == "rare":
        if sort_mode and _resolve_sort_mode(None, sort_mode)[0].upper() != "NAME":
            raise ValueError("Stat-based --sort is only supported for UNIQUE database (`--type UNIQUE`).")
        if league or requirement or obtainable:
            raise ValueError("--league, --requirement, and --obtainable are only supported for UNIQUE database.")

    opts = _fetch_item_db_options(worker, kind)
    if opts is None:
        if has_any_filter:
            raise ValueError(
                "This PoB engine does not expose `item_db_options` required for `--slot`/`--sort` filtering "
                "(run `pob bootstrap` to upgrade PoB engine)."
            )
        base_rpc: dict[str, Any] = {"kind": kind, "slot": 1, "type": 1, "searchMode": 1}
        filters: dict[str, Any] = {"slot": "Any slot", "itemType": "Any type", "searchMode": "Anywhere"}
        if kind == "unique":
            base_rpc.update({"league": 1, "requirement": 1, "obtainable": 1, "sortMode": "Name"})
            filters.update({"league": "Any league", "requirement": "Any requirements", "obtainable": "Obtainable"})
        return base_rpc, filters, "Name", "Sort by Name"

    norm_slot = _normalize_db_slot_label(slot_name) if slot_name else None
    slot_idx, slot_lbl = _match_dropdown_index(opts.get("slot"), norm_slot, default_label="Any slot", field_name="slot")
    type_idx, type_lbl = _match_dropdown_index(
        opts.get("type"), item_type, default_label="Any type", field_name="item-type"
    )
    sm_idx, sm_lbl = _match_dropdown_index(
        opts.get("searchMode"), search_mode, default_label="Anywhere", aliases=_SEARCH_MODE_ALIASES, field_name="search-mode"
    )
    base_rpc = {"kind": kind, "slot": slot_idx, "type": type_idx, "searchMode": sm_idx}
    filters = {"slot": slot_lbl, "itemType": type_lbl, "searchMode": sm_lbl}

    if kind == "unique":
        lg_idx, lg_lbl = _match_dropdown_index(opts.get("league"), league, default_label="Any league", field_name="league")
        req_idx, req_lbl = _match_dropdown_index(
            opts.get("requirement"), requirement, default_label="Any requirements", aliases=_REQUIREMENT_ALIASES, field_name="requirement"
        )
        obt_idx, obt_lbl = _match_dropdown_index(
            opts.get("obtainable"), obtainable, default_label="Obtainable", aliases=_OBTAINABLE_ALIASES, field_name="obtainable"
        )
        resolved_sort, sort_label = _resolve_sort_mode(opts.get("sort"), sort_mode)
        base_rpc.update({"league": lg_idx, "requirement": req_idx, "obtainable": obt_idx, "sortMode": resolved_sort})
        filters.update({"league": lg_lbl, "requirement": req_lbl, "obtainable": obt_lbl})
        return base_rpc, filters, resolved_sort, sort_label

    return base_rpc, filters, "Name", "Sort by Name"
