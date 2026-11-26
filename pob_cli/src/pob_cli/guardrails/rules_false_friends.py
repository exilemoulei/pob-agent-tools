"""PoE1 vs PoE2 same-name false-friend rules (non-blocking warnings)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .warnings import GuardrailScope, S


@dataclass(frozen=True)
class FalseFriendRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    canonical_term: str | None = None
    summary: str = ""
    poe1_behavior: str = ""
    poe2_behavior: str = ""


SAME_NAME_FALSE_FRIENDS_POE1: tuple[FalseFriendRule, ...] = (
    FalseFriendRule(
        rule_id="FF_POE1_SOCKETS_DUPLICATE_SUPPORTS",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.STAT_QUERY, S.CALC_QUERY}),
        en_words=("support gem", "duplicate support", "six-link", "6-link", "6l", "gem sockets"),
        zh_phrases=("辅助宝石", "重复辅助", "六连", "6连", "宝石孔", "不能重复辅助"),
        canonical_term="PoE1 Gear Sockets & Support Gems",
        summary=(
            "In PoE1, skill sockets/links live on **gear** (up to 6-link), support gems level 1-20 "
            "(Exceptional Supports Lv1-3 / corrupt 4; only Awakened Empower/Enhance/Enlighten remain at Lv1-4 / corrupt 5 since 3.28), "
            "and **duplicate support gems ARE completely legal across different skill groups** (do NOT apply PoE2's 1-per-character limit)!"
        ),
        poe1_behavior="Gear holds linked sockets (up to 6L); multiple skill groups can each socket their own copy of the same support gem.",
        poe2_behavior="Skill gems hold their own support sockets and enforce a 1-copy-per-character support limit.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_DAMAGE_CONVERSION_CHAIN",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        en_words=(
            "damage conversion",
            "converted to",
            "convert to",
            "call of the brotherhood",
            "original sin",
            "avatar of fire",
            "hrimsorrow",
        ),
        zh_phrases=(
            "伤害转化",
            "转化为闪电",
            "转化为冰霜",
            "转化为火焰",
            "转化为混沌",
            "意志呼唤",
            "火之化身",
            "原初之罪",
            "原罪",
        ),
        canonical_term="PoE1 Damage Conversion (Phys->Light->Cold->Fire->Chaos)",
        summary=(
            "PoE1 Damage Conversion follows the fixed one-way chain `Phys -> Lightning -> Cold -> Fire -> Chaos` "
            "and **scales with BOTH source AND target `% increased` / `% more` modifiers** (unlike PoE2's final-type-only scaling)!"
        ),
        poe1_behavior="Converted damage remembers every stage (`Phys -> Light -> Cold` scales with `% inc Phys`, `% inc Lightning`, AND `% inc Cold`).",
        poe2_behavior="2-step conversion scaling ONLY with final damage type modifiers.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_ELDRITCH_BATTERY",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("eb",),
        en_words=("eldritch battery",),
        zh_phrases=("异能魔力",),
        canonical_term="Eldritch Battery / 异能魔力 (distinct from Chaos Inoculation / 异灵之体)",
        summary=(
            "PoE1 `Eldritch Battery` (`异能魔力`, Tree 3.30 — distinct from `Chaos Inoculation`/`异灵之体`): "
            "Spend Energy Shield before Mana for Skill Mana Costs; ES protects Mana instead of Life; 50% less ES Recharge Rate. "
            "It does **NOT** convert ES into Maximum Mana (that is PoE2)!"
        ),
        poe1_behavior="ES stays ES and protects Mana / pays Mana costs (`50% less ES Recharge Rate`).",
        poe2_behavior="Converts all Energy Shield into Maximum Mana.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_CHAOS_INOCULATION",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("ci",),
        en_words=("chaos inoculation",),
        zh_phrases=("异灵之体", "混沌接种"),
        canonical_term="Chaos Inoculation / 异灵之体",
        summary=(
            "PoE1 `Chaos Inoculation` (`异灵之体`, Tree 3.30): Maximum Life becomes 1 and Immune to Chaos Damage (incl. Poison). "
            "Physical Bleed still damages ES/Life unless Bleed-immune (and without CI, Chaos damage bypasses ES completely — unlike PoE2's 2x ES damage)."
        ),
        poe1_behavior="Life=1, Chaos/Poison immune; Physical Bleed still affects character unless Bleed-immune; non-CI Chaos bypasses ES.",
        poe2_behavior="CI grants Chaos + Bleed immunity; non-CI Chaos hits remove 2x ES instead of bypassing.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_MIND_OVER_MATTER",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("mom",),
        en_words=("mind over matter",),
        zh_phrases=("心灵升华", "心灵昇华", "心灵灭界"),
        canonical_term="Mind Over Matter / 心灵升华",
        summary=(
            "PoE1 `Mind Over Matter` (`心灵升华`, Tree 3.30; PoE2 CN calls it `心灵灭界`): **40%** of Damage is taken from Mana before Life "
            "(NOT PoE2's 100% Damage from Mana + 50% less Mana Recovery)."
        ),
        poe1_behavior="40% of Damage taken from Mana before Life (no built-in Mana Recovery penalty).",
        poe2_behavior="100% of Damage taken from Mana before Life with 50% less Mana Recovery Rate.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_ACROBATICS",
        scopes=frozenset({S.CALC_QUERY, S.DATA_QUERY, S.STAT_QUERY}),
        en_words=("acrobatics",),
        zh_phrases=("移形换影", "杂耍"),
        canonical_term="Acrobatics / 移形换影 (PoE2 CN: 杂耍)",
        summary=(
            "PoE1 `Acrobatics` (PoE1 CN: **`移形换影`**, PoE2 CN: `杂耍`; Tree 3.30): Modifiers to Chance to Suppress Spell Damage instead apply to "
            "Chance to Dodge Spell Hits at 50% of their value (Max 75%). Does NOT let Evasion evade boss slams/AoE (that is PoE2)!"
        ),
        poe1_behavior="Converts Spell Suppression Chance to Spell Dodge Chance at 50% value (75% cap); Evasion only evades Attacks.",
        poe2_behavior="Allows Evasion to evade AoE / all hits at ~70% less Evasion Rating.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_ARMOUR_MITIGATION",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY}),
        en_exact=("armour", "armor"),
        en_words=("armour", "physical damage reduction"),
        zh_phrases=("护甲",),
        exclude_en_re=re.compile(r"body\s+armour|additional\s+physical\s+damage\s+reduction", re.IGNORECASE),
        exclude_zh_compounds=("胸甲", "护甲槽"),
        canonical_term="Armour / 护甲",
        summary=(
            "PoE1 `Armour` (`护甲`) mitigates **Physical hit damage only** by default (not DoT, not Elemental unless explicit conversion/Transcendence). "
            "When Armour applies to Elemental hits, Elemental Resistance runs **BEFORE** Armour (unlike PoE2)."
        ),
        poe1_behavior="Mitigates Physical hits only by default; Resistance shrinks Elemental hits BEFORE Armour when applicable.",
        poe2_behavior="Armour-to-Elemental runs BEFORE Resistance.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_MAGEBLOOD",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        en_words=("mageblood",),
        zh_phrases=("法师之血", "法師之血", "法血", "魔血"),
        canonical_term="Mageblood / 法师之血 (incl. 3.30 Foulborn Mageblood / 秽生 法师之血)",
        summary=(
            "PoE1 `Mageblood` (`法师之血`, Heavy Belt): Standard `Mageblood` permanently applies **Leftmost 2-4 Magic Utility Flasks** "
            "(skipping Life/Mana/Hybrid/Unique flasks in earlier slots; cannot be used/removed), while 3.30 `Foulborn Mageblood` "
            "(`秽生 法师之血`) permanently applies **Rightmost 2-4 Magic Utility Flasks** (NOT PoE2's `Legacy of ...` Charm belt)."
        ),
        poe1_behavior=(
            "Standard Mageblood permanently activates Leftmost 2-4 Magic Utility Flasks (skipping Life/Mana/Hybrid/Unique flasks in earlier slots; "
            "cannot be used/removed; scaled by Enkindling Orb increased effect); 3.30 Foulborn Mageblood (`秽生 法师之血`) activates Rightmost 2-4 Magic Utility Flasks."
        ),
        poe2_behavior="Utility Belt with selectable `Legacy of <Flask>` variants and Charm Slots.",
    ),
    FalseFriendRule(
        rule_id="FF_POE1_CHARGES_PASSIVE_BONUS",
        scopes=frozenset({S.CONFIG_VAR, S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        en_words=(
            "frenzy charge",
            "frenzy charges",
            "power charge",
            "power charges",
            "usefrenzycharges",
            "usepowercharges",
        ),
        zh_phrases=("狂怒球", "绿球", "暴击球", "蓝球"),
        canonical_term="PoE1 Frenzy / Power Charges",
        summary=(
            "In PoE1, charges grant strong **inherent passive stats** (Frenzy: 4% more Damage + 4% Attack/Cast Speed; "
            "Power: +50% Crit Chance; Endurance: 4% Phys DR + 4% Ele DR) — unlike PoE2's 0 inherent passive bonuses."
        ),
        poe1_behavior="Frenzy = 4% more dmg & speed; Power = +50% crit chance; Endurance = 4% Phys DR & 4% Ele DR.",
        poe2_behavior="0 inherent passive stats per charge.",
    ),
)
