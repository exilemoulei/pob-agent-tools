"""ConfigTab variable mutations and customMods editing (`modify_config`)."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from .common import _BatchCache, _call_batch

_ENEMY_IS_BOSS_ALIASES: dict[str, str] = {
    "shaper": "Pinnacle",
    "sirus": "Pinnacle",
    "uber atziri": "Boss",
    "atziri": "Boss",
}


def _coerce_config_value(raw_val: Any) -> Any:
    if isinstance(raw_val, (bool, int, float)):
        return raw_val
    s = str(raw_val).strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _split_custom_mod_lines(raw_text: str) -> list[str]:
    """Split multiline or semicolon-separated custom mod text into clean non-empty lines."""
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        for part in raw_line.split(";"):
            s = part.strip()
            if s:
                lines.append(s)
    return lines


def _apply_config_in_place(
    w: BridgeWorker,
    assignments: list[tuple[str, Any]],
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
    *,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    applied_vars: list[dict[str, Any]] = []
    touched_boss = False

    for raw_var, _ in assignments:
        check_input(str(raw_var), scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if custom_mods is not None and custom_mods.strip().lower() not in (
        "clear",
        "reset",
        "none",
        "disable",
        "disabled",
        "",
    ):
        check_input(custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if append_custom_mods is not None:
        check_input(append_custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    known_items: dict[str, dict[str, Any]] = {}
    if assignments:
        if cache is not None:
            known_items = cache.get_known_config_items()
        else:
            cfg_meta = w.call("list_config", {})
            for sec in cfg_meta.get("sections", []):
                for item in sec.get("items", []):
                    vname = item.get("var")
                    if vname:
                        known_items[str(vname).lower()] = item

    if assignments:
        rpc_calls: list[tuple[str, dict[str, Any]]] = []
        meta_rows: list[tuple[str, str, Any]] = []
        for raw_var, raw_val in assignments:
            item = known_items.get(str(raw_var).strip().lower(), {})
            var = str(item.get("var") or str(raw_var).strip())
            is_list = item.get("type") == "list"
            list_options = item.get("list") or []
            none_is_option = is_list and any(
                (opt.get("val") is not None and str(opt.get("val")).strip().lower() == "none")
                or strip_pob_escapes(str(opt.get("label") or "")).strip().lower() == "none"
                for opt in list_options
                if isinstance(opt, dict)
            )
            v_str = "" if raw_val is None else str(raw_val).strip()
            v_low = v_str.lower()
            if (
                raw_val is None
                or v_low in ("reset", "null", "nil", "default", "__unset__")
                or (v_low == "none" and not none_is_option)
            ):
                rpc_calls.append(("reset_config", {"var": var}))
                meta_rows.append((var, "reset", None))
            else:
                matched_list_val = False
                coerced: Any = None
                if is_list and not isinstance(raw_val, bool):
                    lookup_str = _ENEMY_IS_BOSS_ALIASES.get(v_low, v_str) if var == "enemyIsBoss" else v_str
                    lookup_low = lookup_str.strip().lower()
                    for opt in list_options:
                        if not isinstance(opt, dict) or "val" not in opt:
                            continue
                        opt_val_str = str(opt.get("val")).strip().lower()
                        opt_lbl = strip_pob_escapes(str(opt.get("label") or "")).strip().lower()
                        opt_lbl_zh = strip_pob_escapes(str(opt.get("labelZh") or "")).strip().lower()
                        if (
                            lookup_low == opt_val_str
                            or (opt_lbl and lookup_low == opt_lbl)
                            or (opt_lbl_zh and lookup_low == opt_lbl_zh)
                        ):
                            coerced = opt["val"]
                            matched_list_val = True
                            break
                    if not matched_list_val and var == "enemyIsBoss" and v_low in _ENEMY_IS_BOSS_ALIASES:
                        coerced = _ENEMY_IS_BOSS_ALIASES[v_low]
                        matched_list_val = True
                if not matched_list_val:
                    coerced = _coerce_config_value(raw_val)
                    if var == "enemyIsBoss" and isinstance(coerced, str):
                        coerced = _ENEMY_IS_BOSS_ALIASES.get(coerced.lower(), coerced)
                if item.get("type") in ("count", "countAllowZero", "integer", "float") and isinstance(coerced, bool):
                    if coerced:
                        coerced = 1
                        rpc_calls.append(("set_config", {"var": var, "value": coerced}))
                        meta_rows.append((var, "set", coerced))
                    else:
                        rpc_calls.append(("reset_config", {"var": var}))
                        meta_rows.append((var, "reset", None))
                else:
                    rpc_calls.append(("set_config", {"var": var, "value": coerced}))
                    meta_rows.append((var, "set", coerced))
            if var in ("enemyIsBoss", "presetBossSkills"):
                touched_boss = True

        results = _call_batch(w, rpc_calls) if len(rpc_calls) > 1 else [w.call(rpc_calls[0][0], rpc_calls[0][1])]
        for (var, act, val), res in zip(meta_rows, results, strict=False):
            if act == "reset":
                applied_vars.append({"var": var, "action": "reset", "result": res})
            else:
                applied_vars.append({"var": var, "action": "set", "value": val, "result": res})
        if cache is not None:
            cache.invalidate("config")

    if touched_boss:
        cfg = w.call("list_config", {})
        if cache is not None:
            cache.config = cfg
            cache._index_config_vars(cfg)
        for sec in cfg.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == "presetBossSkills" and item.get("value") not in (None, "", "None"):
                    w.call("set_config", {"var": "presetBossSkills", "value": item["value"]})
                    if cache is not None:
                        cache.invalidate("config")
                    break

    if custom_mods is not None:
        cm_clean = custom_mods.strip()
        if cm_clean.lower() in ("clear", "reset", "none", "disable", "disabled", ""):
            w.call("set_custom_mods", {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]})
            applied_vars.append({"var": "customMods", "action": "clear"})
        else:
            w.call("set_custom_mods", {"list": [{"title": "CLI Custom Mods", "text": cm_clean, "enabled": True}]})
            applied_vars.append({"var": "customMods", "action": "set", "value": cm_clean})
        if cache is not None:
            cache.invalidate("config", "skills")

    if append_custom_mods is not None:
        new_lines = _split_custom_mod_lines(append_custom_mods)
        if not new_lines:
            raise ValueError("append_custom_mods cannot be empty.")

        cfg_cur = cache.get_config() if cache is not None else w.call("list_config", {})
        existing_blocks = list(cfg_cur.get("customMods") or [])
        updated_blocks: list[dict[str, Any]] = []
        target_idx: int | None = None

        for blk in existing_blocks:
            title = blk.get("title") or "Default"
            enabled = blk.get("enabled", True) is not False
            raw_blk_text = str(blk.get("text") or "")
            cleaned_lines = [ln for ln in raw_blk_text.splitlines() if ln.strip().lower() != "# disabled"]
            cleaned_text = "\n".join(cleaned_lines).strip()
            updated_blocks.append({"title": title, "text": cleaned_text, "enabled": enabled})
            if enabled:
                target_idx = len(updated_blocks) - 1

        append_str = "\n".join(new_lines)
        if target_idx is None:
            updated_blocks.append({"title": "Default", "text": append_str, "enabled": True})
        else:
            prev_text = updated_blocks[target_idx]["text"]
            updated_blocks[target_idx]["text"] = f"{prev_text}\n{append_str}" if prev_text else append_str

        w.call("set_custom_mods", {"list": updated_blocks})
        if cache is not None:
            cache.invalidate("config", "skills")
        combined_text = "\n".join(b["text"] for b in updated_blocks if b.get("enabled") and b.get("text"))
        applied_vars.append(
            {
                "var": "customMods",
                "action": "append",
                "appendedLines": new_lines,
                "customModsText": combined_text,
            }
        )

    return {"assignments": applied_vars}


def modify_config(
    worker: BridgeWorker,
    *,
    assignments: dict[str, Any] | list[tuple[str, Any]] | None = None,
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Set/reset PoB ConfigTab variables and/or replace or append `customMods` (0-residue by default)."""
    pairs: list[tuple[str, Any]] = []
    if isinstance(assignments, dict):
        pairs = list(assignments.items())
    elif assignments:
        pairs = [(k, v) for k, v in assignments]

    if not pairs and custom_mods is None and append_custom_mods is None:
        raise ValueError("No config assignments, custom_mods, or append_custom_mods provided.")

    def _do_cfg(w: BridgeWorker) -> dict[str, Any]:
        return _apply_config_in_place(
            w, pairs, custom_mods=custom_mods, append_custom_mods=append_custom_mods
        )

    return run_mutation(worker, _do_cfg, apply=apply)
