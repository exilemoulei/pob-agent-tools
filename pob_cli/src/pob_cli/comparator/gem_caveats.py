"""Support gem conflict filtering, trigger CDR caveats, and dynamic Sadism math."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from .. import game_data as _game_data
from ..formatters import _parse_numeric_or_none, strip_pob_escapes

_EXCEPTIONAL_AWAKENED_KEEP: frozenset[str] = frozenset(
    {
        "awakened empower",
        "awakened enhance",
        "awakened enlighten",
    }
)


def _is_legacy_awakened_gem_safe(name: str) -> bool:
    fn = getattr(_game_data, "is_legacy_awakened_gem", None)
    if callable(fn):
        try:
            return bool(fn(name))
        except Exception:
            pass
    low = strip_pob_escapes(str(name or "")).strip().lower()
    if low.endswith(" support"):
        low = low[: -len(" support")].strip()
    return low.startswith("awakened ") and low not in _EXCEPTIONAL_AWAKENED_KEEP


_ARCHETYPE_SWAP_SUPPORT_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "trigger",
        (
            "cast when stunned",
            "cast on critical strike",
            "cast when damage taken",
            "cast on death",
            "cast while channelling",
            "cast on melee kill",
            "cast on ward break",
            "spellslinger",
            "arcanist brand",
            "manaforged arrows",
            "mark on hit",
            "hextouch",
            "battlemage's cry",
            "automation",
            "autoexertion",
        ),
    ),
    ("totem", ("spell totem", "ballista totem", "earthbreaker", "multiple totems")),
    (
        "trap",
        ("trap support", "cluster traps", "multiple traps", "charged traps", "advanced traps", "trap and mine damage"),
    ),
    (
        "mine",
        ("high-impact mine", "blastchain mine", "minefield", "swift assembly", "charged mines", "locus mine"),
    ),
)

_COOLDOWN_GATED_TRIGGER_TOKENS: tuple[str, ...] = (
    "cast when stunned",
    "cast on critical strike",
    "cast when damage taken",
    "cast on melee kill",
    "cast on ward break",
    "spellslinger",
    "arcanist brand",
    "manaforged arrows",
    "mark on hit",
    "hextouch",
    "battlemage's cry",
    "automation",
    "autoexertion",
    "晕眩时施放",
    "暈眩時施放",
    "暴击时施放",
    "暴擊時施放",
    "受伤时施放",
    "受傷時施放",
    "法术回荡",
    "奥术烙印",
)

_CHILL_SKILL_TOKENS: tuple[str, ...] = (
    "skitterbots",
    "summon skitterbots",
    "召唤飞掠者",
    "召喚飛掠者",
    "arctic armour",
    "极地装甲",
    "極地裝甲",
    "vortex",
    "漩涡",
    "漩渦",
    "creeping frost",
    "寒霜渗透",
    "寒霜滲透",
    "cold snap",
    "霜暴",
    "frostblink",
    "冰霜闪现",
    "冰霜閃現",
    "frost shield",
    "冰霜护盾",
    "冰霜護盾",
    "hydrosphere",
    "水球",
    "bonechill",
    "彻骨",
    "徹骨",
)

_SADISM_CACHE: dict[str, tuple[int, int]] = {}


def _gem_archetype_bucket(name_en: str) -> str | None:
    low = strip_pob_escapes(name_en or "").strip().lower()
    if low.startswith("awakened "):
        low = low[len("awakened ") :].strip()
    if low == "trap":
        return "trap"
    for bucket, patterns in _ARCHETYPE_SWAP_SUPPORT_BUCKETS:
        for pat in patterns:
            if low == pat or low.startswith(pat + " ") or low == pat + " support":
                return bucket
    return None


def compute_sadism_stats(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> tuple[int, int]:
    """Return `(less_duration_pct, per_ailment_total_pct_delta)` for Lv20 Sadism from game_data (fallback -41%)."""
    cache_key = f"{pob_dir or ''}|{runtime_dir or ''}"
    cached = _SADISM_CACHE.get(cache_key)
    if cached is not None:
        return cached

    faster = 98.0
    dur_mod = -70.0
    loaded_from_db = False
    try:
        db_path = _game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT data_json FROM gems WHERE name_lower IN ('sadism', 'sadism support') "
                "OR display_name_lower IN ('sadism', 'sadism support') LIMIT 1"
            ).fetchone()
            if row:
                obj = json.loads(row[0])
                lvls = obj.get("levels") or {}
                lv20 = lvls.get("20") or lvls.get("1") or (next(iter(lvls.values())) if lvls else {})
                st = (lv20.get("stats") or {}) if isinstance(lv20, dict) else {}
                f_raw = st.get("damaging_ailments_deal_damage_+%_faster")
                d_raw = st.get("support_faster_ailments_ailment_duration_+%_final")
                if f_raw is not None and d_raw is not None:
                    faster = float(f_raw)
                    dur_mod = float(d_raw)
                    loaded_from_db = True
        finally:
            conn.close()
    except Exception:
        pass

    total_mult = (1.0 + faster / 100.0) * (1.0 + dur_mod / 100.0)
    pct_delta = round((total_mult - 1.0) * 100.0)
    less_dur = abs(round(dur_mod))
    result = (less_dur, pct_delta)
    if loaded_from_db:
        _SADISM_CACHE[cache_key] = result
    return result


def compute_sadism_per_ailment_pct(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> int:
    """Return total damage per ailment % delta for Lv20 Sadism (`1.98 * 0.30 - 1 -> -41`)."""
    return compute_sadism_stats(pob_dir=pob_dir, runtime_dir=runtime_dir)[1]


_SUPPORT_GEM_CAVEATS: tuple[tuple[str, str], ...] = (
    ("immolate", "Requires Enemy Burning/Ignited"),
    ("hypothermia", "Requires Enemy Chilled"),
    ("trinity", "Requires 2+ alternating highest hit elements"),
    ("elemental focus", "Cannot inflict Elemental Ailments (breaks Shock/Chill/Freeze/Ignite)"),
    ("controlled destruction", "80% less Crit Chance"),
    ("concentrated effect", "30% less Area of Effect (check overlap/clear)"),
    ("brutality", "Deals NO Elemental or Chaos Damage"),
    ("void manipulation", "Deals NO Elemental Damage"),
    ("sacrifice", "Sacrifices 20% Current Life per cast (bricks CI / deadly on Low-Life)"),
    ("awakened empower", "Exceptional (Max Lv4 / corrupt Lv5; mirror-tier endgame budget)"),
    ("awakened enhance", "Exceptional (Max Lv4 / corrupt Lv5; endgame budget)"),
    ("awakened enlighten", "Exceptional (Max Lv4 / corrupt Lv5; endgame budget)"),
)

_TRIGGER_CDR_CAVEAT = "Actual DPS gated by trigger hit/stun frequency (PoB assumes cap)"


def _normalize_support_name(name_en: str) -> tuple[str, str]:
    raw_low = strip_pob_escapes(name_en or "").strip().lower()
    if raw_low.endswith(" support"):
        raw_low = raw_low[: -len(" support")].strip()
    base_low = raw_low[len("awakened ") :].strip() if raw_low.startswith("awakened ") else raw_low
    return raw_low, base_low


def _group_has_trigger_gem(gems: list[dict[str, Any]]) -> bool:
    for gm in gems:
        if not isinstance(gm, dict) or not gm.get("enabled", True):
            continue
        gsid = str(gm.get("skillId") or "").lower()
        if any(tok in gsid for tok in ("castwhenstunned", "castoncrit", "castwhendamagetaken", "spellslinger", "wardbreak")):
            return True
        combined = " ".join(
            strip_pob_escapes(str(gm.get(k) or ""))
            for k in ("name", "nameSpec", "nameZh")
        ).lower()
        if any(tok in combined for tok in _COOLDOWN_GATED_TRIGGER_TOKENS):
            return True
    return False


def _gem_caveat(
    name_en: str,
    *,
    main_mana_cost: float | None = None,
    group_has_trigger: bool = False,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> str | None:
    raw_low, low = _normalize_support_name(name_en)
    if group_has_trigger and (
        low in ("cooldown recovery", "greater cooldown recovery")
        or low.startswith("cooldown recovery ")
    ):
        return _TRIGGER_CDR_CAVEAT
    if low == "sadism" or low.startswith("sadism "):
        less_dur, pct_delta = compute_sadism_stats(pob_dir=pob_dir, runtime_dir=runtime_dir)
        return (
            f"{less_dur}% LESS Ailment Duration! Inflates PoB DPS rate while slashing "
            f"total per-ignite damage ({pct_delta:+d}%) and breaking uptime"
        )
    for pat, note in _SUPPORT_GEM_CAVEATS:
        if raw_low == pat or raw_low.startswith(pat + " "):
            return note
    if low == "inspiration" and main_mana_cost is not None:
        if main_mana_cost <= 0.0:
            return "0 charges on 0-mana skill!"
        if main_mana_cost >= 400.0:
            return (
                f"High ManaCost ({round(main_mana_cost)}) vs Lv20 800-mana reset threshold "
                "— resets every 1-2 casts (real DPS far below PoB's assumed 5 charges!)"
            )
    if low != raw_low:
        for pat, note in _SUPPORT_GEM_CAVEATS:
            if low == pat or low.startswith(pat + " "):
                return note
    return None


def _max_stat(stats: dict[str, Any], *keys: str) -> float:
    best = 0.0
    for k in keys:
        raw = stats.get(k)
        if raw is None:
            continue
        parsed = _parse_numeric_or_none(raw)
        if parsed is not None and math.isfinite(parsed) and parsed > best:
            best = float(parsed)
    return best


def _has_chill_source(
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    cfg_res: dict[str, Any] | None,
) -> bool:
    if _max_stat(stats, "ColdDamage", "ColdHitAverage", "ColdMax") > 0.0:
        return True
    if isinstance(cfg_res, dict):
        for sec in cfg_res.get("sections") or []:
            for it in (sec.get("items") if isinstance(sec, dict) else None) or []:
                if not isinstance(it, dict):
                    continue
                var = str(it.get("var") or "").lower()
                val = it.get("value")
                if var == "conditionenemychilled" and bool(val):
                    return True
                if var == "enemychilleffect" and (_parse_numeric_or_none(val) or 0.0) > 0.0:
                    return True
        for blk in cfg_res.get("customMods") or []:
            if isinstance(blk, dict) and blk.get("enabled", True) is not False:
                txt = str(blk.get("text") or "").lower()
                if "chill" in txt or "冰缓" in txt or "冰緩" in txt:
                    return True
    for g in (skills_res or {}).get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if not isinstance(gm, dict) or not gm.get("enabled", True):
                continue
            combined = " ".join(
                strip_pob_escapes(str(gm.get(k) or ""))
                for k in ("name", "nameSpec", "nameZh")
            ).lower()
            if any(tok in combined for tok in _CHILL_SKILL_TOKENS):
                return True
    return False


_TYPED_DAMAGE_STAT_KEYS: tuple[str, ...] = (
    "PhysicalDamage",
    "PhysicalHitAverage",
    "FireDamage",
    "FireHitAverage",
    "ColdDamage",
    "ColdHitAverage",
    "LightningDamage",
    "LightningHitAverage",
    "ChaosDamage",
    "ChaosHitAverage",
    "IgniteDPS",
    "BleedDPS",
    "PoisonDPS",
    "HitDPS",
)


def _check_support_hard_conflict(
    name_en: str,
    stats: dict[str, Any] | None,
    skills_res: dict[str, Any] | None = None,
    cfg_res: dict[str, Any] | None = None,
) -> str | None:
    """Return exclusion reason if candidate support gem bricks or hard-conflicts with main skill damage."""
    if not isinstance(stats, dict) or not stats:
        return None
    _, low = _normalize_support_name(name_en)
    if low not in ("elemental focus", "brutality", "void manipulation", "hypothermia"):
        return None

    ignite_dps = _max_stat(stats, "IgniteDPS")
    bleed_dps = _max_stat(stats, "BleedDPS")
    poison_dps = _max_stat(stats, "PoisonDPS")
    hit_dps = _max_stat(stats, "HitDPS") if "HitDPS" in stats else _max_stat(stats, "TotalDPS")

    if low == "elemental focus":
        if ignite_dps > 0.0 and ignite_dps > hit_dps:
            return "Cannot inflict Elemental Ailments (bricks Ignite)"
        return None

    phys = _max_stat(stats, "PhysicalDamage", "PhysicalHitAverage")
    fire = _max_stat(stats, "FireDamage", "FireHitAverage")
    cold = _max_stat(stats, "ColdDamage", "ColdHitAverage")
    light = _max_stat(stats, "LightningDamage", "LightningHitAverage")
    chaos = _max_stat(stats, "ChaosDamage", "ChaosHitAverage")
    if phys + fire + cold + light + chaos <= 0.0:
        phys = bleed_dps
        fire = ignite_dps
        chaos = poison_dps
    ele = fire + cold + light
    non_phys = ele + chaos
    total_typed = phys + non_phys

    if low == "brutality" and total_typed > 0.0 and (non_phys / total_typed) > 0.50:
        return "Deals NO Elemental or Chaos Damage (>50% non-Physical damage)"
    if low == "void manipulation" and total_typed > 0.0 and (ele / total_typed) > 0.50:
        return "Deals NO Elemental Damage (>50% Elemental damage)"
    if low == "hypothermia":
        has_typed_telemetry = any(k in stats for k in _TYPED_DAMAGE_STAT_KEYS)
        if has_typed_telemetry and cold <= 0.0 and not _has_chill_source(stats, skills_res, cfg_res):
            return "No Cold/Chill source detected"
    return None
