"""4-part PoB inflation de-watering orchestration, probe state helpers, and rollback verification."""

from __future__ import annotations

import difflib
import math
from collections.abc import Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none
from ..snapshot import (
    _call_batch,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    verify_build,
)
from .constants import _KEEP_KEYWORDS
from .dewater_steps import (
    _dewater_config_and_custom_mods,
    _dewater_flasks,
    _dewater_skills,
)
from .dewater_steps import (
    _extract_probe_state as _extract_probe_state,
)
from .dewater_steps import (
    _rpc_error_text as _rpc_error_text,
)


def _normalize_keep(keep: Iterable[str] | str | None) -> set[str]:
    """Normalize user keep tokens (config vars, 'Flask N', 'flasks', gem names, 'customMods', 'party')."""
    if keep is None:
        return set()
    tokens: Iterable[str] = (keep,) if isinstance(keep, str) else keep
    out: set[str] = set()
    for tok in tokens:
        for part in str(tok).split(","):
            p = part.strip().lower()
            if not p:
                continue
            if "+" in p:
                for sub in p.split("+"):
                    s = sub.strip()
                    if s:
                        out.add(s)
            else:
                out.add(p)
    return out


def _unmatched_keep(keep_set: set[str], known: dict[str, str]) -> list[str]:
    """Keep tokens that name nothing in this build, with a close-match hint."""
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


def dewater_build_in_place(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    *,
    auto_keep_reasons: dict[str, str] | None = None,
    flask_notes: dict[str, str] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    auto_keep_live: bool = True,
) -> dict[str, list[str]]:
    """Strip PoB inflation ('water') in place and set enemyIsBoss='Pinnacle'."""
    stripped, _records = dewater_build_with_records(
        worker,
        keep=keep,
        auto_keep_reasons=auto_keep_reasons,
        flask_notes=flask_notes,
        equipped_slots_raw=equipped_slots_raw,
        auto_keep_live=auto_keep_live,
    )
    return stripped


def dewater_build_with_records(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    *,
    has_bloodsoaked_blade: bool | None = None,
    auto_keep_reasons: dict[str, str] | None = None,
    flask_notes: dict[str, str] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    auto_keep_live: bool = True,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """De-water in place (2 pipelined RPC batches) and return reversible per-item records."""
    keep_set = _normalize_keep(keep)
    stripped: dict[str, list[str]] = {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
        "kept": [],
        "autoKept": [],
        "keepRejected": [],
    }
    records: list[dict[str, Any]] = []
    known: dict[str, str] = {k: k for k in _KEEP_KEYWORDS}

    cfg_res, items_res, skills_res, party_res = _call_batch(
        worker,
        [
            ("list_config", {}),
            ("list_items", {}),
            ("list_skills", {}),
            ("get_party", {}),
        ],
    )

    mutations: list[tuple[str, dict[str, Any]]] = []
    _dewater_config_and_custom_mods(
        cfg_res,
        keep_set,
        known,
        stripped,
        mutations,
        records,
        auto_keep_reasons=auto_keep_reasons,
    )
    _dewater_flasks(
        worker,
        items_res,
        keep_set,
        known,
        stripped,
        mutations,
        records,
        has_bloodsoaked_blade=has_bloodsoaked_blade,
        flask_notes=flask_notes,
        equipped_slots_raw=equipped_slots_raw,
    )
    _dewater_skills(skills_res, keep_set, known, stripped, mutations, records)

    party_fields = party_res.get("fields", {})
    non_empty_party = [k for k, v in party_fields.items() if str(v or "").strip()]
    if non_empty_party or party_res.get("enableExportBuffs"):
        label = f"PartyTab fields ({', '.join(non_empty_party or ['enableExportBuffs'])})"
        if "party" in keep_set:
            stripped["kept"].append(f"{label} (kept)")
        else:
            stripped["party"].append(f"{label} -> cleared & disabled")
            mutations.append(("party_action", {"action": "disable"}))
            mutations.append(("party_action", {"action": "clear"}))

    if mutations:
        _call_batch(worker, mutations)

    ig_reason = (auto_keep_reasons or {}).get("conditionenemyignited") or ""
    if "IgniteChance" in ig_reason and any(
        s.lower().startswith("conditionenemyignited=") for s in stripped.get("autoKept", [])
    ):
        _revoke_flask_inflated_ignite(worker, cfg_res, stripped, records)

    if auto_keep_live:
        from .dewater_mechanics import refine_post_dewater_mechanics

        refine_post_dewater_mechanics(worker, cfg_res, keep_set, stripped, records, skills_res=skills_res)

    stripped["keepRejected"] = _unmatched_keep(keep_set, known)
    return stripped, records


def _revoke_flask_inflated_ignite(
    worker: BridgeWorker,
    cfg_res: dict[str, Any],
    stripped: dict[str, list[str]],
    records: list[dict[str, Any]],
) -> None:
    """Revoke `conditionEnemyIgnited` auto-keep if post-dewater `IgniteChance` fell below 100%."""
    ig_var = "conditionEnemyIgnited"
    ig_val: Any = True
    for sec in cfg_res.get("sections", []):
        for item in sec.get("items", []):
            if str(item.get("var") or "").lower() == "conditionenemyignited":
                ig_var = str(item["var"])
                ig_val = item.get("value")
                break
    try:
        st_batch = _call_batch(worker, [("get_stats", {})], raise_on_error=False)
    except Exception:
        return
    if not st_batch or not isinstance(st_batch[0], dict) or st_batch[0].get("_error"):
        return
    post_stats = st_batch[0].get("stats")
    if not isinstance(post_stats, dict):
        return
    post_ig = max(
        _finite_float(post_stats.get("IgniteChance")),
        _finite_float(post_stats.get("EnemyIgniteChance")),
    )
    if post_ig >= 100.0:
        return
    _call_batch(worker, [("reset_config", {"var": ig_var})])
    stripped["autoKept"] = [
        s for s in stripped.get("autoKept", []) if not s.lower().startswith("conditionenemyignited=")
    ]
    stripped["config"].append(f"{ig_var}={ig_val!r} -> reset (post-dewater IgniteChance {post_ig:.0f}% < 100%)")
    records.append(
        {
            "dimension": "config",
            "key": ig_var,
            "label": f"{ig_var}={ig_val!r}",
            "restore": [("set_config", {"var": ig_var, "value": ig_val})],
            "revert": [("reset_config", {"var": ig_var})],
            "probe": {"kind": "config", "var": ig_var},
            "original": ig_val,
        }
    )


def _probe_rpc_method(probe: dict[str, Any]) -> str:
    kind = probe.get("kind")
    if kind in ("config", "config_multi", "customMods"):
        return "list_config"
    if kind == "flask":
        return "list_items"
    if kind == "gem":
        return "list_skills"
    raise ValueError(f"unknown probe kind {kind!r}")


def _probe_state(worker: BridgeWorker, probe: dict[str, Any]) -> Any:
    """Read back from PoB the live state a record's restore/revert is meant to set."""
    method = _probe_rpc_method(probe)
    return _extract_probe_state(probe, worker.call(method, {}))


def _metric_val(raw: Any) -> float | str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    parsed = _parse_numeric_or_none(raw)
    if parsed is None or math.isnan(parsed):
        return None
    if math.isinf(parsed):
        return _json_safe_stat(parsed)
    return parsed


def _digest_metrics(digest: dict[str, Any], dps_key: str) -> dict[str, float | str | None]:
    stats = digest.get("stats", {}) if isinstance(digest, dict) else {}
    return {
        "dps": _metric_val(stats.get(dps_key)),
        "totalEhp": _metric_val(stats.get("TotalEHP")),
        "physMaxHit": _metric_val(stats.get("PhysicalMaximumHitTaken")),
        "eleMaxHit": _safe_min_ele_max_hit(stats),
    }


def verify_restored(
    worker: BridgeWorker,
    before_code: str,
    before_digest: dict[str, Any],
) -> dict[str, Any]:
    """Check that rolling back to ``before_code`` really reproduced the original build."""
    return verify_build(worker, before_digest, code=before_code or "")
