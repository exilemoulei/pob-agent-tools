# SPDX-License-Identifier: GPL-3.0-only
"""Stage 4 & standalone `pob2 spirit`: Discrete PoE2 Spirit reservation knapsack solver (L2).

Supports BOTH modes:
  1. Deficit Rescue (`deficit > 0` / `SpiritUnreserved < 0`):
     - Audits PoE2 campaign `+100 Spirit` quests (`Freythorn +30`, `Azak Bog +30`, `Kriar Village +40`)
       and `Soul Core of Azcapa` availability.
     - Evaluates discrete reductions (fixed-Spirit support tier downgrades e.g. `Clarity II -> I`,
       support removals, minion count decrements, and non-main persistent aura toggles) via live CoW
       `BuildTransaction` measuring exact `(spirit_freed, delta_dps, delta_ehp)` -> `loss_per_spirit`.
     - Solves exact multi-choice knapsack freeing `>= deficit` at minimum combat loss (`SpiritUnreserved >= 0`).
  2. Forward Budget Packing (`--budget <N>`):
     - Packs discrete Spirit options (buffs, minions, fixed-Spirit supports) under `sum(cost) <= budget`
       maximizing total `value` (`ΔDPS` or `ΔEHP`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..rune.catalog import RuneInstillCatalog, load_rune_instill_catalog
from ..snapshot import BuildScope, BuildTransaction

CAMPAIGN_SPIRIT_QUESTS: tuple[tuple[str, str, int], ...] = (
    ("questAct 1FreythornKing In The Mists", "Act 1 Freythorn: King in the Mists", 30),
    ("questAct 3Azak BogIgnagduk", "Act 3 Azak Bog: Ignagduk", 30),
    ("questInterlude 3Kriar VillageLythara", "Interlude 3 Kriar Village: Lythara", 40),
)


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
    """Exact multi-choice DP: pick at most 1 option per `choice_group` freeing `>= deficit` at min `loss_score`."""
    if deficit <= 0 or not options:
        return [], 0
    grouped: dict[str, list[SpiritOption]] = {}
    for opt in options:
        if opt.spirit_delta > 0:
            grouped.setdefault(opt.choice_group, []).append(opt)
    if not grouped:
        return [], 0

    cap = int(deficit)
    # dp[s] = (min_loss, chosen_options_tuple), where s in 0..cap and s == cap represents ">= cap"
    inf = float("inf")
    dp: list[tuple[float, tuple[SpiritOption, ...]]] = [(inf, ())] * (cap + 1)
    dp[0] = (0.0, ())

    for group_key in sorted(grouped):
        group_opts = grouped[group_key]
        nxt = list(dp)
        for cur_s in range(cap + 1):
            cur_loss, cur_pick = dp[cur_s]
            if cur_loss == inf:
                continue
            for opt in group_opts:
                ns = min(cap, cur_s + opt.spirit_delta)
                cand_loss = cur_loss + max(0.0, opt.loss_score)
                if cand_loss + 1e-12 < nxt[ns][0]:
                    nxt[ns] = (cand_loss, (*cur_pick, opt))
        dp = nxt

    if dp[cap][0] < inf:
        chosen = list(dp[cap][1])
        return chosen, sum(o.spirit_delta for o in chosen)

    # Fallback: if total available freed Spirit < deficit, pick the max-freed reachable state
    for s in range(cap - 1, 0, -1):
        if dp[s][0] < inf:
            chosen = list(dp[s][1])
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


def _check_campaign_spirit_quests(worker: Any) -> list[dict[str, Any]]:
    """Audit whether any of the 3 PoE2 campaign +100 Spirit quests are disabled in Config."""
    missing: list[dict[str, Any]] = []
    try:
        cfg = worker.call("list_config", {})
    except Exception:
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


def _loss_score(base_dps: float, base_ehp: float, delta_dps: float, delta_ehp: float) -> float:
    dps_loss_pct = max(0.0, -delta_dps) / max(1.0, base_dps) * 100.0
    ehp_loss_pct = max(0.0, -delta_ehp) / max(1.0, base_ehp) * 100.0
    return dps_loss_pct + ehp_loss_pct * 0.6 + 0.01


def evaluate_live_spirit_options(
    worker: Any,
    *,
    catalog: RuneInstillCatalog | None = None,
) -> list[SpiritOption]:
    """Probe every discrete Spirit-freeing action via CoW `BuildTransaction` on a live `BridgeWorker`."""
    cat = catalog or load_rune_instill_catalog()
    skills = worker.call("list_skills", {})
    main_idx = int(skills.get("mainSocketGroup") or 1)
    _tot0, unres0, dps0, ehp0 = _read_spirit_and_combat(worker)

    options: list[SpiritOption] = []
    for grp in skills.get("groups") or ():
        if not isinstance(grp, dict) or not grp.get("enabled"):
            continue
        g_idx = int(grp.get("index") or 0)
        g_label = str(grp.get("displayLabel") or grp.get("label") or f"Group {g_idx}")
        gems = grp.get("gems") or ()

        # 1. Fixed-Spirit support gems: test tier downgrade (II -> I) and disable
        for gem in gems:
            if not isinstance(gem, dict) or not gem.get("enabled", True):
                continue
            gem_idx = int(gem.get("index") or 0)
            gname = str(gem.get("nameSpec") or gem.get("name") or "")
            sp_entry = cat.spirit_supports_by_key.get(gname.strip().lower())
            if sp_entry is not None:
                cgroup = f"g{g_idx}_gem{gem_idx}"
                if sp_entry.downgrade_to:
                    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
                        worker.call("set_gem", {"group": g_idx, "index": gem_idx, "nameSpec": sp_entry.downgrade_to})
                        _t1, unres1, dps1, ehp1 = _read_spirit_and_combat(worker)
                    freed = round(unres1 - unres0)
                    if freed > 0:
                        ddps, dehp = dps1 - dps0, ehp1 - ehp0
                        options.append(
                            SpiritOption(
                                option_id=f"down_{cgroup}",
                                choice_group=cgroup,
                                kind="support_downgrade",
                                label=f"{g_label}: {sp_entry.name} -> {sp_entry.downgrade_to} (-{freed} Spirit)",
                                spirit_delta=freed,
                                delta_dps=ddps,
                                delta_ehp=dehp,
                                loss_score=_loss_score(dps0, ehp0, ddps, dehp),
                                rpc_payload={"method": "set_gem", "params": {"group": g_idx, "index": gem_idx, "nameSpec": sp_entry.downgrade_to}},
                            )
                        )
                with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
                    worker.call("set_gem", {"group": g_idx, "index": gem_idx, "enabled": False})
                    _t1, unres1, dps1, ehp1 = _read_spirit_and_combat(worker)
                freed = round(unres1 - unres0)
                if freed > 0:
                    ddps, dehp = dps1 - dps0, ehp1 - ehp0
                    options.append(
                        SpiritOption(
                            option_id=f"dis_{cgroup}",
                            choice_group=cgroup,
                            kind="support_disable",
                            label=f"{g_label}: disable {sp_entry.name} (-{freed} Spirit)",
                            spirit_delta=freed,
                            delta_dps=ddps,
                            delta_ehp=dehp,
                            loss_score=_loss_score(dps0, ehp0, ddps, dehp),
                            rpc_payload={"method": "set_gem", "params": {"group": g_idx, "index": gem_idx, "enabled": False}},
                        )
                    )

            # 2. Minion count reductions (gem.count > 1)
            cnt = int(gem.get("count") or 1)
            if cnt > 1 and not gem.get("isSupport"):
                cgroup = f"g{g_idx}_min{gem_idx}"
                for new_cnt in range(cnt - 1, max(0, cnt - 4), -1):
                    with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
                        worker.call("set_gem", {"group": g_idx, "index": gem_idx, "count": new_cnt})
                        _t1, unres1, dps1, ehp1 = _read_spirit_and_combat(worker)
                    freed = round(unres1 - unres0)
                    if freed > 0:
                        ddps, dehp = dps1 - dps0, ehp1 - ehp0
                        options.append(
                            SpiritOption(
                                option_id=f"min_{cgroup}_{new_cnt}",
                                choice_group=cgroup,
                                kind="minion_count",
                                label=f"{g_label}: {gname} count {cnt} -> {new_cnt} (-{freed} Spirit)",
                                spirit_delta=freed,
                                delta_dps=ddps,
                                delta_ehp=dehp,
                                loss_score=_loss_score(dps0, ehp0, ddps, dehp),
                                rpc_payload={"method": "set_gem", "params": {"group": g_idx, "index": gem_idx, "count": new_cnt}},
                            )
                        )

        # 3. Non-main persistent buff / aura group toggle
        if g_idx != main_idx:
            with BuildTransaction(worker, scope=BuildScope.ACTIVE_SETS, will_commit=False):
                worker.call("set_group", {"index": g_idx, "enabled": False})
                _t1, unres1, dps1, ehp1 = _read_spirit_and_combat(worker)
            freed = round(unres1 - unres0)
            if freed > 0:
                ddps, dehp = dps1 - dps0, ehp1 - ehp0
                options.append(
                    SpiritOption(
                        option_id=f"aura_g{g_idx}",
                        choice_group=f"group_{g_idx}",
                        kind="aura_toggle",
                        label=f"Disable persistent buff `{g_label}` (-{freed} Spirit)",
                        spirit_delta=freed,
                        delta_dps=ddps,
                        delta_ehp=dehp,
                        loss_score=_loss_score(dps0, ehp0, ddps, dehp) + 1.5,  # Slight tie-break preference for support cuts over whole aura cuts
                        rpc_payload={"method": "set_group", "params": {"index": g_idx, "enabled": False}},
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
    _tot, unres, _dps, _ehp = _read_spirit_and_combat(worker)
    deficit = deficit_override if deficit_override is not None else max(0, round(-unres))
    missing_quests = _check_campaign_spirit_quests(worker)
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
        )

    evaluated = evaluate_live_spirit_options(worker, catalog=cat)
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
    cat = catalog or load_rune_instill_catalog()
    is_ehp = any(k in metric.lower() for k in ("ehp", "def", "life", "surv"))
    presets: tuple[tuple[str, str, int, float, float], ...] = (
        ("Wind Dancer", "buff_wind_dancer", 30, 8.0, 26.0),
        ("Ghost Dance", "buff_ghost_dance", 30, 0.0, 28.0),
        ("Herald of Ice", "buff_hoi", 30, 22.0, 4.0),
        ("Herald of Thunder", "buff_hot", 30, 24.0, 2.0),
        ("Herald of Ash", "buff_hoa", 30, 18.0, 0.0),
        ("Archmage", "buff_archmage", 100, 75.0, 0.0),
        ("Blasphemy", "buff_blasphemy", 60, 28.0, 18.0),
        ("Cast on Critical", "buff_coc", 100, 68.0, 0.0),
        ("Grim Feast", "buff_grim_feast", 30, 0.0, 32.0),
        ("Charge Regulation", "buff_charge_reg", 30, 20.0, 12.0),
    )
    opts: list[SpiritOption] = []
    for label, grp, cost, dps_v, ehp_v in presets:
        val = ehp_v if is_ehp else dps_v
        opts.append(
            SpiritOption(
                option_id=grp,
                choice_group=grp,
                kind="pack_item",
                label=f"{label} ({cost} Spirit)",
                spirit_delta=cost,
                delta_dps=dps_v,
                delta_ehp=ehp_v,
                value_score=val,
            )
        )
    # Also include tiered fixed-Spirit supports as mutually exclusive tier choices
    by_family: dict[str, list[Any]] = {}
    for sp in cat.spirit_supports:
        fam = sp.name.rsplit(" ", 1)[0] if sp.name.endswith((" I", " II", " III")) else sp.name
        by_family.setdefault(fam, []).append(sp)
    for fam, tiers in sorted(by_family.items()):
        for sp in tiers:
            low = sp.name.lower()
            dps_v = 9.0 if ("precision" in low or "direstrike" in low or "herald" in low) else 3.0
            ehp_v = 11.0 if any(k in low for k in ("vitality", "cannibalism", "herbalism", "clarity", "strong", "warm", "cool")) else 4.0
            if sp.name.endswith(" II"):
                dps_v *= 1.5
                ehp_v *= 1.5
            opts.append(
                SpiritOption(
                    option_id=f"sup_{sp.skill_id}",
                    choice_group=f"sup_{fam}",
                    kind="pack_item",
                    label=f"{sp.name} ({sp.spirit_cost} Spirit)",
                    spirit_delta=sp.spirit_cost,
                    delta_dps=dps_v,
                    delta_ehp=ehp_v,
                    value_score=ehp_v if is_ehp else dps_v,
                )
            )
    return opts


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
