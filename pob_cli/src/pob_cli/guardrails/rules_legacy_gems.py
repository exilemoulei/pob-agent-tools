"""PoE1 3.28-3.30 gem, trigger, and build-synergy patch trap rules."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .warnings import GuardrailScope, S


@dataclass(frozen=True)
class LegacyPatchTrapRule:
    rule_id: str
    patch_version: str
    scopes: frozenset[GuardrailScope]
    hard_block_on_mutate: bool = False
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    canonical_term: str | None = None
    summary: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


LEGACY_PATCH_TRAPS_328_330: tuple[LegacyPatchTrapRule, ...] = (
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SWORD_OF_AGES_TEMPORAL_MIRAGE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.GEM_NAME, S.CALC_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sword of ages", "temporal mirage"),
        zh_phrases=("万古之剑", "万古剑", "时空幻影"),
        canonical_term="The Sword of Ages / Temporal Mirage (3.30 Verified)",
        summary=(
            "**3.30 Live-vs-PoB Advisory (`The Sword of Ages` / `Temporal Mirage`)**: "
            "(1) **Two separate `30% less` lines**: Temporal Mirage clones **DO** take their own innate `30% less Damage` "
            "(`Skills used by Temporal Mirages deal 30% less Damage`, ALREADY modeled in PoB `other.lua` `SupportUniqueSuspendInTimeCanUse`), "
            "and do **NOT** take a SECOND `-30% less` from Saboteur `Perfect Crime` (`Triggerbots`, since clones *use* the spell rather than your character triggering it). "
            "PoB 2.68.1 omits the `0.50s` base cooldown AND double-penalizes `0.7 * 0.7 = 0.49` by wrongly applying `Perfect Crime` `-30% less` on top of Temporal Mirage's own `-30% less` "
            "(`43% more Spell Damage` in `customMods` cancels ONLY that erroneous second penalty back to live `0.70` per-cast). "
            "(2) `on using a Movement Attack`: holding channelling `Cyclone` triggers ONLY ONCE on channel start (never continuously while held); "
            "requires tap-macro or discrete movement attacks (`Leap Slam`/`Flicker`/`Consecrated Path`). "
            "(3) `Greater Spell Cascade` shares `Firestorm of Pelting` global 12-storm cap (hurts single-target). "
            "(4) PoE1 server tick is **`0.033s` (`33ms`, NOT `33.33ms`)**: `0.50s` CD breakpoints are "
            "`0%->16t(528ms)`, `2%->15t(495ms)`, `9%->14t(462ms)`, `17%->13t(429ms)`, `27%->12t(396ms)`, "
            "`38%->11t(363ms)`, `52%->10t(330ms)`, `69%->9t(297ms)`, `90%->8t(264ms)`."
        ),
        remediation="Calculate effective cooldown via `ceil((0.50 / (1 + cdr)) / 0.033) * 0.033` and set Cyclone tap interval >= effective CD + jitter margin.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SKILL_GEM_QUALITY_SCOPE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.STAT_QUERY, S.CALC_QUERY}),
        hard_block_on_mutate=False,
        en_words=("quality of all skill gems", "to quality of all skill gems", "ashes of the stars"),
        zh_phrases=("技能宝石品质", "技能石品质", "柳条人肢体"),
        canonical_term="Skill Gem Quality Scope (Active ONLY, NOT Support Gems)",
        summary=(
            "**PoE1 3.30 Rule**: `+% to Quality of all Skill Gems` (`所有技能宝石品质`, e.g. `Ashes of the Stars`, 3.30 Wickerman Grafts) "
            "applies **ONLY to Active Skill Gems** (`grants_active_skill`) and **DOES NOT apply to Support Gems** "
            "(e.g. `Archmage Support`, `Spell Echo Support`)! Only `+% to Quality of Socketed [Support] Gems` "
            "(e.g. `Foulborn Skin of the Loyal/Lords`, `Crest of Desire`, `Enhance Support`) applies to Support Gems."
        ),
        remediation="Run `pob data gem --quality-breakpoints --active-only` for live 3.30 Active Skill Gem 20%->40%+ quality breakpoints.",
        equivalent_command="pob data gem --quality-breakpoints --active-only --limit 25",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SERVER_TICK_COOLDOWN",
        patch_version="3.30",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.GEM_NAME, S.DATA_QUERY}),
        hard_block_on_mutate=False,
        en_words=(
            "server tick",
            "cooldown breakpoint",
            "trigger cooldown",
            "cooldown recovery rate",
            "cast on critical strike",
            "awakened cast on critical strike",
        ),
        zh_phrases=("冷却回复速度", "触发冷却", "帧档位", "服务器帧", "鼠标宏", "暴击时施放"),
        canonical_term="PoE1 Server Tick = 0.033s (33ms)",
        summary=(
            "**PoE1 Server Tick Rule**: Cooldowns round UP to **`0.033s` (`33ms`, NOT `1/30 = 33.33ms`)** server ticks: "
            "`ticks = ceil((base_cd / (1 + cdr)) / 0.033)`, `effective_cd = ticks * 0.033s`. "
            "CoC (`0.15s` base): `0%->5t(165ms, 6.06/s)`, `14%->4t(132ms, 7.58/s)`, `52%->3t(99ms, 10.10/s)`, `128%->2t(66ms, 15.15/s)`; "
            "never let trigger attack APS exceed the trigger cap or effective trigger rate HALVES!"
        ),
        remediation="Run `pob data tick <base_cd> --cdr <pct> --aps <rate>` for exact 0.033s server-tick breakpoints.",
        equivalent_command="pob data tick 0.15 --cdr 14",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_CWC_NOT_COOLDOWN",
        patch_version="3.30",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("cast while channelling", "awakened cast while channelling", "cwc"),
        zh_phrases=("吟唱时施放", "引导时施放"),
        canonical_term="Cast while Channelling (Fixed Interval — CDR has ZERO effect)",
        summary=(
            "**PoE1 Mechanic Rule (`Cast while Channelling` / `吟唱时施放`)**: CwC triggers on a **fixed channelling interval** "
            "(`0.32s` / `320ms` at Lv20–21) — it is **NOT a Cooldown**! "
            "Stacking `Cooldown Recovery Rate (CDR)` has **ZERO effect** on CwC trigger interval (only CwC gem level changes it)."
        ),
        remediation="Do NOT stack CDR expecting faster CwC triggers; level up Cast while Channelling (Lv20–21 = 320ms) instead (regular Awakened CwC was removed/drop-disabled in 3.28).",
        equivalent_command='pob data gem "Cast while Channelling"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_LOW_LIFE_ESH_VISAGE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("esh's visage",),
        zh_phrases=("艾许之面",),
        canonical_term="Esh's Visage (DISABLES on Low Life!)",
        summary=(
            "**PoE1 Low-Life Trap (`Esh's Visage` / `艾许之面`)**: Mod reads `'Chaos Damage taken does not bypass Energy Shield while NOT on Low Life'`. "
            "If you reserve `>=50%` of Life (Low Life, `LifeUnreserved <= 50%`), **Esh's Visage STOPS WORKING completely** and Chaos damage bypasses ES to one-shot your Life!"
        ),
        remediation="For Low-Life Chaos bypass protection, use `Shavronne's Wrappings`, `Solaris Lorica`, `The Ivory Tower` (with unreserved Mana), or `Coruscating Elixir` (100% uptime). Search all via `pob data unique --mod \"Chaos Damage taken does not bypass\"`.",
        equivalent_command='pob data unique --mod "Chaos Damage taken does not bypass"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_328_AWAKENED_REMOVED",
        patch_version="3.28",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=(
            "awakened added chaos damage",
            "awakened added cold damage",
            "awakened added fire damage",
            "awakened added lightning damage",
            "awakened ancestral call",
            "awakened arrow nova",
            "awakened blasphemy",
            "awakened brutality",
            "awakened burning damage",
            "awakened cast on critical strike",
            "awakened cast while channelling",
            "awakened chain",
            "awakened cold penetration",
            "awakened controlled destruction",
            "awakened deadly ailments",
            "awakened elemental damage with attacks",
            "awakened elemental focus",
            "awakened fire penetration",
            "awakened fork",
            "awakened generosity",
            "awakened greater multiple projectiles",
            "awakened hextouch",
            "awakened increased area of effect",
            "awakened lightning penetration",
            "awakened melee physical damage",
            "awakened melee splash",
            "awakened minion damage",
            "awakened multistrike",
            "awakened spell cascade",
            "awakened spell echo",
            "awakened swift affliction",
            "awakened unbound ailments",
            "awakened unleash",
            "awakened vicious projectiles",
            "awakened void manipulation",
            "awakened coc",
            "awakened cwc",
            "awakened gmp",
            "awakened edwa",
        ),
        regex=re.compile(
            r"(?:觉醒|覺醒|强辅|強輔)\s*[·・.．]?\s*(?!赋予|賦予|赋能|賦能|增幅|增强|增強|启蒙|啟蒙|六分仪|六分儀|"
            r"宝石|寶石|辅助|輔助|石|者|之|的|后|後|了|吗|嗎|被|已|都|和|与|與|或|别|別|不|没|沒|无|無|只|仅|僅|"
            r"在|是|为|為|等|同样|體系|体系|上限|等级|掉落|替代|取代)[\u4e00-\u9fff]{2,}"
        ),
        canonical_term="Exceptional Supports (3.28+) / Only Awakened Empower, Enhance, Enlighten remain",
        summary=(
            "**3.28 (Mirage) Removal**: Regular **Awakened Support Gems** (`觉醒辅助`) were **removed/drop-disabled in 3.28** "
            "(replaced by 3.28+ **Exceptional Supports**, max natural Lv3 / corrupt Lv4). "
            "**ONLY `Awakened Empower`, `Awakened Enhance`, and `Awakened Enlighten`** (max natural Lv4 / corrupt Lv5) remain obtainable in 3.28–3.30 leagues! "
            "(Legacy Awakened rows remain in PoB solely for old Standard character imports.)"
        ),
        remediation="Use standard Lv20/21 supports or 3.28+ Exceptional supports (e.g. `Greater Spell Echo`, `Greater Multistrike`, `Cooldown Recovery`) instead of removed Awakened supports.",
        equivalent_command='pob data gem "Greater Spell Echo"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_INSPIRATION_MANA_RESET",
        patch_version="3.30",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("inspiration", "inspiration support", "inspiration charge", "inspiration charges"),
        zh_phrases=("启迪", "启迪辅助", "启迪球", "啟發", "啟發輔助", "启发辅助"),
        canonical_term="Inspiration Support (Lv20 resets at 800 Mana / Lv21 at 922 Mana)",
        summary=(
            "**PoE1 Mechanic Trap (`Inspiration Support` / `启迪` / `啟發`)**: "
            "`Inspiration` **loses ALL Inspiration Charges** after spending a total of **`800 Mana` at Lv20 (`922 Mana` at Lv21)**! "
            "On high-mana-cost builds (e.g. Archmage / Indigon / ManaCost >= 400), charges reset every 1–2 casts — "
            "real average charges and DPS are far below PoB's assumed permanent 5 charges."
        ),
        remediation="Check main skill `ManaCost` vs 800 (Lv20) / 922 (Lv21); reduce ManaCost or use Lv21 Inspiration / an alternative multiplier if resetting every 1–2 casts.",
        equivalent_command='pob data gem "Inspiration"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SADISM_DURATION_TRAP",
        patch_version="3.30",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sadism", "sadism support"),
        zh_phrases=("凌虐", "凌虐辅助", "虐待", "虐待辅助", "虐待輔助", "施虐辅助"),
        canonical_term="Sadism Support (70% LESS Ailment Duration Trap)",
        summary=(
            "**PoE1 DPS Trap (`Sadism Support` / `凌虐` / `虐待`)**: "
            "`Sadism` makes damaging ailments deal damage faster (`98%` at Lv20) at the cost of **`70% LESS Ailment Duration`**! "
            "This artificially inflates PoB's Ignite/Bleed/Poison DPS rate while **slashing total damage per ailment (~-70%+)** and breaking uptime whenever you move/dodge."
        ),
        remediation="Compare `Total Damage per Ignite` / ailment duration alongside DPS before socketing `Sadism Support`; prefer `Deadly Ailments`, `Swift Affliction`, or `Cruelty` for reliable uptime.",
        equivalent_command='pob data gem "Sadism"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_AGNOSTIC_CWS_SYNERGY",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY, S.ITEM_RAW, S.GEM_NAME}),
        hard_block_on_mutate=False,
        en_words=("the agnostic", "agnostic", "valyrium", "bloodnotch", "cast when stunned", "immutable force"),
        zh_phrases=("惘信者", "不可知论", "不可知論", "女武神", "战日", "戰日", "血痕", "缺血症", "晕眩时施放", "暈眩時施放", "永恒之力", "不變之力", "不变之力"),
        canonical_term="The Agnostic ES=0 Lock (CWS / Valyrium / Bloodnotch Core Synergy)",
        summary=(
            "**PoE1 Core Synergy (`The Agnostic` / `惘信者` + `Valyrium` / `Bloodnotch` / `Cast when Stunned`)**: "
            "`The Agnostic` locks **Maximum Energy Shield = 0**, which sets `Valyrium`'s ES-based Stun Threshold to 0 AND "
            "removes Energy Shield's inherent **50% Stun Avoidance** — essential for `Cast when Stunned` (`CWS`) / `Bloodnotch` + `Immutable Force` loops! "
            "**Do NOT unspec `The Agnostic` even when using `Blood Magic`** (unspec'ing it restores ES > 0 and bricks the build)."
        ),
        remediation="Keep `The Agnostic` allocated (ES = 0) on any `Valyrium` / `Bloodnotch` / `Cast when Stunned` build.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_DORYANI_PROTOTYPE_NEG_RES",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("doryani's prototype", "doryanis prototype"),
        zh_phrases=("多里亚尼的试作甲", "多里亚尼的原型", "多里亞尼之型", "多里亚尼之型", "多利亚尼的试作甲", "多利亚尼的原型"),
        canonical_term="Doryani's Prototype (-200% Lightning Res Intentional; Armour = Hits Only)",
        summary=(
            "**PoE1 Core Mechanic (`Doryani's Prototype` / `多里亚尼的试作甲` / `多里亞尼之型`)**: "
            "Stacking **`-200%` Lightning Resistance** is **INTENTIONAL** (sets nearby enemies' Lightning Resistance within 6m equal to yours for massive DPS). "
            "Armour mitigates **Lightning Damage from HITS only** — **Lightning Damage over Time (DoT)** (Mana Siphoner, Shaper Beam, Baran runes) "
            "**bypasses Armour completely and is deadly** unless converted away (e.g. `Divine Flesh`, `Tempered by War`, `Mahuxotl's Machination`, `Eyes of the Greatwolf`, `Avian Twins`)."
        ),
        remediation="Do NOT try to cap or fix negative Lightning Resistance on a `Doryani's Prototype` build; instead stack high Armour for Lightning Hits and Lightning-DoT-taken-as-Fire/Chaos/Cold mitigation.",
        equivalent_command='pob data unique "Doryani\'s Prototype"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_RAMAKO_FIXED_DOT_RES",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY, S.GEM_NAME}),
        hard_block_on_mutate=False,
        en_words=("ramako", "ramako, sun's light", "ramako sun's light"),
        zh_phrases=("雷默科", "烈阳之光", "烈陽之光", "太阳之光", "太陽之光"),
        canonical_term="Ramako, Sun's Light (Fixed -20% Enemy Fire Res vs DoT While Stationary)",
        summary=(
            "**PoE1 Core Mechanic (`Ramako, Sun's Light` / `雷默科`)**: "
            "While Stationary, nearby enemies' Fire Resistance against Damage over Time is **SET TO A FIXED `-20%`** — "
            "`Flammability`, `Elemental Weakness`, Fire Exposure, and Scorch have **ZERO effect** on Fire DoT while Ramako is active! "
            "Use `Punishment` (increased damage taken on Low Life) or defensive curses instead of `Flammability`."
        ),
        remediation="Do NOT swap `Punishment` for `Flammability` on a stationary `Ramako` Chieftain DoT build; `Flammability` cannot lower Fire DoT resistance below Ramako's fixed `-20%`.",
        equivalent_command='pob data tree "Ramako"',
    ),
)
