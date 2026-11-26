# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Trilingual (zh-CN / zh-TW / EN) PoE2 item clipboard parser, affix inspector, and 0.5.5 health check."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import (
    AffixSide,
    CraftingMod,
    CraftingState,
    ItemRarity,
    ModDomain,
    ModEntry,
    WeightConfidence,
    WhittlingReport,
)
from .pool_engine import ModPoolEngine
from .weights_loader import strip_numbers_to_pattern

_RICH_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_RANGE_OR_NUM_RE = re.compile(
    r"\((?P<lo>[+-]?\d+(?:\.\d+)?)\s*-\s*(?P<hi>[+-]?\d+(?:\.\d+)?)\)"
    r"|(?P<num>[+-]?\d+(?:\.\d+)?)"
)
_CTRL_ALT_HEADER_RE = re.compile(
    r"\{\s*(Prefix|Suffix)\s+Modifier\s+\"([^\"]+)\"\s+\(Tier:\s*(\d+)\)",
    re.IGNORECASE,
)

_ZH_RARITY: dict[str, ItemRarity] = {
    "普通": ItemRarity.NORMAL,
    "魔法": ItemRarity.MAGIC,
    "稀有": ItemRarity.RARE,
    "传奇": ItemRarity.UNIQUE,
    "傳奇": ItemRarity.UNIQUE,
    "暗金": ItemRarity.UNIQUE,
    "normal": ItemRarity.NORMAL,
    "magic": ItemRarity.MAGIC,
    "rare": ItemRarity.RARE,
    "unique": ItemRarity.UNIQUE,
}

_CATALYST_ZH_MAP: dict[str, str] = {
    "火焰": "fire",
    "冰霜": "cold",
    "闪电": "lightning",
    "閃電": "lightning",
    "混沌": "chaos",
    "物理": "physical",
    "生命": "life",
    "魔力": "mana",
    "防御": "defences",
    "防禦": "defences",
    "攻击": "attack",
    "攻擊": "attack",
    "施法": "caster",
    "属性": "attribute",
    "屬性": "attribute",
    "速度": "speed",
}

_POE1_ILLEGAL_LINE_RE = re.compile(
    r"prefixes cannot be changed|suffixes cannot be changed|can have up to 3 crafted modifiers|"
    r"cannot roll attack modifiers|cannot roll caster modifiers|前缀无法被改变|后缀无法被改变|"
    r"最多可以有\s*3\s*个工艺词缀",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class InspectedMod:
    raw_text: str
    mod_id: str
    affix_side: AffixSide
    source_pool: str
    family_id: str
    display_tier: int
    required_level: int
    is_crafted: bool
    fractured: bool
    is_desecrated: bool
    resolved: bool
    weight_confidence: WeightConfidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "rawText": self.raw_text,
            "modId": self.mod_id,
            "affixSide": self.affix_side.value,
            "sourcePool": self.source_pool,
            "familyId": self.family_id,
            "displayTier": self.display_tier,
            "requiredLevel": self.required_level,
            "isCrafted": self.is_crafted,
            "fractured": self.fractured,
            "isDesecrated": self.is_desecrated,
            "resolved": self.resolved,
            "weightConfidence": self.weight_confidence.value,
        }


@dataclass(slots=True)
class ItemInspectionReport:
    patch: str
    rarity: ItemRarity
    item_name: str
    base_type: str
    base_slug: str
    ilvl: int
    quality: int
    catalyst_tag: str | None
    corrupted: bool
    sanctified: bool
    max_prefixes: int
    max_suffixes: int
    prefixes: list[InspectedMod] = field(default_factory=list)
    suffixes: list[InspectedMod] = field(default_factory=list)
    implicits: list[str] = field(default_factory=list)
    crafted_mod_count: int = 0
    desecrated_mod_count: int = 0
    fractured_crafted_locked: bool = False
    is_legal_0_5_5: bool = True
    legality_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    whittling_preview: WhittlingReport | None = None
    state: CraftingState | None = None

    @property
    def open_prefixes(self) -> int:
        return max(0, self.max_prefixes - len(self.prefixes))

    @property
    def open_suffixes(self) -> int:
        return max(0, self.max_suffixes - len(self.suffixes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "patch": self.patch,
            "rarity": self.rarity.value,
            "itemName": self.item_name,
            "baseType": self.base_type,
            "baseSlug": self.base_slug,
            "ilvl": self.ilvl,
            "quality": self.quality,
            "catalystTag": self.catalyst_tag,
            "corrupted": self.corrupted,
            "sanctified": self.sanctified,
            "slots": {
                "prefixes": len(self.prefixes),
                "maxPrefixes": self.max_prefixes,
                "openPrefixes": self.open_prefixes,
                "suffixes": len(self.suffixes),
                "maxSuffixes": self.max_suffixes,
                "openSuffixes": self.open_suffixes,
            },
            "craftedSlot": {
                "used": self.crafted_mod_count,
                "limit": 1,
                "fracturedLocked": self.fractured_crafted_locked,
                "canAddCrafted": self.crafted_mod_count == 0 and not self.corrupted and not self.sanctified,
            },
            "desecratedSlot": {
                "used": self.desecrated_mod_count,
                "limit": 1,
                "canDesecrate": self.desecrated_mod_count == 0 and not self.corrupted and not self.sanctified,
            },
            "isLegal055": self.is_legal_0_5_5,
            "legalityIssues": list(self.legality_issues),
            "warnings": list(self.warnings),
            "prefixes": [m.to_dict() for m in self.prefixes],
            "suffixes": [m.to_dict() for m in self.suffixes],
            "implicits": list(self.implicits),
            "whittlingPreview": self.whittling_preview.to_dict() if self.whittling_preview else None,
        }


def _clean_rich_markup(text: str) -> str:
    return _RICH_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)


def _extract_values(text: str) -> list[float]:
    vals: list[float] = []
    for m in _RANGE_OR_NUM_RE.finditer(text):
        if m.group("num") is not None:
            vals.append(abs(float(m.group("num"))))
    return vals


def _extract_bounds(stat_text: str) -> list[tuple[float, float]]:
    bounds: list[tuple[float, float]] = []
    for m in _RANGE_OR_NUM_RE.finditer(stat_text):
        if m.group("lo") is not None and m.group("hi") is not None:
            a, b = abs(float(m.group("lo"))), abs(float(m.group("hi")))
            bounds.append((min(a, b), max(a, b)))
        elif m.group("num") is not None:
            v = abs(float(m.group("num")))
            bounds.append((v, v))
    return bounds


def _detect_domain_and_strip(raw_line: str) -> tuple[str, ModDomain, bool, bool, bool]:
    """Return `(cleaned_text, domain, fractured, is_crafted, is_desecrated)`."""
    s = _clean_rich_markup(raw_line).strip()
    fractured = False
    is_crafted = False
    is_desecrated = False
    domain = ModDomain.EXPLICIT

    for tag in ("{fractured}", "(fractured)", "(破裂)", "（破裂）"):
        if tag in s.lower() or tag in s:
            fractured = True
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag in ("{crafted}", "(crafted)", "(工艺)", "（工艺）", "(工藝)", "（工藝）"):
        if tag in s.lower() or tag in s:
            is_crafted = True
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag in ("{desecrated}", "(desecrated)", "(亵渎)", "（亵渎）", "(褻瀆)", "（褻瀆）"):
        if tag in s.lower() or tag in s:
            is_desecrated = True
            domain = ModDomain.DESECRATED
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    for tag, dom in (
        ("{implicit}", ModDomain.IMPLICIT),
        ("(implicit)", ModDomain.IMPLICIT),
        ("(基底)", ModDomain.IMPLICIT),
        ("(固定词缀)", ModDomain.IMPLICIT),
        ("(固定詞綴)", ModDomain.IMPLICIT),
        ("{enchant}", ModDomain.ENCHANT),
        ("(enchant)", ModDomain.ENCHANT),
        ("(附魔)", ModDomain.ENCHANT),
        ("{rune}", ModDomain.RUNE),
        ("(rune)", ModDomain.RUNE),
        ("(符文)", ModDomain.RUNE),
    ):
        if tag in s.lower() or tag in s:
            domain = dom
            s = re.sub(re.escape(tag), "", s, flags=re.IGNORECASE).strip()
    if fractured and domain == ModDomain.EXPLICIT:
        domain = ModDomain.FRACTURED
    elif is_crafted and domain == ModDomain.EXPLICIT:
        domain = ModDomain.CRAFTED
    return s.strip(), domain, fractured, is_crafted, is_desecrated


def _parse_catalyst_tag(key_raw: str) -> str | None:
    m = re.search(r"[（(]([^）)]+)[）)]", key_raw)
    if not m:
        return None
    inner = m.group(1).strip().lower()
    for zh, en in _CATALYST_ZH_MAP.items():
        if zh in inner:
            return en
    word = inner.replace("modifiers", "").replace("词缀", "").replace("詞綴", "").strip()
    return word.split()[0] if word else None


def _pick_best_entry(
    candidates: list[ModEntry],
    *,
    ilvl: int,
    side_hint: AffixSide | None,
    tier_hint: int | None,
    name_hint: str | None,
    is_crafted: bool,
    is_desecrated: bool,
    vals: list[float],
    catalyst_scalar: float,
) -> ModEntry | None:
    filtered = [m for m in candidates if side_hint is None or m.affix_side == side_hint] or candidates
    if not filtered:
        return None
    best: ModEntry | None = None
    best_key: tuple[float, float, int] | None = None
    for m in filtered:
        score = 0.0
        if is_desecrated:
            score += 140.0 if m.source_pool == "desecrated" else 20.0
        elif is_crafted:
            score += 140.0 if m.is_crafted_pool else 20.0
        else:
            score += 100.0 if m.source_pool == "normal" else (35.0 if m.is_crafted_pool else 15.0)
        score += 30.0 if m.required_level <= ilvl else -180.0
        if name_hint and m.affix_name and m.affix_name.lower() == name_hint.lower():
            score += 400.0
        if tier_hint is not None:
            score += 300.0 if m.display_tier == tier_hint else -100.0 * abs(m.display_tier - tier_hint)
        bounds = _extract_bounds(m.stat_text)
        dist = 0.0
        if vals and bounds:
            all_in = True
            for v, (lo, hi) in zip(vals, bounds, strict=False):
                d_raw = 0.0 if lo <= v <= hi else min(abs(v - lo), abs(v - hi))
                if catalyst_scalar > 1.0:
                    vu = v / catalyst_scalar
                    d_cat = 0.0 if lo <= vu <= hi else min(abs(vu - lo), abs(vu - hi))
                    d_best = min(d_raw, d_cat)
                else:
                    d_best = d_raw
                if d_best > 0.0:
                    all_in = False
                    dist += d_best
            score += 320.0 if all_in else -min(500.0, dist * 8.0)
        ck = (score, -dist, -m.display_tier)
        if best_key is None or ck > best_key:
            best_key = ck
            best = m
    return best


@dataclass(slots=True)
class _ParsedClipboard:
    rarity: ItemRarity
    ilvl: int
    quality: int
    catalyst_tag: str | None
    corrupted: bool
    sanctified: bool
    header_names: list[str]
    implicits: list[str]
    raw_mod_lines: list[tuple[str, AffixSide | None, str | None, int | None]]
    legality_issues: list[str]


def _parse_clipboard_sections(raw_text: str, ilvl_override: int | None) -> _ParsedClipboard:
    rarity = ItemRarity.RARE
    ilvl = ilvl_override or 82
    quality = 0
    catalyst_tag: str | None = None
    corrupted = sanctified = False
    header_names: list[str] = []
    implicits: list[str] = []
    raw_mod_lines: list[tuple[str, AffixSide | None, str | None, int | None]] = []
    legality_issues: list[str] = []

    sections = [
        [ln.strip() for ln in sec.splitlines() if ln.strip()]
        for sec in re.split(r"(?m)^-{4,}\s*$", raw_text.strip())
    ]
    sections = [s for s in sections if s]
    pending_side: AffixSide | None = None
    pending_name: str | None = None
    pending_tier: int | None = None
    in_pob_implicits = 0

    for sec_idx, sec in enumerate(sections):
        for ln in sec:
            clean_ln = _clean_rich_markup(ln).strip()
            low = clean_ln.lower()
            hm = _CTRL_ALT_HEADER_RE.search(clean_ln)
            if hm:
                pending_side = AffixSide.PREFIX if hm.group(1).lower() == "prefix" else AffixSide.SUFFIX
                pending_name = hm.group(2)
                pending_tier = int(hm.group(3))
                continue
            if _POE1_ILLEGAL_LINE_RE.search(clean_ln):
                legality_issues.append(f"检测到 PoE1 工艺台元词缀（PoE2 0.5.5 不存在）: {clean_ln}")
                continue
            if low in ("corrupted", "已腐化", "已污染", "已汙染"):
                corrupted = True
                continue
            if low in ("sanctified", "已神圣化", "已聖化"):
                sanctified = True
                continue
            if ":" in clean_ln or "：" in clean_ln:
                k_raw, v_raw = clean_ln.replace("：", ":", 1).split(":", 1)
                k_comp = re.sub(r"\s+", "", k_raw).lower()
                v_str = v_raw.strip()
                if k_comp in ("rarity", "稀有度"):
                    rarity = _ZH_RARITY.get(v_str.lower(), _ZH_RARITY.get(v_str, ItemRarity.RARE))
                    continue
                if k_comp in ("itemlevel", "物品等级", "物品等級"):
                    nm = re.search(r"\d+", v_str)
                    if nm and ilvl_override is None:
                        ilvl = int(nm.group(0))
                    continue
                if k_comp.startswith(("quality", "品质", "品質")):
                    nm = re.search(r"\d+", v_str)
                    if nm:
                        quality = int(nm.group(0))
                    cat = _parse_catalyst_tag(k_raw)
                    if cat:
                        catalyst_tag = cat
                    continue
                if k_comp == "implicits":
                    nm = re.search(r"\d+", v_str)
                    in_pob_implicits = int(nm.group(0)) if nm else 0
                    continue
                if k_comp in (
                    "itemclass", "物品类别", "物品類別", "sockets", "插槽", "levelreq", "level",
                    "str", "dex", "int", "armour", "evasion", "evasionrating", "energyshield",
                    "ward", "spirit", "uniqueid", "rune", "charmslots", "requirements", "requires",
                    "物理伤害", "元素伤害", "护甲", "闪避值", "能量护盾", "需求", "等级",
                ):
                    continue
            if sec_idx == 0 and in_pob_implicits == 0 and not clean_ln.startswith(("{", "+", "-")):
                header_names.append(clean_ln)
                continue
            if in_pob_implicits > 0:
                in_pob_implicits -= 1
                implicits.append(clean_ln)
                continue
            raw_mod_lines.append((clean_ln, pending_side, pending_name, pending_tier))
            pending_side = pending_name = pending_tier = None

    return _ParsedClipboard(
        rarity=rarity, ilvl=ilvl, quality=quality, catalyst_tag=catalyst_tag,
        corrupted=corrupted, sanctified=sanctified, header_names=header_names,
        implicits=implicits, raw_mod_lines=raw_mod_lines, legality_issues=legality_issues,
    )


def inspect_item_text(
    raw_text: str,
    engine: ModPoolEngine | None = None,
    *,
    base_override: str | None = None,
    ilvl_override: int | None = None,
) -> ItemInspectionReport:
    """Parse PoE2 Ctrl+C / Ctrl+Alt+C / PoB2 item text and run full 0.5.5 affix & legality audit."""
    eng = engine or ModPoolEngine()
    clip = _parse_clipboard_sections(raw_text, ilvl_override)
    rarity, ilvl, quality, catalyst_tag = clip.rarity, clip.ilvl, clip.quality, clip.catalyst_tag
    corrupted, sanctified = clip.corrupted, clip.sanctified
    header_names, implicits = clip.header_names, clip.implicits
    raw_mod_lines, legality_issues = clip.raw_mod_lines, clip.legality_issues
    warnings: list[str] = []

    if base_override:
        base_str = base_override
        item_name = header_names[0] if header_names else base_override
    elif rarity in (ItemRarity.RARE, ItemRarity.UNIQUE) and len(header_names) >= 2:
        item_name, base_str = header_names[0], header_names[1]
    elif header_names:
        item_name = base_str = header_names[-1]
    else:
        raise ValueError("Unable to determine item base type; pass --base explicitly.")

    base = eng.resolve_base(base_str)
    all_tiers = eng.get_mod_tiers(base.poe2db_slug)
    pat_map: dict[str, list[ModEntry]] = {}
    for m in all_tiers:
        if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX):
            pat_map.setdefault(strip_numbers_to_pattern(m.stat_text), []).append(m)

    cat_scalar = 1.0 + (quality / 100.0) if (catalyst_tag and quality > 0) else 1.0
    prefixes: list[InspectedMod] = []
    suffixes: list[InspectedMod] = []
    craft_mods: list[CraftingMod] = []

    idx = 0
    while idx < len(raw_mod_lines):
        ln_txt, s_hint, n_hint, t_hint = raw_mod_lines[idx]
        clean_mod, dom, frac, is_cr, is_des = _detect_domain_and_strip(ln_txt)
        if dom in (ModDomain.IMPLICIT, ModDomain.ENCHANT, ModDomain.RUNE):
            implicits.append(ln_txt)
            idx += 1
            continue
        chosen: ModEntry | None = None
        consumed = 1
        # Try 2-line hybrid mod match
        if idx + 1 < len(raw_mod_lines):
            nxt_txt, _, _, _ = raw_mod_lines[idx + 1]
            nxt_clean, nxt_dom, nxt_frac, nxt_cr, nxt_des = _detect_domain_and_strip(nxt_txt)
            if (nxt_dom, nxt_frac, nxt_cr, nxt_des) == (dom, frac, is_cr, is_des):
                combo = f"{clean_mod}\n{nxt_clean}"
                cands = pat_map.get(strip_numbers_to_pattern(combo), [])
                if cands:
                    chosen = _pick_best_entry(
                        cands, ilvl=ilvl, side_hint=s_hint, tier_hint=t_hint, name_hint=n_hint,
                        is_crafted=is_cr, is_desecrated=is_des,
                        vals=_extract_values(combo), catalyst_scalar=cat_scalar,
                    )
                    if chosen is not None:
                        clean_mod = combo
                        consumed = 2
        if chosen is None:
            cands = pat_map.get(strip_numbers_to_pattern(clean_mod), [])
            chosen = _pick_best_entry(
                cands, ilvl=ilvl, side_hint=s_hint, tier_hint=t_hint, name_hint=n_hint,
                is_crafted=is_cr, is_desecrated=is_des,
                vals=_extract_values(clean_mod), catalyst_scalar=cat_scalar,
            )

        if chosen is not None:
            eff_crafted = is_cr or chosen.is_crafted_pool
            eff_desec = is_des or (chosen.source_pool == "desecrated")
            cm = CraftingMod.from_entry(
                chosen, fractured=frac, is_crafted=eff_crafted, is_desecrated=eff_desec
            )
            im = InspectedMod(
                raw_text=clean_mod, mod_id=chosen.mod_id, affix_side=chosen.affix_side,
                source_pool=chosen.source_pool, family_id=chosen.family_id,
                display_tier=chosen.display_tier, required_level=chosen.required_level,
                is_crafted=eff_crafted, fractured=frac, is_desecrated=eff_desec,
                resolved=True, weight_confidence=chosen.weight_confidence,
            )
        else:
            side = s_hint or (AffixSide.PREFIX if len(prefixes) < base.max_prefixes else AffixSide.SUFFIX)
            syn = f"Unresolved_{idx}"
            cm = CraftingMod(
                mod_id=syn, mod_type=syn, family_id=syn, families=(syn,), affix_side=side,
                display_tier=t_hint or 1, required_level=1, stat_text=clean_mod,
                fractured=frac, is_crafted=is_cr, is_desecrated=is_des,
            )
            im = InspectedMod(
                raw_text=clean_mod, mod_id=syn, affix_side=side, source_pool="unknown",
                family_id=syn, display_tier=t_hint or 1, required_level=1,
                is_crafted=is_cr, fractured=frac, is_desecrated=is_des,
                resolved=False, weight_confidence=WeightConfidence.UNMEASURED,
            )
            warnings.append(f"未能从 0.5.5 词缀池精确匹配（保留原文）: {clean_mod}")
        craft_mods.append(cm)
        (prefixes if im.affix_side == AffixSide.PREFIX else suffixes).append(im)
        idx += consumed

    state = CraftingState(
        base=base, ilvl=ilvl, rarity=rarity, mods=craft_mods,
        quality=quality, catalyst_tag=catalyst_tag, corrupted=corrupted, sanctified=sanctified,
    )
    max_p, max_s = state.max_slots_for_rarity()
    if state.crafted_mod_count > 1:
        legality_issues.append(
            f"[0.5.0+ 非法遗产/违例] 检测到 {state.crafted_mod_count} 条 Crafted 保底词缀"
            "（精华/合金/涂灌共享上限，0.5.0+ 单件最多 1 条）。"
        )
    if state.desecrated_mod_count > 1:
        legality_issues.append(
            f"[0.5.5 违例] 检测到 {state.desecrated_mod_count} 条 Desecrated 亵渎词缀（单件上限 1 条）。"
        )
    if len(prefixes) > max_p or len(suffixes) > max_s:
        legality_issues.append(
            f"词缀数量超槽位上限: 前缀 {len(prefixes)}/{max_p}，后缀 {len(suffixes)}/{max_s}。"
        )
    if state.has_fractured_crafted_mod:
        warnings.append(
            "注意: 破裂词缀属于 Crafted 槽位，按 PoE2 0.5.0+ 规则该装备已永久占满唯一 Crafted 名额，不可再点精华/合金。"
        )
    if corrupted or sanctified:
        warnings.append("物品已腐化或神圣化，制作通货已锁定（普通符文/灵核仍可覆盖插槽）。")

    whittle_preview = eng.analyze_whittling(state) if (rarity == ItemRarity.RARE and craft_mods) else None
    return ItemInspectionReport(
        patch=eng.patch, rarity=rarity, item_name=item_name, base_type=base.name,
        base_slug=base.poe2db_slug, ilvl=ilvl, quality=quality, catalyst_tag=catalyst_tag,
        corrupted=corrupted, sanctified=sanctified, max_prefixes=max_p, max_suffixes=max_s,
        prefixes=prefixes, suffixes=suffixes, implicits=implicits,
        crafted_mod_count=state.crafted_mod_count, desecrated_mod_count=state.desecrated_mod_count,
        fractured_crafted_locked=state.has_fractured_crafted_mod,
        is_legal_0_5_5=(len(legality_issues) == 0), legality_issues=legality_issues,
        warnings=warnings, whittling_preview=whittle_preview, state=state,
    )
