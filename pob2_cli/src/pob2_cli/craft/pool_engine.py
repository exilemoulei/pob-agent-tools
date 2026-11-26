# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Exact PoE2 0.5.5 crafting modifier pool engine with 1-Crafted-mod & Whittling guards."""

from __future__ import annotations

import difflib
import random
import re
from pathlib import Path
from typing import Any

from .base_catalog import REMOVED_LEVER_MESSAGES, ZH_TARGET_ALIASES
from .models import (
    MARK_OF_ABYSS_MOD_ID,
    AffixSide,
    BaseItemEntry,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    PoolEvalResult,
    RemovedLeverError,
    TargetSpec,
    UnknownTargetError,
    WeightConfidence,
    WhittlingReport,
    WhittlingTarget,
)
from .pool_actions import (
    evaluate_action_impl,
    exact_three_choice_unveil_prob,
    sample_transition_impl,
)
from .weights_loader import CraftDataBundle, load_craft_data, norm_key, resolve_base_entry

__all__ = [
    "ModPoolEngine",
    "exact_three_choice_unveil_prob",
    "extract_primary_roll_bounds",
]

_LEVER_ALIASES: dict[str, str] = {
    "scour": "orb_of_scouring",
    "scouring": "orb_of_scouring",
    "重铸石": "orb_of_scouring",
    "重鑄石": "orb_of_scouring",
    "alt": "orb_of_alteration",
    "alteration": "orb_of_alteration",
    "改造石": "orb_of_alteration",
    "重组器": "recombinator",
    "重組器": "recombinator",
    "重组预兆": "omen_of_recombination",
    "同质化预兆": "omen_of_homogenising_exaltation",
    "同质化崇高预兆": "omen_of_homogenising_exaltation",
    "同质化富豪预兆": "omen_of_homogenising_coronation",
    "transmute": "orb_of_transmutation",
    "transmutation": "orb_of_transmutation",
    "greater_transmute": "greater_orb_of_transmutation",
    "perfect_transmute": "perfect_orb_of_transmutation",
    "augment": "orb_of_augmentation",
    "augmentation": "orb_of_augmentation",
    "greater_augment": "greater_orb_of_augmentation",
    "perfect_augment": "perfect_orb_of_augmentation",
    "regal": "regal_orb",
    "greater_regal": "greater_regal_orb",
    "perfect_regal": "perfect_regal_orb",
    "exalt": "exalted_orb",
    "exalted": "exalted_orb",
    "greater_exalt": "greater_exalted_orb",
    "greater_exalted": "greater_exalted_orb",
    "perfect_exalt": "perfect_exalted_orb",
    "perfect_exalted": "perfect_exalted_orb",
    "chaos": "chaos_orb",
    "greater_chaos": "greater_chaos_orb",
    "perfect_chaos": "perfect_chaos_orb",
    "annul": "orb_of_annulment",
    "annulment": "orb_of_annulment",
    "alchemy": "orb_of_alchemy",
    "fracture": "fracturing_orb",
    "fracturing": "fracturing_orb",
    "whittling": "omen_of_whittling",
    "削切预兆": "omen_of_whittling",
}


def extract_primary_roll_bounds(stat_text: str) -> tuple[float, float] | None:
    first = stat_text.splitlines()[0] if stat_text else ""
    rm = re.search(r"\(([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\)", first)
    if rm:
        a, b = abs(float(rm.group(1))), abs(float(rm.group(2)))
        return min(a, b), max(a, b)
    nm = re.search(r"[+-]?(\d+(?:\.\d+)?)", first)
    if nm:
        v = abs(float(nm.group(1)))
        return v, v
    return None


def _translate_zh_target(query: str) -> str:
    out = query
    for zh, en in sorted(ZH_TARGET_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if zh in out:
            out = out.replace(zh, f" {en} ")
    return " ".join(out.split())


class ModPoolEngine:
    """Zero-SQLite exact PoE2 0.5.5 crafting pool engine with 1-Crafted-mod & Whittling guards."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.bundle: CraftDataBundle = load_craft_data(data_dir)

    @property
    def patch(self) -> str:
        return self.bundle.patch

    def list_levers(
        self, category: str | None = None, *, active_only: bool = True
    ) -> list[LeverEntry]:
        out = list(self.bundle.levers)
        if category is not None:
            out = [lv for lv in out if lv.category == category]
        if active_only:
            out = [lv for lv in out if lv.is_active_drop_in_patch]
        return out

    def resolve_lever(self, name_or_id: str, *, allow_legacy: bool = False) -> LeverEntry:
        raw = name_or_id.strip()
        key = _LEVER_ALIASES.get(raw) or _LEVER_ALIASES.get(norm_key(raw), norm_key(raw))
        if "homogenizing" in key:
            key = key.replace("homogenizing", "homogenising")

        if not allow_legacy:
            if key in REMOVED_LEVER_MESSAGES:
                reason, alt = REMOVED_LEVER_MESSAGES[key]
                raise RemovedLeverError(raw, reason, alt)
            if "recombinat" in key or "重组" in raw or "重組" in raw:
                reason, alt = REMOVED_LEVER_MESSAGES["recombinator"]
                raise RemovedLeverError(raw, reason, alt)
            if "homogenis" in key or "同质化" in raw or "同質化" in raw:
                reason, alt = REMOVED_LEVER_MESSAGES["omen_of_homogenising_exaltation"]
                raise RemovedLeverError(raw, reason, alt)
            if key in self.bundle.dormant_poe1_orbs:
                raise RemovedLeverError(
                    raw,
                    f"{raw!r} 属于 PoE1 遗留通货，在 PoE2 0.5.5 中不存在。",
                    "改用 PoE2 0.5.5 合法通货（三阶基础通货、精华/合金、深渊骨头、预兆、破裂石）。",
                )

        entry = self.bundle.levers_by_key.get(key)
        if entry is None:
            for cand_k, cand in self.bundle.levers_by_key.items():
                if key and key in cand_k:
                    entry = cand
                    break
        if entry is None:
            raise ValueError(f"Unknown PoE2 0.5.5 crafting lever: {name_or_id!r}")
        if not entry.is_active_drop_in_patch and not allow_legacy:
            reason, alt = REMOVED_LEVER_MESSAGES.get(
                entry.lever_id,
                (f"{entry.name!r} 已在 PoE2 0.4/0.5 移除。", "请改用 0.5.5 合法通货/预兆组合。"),
            )
            raise RemovedLeverError(entry.name, reason, alt)
        return entry

    def resolve_base(self, base_name_or_slug: str) -> BaseItemEntry:
        return resolve_base_entry(self.bundle, base_name_or_slug)

    def get_mod_tiers(self, base_slug: str, source_pool: str | None = None) -> list[ModEntry]:
        mods = self.bundle.mods_by_slug.get(base_slug, ())
        if source_pool is None:
            return list(mods)
        return [m for m in mods if m.source_pool == source_pool]

    def resolve_target(
        self,
        base_slug: str,
        target: str | dict[str, Any] | TargetSpec,
        ilvl: int = 82,
    ) -> TargetSpec:
        if isinstance(target, TargetSpec):
            return target
        max_tier: int | None = 1
        explicit_tier = False
        pool_filter: str | None = None
        if isinstance(target, dict):
            query = str(target.get("query") or target.get("mod") or target.get("mod_id") or "").strip()
            explicit_tier = "max_display_tier" in target or "tier" in target
            max_tier = target.get("max_display_tier", target.get("tier", 1))
            pool_filter = target.get("source_pool")
        else:
            query = _translate_zh_target(target.strip())
            tm = re.match(r"^[Tt](?:ier\s*)?(\d+)\+?\s+(.+)$", query)
            if tm:
                explicit_tier = True
                max_tier = int(tm.group(1))
                query = tm.group(2).strip()

        requested_val: float | None = None
        if not explicit_tier:
            vm = re.search(r"(?<![a-zA-Z])[+-]?(\d+(?:\.\d+)?)", query)
            if vm:
                requested_val = float(vm.group(1))

        candidates = [
            m
            for m in self.get_mod_tiers(base_slug)
            if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)
            and (pool_filter is None or m.source_pool == pool_filter)
        ]
        if not candidates:
            raise UnknownTargetError(
                query,
                base_slug,
                (),
                detail=f"No prefix/suffix modifiers found for base_slug={base_slug!r}.",
            )

        q_low = query.lower()
        exact_id = [
            m
            for m in candidates
            if m.mod_id.lower() == q_low or m.mod_id.rstrip("_").lower() == q_low.rstrip("_")
        ]
        if exact_id:
            hit = exact_id[0]
            same = [
                m
                for m in candidates
                if m.source_pool == hit.source_pool
                and m.mod_type == hit.mod_type
                and m.display_tier <= hit.display_tier
                and m.required_level <= ilvl
            ] or [hit]
            return TargetSpec(
                label=f"{hit.stat_text} (T{hit.display_tier}+)",
                affix_side=hit.affix_side,
                mod_type=hit.mod_type,
                family_id=hit.family_id,
                matching_mod_ids=frozenset(m.mod_id for m in same),
                matching_entries=tuple(same),
                max_display_tier=hit.display_tier,
            )

        by_type = [m for m in candidates if m.mod_type.lower() == q_low or m.family_id.lower() == q_low]
        if by_type:
            normal_only = [m for m in by_type if m.source_pool == "normal"]
            if normal_only:
                first_t = normal_only[0].mod_type
                by_type = [m for m in normal_only if m.mod_type == first_t]
        else:
            by_type = self._fuzzy_match_ladder(candidates, q_low, requested_val, base_slug, query)

        ilvl_ok = [m for m in by_type if m.required_level <= ilvl]
        if not ilvl_ok:
            sugs = tuple(self.suggest_targets(base_slug, query, ilvl=ilvl, limit=6))
            min_req = min(m.required_level for m in by_type)
            raise UnknownTargetError(
                query,
                base_slug,
                sugs,
                detail=f"Target {query!r} on `{base_slug}` requires ilvl >= {min_req} (got ilvl={ilvl}).",
            )
        ilvl_ok.sort(key=lambda m: m.display_tier)
        eff_max_t = max(ilvl_ok[0].display_tier, max_tier) if max_tier is not None else ilvl_ok[-1].display_tier
        chosen = [m for m in ilvl_ok if m.display_tier <= eff_max_t]
        if requested_val is not None and not explicit_tier:
            val_hits = [
                m
                for m in ilvl_ok
                if (b := extract_primary_roll_bounds(m.stat_text)) is not None and b[1] >= requested_val
            ]
            if val_hits:
                chosen = val_hits
                eff_max_t = max(m.display_tier for m in chosen)

        top = chosen[0]
        if pool_filter is None:
            bounds = [b[0] for m in chosen if (b := extract_primary_roll_bounds(m.stat_text)) is not None]
            floor_val = requested_val if requested_val is not None else (min(bounds) if bounds else None)
            chosen_ids = {m.mod_id for m in chosen}
            for cand in candidates:
                if cand.mod_id in chosen_ids or cand.required_level > ilvl:
                    continue
                if cand.affix_side != top.affix_side or cand.family_id != top.family_id:
                    continue
                if cand.source_pool not in ("essence", "perfect_essence", "alloy", "desecrated"):
                    continue
                cb = extract_primary_roll_bounds(cand.stat_text)
                if floor_val is not None and cb is not None and cb[1] >= floor_val:
                    chosen.append(cand)
                    chosen_ids.add(cand.mod_id)

        return TargetSpec(
            label=f"{top.stat_text} (<=T{eff_max_t})",
            affix_side=top.affix_side,
            mod_type=top.mod_type,
            family_id=top.family_id,
            matching_mod_ids=frozenset(m.mod_id for m in chosen),
            matching_entries=tuple(chosen),
            max_display_tier=eff_max_t,
        )

    def _fuzzy_match_ladder(
        self,
        candidates: list[ModEntry],
        q_low: str,
        requested_val: float | None,
        base_slug: str,
        query: str,
    ) -> list[ModEntry]:
        raw_tokens = [
            tok.lstrip("+")
            for tok in re.split(r"\s+", re.sub(r"[^a-z0-9+%]+", " ", q_low))
            if tok.lstrip("+")
        ]
        word_tokens = [t for t in raw_tokens if not re.fullmatch(r"\d+%?", t)]
        grouped: dict[tuple[str, str], list[ModEntry]] = {}
        for m in candidates:
            grouped.setdefault((m.source_pool, m.mod_type), []).append(m)
        scored: list[tuple[int, str, str]] = []
        for (spool, mtype), mlist in grouped.items():
            sample = " ".join(f"{x.stat_text} {x.affix_name} {x.mod_type} {x.boss_tag or ''}" for x in mlist).lower()
            tok_ok = bool(raw_tokens) and all(t in sample for t in raw_tokens)
            word_ok = (
                not tok_ok
                and requested_val is not None
                and len(word_tokens) >= 2
                and all(t in sample for t in word_tokens)
                and any(
                    (b := extract_primary_roll_bounds(x.stat_text)) is not None and b[0] <= requested_val <= b[1]
                    for x in mlist
                )
            )
            if q_low in sample or tok_ok or word_ok:
                pool_bonus = 100 if spool == "normal" else (60 if spool == "desecrated" else 40)
                exact_sub = 50 if any(q_low in x.stat_text.lower() for x in mlist) else 0
                scored.append((pool_bonus + exact_sub - min(len(x.stat_text) for x in mlist), spool, mtype))
        if not scored:
            sugs = tuple(self.suggest_targets(base_slug, query, limit=6))
            raise UnknownTargetError(query, base_slug, sugs)
        scored.sort(reverse=True)
        best_pool, best_type = scored[0][1], scored[0][2]
        return [m for m in candidates if m.source_pool == best_pool and m.mod_type == best_type]

    def suggest_targets(
        self,
        base_slug: str,
        query: str,
        *,
        ilvl: int = 82,
        limit: int = 6,
    ) -> list[str]:
        """Return Top-*limit* closest valid `--target` query strings from *base_slug*'s modifier pool."""
        cands = [
            m
            for m in self.get_mod_tiers(base_slug)
            if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX) and m.required_level <= ilvl
        ]
        if not cands:
            cands = [
                m for m in self.get_mod_tiers(base_slug) if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)
            ]
        # Deduplicate to one clean representative phrase per (source_pool, mod_type)
        reps: dict[str, tuple[int, str]] = {}
        for m in cands:
            first_ln = (m.stat_text.splitlines()[0] if m.stat_text else m.mod_type).strip()
            clean = re.sub(r"[+-]?\([^)]*\)\s*%?\s*(?:to\s+)?", "", first_ln).strip()
            clean = re.sub(r"^[+-]?\d+(?:\.\d+)?%?\s*(?:to\s+)?", "", clean).strip()
            clean = re.sub(r"\s+", " ", clean)
            if not clean:
                clean = m.mod_type
            label = f"T1 {clean}"
            pool_prio = 2 if m.source_pool == "normal" else (1 if m.source_pool == "desecrated" else 0)
            prev = reps.get(label.lower())
            score_w = pool_prio * 100000 + m.weight
            if prev is None or score_w > prev[0]:
                reps[label.lower()] = (score_w, label)

        q_low = _translate_zh_target(query or "").lower()
        q_toks = [t for t in re.split(r"[^a-z0-9]+", q_low) if len(t) >= 2]
        ranked: list[tuple[float, int, str]] = []
        for low_lbl, (w_prio, lbl) in reps.items():
            tok_hits = sum(1 for t in q_toks if t in low_lbl)
            sim = difflib.SequenceMatcher(None, q_low, low_lbl).ratio() if q_low else 0.0
            ranked.append((tok_hits * 2.0 + sim, w_prio, lbl))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        return [lbl for _, _, lbl in ranked[:limit]]

    def _is_tag_eligible(self, mod: ModEntry, effective_tags: set[str]) -> bool:
        if "caster" in mod.mod_tags or "Spell" in mod.stat_text:
            for el in ("fire", "cold", "lightning", "chaos", "physical"):
                if el in mod.mod_tags and f"no_{el}_spell_mods" in effective_tags:
                    return False
        return True

    def eligible_pool(
        self,
        state: CraftingState,
        *,
        source_pool: str = "normal",
        target_rarity: ItemRarity | None = None,
        side_constraint: AffixSide | None = None,
        min_mod_level: int = 1,
        boss_tag: str | None = None,
        use_catalyst: bool = False,
    ) -> list[tuple[ModEntry, int]]:
        eff_rar = target_rarity or state.rarity
        open_p = state.has_open_prefix(eff_rar)
        open_s = state.has_open_suffix(eff_rar)
        if side_constraint == AffixSide.PREFIX:
            open_s = False
        elif side_constraint == AffixSide.SUFFIX:
            open_p = False
        if not open_p and not open_s:
            return []

        blocked = state.blocked_families()
        eff_tags = state.effective_tags()
        pools = ["normal", "desecrated"] if source_pool == "desecrated_combined" else [source_pool]
        out: list[tuple[ModEntry, int]] = []
        for pname in pools:
            for mod in self.get_mod_tiers(state.base.poe2db_slug, pname):
                if mod.affix_side == AffixSide.PREFIX and not open_p:
                    continue
                if mod.affix_side == AffixSide.SUFFIX and not open_s:
                    continue
                if mod.affix_side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                    continue
                if mod.required_level > state.ilvl or mod.required_level < min_mod_level:
                    continue
                if boss_tag is not None and mod.boss_tag != boss_tag:
                    continue
                if any(f in blocked for f in mod.families):
                    continue
                if not self._is_tag_eligible(mod, eff_tags):
                    continue
                w = 1000 if (boss_tag is not None and pname == "desecrated") else mod.weight
                if w <= 0:
                    continue
                if use_catalyst and state.catalyst_tag and state.quality > 0 and state.catalyst_tag in mod.mod_tags:
                    w = round(w * (1.0 + state.quality / 20.0))
                out.append((mod, w))
        return out

    def lookup_forced_mod_entry(self, base: BaseItemEntry, lever: LeverEntry) -> ModEntry | None:
        if lever.name == "Essence of the Abyss":
            return ModEntry(
                mod_id=MARK_OF_ABYSS_MOD_ID,
                mod_type=MARK_OF_ABYSS_MOD_ID,
                family_id=MARK_OF_ABYSS_MOD_ID,
                families=(MARK_OF_ABYSS_MOD_ID,),
                base_group_slug=base.poe2db_slug,
                source_pool="perfect_essence",
                affix_side=AffixSide.SUFFIX,
                affix_name="of the Abyss",
                internal_rank_asc=1,
                display_tier=1,
                required_level=1,
                weight=0,
                weight_confidence=WeightConfidence.EXACT_DETERMINISTIC,
                currency_name=lever.name,
                removes_mod=True,
                stat_text="Bears the Mark of the Abyssal Lord",
            )
        mod_id = lever.forced_mods.get(base.poe2db_slug)
        if not mod_id:
            return None
        for m in self.get_mod_tiers(base.poe2db_slug):
            if m.mod_id == mod_id and m.currency_name == lever.name:
                return m
        return next((m for m in self.get_mod_tiers(base.poe2db_slug) if m.mod_id == mod_id), None)

    def removable_indices(
        self,
        state: CraftingState,
        *,
        side_constraint: AffixSide | None = None,
        only_desecrated: bool = False,
        whittling: bool = False,
    ) -> list[int]:
        idxs = [
            i
            for i, m in enumerate(state.mods)
            if not m.fractured
            and (side_constraint is None or m.affix_side == side_constraint)
            and (not only_desecrated or m.is_desecrated)
        ]
        if whittling and idxs:
            min_lvl = min(state.mods[i].required_level for i in idxs)
            idxs = [i for i in idxs if state.mods[i].required_level == min_lvl]
        return idxs

    def analyze_whittling(
        self,
        state: CraftingState,
        protected_mod_ids: set[str] | None = None,
        *,
        side_constraint: AffixSide | None = None,
    ) -> WhittlingReport:
        """Exact Omen of Whittling lowest `required_level` targeting & collateral risk analyzer."""
        protected = protected_mod_ids or set()
        rem = self.removable_indices(state, side_constraint=side_constraint, whittling=True)
        if not rem:
            return WhittlingReport(None, (), 0.0, False, False, "无可削切的非破裂词缀")
        min_lvl = state.mods[rem[0]].required_level
        p_each = 1.0 / len(rem)
        targets: list[WhittlingTarget] = []
        collat = 0.0
        for idx in rem:
            m = state.mods[idx]
            is_prot = m.mod_id in protected
            if is_prot:
                collat += p_each
            targets.append(
                WhittlingTarget(
                    mod_id=m.mod_id,
                    stat_text=m.stat_text.splitlines()[0],
                    affix_side=m.affix_side,
                    display_tier=m.display_tier,
                    required_level=m.required_level,
                    is_protected=is_prot,
                    remove_probability=p_each,
                )
            )
        is_tied = len(targets) > 1
        alert = collat > 0.0
        names = ", ".join(
            f"{t.stat_text} [T{t.display_tier}, req_lvl={t.required_level}"
            + (" · 受保护!" if t.is_protected else "")
            + "]"
            for t in targets
        )
        prefix = "[高危误伤预警] " if alert else ""
        tie_tag = f"（{len(targets)} 条同级平局随机）" if is_tied else ""
        return WhittlingReport(
            min_lvl, tuple(targets), collat, is_tied, alert,
            f"{prefix}削切预兆锁定最低需求等级 req_lvl={min_lvl}{tie_tag}: {names}",
        )

    def evaluate_action(
        self,
        state: CraftingState,
        target_mod_ids: set[str],
        lever_name: str,
        omens: list[str] | None = None,
        *,
        protected_mod_ids: set[str] | None = None,
    ) -> PoolEvalResult:
        return evaluate_action_impl(self, state, target_mod_ids, lever_name, omens, protected_mod_ids)

    def sample_transition(
        self,
        state: CraftingState,
        lever_name: str,
        omens: list[str] | tuple[str, ...] | None,
        desired_mod_ids: set[str],
        rng: random.Random,
    ) -> CraftingState:
        return sample_transition_impl(self, state, lever_name, omens, desired_mod_ids, rng)
