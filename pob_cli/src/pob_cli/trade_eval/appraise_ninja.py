"""poe.ninja table loading, variant-safe gem/unique/oil lookup, and anoint pricing for `pob trade appraise`."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from typing import Any

_DEFAULT_TW_DIVINE_CHAOS = 150.0
_GEM_VARIANT_RE = re.compile(r"^(\d+)(?:/(\d+))?(c)?$", re.IGNORECASE)
_NINJA_UNIQUE_CATS: tuple[str, ...] = (
    "UniqueWeapon",
    "UniqueArmour",
    "UniqueAccessory",
    "UniqueFlask",
    "UniqueJewel",
    "ForbiddenJewel",
    "UniqueTincture",
)


def _tw_divine_rate() -> tuple[float, str]:
    raw_div = os.environ.get("POB_TW_DIVINE_CHAOS", "").strip()
    if raw_div:
        try:
            div_val = float(raw_div)
            if math.isfinite(div_val) and div_val > 0:
                return div_val, "env"
        except ValueError:
            pass
    return _DEFAULT_TW_DIVINE_CHAOS, "static_fallback"


def load_ninja_tables(
    league: str,
    *,
    realm: str,
    proxy: str | None = None,
    dense_override: dict[str, list[dict[str, Any]]] | None = None,
    stash_override: dict[str, list[dict[str, Any]]] | None = None,
    fetch_dense: Callable[..., dict[str, list[dict[str, Any]]]],
    fetch_stash: Callable[..., tuple[dict[str, list[dict[str, Any]]], set[str], set[str]]],
    fetch_rates: Callable[..., dict[str, float]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], float, dict[str, float], str]:
    """Load dense + stash poe.ninja tables and currency rates, returning `divineRateSource`."""
    if realm == "tw" and dense_override is None:
        tw_div, tw_src = _tw_divine_rate()
        return {}, {}, tw_div, {"chaos": 1.0, "divine": tw_div, "exalted": 15.0}, tw_src

    dense = dense_override if dense_override is not None else {}
    stash = stash_override if stash_override is not None else {}
    if dense_override is None:
        try:
            dense = fetch_dense(league, proxy=proxy)
        except Exception:
            dense = {}
    if stash_override is None and dense:
        try:
            fetched, _, _ = fetch_stash(
                league,
                ("SkillGem", "UniqueWeapon", "UniqueArmour", "ForbiddenJewel", "Oil"),
                proxy=proxy,
                max_workers=4,
            )
            stash = fetched
        except Exception:
            stash = {}

    div_rate = _DEFAULT_TW_DIVINE_CHAOS
    div_rate_source = "static_fallback"
    for ln in dense.get("Currency", []):
        if str(ln.get("name") or "").lower() == "divine orb":
            val = float(ln.get("chaos") or ln.get("chaosValue") or 0.0)
            if val > 0:
                div_rate = val
                div_rate_source = "ninja"
                break
    rates: dict[str, float] = {"chaos": 1.0, "divine": div_rate, "exalted": 15.0}
    if dense_override is None and realm != "tw":
        try:
            ninja_rates = fetch_rates(league, proxy=proxy)
            if ninja_rates.get("Divine Orb"):
                div_rate = float(ninja_rates["Divine Orb"])
                rates["divine"] = div_rate
                div_rate_source = "ninja"
            if ninja_rates.get("Exalted Orb"):
                rates["exalted"] = float(ninja_rates["Exalted Orb"])
            if ninja_rates.get("Mirror of Kalandra"):
                rates["mirror"] = float(ninja_rates["Mirror of Kalandra"])
        except Exception:
            pass
    return dense, stash, div_rate, rates, div_rate_source


def parse_gem_line_spec(ln: dict[str, Any]) -> tuple[int | None, int | None, bool | None]:
    raw_lv = ln.get("gemLevel")
    if raw_lv is not None:
        return int(raw_lv), int(ln.get("gemQuality") or 0), bool(ln.get("corrupted"))
    var = str(ln.get("variant") or "").strip()
    m = _GEM_VARIANT_RE.match(var)
    if not m:
        return None, None, None
    lv = int(m.group(1))
    ql = int(m.group(2)) if m.group(2) is not None else 0
    corr = bool(m.group(3))
    return lv, ql, corr


def lookup_ninja_item_chaos(
    item_rec: dict[str, Any],
    dense: dict[str, list[dict[str, Any]]],
    stash: dict[str, list[dict[str, Any]]],
) -> float | None:
    """Look up a poe.ninja floor price in chaos; never falls back to a mismatched gem variant."""
    kind = str(item_rec.get("kind") or "")
    name_low = str(item_rec.get("name") or "").strip().lower()
    if not name_low:
        return None

    if kind == "gem":
        target_lv = int(item_rec.get("gemLevel") or 1)
        target_q = int(item_rec.get("gemQuality") or 0)
        target_corr = bool(item_rec.get("corrupted"))
        gem_names = (
            {name_low, f"{name_low} support"}
            if not name_low.endswith(" support")
            else {name_low, name_low[:-8].strip()}
        )
        gem_rows = (stash.get("SkillGem") or []) + (dense.get("SkillGem") or []) + (dense.get("ImbuedGem") or [])
        exact_var_cands = {
            f"{target_lv}/{target_q}{'c' if target_corr else ''}",
            *(({f"{target_lv}{'c' if target_corr else ''}"}) if target_q == 0 else ()),
        }
        same_lv_q_val: float | None = None
        for ln in gem_rows:
            if str(ln.get("name") or "").strip().lower() not in gem_names:
                continue
            val = float(ln.get("chaosValue") or ln.get("chaos") or 0.0)
            if val <= 0:
                continue
            var_low = str(ln.get("variant") or "").strip().lower()
            lv, ql, corr = parse_gem_line_spec(ln)
            if var_low and var_low in exact_var_cands:
                return val
            if lv is not None and lv == target_lv and ql == target_q and bool(corr) == target_corr:
                return val
            # Only fall back across the corruption flag when level AND quality match exactly;
            # never price a 5/20 Awakened or 21/20 gem from a 1/0 base row.
            if lv is not None and lv == target_lv and ql == target_q and same_lv_q_val is None:
                same_lv_q_val = val
        return same_lv_q_val

    if kind == "forbidden_jewel":
        notable_notes = [str(n).lower() for n in (item_rec.get("notes") or [])]
        for ln in (stash.get("ForbiddenJewel") or []) + (dense.get("ForbiddenJewel") or []):
            if str(ln.get("name") or "").strip().lower() != name_low:
                continue
            var_low = str(ln.get("variant") or "").strip().lower()
            if var_low and any(var_low in nt or nt in var_low for nt in notable_notes):
                val = float(ln.get("chaosValue") or ln.get("chaos") or 0.0)
                if val > 0:
                    return val

    if kind in ("unique", "watchers_eye", "radius_jewel", "timeless_jewel", "forbidden_jewel"):
        want_links = int(item_rec.get("links") or 0)
        for ov in _NINJA_UNIQUE_CATS:
            rows = (stash.get(ov) or []) + (dense.get(ov) or [])
            exact_link_val: float | None = None
            base_val: float | None = None
            for ln in rows:
                if str(ln.get("name") or "").strip().lower() != name_low:
                    continue
                val = float(ln.get("chaosValue") or ln.get("chaos") or 0.0)
                if val <= 0:
                    continue
                ln_links = int(ln.get("links") or 0)
                if want_links >= 5 and ln_links == want_links:
                    exact_link_val = val
                    break
                if base_val is None or ln_links == 0:
                    base_val = val
            if exact_link_val is not None:
                return exact_link_val
            if base_val is not None:
                return base_val
    return None


def lookup_oil_chaos(
    oil_name: str,
    dense: dict[str, list[dict[str, Any]]],
    stash: dict[str, list[dict[str, Any]]],
) -> float | None:
    target = oil_name.strip().lower()
    for ln in (stash.get("Oil") or []) + (dense.get("Oil") or []):
        if str(ln.get("name") or "").strip().lower() == target:
            val = float(ln.get("chaosValue") or ln.get("chaos") or 0.0)
            if val > 0:
                return val
    return None


def price_anoint(
    anoint_obj: dict[str, Any] | None,
    dense: dict[str, list[dict[str, Any]]],
    stash: dict[str, list[dict[str, Any]]],
    div_rate: float,
) -> dict[str, Any] | None:
    if not isinstance(anoint_obj, dict) or not anoint_obj.get("notable"):
        return None
    oils = list(anoint_obj.get("oils") or ())
    oil_prices: list[dict[str, Any]] = []
    total_c = 0.0
    missing_any = False
    for oil in oils:
        c_val = lookup_oil_chaos(oil, dense, stash)
        if c_val is not None:
            total_c += c_val
        else:
            missing_any = True
        oil_prices.append({"oil": oil, "chaos": round(c_val, 2) if c_val is not None else None})
    all_priced = bool(oils) and not missing_any
    return {
        **anoint_obj,
        "oilPrices": oil_prices,
        "priceChaos": round(total_c, 2) if all_priced else None,
        "priceDivine": round(total_c / div_rate, 3) if (all_priced and div_rate > 0) else None,
    }
