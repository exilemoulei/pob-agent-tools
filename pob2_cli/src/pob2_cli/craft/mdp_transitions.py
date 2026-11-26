# SPDX-License-Identifier: GPL-3.0-only
"""Compressed-state MDP representation and Bellman transition synthesis for PoE2 0.5.5 crafting."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    AffixSide,
    BaseItemEntry,
    CraftingState,
    ItemRarity,
    LeverEntry,
    TargetSpec,
    WeightConfidence,
    lowest_confidence,
)
from .pool_actions import exact_three_choice_unveil_prob
from .pool_engine import ModPoolEngine
from .pool_outcomes import filter_pool_with_family_exemption
from .price_book import PriceBook


@dataclass(frozen=True, slots=True)
class MdpState:
    rarity: ItemRarity
    hit_mask: int
    n_pref: int
    n_suff: int
    has_crafted: bool
    has_desecrated: bool
    has_fractured_target: bool
    has_abyss_mark: bool


@dataclass(frozen=True, slots=True)
class MdpAction:
    kind: str  # "lever" | "restart"
    lever_name: str
    omens: tuple[str, ...]
    cost_ex: float
    transitions: tuple[tuple[MdpState, float], ...]
    confidence: WeightConfidence
    note: str = ""


def bone_family_for_slug(slug: str) -> str | None:
    if slug in ("Amulets", "Rings", "Belts"):
        return "Collarbone"
    if any(
        slug.startswith(p)
        for p in ("Body_Armours", "Helmets", "Gloves", "Boots", "Shields", "Bucklers", "Foci")
    ):
        return "Rib"
    if slug in (
        "Claws", "Daggers", "Wands", "One_Hand_Swords", "One_Hand_Axes", "One_Hand_Maces",
        "Sceptres", "Spears", "Flails", "Bows", "Staves", "Two_Hand_Swords", "Two_Hand_Axes",
        "Two_Hand_Maces", "Quarterstaves", "Crossbows", "Talismans", "Quivers",
    ):
        return "Jawbone"
    return None


def state_to_key(state: CraftingState, targets: tuple[TargetSpec, ...]) -> MdpState:
    mask = 0
    has_frac_target = False
    for idx, t in enumerate(targets):
        hit_mod = next((m for m in state.mods if m.mod_id in t.matching_mod_ids), None)
        if hit_mod is not None:
            mask |= 1 << idx
            if hit_mod.fractured:
                has_frac_target = True
    return MdpState(
        rarity=state.rarity,
        hit_mask=mask,
        n_pref=min(state.prefix_count, state.base.max_prefixes),
        n_suff=min(state.suffix_count, state.base.max_suffixes),
        has_crafted=state.has_crafted_mod,
        has_desecrated=state.has_desecrated,
        has_fractured_target=has_frac_target,
        has_abyss_mark=state.has_abyss_mark,
    )


def merge_dist(items: list[tuple[MdpState, float]]) -> tuple[tuple[MdpState, float], ...]:
    acc: dict[MdpState, float] = {}
    for st, p in items:
        if p > 1e-12:
            acc[st] = acc.get(st, 0.0) + p
    tot = sum(acc.values())
    return tuple((st, p / tot) for st, p in acc.items()) if tot > 0 else ()


class PoolStatsCache:
    """Precomputed target hit probabilities per `(open_p, open_s, floor, hit_mask)`."""

    def __init__(
        self,
        engine: ModPoolEngine,
        base: BaseItemEntry,
        ilvl: int,
        targets: tuple[TargetSpec, ...],
        price_book: PriceBook,
    ) -> None:
        self.engine = engine
        self.base = base
        self.ilvl = ilvl
        self.targets = targets
        self.price_book = price_book
        self._normal_cache: dict[
            tuple[bool, bool, int, int], tuple[dict[int, float], float, float, WeightConfidence]
        ] = {}
        self.matching_essences: dict[int, list[LeverEntry]] = {}
        self.matching_perf_essences: dict[int, list[LeverEntry]] = {}
        slug = base.poe2db_slug
        for lv in engine.list_levers(active_only=True):
            if price_book.is_excluded(lv):
                continue
            mid = lv.forced_mods.get(slug)
            if not mid:
                continue
            for idx, t in enumerate(targets):
                if mid in t.matching_mod_ids:
                    if lv.category == "essence":
                        self.matching_essences.setdefault(idx, []).append(lv)
                    elif lv.category in ("perfect_or_corrupted_essence", "alloy", "liquid_emotion"):
                        self.matching_perf_essences.setdefault(idx, []).append(lv)

    def normal_add_probs(
        self, open_p: bool, open_s: bool, floor: int, hit_mask: int
    ) -> tuple[dict[int, float], float, float, WeightConfidence]:
        key = (open_p, open_s, floor, hit_mask)
        if key in self._normal_cache:
            return self._normal_cache[key]
        blocked = {self.targets[i].family_id for i in range(len(self.targets)) if hit_mask & (1 << i)}
        eff_tags = set(self.base.tags) | {"default"}
        pre_mods = [
            m
            for m in self.engine.get_mod_tiers(self.base.poe2db_slug, "normal")
            if ((m.affix_side == AffixSide.PREFIX and open_p) or (m.affix_side == AffixSide.SUFFIX and open_s))
            and not any(f in blocked for f in m.families)
            and m.weight > 0
            and self.engine._is_tag_eligible(m, eff_tags)
        ]
        mods = filter_pool_with_family_exemption(pre_mods, ilvl=self.ilvl, min_mod_level=floor)
        tot_w = sum(m.weight for m in mods)
        if tot_w <= 0:
            res: tuple[dict[int, float], float, float, WeightConfidence] = ({}, 0.0, 0.0, WeightConfidence.UNMEASURED)
            self._normal_cache[key] = res
            return res
        conf = lowest_confidence([m.weight_confidence for m in mods])
        hit_probs: dict[int, float] = {}
        hit_w_p = hit_w_s = 0
        pref_w = sum(m.weight for m in mods if m.affix_side == AffixSide.PREFIX)
        suff_w = tot_w - pref_w
        for i, t in enumerate(self.targets):
            if hit_mask & (1 << i):
                continue
            w_i = sum(m.weight for m in mods if m.mod_id in t.matching_mod_ids)
            if w_i > 0:
                hit_probs[i] = w_i / tot_w
                if t.affix_side == AffixSide.PREFIX:
                    hit_w_p += w_i
                else:
                    hit_w_s += w_i
        res = (hit_probs, max(0.0, (pref_w - hit_w_p) / tot_w), max(0.0, (suff_w - hit_w_s) / tot_w), conf)
        self._normal_cache[key] = res
        return res

    def desecrate_probs(
        self, open_p: bool, open_s: bool, floor: int, hit_mask: int, boss_tag: str | None, echoes: bool
    ) -> tuple[dict[int, float], float, float, WeightConfidence]:
        blocked = {self.targets[i].family_id for i in range(len(self.targets)) if hit_mask & (1 << i)}
        pools = ["desecrated"] if boss_tag else ["normal", "desecrated"]
        elig = []
        for pname in pools:
            pre_des = [
                m
                for m in self.engine.get_mod_tiers(self.base.poe2db_slug, pname)
                if (boss_tag is None or m.boss_tag == boss_tag)
                and ((m.affix_side == AffixSide.PREFIX and open_p) or (m.affix_side == AffixSide.SUFFIX and open_s))
                and not any(f in blocked for f in m.families)
                and (1000 if (boss_tag and pname == "desecrated") else m.weight) > 0
            ]
            for m in filter_pool_with_family_exemption(pre_des, ilvl=self.ilvl, min_mod_level=floor):
                w = 1000 if (boss_tag and pname == "desecrated") else m.weight
                elig.append((m, w))
        conf = WeightConfidence.EXACT_DETERMINISTIC if boss_tag else WeightConfidence.EMPIRICAL_ESTIMATE
        if not elig:
            return {}, 0.0, 0.0, conf
        hit_probs: dict[int, float] = {}
        rem_prob = 1.0
        for i, t in enumerate(self.targets):
            if hit_mask & (1 << i):
                continue
            p3 = exact_three_choice_unveil_prob(elig, set(t.matching_mod_ids))
            if echoes:
                p3 = 1.0 - (1.0 - p3) ** 2
            if p3 > 0:
                eff = min(rem_prob, p3)
                hit_probs[i] = eff
                rem_prob = max(0.0, rem_prob - eff)
        tot_w = sum(w for _, w in elig)
        frac_p = (sum(w for m, w in elig if m.affix_side == AffixSide.PREFIX) / tot_w) if tot_w > 0 else 0.5
        return hit_probs, rem_prob * frac_p, rem_prob * (1.0 - frac_p), conf


def _lever_cost(
    engine: ModPoolEngine, pb: PriceBook, lever_name: str, omens: tuple[str, ...] = ()
) -> float | None:
    lv = engine.resolve_lever(lever_name)
    if pb.is_excluded(lv):
        return None
    om_entries = tuple(engine.resolve_lever(o) for o in omens)
    if any(pb.is_excluded(o) for o in om_entries):
        return None
    try:
        return pb.action_cost(lv, om_entries)
    except ValueError:
        return None


def _add_dist(
    st: MdpState,
    targets: tuple[TargetSpec, ...],
    hp: dict[int, float],
    mp: float,
    ms: float,
    *,
    next_rarity: ItemRarity | None = None,
    set_desecrated: bool = False,
    clear_mark: bool = False,
) -> list[tuple[MdpState, float]]:
    rar = next_rarity or st.rarity
    des = True if set_desecrated else st.has_desecrated
    mrk = False if clear_mark else st.has_abyss_mark
    dist: list[tuple[MdpState, float]] = []
    for idx, p in hp.items():
        is_p = targets[idx].affix_side == AffixSide.PREFIX
        dist.append((replace(st, rarity=rar, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff + int(not is_p), has_desecrated=des, has_abyss_mark=mrk), p))
    if mp > 0:
        dist.append((replace(st, rarity=rar, n_pref=st.n_pref + 1, has_desecrated=des, has_abyss_mark=mrk), mp))
    if ms > 0:
        dist.append((replace(st, rarity=rar, n_suff=st.n_suff + 1, has_desecrated=des, has_abyss_mark=mrk), ms))
    return dist


def _rare_surgery_actions(
    st: MdpState,
    start_key: MdpState,
    base: BaseItemEntry,
    ilvl: int,
    targets: tuple[TargetSpec, ...],
    cache: PoolStatsCache,
    pb: PriceBook,
) -> list[MdpAction]:
    eng = cache.engine
    actions: list[MdpAction] = []
    max_p, max_s = base.max_prefixes, base.max_suffixes
    hit_p = sum(1 for i, t in enumerate(targets) if (st.hit_mask & (1 << i)) and t.affix_side == AffixSide.PREFIX)
    hit_s = sum(1 for i, t in enumerate(targets) if (st.hit_mask & (1 << i)) and t.affix_side == AffixSide.SUFFIX)
    junk_p, junk_s = max(0, st.n_pref - hit_p), max(0, st.n_suff - hit_s)
    total_mods = st.n_pref + st.n_suff
    open_p, open_s = st.n_pref < max_p, st.n_suff < max_s

    # Annul (+ Sinistral/Dextral)
    if total_mods > 0:
        for omens, side_only in (
            ((), None),
            (("Omen of Sinistral Annulment",), AffixSide.PREFIX),
            (("Omen of Dextral Annulment",), AffixSide.SUFFIX),
        ):
            rem_p = st.n_pref if side_only != AffixSide.SUFFIX else 0
            rem_s = st.n_suff if side_only != AffixSide.PREFIX else 0
            rem_tot = rem_p + rem_s - (1 if st.has_fractured_target and side_only is None else 0)
            if rem_tot <= 0 or (c := _lever_cost(eng, pb, "Orb of Annulment", omens)) is None:
                continue
            dist: list[tuple[MdpState, float]] = []
            if rem_p > 0 and junk_p > 0:
                dist.append((replace(st, n_pref=st.n_pref - 1), junk_p / rem_tot))
            if rem_s > 0 and junk_s > 0:
                dist.append((replace(st, n_suff=st.n_suff - 1), junk_s / rem_tot))
            for idx, t in enumerate(targets):
                if not (st.hit_mask & (1 << idx)) or (side_only and t.affix_side != side_only):
                    continue
                if st.has_fractured_target and idx == 0:
                    continue
                dp = 1 if t.affix_side == AffixSide.PREFIX else 0
                dist.append((replace(st, hit_mask=st.hit_mask & ~(1 << idx), n_pref=st.n_pref - dp, n_suff=st.n_suff - (1 - dp)), 1.0 / rem_tot))
            if dist:
                actions.append(MdpAction("lever", "Orb of Annulment", omens, c, merge_dist(dist), WeightConfidence.EXACT_DETERMINISTIC))

    # Chaos + Whittling
    if junk_p + junk_s > 0:
        p_jp = junk_p / (junk_p + junk_s)
        for lname, floor in (("Chaos Orb", 1), ("Greater Chaos Orb", 35), ("Perfect Chaos Orb", 50)):
            if (c := _lever_cost(eng, pb, lname, ("Omen of Whittling",))) is None:
                continue
            dist = []
            conf = WeightConfidence.VERIFIED_COMMUNITY
            for rem_p_flag, ws in ((True, p_jp), (False, 1.0 - p_jp)):
                if ws <= 0:
                    continue
                sub = replace(st, n_pref=st.n_pref - int(rem_p_flag), n_suff=st.n_suff - int(not rem_p_flag))
                hp, mp, ms, conf = cache.normal_add_probs(sub.n_pref < max_p, sub.n_suff < max_s, floor, st.hit_mask)
                for ns, p in _add_dist(sub, targets, hp, mp, ms):
                    dist.append((ns, ws * p))
            if dist:
                actions.append(MdpAction("lever", lname, ("Omen of Whittling",), c, merge_dist(dist), conf))

    # Perfect Essence / Alloy (STRICTLY gated by `not st.has_crafted` per 0.5.0+)
    if not st.has_crafted and total_mods > 0:
        for idx, plist in cache.matching_perf_essences.items():
            if st.hit_mask & (1 << idx):
                continue
            is_p = targets[idx].affix_side == AffixSide.PREFIX
            for lv in plist[:2]:
                for omens, side_only in (
                    ((), None),
                    (("Omen of Sinistral Crystallisation",), AffixSide.PREFIX),
                    (("Omen of Dextral Crystallisation",), AffixSide.SUFFIX),
                ):
                    if (c := _lever_cost(eng, pb, lv.name, omens)) is None:
                        continue
                    rem_p = st.n_pref if side_only != AffixSide.SUFFIX else 0
                    rem_s = st.n_suff if side_only != AffixSide.PREFIX else 0
                    rem_tot = rem_p + rem_s
                    if rem_tot <= 0:
                        continue
                    dist = []
                    if rem_p > 0 and junk_p > 0 and ((is_p and st.n_pref - 1 < max_p) or (not is_p and st.n_suff < max_s)):
                        dist.append((replace(st, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref - 1 + int(is_p), n_suff=st.n_suff + int(not is_p), has_crafted=True), junk_p / rem_tot))
                    if rem_s > 0 and junk_s > 0 and ((not is_p and st.n_suff - 1 < max_s) or (is_p and st.n_pref < max_p)):
                        dist.append((replace(st, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff - 1 + int(not is_p), has_crafted=True), junk_s / rem_tot))
                    for h_i, ht in enumerate(targets):
                        if not (st.hit_mask & (1 << h_i)) or (side_only and ht.affix_side != side_only):
                            continue
                        dp = 1 if ht.affix_side == AffixSide.PREFIX else 0
                        np2, ns2 = st.n_pref - dp + int(is_p), st.n_suff - (1 - dp) + int(not is_p)
                        if np2 <= max_p and ns2 <= max_s:
                            dist.append((replace(st, hit_mask=(st.hit_mask & ~(1 << h_i)) | (1 << idx), n_pref=np2, n_suff=ns2, has_crafted=True), 1.0 / rem_tot))
                    if dist:
                        actions.append(MdpAction("lever", lv.name, omens, c, merge_dist(dist), WeightConfidence.EXACT_DETERMINISTIC))

    # Abyssal Desecration Bone (independent cap 1)
    bfam = bone_family_for_slug(base.poe2db_slug)
    if bfam and not st.has_desecrated and (open_p or open_s):
        bname = f"Ancient {bfam}" if ilvl >= 40 else f"Preserved {bfam}"
        floor = 40 if "Ancient" in bname else 1
        for omens, op, os_, ech in (
            ((), open_p, open_s, False),
            (("Omen of Sinistral Necromancy",), open_p, False, False),
            (("Omen of Dextral Necromancy",), False, open_s, False),
            (("Omen of Abyssal Echoes",), open_p, open_s, True),
        ):
            if not (op or os_) or (c := _lever_cost(eng, pb, bname, omens)) is None:
                continue
            hp, mp, ms, conf = cache.desecrate_probs(op, os_, floor, st.hit_mask, None, ech)
            dist = _add_dist(st, targets, hp, mp if open_p else 0.0, ms if open_s else 0.0, set_desecrated=True, clear_mark=True)
            if dist:
                actions.append(MdpAction("lever", bname, omens, c, merge_dist(dist), conf))

    if total_mods == 4 and not st.has_fractured_target and st.hit_mask != 0:
        if (c := _lever_cost(eng, pb, "Fracturing Orb")) is not None:
            p_lock = (hit_p + hit_s) / 4.0
            actions.append(
                MdpAction("lever", "Fracturing Orb", (), c, merge_dist([(replace(st, has_fractured_target=True), p_lock), (start_key, 1.0 - p_lock)]), WeightConfidence.EXACT_DETERMINISTIC)
            )
    return actions


def actions_for_state(
    st: MdpState,
    start_key: MdpState,
    base: BaseItemEntry,
    ilvl: int,
    targets: tuple[TargetSpec, ...],
    cache: PoolStatsCache,
    pb: PriceBook,
    base_cost_ex: float,
) -> list[MdpAction]:
    """Generate legal PoE2 0.5.5 MDP actions & transition distributions for `st`."""
    eng = cache.engine
    actions: list[MdpAction] = []
    if st.hit_mask == (1 << len(targets)) - 1:
        return actions
    if st != start_key:
        actions.append(
            MdpAction("restart", "Restart (Fresh Base)", (), max(0.1, base_cost_ex), ((start_key, 1.0),), WeightConfidence.EXACT_DETERMINISTIC, "止损重开")
        )
    if st.rarity == ItemRarity.NORMAL:
        for lname, floor in (("Orb of Transmutation", 1), ("Greater Orb of Transmutation", 55), ("Perfect Orb of Transmutation", 70)):
            if (c := _lever_cost(eng, pb, lname)) is None:
                continue
            hp, mp, ms, conf = cache.normal_add_probs(True, True, floor, 0)
            if (dist := _add_dist(st, targets, hp, mp, ms, next_rarity=ItemRarity.MAGIC)):
                actions.append(MdpAction("lever", lname, (), c, merge_dist(dist), conf))
        return actions
    if st.rarity == ItemRarity.MAGIC:
        open_p, open_s = st.n_pref < 1, st.n_suff < 1
        if open_p or open_s:
            for lname, floor in (("Orb of Augmentation", 1), ("Greater Orb of Augmentation", 55), ("Perfect Orb of Augmentation", 70)):
                if (c := _lever_cost(eng, pb, lname)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(open_p, open_s, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp, ms)):
                    actions.append(MdpAction("lever", lname, (), c, merge_dist(dist), conf))
        for lname, floor in (("Regal Orb", 1), ("Greater Regal Orb", 35), ("Perfect Regal Orb", 50)):
            for omens, op, os_ in (((), True, True), (("Omen of Sinistral Coronation",), True, False), (("Omen of Dextral Coronation",), False, True)):
                if (c := _lever_cost(eng, pb, lname, omens)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(op, os_, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp, ms, next_rarity=ItemRarity.RARE)):
                    actions.append(MdpAction("lever", lname, omens, c, merge_dist(dist), conf))
        if not st.has_crafted:
            for idx, ess_list in cache.matching_essences.items():
                if st.hit_mask & (1 << idx):
                    continue
                for ess in ess_list[:2]:
                    if (c := _lever_cost(eng, pb, ess.name)) is None:
                        continue
                    is_p = targets[idx].affix_side == AffixSide.PREFIX
                    ns = replace(st, rarity=ItemRarity.RARE, hit_mask=st.hit_mask | (1 << idx), n_pref=st.n_pref + int(is_p), n_suff=st.n_suff + int(not is_p), has_crafted=True)
                    actions.append(MdpAction("lever", ess.name, (), c, ((ns, 1.0),), WeightConfidence.EXACT_DETERMINISTIC))
        return actions

    open_p, open_s = st.n_pref < base.max_prefixes, st.n_suff < base.max_suffixes
    if open_p or open_s:
        for lname, floor in (("Exalted Orb", 1), ("Greater Exalted Orb", 35), ("Perfect Exalted Orb", 50)):
            for omens, op, os_ in (((), open_p, open_s), (("Omen of Sinistral Exaltation",), open_p, False), (("Omen of Dextral Exaltation",), False, open_s)):
                if not (op or os_) or (c := _lever_cost(eng, pb, lname, omens)) is None:
                    continue
                hp, mp, ms, conf = cache.normal_add_probs(op, os_, floor, st.hit_mask)
                if (dist := _add_dist(st, targets, hp, mp if open_p else 0.0, ms if open_s else 0.0)):
                    actions.append(MdpAction("lever", lname, omens, c, merge_dist(dist), conf))
    actions.extend(_rare_surgery_actions(st, start_key, base, ilvl, targets, cache, pb))
    return actions
