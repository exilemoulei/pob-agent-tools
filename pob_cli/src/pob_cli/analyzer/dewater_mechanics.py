"""Precision dewatering mechanics: on-hit debuffs/charges, proxy-safe crit & Boss shock clamp (L2)."""

from __future__ import annotations

import math
import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch, _finite_float
from .dewater_steps import _extract_probe_state, _rpc_error_text, _same_state

_PROXY_SUPPORT_NAMES = frozenset(
    {
        "spell totem",
        "ballista totem",
        "ranged attack totem",
        "trap",
        "cluster traps",
        "multiple traps",
        "high-impact mine",
        "blastchain mine",
        "minefield",
        "remote mine",
        "trap and mine damage",
    }
)
_PROXY_SUPPORT_IDS = frozenset(
    {
        "SupportSpellTotem",
        "SupportBallistaTotem",
        "SupportTrap",
        "SupportClusterTraps",
        "SupportMultipleTraps",
        "SupportRemoteMine",
        "SupportHighImpactMine",
        "SupportBlastchainMine",
        "SupportMinefield",
    }
)
_PROXY_ACTIVE_RE = re.compile(
    r"\b(?:totem|ballista)\b|\b(?:trap|mine)s?$|^(?:summon|raise|animate)\s+|herald\s+of\s+agony|"
    r"图腾|圖騰|弩炮|陷阱|地雷|召唤|召喚|幻化",
    re.IGNORECASE,
)

_ON_KILL_EXCLUDE_RE = re.compile(
    r"\bon\s+kill\b|\bwhen\s+you\s+kill\b|\bkilled\s+recently\b|\byou\s+kill\b|\byou\s+or\s+your\s+\w+\s+kill\b",
    re.IGNORECASE,
)
_ON_FULL_LIFE_RE = re.compile(r"\bon\s+full\s+life\b|\bfull\s+life\b", re.IGNORECASE)

_DEBUFF_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]] = {
    "conditionenemyunnerved": (
        re.compile(r"nearby\s+enemies\s+are\s+unnerved|curses?\s+.*?\bunnerve\b", re.IGNORECASE),
        re.compile(
            r"(?<!chance to )\b(?:unnerve\s+enemies|inflict\s+unnerve)\b.*?\bon\s+(?:spell\s+)?hit\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\d+(?:\.\d+)?)%\s+chance\s+to\s+unnerve\s+enemies\b.*?\bon\s+(?:spell\s+)?hit\b",
            re.IGNORECASE,
        ),
    ),
    "conditionenemyintimidated": (
        re.compile(r"nearby\s+enemies\s+are\s+intimidated", re.IGNORECASE),
        re.compile(
            r"(?<!chance to )\b(?:intimidate\s+enemies|inflict\s+intimidate)\b.*?\bon\s+(?:attack\s+|melee\s+)?hit\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\d+(?:\.\d+)?)%\s+chance\s+to\s+intimidate\s+enemies\b.*?\bon\s+(?:attack\s+|melee\s+)?hit\b",
            re.IGNORECASE,
        ),
    ),
    "conditionenemymaimed": (
        re.compile(r"nearby\s+enemies\s+are\s+maimed", re.IGNORECASE),
        re.compile(
            r"(?<!chance to )\b(?:maim\s+enemies|inflict\s+maim)\b.*?\bon\s+(?:attack\s+|melee\s+)?hit\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\d+(?:\.\d+)?)%\s+chance\s+to\s+maim\s+(?:enemies\s+)?.*?\bon\s+(?:attack\s+|melee\s+)?hit\b",
            re.IGNORECASE,
        ),
    ),
}

_CHARGE_ON_HIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)%\s+chance\s+to\s+gain\s+an?\s+(endurance|frenzy|power)\s+charge\s+"
    r"(?:when\s+you\s+(hit|crit|critically\s+strike)|on\s+(critical\s+strike|hit))\b",
    re.IGNORECASE,
)
_ALLY_OWNED_CHARGE_RE = re.compile(
    r"\b(?:minions?|allies)\b|召唤物|召喚物|友军|友軍",
    re.IGNORECASE,
)


def is_proxy_main_skill(skills_res: dict[str, Any] | None) -> bool:
    """True when the main skill group deals damage via a proxy (Totem, Trap, Mine, or pure Minion)."""
    if not isinstance(skills_res, dict):
        return False
    groups = skills_res.get("groups") or []
    main_idx = skills_res.get("mainSocketGroup")
    mg = next(
        (g for g in groups if isinstance(g, dict) and (g.get("isMain") or (main_idx is not None and g.get("index") == main_idx))),
        None,
    )
    if not mg or not mg.get("enabled", True):
        return False
    enabled_gems = [gm for gm in (mg.get("gems") or []) if isinstance(gm, dict) and gm.get("enabled", True)]
    for gm in enabled_gems:
        if not gm.get("support"):
            continue
        sid = str(gm.get("skillId") or "")
        nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip().lower()
        base = nm[:-8].strip() if nm.endswith(" support") else nm
        if base.startswith("awakened "):
            base = base[9:].strip()
        if sid in _PROXY_SUPPORT_IDS or base in _PROXY_SUPPORT_NAMES:
            return True

    raw_mas = mg.get("mainActiveSkill")
    try:
        mas = int(raw_mas) if raw_mas is not None else None
    except (TypeError, ValueError):
        mas = None

    skills_list = [s for s in (mg.get("skills") or []) if isinstance(s, dict)]
    if skills_list:
        sel_idx = mas if (mas is not None and 1 <= mas <= len(skills_list)) else 1
        sel_sk = skills_list[sel_idx - 1]
        snm = strip_pob_escapes(str(sel_sk.get("name") or sel_sk.get("label") or "")).strip().lower()
        snm_zh = strip_pob_escapes(str(sel_sk.get("nameZh") or "")).strip().lower()
        return bool(_PROXY_ACTIVE_RE.search(snm) or (snm_zh and _PROXY_ACTIVE_RE.search(snm_zh)))

    active_gems = [gm for gm in enabled_gems if not gm.get("support")]
    if not active_gems:
        return False
    if mas is not None and 1 <= mas <= len(active_gems):
        target_gems = [active_gems[mas - 1]]
    elif len(active_gems) == 1:
        target_gems = active_gems
    else:
        target_gems = [active_gems[0]]
    for gm in target_gems:
        nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip().lower()
        nm_zh = strip_pob_escapes(str(gm.get("nameZh") or "")).strip().lower()
        if _PROXY_ACTIVE_RE.search(nm) or (nm_zh and _PROXY_ACTIVE_RE.search(nm_zh)):
            return True
    return False


def _effective_hit_rate(stats: dict[str, Any] | None) -> float:
    st = stats or {}
    speed = _finite_float(st.get("Speed"), default=0.0)
    if speed <= 0.0:
        return 0.0
    raw_hc = st.get("HitChance")
    if raw_hc is None:
        return speed
    hc = _finite_float(raw_hc, default=0.0)
    return speed * min(1.0, max(0.0, hc / 100.0))


def _hit_trigger_matches_skill(line_low: str, stats: dict[str, Any] | None) -> bool:
    """Reject obvious Spell-vs-Attack mismatch for qualified `on spell/attack/melee hit` mods."""
    st = stats or {}
    is_attack = _finite_float(st.get("MainHandAccuracy"), default=0.0) > 0.0 or (
        _finite_float(st.get("OffHandAccuracy"), default=0.0) > 0.0
    )
    is_spell = (st.get("HitChance") is None) and not is_attack
    if "on spell hit" in line_low and is_attack:
        return False
    if ("on attack hit" in line_low or "on melee hit" in line_low) and is_spell:
        return False
    return True


def find_on_hit_charge_sources(
    mod_lines: list[str],
    stats: dict[str, Any] | None,
) -> dict[str, str]:
    """Return `{use<kind>charges: reason}` for Boss-sustainable offensive on-hit charge mods."""
    hit_rate = _effective_hit_rate(stats)
    if hit_rate <= 0.0:
        return {}
    st = stats or {}
    crit_pct = max(
        _finite_float(st.get("EffectiveCritChance"), default=0.0),
        _finite_float(st.get("CritChance"), default=0.0),
    )
    crit_factor = min(1.0, max(0.0, crit_pct / 100.0))

    best: dict[str, tuple[float, str]] = {}
    for raw_ln in mod_lines:
        ln = strip_pob_escapes(str(raw_ln or "")).strip()
        if not ln or _ON_KILL_EXCLUDE_RE.search(ln):
            continue
        low = ln.lower()
        if "when you are hit" in low or "when hit" in low or _ALLY_OWNED_CHARGE_RE.search(ln):
            continue
        m = _CHARGE_ON_HIT_RE.search(ln)
        if not m:
            continue
        pct = float(m.group(1))
        kind = m.group(2).lower()
        trigger_tok = (m.group(3) or m.group(4) or "").lower()
        eff_rate = hit_rate * crit_factor if "crit" in trigger_tok else hit_rate
        expected_10s = 10.0 * eff_rate * (pct / 100.0)
        if expected_10s < 1.5:
            continue
        cfg_key = f"use{kind}charges"
        prev = best.get(cfg_key)
        if prev is None or expected_10s > prev[0]:
            best[cfg_key] = (
                expected_10s,
                f"On-hit {kind.title()} Charge ({pct:.0f}% @ {eff_rate:.1f}/s -> {expected_10s:.1f}/10s)",
            )
    return {k: v[1] for k, v in best.items()}


def _scan_debuff_lines(
    lines: list[str],
    *,
    hit_rate: float,
    stats: dict[str, Any] | None,
    check_skill_type: bool,
    out: dict[str, str],
) -> None:
    for cfg_key, (passive_re, guar_hit_re, prob_hit_re) in _DEBUFF_PATTERNS.items():
        if cfg_key in out:
            continue
        best_prob = 0.0
        best_pct = 0.0
        guar_line: str | None = None
        for raw_ln in lines:
            ln = strip_pob_escapes(str(raw_ln or "")).strip()
            if not ln or _ON_KILL_EXCLUDE_RE.search(ln):
                continue
            if cfg_key == "conditionenemyintimidated" and _ON_FULL_LIFE_RE.search(ln):
                continue
            if passive_re.search(ln):
                guar_line = ln
                break
            low = ln.lower()
            if check_skill_type and not _hit_trigger_matches_skill(low, stats):
                continue
            pm = prob_hit_re.search(ln)
            if pm:
                pct = min(100.0, max(0.0, float(pm.group(1))))
                p = pct / 100.0
                if hit_rate > 0.0:
                    uptime = 1.0 - ((1.0 - p) ** (4.0 * hit_rate))
                    if uptime >= 0.85 and uptime > best_prob:
                        best_prob = uptime
                        best_pct = pct
                continue
            if hit_rate > 0.0 and guar_hit_re.search(ln):
                guar_line = ln
                break
        if guar_line:
            out[cfg_key] = f"Boss-sustainable source ({guar_line[:72]})"
        elif best_prob >= 0.85:
            out[cfg_key] = (
                f"{best_pct:.0f}% on Hit @ {hit_rate:.2f}/s (4s uptime {best_prob * 100.0:.1f}% >= 85%)"
            )


def infer_on_hit_auto_keeps(
    *,
    stats: dict[str, Any] | None,
    skills_res: dict[str, Any] | None,
    mod_lines: list[str],
    extra_mod_lines: list[str] | None = None,
) -> dict[str, str]:
    """Infer Boss-sustainable Unnerve/Intimidate/Maim and non-proxy on-hit charge auto-keeps."""
    player_lines = list(mod_lines)
    hit_rate = _effective_hit_rate(stats)
    out: dict[str, str] = {}

    _scan_debuff_lines(player_lines, hit_rate=hit_rate, stats=stats, check_skill_type=True, out=out)
    if extra_mod_lines:
        # AG / companion gear hits at AG's own slow melee cadence, never at player cast/attack speed.
        _scan_debuff_lines(list(extra_mod_lines), hit_rate=1.8, stats=None, check_skill_type=False, out=out)

    if "conditionenemymaimed" not in out and isinstance(skills_res, dict):
        main_idx = skills_res.get("mainSocketGroup")
        for g in skills_res.get("groups") or []:
            if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
                continue
            is_main = bool(g.get("isMain")) or (main_idx is not None and g.get("index") == main_idx)
            for gm in g.get("gems") or []:
                if not isinstance(gm, dict) or not gm.get("enabled", True):
                    continue
                nm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip().lower()
                if is_main and gm.get("support") and nm in ("maim", "maim support"):
                    out["conditionenemymaimed"] = "Maim Support linked in main skill group"
                    break

    if not is_proxy_main_skill(skills_res):
        for ck, reason in find_on_hit_charge_sources(player_lines, stats).items():
            out.setdefault(ck, reason)
    return out


def _find_config_item(cfg_res: dict[str, Any], var_low: str) -> tuple[str, Any] | None:
    for sec in cfg_res.get("sections", []):
        for item in sec.get("items", []):
            var = str(item.get("var") or "")
            if var.lower() == var_low:
                return var, item.get("value")
    return None


def _remove_stripped_config(
    stripped: dict[str, list[str]],
    records: list[dict[str, Any]],
    var_low: str,
) -> None:
    pref = f"{var_low}="
    stripped["config"] = [s for s in stripped.get("config", []) if not s.lower().startswith(pref)]
    records[:] = [
        r for r in records if not (r.get("dimension") == "config" and str(r.get("key") or "").lower() == var_low)
    ]


def _set_configs_verified(
    worker: BridgeWorker,
    assignments: list[tuple[str, Any]],
    rollback_calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> bool:
    """Apply config assignments and verify read-back via `list_config`; roll back on any failure."""
    calls: list[tuple[str, dict[str, Any]]] = [
        ("set_config", {"var": var, "value": val}) for var, val in assignments
    ]
    calls.append(("list_config", {}))

    def _fail() -> bool:
        if rollback_calls:
            try:
                _call_batch(worker, rollback_calls, raise_on_error=False)
            except Exception:
                pass
        return False

    try:
        res = _call_batch(worker, calls, raise_on_error=False)
    except Exception:
        return _fail()
    if len(res) < len(calls):
        return _fail()
    if any(_rpc_error_text(r) is not None for r in res):
        return _fail()
    cfg_after = res[-1]
    if not isinstance(cfg_after, dict):
        return _fail()
    for var, expected in assignments:
        got = _extract_probe_state({"kind": "config", "var": var}, cfg_after)
        if not _same_state(got, expected):
            return _fail()
    return True


def _maybe_restore_crit_recently(
    worker: BridgeWorker,
    cfg_res: dict[str, Any],
    keep_set: set[str],
    stripped: dict[str, list[str]],
    records: list[dict[str, Any]],
    *,
    is_proxy: bool = False,
) -> None:
    if is_proxy or "conditioncritrecently" in keep_set:
        return
    if not any(
        r.get("dimension") == "config" and str(r.get("key") or "").lower() == "conditioncritrecently"
        for r in records
    ):
        return
    found = _find_config_item(cfg_res, "conditioncritrecently")
    if not found or not found[1]:
        return
    crit_var = found[0]
    try:
        res = _call_batch(worker, [("get_stats", {}), ("get_shock_probe", {})], raise_on_error=False)
    except Exception:
        return
    if not res or not isinstance(res[0], dict) or res[0].get("_error"):
        return
    if len(res) > 1 and isinstance(res[1], dict) and res[1].get("ok") is True and res[1].get("isProxy"):
        return
    st = res[0].get("stats")
    if not isinstance(st, dict):
        return
    crit_ch = max(
        _finite_float(st.get("EffectiveCritChance"), default=0.0),
        _finite_float(st.get("CritChance"), default=0.0),
    )
    hit_rate = _effective_hit_rate(st)
    if crit_ch < 35.0 or hit_rate <= 0.0:
        return
    p_crit = min(1.0, crit_ch / 100.0)
    prob_4s = 1.0 - ((1.0 - p_crit) ** (4.0 * hit_rate))
    if prob_4s < 0.90:
        return
    if not _set_configs_verified(worker, [(crit_var, True)], rollback_calls=[("reset_config", {"var": crit_var})]):
        return
    _remove_stripped_config(stripped, records, "conditioncritrecently")
    stripped.setdefault("autoKept", []).append(
        f"{crit_var}=True (auto-kept: post-dewater CritChance {crit_ch:.1f}% @ {hit_rate:.2f}/s, "
        f"4s crit prob {prob_4s * 100.0:.1f}% >= 90%)"
    )


def _maybe_clamp_boss_shock(
    worker: BridgeWorker,
    cfg_res: dict[str, Any],
    keep_set: set[str],
    stripped: dict[str, list[str]],
    records: list[dict[str, Any]],
) -> None:
    if "conditionenemyshocked" in keep_set or "conditionshockeffect" in keep_set:
        return
    if not any(
        r.get("dimension") == "config" and str(r.get("key") or "").lower() == "conditionenemyshocked"
        for r in records
    ):
        return
    flag_item = _find_config_item(cfg_res, "conditionenemyshocked")
    if not flag_item or not flag_item[1]:
        return
    flag_var = flag_item[0]
    eff_item = _find_config_item(cfg_res, "conditionshockeffect")
    eff_var = eff_item[0] if eff_item else "conditionShockEffect"
    raw_eff = eff_item[1] if eff_item else None
    try:
        orig_eff = round(float(raw_eff)) if raw_eff not in (None, False, "", 0) else 0
    except (TypeError, ValueError):
        orig_eff = 0

    try:
        probe_res = _call_batch(worker, [("get_shock_probe", {})], raise_on_error=False)
    except Exception:
        return
    if not probe_res or not isinstance(probe_res[0], dict) or probe_res[0].get("_error") or probe_res[0].get("ok") is False:
        return
    probe = probe_res[0]
    can_shock = bool(probe.get("canShock"))
    computed = _finite_float(probe.get("computedShockPct"), default=0.0)
    max_cap = max(5, int(_finite_float(probe.get("maxShockCap"), default=50.0)))
    boss_name = str(probe.get("enemyIsBoss") or "Pinnacle")
    if not can_shock or computed < 5.0:
        return

    cap = min(max_cap, math.floor(computed + 1e-6))
    if cap < 5:
        return

    if orig_eff <= 0:
        if not _set_configs_verified(worker, [(flag_var, True)], rollback_calls=[("reset_config", {"var": flag_var})]):
            return
        _remove_stripped_config(stripped, records, "conditionenemyshocked")
        stripped.setdefault("autoKept", []).append(
            f"{flag_var}=True (auto-kept: PoB computed {boss_name} Boss shock {computed:.0f}% >= 5%)"
        )
        return

    shock_rollback = [("reset_config", {"var": flag_var}), ("reset_config", {"var": eff_var})]
    if orig_eff <= cap:
        if not _set_configs_verified(worker, [(flag_var, True), (eff_var, orig_eff)], rollback_calls=shock_rollback):
            return
        _remove_stripped_config(stripped, records, "conditionenemyshocked")
        _remove_stripped_config(stripped, records, "conditionshockeffect")
        reason = f"PoB computed {boss_name} Boss shock {computed:.0f}% (cap {cap}%) >= configured {orig_eff}%"
        stripped.setdefault("autoKept", []).append(f"{flag_var}=True (auto-kept: {reason})")
        stripped.setdefault("autoKept", []).append(f"{eff_var}={orig_eff} (auto-kept: {reason})")
        return

    if not _set_configs_verified(worker, [(flag_var, True), (eff_var, cap)], rollback_calls=shock_rollback):
        return
    _remove_stripped_config(stripped, records, "conditionenemyshocked")
    _remove_stripped_config(stripped, records, "conditionshockeffect")
    stripped.setdefault("autoKept", []).append(
        f"{flag_var}=True (auto-kept: PoB computed {boss_name} Boss shock {computed:.0f}%, clamped {orig_eff}% -> {cap}%)"
    )
    stripped["config"].append(f"{eff_var}={orig_eff!r} -> {cap} (clamped to PoB {boss_name} Boss shock {cap}%)")
    records.append(
        {
            "dimension": "config",
            "key": eff_var,
            "label": f"{eff_var}={orig_eff!r} -> {cap} (Boss shock clamp)",
            "restore": [("set_config", {"var": eff_var, "value": orig_eff})],
            "revert": [("set_config", {"var": eff_var, "value": cap})],
            "probe": {"kind": "config", "var": eff_var},
            "original": orig_eff,
        }
    )


def refine_post_dewater_mechanics(
    worker: BridgeWorker,
    cfg_res: dict[str, Any],
    keep_set: set[str],
    stripped: dict[str, list[str]],
    records: list[dict[str, Any]],
    *,
    skills_res: dict[str, Any] | None = None,
) -> None:
    """Apply live post-customMods/post-flask auto-keep (CritRecently) and Boss shock clamping."""
    _maybe_restore_crit_recently(
        worker,
        cfg_res,
        keep_set,
        stripped,
        records,
        is_proxy=is_proxy_main_skill(skills_res),
    )
    _maybe_clamp_boss_shock(worker, cfg_res, keep_set, stripped, records)
