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
    _json_safe_stat,
    _safe_min_ele_max_hit,
    capture_digest,
)
from .constants import _KEEP_KEYWORDS
from .dewater_steps import (
    _dewater_config_and_custom_mods,
    _dewater_flasks,
    _dewater_skills,
    _normalize_custom_mod_blocks,
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
            if p:
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
) -> dict[str, list[str]]:
    """Strip PoB inflation ('water') in place and set enemyIsBoss='Pinnacle'."""
    stripped, _records = dewater_build_with_records(worker, keep=keep)
    return stripped


def dewater_build_with_records(
    worker: BridgeWorker,
    keep: Iterable[str] | str | None = None,
    *,
    has_bloodsoaked_blade: bool | None = None,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """De-water in place (2 pipelined RPC batches) and return reversible per-item records."""
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
    _dewater_config_and_custom_mods(cfg_res, keep_set, known, stripped, mutations, records)
    _dewater_flasks(
        worker,
        items_res,
        keep_set,
        known,
        stripped,
        mutations,
        records,
        has_bloodsoaked_blade=has_bloodsoaked_blade,
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

    stripped["keepRejected"] = _unmatched_keep(keep_set, known)
    return stripped, records


def _probe_rpc_method(probe: dict[str, Any]) -> str:
    kind = probe.get("kind")
    if kind in ("config", "customMods"):
        return "list_config"
    if kind == "flask":
        return "list_items"
    if kind == "gem":
        return "list_skills"
    raise ValueError(f"unknown probe kind {kind!r}")


def _extract_probe_state(probe: dict[str, Any], res: dict[str, Any]) -> Any:
    kind = probe.get("kind")
    if kind == "customMods":
        return _normalize_custom_mod_blocks(res.get("customMods", []))
    if kind == "config":
        for sec in res.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == probe["var"]:
                    return item.get("value")
        return None
    if kind == "flask":
        for slot in res.get("slots", []):
            if slot.get("name") == probe["slot"]:
                return bool(slot.get("active"))
        return None
    if kind == "gem":
        for group in res.get("groups", []):
            if group.get("index") != probe["group"]:
                continue
            for gem in group.get("gems", []):
                if gem.get("index") == probe["index"]:
                    state: dict[str, Any] = {}
                    for f in probe["fields"]:
                        if f == "mainActiveSkill":
                            state[f] = int(group.get("mainActiveSkill") or 1)
                        else:
                            state[f] = bool(gem.get(f, f == "enabled"))
                    return state
        return None
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


def _parse_stat_for_compare(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return _parse_numeric_or_none(val)


def verify_restored(
    worker: BridgeWorker,
    before_code: str,
    before_digest: dict[str, Any],
) -> dict[str, Any]:
    """Check that rolling back to ``before_code`` really reproduced the original build."""
    after_code = worker.call("export_code", {}).get("code")
    if before_code and after_code == before_code:
        return {"verified": True, "detail": "restored build exports identical code"}
    after = capture_digest(worker).get("stats") or {}
    before = (before_digest.get("stats") or {}) if isinstance(before_digest, dict) else {}
    differing: list[str] = []
    for k in set(before.keys()) | set(after.keys()):
        b = _parse_stat_for_compare(before.get(k))
        a = _parse_stat_for_compare(after.get(k))
        if b is None and a is None:
            continue
        if b is None or a is None:
            differing.append(k)
            continue
        if math.isnan(b) or math.isnan(a):
            if not (math.isnan(b) and math.isnan(a)):
                differing.append(k)
            continue
        if math.isinf(b) or math.isinf(a):
            if a != b:
                differing.append(k)
            continue
        if abs(a - b) > 1e-6 * max(1.0, abs(b)):
            differing.append(k)
    if differing:
        differing.sort()
        return {
            "verified": False,
            "detail": f"{len(differing)} stat(s) differ from before the run (e.g. {', '.join(differing[:5])})",
            "differing": differing,
        }
    return {"verified": True, "detail": "export code differs, but every stat recalculates identical"}


def _rpc_error_text(res: Any) -> str | None:
    if not isinstance(res, dict):
        return None
    if res.get("ok") is False or res.get("_error"):
        return str(res.get("error") or res.get("_error") or "RPC failed")
    return None
