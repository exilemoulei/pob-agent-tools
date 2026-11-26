"""Tier 3 (Jewelry +20% Catalyst) and Tier 4 (Free Small-Node Attribute/Resist Tattoos) enumerators (L2)."""

from __future__ import annotations

import math
import re
from typing import Any

from ..analyzer.pitfalls_tattoos import load_tattoo_catalog
from ..analyzer.tattoo_slots import (
    BUCKET_SMALL_DEX,
    BUCKET_SMALL_INT,
    BUCKET_SMALL_STR,
    TATTOO_MAX_LIMIT,
    _classify_raw_base_bucket,
    _override_tattoo_kind,
    analyze_tattoo_slots,
)
from ..trade_item import parse_item_text
from .deficits import BuildDeficits, RebalanceStep
from .steps_craft_harvest import parse_stat_contributions

JEWELRY_SLOTS: frozenset[str] = frozenset({"Amulet", "Ring 1", "Ring 2", "Belt"})
_CATALYST_HDR_RE = re.compile(r"^Catalyst:\s*(.+?)\s*$", re.IGNORECASE)
_CATALYST_QUAL_RE = re.compile(r"^CatalystQuality:\s*(\d+)\s*$", re.IGNORECASE)
_QUAL_DESC_RE = re.compile(r"^Quality\s*\(([^)]+)\s+Modifiers\):\s*\+?(\d+)%", re.IGNORECASE)
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*\d+", re.IGNORECASE)
_TAGS_PREFIX_RE = re.compile(r"\{tags:[^}]*\}", re.IGNORECASE)
_ITEM_TRAILER_RE = re.compile(
    r"^(?:Split|Mirrored|Fractured Item|Corrupted|Unidentified|Synthesised Item|Foil Unique(?:\s*\(.*\))?)$",
    re.IGNORECASE,
)
_CATALYST_STAT_TAGS: dict[str, tuple[tuple[str, ...], str]] = {
    "intrinsic": (("Str", "Dex", "Int"), "{tags:jewellery_attribute}"),
    "prismatic": (("Fire", "Cold", "Lightning", "Chaos"), "{tags:jewellery_resistance}"),
}

_BUCKET_TO_ATTR: dict[str, str] = {
    BUCKET_SMALL_STR: "Str",
    BUCKET_SMALL_DEX: "Dex",
    BUCKET_SMALL_INT: "Int",
}
_DESC_TO_CATALYST: dict[str, str] = {
    "attribute": "Intrinsic",
    "resistance": "Prismatic",
}

# Verified PoE1 3.30 non-legacy small-node resistance/suppression tattoos (offline fallback when no engine DB).
_FALLBACK_TATTOOS: tuple[dict[str, Any], ...] = (
    {
        "name": "Tattoo of the Ngamahu Firewalker",
        "target_type": BUCKET_SMALL_STR,
        "stats": ["+6% to Fire Resistance"],
        "est_cost": 8.0,
    },
    {
        "name": "Tattoo of the Arohongui Moonwarden",
        "target_type": BUCKET_SMALL_DEX,
        "stats": ["+6% to Cold Resistance"],
        "est_cost": 8.0,
    },
    {
        "name": "Tattoo of the Valako Stormrider",
        "target_type": BUCKET_SMALL_INT,
        "stats": ["+6% to Lightning Resistance"],
        "est_cost": 8.0,
    },
    {
        "name": "Tattoo of the Hinekora Storyteller",
        "target_type": BUCKET_SMALL_INT,
        "stats": ["+3% to Chaos Resistance"],
        "est_cost": 15.0,
    },
    {
        "name": "Tattoo of the Ramako Shaman",
        "target_type": BUCKET_SMALL_DEX,
        "stats": ["+2% chance to Suppress Spell Damage"],
        "est_cost": 22.0,
    },
)


def parse_catalyst_header(raw_text: str) -> tuple[str | None, int]:
    """Return `(catalyst_name, quality_pct)` parsed from PoB item raw headers."""
    cat_name: str | None = None
    qual = 0
    for ln in str(raw_text or "").splitlines():
        s = ln.strip()
        m = _CATALYST_HDR_RE.match(s)
        if m:
            cat_name = m.group(1).strip()
            continue
        m = _CATALYST_QUAL_RE.match(s)
        if m:
            qual = int(m.group(1))
            continue
        m = _QUAL_DESC_RE.match(s)
        if m:
            desc = m.group(1).strip().lower()
            cat_name = _DESC_TO_CATALYST.get(desc, m.group(1).strip())
            qual = int(m.group(2))
    return cat_name, qual


def _annotate_catalyst_mod_lines(lines: list[str], mod_start: int, catalyst_type: str) -> list[str]:
    """Ensure matching implicit/explicit mod lines carry PoB `{tags:...}` so `Item.lua:getCatalystScalar` scales them."""
    spec = _CATALYST_STAT_TAGS.get(catalyst_type.strip().lower())
    if spec is None or mod_start >= len(lines):
        return lines
    target_keys, tag_prefix = spec
    out = list(lines)
    for idx in range(mod_start, len(out)):
        ln = out[idx]
        s = ln.strip()
        if not s or s == "--------" or _ITEM_TRAILER_RE.match(s) or _TAGS_PREFIX_RE.search(s):
            continue
        contrib = parse_stat_contributions(s, use_min_roll=False)
        if any(contrib.get(k, 0.0) > 0 for k in target_keys):
            out[idx] = f"{tag_prefix}{ln}"
    return out


def apply_catalyst_to_raw(raw_text: str, catalyst_type: str, quality: int = 20) -> str:
    """Insert or replace PoB `Catalyst:` / `CatalystQuality:` headers and required `{tags:...}` mod annotations."""
    lines = str(raw_text or "").splitlines()
    cleaned = [
        ln
        for ln in lines
        if not (
            _CATALYST_HDR_RE.match(ln.strip())
            or _CATALYST_QUAL_RE.match(ln.strip())
            or _QUAL_DESC_RE.match(ln.strip())
        )
    ]
    insert_at = len(cleaned)
    has_implicits = False
    for idx, ln in enumerate(cleaned):
        if _IMPLICITS_HDR_RE.match(ln.strip()):
            insert_at = idx
            has_implicits = True
            break
    else:
        if cleaned and cleaned[0].strip().lower().startswith("rarity:"):
            rv = cleaned[0].split(":", 1)[1].strip().upper()
            insert_at = min(len(cleaned), 3 if rv in ("RARE", "UNIQUE") else 2)
    out = [*cleaned[:insert_at], f"Catalyst: {catalyst_type}", f"CatalystQuality: {int(quality)}", *cleaned[insert_at:]]
    # Mod lines begin right after `Implicits: N` (insert_at + 3), or right after the inserted Catalyst headers.
    mod_start = insert_at + (3 if has_implicits else 2)
    out = _annotate_catalyst_mod_lines(out, mod_start, catalyst_type)
    return "\n".join(out)


def _catalyst_eff(raw_val: float, qual: int) -> int:
    """Compute PoB effective stat value from an unscaled base roll and Catalyst quality percentage."""
    if raw_val <= 0.0:
        return 0
    return math.floor(raw_val * (1.0 + max(0, qual) / 100.0) + 1e-9)


def _strip_catalyst_loss(raw_val: float, cur_qual: int) -> int:
    """Compute stat loss when stripping an existing catalyst from an unscaled PoB base roll."""
    if raw_val <= 0.0 or cur_qual <= 0:
        return 0
    return _catalyst_eff(raw_val, cur_qual) - _catalyst_eff(raw_val, 0)


def _item_stat_totals(raw_text: str) -> dict[str, float]:
    item = parse_item_text(raw_text)
    totals: dict[str, float] = {}
    for mod in item.mods:
        contrib = parse_stat_contributions(mod.raw, use_min_roll=False)
        for k, v in contrib.items():
            totals[k] = totals.get(k, 0.0) + v
    return totals


def enumerate_catalyst_steps(
    equipped_items: list[dict[str, Any]],
    deficits: BuildDeficits,
) -> list[RebalanceStep]:
    """Enumerate Tier 3 (~4-30c) Intrinsic/Prismatic +20% Catalyst upgrades on non-corrupted jewelry/belts."""
    needs = deficits.need_map()
    attr_needs = {k: v for k, v in needs.items() if k in ("Str", "Dex", "Int", "Omni")}
    res_needs = {k: v for k, v in needs.items() if k in ("Fire", "Cold", "Lightning", "Chaos")}
    if not attr_needs and not res_needs:
        return []

    steps: list[RebalanceStep] = []
    for entry in equipped_items:
        slot = str(entry.get("slotName") or entry.get("slot") or "")
        raw = str(entry.get("raw") or "")
        if slot not in JEWELRY_SLOTS or not raw.strip():
            continue
        item = parse_item_text(raw)
        if item.corrupted or item.mirrored or item.unidentified:
            continue

        cur_cat, cur_qual = parse_catalyst_header(raw)
        cur_cat_low = (cur_cat or "").lower()
        totals = _item_stat_totals(raw)
        if not totals:
            continue

        # Intrinsic Catalyst -> Attribute modifiers (PoB raw lines hold unscaled base rolls)
        if attr_needs and not (cur_cat_low == "intrinsic" and cur_qual >= 20):
            cur_eff_q = cur_qual if cur_cat_low == "intrinsic" else 0
            covers: dict[str, float] = {}
            for ak in ("Str", "Dex", "Int"):
                raw_val = totals.get(ak, 0.0)
                gain = _catalyst_eff(raw_val, 20) - _catalyst_eff(raw_val, cur_eff_q)
                if gain > 0:
                    covers[ak] = float(gain)
            if cur_cat_low == "prismatic" and cur_qual > 0:
                for rk in ("Fire", "Cold", "Lightning", "Chaos"):
                    loss = _strip_catalyst_loss(totals.get(rk, 0.0), cur_qual)
                    if loss > 0:
                        covers[rk] = -float(loss)
            if any(covers.get(k, 0.0) > 0 for k in attr_needs) or ("Omni" in attr_needs and sum(covers.get(k, 0.0) for k in ("Str", "Dex", "Int")) > 0):
                steps.append(
                    RebalanceStep(
                        tier=3,
                        kind="catalyst",
                        slot=slot,
                        target_key=f"item:{slot}:catalyst",
                        title=f"Apply 20% Intrinsic Catalyst (Attribute) on {slot}",
                        est_cost_chaos=14.0,
                        cost_label="~14c (20x Intrinsic Catalyst)",
                        covers=covers,
                        apply_spec={"kind": "item_catalyst", "slot": slot, "catalyst": "Intrinsic", "quality": 20},
                    )
                )

        # Prismatic Catalyst -> Resistance modifiers (PoB raw lines hold unscaled base rolls)
        if res_needs and not (cur_cat_low == "prismatic" and cur_qual >= 20):
            cur_eff_q = cur_qual if cur_cat_low == "prismatic" else 0
            covers_r: dict[str, float] = {}
            for rk in ("Fire", "Cold", "Lightning", "Chaos"):
                raw_val = totals.get(rk, 0.0)
                gain = _catalyst_eff(raw_val, 20) - _catalyst_eff(raw_val, cur_eff_q)
                if gain > 0:
                    covers_r[rk] = float(gain)
            if cur_cat_low == "intrinsic" and cur_qual > 0:
                for ak in ("Str", "Dex", "Int"):
                    loss = _strip_catalyst_loss(totals.get(ak, 0.0), cur_qual)
                    if loss > 0:
                        covers_r[ak] = -float(loss)
            if any(covers_r.get(k, 0.0) > 0 for k in res_needs):
                steps.append(
                    RebalanceStep(
                        tier=3,
                        kind="catalyst",
                        slot=slot,
                        target_key=f"item:{slot}:catalyst",
                        title=f"Apply 20% Prismatic Catalyst (Resistance) on {slot}",
                        est_cost_chaos=22.0,
                        cost_label="~22c (20x Prismatic Catalyst)",
                        covers=covers_r,
                        apply_spec={"kind": "item_catalyst", "slot": slot, "catalyst": "Prismatic", "quality": 20},
                    )
                )
    return steps


def _load_small_rebalance_tattoos(
    tattoo_catalog: dict[str, dict[str, Any]] | None,
    worker: Any,
) -> list[dict[str, Any]]:
    cat = tattoo_catalog if tattoo_catalog is not None else load_tattoo_catalog(worker)
    if not cat:
        return [
            {
                "name": fb["name"],
                "target_type": fb["target_type"],
                "contrib": parse_stat_contributions("\n".join(fb["stats"]), use_min_roll=True),
                "est_cost": fb["est_cost"],
            }
            for fb in _FALLBACK_TATTOOS
        ]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in cat.values():
        if not isinstance(rec, dict) or rec.get("legacy") or rec.get("kind") != "tattoo":
            continue
        if int(rec.get("min_connected") or 0) > 0 or int(rec.get("max_connected") or 100) < 100:
            continue
        tt = str(rec.get("target_type") or "")
        if tt not in _BUCKET_TO_ATTR:
            continue
        nm = str(rec.get("name") or "")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        stats_txt = "\n".join(str(s) for s in (rec.get("stats") or ()) if s)
        contrib = parse_stat_contributions(stats_txt, use_min_roll=True)
        if contrib:
            est = 22.0 if "SpellSuppression" in contrib else (15.0 if "Chaos" in contrib else 8.0)
            out.append({"name": nm, "target_type": tt, "contrib": contrib, "est_cost": est})
    return out


def enumerate_tattoo_steps(
    tree_state: dict[str, Any] | None,
    nodes_db: dict[str, Any] | None,
    deficits: BuildDeficits,
    *,
    tattoo_catalog: dict[str, dict[str, Any]] | None = None,
    worker: Any = None,
    max_nodes_per_bucket: int = 3,
) -> list[RebalanceStep]:
    """Enumerate Tier 4 (~8-22c) free small-node Tattoo candidates on surplus-attribute passives."""
    needs = deficits.need_map()
    if not needs or not isinstance(tree_state, dict) or not isinstance(nodes_db, dict):
        return []

    slots_info = analyze_tattoo_slots(tree_state, nodes_db)
    if not slots_info.get("buildLoaded") or int(slots_info.get("tattoosUsed") or 0) >= TATTOO_MAX_LIMIT:
        return []

    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    dyn_map = {
        str(n["id"]): n
        for n in (tree_state.get("dynamicNodes") or ())
        if isinstance(n, dict) and "id" in n
    }
    free_by_bucket: dict[str, list[int]] = {BUCKET_SMALL_STR: [], BUCKET_SMALL_DEX: [], BUCKET_SMALL_INT: []}
    for raw_nid in sorted({int(x) for x in (tree_state.get("allocatedNodes") or ()) if str(x).lstrip("-").isdigit()}):
        sid = str(raw_nid)
        dyn = dyn_map.get(sid)
        if isinstance(dyn, dict) and (dyn.get("expansion") or dyn.get("expansionSkill")):
            continue
        raw_base = nodes_db.get(sid)
        if not isinstance(raw_base, dict):
            continue
        bk = _classify_raw_base_bucket(raw_base)
        if bk not in free_by_bucket:
            continue
        if _override_tattoo_kind(overrides.get(sid), bk) != "free":
            continue
        free_by_bucket[bk].append(raw_nid)

    tattoos = _load_small_rebalance_tattoos(tattoo_catalog, worker)
    steps: list[RebalanceStep] = []
    for tat in tattoos:
        bk = tat["target_type"]
        sacrificed_attr = _BUCKET_TO_ATTR[bk]
        # Never sacrifice an attribute that does not have >=10 surplus over requirements!
        if deficits.attr_surplus(sacrificed_attr) < 10.0:
            continue
        contrib: dict[str, float] = dict(tat["contrib"])
        if not any(contrib.get(k, 0.0) > 0 for k in needs):
            continue
        covers = dict(contrib)
        covers[sacrificed_attr] = covers.get(sacrificed_attr, 0.0) - 10.0
        for nid in free_by_bucket.get(bk, [])[:max_nodes_per_bucket]:
            steps.append(
                RebalanceStep(
                    tier=4,
                    kind="tattoo",
                    slot=f"Node {nid}",
                    target_key=f"node:{nid}",
                    title=f"Tattoo Node {nid} ({bk}): {tat['name']}",
                    est_cost_chaos=float(tat["est_cost"]),
                    cost_label=f"~{tat['est_cost']:g}c (Tattoo)",
                    covers=covers,
                    apply_spec={"kind": "tattoo", "node": nid, "tattoo": tat["name"]},
                )
            )
    return steps
