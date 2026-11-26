"""Curated Chinese PoE1 stat aliases and bilingual stat query translation."""

from __future__ import annotations

import re

from .apt_supplements import _CJK_BRACKET_RE, lookup_apt_zh_stat

ZH_STAT_ALIASES: dict[str, str] = {
    # Life & Mana
    "生命": "+# to maximum Life",
    "最大生命": "+# to maximum Life",
    "血量": "+# to maximum Life",
    "最大生命百分比": "#% increased maximum Life",
    "生命百分比": "#% increased maximum Life",
    "生命回复": "# Life Regenerated per Second",
    "每秒生命回复": "# Life Regenerated per Second",
    "魔力": "+# to maximum Mana",
    "最大魔力": "+# to maximum Mana",
    "魔力回复": "#% increased Mana Regeneration Rate",
    "魔力回复速度": "#% increased Mana Regeneration Rate",
    # Resistances
    "火抗": "+#% to Fire Resistance",
    "火焰抗性": "+#% to Fire Resistance",
    "冰抗": "+#% to Cold Resistance",
    "冰霜抗性": "+#% to Cold Resistance",
    "电抗": "+#% to Lightning Resistance",
    "闪电抗性": "+#% to Lightning Resistance",
    "混沌抗性": "+#% to Chaos Resistance",
    "混抗": "+#% to Chaos Resistance",
    "全抗": "+#% to all Elemental Resistances",
    "全元素抗性": "+#% to all Elemental Resistances",
    "所有元素抗性": "+#% to all Elemental Resistances",
    "元素抗性": "+#% total Elemental Resistance",
    "总元素抗性": "+#% total Elemental Resistance",
    "总抗性": "+#% total Resistance",
    "总抗": "+#% total Resistance",
    # Speed
    "攻速": "#% increased Attack Speed",
    "攻击速度": "#% increased Attack Speed",
    "施法速度": "#% increased Cast Speed",
    "施速": "#% increased Cast Speed",
    "移速": "#% increased Movement Speed",
    "移动速度": "#% increased Movement Speed",
    # Defences & Suppression & Block
    "法压": "+#% chance to Suppress Spell Damage",
    "法术压制": "+#% chance to Suppress Spell Damage",
    "法术压制率": "+#% chance to Suppress Spell Damage",
    "压制法术伤害几率": "+#% chance to Suppress Spell Damage",
    "能量护盾": "+# to maximum Energy Shield",
    "护盾": "+# to maximum Energy Shield",
    "最大能量护盾": "+# to maximum Energy Shield",
    "能量护盾百分比": "#% increased Energy Shield",
    "护盾百分比": "#% increased Energy Shield",
    "护甲": "+# to Armour",
    "护甲百分比": "#% increased Armour",
    "闪避": "+# to Evasion Rating",
    "闪避值": "+# to Evasion Rating",
    "闪避百分比": "#% increased Evasion Rating",
    "结界": "+# to Ward",
    "格挡": "+#% Chance to Block",
    "格挡率": "+#% Chance to Block",
    "格挡几率": "+#% Chance to Block",
    "攻击格挡": "#% Chance to Block Attack Damage",
    "攻击格挡率": "#% Chance to Block Attack Damage",
    "法术格挡": "#% Chance to Block Spell Damage",
    "法术格挡率": "#% Chance to Block Spell Damage",
    "法格": "#% Chance to Block Spell Damage",
    # Crit & Accuracy
    "暴击率": "#% increased Critical Strike Chance",
    "暴击几率": "#% increased Critical Strike Chance",
    "全局暴击率": "#% increased Global Critical Strike Chance",
    "暴击伤害": "+#% to Global Critical Strike Multiplier",
    "暴伤": "+#% to Global Critical Strike Multiplier",
    "全局暴击伤害加成": "+#% to Global Critical Strike Multiplier",
    "命中": "+# to Accuracy Rating",
    "命中值": "+# to Accuracy Rating",
    # Attributes
    "力量": "+# to Strength",
    "敏捷": "+# to Dexterity",
    "智慧": "+# to Intelligence",
    "智力": "+# to Intelligence",
    "全属性": "+# to all Attributes",
    "所有属性": "+# to all Attributes",
    # Damage & Flat Added Damage
    "物理伤害": "#% increased Physical Damage",
    "法术伤害": "#% increased Spell Damage",
    "法伤": "#% increased Spell Damage",
    "元素伤害": "#% increased Elemental Damage",
    "火焰伤害": "#% increased Fire Damage",
    "火伤": "#% increased Fire Damage",
    "冰霜伤害": "#% increased Cold Damage",
    "冰伤": "#% increased Cold Damage",
    "闪电伤害": "#% increased Lightning Damage",
    "电伤": "#% increased Lightning Damage",
    "混沌伤害": "#% increased Chaos Damage",
    "混伤": "#% increased Chaos Damage",
    "火焰点伤": "Adds # to # Fire Damage",
    "附加火焰伤害": "Adds # to # Fire Damage",
    "冰霜点伤": "Adds # to # Cold Damage",
    "附加冰霜伤害": "Adds # to # Cold Damage",
    "闪电点伤": "Adds # to # Lightning Damage",
    "附加闪电伤害": "Adds # to # Lightning Damage",
    "物理点伤": "Adds # to # Physical Damage",
    "附加物理伤害": "Adds # to # Physical Damage",
    "混沌点伤": "Adds # to # Chaos Damage",
    "附加混沌伤害": "Adds # to # Chaos Damage",
    "攻击火焰点伤": "Adds # to # Fire Damage to Attacks",
    "攻击附加火焰伤害": "Adds # to # Fire Damage to Attacks",
    "攻击冰霜点伤": "Adds # to # Cold Damage to Attacks",
    "攻击附加冰霜伤害": "Adds # to # Cold Damage to Attacks",
    "攻击闪电点伤": "Adds # to # Lightning Damage to Attacks",
    "攻击附加闪电伤害": "Adds # to # Lightning Damage to Attacks",
    "攻击物理点伤": "Adds # to # Physical Damage to Attacks",
    "攻击附加物理伤害": "Adds # to # Physical Damage to Attacks",
    "法术火焰点伤": "Adds # to # Fire Damage to Spells",
    "法术附加火焰伤害": "Adds # to # Fire Damage to Spells",
    "法术冰霜点伤": "Adds # to # Cold Damage to Spells",
    "法术附加冰霜伤害": "Adds # to # Cold Damage to Spells",
    "法术闪电点伤": "Adds # to # Lightning Damage to Spells",
    "法术附加闪电伤害": "Adds # to # Lightning Damage to Spells",
    "法术物理点伤": "Adds # to # Physical Damage to Spells",
    "法术附加物理伤害": "Adds # to # Physical Damage to Spells",
    "物品稀有度": "#% increased Rarity of Items found",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BRACE_NUM_RE = re.compile(r"\{\d+\}")


def translate_stat_query(text: str) -> str:
    """Translate a Chinese PoE1 stat query into English stat text.

    Non-CJK text is returned unchanged. Uses the curated ``ZH_STAT_ALIASES`` table
    first, then falls back to local SQLite ``game_data`` translations if available.
    """
    import pob_cli.trade_query as _tq

    original = (text or "").strip()
    if not original or not _CJK_RE.search(original):
        return original
    from ..game_data import fold_han

    # The alias table is Simplified, while pob-zh shows Traditional names (能量護盾). Unfolded, such
    # a query skipped the table and the fuzzy fallback picked another stat ("Has no Energy Shield").
    raw = _CJK_BRACKET_RE.sub("", fold_han(original)).strip()

    # Direct exact lookup
    direct = ZH_STAT_ALIASES.get(raw)
    if direct:
        return direct

    # Strip common numeric/symbol prefixes/suffixes (e.g., "+#% 火焰抗性", "最大生命 +")
    cleaned = re.sub(r"[+#%\d\s()（）\-–—]+", "", raw)
    if cleaned:
        direct_clean = ZH_STAT_ALIASES.get(cleaned)
        if direct_clean:
            return direct_clean
        # Strip leading verb hints like "提高"/"增加" when exact alias follows
        for prefix in ("提高", "增加"):
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                sub = cleaned[len(prefix) :]
                if sub in ZH_STAT_ALIASES:
                    return ZH_STAT_ALIASES[sub]

    apt_en = lookup_apt_zh_stat(raw)
    if apt_en:
        return apt_en

    # Fallback: query local PoB SQLite translations table when available
    try:
        resolved_root = _tq.resolve_engine_root()
        if (resolved_root / "PoE1" / "Data").is_dir():
            from .. import game_data

            query_term = cleaned or raw
            res = game_data.query_local_data("translate", query_term, limit=12)
            rows = [
                r
                for r in (res.get("results") or [])
                if str(r.get("match_type") or "") != "partial"
            ]
            if rows:
                cat_rank = {"mod": 0, "stat": 1, "passive": 2, "base": 3, "gem": 4, "unique": 5}
                match_rank = {"exact": 0, "prefix": 1, "substring": 2}
                best_row = min(
                    rows,
                    key=lambda r: (
                        cat_rank.get(str(r.get("category") or ""), 9),
                        match_rank.get(str(r.get("match_type") or ""), 9),
                        len(str(r.get("en") or "")),
                    ),
                )
                en_text = str(best_row.get("en") or "").strip()
                if en_text:
                    return _BRACE_NUM_RE.sub("#", en_text)
    except Exception:
        pass

    return original
