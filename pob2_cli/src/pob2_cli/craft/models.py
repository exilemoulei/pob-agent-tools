# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Pure-stdlib dataclass contracts for the PoE2 0.5.5 crafting engine and item inspector."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MARK_OF_ABYSS_MOD_ID = "MarkOfTheAbyssalLord"

CRAFTED_SOURCE_POOLS: frozenset[str] = frozenset(
    {"essence", "perfect_essence", "alloy", "liquid", "crafted"}
)


class AffixSide(StrEnum):
    PREFIX = "prefix"
    SUFFIX = "suffix"
    IMPLICIT = "implicit"
    ENCHANT = "enchant"
    RUNE = "rune"
    NONE = "none"


class ItemRarity(StrEnum):
    NORMAL = "NORMAL"
    MAGIC = "MAGIC"
    RARE = "RARE"
    UNIQUE = "UNIQUE"


class ModDomain(StrEnum):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    CRAFTED = "crafted"
    FRACTURED = "fractured"
    DESECRATED = "desecrated"
    ENCHANT = "enchant"
    RUNE = "rune"
    CORRUPTED = "corrupted"
    MUTATED = "mutated"


class WeightConfidence(StrEnum):
    """Four-tier honest weight confidence scale for PoE2 0.5.5 crafting probabilities."""

    EXACT_DETERMINISTIC = "exact_deterministic"
    VERIFIED_COMMUNITY = "verified_community"
    EMPIRICAL_ESTIMATE = "empirical_estimate"
    UNMEASURED = "unmeasured"


_CONFIDENCE_RANK: dict[WeightConfidence, int] = {
    WeightConfidence.EXACT_DETERMINISTIC: 3,
    WeightConfidence.VERIFIED_COMMUNITY: 2,
    WeightConfidence.EMPIRICAL_ESTIMATE: 1,
    WeightConfidence.UNMEASURED: 0,
}


def lowest_confidence(values: list[WeightConfidence]) -> WeightConfidence:
    if not values:
        return WeightConfidence.UNMEASURED
    return min(values, key=lambda c: _CONFIDENCE_RANK[c])


class RemovedLeverError(ValueError):
    """Raised when a PoE1 orb or PoE2 0.4/0.5 removed crafting lever is requested."""

    def __init__(self, lever_name: str, reason: str, alternatives: str) -> None:
        self.lever_name = lever_name
        self.reason = reason
        self.alternatives = alternatives
        super().__init__(f"{lever_name}: {reason} 0.5.5 合法替代: {alternatives}")


class CraftedSlotLimitError(ValueError):
    """Raised when violating PoE2 0.5.0+ single Crafted modifier per item limit."""


@dataclass(frozen=True, slots=True)
class BaseItemEntry:
    id: str
    name: str
    item_class: str
    poe2db_slug: str
    drop_level: int
    max_prefixes: int
    max_suffixes: int
    tags: tuple[str, ...] = ("default",)
    implicits: tuple[str, ...] = ()
    has_measured_weights: bool = True


@dataclass(frozen=True, slots=True)
class ModEntry:
    mod_id: str
    mod_type: str
    family_id: str
    families: tuple[str, ...]
    base_group_slug: str
    source_pool: str
    affix_side: AffixSide
    affix_name: str
    internal_rank_asc: int
    display_tier: int
    required_level: int
    weight: int
    weight_confidence: WeightConfidence
    boss_tag: str | None = None
    currency_name: str | None = None
    is_alloy: bool = False
    removes_mod: bool = False
    adds_tags: tuple[str, ...] = ()
    mod_tags: tuple[str, ...] = ()
    stat_text: str = ""

    @property
    def is_crafted_pool(self) -> bool:
        if self.mod_id == MARK_OF_ABYSS_MOD_ID or "Mark of the Abyssal Lord" in self.stat_text:
            return False
        return self.source_pool in CRAFTED_SOURCE_POOLS or self.is_alloy


@dataclass(frozen=True, slots=True)
class LeverEntry:
    lever_id: str
    name: str
    category: str
    is_active_drop_in_patch: bool
    applies_to_rarities: str
    target_classes: tuple[str, ...] = ()
    max_item_level: int | None = None
    min_mod_level_floor: int | None = None
    omen_side_constraint: AffixSide | None = None
    omen_boss_constraint: str | None = None
    forced_mods: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True, slots=True)
class CraftingMod:
    mod_id: str
    mod_type: str
    family_id: str
    families: tuple[str, ...]
    affix_side: AffixSide
    display_tier: int
    required_level: int
    stat_text: str
    source_pool: str = "normal"
    adds_tags: tuple[str, ...] = ()
    mod_tags: tuple[str, ...] = ()
    fractured: bool = False
    is_crafted: bool = False
    is_desecrated: bool = False
    is_abyss_mark: bool = False

    @classmethod
    def from_entry(
        cls,
        entry: ModEntry,
        *,
        fractured: bool = False,
        is_crafted: bool | None = None,
        is_desecrated: bool | None = None,
    ) -> CraftingMod:
        is_mark = (
            "Mark of the Abyssal Lord" in entry.stat_text
            or entry.mod_id == MARK_OF_ABYSS_MOD_ID
        )
        desec = is_desecrated if is_desecrated is not None else (entry.source_pool == "desecrated")
        crafted = (
            is_crafted
            if is_crafted is not None
            else (False if (is_mark or desec) else entry.is_crafted_pool)
        )
        return cls(
            mod_id=entry.mod_id,
            mod_type=entry.mod_type,
            family_id=entry.family_id,
            families=entry.families,
            affix_side=entry.affix_side,
            display_tier=entry.display_tier,
            required_level=entry.required_level,
            stat_text=entry.stat_text,
            source_pool=entry.source_pool,
            adds_tags=entry.adds_tags,
            mod_tags=entry.mod_tags,
            fractured=fractured,
            is_crafted=crafted,
            is_desecrated=desec,
            is_abyss_mark=is_mark,
        )

    def with_fractured(self) -> CraftingMod:
        """Fracturing a mod locks it forever and PRESERVES `is_crafted` (0.5.0+ rule)."""
        return CraftingMod(
            mod_id=self.mod_id,
            mod_type=self.mod_type,
            family_id=self.family_id,
            families=self.families,
            affix_side=self.affix_side,
            display_tier=self.display_tier,
            required_level=self.required_level,
            stat_text=self.stat_text,
            source_pool=self.source_pool,
            adds_tags=self.adds_tags,
            mod_tags=self.mod_tags,
            fractured=True,
            is_crafted=self.is_crafted,
            is_desecrated=self.is_desecrated,
            is_abyss_mark=self.is_abyss_mark,
        )


@dataclass(slots=True)
class CraftingState:
    base: BaseItemEntry
    ilvl: int
    rarity: ItemRarity = ItemRarity.NORMAL
    mods: list[CraftingMod] = field(default_factory=list)
    quality: int = 0
    catalyst_tag: str | None = None
    corrupted: bool = False
    sanctified: bool = False

    def clone(self) -> CraftingState:
        return CraftingState(
            base=self.base,
            ilvl=self.ilvl,
            rarity=self.rarity,
            mods=list(self.mods),
            quality=self.quality,
            catalyst_tag=self.catalyst_tag,
            corrupted=self.corrupted,
            sanctified=self.sanctified,
        )

    @property
    def prefix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_side == AffixSide.PREFIX)

    @property
    def suffix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_side == AffixSide.SUFFIX)

    def max_slots_for_rarity(self, rarity: ItemRarity | None = None) -> tuple[int, int]:
        rar = rarity or self.rarity
        if rar == ItemRarity.NORMAL:
            return 0, 0
        if rar == ItemRarity.MAGIC:
            return 1, 1
        return self.base.max_prefixes, self.base.max_suffixes

    def has_open_prefix(self, rarity: ItemRarity | None = None) -> bool:
        max_p, _ = self.max_slots_for_rarity(rarity)
        return self.prefix_count < max_p

    def has_open_suffix(self, rarity: ItemRarity | None = None) -> bool:
        _, max_s = self.max_slots_for_rarity(rarity)
        return self.suffix_count < max_s

    @property
    def has_fractured(self) -> bool:
        return any(m.fractured for m in self.mods)

    @property
    def crafted_mod_count(self) -> int:
        """Count Crafted mods (Essence/Alloy/Distill), including fractured Crafted mods."""
        return sum(1 for m in self.mods if m.is_crafted)

    @property
    def has_crafted_mod(self) -> bool:
        """PoE2 0.5.0+: max 1 Crafted mod per item; fracturing it permanently occupies the slot."""
        return self.crafted_mod_count >= 1

    @property
    def has_fractured_crafted_mod(self) -> bool:
        return any(m.is_crafted and m.fractured for m in self.mods)

    @property
    def desecrated_mod_count(self) -> int:
        return sum(1 for m in self.mods if m.is_desecrated)

    @property
    def has_desecrated(self) -> bool:
        return self.desecrated_mod_count >= 1

    @property
    def has_abyss_mark(self) -> bool:
        return any(m.is_abyss_mark for m in self.mods)

    def blocked_families(self) -> set[str]:
        return {fam for m in self.mods for fam in m.families}

    def effective_tags(self) -> set[str]:
        tags = set(self.base.tags)
        for m in self.mods:
            tags.update(m.adds_tags)
        tags.add("default")
        return tags


@dataclass(frozen=True, slots=True)
class TargetSpec:
    label: str
    affix_side: AffixSide
    mod_type: str
    family_id: str
    matching_mod_ids: frozenset[str]
    matching_entries: tuple[ModEntry, ...]
    max_display_tier: int | None = 1

    def is_satisfied_by(self, state: CraftingState) -> bool:
        return any(m.mod_id in self.matching_mod_ids for m in state.mods)


@dataclass(frozen=True, slots=True)
class WhittlingTarget:
    mod_id: str
    stat_text: str
    affix_side: AffixSide
    display_tier: int
    required_level: int
    is_protected: bool
    remove_probability: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "modId": self.mod_id,
            "statText": self.stat_text,
            "affixSide": self.affix_side.value,
            "displayTier": self.display_tier,
            "requiredLevel": self.required_level,
            "isProtected": self.is_protected,
            "removeProbability": round(self.remove_probability, 6),
        }


@dataclass(frozen=True, slots=True)
class WhittlingReport:
    min_required_level: int | None
    targets: tuple[WhittlingTarget, ...]
    collateral_risk: float
    is_tied: bool
    collateral_alert: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "minRequiredLevel": self.min_required_level,
            "targets": [t.to_dict() for t in self.targets],
            "collateralRisk": round(self.collateral_risk, 6),
            "isTied": self.is_tied,
            "collateralAlert": self.collateral_alert,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class PoolEvalResult:
    lever: LeverEntry
    omens: tuple[LeverEntry, ...]
    hit_probability: float
    collateral_remove_probability: float
    eligible_pool_weight: int
    target_weight: int
    matching_tiers: tuple[ModEntry, ...]
    weight_confidence: WeightConfidence
    notes: str = ""
    whittling: WhittlingReport | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lever": self.lever.name,
            "omens": [o.name for o in self.omens],
            "hitProbability": round(self.hit_probability, 6),
            "collateralRemoveProbability": round(self.collateral_remove_probability, 6),
            "eligiblePoolWeight": self.eligible_pool_weight,
            "targetWeight": self.target_weight,
            "weightConfidence": self.weight_confidence.value,
            "matchingModIds": [m.mod_id for m in self.matching_tiers],
            "notes": self.notes,
        }
        if self.whittling is not None:
            out["whittling"] = self.whittling.to_dict()
        return out


@dataclass(frozen=True, slots=True)
class ActionEval:
    lever: str
    omens: tuple[str, ...]
    step_cost_ex: float
    hit_probability: float
    expected_cost_per_hit_ex: float | None
    collateral_risk: float
    weight_confidence: WeightConfidence
    notes: str = ""
    whittling: WhittlingReport | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lever": self.lever,
            "omens": list(self.omens),
            "stepCostEx": round(self.step_cost_ex, 4),
            "hitProbability": round(self.hit_probability, 6),
            "expectedCostPerHitEx": (
                round(self.expected_cost_per_hit_ex, 2)
                if self.expected_cost_per_hit_ex is not None
                else None
            ),
            "collateralRisk": round(self.collateral_risk, 6),
            "weightConfidence": self.weight_confidence.value,
            "notes": self.notes,
        }
        if self.whittling is not None:
            out["whittling"] = self.whittling.to_dict()
        return out


@dataclass(frozen=True, slots=True)
class PolicyStep:
    step_index: int
    stage: str
    action: str
    omens: tuple[str, ...]
    goal: str
    step_cost_ex: float
    hit_probability: float
    on_miss: str
    weight_confidence: WeightConfidence
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step_index,
            "stage": self.stage,
            "action": self.action,
            "omens": list(self.omens),
            "goal": self.goal,
            "stepCostEx": round(self.step_cost_ex, 4),
            "hitProbability": round(self.hit_probability, 6),
            "onMiss": self.on_miss,
            "weightConfidence": self.weight_confidence.value,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class CraftPlan:
    patch: str
    base_type: str
    base_slug: str
    ilvl: int
    targets: tuple[str, ...]
    expected_cost_ex: float
    expected_cost_div: float | None
    p50_cost_ex: float
    p80_cost_ex: float
    p95_cost_ex: float
    restart_rate: float
    mdp_states_solved: int
    mdp_iterations: int
    recommendation: str
    weight_confidence: WeightConfidence
    policy_steps: tuple[PolicyStep, ...]
    candidate_actions: tuple[ActionEval, ...]
    warnings: tuple[str, ...] = ()
    used_fallback_prices: bool = False
    buy_price_ex: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": "poe2",
            "patch": self.patch,
            "baseType": self.base_type,
            "baseSlug": self.base_slug,
            "ilvl": self.ilvl,
            "targets": list(self.targets),
            "expectedCostEx": round(self.expected_cost_ex, 2),
            "expectedCostDiv": (
                round(self.expected_cost_div, 3) if self.expected_cost_div is not None else None
            ),
            "p50CostEx": round(self.p50_cost_ex, 2),
            "p80CostEx": round(self.p80_cost_ex, 2),
            "p95CostEx": round(self.p95_cost_ex, 2),
            "restartRate": round(self.restart_rate, 4),
            "mdpStatesSolved": self.mdp_states_solved,
            "mdpIterations": self.mdp_iterations,
            "recommendation": self.recommendation,
            "weightConfidence": self.weight_confidence.value,
            "usedFallbackPrices": self.used_fallback_prices,
            "buyPriceEx": self.buy_price_ex,
            "policySteps": [s.to_dict() for s in self.policy_steps],
            "candidateActions": [a.to_dict() for a in self.candidate_actions],
            "warnings": list(self.warnings),
        }
