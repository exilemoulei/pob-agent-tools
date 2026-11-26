# SPDX-License-Identifier: GPL-3.0-only
"""Stage 4 & standalone `pob2 spirit`: Discrete PoE2 Spirit reservation knapsack solver (L2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from ..snapshot import BuildScope, BuildTransaction
from .spirit_catalog import (
    CAMPAIGN_SPIRIT_QUESTS,
    is_spirit_active_gem,
    iter_offline_pack_entries,
)

_POB_COLOR_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_UNRES_SPIRIT_RE = re.compile(r"^([+-][\d,]+(?:\.\d+)?)\s+Unreserved\s+Spirit\b", re.IGNORECASE)
_UNRES_LIFE_RE = re.compile(r"^([+-][\d,]+(?:\.\d+)?)\s+Unreserved\s+Life\b", re.IGNORECASE)
_EHP_LINE_RE = re.compile(r"^([+-][\d,]+(?:\.\d+)?)\s+Effective\s+Hit\s+Pool\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SpiritOption:
    """One discrete Spirit reservation choice (either a deficit cut or a forward-packing pick)."""

    option_id: str
    choice_group: str  # Mutually exclusive group (e.g. same gem downgrade vs disable)
    kind: str  # "support_downgrade" | "support_disable" | "minion_count" | "aura_toggle" | "pack_item"
    label: str
    spirit_delta: int  # Positive = Spirit freed (rescue) or Spirit cost (forward packing)
    delta_dps: float
    delta_ehp: float
    loss_score: float = 0.0  # Lower is better for deficit rescue
    value_score: float = 0.0  # Higher is better for forward packing
    rpc_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.option_id,
            "group": self.choice_group,
            "kind": self.kind,
            "label": self.label,
            "spirit": self.spirit_delta,
            "deltaDps": round(self.delta_dps, 2),
            "deltaEhp": round(self.delta_ehp, 2),
            "dpsPerSpirit": round(abs(self.delta_dps) / max(1, self.spirit_delta), 2),
            "ehpPerSpirit": round(abs(self.delta_ehp) / max(1, self.spirit_delta), 2),
            "lossScore": round(self.loss_score, 4),
            "valueScore": round(self.value_score, 4),
        }


@dataclass(frozen=True)
class SpiritKnapsackPlan:
    """Output of either deficit-rescue or forward-packing discrete Spirit knapsack."""

    mode: str  # "rescue" | "pack"
    target_spirit: int
    achieved_spirit: int
    remaining_deficit: int
    selected: tuple[SpiritOption, ...]
    all_evaluated: tuple[SpiritOption, ...]
    missing_campaign_spirit: tuple[dict[str, Any], ...] = ()
    azcapa_hint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": 4,
            "name": "spirit_knapsack",
            "mode": self.mode,
            "targetSpirit": self.target_spirit,
            "achievedSpirit": self.achieved_spirit,
            "remainingDeficit": self.remaining_deficit,
            "selected": [o.to_dict() for o in self.selected],
            "evaluatedCount": len(self.all_evaluated),
            "evaluated": [o.to_dict() for o in self.all_evaluated],
            "missingCampaignSpiritQuests": list(self.missing_campaign_spirit),
            "azcapaHint": self.azcapa_hint,
            "warnings": list(self.warnings),
        }


def solve_deficit_knapsack(
    options: list[SpiritOption],
    deficit: int,
) -> tuple[list[SpiritOption], int]:
    """Exact multi-choice DP: pick <=1 option per `choice_group` freeing `>= deficit` minimizing loss & overshoot."""
    if deficit <= 0 or not options:
        return [], 0
    grouped: dict[str, list[SpiritOption]] = {}
    for opt in options:
        if opt.spirit_delta > 0:
            grouped.setdefault(opt.choice_group, []).append(opt)
    if not grouped:
        return [], 0

    cap = int(deficit)
    inf = float("inf")
    overshoot_w = 0.005
    # State tuple: (objective_cost, raw_loss, total_freed, count, chosen_options)
    dp: list[tuple[float, float, int, int, tuple[SpiritOption, ...]]] = [(inf, inf, 0, 0, ())] * (cap + 1)
    dp[0] = (0.0, 0.0, 0, 0, ())

    for group_key in sorted(grouped):
        group_opts = grouped[group_key]
        nxt = list(dp)
        for cur_s in range(cap):
            _cur_obj, cur_loss, cur_freed, cur_cnt, cur_pick = dp[cur_s]
            if cur_loss == inf:
                continue
            for opt in group_opts:
                new_freed = cur_freed + opt.spirit_delta
                ns = min(cap, new_freed)
                cand_loss = cur_loss + max(0.0, opt.loss_score)
                overshoot = max(0, new_freed - cap) if ns == cap else 0
                cand_obj = cand_loss + overshoot * overshoot_w
                cand_cnt = cur_cnt + 1
                prev_obj, prev_loss, prev_freed, prev_cnt, _ = nxt[ns]
                if (cand_obj, new_freed, cand_cnt) < (prev_obj - 1e-12, prev_freed, prev_cnt) or (
                    abs(cand_obj - prev_obj) <= 1e-12
                    and (new_freed, cand_loss, cand_cnt) < (prev_freed, prev_loss, prev_cnt)
                ):
                    nxt[ns] = (cand_obj, cand_loss, new_freed, cand_cnt, (*cur_pick, opt))
        dp = nxt

    if dp[cap][1] < inf:
        chosen = list(dp[cap][4])
        return chosen, sum(o.spirit_delta for o in chosen)

    for s in range(cap - 1, 0, -1):
        if dp[s][1] < inf:
            chosen = list(dp[s][4])
            return chosen, sum(o.spirit_delta for o in chosen)
    return [], 0


def solve_forward_spirit_knapsack(
    options: list[SpiritOption],
    budget: int,
) -> tuple[list[SpiritOption], int]:
    """Exact multi-choice 0/1 knapsack maximizing `value_score` subject to `sum(spirit_delta) <= budget`."""
    if budget <= 0 or not options:
        return [], 0
    grouped: dict[str, list[SpiritOption]] = {}
    for opt in options:
        if 0 < opt.spirit_delta <= budget and opt.value_score > 0.0:
            grouped.setdefault(opt.choice_group, []).append(opt)
    if not grouped:
        return [], 0

    cap = int(budget)
    dp: list[tuple[float, tuple[SpiritOption, ...]]] = [(-1.0, ())] * (cap + 1)
    dp[0] = (0.0, ())

    for group_key in sorted(grouped):
        group_opts = grouped[group_key]
        nxt = list(dp)
        for cur_s in range(cap + 1):
            cur_val, cur_pick = dp[cur_s]
            if cur_val < 0.0:
                continue
            for opt in group_opts:
                ns = cur_s + opt.spirit_delta
                if ns <= cap:
                    cand_val = cur_val + opt.value_score
                    if cand_val > nxt[ns][0] + 1e-12:
                        nxt[ns] = (cand_val, (*cur_pick, opt))
        dp = nxt

    best_s = max(range(cap + 1), key=lambda s: (dp[s][0], -s))
    if dp[best_s][0] <= 0.0:
        return [], 0
    chosen = list(dp[best_s][1])
    return chosen, sum(o.spirit_delta for o in chosen)


def _read_spirit_and_combat(worker: Any) -> tuple[float, float, float, float]:
    st = worker.call("get_stats", {}).get("stats") or {}
    total = float(st.get("Spirit") or 0.0)
    unres = float(st.get("SpiritUnreserved", total) or 0.0)
    res = float(st.get("SpiritReserved", max(0.0, total - unres)) or 0.0)
    true_unres = min(unres, total - res)
    dps = float(st.get("CombinedDPS") or st.get("TotalDPS") or 0.0)
    ehp = float(st.get("TotalEHP") or 0.0)
    return total, true_unres, dps, ehp


def _check_campaign_spirit_quests(worker: Any, *, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    try:
        cfg = worker.call("list_config", {})
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"[spirit-degraded] list_config failed during campaign Spirit check ({exc}).")
        return missing
    vals: dict[str, Any] = {}
    for sec in cfg.get("sections") or ():
        for item in (sec.get("items") or ()) if isinstance(sec, dict) else ():
            if isinstance(item, dict) and item.get("var"):
                vals[str(item["var"])] = item.get("value")
    for var_name, label, spirit_gain in CAMPAIGN_SPIRIT_QUESTS:
        if var_name in vals and vals[var_name] is False:
            missing.append({"configVar": var_name, "quest": label, "spiritGain": spirit_gain})
    return missing


def _loss_score(
    base_dps: float, base_ehp: float, delta_dps: float, delta_ehp: float, tie_bias: float = 0.002
) -> float:
    dps_loss_pct = max(0.0, -delta_dps) / max(1.0, base_dps) * 100.0
    ehp_loss_pct = max(0.0, -delta_ehp) / max(1.0, base_ehp) * 100.0
    return dps_loss_pct + ehp_loss_pct * 0.6 + tie_bias


def _probe_gem_tooltip_cut(
    worker: Any,
    g_idx: int,
    gem_idx: int,
    *,
    warnings: list[str] | None = None,
) -> tuple[int, float, float, bool, bool] | None:
    """Zero-mutation ~15ms probe via `gem_tooltip`.

    Returns `(freed_spirit, ddps, dehp, mentions_spirit_res, reserves_life_instead)` or `None`.
    """
    try:
        gt = worker.call("gem_tooltip", {"group": g_idx, "index": gem_idx})
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                f"[live-eval-degraded] gem_tooltip probe failed on group {g_idx} gem {gem_idx} ({exc}); fell back to BuildTransaction CoW."
            )
        return None
    if not isinstance(gt, dict):
        return None
    dps_info = gt.get("dps")
    ddps = float(dps_info.get("delta") or 0.0) if isinstance(dps_info, dict) else 0.0
    freed = 0
    dehp = 0.0
    mentions_res = False
    reserves_life = False
    for row in gt.get("lines") or ():
        raw = _POB_COLOR_RE.sub(
            "", str(row.get("raw") or row.get("text") or "") if isinstance(row, dict) else str(row)
        ).strip()
        if "reservation:" in raw.lower() and "spirit" in raw.lower():
            mentions_res = True
        um = _UNRES_SPIRIT_RE.match(raw)
        if um:
            freed = round(float(um.group(1).replace(",", "")))
        lm = _UNRES_LIFE_RE.match(raw)
        if lm and float(lm.group(1).replace(",", "")) > 0.0:
            reserves_life = True
        em = _EHP_LINE_RE.match(raw)
        if em:
            dehp = float(em.group(1).replace(",", ""))
    return freed, ddps, dehp, mentions_res, reserves_life


def evaluate_live_spirit_options(
    worker: Any,
    *,
    catalog: RuneInstillCatalog | None = None,
    warnings: list[str] | None = None,
) -> list[SpiritOption]:
    """Probe Spirit-freeing actions on a live `BridgeWorker` in `<3s` with consistent single-buff deltas."""
    cat = catalog or load_rune_instill_catalog()
    skills = worker.call("list_skills", {})
    main_idx = int(skills.get("mainSocketGroup") or 1)
    _tot0, _unres0, dps0, ehp0 = _read_spirit_and_combat(worker)

    options: list[SpiritOption] = []
    cow_queue: list[tuple[str, str, str, str, float, str, dict[str, Any]]] = []

    for grp in skills.get("groups") or ():
        if not isinstance(grp, dict) or not grp.get("enabled"):
            continue
        g_idx = int(grp.get("index") or 0)
        g_label = str(grp.get("displayLabel") or grp.get("label") or f"Group {g_idx}")
        gems = [gm for gm in (grp.get("gems") or ()) if isinstance(gm, dict) and gm.get("enabled", True)]
        if not gems:
            continue

        primary_sp_gem: dict[str, Any] | None = None
        for gem in gems:
            gem_idx = int(gem.get("index") or 0)
            gname = str(gem.get("nameSpec") or gem.get("name") or "")
            sp_entry = cat.spirit_supports_by_key.get(gname.strip().lower())
            if sp_entry is not None:
                cgroup = f"g{g_idx}_gem{gem_idx}"
                probe = _probe_gem_tooltip_cut(worker, g_idx, gem_idx, warnings=warnings) if gem.get("gemId") else None
                if probe is not None:
                    freed, ddps, dehp, _mres, _rlife = probe
                    if freed > 0:
                        options.append(
                            SpiritOption(
                                option_id=f"dis_{cgroup}", choice_group=cgroup, kind="support_disable",
                                label=f"{g_label}: disable {sp_entry.name} (-{freed} Spirit)",
                                spirit_delta=freed, delta_dps=ddps, delta_ehp=dehp,
                                loss_score=_loss_score(dps0, ehp0, ddps, dehp, tie_bias=0.002),
                                rpc_payload={"method": "set_gem", "params": {"group": g_idx, "index": gem_idx, "enabled": False}},
                            )
                        )
                        if sp_entry.downgrade_to:
                            cow_queue.append((
                                f"down_{cgroup}", cgroup, "support_downgrade",
                                f"{g_label}: {sp_entry.name} -> {sp_entry.downgrade_to} (-{{freed}} Spirit)",
                                0.001, "set_gem", {"group": g_idx, "index": gem_idx, "nameSpec": sp_entry.downgrade_to},
                            ))
                else:
                    if sp_entry.downgrade_to:
                        cow_queue.append((
                            f"down_{cgroup}", cgroup, "support_downgrade",
                            f"{g_label}: {sp_entry.name} -> {sp_entry.downgrade_to} (-{{freed}} Spirit)",
                            0.001, "set_gem", {"group": g_idx, "index": gem_idx, "nameSpec": sp_entry.downgrade_to},
                        ))
                    cow_queue.append((
                        f"dis_{cgroup}", cgroup, "support_disable",
                        f"{g_label}: disable {sp_entry.name} (-{{freed}} Spirit)",
                        0.002, "set_gem", {"group": g_idx, "index": gem_idx, "enabled": False},
                    ))

            cnt = int(gem.get("count") or 1)
            if cnt > 1 and not gem.get("isSupport") and not gem.get("support"):
                cgroup = f"g{g_idx}_min{gem_idx}"
                for new_cnt in range(cnt - 1, max(0, cnt - 4), -1):
                    cow_queue.append((
                        f"min_{cgroup}_{new_cnt}", cgroup, "minion_count",
                        f"{g_label}: {gname} count {cnt} -> {new_cnt} (-{{freed}} Spirit)",
                        0.002, "set_gem", {"group": g_idx, "index": gem_idx, "count": new_cnt},
                    ))

            if primary_sp_gem is None and is_spirit_active_gem(gem):
                primary_sp_gem = gem

        if g_idx != main_idx and primary_sp_gem is not None:
            probe = (
                _probe_gem_tooltip_cut(worker, g_idx, int(primary_sp_gem.get("index") or 1), warnings=warnings)
                if primary_sp_gem.get("gemId")
                else None
            )
            if probe is not None and probe[0] > 0:
                freed, ddps, dehp, _mres, _rlife = probe
                options.append(
                    SpiritOption(
                        option_id=f"aura_g{g_idx}", choice_group=f"group_{g_idx}", kind="aura_toggle",
                        label=f"Disable persistent buff `{g_label}` (-{freed} Spirit)",
                        spirit_delta=freed, delta_dps=ddps, delta_ehp=dehp,
                        loss_score=_loss_score(dps0, ehp0, ddps, dehp, tie_bias=0.005),
                        rpc_payload={"method": "set_group", "params": {"index": g_idx, "enabled": False}},
                    )
                )
            elif probe is None or (probe[3] and not probe[4] and not grp.get("source")):
                cow_queue.append((
                    f"aura_g{g_idx}", f"group_{g_idx}", "aura_toggle",
                    f"Disable persistent buff `{g_label}` (-{{freed}} Spirit)",
                    0.005, "set_group", {"index": g_idx, "enabled": False},
                ))

    if cow_queue:
        with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False) as tx:
            _tb, unres_b, dps_b, ehp_b = _read_spirit_and_combat(worker)
            for idx, (opt_id, cgroup, kind, label_fmt, tie_bias, method, params) in enumerate(cow_queue):
                if idx > 0:
                    tx.restore(rebuild=False)
                worker.call(method, params)
                _t1, unres1, dps1, ehp1 = _read_spirit_and_combat(worker)
                freed = round(unres1 - unres_b)
                if freed > 0:
                    ddps, dehp = dps1 - dps_b, ehp1 - ehp_b
                    options.append(
                        SpiritOption(
                            option_id=opt_id, choice_group=cgroup, kind=kind,
                            label=label_fmt.format(freed=freed),
                            spirit_delta=freed, delta_dps=ddps, delta_ehp=dehp,
                            loss_score=_loss_score(dps_b, ehp_b, ddps, dehp, tie_bias=tie_bias),
                            rpc_payload={"method": method, "params": params},
                        )
                    )
    return options


def solve_live_spirit_deficit(
    worker: Any,
    *,
    deficit_override: int | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> SpiritKnapsackPlan:
    """Solve Spirit over-reservation on a live `BridgeWorker` (`SpiritUnreserved >= 0`)."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []
    _tot, unres, _dps, _ehp = _read_spirit_and_combat(worker)
    deficit = deficit_override if deficit_override is not None else max(0, round(-unres))
    missing_quests = _check_campaign_spirit_quests(worker, warnings=warnings)
    azcapa_hint = (
        "Check Soul Core of Azcapa (+15 Spirit on martial weapon/helmet socket where legal) "
        "and campaign +100 Spirit rewards before cutting buffs."
        if deficit > 0
        else None
    )
    if deficit <= 0:
        return SpiritKnapsackPlan(
            mode="rescue",
            target_spirit=0,
            achieved_spirit=0,
            remaining_deficit=0,
            selected=(),
            all_evaluated=(),
            missing_campaign_spirit=tuple(missing_quests),
            azcapa_hint=None,
            warnings=tuple(warnings),
        )

    evaluated = evaluate_live_spirit_options(worker, catalog=cat, warnings=warnings)
    chosen, freed = solve_deficit_knapsack(evaluated, deficit)
    return SpiritKnapsackPlan(
        mode="rescue",
        target_spirit=deficit,
        achieved_spirit=freed,
        remaining_deficit=max(0, deficit - freed),
        selected=tuple(chosen),
        all_evaluated=tuple(sorted(evaluated, key=lambda o: (o.loss_score / max(1, o.spirit_delta), -o.spirit_delta))),
        missing_campaign_spirit=tuple(missing_quests),
        azcapa_hint=azcapa_hint,
        warnings=tuple(warnings),
    )


def apply_spirit_plan_live(worker: Any, plan: SpiritKnapsackPlan) -> None:
    """Apply selected Stage 4 Spirit knapsack mutations on live `BridgeWorker` (inside BuildTransaction)."""
    for opt in plan.selected:
        rpc = opt.rpc_payload
        if rpc and rpc.get("method"):
            worker.call(str(rpc["method"]), dict(rpc.get("params") or {}))


def build_offline_pack_catalog_options(
    *,
    metric: str = "CombinedDPS",
    catalog: RuneInstillCatalog | None = None,
) -> list[SpiritOption]:
    """Build deterministic offline Spirit packing options from the 30 fixed-Spirit supports & common buffs."""
    return [
        SpiritOption(
            option_id=oid,
            choice_group=grp,
            kind="pack_item",
            label=label,
            spirit_delta=cost,
            delta_dps=dps_v,
            delta_ehp=ehp_v,
            value_score=val,
        )
        for oid, grp, label, cost, dps_v, ehp_v, val in iter_offline_pack_entries(metric=metric, catalog=catalog)
    ]


__all__ = [
    "CAMPAIGN_SPIRIT_QUESTS",
    "SpiritKnapsackPlan",
    "SpiritOption",
    "apply_spirit_plan_live",
    "build_offline_pack_catalog_options",
    "evaluate_live_spirit_options",
    "solve_deficit_knapsack",
    "solve_forward_spirit_knapsack",
    "solve_live_spirit_deficit",
]
