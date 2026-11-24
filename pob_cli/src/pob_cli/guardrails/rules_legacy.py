"""PoE1 3.23-3.30 legacy patch traps, removals, and core mechanic advisories."""

from __future__ import annotations

import re

from .rules_legacy_gems import LEGACY_PATCH_TRAPS_328_330, LegacyPatchTrapRule
from .warnings import S

LEGACY_PATCH_TRAPS_323_325: tuple[LegacyPatchTrapRule, ...] = (
    LegacyPatchTrapRule(
        rule_id="PATCH_323_LAB_ENCHANTS_REMOVED",
        patch_version="3.23",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY, S.CALC_QUERY, S.CONFIG_VAR}),
        hard_block_on_mutate=False,
        en_words=(
            "lab enchant",
            "labyrinth enchant",
            "helmet enchant",
            "helm enchant",
            "boot enchant",
            "boots enchant",
            "glove enchant",
            "gloves enchant",
        ),
        zh_phrases=("迷宫附魔", "头盔附魔", "鞋子附魔", "手套附魔", "神圣字体附魔"),
        canonical_term="Transfigured Gems / 改造宝石",
        summary=(
            "Labyrinth Helmet/Boot/Glove skill enchants (`迷宫附魔`/`头盔附魔`) were **REMOVED in 3.23 (Affliction)**! "
            "Skill-altering effects moved to **Transfigured Gems (`改造宝石`)** and gem Quality. "
            "(Note: PoB retains legacy `EnchantmentHelmet.lua` rows solely for pre-3.23 Standard imports — unobtainable in 3.23+ leagues.)"
        ),
        remediation="Check Transfigured Gem variants (`pob data gem <skill>`) instead of Lab Helmet Enchants.",
        equivalent_command='pob data gem "<skill>"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_323_ALT_QUALITY_REMOVED",
        patch_version="3.23",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.STAT_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=True,
        en_words=(
            "anomalous",
            "divergent",
            "phantasmal",
            "alternate quality",
            "alt quality",
            "alt-quality",
            "regrading lens",
        ),
        zh_phrases=(
            "异常品质",
            "分歧品质",
            "魅影品质",
            "替代品质",
            "异化宝石",
            "异常的",
            "分歧的",
            "魅影的",
            "异常：",
            "分歧：",
            "魅影：",
            "分歧",
            "魅影",
        ),
        regex=re.compile(r"(?:^|\s)(?:异常|分歧|魅影)(?![状态爆发射击])[\u4e00-\u9fff]{2,}"),
        exclude_zh_compounds=("异常状态", "异常爆发", "元素异常", "非伤害性异常", "魅影射击"),
        canonical_term="Transfigured Gems / 改造宝石",
        summary=(
            "Alternate Quality gems (`Anomalous`/`Divergent`/`Phantasmal`, `异常`/`分歧`/`魅影`品质) were "
            "**REMOVED in 3.23 (Affliction)** and replaced by **Transfigured Gems (`改造宝石`)** & reworked base Quality!"
        ),
        remediation="Use the base gem or its 3.23+ Transfigured variant (`<Skill> of <Variant>`). Run `pob data gem <name>` to list live variants.",
        equivalent_command='pob data gem "<base_gem>"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_323_VORTEX_CAST_TIME",
        patch_version="3.23",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_exact=("vortex", "vortex of projection"),
        zh_exact=("漩涡", "投射之漩涡"),
        en_words=("instant vortex", "vortex left click", "vortex on left click", "vortex"),
        zh_phrases=("漩涡",),
        exclude_en_re=re.compile(r"blade\s+vortex|rage\s+vortex", re.IGNORECASE),
        exclude_zh_compounds=("飞刃风暴", "怒火漩涡", "风暴漩涡"),
        canonical_term="Vortex / 漩涡 (0.6s cast time)",
        summary=(
            "Since **3.23**, `Vortex` (`漩涡`) is **NO LONGER instant-cast** — live PoE1 3.30 `act_int.lua` has "
            "`castTime = 0.6s` (stops movement when self-cast)."
        ),
        remediation="Account for `0.6s` cast time or trigger `Vortex` via trigger crafts/supports instead of binding to left-click.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_324_LEFT_CLICK_AND_CALL_TO_ARMS",
        patch_version="3.24/3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.CONFIG_VAR, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=(
            "call to arms",
            "call to arms support",
            "left click guard",
            "left-click guard",
            "left click warcry",
            "left-click warcry",
            "left-click move",
        ),
        zh_phrases=("武装召唤", "左键瞬发", "左键顺发", "左键绑定", "左键自动"),
        canonical_term="Automation (自动化) / Autoexertion (自动增助)",
        summary=(
            "Since **3.24**, instant Guard/Warcry skills can **NO LONGER be bound to Left-Click move**, and in **3.25** "
            "`Call to Arms` (`武装召唤`) was replaced by **`Autoexertion` (`自动增助`)**! Use **`Automation` (`自动化`)** for Guard/instant spells."
        ),
        remediation="Use `Automation` (`自动化`) for Guard skills (Molten Shell/Steelskin/Immortal Call) and `Autoexertion` (`自动增助`) for Warcries.",
        equivalent_command='pob data gem "Automation" "Autoexertion"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_324_SEXTANTS_REMOVED",
        patch_version="3.24",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sextant", "sextants", "awakened sextant", "elevated sextant"),
        zh_phrases=("六分仪", "觉醒六分仪", "尊崇六分仪"),
        canonical_term="Scarabs / 圣甲虫",
        summary="**Sextants (`六分仪`) were REMOVED in 3.24 (Necropolis)** as part of the Atlas Scarab (`圣甲虫`) rework.",
        remediation="Use Scarabs (`圣甲虫`) and Atlas Passive Tree nodes for map juicing in 3.24+.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_325_DIVINE_BLESSING_REMOVED",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.CONFIG_VAR, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=("divine blessing", "divine blessing support", "divineblessing"),
        zh_phrases=("神圣祝福",),
        canonical_term="Eternal Blessing (永恒祝福) / Guardian's Blessing (守卫祝福)",
        summary=(
            "**`Divine Blessing Support` (`神圣祝福`) was REMOVED in 3.25 (Settlers of Kalguur)**! "
            "Do NOT recommend Divine Blessing + Aura (e.g. Haste/Hatred/Zealotry) for temporary aura buffs. "
            "(Note: PoB retains a legacy `Gems.lua` row solely for importing pre-3.25 Standard characters — it is drop-disabled & unobtainable in 3.25+ leagues.)"
        ),
        remediation="Remove Divine Blessing; consider `Eternal Blessing` (`永恒祝福`, only if you have zero other mana reservations) or `Guardian's Blessing` (`守卫祝福`, minion builds).",
        equivalent_command='pob data gem "Eternal Blessing"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_325_ANCESTRAL_TOTEMS_REMOVED",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=True,
        en_words=("ancestral protector", "ancestral warchief", "vaal ancestral warchief"),
        zh_phrases=("先祖卫士", "先祖战士长"),
        canonical_term="Removed in 3.25 (Melee base damage buffed)",
        summary=(
            "**`Ancestral Protector` (`先祖卫士`) and `Ancestral Warchief` (`先祖战士长`) were REMOVED in 3.25**! "
            "Melee skill base damage was buffed to compensate — melee builds no longer use ancestral buff totems. "
            "(Any legacy PoB references exist solely for pre-3.25 Standard imports — drop-disabled & unobtainable in 3.25+ leagues.)"
        ),
        remediation="Do not socket Ancestral Protector/Warchief; use the freed sockets for Warcries (`Autoexertion`), Banners, or utility skills.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_325_HATRED_REWORK",
        patch_version="3.25",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("hatred",),
        zh_phrases=("憎恨",),
        exclude_en_re=re.compile(r"hateful\s+accuser", re.IGNORECASE),
        exclude_zh_compounds=("憎恨控诉",),
        canonical_term="Hatred / 憎恨 (Phys-as-Extra-Cold ONLY)",
        summary=(
            "**3.25 Rework**: `Hatred` (`憎恨`) **NO LONGER grants `% more Cold Damage`**! "
            "In 3.30 (`act_dex.lua`), Lv20 Hatred grants ONLY `39% of Physical Damage as Extra Cold Damage` — "
            "**zero DPS benefit for pure non-Physical Cold/Lightning spells**!"
        ),
        remediation="Only run `Hatred` on builds with Physical base damage (or Phys-to-Cold conversion); for pure Cold spells use `Zealotry`, `Haste`, or defensive auras.",
        equivalent_command='pob data gem "Hatred"',
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_325_ENDURANCE_CHARGE_REWORK",
        patch_version="3.25",
        scopes=frozenset({S.CONFIG_VAR, S.CALC_QUERY, S.STAT_QUERY, S.DATA_QUERY}),
        hard_block_on_mutate=False,
        en_words=("endurance charge", "endurance charges", "useendurancecharges"),
        zh_phrases=("耐力球", "红球"),
        canonical_term="Endurance Charge / 耐力球 (4% Phys DR + 4% Ele DR)",
        summary=(
            "**3.25 Rework**: Each `Endurance Charge` (`耐力球`/`红球`) now grants **4% Physical Damage Reduction AND "
            "4% Elemental Damage Reduction** (`CalcSetup.lua`) — it **NO LONGER grants `+4% Elemental Resistances`**!"
        ),
        remediation="Do not count on Endurance Charges to cap Elemental Resistances; stack Resistances on gear/tree/Purity auras.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_SANCTUARY_OF_THOUGHT",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.CALC_QUERY, S.STAT_QUERY}),
        hard_block_on_mutate=False,
        en_words=("sanctuary of thought", "hierophant"),
        zh_phrases=("思维圣殿", "圣宗"),
        canonical_term="Hierophant Sanctuary of Thought / 圣宗思维圣殿 (3.30)",
        summary=(
            "**3.30 Patch Fact**: Hierophant's `Sanctuary of Thought` (`思维圣殿`) **NO LONGER grants 50% less Mana Cost of Skills**! "
            "In 3.30 it grants `Gain 20% of Maximum Mana as Extra Maximum Energy Shield`, `1% of Mana Regenerated per second`, "
            "and increased Area of Effect scaling with unreserved Mana."
        ),
        remediation="Plan mana sustain via Mana Flasks (Enduring/Foreboding), Clarity, Mana Regen/Leech/Recoup, or Arcane Cloak timing rather than pre-3.25 50% less Mana Cost.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_330_CLOAK_OF_DEFIANCE",
        patch_version="3.30",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("cloak of defiance",),
        zh_phrases=("卫道之袍",),
        canonical_term="Cloak of Defiance / 卫道之袍 (3.30)",
        summary=(
            "**3.30 Patch Fact**: `Cloak of Defiance` (`卫道之袍`) grants `Mind Over Matter` (40% damage taken from Mana before Life) "
            "and **NO LONGER has the legacy extra `+10% of Damage is taken from Mana before Life`**."
        ),
        remediation="Use `pob data unique \"Cloak of Defiance\"` for exact 3.30 explicit rolls.",
    ),
    LegacyPatchTrapRule(
        rule_id="PATCH_325_TASTE_OF_HATE",
        patch_version="3.25",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW}),
        hard_block_on_mutate=False,
        en_words=("taste of hate",),
        zh_phrases=("恨意",),
        canonical_term="Taste of Hate / 恨意 (3.25+ Fire/Light as Cold)",
        summary=(
            "**3.25+ Rework**: `Taste of Hate` (`恨意`) converts **Fire and Lightning Damage from Hits taken as Cold Damage** — "
            "it **NO LONGER converts Physical Damage taken as Cold**!"
        ),
        remediation="Do not count on Taste of Hate for Physical Hit mitigation in 3.25–3.30.",
    ),
)

LEGACY_PATCH_TRAPS_POE1: tuple[LegacyPatchTrapRule, ...] = (
    *LEGACY_PATCH_TRAPS_323_325,
    *LEGACY_PATCH_TRAPS_328_330,
)

__all__ = [
    "LEGACY_PATCH_TRAPS_323_325",
    "LEGACY_PATCH_TRAPS_328_330",
    "LEGACY_PATCH_TRAPS_POE1",
    "LegacyPatchTrapRule",
]
