"""Two-pass Pinnacle dewater physical engine: Pass 1 constants/clamps + Pass 2 clean-base clamp/verify (L2)."""

from __future__ import annotations

import math
import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch, _finite_float
from .dewater_mechanics import (
    AILMENT_CLAMP_SPECS,
    _effective_hit_rate,
    _find_config_item,
    _remove_stripped_config,
    _set_configs_verified,
    is_proxy_main_skill,
    refine_post_dewater_mechanics,
)
from .dewater_provenance import _group_gem_names, _iter_active_groups, supports_dewater_probes

_AVERAGE_MODE_VARS = frozenset({"buffheartstopper", "warcrymode", "pactmode"})
_NEARBY_ENEMY_VARS = frozenset({"multipliernearbyenemies", "multipliernearbyrareoruniqueenemies"})
_MELEE_FORTIFY_RE = re.compile(r"melee\s+hits?\s+(?:have\s+\d+%\s+chance\s+to\s+)?fortify", re.IGNORECASE)

# Keys temporarily cleared in Pass 1 so Pass 2 evaluates on a clean, non-ramping Boss baseline.
PASS1_RAMPING_KEYS = frozenset({
    "conditionenemyshocked",
    "conditionshockeffect",
    "conditionenemychilled",
    "conditionenemychilledeffect",
    "conditionenemyscorched",
    "conditionscorchedeffect",
    "conditionenemybrittle",
    "conditionbrittleeffect",
    "conditionenemysapped",
    "conditionsapeffect",
    "conditioncritrecently",
    "multipliermanaspentrecently",
    "overridefortification",
    "overridebuffelusive",
    "bufffortification",
    "buffelusive",
    "multiplierrage",
    "multiplierwitheredstackcount",
    "shockstacks",
    "conditionlowlife",
    "conditionenemybleeding",
})


def _record_pass2_warning(stripped: dict[str, Any], message: str) -> None:
    stripped.setdefault("pass2Warnings", []).append(message)


def pass1_config_action(
    var: str,
    val: Any,
    *,
    auto_keep_live: bool,
    is_close_range: bool,
    auto_keep_reasons: dict[str, str] | None,
) -> tuple[str, Any, str]:
    """Return `(action, target_val, reason)` where action is `'keep'`, `'clamp'`, or `'reset'`."""
    vlow = var.lower()
    if vlow in PASS1_RAMPING_KEYS:
        return "reset", None, ""
    if auto_keep_live and vlow in _NEARBY_ENEMY_VARS:
        try:
            num = float(val)
        except (TypeError, ValueError):
            num = 0.0
        if num >= 1.0:
            return ("keep", val, "Single Boss constant (=1)") if abs(num - 1.0) <= 1e-6 else ("clamp", 1, "clamped to single Boss (=1)")
    if vlow in _AVERAGE_MODE_VARS:
        if str(val).upper() == "AVERAGE":
            return "keep", val, "PoB AVERAGE uptime mode"
        if auto_keep_live:
            return "clamp", "AVERAGE", "clamped to PoB AVERAGE uptime mode"
    if auto_keep_live and vlow == "conditionatcloserange" and is_close_range:
        return "keep", val, "Melee / close-range main skill"
    if vlow == "traumastacks":
        return "reset", None, ""
    reason = (auto_keep_reasons or {}).get(vlow)
    return ("keep", val, reason) if reason else ("reset", None, "")


def _has_wither_source(skills_res: dict[str, Any] | None, node_names: set[str], mods_low: str, hit_rate: float) -> tuple[int, str]:
    if any("withering presence" in n for n in node_names):
        return 15, "Occultist Withering Presence (15 stacks)"
    for grp in _iter_active_groups(skills_res):
        gnames = {nm for _, nm in _group_gem_names(grp)}
        if "wither" in gnames and any(t in g for g in gnames for t in ("spell totem", "arcanist brand")):
            return 15, "Wither Totem/Brand (15 stacks)"
    has_reprisal = any("nature's reprisal" in n for n in node_names)
    has_touch = any("withering touch" in nm for grp in _iter_active_groups(skills_res) for _, nm in _group_gem_names(grp))
    has_unholy = "unholy might" in mods_low and "on kill" not in mods_low
    if has_reprisal or has_touch or has_unholy:
        src = "Pathfinder Nature's Reprisal" if has_reprisal else ("Withering Touch" if has_touch else "Unholy Might")
        stacks = min(15, math.floor(max(hit_rate, 0.0) * 0.25 * 2.0 + 1e-6))
        if stacks >= 1:
            return stacks, f"{src} ({hit_rate:.1f}/s x 25% x 2s -> {stacks})"
    return 0, ""


def _has_fortify_source(skills_res: dict[str, Any] | None, node_names: set[str], mods_low: str, ms: dict[str, Any]) -> bool:
    if ms.get("canFortify") or any("overlord" in n or "fortitude" in n for n in node_names) or _MELEE_FORTIFY_RE.search(mods_low):
        return True
    main_idx = (skills_res or {}).get("mainSocketGroup")
    for grp in _iter_active_groups(skills_res):
        if bool(grp.get("isMain")) or (main_idx is not None and grp.get("index") == main_idx):
            if any("fortify" in nm for gm, nm in _group_gem_names(grp) if gm.get("support")):
                return True
    return False


def _has_petrified_blood(skills_res: dict[str, Any] | None) -> bool:
    return any("petrified blood" in nm for grp in _iter_active_groups(skills_res) for gm, nm in _group_gem_names(grp) if not gm.get("support"))


def _has_elusive_source(skills_res: dict[str, Any] | None, node_names: set[str], flags: dict[str, Any]) -> bool:
    if any("mistwalker" in n for n in node_names) or bool(flags.get("canBeElusive")):
        return True
    return any("nightblade" in nm for grp in _iter_active_groups(skills_res) for gm, nm in _group_gem_names(grp) if gm.get("support"))


def _eval_ignite_bleed_sustain(chance: float, dps: float, dur: float, hit_rate: float) -> bool:
    if dps <= 0.0 or chance <= 0.0:
        return False
    return chance >= 99.5 or (hit_rate * (chance / 100.0) * max(dur, 1.0)) >= 3.0


def _collect_node_and_mod_context(
    tree_summary: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
    alloc_node_stats: list[tuple[str, list[str]]] | None,
) -> tuple[set[str], str]:
    nodes: set[str] = set()
    lines: list[str] = []
    if isinstance(tree_summary, dict):
        for bucket in ("keystones", "ascendancyNodes", "notables"):
            for nd in tree_summary.get(bucket) or []:
                if isinstance(nd, dict):
                    nm = strip_pob_escapes(str(nd.get("name") or "")).strip().lower()
                    if nm:
                        nodes.add(nm)
                    lines.extend(strip_pob_escapes(str(x)) for x in (nd.get("stats") or []))
    for lbl, st_lines in alloc_node_stats or []:
        nm = strip_pob_escapes(str(lbl or "")).strip().lower()
        if nm:
            nodes.add(nm)
        lines.extend(strip_pob_escapes(str(x)) for x in st_lines)
    for entry in equipped_slots_raw or []:
        if isinstance(entry, dict) and entry.get("raw") and "Swap" not in str(entry.get("slotName") or ""):
            lines.append(strip_pob_escapes(str(entry["raw"])))
    return nodes, "\n".join(lines).lower()


def _record_clamp_mutation(
    var: str, orig_val: Any, clamped_val: Any, reason: str, stripped: dict[str, list[str]], records: list[dict[str, Any]]
) -> None:
    _remove_stripped_config(stripped, records, var.lower())
    stripped.setdefault("autoClamped", []).append(f"{var}={orig_val!r} -> {clamped_val!r} ({reason})")
    stripped["config"].append(f"{var}={orig_val!r} -> {clamped_val!r} ({reason})")
    records.append({
        "dimension": "config", "key": var, "label": f"{var}={orig_val!r} -> {clamped_val!r} ({reason})",
        "restore": [("set_config", {"var": var, "value": orig_val})],
        "revert": [("set_config", {"var": var, "value": clamped_val})],
        "probe": {"kind": "config", "var": var}, "original": orig_val,
    })


def _plan_pass2_ailments(
    cfg_res: dict[str, Any], stripped_keys: set[str], keep_set: set[str], ailments: dict[str, Any],
    boss_name: str, assignments: list[tuple[str, Any]], actions: list[tuple[str, str, Any, Any, str]],
) -> None:
    for a_name, flag_low, eff_low, min_t, def_max in AILMENT_CLAMP_SPECS:
        if flag_low in keep_set or eff_low in keep_set or flag_low not in stripped_keys:
            continue
        f_item = _find_config_item(cfg_res, flag_low)
        a_info = ailments.get(a_name) if isinstance(ailments, dict) else None
        if not f_item or not f_item[1] or not isinstance(a_info, dict) or not a_info.get("canInflict"):
            continue
        computed = _finite_float(a_info.get("computedPct"))
        cap = min(max(int(min_t), int(_finite_float(a_info.get("maxCap"), float(def_max)))), math.floor(computed + 1e-6))
        if computed < min_t or cap < min_t:
            continue
        f_var = f_item[0]
        e_item = _find_config_item(cfg_res, eff_low)
        e_var = e_item[0] if e_item else ""
        try:
            orig_eff = round(float(e_item[1])) if (e_item and e_item[1] not in (None, False, "", 0)) else 0
        except (TypeError, ValueError):
            orig_eff = 0
        a_low = a_name.lower()
        if orig_eff <= 0 or not e_var:
            assignments.append((f_var, True))
            actions.append(("keep", f_var, True, True, f"PoB computed {boss_name} Boss {a_low} {computed:.0f}% >= {min_t:.0f}%"))
        elif orig_eff <= cap:
            reason = f"PoB computed {boss_name} Boss {a_low} {computed:.0f}% (cap {cap}%) >= configured {orig_eff}%"
            assignments.extend([(f_var, True), (e_var, orig_eff)])
            actions.extend([("keep", f_var, True, True, reason), ("keep", e_var, orig_eff, orig_eff, reason)])
        else:
            assignments.extend([(f_var, True), (e_var, cap)])
            actions.append(("keep", f_var, True, True, f"PoB computed {boss_name} Boss {a_low} {computed:.0f}%, clamped {orig_eff}% -> {cap}%"))
            actions.append(("clamp", e_var, orig_eff, cap, f"clamped to PoB {boss_name} Boss {a_low} {cap}%"))


def _plan_pass2_numeric_and_buffs(
    cfg_res: dict[str, Any], stripped_keys: set[str], keep_set: set[str], prov: dict[str, Any],
    skills_res: dict[str, Any] | None, flask_verdict: dict[str, Any] | None,
    node_names: set[str], mods_low: str, assignments: list[tuple[str, Any]], actions: list[tuple[str, str, Any, Any, str]],
) -> None:
    flags, ailments = prov.get("flags") or {}, prov.get("ailments") or {}
    ms = flags.get("mainSkill") or {}
    is_proxy = bool(ms.get("isProxy")) or is_proxy_main_skill(skills_res)
    hit_rate = _effective_hit_rate({"Speed": ms.get("speed"), "HitChance": ms.get("hitChance")})
    crit_ch, eth = _finite_float(ms.get("critChance")), _finite_float(flags.get("enemyThreshold"))

    def _keep_or_clamp_int(var_low: str, cap: int, keep_r: str, clamp_r: str) -> None:
        if var_low in keep_set or var_low not in stripped_keys or cap < 1:
            return
        found = _find_config_item(cfg_res, var_low)
        if not found:
            return
        try:
            orig = round(float(found[1]))
        except (TypeError, ValueError):
            return
        if orig <= 0:
            return
        if orig <= cap:
            assignments.append((found[0], orig))
            actions.append(("keep", found[0], orig, orig, keep_r))
        else:
            assignments.append((found[0], cap))
            actions.append(("clamp", found[0], orig, cap, clamp_r))

    if "conditioncritrecently" in stripped_keys and "conditioncritrecently" not in keep_set and not is_proxy:
        found = _find_config_item(cfg_res, "conditioncritrecently")
        if found and found[1] and crit_ch >= 35.0 and hit_rate > 0.0:
            prob_4s = 1.0 - ((1.0 - min(1.0, crit_ch / 100.0)) ** (4.0 * hit_rate))
            if prob_4s >= 0.90:
                assignments.append((found[0], True))
                actions.append(("keep", found[0], True, True, f"post-dewater CritChance {crit_ch:.1f}% @ {hit_rate:.2f}/s (4s {prob_4s * 100:.1f}%)"))
    if "conditionenemyignited" in stripped_keys and "conditionenemyignited" not in keep_set:
        found = _find_config_item(cfg_res, "conditionenemyignited")
        if found and found[1] and _eval_ignite_bleed_sustain(_finite_float(ms.get("igniteChance")), _finite_float(ms.get("igniteDps")), _finite_float(ms.get("igniteDuration")) or 4.0, hit_rate):
            assignments.append((found[0], True))
            actions.append(("keep", found[0], True, True, f"Sustained Ignite ({_finite_float(ms.get('igniteChance')):.1f}% @ {hit_rate:.2f}/s)"))
    if "conditionenemybleeding" in stripped_keys and "conditionenemybleeding" not in keep_set:
        found = _find_config_item(cfg_res, "conditionenemybleeding")
        if found and found[1] and _eval_ignite_bleed_sustain(_finite_float(ms.get("bleedChance")), _finite_float(ms.get("bleedDps")), _finite_float(ms.get("bleedDuration")) or 5.0, hit_rate):
            assignments.append((found[0], True))
            actions.append(("keep", found[0], True, True, f"Sustained Bleed ({_finite_float(ms.get('bleedChance')):.1f}% @ {hit_rate:.2f}/s)"))
    if (flags.get("canGainRage") and not is_proxy) or _finite_float(flags.get("rageRegen")) > 0.0:
        max_r = max(1, int(_finite_float(flags.get("maximumRage")) or 30))
        _keep_or_clamp_int("multiplierrage", max_r, f"Sustained Rage (<= {max_r} max)", f"clamped to MaximumRage {max_r}")
    if "bufffortification" in stripped_keys and "bufffortification" not in keep_set and not is_proxy and ms.get("isMelee"):
        found = _find_config_item(cfg_res, "bufffortification")
        if found and found[1] and _has_fortify_source(skills_res, node_names, mods_low, ms) and (eth <= 0.0 or _finite_float(ms.get("averageHit")) >= 0.005 * eth):
            assignments.append((found[0], True))
            actions.append(("keep", found[0], True, True, "Non-proxy melee Fortify hit >= 0.5% Boss threshold"))
    if "multiplierwitheredstackcount" in stripped_keys and "multiplierwitheredstackcount" not in keep_set:
        w_cap, w_reason = _has_wither_source(skills_res, node_names, mods_low, hit_rate)
        _keep_or_clamp_int("multiplierwitheredstackcount", w_cap, w_reason, f"clamped Wither to {w_cap} ({w_reason})")
    if flags.get("shockCanStack") and "shockstacks" in stripped_keys:
        s_info = ailments.get("Shock") if isinstance(ailments, dict) else {}
        s_ch = (_finite_float((s_info or {}).get("chance")) or 100.0) / 100.0
        s_dur = _finite_float((s_info or {}).get("duration")) or 2.0
        s_cap = min(int(_finite_float(flags.get("shockStacksMax")) or 50), math.floor(hit_rate * s_ch * s_dur + 1e-6))
        _keep_or_clamp_int("shockstacks", s_cap, f"Warden ShockStacks <= {s_cap}", f"clamped Warden ShockStacks to {s_cap}")
    if "buffelusive" in stripped_keys and "buffelusive" not in keep_set and not is_proxy:
        found = _find_config_item(cfg_res, "buffelusive")
        if found and found[1] and _has_elusive_source(skills_res, node_names, flags):
            assignments.append((found[0], True))
            actions.append(("keep", found[0], True, True, "Mistwalker/Nightblade Elusive (50% PoB average)"))
    if "conditionlowlife" in stripped_keys and "conditionlowlife" not in keep_set:
        found = _find_config_item(cfg_res, "conditionlowlife")
        life_pct = 100.0 if ms.get("lifeUnreservedPct") is None else _finite_float(ms.get("lifeUnreservedPct"))
        sust_lf = bool((flask_verdict or {}).get("sustainedLifeFlask"))
        if found and found[1] and (life_pct <= 50.0 or (_has_petrified_blood(skills_res) and not sust_lf)):
            assignments.append((found[0], True))
            actions.append(("keep", found[0], True, True, "Life unreserved <= 50%" if life_pct <= 50.0 else "Petrified Blood Low Life (no sustained Life Flask)"))
    if "multipliermanaspentrecently" in stripped_keys and "multipliermanaspentrecently" not in keep_set:
        m_cap = math.floor(min(4.0 * _finite_float(ms.get("manaSpendPerSec")), 4.0 * _finite_float(ms.get("sustainableManaRecovPerSec"))) + 1e-6)
        _keep_or_clamp_int("multipliermanaspentrecently", m_cap, f"Sustainable 4s Mana spend (<= {m_cap})", f"clamped to 4s sustainable Mana spend ({m_cap})")


def _revoke_ignite_if_unsustained(
    worker: BridgeWorker, cfg_res: dict[str, Any], stripped: dict[str, list[str]], records: list[dict[str, Any]],
    post_ig: float, ig_dps: float, ig_dur: float, hit_rate: float,
) -> None:
    if _eval_ignite_bleed_sustain(post_ig, ig_dps, ig_dur, hit_rate):
        return
    found = _find_config_item(cfg_res, "conditionenemyignited")
    ig_var, ig_val = found if found else ("conditionEnemyIgnited", True)
    _call_batch(worker, [("reset_config", {"var": ig_var})])
    stripped["autoKept"] = [s for s in stripped.get("autoKept", []) if not s.lower().startswith("conditionenemyignited=")]
    ig_pct_str = f"{post_ig:.0f}%" if abs(post_ig - round(post_ig)) < 1e-6 else f"{post_ig:.1f}%"
    stripped["config"].append(f"{ig_var}={ig_val!r} -> reset (post-dewater IgniteChance {ig_pct_str} < 99.5%)")
    records.append({
        "dimension": "config", "key": ig_var, "label": f"{ig_var}={ig_val!r}",
        "restore": [("set_config", {"var": ig_var, "value": ig_val})], "revert": [("reset_config", {"var": ig_var})],
        "probe": {"kind": "config", "var": ig_var}, "original": ig_val,
    })


def run_pass2_clamp_and_verify(
    worker: BridgeWorker, cfg_res: dict[str, Any], keep_set: set[str], stripped: dict[str, Any], records: list[dict[str, Any]],
    *, auto_keep_reasons: dict[str, str] | None = None, skills_res: dict[str, Any] | None = None,
    flask_verdict: dict[str, Any] | None = None, tree_summary: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None, alloc_node_stats: list[tuple[str, list[str]]] | None = None,
    auto_keep_live: bool = True,
) -> bool:
    """Execute Pass 2 on the clean post-Pass-1 baseline with at most 1 extra recalc. Returns True unless degraded."""
    if not auto_keep_live:
        return True
    stripped_keys = {str(r.get("key") or "").lower() for r in records if r.get("dimension") == "config"}
    ig_check = "IgniteChance" in ((auto_keep_reasons or {}).get("conditionenemyignited") or "") and any(
        s.lower().startswith("conditionenemyignited=") for s in stripped.get("autoKept", [])
    )
    if not (stripped_keys & (PASS1_RAMPING_KEYS | {"conditionenemyignited"})) and not ig_check:
        return True

    if not supports_dewater_probes(worker):
        ok = True
        if ig_check:
            try:
                st_b = _call_batch(worker, [("get_stats", {})], raise_on_error=False)
                st = st_b[0].get("stats") if (st_b and isinstance(st_b[0], dict) and not st_b[0].get("_error")) else None
                if isinstance(st, dict):
                    ig_dps = 1.0 if st.get("IgniteDPS") is None else _finite_float(st.get("IgniteDPS"))
                    ig_dur = _finite_float(st.get("IgniteDuration")) or 4.0
                    _revoke_ignite_if_unsustained(
                        worker, cfg_res, stripped, records,
                        max(_finite_float(st.get("IgniteChance")), _finite_float(st.get("EnemyIgniteChance"))),
                        ig_dps, ig_dur, _effective_hit_rate(st),
                    )
                else:
                    _record_pass2_warning(stripped, "ignite-undo skipped: get_stats returned no stats")
                    ok = False
            except Exception as exc:
                _record_pass2_warning(stripped, f"ignite-undo failed: {exc}")
                ok = False
        refine_post_dewater_mechanics(worker, cfg_res, keep_set, stripped, records, skills_res=skills_res)
        return ok

    need_ailments = bool(stripped_keys & ({f for _, f, _, _, _ in AILMENT_CLAMP_SPECS} | {"shockstacks"}))
    prov: Any = None
    prov_err: str | None = None
    try:
        prov = worker.call("get_dewater_provenance", {"includeAilments": need_ailments})
    except Exception as exc:
        prov_err = str(exc) or type(exc).__name__
    if not isinstance(prov, dict) or prov.get("ok") is not True:
        detail = prov_err or (str(prov.get("_error") or prov.get("error")) if isinstance(prov, dict) else "invalid response")
        _record_pass2_warning(stripped, f"get_dewater_provenance failed ({detail}); fell back to basic mechanics")
        refine_post_dewater_mechanics(worker, cfg_res, keep_set, stripped, records, skills_res=skills_res)
        return False

    pass2_ok = True
    if ig_check:
        try:
            ms = (prov.get("flags") or {}).get("mainSkill") or {}
            _revoke_ignite_if_unsustained(
                worker, cfg_res, stripped, records, _finite_float(ms.get("igniteChance")),
                _finite_float(ms.get("igniteDps")), _finite_float(ms.get("igniteDuration"), 4.0),
                _effective_hit_rate({"Speed": ms.get("speed"), "HitChance": ms.get("hitChance")}),
            )
        except Exception as exc:
            _record_pass2_warning(stripped, f"ignite-undo failed: {exc}")
            pass2_ok = False

    assignments: list[tuple[str, Any]] = []
    actions: list[tuple[str, str, Any, Any, str]] = []
    boss_name = str((prov.get("flags") or {}).get("enemyIsBoss") or "Pinnacle")
    _plan_pass2_ailments(cfg_res, stripped_keys, keep_set, prov.get("ailments") or {}, boss_name, assignments, actions)
    node_names, mods_low = _collect_node_and_mod_context(tree_summary, equipped_slots_raw, alloc_node_stats)
    _plan_pass2_numeric_and_buffs(cfg_res, stripped_keys, keep_set, prov, skills_res, flask_verdict, node_names, mods_low, assignments, actions)

    if not assignments:
        return pass2_ok
    if not _set_configs_verified(worker, assignments):
        _record_pass2_warning(
            stripped,
            f"Pass 2 config verification failed for {len(assignments)} key(s); fell back to basic mechanics",
        )
        refine_post_dewater_mechanics(worker, cfg_res, keep_set, stripped, records, skills_res=skills_res)
        return False
    for kind, var, orig_val, new_val, reason in actions:
        if kind == "keep":
            _remove_stripped_config(stripped, records, var.lower())
            stripped.setdefault("autoKept", []).append(f"{var}={new_val!r} (auto-kept: {reason})")
        elif kind == "clamp":
            _record_clamp_mutation(var, orig_val, new_val, reason, stripped, records)
    return pass2_ok
