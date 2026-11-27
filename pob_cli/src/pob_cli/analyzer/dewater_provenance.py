"""Pass 0 dewater provenance: Lua probe gating, Mageblood/flask sustain, and Offering corpse checks (L2)."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from .dewater_mageblood import mageblood_exempt_flask_slots
from .summary import _has_bloodsoaked_blade_keystone, _is_tincture_item

_OFFERING_TOKENS = (
    "bone offering",
    "flesh offering",
    "spirit offering",
    "骸骨奉献",
    "骸骨奉獻",
    "装甲奉献",
    "血肉奉献",
    "血肉奉獻",
    "灵魂奉献",
    "靈魂奉獻",
)
_CORPSE_GEM_TOKENS = ("desecrate", "unearth", "亵渎", "褻瀆", "亵渎之矛", "屍術矢")
_CORPSE_ITEM_TOKENS = ("corpsewalker", "aukuna's will", "灵盒行者", "行屍走肉", "奥库娜的意志", "奧庫納之意")
_UTILITY_DISABLED_TOKENS = (
    "utility flasks are disabled",
    "flasks do not apply to you",
    "功能药剂被禁用",
    "功能藥劑被禁用",
)


def supports_dewater_probes(worker: Any) -> bool:
    """True only when worker advertises `dewaterProbes` (FakeWorker with caps={} stays 0-RPC)."""
    caps = getattr(worker, "caps", None)
    return bool(isinstance(caps, dict) and caps.get("dewaterProbes"))


def fetch_dewater_provenance(worker: Any) -> dict[str, Any] | None:
    """Fetch read-only Pass 0 provenance from Lua when supported; returns None with 0 RPCs otherwise."""
    if not supports_dewater_probes(worker):
        return None
    try:
        res = worker.call("get_dewater_provenance", {})
    except Exception:
        return None
    if isinstance(res, dict) and res.get("ok") is True and not res.get("_error"):
        return res
    return None


def _has_utility_flasks_disabled_fallback(
    items_res: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
) -> bool:
    for entry in equipped_slots_raw or []:
        if not isinstance(entry, dict) or "Swap" in str(entry.get("slotName") or ""):
            continue
        low = strip_pob_escapes(str(entry.get("raw") or "")).lower()
        if any(tok in low for tok in _UTILITY_DISABLED_TOKENS):
            return True
    if not isinstance(items_res, dict):
        return False
    items_by_id = {
        it["id"]: it for it in (items_res.get("items") or []) if isinstance(it, dict) and "id" in it
    }
    for slot in items_res.get("slots") or []:
        if not isinstance(slot, dict) or slot.get("inactive") or "Swap" in str(slot.get("name") or ""):
            continue
        it = items_by_id.get(slot.get("selItemId") or 0) or {}
        blob = " ".join(
            strip_pob_escapes(str(it.get(k) or ""))
            for k in ("title", "name", "titleZh", "nameZh", "raw")
        ).lower()
        if any(tok in blob for tok in _UTILITY_DISABLED_TOKENS):
            return True
    return False


def _mageblood_label_from_probe(mb: dict[str, Any]) -> str:
    left_c = int(mb.get("leftMagicUtility") or 0)
    right_c = int(mb.get("rightMagicUtility") or 0)
    if right_c > 0 and left_c <= 0:
        return f"Mageblood Rightmost {right_c} Magic Utility Flask"
    if left_c > 0 and right_c > 0:
        return f"Mageblood Leftmost {left_c} + Rightmost {right_c} Magic Utility Flask"
    return f"Mageblood Leftmost {max(1, left_c)} Magic Utility Flask"


def evaluate_flask_provenance(
    worker: BridgeWorker,
    items_res: dict[str, Any],
    *,
    provenance: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    has_bloodsoaked_blade: bool | None = None,
    auto_keep_sustain: bool = True,
) -> dict[str, Any]:
    """Compute Pass 0 Mageblood exemptions, single-boss sustained flasks, Alchemist's Genius, and Tincture state."""
    mb_probe = provenance.get("mageblood") if isinstance(provenance, dict) else None
    flasks_probe = provenance.get("flasks") if isinstance(provenance, dict) else None
    util_disabled = (
        bool(mb_probe.get("utilityFlasksDisabled"))
        if isinstance(mb_probe, dict)
        else _has_utility_flasks_disabled_fallback(items_res, equipped_slots_raw)
    )

    mb_slots: set[str] = set()
    mb_label = ""
    if util_disabled:
        mb_slots, mb_label = set(), ""
    elif isinstance(mb_probe, dict):
        controlled = [str(s) for s in (mb_probe.get("controlledSlots") or []) if s]
        if controlled:
            mb_slots = set(controlled)
            mb_label = _mageblood_label_from_probe(mb_probe)
    else:
        mb_slots, mb_label = mageblood_exempt_flask_slots(
            worker, items_res, equipped_slots_raw=equipped_slots_raw
        )

    sustained_slots: dict[str, str] = {}
    sustained_life_flask = False
    sustained_active_non_mb = 0
    if isinstance(flasks_probe, list):
        for f in flasks_probe:
            if not isinstance(f, dict):
                continue
            sname = str(f.get("slot") or "")
            if not sname or f.get("isTincture") or f.get("chargeGainBlocked"):
                continue
            is_sust = bool(f.get("sustained"))
            if not is_sust:
                continue
            if f.get("isLife") and f.get("active"):
                sustained_life_flask = True
            if sname in mb_slots:
                continue
            if auto_keep_sustain:
                min_pct = float(f.get("minUptimePct") or 100.0)
                rate = float(f.get("chargesPerSec") or 0.0)
                sustained_slots[sname] = (
                    f"Single-boss sustained flask (minUptime {min_pct:.0f}%, {rate:.2f} ch/s)"
                )
            if f.get("active"):
                sustained_active_non_mb += 1

    alchemists_genius_reason: str | None = None
    if (
        auto_keep_sustain
        and isinstance(mb_probe, dict)
        and mb_probe.get("hasSpikedConcoction")
        and sustained_active_non_mb >= 1
    ):
        alchemists_genius_reason = (
            f"Spiked Concoction + {sustained_active_non_mb} single-boss sustained flask(s)"
        )

    items_by_id = {
        it["id"]: it for it in (items_res.get("items") or []) if isinstance(it, dict) and "id" in it
    }
    has_active_tincture = any(
        slot.get("isFlask")
        and slot.get("active")
        and _is_tincture_item(items_by_id.get(slot.get("selItemId") or 0))
        for slot in (items_res.get("slots") or [])
        if isinstance(slot, dict)
    )
    if has_bloodsoaked_blade is not None:
        bloodsoaked_active = bool(has_bloodsoaked_blade)
    elif isinstance(mb_probe, dict) and "hasBloodsoakedBlade" in mb_probe:
        bloodsoaked_active = bool(mb_probe.get("hasBloodsoakedBlade"))
    else:
        bloodsoaked_active = _has_bloodsoaked_blade_keystone(worker) if has_active_tincture else False

    return {
        "magebloodSlots": mb_slots,
        "magebloodLabel": mb_label,
        "utilityFlasksDisabled": util_disabled,
        "sustainedSlots": sustained_slots,
        "sustainedLifeFlask": sustained_life_flask,
        "alchemistsGeniusReason": alchemists_genius_reason,
        "bloodsoakedBlade": bloodsoaked_active,
    }


def is_offering_gem(gem: dict[str, Any]) -> bool:
    """True when `gem` is Bone / Flesh / Spirit Offering."""
    blob = " ".join(
        strip_pob_escapes(str(gem.get(k) or "")) for k in ("nameSpec", "name", "nameZh", "skillId")
    ).lower()
    return any(tok in blob for tok in _OFFERING_TOKENS)


def has_corpse_source(
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None = None,
    *,
    provenance: dict[str, Any] | None = None,
) -> bool:
    """True when the build has a single-boss corpse generator (Desecrate, Unearth, Corpsewalker, Aukuna's Will)."""
    if isinstance(provenance, dict) and provenance.get("corpseSources"):
        return True
    for grp in (skills_res or {}).get("groups") or []:
        if not isinstance(grp, dict) or not grp.get("enabled", True) or "Swap" in str(grp.get("slot") or ""):
            continue
        for gm in grp.get("gems") or []:
            if not isinstance(gm, dict) or not gm.get("enabled", True):
                continue
            blob = " ".join(
                strip_pob_escapes(str(gm.get(k) or "")) for k in ("nameSpec", "name", "nameZh", "skillId")
            ).lower()
            if any(tok in blob for tok in _CORPSE_GEM_TOKENS):
                return True
    if isinstance(items_res, dict):
        items_by_id = {
            it["id"]: it for it in (items_res.get("items") or []) if isinstance(it, dict) and "id" in it
        }
        for slot in items_res.get("slots") or []:
            if not isinstance(slot, dict) or slot.get("inactive") or "Swap" in str(slot.get("name") or ""):
                continue
            it = items_by_id.get(slot.get("selItemId") or 0) or {}
            blob = " ".join(
                strip_pob_escapes(str(it.get(k) or "")) for k in ("title", "name", "titleZh", "nameZh")
            ).lower()
            if any(tok in blob for tok in _CORPSE_ITEM_TOKENS):
                return True
    return False


_CLOSE_RANGE_SKILLS = (
    "flicker strike",
    "cyclone",
    "blade vortex",
    "smite",
    "闪现打击",
    "旋风斩",
    "飞刃风暴",
    "惩击",
    "閃現打擊",
    "旋風斬",
    "飛刃風暴",
    "重擊",
)
_ELDRITCH_ENDURANCE_RE = re.compile(r"gain\s+an?\s+endurance\s+charge\s+every\s+(\d+(?:\.\d+)?)\s+seconds?", re.IGNORECASE)
_SWORD_FRENZY_RE = re.compile(r"(\d+)%\s+chance\s+to\s+gain\s+a\s+frenzy\s+charge\s+when\s+you\s+hit\s+a\s+unique\s+enemy", re.IGNORECASE)
_MARK_FRENZY_RE = re.compile(r"chance\s+to\s+gain\s+a\s+frenzy\s+charge\s+when\s+you\s+hit\s+your\s+marked\s+enemy", re.IGNORECASE)
_PROXY_GEM_TOKENS = (
    "spell totem", "ballista totem", "trap", "cluster traps", "multiple traps",
    "high-impact mine", "blastchain mine", "minefield", "remote mine",
)


def _iter_active_groups(skills_res: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        g for g in ((skills_res or {}).get("groups") or [])
        if isinstance(g, dict) and g.get("enabled", True) and "Swap" not in str(g.get("slot") or "")
    ]


def _group_gem_names(grp: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    return [
        (gm, strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip().lower())
        for gm in (grp.get("gems") or [])
        if isinstance(gm, dict) and gm.get("enabled", True)
    ]


def _group_is_proxy(grp: dict[str, Any]) -> bool:
    for _, nm in _group_gem_names(grp):
        base = nm[:-8].strip() if nm.endswith(" support") else nm
        if base.startswith("awakened "):
            base = base[9:].strip()
        if base in _PROXY_GEM_TOKENS or nm.startswith(("summon ", "raise ", "animate ")):
            return True
    return False


def is_close_range_main_skill(skills_res: dict[str, Any] | None, provenance: dict[str, Any] | None = None) -> bool:
    """True when main skill is a non-proxy melee or close-range skill."""
    ms = ((provenance or {}).get("flags") or {}).get("mainSkill") or {}
    if ms.get("isMelee") and not ms.get("isProxy"):
        return True
    main_idx = (skills_res or {}).get("mainSocketGroup")
    for g in _iter_active_groups(skills_res):
        if bool(g.get("isMain")) or (main_idx is not None and g.get("index") == main_idx):
            if any(not gm.get("support") and any(tok in nm for tok in _CLOSE_RANGE_SKILLS) for gm, nm in _group_gem_names(g)):
                return True
    return False


def infer_v2_static_auto_keeps(
    *,
    stats: dict[str, Any] | None,
    skills_res: dict[str, Any] | None,
    node_names: set[str],
    item_blob: str,
    mod_lines: list[str],
    mods_low: str,
    endurance_duration: float = 10.0,
) -> dict[str, str]:
    """Infer V2 non-proxy Power/Frenzy/Endurance charge and core buff auto-keeps."""
    from .dewater_mechanics import _effective_hit_rate, is_proxy_main_skill

    out: dict[str, str] = {}
    hit_rate = _effective_hit_rate(stats)
    st = stats or {}
    crit_pct = max(_finite_float(st.get("EffectiveCritChance")), _finite_float(st.get("CritChance")))
    main_proxy = is_proxy_main_skill(skills_res)
    has_any_mark = False

    for grp in _iter_active_groups(skills_res):
        grp_proxy = _group_is_proxy(grp)
        gnames = _group_gem_names(grp)
        sup_bases = {
            (nm[:-8].strip() if nm.endswith(" support") else nm).removeprefix("awakened ").strip()
            for gm, nm in gnames
            if gm.get("support")
        }
        has_autoexert = "autoexertion" in sup_bases
        for gm, nm in gnames:
            if not gm.get("support") and "mark" in nm and "support" not in nm:
                has_any_mark = True
            if gm.get("support") and not grp_proxy and "power charge on critical" in nm and (crit_pct <= 0 or crit_pct >= 15.0):
                out.setdefault("usepowercharges", "Non-proxy Power Charge on Critical Support")
            if gm.get("support") and "charged mines" in nm:
                out.setdefault("usepowercharges", "Charged Mines Support")
            if not gm.get("support") and "assassin's mark" in nm:
                qual = int(_finite_float(gm.get("quality"), default=0.0))
                if qual >= 4 and (hit_rate <= 0.0 or hit_rate * (0.0025 * qual) * 10.0 >= 1.5):
                    out.setdefault("usepowercharges", f"Assassin's Mark Q{qual}% ({0.25 * qual:.1f}% on hit)")
            if not gm.get("support") and "flicker strike" in nm:
                out.setdefault("usefrenzycharges", "Flicker Strike 20% Frenzy Charge on hit")
            if not gm.get("support") and (nm == "frenzy" or nm.startswith("frenzy of ")):
                out.setdefault("usefrenzycharges", "Manaforged + Frenzy" if "manaforged arrows" in sup_bases else "Enabled Frenzy skill")
            if not gm.get("support") and "enduring cry" in nm and not has_autoexert:
                out.setdefault("useendurancecharges", "Manual Enduring Cry (non-Autoexertion)")
            if gm.get("support") and not grp_proxy and "lifetap" in nm:
                out.setdefault("bufflifetap", "Non-proxy Lifetap Support")
            if gm.get("support") and not grp_proxy and "arcane surge" in nm:
                out.setdefault("buffarcanesurge", "Arcane Surge Support")
            if not gm.get("support") and "zealotry" in nm and (crit_pct >= 35.0 or not st):
                out.setdefault("conditiononconsecratedground", "Zealotry crit Consecrated Ground")

    if any("unstable infusion" in n for n in node_names) and not main_proxy:
        out.setdefault("usepowercharges", "Assassin Unstable Infusion")
    if any("forbidden power" in n for n in node_names):
        out.setdefault("usepowercharges", "Occultist Forbidden Power")
    if "terminus est" in item_blob and not main_proxy:
        out.setdefault("usefrenzycharges", "Terminus Est Frenzy Charge on crit")
    if has_any_mark and (any("mark the prey" in n for n in node_names) or _MARK_FRENZY_RE.search(mods_low)):
        out.setdefault("usefrenzycharges", "Mark Mastery Frenzy Charge on hit")
    if _SWORD_FRENZY_RE.search(mods_low) and not main_proxy:
        out.setdefault("usefrenzycharges", "Sword Mastery 8% Frenzy Charge on Unique hit")
    for ln in mod_lines:
        em = _ELDRITCH_ENDURANCE_RE.search(ln)
        if em and float(em.group(1)) <= max(endurance_duration, 10.0):
            out.setdefault("useendurancecharges", f"Eldritch Endurance Charge every {em.group(1)}s (dur {endurance_duration:.0f}s)")
            break
    if any("arcane blessing" in n for n in node_names):
        out.setdefault("buffarcanesurge", "Hierophant Arcane Blessing")
    if any("sanctuary" in n for n in node_names) and any("pious path" in n for n in node_names):
        out.setdefault("conditiononconsecratedground", "Inquisitor Sanctuary + Pious Path")
    if "rational doctrine" in item_blob:
        out.setdefault("conditiononconsecratedground", "Rational Doctrine Consecrated Ground")
    if any("brutal fervour" in n for n in node_names):
        out.setdefault("conditionleeching", "Slayer Brutal Fervour overleech")
        out.setdefault("conditionleechinglife", "Slayer Brutal Fervour overleech")
    if any("soul drinker" in n for n in node_names):
        out.setdefault("conditionleeching", "Trickster Soul Drinker ES overleech")
        out.setdefault("conditionleechingenergyshield", "Trickster Soul Drinker ES overleech")
        out.setdefault("conditionfullenergyshield", "Trickster Soul Drinker Full ES")
    return out


def _dewater_flasks(
    worker: BridgeWorker,
    items_res: dict[str, Any],
    keep_set: set[str],
    known: dict[str, str],
    stripped: dict[str, Any],
    mutations: list[tuple[str, dict[str, Any]]],
    records: list[dict[str, Any]],
    *,
    has_bloodsoaked_blade: bool | None,
    flask_notes: dict[str, str] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    flask_verdict: dict[str, Any] | None = None,
) -> None:
    verdict = flask_verdict or evaluate_flask_provenance(
        worker,
        items_res,
        equipped_slots_raw=equipped_slots_raw,
        has_bloodsoaked_blade=has_bloodsoaked_blade,
    )
    mb_exempt: set[str] = set(verdict.get("magebloodSlots") or ())
    mb_spec = str(verdict.get("magebloodLabel") or "")
    sustained_slots: dict[str, str] = dict(verdict.get("sustainedSlots") or {})
    bloodsoaked_active = bool(verdict.get("bloodsoakedBlade"))
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("isFlask") and slot.get("name"):
            known[str(slot["name"]).lower()] = str(slot["name"])
    for slot in items_res.get("slots", []):
        if slot.get("isFlask") and slot.get("active"):
            slot_name = slot["name"]
            item = items_by_id.get(slot.get("selItemId") or 0, {})
            item_title = strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("name") or slot_name)
            if "flasks" in keep_set or slot_name.lower() in keep_set:
                stripped["kept"].append(f"{slot_name} ({item_title}) (kept)")
                continue
            if slot_name in mb_exempt:
                stripped.setdefault("autoKept", []).append(f"{slot_name} ({item_title}) (auto-kept: {mb_spec})")
                continue
            if slot_name in sustained_slots:
                stripped.setdefault("autoKept", []).append(
                    f"{slot_name} ({item_title}) (auto-kept: {sustained_slots[slot_name]})"
                )
                continue
            if bloodsoaked_active and _is_tincture_item(item):
                stripped["kept"].append(
                    f"{slot_name} ({item_title}) (kept: Bloodsoaked Blade sustained Tincture; Tinctures != Flasks)"
                )
                continue
            f_note = (flask_notes or {}).get(slot_name)
            note_suffix = f" [{f_note}]" if f_note else ""
            stripped["flasks"].append(f"{slot_name} ({item_title}) -> active=False{note_suffix}")
            mutations.append(("set_slot_active", {"slotName": slot_name, "active": False}))
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
