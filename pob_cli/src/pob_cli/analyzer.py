"""Build inspection, offence/defence summary, 4-part inflation audit, and calcs breakdown."""

from __future__ import annotations

import re
import difflib
from collections.abc import Iterable
from typing import Any

from .bridge_client import BridgeWorker
from .formatters import strip_pob_escapes
from .snapshot import capture_digest, diff_digest

# Config variables that represent permanent character choices and must NEVER be stripped in de-watering
PERMANENT_CONFIG_VARS = frozenset({
    "bandit",
    "pantheonMajorGod",
    "pantheonMinorGod",
    "resistancePenalty",
    "characterLevel",
})

# Config variables that represent temporary buffs, conditional states, or boss/target overrides
INFLATION_CONFIG_PREFIXES = (
    "use",
    "buff",
    "condition",
    "multiplier",
    "override",
)

INFLATION_CONFIG_EXACT = frozenset({
    "onslaught",
    "adrenaline",
    "fortify",
    "tailwind",
    "elusive",
    "unholyMight",
    "divineBlessing",
    "innervation",
    "infusedChannelling",
})

TEMPORARY_SKILL_PATTERNS = (
    "molten shell",
    "steelskin",
    "immortal call",
    "arcane cloak",
    "berserk",
    "frost shield",
    "sigil of power",
    "focus",
    "vaal breach",
    "vaal impurity",
    "vaal righteous fire",
    "熔岩护盾",
    "钢铁之肤",
    "不朽怒嚎",
    "奥术斗篷",
    "狂战",
    "冰霜护盾",
    "威能法印",
    "瓦尔裂隙",
    "瓦尔不净",
    "瓦尔正义之火",
)


def extract_summary(worker: BridgeWorker) -> dict[str, Any]:
    """Extract a complete structured summary of the currently loaded build."""
    with worker.lock:
        info = worker.call("get_build_info", {})
        header = worker.call("get_build_header", {})
        stats_res = worker.call("get_stats", {})
        sidebar = worker.call("get_sidebar", {})
        skills_res = worker.call("list_skills", {})
        specs_res = worker.call("list_specs", {})
        items_res = worker.call("list_items", {})
        config_res = worker.call("list_config", {})

    stats = stats_res.get("stats", {})
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar.get("warnings", [])
        if (w.get("text") or w.get("raw"))
    ]

    # Identify main skill group & gems
    main_idx = skills_res.get("mainSocketGroup") or header.get("mainSocketGroup", {}).get("index") or 1
    main_group: dict[str, Any] | None = None
    for g in skills_res.get("groups", []):
        if g.get("index") == main_idx or g.get("isMain"):
            main_group = g
            break

    # Detect minion sidebar rows if any
    minion_sidebar: list[dict[str, str]] = []
    for row in sidebar.get("rows", []):
        if row.get("actor") == "minion" and (row.get("lhs") or row.get("rhs")):
            minion_sidebar.append(
                {
                    "label": strip_pob_escapes(row.get("lhs", "")),
                    "value": strip_pob_escapes(row.get("rhs", "")),
                    "stat": row.get("stat") or "",
                }
            )

    has_minion_stats = bool(minion_sidebar) or float(stats.get("MinionCombinedDPS") or 0) > 0

    offence = {
        "FullDPS": stats.get("FullDPS"),
        "CombinedDPS": stats.get("CombinedDPS"),
        "TotalDPS": stats.get("TotalDPS"),
        "TotalDot": stats.get("TotalDot"),
        "TotalDotDPS": stats.get("TotalDotDPS"),
        "WithImpaleDPS": stats.get("WithImpaleDPS"),
        "BleedDPS": stats.get("BleedDPS"),
        "IgniteDPS": stats.get("IgniteDPS"),
        "PoisonDPS": stats.get("PoisonDPS"),
        "ImpaleDPS": stats.get("ImpaleDPS"),
        "AverageDamage": stats.get("AverageDamage"),
        "AverageHit": stats.get("AverageHit"),
        "Speed": stats.get("Speed"),
        "CritChance": stats.get("CritChance"),
        "EffectiveCritChance": stats.get("EffectiveCritChance"),
        "CritMultiplier": stats.get("CritMultiplier"),
        "HitChance": stats.get("HitChance"),
        "ManaCost": stats.get("ManaCost"),
        "MainHandAccuracy": stats.get("MainHandAccuracy"),
    }

    minion_offence = None
    if has_minion_stats:
        minion_offence = {
            "MinionCombinedDPS": stats.get("MinionCombinedDPS"),
            "MinionTotalDPS": stats.get("MinionTotalDPS"),
            "MinionTotalDot": stats.get("MinionTotalDot"),
            "MinionAverageDamage": stats.get("MinionAverageDamage"),
            "MinionSpeed": stats.get("MinionSpeed"),
            "MinionCritChance": stats.get("MinionCritChance"),
            "MinionCritMultiplier": stats.get("MinionCritMultiplier"),
            "MinionHitChance": stats.get("MinionHitChance"),
            "MinionLife": stats.get("MinionLife"),
            "sidebarRows": minion_sidebar,
        }

    defence = {
        "TotalEHP": stats.get("TotalEHP"),
        "Life": stats.get("Life"),
        "LifeUnreserved": stats.get("LifeUnreserved"),
        "LifeUnreservedPercent": stats.get("LifeUnreservedPercent"),
        "Mana": stats.get("Mana"),
        "ManaUnreserved": stats.get("ManaUnreserved"),
        "ManaUnreservedPercent": stats.get("ManaUnreservedPercent"),
        "EnergyShield": stats.get("EnergyShield"),
        "Ward": stats.get("Ward"),
        "Armour": stats.get("Armour"),
        "PhysicalDamageReduction": stats.get("PhysicalDamageReduction"),
        "Evasion": stats.get("Evasion"),
        "MeleeEvadeChance": stats.get("MeleeEvadeChance"),
        "ProjectileEvadeChance": stats.get("ProjectileEvadeChance"),
        "PhysicalMaximumHitTaken": stats.get("PhysicalMaximumHitTaken"),
        "FireMaximumHitTaken": stats.get("FireMaximumHitTaken"),
        "ColdMaximumHitTaken": stats.get("ColdMaximumHitTaken"),
        "LightningMaximumHitTaken": stats.get("LightningMaximumHitTaken"),
        "ChaosMaximumHitTaken": stats.get("ChaosMaximumHitTaken"),
        "FireResist": stats.get("FireResist"),
        "FireResistOverCap": stats.get("FireResistOverCap"),
        "ColdResist": stats.get("ColdResist"),
        "ColdResistOverCap": stats.get("ColdResistOverCap"),
        "LightningResist": stats.get("LightningResist"),
        "LightningResistOverCap": stats.get("LightningResistOverCap"),
        "ChaosResist": stats.get("ChaosResist"),
        "ChaosResistOverCap": stats.get("ChaosResistOverCap"),
        "EffectiveBlockChance": stats.get("EffectiveBlockChance"),
        "EffectiveSpellBlockChance": stats.get("EffectiveSpellBlockChance"),
        "EffectiveSpellSuppressionChance": stats.get("EffectiveSpellSuppressionChance"),
        "NetLifeRegen": stats.get("LifeRegenRecovery") if stats.get("LifeRegenRecovery") is not None else stats.get("LifeRegen"),
        "NetManaRegen": stats.get("ManaRegenRecovery") if stats.get("ManaRegenRecovery") is not None else stats.get("ManaRegen"),
        "EnergyShieldRegen": stats.get("EnergyShieldRegenRecovery") if stats.get("EnergyShieldRegenRecovery") is not None else stats.get("EnergyShieldRegen"),
        "Str": stats.get("Str"),
        "ReqStr": stats.get("ReqStr"),
        "Dex": stats.get("Dex"),
        "ReqDex": stats.get("ReqDex"),
        "Int": stats.get("Int"),
        "ReqInt": stats.get("ReqInt"),
    }

    return {
        "buildInfo": info,
        "buildHeader": header,
        "activeSpec": specs_res.get("activeSpec"),
        "specs": specs_res.get("specs", []),
        "activeItemSetId": items_res.get("activeItemSetId"),
        "itemSets": items_res.get("itemSets", []),
        "activeSkillSetId": skills_res.get("activeSkillSetId"),
        "skillSets": skills_res.get("skillSets", []),
        "activeConfigSetId": config_res.get("activeConfigSetId"),
        "configSets": config_res.get("configSets", []),
        "mainGroup": main_group,
        "offence": offence,
        "minionOffence": minion_offence,
        "defence": defence,
        "warnings": warnings,
    }


def _has_mageblood_equipped(items_res: dict[str, Any]) -> bool:
    """Return True if a Belt slot has Mageblood equipped."""
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("name") == "Belt" and slot.get("selItemId"):
            item = items_by_id.get(slot["selItemId"], {})
            text = " ".join(
                str(item.get(k) or "")
                for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
            ).lower()
            if "mageblood" in text or "法师之血" in text:
                return True
    return False


def _normalize_keep(keep: Iterable[str] | None) -> set[str]:
    """Normalize user keep tokens (config vars, 'Flask N', 'flasks', gem names, 'customMods', 'party')."""
    out: set[str] = set()
    for tok in keep or ():
        for part in str(tok).split(","):
            p = part.strip().lower()
            if p:
                out.add(p)
    return out


def dewater_build_in_place(
    worker: BridgeWorker,
    keep: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    """Strip PoB inflation ('water') in place and set enemyIsBoss='Pinnacle'.

    Must be called inside a snapshot wrapper if non-destructive behavior is desired.
    Returns a dictionary of stripped inflation items across the 4 audit dimensions,
    plus ``kept`` (items left untouched because the user listed them in ``keep``).
    """
    stripped, _records = dewater_build_with_records(worker, keep=keep)
    return stripped


_KEEP_KEYWORDS = ("flasks", "custommods", "party")


def _unmatched_keep(keep_set: set[str], known: dict[str, str]) -> list[str]:
    """Keep tokens that name nothing in this build, with a close-match hint.

    pob-mcp rejects an unknown config key per key instead of silently ignoring it;
    a mistyped ``--keep`` would otherwise strip the item it meant to protect.
    """
    out: list[str] = []
    for tok in sorted(keep_set):
        if tok in known:
            continue
        hint = difflib.get_close_matches(tok, list(known), n=1, cutoff=0.8)
        msg = f"{tok}: not a config var, flask slot, skill gem, or flasks/customMods/party"
        if hint:
            msg += f" (did you mean {known[hint[0]]}?)"
        out.append(msg)
    return out


def dewater_build_with_records(
    worker: BridgeWorker,
    keep: Iterable[str] | None = None,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """De-water in place and also return reversible per-item records.

    Each record carries the exact bridge calls that ``restore`` the original state of
    that single item and ``revert`` it back to the de-watered state, so callers can
    measure every stripped item's real PoB contribution one at a time
    (see :func:`attribute_water`). ``keep`` tokens are matched case-insensitively
    against config var names, flask slot names (``Flask 1``) or ``flasks`` (all),
    skill gem names, ``customMods`` and ``party``.
    """
    keep_set = _normalize_keep(keep)
    stripped: dict[str, list[str]] = {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
        "kept": [],
        "keepRejected": [],
    }
    records: list[dict[str, Any]] = []
    # Every name a keep token may legitimately refer to (lowercase -> display form).
    known: dict[str, str] = {k: k for k in _KEEP_KEYWORDS}

    # 1. Config inflation & enemyIsBoss = Pinnacle
    cfg_res = worker.call("list_config", {})
    current_boss = None
    had_preset_boss_skills = False

    for sec in cfg_res.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            val = item.get("value")
            if var:
                known[str(var).lower()] = str(var)
            if not var or var in PERMANENT_CONFIG_VARS:
                continue
            if var == "enemyIsBoss":
                current_boss = val
                continue
            if val in (None, False, "", 0):
                continue

            is_inflation = (
                var == "presetBossSkills"
                or var.startswith(INFLATION_CONFIG_PREFIXES)
                or var in INFLATION_CONFIG_EXACT
            )
            if not is_inflation:
                continue
            if var.lower() in keep_set:
                stripped["kept"].append(f"{var}={val!r} (kept)")
                continue
            if var == "presetBossSkills":
                had_preset_boss_skills = True
            stripped["config"].append(f"{var}={val!r} -> reset")
            worker.call("reset_config", {"var": var})
            records.append(
                {
                    "dimension": "config",
                    "key": var,
                    "label": f"{var}={val!r}",
                    "restore": [("set_config", {"var": var, "value": val})],
                    "revert": [("reset_config", {"var": var})],
                    "probe": {"kind": "config", "var": var},
                    "original": val,
                }
            )

    if current_boss not in ("Pinnacle", "Uber"):
        stripped["config"].append(f"enemyIsBoss={current_boss!r} -> 'Pinnacle'")
    worker.call("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})
    if had_preset_boss_skills:
        # Second call ensures ConfigTab BuildModList sees updated enemyIsBoss selIndex
        worker.call("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})

    # Check customMods (use '# disabled' comment block so ConfigTab.lua doesn't fall back to legacy input.customMods)
    custom_mods = cfg_res.get("customMods", [])
    active_custom_lines: list[str] = []
    for block in custom_mods:
        if block.get("enabled", True):
            text = (block.get("text") or "").strip()
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    active_custom_lines.append(s)
    if active_custom_lines:
        label = f"customMods ({len(active_custom_lines)} active lines: {'; '.join(active_custom_lines[:3])})"
        if "custommods" in keep_set:
            stripped["kept"].append(f"{label} (kept)")
        else:
            disabled = [{"title": "Default", "text": "# disabled", "enabled": True}]
            original = [
                {"title": b.get("title") or "Default", "text": b.get("text") or "", "enabled": b.get("enabled", True)}
                for b in custom_mods
            ]
            stripped["config"].append(f"{label} -> disabled")
            worker.call("set_custom_mods", {"list": disabled})
            records.append(
                {
                    "dimension": "config",
                    "key": "customMods",
                    "label": label,
                    "restore": [("set_custom_mods", {"list": original})],
                    "revert": [("set_custom_mods", {"list": disabled})],
                    "probe": {"kind": "customMods"},
                    "original": original,
                }
            )

    # 2. Flask inflation (active flasks without Mageblood)
    items_res = worker.call("list_items", {})
    has_mb = _has_mageblood_equipped(items_res)
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    if not has_mb:
        for slot in items_res.get("slots", []):
            if slot.get("isFlask") and slot.get("name"):
                known[str(slot["name"]).lower()] = str(slot["name"])
            if slot.get("isFlask") and slot.get("active"):
                slot_name = slot["name"]
                item = items_by_id.get(slot.get("selItemId") or 0, {})
                item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
                if "flasks" in keep_set or slot_name.lower() in keep_set:
                    stripped["kept"].append(f"{slot_name} ({item_title}) (kept)")
                    continue
                stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False")
                worker.call("set_slot_active", {"slotName": slot_name, "active": False})
                records.append(
                    {
                        "dimension": "flasks",
                        "key": slot_name,
                        "label": f"{slot_name} ({item_title})",
                        "restore": [("set_slot_active", {"slotName": slot_name, "active": True})],
                        "revert": [("set_slot_active", {"slotName": slot_name, "active": False})],
                        "probe": {"kind": "flask", "slot": slot_name},
                        "original": True,
                    }
                )

    # 3. Temporary guard / burst / Vaal skills
    skills_res = worker.call("list_skills", {})
    for group in skills_res.get("groups", []):
        g_idx = group["index"]
        group_enabled = group.get("enabled", True)
        for gem in group.get("gems", []):
            names = [
                str(gem.get("nameSpec") or ""),
                str(gem.get("name") or ""),
                str(gem.get("nameZh") or ""),
                str(gem.get("skillId") or ""),
            ]
            for n in names:
                if n.strip():
                    known[n.strip().lower()] = n.strip()
            if not group_enabled or not gem.get("enabled", True) or gem.get("support"):
                continue
            combined_lower = " ".join(names).lower()
            is_vaal = (
                combined_lower.startswith("vaal ")
                or " vaal " in combined_lower
                or "瓦尔" in combined_lower
            )
            is_temp = any(pat in combined_lower for pat in TEMPORARY_SKILL_PATTERNS)
            disp = strip_pob_escapes(gem.get("nameZh") or gem.get("name") or gem.get("nameSpec") or "")
            if not (is_temp or (is_vaal and gem.get("enableGlobal2", False))):
                continue
            if any(n.strip().lower() in keep_set for n in names if n.strip()):
                stripped["kept"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) (kept)")
                continue
            gem_ref = {"group": g_idx, "index": gem["index"]}
            if is_temp:
                stripped["skills"].append(f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enabled=False")
                worker.call("set_gem", {**gem_ref, "enabled": False})
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp}",
                        "restore": [("set_gem", {**gem_ref, "enabled": True})],
                        "revert": [("set_gem", {**gem_ref, "enabled": False})],
                        "probe": {"kind": "gem", **gem_ref, "fields": ["enabled"]},
                        "original": {"enabled": True},
                    }
                )
            else:
                original_base_enabled = gem.get("enableGlobal1", True)
                stripped["skills"].append(
                    f"Group #{g_idx} Gem #{gem['index']} ({disp}) -> enableGlobal2=False (base skill kept)"
                )
                worker.call("set_gem", {**gem_ref, "enableGlobal1": True, "enableGlobal2": False})
                records.append(
                    {
                        "dimension": "skills",
                        "key": disp,
                        "label": f"Group #{g_idx} {disp} (Vaal)",
                        "restore": [("set_gem", {**gem_ref, "enableGlobal1": original_base_enabled, "enableGlobal2": True})],
                        "revert": [("set_gem", {**gem_ref, "enableGlobal1": True, "enableGlobal2": False})],
                        "probe": {"kind": "gem", **gem_ref, "fields": ["enableGlobal1", "enableGlobal2"]},
                        "original": {"enableGlobal1": original_base_enabled, "enableGlobal2": True},
                    }
                )

    # 4. PartyTab buffs (cleared wholesale; not individually reversible, so no attribution record)
    party_res = worker.call("get_party", {})
    party_fields = party_res.get("fields", {})
    non_empty_party = [k for k, v in party_fields.items() if str(v or "").strip()]
    if non_empty_party or party_res.get("enableExportBuffs"):
        label = f"PartyTab fields ({', '.join(non_empty_party or ['enableExportBuffs'])})"
        if "party" in keep_set:
            stripped["kept"].append(f"{label} (kept)")
        else:
            stripped["party"].append(f"{label} -> cleared & disabled")
            worker.call("party_action", {"action": "disable"})
            worker.call("party_action", {"action": "clear"})

    stripped["keepRejected"] = _unmatched_keep(keep_set, known)
    return stripped, records


def _probe_state(worker: BridgeWorker, probe: dict[str, Any]) -> Any:
    """Read back from PoB the live state a record's restore/revert is meant to set."""
    kind = probe["kind"]
    if kind in ("config", "customMods"):
        cfg = worker.call("list_config", {})
        if kind == "customMods":
            return [
                {"title": b.get("title") or "Default", "text": b.get("text") or "", "enabled": b.get("enabled", True)}
                for b in cfg.get("customMods", [])
            ]
        for sec in cfg.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == probe["var"]:
                    return item.get("value")
        return None
    if kind == "flask":
        for slot in worker.call("list_items", {}).get("slots", []):
            if slot.get("name") == probe["slot"]:
                return bool(slot.get("active"))
        return None
    if kind == "gem":
        for group in worker.call("list_skills", {}).get("groups", []):
            if group.get("index") != probe["group"]:
                continue
            for gem in group.get("gems", []):
                if gem.get("index") == probe["index"]:
                    return {f: bool(gem.get(f, f == "enabled")) for f in probe["fields"]}
        return None
    raise ValueError(f"unknown probe kind {kind!r}")


def _same_state(a: Any, b: Any) -> bool:
    """Compare a read-back value with the expected one (PoB stores unset checks as nil)."""
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def _digest_metrics(digest: dict[str, Any], dps_key: str) -> dict[str, float | None]:
    stats = digest.get("stats", {})

    def f(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    ele = [f(stats.get(k)) for k in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken")]
    ele_vals = [v for v in ele if v is not None]
    return {
        "dps": f(stats.get(dps_key)),
        "totalEhp": f(stats.get("TotalEHP")),
        "physMaxHit": f(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": min(ele_vals) if ele_vals else None,
    }


def attribute_water(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
    dewatered_digest: dict[str, Any],
    dps_key: str,
) -> list[dict[str, Any]]:
    """Measure each stripped item's real PoB contribution, one at a time.

    Starting from the de-watered state, every record is restored alone, the build is
    recalculated by PoB, and the record is reverted again. Deltas are relative to the
    de-watered baseline, so they answer "how much does keeping only this item add?".
    No value is estimated: every number is read from the engine.
    """
    base = _digest_metrics(dewatered_digest, dps_key)
    rows: list[dict[str, Any]] = []
    broken_after: str | None = None
    for rec in records:
        row: dict[str, Any] = {
            "dimension": rec["dimension"],
            "key": rec["key"],
            "label": rec["label"],
        }
        if broken_after is not None:
            # The de-watered baseline is no longer trustworthy: never measure on it.
            row["error"] = f"skipped: baseline not restored after {broken_after}"
            rows.append(row)
            continue
        probe = rec.get("probe")
        try:
            before = _probe_state(worker, probe) if probe else None
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"cannot read state before restore: {exc}"
            rows.append(row)
            continue
        try:
            for method, params in rec["restore"]:
                worker.call(method, params)
            # Like pob-mcp's set_config applied/rejected: read the state back before
            # trusting the delta, so a silent no-op is not reported as "no water".
            if probe:
                got = _probe_state(worker, probe)
                if not _same_state(got, rec.get("original")):
                    raise RuntimeError(
                        f"restore not applied: expected {rec.get('original')!r}, PoB reads {got!r}"
                    )
            m = _digest_metrics(capture_digest(worker), dps_key)
            for k in ("dps", "totalEhp", "physMaxHit", "eleMaxHit"):
                row[k] = m[k]
                b = base[k]
                row[f"{k}Delta"] = (m[k] - b) if (m[k] is not None and b is not None) else None
                row[f"{k}Pct"] = ((m[k] - b) / b * 100.0) if (m[k] is not None and b) else None
        except Exception as exc:  # noqa: BLE001 - report per-item failures instead of aborting the audit
            row["error"] = str(exc)
        finally:
            try:
                for method, params in rec["revert"]:
                    worker.call(method, params)
                if probe:
                    back = _probe_state(worker, probe)
                    if not _same_state(back, before):
                        raise RuntimeError(f"revert not applied: expected {before!r}, PoB reads {back!r}")
            except Exception as exc:  # noqa: BLE001
                broken_after = rec["key"]
                prev = row.get("error")
                row["error"] = f"{prev}; {exc}" if prev else str(exc)
        rows.append(row)
    return rows


def audit_build(
    worker: BridgeWorker,
    keep: Iterable[str] | None = None,
    attribution: bool = True,
) -> dict[str, Any]:
    """Run static build health checks + non-destructive 4-part PoB inflation ('water') audit.

    ``keep`` lists inflation items the user asserts are real for this build (e.g. an
    ascendancy that needs ``conditionStationary``); they are left untouched. With
    ``attribution`` every stripped item is restored alone and re-measured by PoB.
    """
    with worker.lock:
        snap_code = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        stats = digest_before["stats"]
        info = digest_before["buildInfo"]
        items_res = worker.call("list_items", {})

        # Static health checks
        health_issues: list[dict[str, str]] = []

        # 1. Elemental & Chaos resistances
        for elem in ("Fire", "Cold", "Lightning"):
            val = float(stats.get(f"{elem}Resist") or 0)
            over = float(stats.get(f"{elem}ResistOverCap") or 0)
            if val < 75:
                health_issues.append(
                    {
                        "category": "Resistance",
                        "severity": "HIGH",
                        "message": f"{elem} Resist is uncapped: {val:.0f}% (gap: {75 - val:.0f}%)",
                    }
                )
            elif over < 15:
                health_issues.append(
                    {
                        "category": "Resistance",
                        "severity": "LOW",
                        "message": f"{elem} Resist overcap is thin: +{over:.0f}% over cap (vulnerable to curses/exposure)",
                    }
                )

        chaos_res = float(stats.get("ChaosResist") or 0)
        if chaos_res < 0:
            health_issues.append(
                {
                    "category": "Resistance",
                    "severity": "MEDIUM",
                    "message": f"Chaos Resist is negative: {chaos_res:.0f}%",
                }
            )

        # 2. Attributes vs Requirements
        for attr in ("Str", "Dex", "Int"):
            cur = float(stats.get(attr) or 0)
            req = float(stats.get(f"Req{attr}") or 0)
            if req > cur:
                health_issues.append(
                    {
                        "category": "Attribute",
                        "severity": "HIGH",
                        "message": f"{attr} insufficient: have {cur:.0f}, require {req:.0f}",
                    }
                )

        # 3. Accuracy / Hit Chance
        hit_chance = stats.get("HitChance")
        if hit_chance is not None and float(hit_chance) < 100.0:
            health_issues.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": f"Hit Chance is {float(hit_chance):.1f}% (< 100%)",
                }
            )

        # 4. Mana Cost vs Unreserved Mana & Regen
        mana_cost = float(stats.get("ManaCost") or 0)
        mana_unres = float(stats.get("ManaUnreserved") or 0)
        raw_mana_regen = (
            stats.get("ManaRegenRecovery")
            if stats.get("ManaRegenRecovery") is not None
            else stats.get("ManaRegen")
        )
        mana_regen = float(raw_mana_regen or 0)
        mana_leech = float(stats.get("ManaLeechGainRate") or 0)
        speed = float(stats.get("Speed") or 0)
        if mana_cost > 0 and mana_cost > mana_unres and float(stats.get("LifeUnreserved") or 0) > 0:
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "HIGH",
                    "message": f"Skill Mana Cost ({mana_cost:.0f}) exceeds Unreserved Mana ({mana_unres:.0f})",
                }
            )
        elif mana_cost > 0 and mana_regen < 0:
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "MEDIUM",
                    "message": f"Net Mana Regen is negative ({mana_regen:.1f}/s) with Mana Cost {mana_cost:.0f}",
                }
            )
        elif (
            mana_cost > 0
            and speed > 0
            and (mana_cost * speed) > (max(0.0, mana_regen) + mana_leech)
            and float(stats.get("LifeUnreserved") or 0) > 0
        ):
            spend_rate = mana_cost * speed
            rec_rate = max(0.0, mana_regen) + mana_leech
            health_issues.append(
                {
                    "category": "Sustain",
                    "severity": "LOW",
                    "message": (
                        f"Mana spend rate ({spend_rate:.1f}/s = {mana_cost:.0f} * {speed:.2f}/s) "
                        f"exceeds net Mana recovery + leech ({rec_rate:.1f}/s)"
                    ),
                }
            )

        # 5. Unallocated points
        pts = info.get("points", {})
        used, used_max = int(pts.get("used") or 0), int(pts.get("usedMax") or 0)
        asc_used, asc_max = int(pts.get("ascUsed") or 0), int(pts.get("ascMax") or 0)
        if used > used_max:
            health_issues.append(
                {
                    "category": "Points",
                    "severity": "HIGH",
                    "message": f"Over-allocated passive points: {used}/{used_max}",
                }
            )
        if asc_used < asc_max:
            health_issues.append(
                {
                    "category": "Points",
                    "severity": "LOW",
                    "message": f"Unallocated Ascendancy points: {asc_used}/{asc_max}",
                }
            )

        # 6. Sidebar warnings
        for w in digest_before["warnings"]:
            health_issues.append(
                {
                    "category": "EngineWarning",
                    "severity": "HIGH",
                    "message": w,
                }
            )

        # 7. Empty primary equipment slots
        primary_slots = {"Weapon 1", "Body Armour", "Helmet", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt"}
        for slot in items_res.get("slots", []):
            if slot.get("name") in primary_slots and not slot.get("inactive") and not slot.get("selItemId"):
                health_issues.append(
                    {
                        "category": "Equipment",
                        "severity": "MEDIUM",
                        "message": f"Equipment slot '{slot['name']}' is empty",
                    }
                )

        # Run non-destructive de-watering snapshot
        try:
            stripped, records = dewater_build_with_records(worker, keep=keep)
            digest_dewatered = capture_digest(worker)
            dewater_diff = diff_digest(digest_before, digest_dewatered)
            water_attribution = (
                attribute_water(worker, records, digest_dewatered, digest_before["primaryDpsKey"])
                if attribution
                else []
            )
        finally:
            worker.call("import_code", {"code": snap_code, "mode": "replace"})

        return {
            "healthIssues": health_issues,
            "inflationStripped": stripped,
            "waterAttribution": water_attribution,
            "original": {
                "primaryDpsKey": digest_before["primaryDpsKey"],
                "primaryDps": digest_before["primaryDps"],
                "fullDps": stats.get("FullDPS"),
                "combinedDps": stats.get("CombinedDPS"),
                "totalDps": stats.get("TotalDPS"),
                "minionCombinedDps": stats.get("MinionCombinedDPS"),
                "totalEhp": digest_before["totalEhp"],
                "life": digest_before["life"],
                "physMaxHit": stats.get("PhysicalMaximumHitTaken"),
                "eleMaxHit": min(
                    float(stats.get("FireMaximumHitTaken") or 0),
                    float(stats.get("ColdMaximumHitTaken") or 0),
                    float(stats.get("LightningMaximumHitTaken") or 0),
                ),
                "chaosMaxHit": stats.get("ChaosMaximumHitTaken"),
            },
            "dewateredPinnacle": {
                "primaryDpsKey": digest_dewatered["primaryDpsKey"],
                "primaryDps": digest_dewatered["primaryDps"],
                "fullDps": digest_dewatered["stats"].get("FullDPS"),
                "combinedDps": digest_dewatered["stats"].get("CombinedDPS"),
                "totalDps": digest_dewatered["stats"].get("TotalDPS"),
                "minionCombinedDps": digest_dewatered["stats"].get("MinionCombinedDPS"),
                "totalEhp": digest_dewatered["totalEhp"],
                "life": digest_dewatered["life"],
                "physMaxHit": digest_dewatered["stats"].get("PhysicalMaximumHitTaken"),
                "eleMaxHit": min(
                    float(digest_dewatered["stats"].get("FireMaximumHitTaken") or 0),
                    float(digest_dewatered["stats"].get("ColdMaximumHitTaken") or 0),
                    float(digest_dewatered["stats"].get("LightningMaximumHitTaken") or 0),
                ),
                "chaosMaxHit": digest_dewatered["stats"].get("ChaosMaximumHitTaken"),
            },
            "dewaterDiff": dewater_diff,
        }


def resolve_slot_name(worker: BridgeWorker, slot_query: str) -> str:
    """Resolve user slot query (including '#1', '#2' jewel socket shorthand or Flask 1..5) to exact PoB slotName."""
    q = slot_query.strip()
    items_res = worker.call("list_items", {})
    slots = items_res.get("slots", [])

    # Check '#1', '#2' jewel socket shorthand
    m_jewel = re.match(r"^#?(\d+)$", q)
    if m_jewel:
        idx = int(m_jewel.group(1))
        for s in slots:
            if s.get("socketIndex") == idx:
                return s["name"]
        raise ValueError(f"Jewel socket #{idx} not found in build slots.")

    # Check exact or case-insensitive match on name / label / labelZh
    q_lower = q.lower()
    for s in slots:
        if (
            s.get("name", "").lower() == q_lower
            or (s.get("label") or "").lower() == q_lower
            or (s.get("labelZh") or "").lower() == q_lower
        ):
            return s["name"]

    # Check alias like "Flask 1" -> "Flask 1"
    for s in slots:
        if q_lower in s.get("name", "").lower() or q_lower in (s.get("labelZh") or "").lower():
            return s["name"]

    raise ValueError(f"Equipment slot '{slot_query}' not found.")


_KNOWN_CONFIG_DEFAULTS: dict[str, Any] = {
    "enemyIsBoss": "Pinnacle",
    "resistancePenalty": -60,
    "bandit": "None",
    "pantheonMajorGod": "None",
    "pantheonMinorGod": "None",
    "presetBossSkills": "None",
}

_ALWAYS_SHOW_VISIBLE_CONFIG_VARS = frozenset({
    "enemyIsBoss",
})


def _infer_config_default(item: dict[str, Any]) -> Any:
    var = str(item.get("var") or "")
    if var in _KNOWN_CONFIG_DEFAULTS:
        return _KNOWN_CONFIG_DEFAULTS[var]
    itype = str(item.get("type") or "")
    if itype == "check":
        return False
    if itype == "list":
        opts = item.get("list") or []
        if opts and isinstance(opts[0], dict):
            return opts[0].get("val")
        return "None"
    return None


def inspect_config(
    worker: BridgeWorker,
    *,
    include_defaults: bool = False,
) -> dict[str, Any]:
    """Inspect active PoB ConfigTab set, active/non-default config variables, and raw customMods."""
    with worker.lock:
        cfg_res = worker.call("list_config", {})

    active_id = cfg_res.get("activeConfigSetId")
    config_sets = cfg_res.get("configSets", [])
    active_set = next((s for s in config_sets if s.get("id") == active_id), None)

    variables: list[dict[str, Any]] = []
    total_count = 0
    active_count = 0

    for sec in cfg_res.get("sections", []):
        sec_name = sec.get("name") or ""
        sec_name_zh = sec.get("nameZh") or sec_name
        for item in sec.get("items", []):
            var = item.get("var")
            if not var:
                continue
            total_count += 1
            val = item.get("value")
            default_val = _infer_config_default(item)
            visible = bool(item.get("visible", True))

            if item.get("type") == "check":
                is_default = (val in (None, False)) and (default_val is False)
            elif item.get("type") == "list":
                is_default = (val is None) or (val == default_val)
            else:
                is_default = val in (None, "") or (val == default_val)

            is_active = visible and (not is_default or var in _ALWAYS_SHOW_VISIBLE_CONFIG_VARS)
            if is_active:
                active_count += 1

            if include_defaults or is_active:
                entry: dict[str, Any] = {
                    "section": sec_name,
                    "sectionZh": sec_name_zh,
                    "var": var,
                    "label": strip_pob_escapes(item.get("label") or ""),
                    "labelZh": strip_pob_escapes(item.get("labelZh") or item.get("label") or ""),
                    "type": item.get("type"),
                    "value": val,
                    "defaultValue": default_val,
                    "placeholder": item.get("placeholder"),
                    "visible": visible,
                    "isDefault": is_default,
                }
                if item.get("list"):
                    entry["options"] = [
                        {
                            "val": o.get("val"),
                            "label": strip_pob_escapes(o.get("label") or ""),
                            "labelZh": strip_pob_escapes(o.get("labelZh") or o.get("label") or ""),
                        }
                        for o in item["list"]
                    ]
                variables.append(entry)

    custom_mods = cfg_res.get("customMods", [])
    enabled_texts: list[str] = []
    active_custom_lines: list[str] = []
    for blk in custom_mods:
        if blk.get("enabled", True):
            txt = str(blk.get("text") or "")
            if txt.strip():
                enabled_texts.append(txt.strip())
                for line in txt.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        active_custom_lines.append(s)

    return {
        "activeConfigSetId": active_id,
        "activeConfigSet": active_set,
        "configSets": config_sets,
        "variables": variables,
        "activeCount": active_count,
        "totalCount": total_count,
        "includeDefaults": include_defaults,
        "customMods": custom_mods,
        "customModsText": "\n".join(enabled_texts),
        "activeCustomModLines": active_custom_lines,
        "rev": cfg_res.get("rev"),
    }


def inspect_items(
    worker: BridgeWorker,
    slot_selector: str | None = None,
    *,
    slot_name: str | None = None,
    raw: bool = False,
    include_raw: bool = False,
    all_raw: bool = False,
) -> dict[str, Any]:
    """List equipped items (optionally fetching all raw texts + allocated jewel sockets in one lock pass) or inspect one slot."""
    effective_slot = slot_selector if slot_selector is not None else slot_name
    want_raw = bool(raw or include_raw or all_raw)

    with worker.lock:
        items_res = worker.call("list_items", {})
        items_by_id = {it["id"]: it for it in items_res.get("items", [])}

        if effective_slot:
            resolved_slot = resolve_slot_name(worker, effective_slot)
            slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
            item_id = slot_obj.get("selItemId") if slot_obj else None
            if not item_id:
                return {
                    "slot": slot_obj or {"name": resolved_slot},
                    "item": None,
                    "tooltip": None,
                    "raw": None,
                }
            tip = worker.call("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False})
            raw_text = worker.call("item_raw", {"id": item_id}).get("raw") if want_raw else None
            return {
                "slot": slot_obj,
                "item": items_by_id.get(item_id),
                "tooltip": tip,
                "raw": raw_text,
            }

        allocated_nodes: set[int] = set()
        if all_raw:
            tree_state = worker.call("get_tree_state", {})
            allocated_nodes = {int(x) for x in tree_state.get("allocatedNodes", [])}

        equipped_slots: list[dict[str, Any]] = []
        raw_cache: dict[int, str | None] = {}
        for s in items_res.get("slots", []):
            item_id = s.get("selItemId")
            node_id = s.get("nodeId")
            is_jewel_socket = node_id is not None
            is_allocated_socket = is_jewel_socket and (int(node_id) in allocated_nodes)

            if all_raw:
                # Include all equipped active slots AND all allocated jewel sockets (#1..#N)
                if s.get("inactive"):
                    continue
                if not item_id and not is_allocated_socket:
                    continue
            else:
                if not item_id and not s.get("shown"):
                    continue

            item = items_by_id.get(item_id) if item_id else None
            entry: dict[str, Any] = {
                "slotName": s["name"],
                "label": s.get("labelZh") or s.get("label") or s["name"],
                "socketIndex": s.get("socketIndex"),
                "nodeId": node_id,
                "isFlask": bool(s.get("isFlask")),
                "active": bool(s.get("active")),
                "inactive": bool(s.get("inactive")),
                "itemId": item_id,
                "item": item,
            }
            if all_raw and is_jewel_socket:
                entry["allocated"] = is_allocated_socket
            if want_raw and item_id:
                iid = int(item_id)
                if iid not in raw_cache:
                    raw_cache[iid] = worker.call("item_raw", {"id": iid}).get("raw")
                entry["raw"] = raw_cache[iid]
            equipped_slots.append(entry)

        return {
            "activeItemSetId": items_res.get("activeItemSetId"),
            "itemSets": items_res.get("itemSets", []),
            "equippedSlots": equipped_slots,
            "equippedCount": sum(1 for e in equipped_slots if e.get("itemId")),
            "allRaw": all_raw,
            "totalItemsInBuild": len(items_res.get("items", [])),
        }


def resolve_skill_group_index(skills_res: dict[str, Any], group_selector: str | int | None) -> int:
    """Resolve 'main', 1-based group index, or group label substring into 1-based group index."""
    groups = skills_res.get("groups", [])
    if not groups:
        raise ValueError("Build has no skill groups.")
    if group_selector is None or str(group_selector).strip().lower() == "main":
        return int(skills_res.get("mainSocketGroup") or 1)
    s = str(group_selector).strip()
    if s.isdigit():
        idx = int(s)
        if any(g.get("index") == idx for g in groups):
            return idx
        raise ValueError(f"Skill group #{idx} does not exist (1..{len(groups)}).")
    s_lower = s.lower()
    for g in groups:
        for key in ("label", "displayLabel", "displayLabelZh"):
            if s_lower in str(g.get(key) or "").lower():
                return int(g["index"])
    raise ValueError(f"Could not find skill group matching '{group_selector}'.")


def inspect_skills(
    worker: BridgeWorker,
    *,
    group_selector: str | None = None,
) -> dict[str, Any]:
    """List all skill groups or inspect a specific group with per-gem DPS tooltips."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        if group_selector is not None:
            idx = resolve_skill_group_index(skills_res, group_selector)
            group = next(g for g in skills_res.get("groups", []) if g["index"] == idx)
            gems_with_tips = []
            for gem in group.get("gems", []):
                tip = worker.call("gem_tooltip", {"group": idx, "index": gem["index"]})
                gems_with_tips.append({**gem, "tooltipDps": tip.get("dps")})
            return {
                "group": {**group, "gems": gems_with_tips},
                "mainSocketGroup": skills_res.get("mainSocketGroup"),
            }
        return skills_res


def inspect_tree(
    worker: BridgeWorker,
    *,
    search: str | None = None,
) -> dict[str, Any]:
    """Summarize allocated Keystones, Notables, Masteries, Ascendancy nodes, Jewels, or search tree nodes."""
    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        specs_res = worker.call("list_specs", {})
        items_res = worker.call("list_items", {})
        tree_version = tree_state.get("treeVersion")
        nodes_db = worker.get_cached_tree_data(tree_version).get("nodes", {})

        # Build map of dynamic/overridden nodes from get_tree_state
        overrides = tree_state.get("overrides", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
        allocated_ids = [int(nid) for nid in tree_state.get("allocatedNodes", [])]
        allocated_set = set(allocated_ids)

        # Map socket nodeId -> socketIndex from list_items.slots
        socket_index_by_node: dict[int, int] = {}
        for slot in items_res.get("slots", []):
            if slot.get("nodeId") and slot.get("socketIndex"):
                socket_index_by_node[int(slot["nodeId"])] = int(slot["socketIndex"])

        def _get_node_meta(nid: int) -> dict[str, Any]:
            sid = str(nid)
            base = dict(nodes_db.get(sid) or {})
            if sid in dynamic_map:
                base.update(dynamic_map[sid])
            if sid in overrides:
                ov = overrides[sid]
                base["overrideWhy"] = ov.get("why")
                if ov.get("name"):
                    base["name"] = ov["name"]
                if ov.get("nameZh"):
                    base["nameZh"] = ov["nameZh"]
                if ov.get("stats"):
                    base["stats"] = ov["stats"]
                if ov.get("statsZh"):
                    base["statsZh"] = ov["statsZh"]
            base["id"] = nid
            base["allocated"] = nid in allocated_set
            if nid in socket_index_by_node:
                base["socketIndex"] = socket_index_by_node[nid]
            return base

        if search:
            q = search.strip()
            # Check '#1', '#2' jewel socket shorthand
            m_sock = re.match(r"^#(\d+)$", q)
            matches: list[dict[str, Any]] = []
            if m_sock:
                target_idx = int(m_sock.group(1))
                for nid, s_idx in socket_index_by_node.items():
                    if s_idx == target_idx:
                        meta = _get_node_meta(nid)
                        hover = worker.call("node_hover", {"id": nid})
                        meta["cost"] = hover.get("cost")
                        meta["path"] = hover.get("path")
                        matches.append(meta)
            else:
                q_lower = q.lower()
                all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
                for sid in all_node_ids:
                    nid = int(sid)
                    meta = _get_node_meta(nid)
                    haystack = " ".join(
                        [
                            str(meta.get("id") or ""),
                            str(meta.get("name") or ""),
                            str(meta.get("nameZh") or ""),
                            " ".join(str(s) for s in (meta.get("stats") or [])),
                            " ".join(str(s) for s in (meta.get("statsZh") or [])),
                        ]
                    ).lower()
                    if q_lower in haystack:
                        matches.append(meta)

                # Sort allocated first, then Keystone/Notable/Mastery/Socket before Normal
                type_rank = {"Keystone": 0, "Notable": 1, "Mastery": 2, "Socket": 3, "Normal": 4}
                matches.sort(
                    key=lambda m: (
                        0 if m.get("allocated") else 1,
                        type_rank.get(m.get("type", "Normal"), 5),
                        m.get("id", 0),
                    )
                )
                # Enrich top 25 matches with path cost
                for m in matches[:25]:
                    try:
                        hover = worker.call("node_hover", {"id": m["id"]})
                        m["cost"] = hover.get("cost")
                    except Exception:
                        pass

            return {
                "search": search,
                "totalMatches": len(matches),
                "matches": matches[:50],
            }

        keystones: list[dict[str, Any]] = []
        notables: list[dict[str, Any]] = []
        masteries: list[dict[str, Any]] = []
        ascendancy_nodes: list[dict[str, Any]] = []

        for nid in sorted(allocated_ids):
            meta = _get_node_meta(nid)
            ntype = meta.get("type")
            if meta.get("asc"):
                if ntype not in ("AscendClassStart", "ClassStart"):
                    ascendancy_nodes.append(meta)
            elif ntype == "Keystone":
                keystones.append(meta)
            elif ntype == "Notable":
                notables.append(meta)
            elif ntype == "Mastery" or meta.get("overrideWhy") == "mastery":
                masteries.append(meta)

        jewel_sockets = []
        for sock in tree_state.get("sockets", []):
            nid = int(sock["nodeId"])
            jewel_sockets.append(
                {
                    **sock,
                    "socketIndex": socket_index_by_node.get(nid),
                    "allocated": nid in allocated_set,
                }
            )

        return {
            "treeVersion": tree_version,
            "className": tree_state.get("className"),
            "ascendClassName": tree_state.get("ascendClassName"),
            "points": tree_state.get("points"),
            "activeSpec": specs_res.get("activeSpec"),
            "specs": specs_res.get("specs", []),
            "allocatedCount": len(allocated_ids),
            "keystones": keystones,
            "ascendancyNodes": ascendancy_nodes,
            "masteries": masteries,
            "notables": notables,
            "jewelSockets": jewel_sockets,
        }


_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_CALC_PUNCT_RE = re.compile(r"[\s_\-\"'`.,:/\\()\[\]{}%+]+")

_CALC_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "regen": ("regeneration", "recovery", "回复"),
    "regeneration": ("regen", "recovery", "回复"),
    "recovery": ("regen", "regeneration", "回复"),
    "es": ("energy shield", "能量护盾"),
    "ehp": ("effective hit pool", "effective health pool", "有效生命"),
    "effective": ("eff", "有效"),
    "eff": ("effective", "有效"),
    "dps": ("damage", "totaldps", "秒伤", "伤害"),
    "dmg": ("damage", "伤害"),
    "avg": ("average", "平均"),
    "average": ("avg", "平均"),
    "crit": ("critical", "crits", "暴击"),
    "critical": ("crit", "crits", "暴击"),
    "mult": ("multiplier", "multi", "加成", "倍率"),
    "multi": ("multiplier", "mult", "加成", "倍率"),
    "multiplier": ("multi", "mult", "加成", "倍率"),
    "res": ("resist", "resistance", "抗性"),
    "resist": ("res", "resistance", "抗性"),
    "resistance": ("resist", "res", "抗性"),
    "phys": ("physical", "物理"),
    "ele": ("elemental", "元素"),
    "pen": ("penetration", "穿透"),
    "supp": ("suppression", "压制"),
    "suppression": ("supp", "suppress", "压制"),
    "inc": ("increased", "提高"),
    "max": ("maximum", "最大"),
    "min": ("minimum", "最小"),
    "mana": ("魔力",),
    "life": ("生命",),
    "hit": ("击中",),
    "dot": ("持续伤", "damage over time"),
    "speed": ("rate", "速度"),
    "armour": ("armor", "护甲"),
    "armor": ("armour", "护甲"),
    "evasion": ("evade", "闪避"),
    "block": ("格挡",),
    "leech": ("偷取",),
    "cost": ("消耗",),
}


def _norm_calc_text(text: str | None) -> str:
    """Normalize calc label/query: strip PoB escapes, split camelCase, lowercase, collapse punctuation."""
    if not text:
        return ""
    cleaned = strip_pob_escapes(str(text))
    cleaned = _CAMEL_SPLIT_RE.sub(r"\1 \2", cleaned)
    cleaned = _CALC_PUNCT_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def _compact_calc_text(text: str | None) -> str:
    """Strip spaces/underscores/punctuation and lowercase for compact key matching ('Average Hit' == 'averagehit')."""
    return _norm_calc_text(text).replace(" ", "")


def _token_matches_haystack(token: str, hay_norm: str, hay_compact: str) -> bool:
    if token in hay_norm or token in hay_compact:
        return True
    for syn in _CALC_TOKEN_SYNONYMS.get(token, ()):
        syn_norm = _norm_calc_text(syn)
        syn_compact = syn_norm.replace(" ", "")
        if syn_norm in hay_norm or (syn_compact and syn_compact in hay_compact):
            return True
    return False


def _score_calc_row(
    q_norm: str,
    q_compact: str,
    q_tokens: list[str],
    sec_norm: str,
    sub_norm: str,
    row_norm: str,
    cells_norm: str,
    sub_row_compact: str,
) -> float:
    """Score how well a calcs row matches the bilingual query (0.0 means no match)."""
    if not q_compact:
        return 1.0

    row_compact = row_norm.replace(" ", "")
    sub_compact = sub_norm.replace(" ", "")
    full_norm = f"{sec_norm} {sub_norm} {row_norm} {cells_norm}".strip()
    full_compact = full_norm.replace(" ", "")

    # 1. Direct normalized or compact match in row label (highest priority)
    if q_norm in row_norm or q_compact in row_compact:
        return 100.0 + (20.0 if row_compact == q_compact else 0.0)

    # 2. Combined subsection + row match (e.g. '魔力回复' matching sub '魔力' + row '回复')
    if q_compact in sub_row_compact:
        return 90.0

    # 3. Direct match in subsection or section or cell values (e.g. '有效生命' or skill/minion names like '寒霜法师')
    if q_norm in sub_norm or q_compact in sub_compact:
        return 75.0
    if q_norm in cells_norm or q_compact in cells_norm.replace(" ", ""):
        return 70.0
    if q_norm in sec_norm or q_compact in sec_norm.replace(" ", ""):
        return 60.0

    # 4. Multi-token + bilingual synonym matching (e.g. 'Mana Regeneration' -> sub 'Mana' + row 'Recovery')
    if len(q_tokens) >= 2:
        if all(_token_matches_haystack(tok, full_norm, full_compact) for tok in q_tokens):
            row_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, row_norm, row_compact))
            sub_hits = sum(1 for tok in q_tokens if _token_matches_haystack(tok, sub_norm, sub_compact))
            if row_hits > 0:
                return 80.0 + row_hits * 5.0 + sub_hits * 3.0
            return 55.0 + sub_hits * 3.0
    elif len(q_tokens) == 1:
        tok = q_tokens[0]
        if _token_matches_haystack(tok, row_norm, row_compact):
            return 65.0
        if _token_matches_haystack(tok, sub_norm, sub_compact):
            return 50.0
        if _token_matches_haystack(tok, full_norm, full_compact):
            return 40.0

    return 0.0


def search_calcs_breakdown(
    worker: BridgeWorker,
    keyword: str,
    *,
    limit: int = 20,
    max_breakdowns: int = 8,
) -> dict[str, Any]:
    """Bilingual (EN + ZH) search across `get_calcs` rows and fetch `calcs_breakdown` for top matches."""
    q_norm = _norm_calc_text(keyword)
    q_compact = q_norm.replace(" ", "")
    q_tokens = [t for t in q_norm.split(" ") if t]

    with worker.lock:
        # Ensure we have both English and Chinese labels regardless of current set_translate state.
        # When translate=True, bridge.lua's get_calcs returns BOTH English (label / cell.raw) and Chinese (labelZh / cell.text).
        # If translate was False, briefly toggle it on inside try...finally so ZH labels are also indexed.
        orig_translate: bool | None = None
        toggled_translate = False
        if worker.caps.get("translateToggle", False):
            try:
                ver = worker.call("version", {})
                orig_translate = ver.get("translate")
                if orig_translate is False:
                    worker.call("set_translate", {"enabled": True})
                    toggled_translate = True
            except Exception:
                toggled_translate = False

        try:
            calcs = worker.call("get_calcs", {})
            scored_rows: list[tuple[float, int, dict[str, Any]]] = []
            seq = 0

            for sec in calcs.get("sections", []):
                si = sec.get("si")
                sec_id = str(sec.get("id") or "")
                sec_group = str(sec.get("group") or "")
                sec_norm = _norm_calc_text(f"{sec_id} {sec_group}")
                for sub in sec.get("subsections", []):
                    ui = sub.get("ui")
                    sub_en = str(sub.get("label") or "")
                    sub_zh = str(sub.get("labelZh") or "")
                    sub_extra = str(sub.get("extra") or "")
                    sub_norm = _norm_calc_text(f"{sub_en} {sub_zh} {sub_extra}")
                    for row in sub.get("rows", []):
                        ri = row.get("ri")
                        r_en = str(row.get("label") or "")
                        r_zh = str(row.get("labelZh") or "")
                        row_norm = _norm_calc_text(f"{r_en} {r_zh}")

                        cell_parts: list[str] = []
                        for cell in row.get("cells", []):
                            if cell.get("text"):
                                cell_parts.append(str(cell["text"]))
                            if cell.get("raw") and cell.get("raw") != cell.get("text"):
                                cell_parts.append(str(cell["raw"]))
                            widget = cell.get("widget")
                            if isinstance(widget, dict):
                                if widget.get("value"):
                                    cell_parts.append(str(widget["value"]))
                                sel_idx = widget.get("selIndex")
                                w_list = widget.get("list")
                                if isinstance(w_list, list) and isinstance(sel_idx, int) and 1 <= sel_idx <= len(w_list):
                                    sel_entry = w_list[sel_idx - 1]
                                    if isinstance(sel_entry, dict):
                                        cell_parts.append(str(sel_entry.get("label") or ""))
                                        cell_parts.append(str(sel_entry.get("labelZh") or ""))
                                    else:
                                        cell_parts.append(str(sel_entry))
                        cells_norm = _norm_calc_text(" ".join(cell_parts))
                        sub_row_compact = (
                            _compact_calc_text(f"{sub_en} {r_en}")
                            + " "
                            + _compact_calc_text(f"{sub_zh} {r_zh}")
                        )

                        score = _score_calc_row(
                            q_norm,
                            q_compact,
                            q_tokens,
                            sec_norm,
                            sub_norm,
                            row_norm,
                            cells_norm,
                            sub_row_compact,
                        )
                        if score > 0:
                            scored_rows.append(
                                (
                                    score,
                                    seq,
                                    {
                                        "si": si,
                                        "ui": ui,
                                        "ri": ri,
                                        "sec_id": sec_id,
                                        "sec_group": sec_group,
                                        "sub_en": sub_en,
                                        "sub_zh": sub_zh or sub_en,
                                        "row_en": r_en,
                                        "row_zh": r_zh,
                                        "cells": row.get("cells", []),
                                    },
                                )
                            )
                            seq += 1

            # Sort highest match score first, preserving original Calcs order for ties
            scored_rows.sort(key=lambda item: (-item[0], item[1]))

            matches: list[dict[str, Any]] = []
            breakdowns_fetched = 0
            for score, _, item in scored_rows[:limit]:
                cells_out: list[dict[str, Any]] = []
                for cell in item["cells"]:
                    ci = cell.get("ci")
                    c_entry: dict[str, Any] = {
                        "ci": ci,
                        "text": strip_pob_escapes(cell.get("text") or cell.get("raw") or ""),
                        "hasBreakdown": bool(cell.get("hasBreakdown")),
                    }
                    if cell.get("hasBreakdown") and breakdowns_fetched < max_breakdowns:
                        try:
                            bd = worker.call(
                                "calcs_breakdown",
                                {"si": item["si"], "ui": item["ui"], "ri": item["ri"], "ci": ci},
                            )
                            c_entry["breakdown"] = bd.get("sections", [])
                            breakdowns_fetched += 1
                        except Exception:
                            pass
                    cells_out.append(c_entry)

                matches.append(
                    {
                        "sectionId": item["sec_id"],
                        "sectionGroup": item["sec_group"],
                        "subsection": item["sub_zh"],
                        "subsectionEn": item["sub_en"],
                        "rowLabel": item["row_en"],
                        "rowLabelZh": item["row_zh"],
                        "score": score,
                        "coords": {"si": item["si"], "ui": item["ui"], "ri": item["ri"]},
                        "cells": cells_out,
                    }
                )
        finally:
            if toggled_translate and orig_translate is not None:
                try:
                    worker.call("set_translate", {"enabled": orig_translate})
                except Exception:
                    pass

        return {
            "keyword": keyword,
            "totalMatches": len(scored_rows),
            "matches": matches,
        }
