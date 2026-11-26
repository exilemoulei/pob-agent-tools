"""L0 locale configuration and runtime config.json persistence (`zh-CN`, `zh-TW`, `en`)."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

SUPPORTED_LANGS: tuple[str, ...] = ("zh-CN", "zh-TW", "en")
DEFAULT_LANG: str = "zh-CN"
SUPPORTED_REALMS: tuple[str, ...] = ("global", "tw")
DEFAULT_REALM: str = "global"

REALM_TRADE_BASE_URLS: dict[str, str] = {
    "global": "https://www.pathofexile.com",
    "tw": "https://pathofexile.tw",
}

_REALM_ALIASES: dict[str, str] = {
    "global": "global",
    "intl": "global",
    "international": "global",
    "ggg": "global",
    "pc": "global",
    "en": "global",
    "tw": "tw",
    "taiwan": "tw",
    "zh-tw": "tw",
    "zh_tw": "tw",
    "hk": "tw",
    "mo": "tw",
    "pathofexile.tw": "tw",
}

TW_REALM_CONFIRM_PROMPT: str = (
    "你玩的是台服吗？請問您遊玩的是台港澳服（pathofexile.tw）嗎？"
    "若是，請確認後執行 `pob lang --realm tw` 開啟台服市集；國際服繁中玩家無需切換。"
)

_LANG_ALIASES: dict[str, str] = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-rcn": "zh-CN",
    "zh_rcn": "zh-CN",
    "cn": "zh-CN",
    "chs": "zh-CN",
    "simplified": "zh-CN",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "zh-rtw": "zh-TW",
    "zh_rtw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh_hk": "zh-TW",
    "tw": "zh-TW",
    "cht": "zh-TW",
    "traditional": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "en_gb": "en",
    "english": "en",
}

_DATA_SUBDIRS: dict[str, str | None] = {
    "zh-CN": "zh-rCN",
    "zh-TW": "zh-rTW",
    "en": None,
}

__all__ = [
    "DEFAULT_LANG",
    "DEFAULT_REALM",
    "REALM_TRADE_BASE_URLS",
    "SUPPORTED_LANGS",
    "SUPPORTED_REALMS",
    "TW_REALM_CONFIRM_PROMPT",
    "_has_runtime_data_l0",
    "current_lang",
    "current_realm",
    "get_active_lang",
    "get_active_realm",
    "invalid_env_lang",
    "invalid_env_realm",
    "lang_to_data_subdir",
    "normalize_lang",
    "normalize_realm",
    "resolve_runtime_dir_l0",
    "set_active_lang",
    "set_active_realm",
    "trade_base_url_for_realm",
]


def normalize_lang(val: str | None) -> str:
    """Normalize a locale identifier to one of `('zh-CN', 'zh-TW', 'en')`."""
    if val is None:
        raise ValueError(f"Unsupported language {val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}")
    cleaned = str(val).strip().lower()
    mapped = _LANG_ALIASES.get(cleaned)
    if mapped is None:
        raise ValueError(f"Unsupported language {val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}")
    return mapped


def lang_to_data_subdir(lang: str) -> str | None:
    """Map normalized locale to `Data/poe1/<subdir>` (`'zh-rCN'`, `'zh-rTW'`, or `None` for `'en'`)."""
    norm = normalize_lang(lang)
    return _DATA_SUBDIRS[norm]


def _detect_checkout_root_l0() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    try:
        parents = Path(__file__).resolve().parents
        if len(parents) > 3:
            repo_root = parents[3]
            if (repo_root / "pob_cli" / "pyproject.toml").is_file():
                return repo_root
    except OSError:
        return None
    return None


def _has_runtime_data_l0(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        for marker in ("engine", "daemon.json", "update_state.json", "cache"):
            if (path / marker).exists():
                return True
        return any(path.glob("slot_*.json"))
    except OSError:
        return False


def resolve_runtime_dir_l0(runtime_dir: Path | None = None) -> Path:
    """L0 stdlib-only runtime directory resolver matching `updater.paths.get_runtime_dir` 100%."""
    if runtime_dir is not None:
        return Path(runtime_dir)

    env_rt = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if env_rt:
        return Path(env_rt)

    env_home = os.environ.get("POB_HOME", "").strip()
    if env_home:
        return Path(env_home) / ".runtime"

    checkout_root = _detect_checkout_root_l0()
    if checkout_root is not None:
        return checkout_root / ".runtime"

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            primary = Path(local_app_data) / "pob4agents"
            legacy = Path(local_app_data) / "pob-agent-tools"
        else:
            primary = Path.home() / ".pob4agents"
            legacy = Path.home() / ".pob-agent-tools"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            primary = Path(xdg_data) / "pob4agents"
            legacy = Path(xdg_data) / "pob-agent-tools"
        else:
            primary = Path.home() / ".local" / "share" / "pob4agents"
            legacy = Path.home() / ".local" / "share" / "pob-agent-tools"

    if not _has_runtime_data_l0(primary) and _has_runtime_data_l0(legacy):
        return legacy
    return primary


_WARNED_ENV_VALUES: set[str] = set()


def invalid_env_lang() -> str | None:
    """Return the raw `POB_LANG` value when it is set but is not a supported locale, else None."""
    env_val = os.environ.get("POB_LANG", "").strip()
    if not env_val:
        return None
    try:
        normalize_lang(env_val)
    except ValueError:
        return env_val
    return None


def get_active_lang(runtime_dir: Path | None = None) -> tuple[str, str]:
    """Return `(lang, source)` where `source` is `'env'`, `'config'`, or `'default'`."""
    env_val = os.environ.get("POB_LANG", "").strip()
    if env_val:
        try:
            return normalize_lang(env_val), "env"
        except ValueError:
            # A typo such as `zh-Hant` would otherwise switch languages with no trace.
            if env_val not in _WARNED_ENV_VALUES:
                _WARNED_ENV_VALUES.add(env_val)
                print(
                    f"Warning: ignoring POB_LANG={env_val!r}; valid choices: {', '.join(SUPPORTED_LANGS)}.",
                    file=sys.stderr,
                )

    cfg_path = resolve_runtime_dir_l0(runtime_dir) / "config.json"
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("lang") is not None:
                return normalize_lang(str(raw["lang"])), "config"
        except (OSError, ValueError, TypeError):
            pass

    return DEFAULT_LANG, "default"


def current_lang(runtime_dir: Path | None = None) -> str:
    """Return the active normalized locale (`'zh-CN'`, `'zh-TW'`, or `'en'`)."""
    return get_active_lang(runtime_dir)[0]


def set_active_lang(lang: str, runtime_dir: Path | None = None) -> dict[str, Any]:
    """Normalize `lang`, atomically persist `<runtime_dir>/config.json`, and return status dict."""
    norm = normalize_lang(lang)
    rt_dir = resolve_runtime_dir_l0(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = rt_dir / "config.json"

    payload: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = dict(existing)
        except (OSError, ValueError, TypeError):
            payload = {}

    payload["lang"] = norm
    tmp_path = cfg_path.with_name(f".config.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, cfg_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return {"lang": norm, "configPath": str(cfg_path)}


_WARNED_ENV_REALMS: set[str] = set()


def normalize_realm(val: str | None) -> str:
    """Normalize a trade realm identifier to `'global'` or `'tw'`."""
    if val is None:
        raise ValueError(f"Unsupported trade realm {val!r}; valid choices: {', '.join(SUPPORTED_REALMS)}")
    cleaned = str(val).strip().lower()
    mapped = _REALM_ALIASES.get(cleaned)
    if mapped is None:
        raise ValueError(f"Unsupported trade realm {val!r}; valid choices: {', '.join(SUPPORTED_REALMS)}")
    return mapped


def trade_base_url_for_realm(realm: str | None = None) -> str:
    """Return official trade host URL (`https://www.pathofexile.com` or `https://pathofexile.tw`)."""
    norm = normalize_realm(realm) if realm is not None else DEFAULT_REALM
    return REALM_TRADE_BASE_URLS[norm]


def invalid_env_realm() -> str | None:
    """Return raw `POB_REALM` when set to an unsupported value, else None."""
    env_val = os.environ.get("POB_REALM", "").strip()
    if not env_val:
        return None
    try:
        normalize_realm(env_val)
    except ValueError:
        return env_val
    return None


def get_active_realm(
    runtime_dir: Path | None = None,
    *,
    cli_realm: str | None = None,
) -> tuple[str, str]:
    """Return `(realm, source)` where precedence is CLI `--realm` > `POB_REALM` > `config.json` > `'global'`."""
    if cli_realm is not None and str(cli_realm).strip():
        return normalize_realm(cli_realm), "cli"

    env_val = os.environ.get("POB_REALM", "").strip()
    if env_val:
        try:
            return normalize_realm(env_val), "env"
        except ValueError:
            if env_val not in _WARNED_ENV_REALMS:
                _WARNED_ENV_REALMS.add(env_val)
                print(
                    f"Warning: ignoring POB_REALM={env_val!r}; valid choices: {', '.join(SUPPORTED_REALMS)}.",
                    file=sys.stderr,
                )

    cfg_path = resolve_runtime_dir_l0(runtime_dir) / "config.json"
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("realm") is not None:
                return normalize_realm(str(raw["realm"])), "config"
        except (OSError, ValueError, TypeError):
            pass

    return DEFAULT_REALM, "default"


def current_realm(runtime_dir: Path | None = None, *, cli_realm: str | None = None) -> str:
    """Return active normalized trade realm (`'global'` or `'tw'`)."""
    return get_active_realm(runtime_dir, cli_realm=cli_realm)[0]


def set_active_realm(realm: str, runtime_dir: Path | None = None) -> dict[str, Any]:
    """Normalize `realm`, atomically persist `<runtime_dir>/config.json["realm"]`, and return status dict."""
    norm = normalize_realm(realm)
    rt_dir = resolve_runtime_dir_l0(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = rt_dir / "config.json"

    payload: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = dict(existing)
        except (OSError, ValueError, TypeError):
            payload = {}

    payload["realm"] = norm
    tmp_path = cfg_path.with_name(f".config.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, cfg_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return {"realm": norm, "tradeBaseUrl": REALM_TRADE_BASE_URLS[norm], "configPath": str(cfg_path)}
