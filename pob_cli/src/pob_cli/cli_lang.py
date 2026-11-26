"""Display language (`zh-CN`/`zh-TW`/`en`) and trade realm (`global`/`tw`) Click command (`pob lang`)."""

from __future__ import annotations

import sys
from typing import Any

import click

from .cli_common import (
    DAEMON_FAILURE_EXIT_CODE,
    _emit_json,
    _emit_text,
    _output_options,
)


def _installed_locales(rt_dir: Any) -> list[str] | None:
    """Locales the engine on disk ships dictionaries for, or None when no engine is installed."""
    from .game_data.paths import list_available_zh_dirs
    from .updater import is_valid_engine_dir, resolve_engine_root

    try:
        eng = resolve_engine_root(runtime_dir=rt_dir)
        if not is_valid_engine_dir(eng):
            return None
        names = {d.name for d in list_available_zh_dirs(eng)}
    except Exception:
        return None
    return [lang for lang, sub in (("zh-CN", "zh-rCN"), ("zh-TW", "zh-rTW")) if sub in names] + ["en"]


_DEFAULT_INSTALLED_LOCALES = _installed_locales


def _resolve_installed_locales(rt_dir: Any) -> list[str] | None:
    """Honor monkeypatches on either `cli_lang._installed_locales` or `cli_lifecycle._installed_locales`."""
    if _installed_locales is not _DEFAULT_INSTALLED_LOCALES:
        return _installed_locales(rt_dir)
    lc_mod = sys.modules.get("pob_cli.cli_lifecycle")
    lc_fn = getattr(lc_mod, "_installed_locales", None) if lc_mod is not None else None
    if callable(lc_fn) and lc_fn is not _DEFAULT_INSTALLED_LOCALES:
        return lc_fn(rt_dir)
    return _installed_locales(rt_dir)


@click.command("lang")
@click.argument("language", required=False, default=None)
@click.option(
    "--realm",
    "trade_realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Set trade server realm (`global` or `tw` for pathofexile.tw); never auto-switched by zh-TW.",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_lang(
    language: str | None,
    trade_realm: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Get or set display language (`zh-CN`/`zh-TW`/`en`) and trade realm (`global`/`tw`)."""
    from .daemon import call_daemon, read_daemon_state
    from .locale_cfg import (
        TW_REALM_CONFIRM_PROMPT,
        get_active_lang,
        get_active_realm,
        invalid_env_lang,
        invalid_env_realm,
        normalize_lang,
        resolve_runtime_dir_l0,
        set_active_lang,
        set_active_realm,
        trade_base_url_for_realm,
    )

    rt_dir = resolve_runtime_dir_l0()
    installed = _resolve_installed_locales(rt_dir)

    persisted_info: dict[str, Any] | None = None
    persisted_realm: dict[str, Any] | None = None
    target_lang: str | None = None
    if language is not None and language.strip():
        try:
            target_lang = normalize_lang(language)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if installed is not None and target_lang not in installed:
            raise click.ClickException(
                f"Language {target_lang!r} is not installed in this engine (available: {', '.join(installed)})."
            )
        persisted_info = set_active_lang(target_lang, runtime_dir=rt_dir)
    if trade_realm is not None and trade_realm.strip():
        try:
            persisted_realm = set_active_realm(trade_realm, runtime_dir=rt_dir)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    eff_lang, eff_src = get_active_lang(rt_dir)
    eff_realm, realm_src = get_active_realm(rt_dir)
    trade_host = trade_base_url_for_realm(eff_realm)

    daemon_res: dict[str, Any] | None = None
    if read_daemon_state(rt_dir) is not None:
        try:
            daemon_res = call_daemon(
                "lang",
                {"lang": target_lang, "persist": False} if target_lang else {},
                auto_start=False,
            )
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            if "not running" not in detail.lower():
                saved = f"Saved `{target_lang}` to {persisted_info['configPath']}, but the" if persisted_info else "The"
                err = click.ClickException(
                    f"{saved} running daemon did not answer the language request: {detail}\n"
                    "Restart it with `pob stop` so the next command starts it in the new language."
                )
                err.exit_code = DAEMON_FAILURE_EXIT_CODE
                raise err from exc

    avail = installed or ["en"]
    if isinstance(daemon_res, dict) and isinstance(daemon_res.get("availableLocales"), list):
        avail = list(daemon_res["availableLocales"])

    raw_slots = daemon_res.get("slotsUpdated") if isinstance(daemon_res, dict) else None
    slots_updated = raw_slots if isinstance(raw_slots, dict) else {}
    slot_results = [v for v in slots_updated.values() if isinstance(v, dict)]
    failed_slots = {
        k: str(v.get("error") or "failed")
        for k, v in slots_updated.items()
        if isinstance(v, dict) and v.get("ok") is False
    }
    dict_flags = [bool(v["dictLoaded"]) for v in slot_results if "dictLoaded" in v]
    legacy_fallback = any(v.get("fallback") for v in slot_results)

    prompt_tw_realm = target_lang == "zh-TW" and eff_realm != "tw" and persisted_realm is None
    payload: dict[str, Any] = {
        "lang": eff_lang,
        "source": eff_src,
        "realm": eff_realm,
        "realmSource": realm_src,
        "tradeBaseUrl": trade_host,
        "availableLocales": avail,
        "daemonRunning": daemon_res is not None,
        "daemonSynced": daemon_res is not None and not failed_slots,
    }
    if isinstance(daemon_res, dict) and daemon_res.get("effectiveLang"):
        payload["daemonLang"] = daemon_res["effectiveLang"]
    if dict_flags:
        payload["dictLoaded"] = all(dict_flags)
    if persisted_info is not None:
        payload["persistedLang"] = persisted_info["lang"]
        payload["configPath"] = persisted_info["configPath"]
    if persisted_realm is not None:
        payload["persistedRealm"] = persisted_realm["realm"]
        payload.setdefault("configPath", persisted_realm["configPath"])
    if prompt_tw_realm:
        payload["twRealmPrompt"] = TW_REALM_CONFIRM_PROMPT
    if slots_updated:
        payload["slotsUpdated"] = slots_updated
    if failed_slots:
        payload["failedSlots"] = failed_slots
    if legacy_fallback:
        payload["legacyFallback"] = True
    env_bad = invalid_env_lang()
    if env_bad is not None:
        payload["envIgnored"] = env_bad
    env_bad_realm = invalid_env_realm()
    if env_bad_realm is not None:
        payload["envRealmIgnored"] = env_bad_realm
    if isinstance(daemon_res, dict) and daemon_res.get("daemonEnvOverridden"):
        payload["daemonEnvOverridden"] = daemon_res["daemonEnvOverridden"]

    if as_json:
        _emit_json(payload, compact=compact, out_path=out_path)
        return

    sync_label = "no daemon running (applies on next start)" if daemon_res is None else ("partial/failed" if failed_slots else "yes")
    msg = (
        f"Language: `{eff_lang}` (source: `{eff_src}`) | Available: `{', '.join(avail)}` | "
        f"Trade realm: `{eff_realm}` (`{trade_host}`, source: `{realm_src}`) | Daemon synced: `{sync_label}`"
    )
    if dict_flags:
        msg += f" | Dictionary loaded: `{'yes' if all(dict_flags) else 'no'}`"
    if failed_slots:
        err_summary = ", ".join(f"{s}: {e}" for s, e in sorted(failed_slots.items()))
        msg += f"\nWarning: Slot locale sync failed ({err_summary})."
    if legacy_fallback:
        msg += "\nNote: This engine cannot switch dictionaries live; it only toggled Chinese on or off."
    if payload.get("daemonLang") and payload["daemonLang"] != eff_lang:
        msg += f"\nNote: The daemon now shows `{payload['daemonLang']}`, while this shell resolves `{eff_lang}`."
    if persisted_info is not None and eff_lang != persisted_info["lang"]:
        msg += f"\nNote: Persisted `{persisted_info['lang']}`, but `POB_LANG={eff_lang}` in this shell overrides it."
    if persisted_realm is not None and eff_realm != persisted_realm["realm"]:
        msg += f"\nNote: Persisted realm `{persisted_realm['realm']}`, but `POB_REALM={eff_realm}` overrides it."
    if env_bad is not None:
        msg += f"\nNote: Ignored invalid `POB_LANG={env_bad}`."
    if env_bad_realm is not None:
        msg += f"\nNote: Ignored invalid `POB_REALM={env_bad_realm}`."
    if prompt_tw_realm:
        msg += f"\n{TW_REALM_CONFIRM_PROMPT}"
    _emit_text(msg, out_path=out_path)
