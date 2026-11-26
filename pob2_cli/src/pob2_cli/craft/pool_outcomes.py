# SPDX-License-Identifier: GPL-3.0-only
"""Currency mod-family floor exemption, full next-step outcome distribution, and target similarity ranking (L2)."""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from .models import (
    AffixSide,
    CraftingState,
    ItemRarity,
    LeverEntry,
    ModEntry,
    WeightConfidence,
    lowest_confidence,
)
from .pool_actions import (
    CRAFTED_LEVER_CATEGORIES,
    _parse_omens,
    exact_three_choice_unveil_prob,
)
from .weights_loader import strip_numbers_to_pattern

if TYPE_CHECKING:
    from .pool_engine import ModPoolEngine

_FILLER_TOKENS: frozenset[str] = frozenset({"to", "of", "the", "on", "in", "with", "and", "per", "all", "for", "your"})
_SECONDARY_QUALIFIERS: frozenset[str] = frozenset(
    {"regeneration", "leech", "leeched", "flask", "minion", "minions", "kill", "recoup", "recouped", "totem", "trap"}
)
_CLEAN_PAREN_RE = re.compile(r"[+-]?\([^)]*\)\s*%?\s*(?:to\s+)?")
_CLEAN_LEADING_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%?\s*(?:to\s+)?")
_MULTI_SPACE_RE = re.compile(r"\s+")
_TIER_PREFIX_RE = re.compile(r"^\s*t(\d+)\b\s*(.*)$", re.IGNORECASE)


def filter_pool_with_family_exemption(
    candidates: list[ModEntry],
    *,
    ilvl: int,
    min_mod_level: int,
) -> list[ModEntry]:
    """Apply `ilvl` cap + official GGG / Craft of Exile 2 `Greater`/`Perfect` mod-family floor exemption.

    Rule:
      - Filter `mod.required_level <= ilvl` first.
      - If a mod family/ladder's highest `ilvl`-eligible tier has `max_req >= min_mod_level`,
        prune lower tiers with `required_level < min_mod_level`.
      - If a mod family/ladder's highest `ilvl`-eligible tier has `max_req < min_mod_level`,
        that family is EXEMPTED rather than dropped and retains its highest available tier (`required_level == max_req`).
    """
    ilvl_ok = [m for m in candidates if m.required_level <= ilvl]
    if min_mod_level <= 1 or not ilvl_ok:
        return ilvl_ok

    max_req_by_ladder: dict[tuple[str, AffixSide, str, str], int] = {}
    for m in ilvl_ok:
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        prev = max_req_by_ladder.get(key, -1)
        if m.required_level > prev:
            max_req_by_ladder[key] = m.required_level

    out: list[ModEntry] = []
    for m in ilvl_ok:
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        max_req = max_req_by_ladder[key]
        if max_req >= min_mod_level:
            if m.required_level >= min_mod_level:
                out.append(m)
        elif m.required_level == max_req:
            out.append(m)
    return out


def default_rarity_for_lever(lever: LeverEntry | None) -> ItemRarity:
    """Infer sensible default item rarity for `craft odds` when `--text` is omitted."""
    if lever is None:
        return ItemRarity.NORMAL
    nm = lever.name
    if "Transmutation" in nm or nm == "Orb of Alchemy":
        return ItemRarity.NORMAL
    if "Augmentation" in nm or "Regal" in nm or lever.category == "essence":
        return ItemRarity.MAGIC
    return ItemRarity.RARE


def _default_next_lever_name(state: CraftingState) -> str:
    if state.rarity == ItemRarity.NORMAL:
        return "Orb of Transmutation"
    if state.rarity == ItemRarity.MAGIC:
        if state.has_open_prefix(ItemRarity.MAGIC) or state.has_open_suffix(ItemRarity.MAGIC):
            return "Orb of Augmentation"
        return "Regal Orb"
    if state.rarity == ItemRarity.RARE:
        return "Exalted Orb"
    raise ValueError(f"Cannot compute crafting outcomes on {state.rarity.value} item.")


def _clean_mod_phrase(stat_text: str, fallback: str) -> str:
    first_ln = (stat_text.splitlines()[0] if stat_text else fallback).strip()
    clean = _CLEAN_PAREN_RE.sub("", first_ln).strip()
    clean = _CLEAN_LEADING_NUM_RE.sub("", clean).strip()
    clean = _MULTI_SPACE_RE.sub(" ", clean)
    return clean or fallback


def _group_weighted_outcomes(
    weighted: list[tuple[ModEntry, float]],
    *,
    floor: int,
    unveil_pool: list[tuple[ModEntry, int]] | None = None,
    has_echoes: bool = False,
) -> tuple[list[dict[str, Any]], float]:
    tot_w = sum(w for _, w in weighted if w > 0)
    if tot_w <= 0:
        return [], 0.0

    groups: dict[tuple[str, AffixSide, str, str], list[tuple[ModEntry, float]]] = {}
    for m, w in weighted:
        if w <= 0:
            continue
        key = (m.source_pool, m.affix_side, m.family_id, strip_numbers_to_pattern(m.stat_text))
        groups.setdefault(key, []).append((m, w))

    outcomes: list[dict[str, Any]] = []
    for (spool, side, fam_id, pat), members in groups.items():
        members.sort(key=lambda x: (x[0].display_tier, -x[0].required_level, x[0].mod_id))
        fam_w = sum(w for _, w in members)
        prob = fam_w / tot_w
        tiers_num = [m.display_tier for m, _ in members]
        reqs = [m.required_level for m, _ in members]
        min_t, max_t = min(tiers_num), max(tiers_num)
        min_req, max_req = min(reqs), max(reqs)
        rep = members[0][0]
        entry: dict[str, Any] = {
            "familyId": fam_id,
            "modType": rep.mod_type,
            "name": _clean_mod_phrase(rep.stat_text, fam_id),
            "statPattern": pat or rep.stat_text,
            "slot": side.value,
            "sourcePool": spool,
            "weight": round(fam_w, 4) if isinstance(fam_w, float) and not fam_w.is_integer() else round(fam_w),
            "probability": round(prob, 8),
            "tierCount": len(members),
            "tierRange": f"T{min_t}-T{max_t}" if min_t != max_t else f"T{min_t}",
            "reqLevelRange": [min_req, max_req],
            "exemptedBelowFloor": bool(floor > 1 and max_req < floor),
            "availableTiers": [
                {
                    "modId": m.mod_id,
                    "tier": m.display_tier,
                    "requiredLevel": m.required_level,
                    "weight": round(w, 4) if isinstance(w, float) and not w.is_integer() else round(w),
                    "probability": round(w / tot_w, 8),
                    "statText": m.stat_text,
                }
                for m, w in members
            ],
        }
        if unveil_pool is not None:
            fam_ids = {m.mod_id for m, _ in members}
            p3 = exact_three_choice_unveil_prob(unveil_pool, fam_ids)
            if has_echoes:
                p3 = 1.0 - (1.0 - p3) ** 2
            entry["unveilHitProbability"] = round(p3, 8)
        outcomes.append(entry)

    outcomes.sort(key=lambda o: (-o["probability"], o["slot"], o["familyId"], o["name"]))
    return outcomes, tot_w


def compute_next_step_outcomes(
    eng: ModPoolEngine,
    state: CraftingState,
    *,
    lever_name: str | None = None,
    omens: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Compute complete next-step `ModFamily` weight & probability distribution for `craft odds --outcomes`."""
    if state.corrupted or state.sanctified:
        raise ValueError("Item is Corrupted or Sanctified; no further currency outcomes are legal.")

    eff_lever_name = lever_name or _default_next_lever_name(state)
    lever = eng.resolve_lever(eff_lever_name)
    omen_entries = tuple(eng.resolve_lever(om) for om in (omens or ()))
    if lever.max_item_level is not None and state.ilvl > lever.max_item_level:
        raise ValueError(f"{lever.name} has max item level {lever.max_item_level}, got ilvl={state.ilvl}")

    side_con, boss_con, has_whittle, _has_light, has_echoes, has_cat, _extra = _parse_omens(omen_entries)
    floor = lever.min_mod_level_floor or 1

    if lever.category in CRAFTED_LEVER_CATEGORIES:
        forced = eng.lookup_forced_mod_entry(state.base, lever)
        if forced is None:
            raise ValueError(f"{lever.name} has no forced modifier on `{state.base.poe2db_slug}`.")
        if lever.name != "Essence of the Abyss" and state.has_crafted_mod:
            raise ValueError(f"{lever.name} blocked: item already has 1 Crafted modifier (0.5.0+ limit).")
        outcomes, tot_w = _group_weighted_outcomes([(forced, 1.0)], floor=1)
        conf = WeightConfidence.EXACT_DETERMINISTIC
    elif lever.category == "bone":
        if state.rarity != ItemRarity.RARE:
            raise ValueError(f"{lever.name} requires a RARE item (got {state.rarity.value}).")
        if state.has_desecrated:
            raise ValueError("Item already has 1 Desecrated modifier.")
        pool_mode = "desecrated" if boss_con else "desecrated_combined"
        elig = eng.eligible_pool(
            state,
            source_pool=pool_mode,
            target_rarity=ItemRarity.RARE,
            side_constraint=side_con,
            min_mod_level=floor,
            boss_tag=boss_con,
        )
        outcomes, tot_w = _group_weighted_outcomes(
            [(m, float(w)) for m, w in elig],
            floor=floor,
            unveil_pool=elig,
            has_echoes=has_echoes,
        )
        conf = WeightConfidence.EXACT_DETERMINISTIC if boss_con else WeightConfidence.EMPIRICAL_ESTIMATE
    elif "Chaos Orb" in lever.name:
        if state.rarity != ItemRarity.RARE:
            raise ValueError(f"{lever.name} requires a RARE item (got {state.rarity.value}).")
        rem = eng.removable_indices(state, side_constraint=side_con, whittling=has_whittle)
        if not rem:
            elig = eng.eligible_pool(state, target_rarity=ItemRarity.RARE, side_constraint=side_con, min_mod_level=floor)
            outcomes, tot_w = _group_weighted_outcomes([(m, float(w)) for m, w in elig], floor=floor)
            conf = lowest_confidence([m.weight_confidence for m, _ in elig]) if elig else WeightConfidence.UNMEASURED
        else:
            acc_prob: dict[str, tuple[ModEntry, float]] = {}
            confs: list[WeightConfidence] = []
            avg_tw = 0.0
            for idx in rem:
                sub = state.clone()
                sub.mods.pop(idx)
                elig = eng.eligible_pool(sub, target_rarity=ItemRarity.RARE, min_mod_level=floor)
                tw = sum(w for _, w in elig)
                avg_tw += tw / len(rem)
                if elig:
                    confs.append(lowest_confidence([m.weight_confidence for m, _ in elig]))
                if tw > 0:
                    p_branch = 1.0 / len(rem)
                    for m, w in elig:
                        prev_m, prev_w = acc_prob.get(m.mod_id, (m, 0.0))
                        acc_prob[m.mod_id] = (prev_m, prev_w + p_branch * (w / tw))
            scaled = [(m, p * max(1.0, avg_tw)) for m, p in acc_prob.values()]
            outcomes, tot_w = _group_weighted_outcomes(scaled, floor=floor)
            conf = lowest_confidence(confs) if confs else WeightConfidence.UNMEASURED
    else:
        req_rar = (
            ItemRarity.NORMAL
            if ("Transmutation" in lever.name or lever.name == "Orb of Alchemy")
            else (ItemRarity.MAGIC if ("Augmentation" in lever.name or "Regal" in lever.name) else ItemRarity.RARE)
        )
        next_rar = (
            ItemRarity.MAGIC
            if ("Transmutation" in lever.name or "Augmentation" in lever.name)
            else ItemRarity.RARE
        )
        if state.rarity != req_rar:
            raise ValueError(f"{lever.name} requires {req_rar.value} item (got {state.rarity.value}).")
        elig = eng.eligible_pool(
            state,
            target_rarity=next_rar,
            side_constraint=side_con,
            min_mod_level=floor,
            use_catalyst=has_cat,
        )
        outcomes, tot_w = _group_weighted_outcomes([(m, float(w)) for m, w in elig], floor=floor)
        conf = lowest_confidence([m.weight_confidence for m, _ in elig]) if elig else WeightConfidence.UNMEASURED

    prob_sum = round(sum(o["probability"] for o in outcomes), 6)
    tot_w_int = round(tot_w, 4) if isinstance(tot_w, float) and not tot_w.is_integer() else round(tot_w)
    return {
        "game": "poe2",
        "patch": eng.patch,
        "base": state.base.name,
        "baseSlug": state.base.poe2db_slug,
        "ilvl": state.ilvl,
        "rarity": state.rarity.value,
        "lever": lever.name,
        "omens": [o.name for o in omen_entries],
        "minModLevelFloor": floor,
        "totalPoolWeight": tot_w_int,
        "familyCount": len(outcomes),
        "probabilitySum": prob_sum,
        "weightConfidence": conf.value,
        "outcomes": outcomes,
    }


def render_outcomes_md(data: dict[str, Any], *, max_rows: int = 40) -> str:
    """Render Markdown summary and probability table for `craft odds --outcomes`."""
    from tabulate import tabulate

    omen_tag = f" + {' + '.join(data['omens'])}" if data.get("omens") else ""
    lines = [
        f"### 下一步词缀族概率分布 [PoE2 {data['patch']}] — {data['base']} (`{data['baseSlug']}`, ilvl {data['ilvl']})",
        f"- 当前稀有度: `{data['rarity']}` | 通货: `{data['lever']}{omen_tag}` | 最低词缀等级门槛: `floor={data['minModLevelFloor']}`",
        f"- 总词缀池权重: `{data['totalPoolWeight']}` | 可命中词缀族数: `{data['familyCount']}` | "
        f"概率总和: `{data['probabilitySum']:.4f}` | 置信度: `{data['weightConfidence']}`",
    ]
    outcomes = data.get("outcomes") or []
    if not outcomes:
        lines.append("- 当前状态与通货组合下无可用词缀池（权重为 0）。")
        return "\n".join(lines)

    rows = [
        [
            o["slot"],
            o["familyId"],
            o["name"],
            o["tierRange"] + (" [豁免]" if o.get("exemptedBelowFloor") else ""),
            f"{o['reqLevelRange'][0]}-{o['reqLevelRange'][1]}",
            o["weight"],
            f"{o['probability'] * 100:.3f}%",
        ]
        for o in outcomes[:max_rows]
    ]
    lines.append("")
    lines.append(
        tabulate(
            rows,
            headers=["槽位", "FamilyId", "词缀描述", "可用阶位", "req_lvl", "权重", "单抽命中率"],
            tablefmt="github",
        )
    )
    if len(outcomes) > max_rows:
        lines.append(f"\n- 其余 {len(outcomes) - max_rows} 个低权重词缀族已折叠（传入 `--json` 查看完整列表）。")
    return "\n".join(lines)


def _stem_token(tok: str) -> str:
    if len(tok) >= 6 and tok.endswith("ing"):
        return tok[:-3]
    if len(tok) >= 5 and tok.endswith("ed"):
        return tok[:-2]
    if len(tok) >= 5 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) >= 4 and tok.endswith("s"):
        return tok[:-1]
    return tok


def rank_target_suggestions(
    cands: list[ModEntry],
    translated_query: str,
    *,
    limit: int = 6,
) -> list[str]:
    """Rank valid `--want` candidate strings by token/stem overlap + edit-distance similarity."""
    raw_q = (translated_query or "").strip()
    m_tier = _TIER_PREFIX_RE.match(raw_q)
    req_tier = int(m_tier.group(1)) if m_tier else None
    core_q = (m_tier.group(2) if m_tier else raw_q).strip().lower()
    if not core_q:
        core_q = raw_q.lower()

    all_q_toks = [t for t in re.split(r"[^a-z0-9]+", core_q) if len(t) >= 2 and not t.isdigit()]
    content_q_toks = [t for t in all_q_toks if t not in _FILLER_TOKENS] or all_q_toks
    q_stems = [_stem_token(t) for t in content_q_toks]

    # Deduplicate to one representative ladder per clean phrase, tracking max available display_tier
    reps: dict[str, tuple[int, int, str]] = {}
    for m in cands:
        clean = _clean_mod_phrase(m.stat_text, m.mod_type)
        key_low = clean.lower()
        pool_prio = 2 if m.source_pool == "normal" else (1 if m.source_pool == "desecrated" else 0)
        score_w = pool_prio * 100000 + m.weight
        prev = reps.get(key_low)
        if prev is None:
            reps[key_low] = (score_w, m.display_tier, clean)
        else:
            reps[key_low] = (max(prev[0], score_w), max(prev[1], m.display_tier), prev[2] if prev[0] >= score_w else clean)

    ranked: list[tuple[float, int, str]] = []
    for clean_low, (w_prio, max_t, clean_txt) in reps.items():
        eff_t = min(req_tier, max(1, max_t)) if req_tier is not None else 1
        label = f"T{eff_t} {clean_txt}"

        c_toks = [t for t in re.split(r"[^a-z0-9]+", clean_low) if len(t) >= 2]
        c_content = [t for t in c_toks if t not in _FILLER_TOKENS] or c_toks
        c_stems = {_stem_token(t) for t in c_content}
        c_word_set = set(c_content)

        if content_q_toks:
            exact_hits = sum(1 for t in content_q_toks if t in c_word_set)
            stem_hits = sum(
                1
                for t, st in zip(content_q_toks, q_stems, strict=False)
                if st in c_stems or any(ct.startswith(st) or st.startswith(ct) for ct in c_stems if len(ct) >= 3)
            )
            exact_frac = exact_hits / len(content_q_toks)
            stem_frac = stem_hits / len(content_q_toks)
            best_tok_sim = sum(
                max((difflib.SequenceMatcher(None, qt, ct).ratio() for ct in c_content), default=0.0)
                for qt in content_q_toks
            ) / len(content_q_toks)
        else:
            exact_frac = stem_frac = best_tok_sim = 0.0

        sub_bonus = 3.0 if (core_q and core_q in clean_low) else 0.0
        seq_sim = difflib.SequenceMatcher(None, core_q, clean_low).ratio() if core_q else 0.0

        score = exact_frac * 6.0 + stem_frac * 3.5 + sub_bonus + seq_sim * 4.0 + best_tok_sim * 2.5
        if exact_frac > 0 or stem_frac > 0 or best_tok_sim >= 0.72:
            extra_words = max(0, len(c_content) - len(content_q_toks))
            score -= 0.45 * extra_words
            unrequested_sec = sum(1 for w in c_content if w in _SECONDARY_QUALIFIERS and w not in content_q_toks)
            score -= 0.8 * unrequested_sec
        ranked.append((score, w_prio, label))

    ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [lbl for _, _, lbl in ranked[:limit]]


__all__ = [
    "compute_next_step_outcomes",
    "default_rarity_for_lever",
    "filter_pool_with_family_exemption",
    "rank_target_suggestions",
    "render_outcomes_md",
]
