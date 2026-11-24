"""Official PoE1 profile import facade (International `pathofexile.com` + CN `poe.game.qq.com`, L1)."""

from __future__ import annotations

import base64
import zlib
from typing import Any

from ..bridge_client import BridgeError, BridgeWorker
from ..guardrails import validate_build_xml
from .cn_fetch import fetch_cn_character_bundle, fetch_cn_characters, get_env_cn_cookie
from .cn_zh2en import Zh2EnDictionary, translate_cn_bundle
from .pob_xml import character_bundle_to_pob_xml
from .site import (
    CN_PROFILE_RE,
    INTL_PROFILE_RE,
    POB_SITE_USER_AGENT,
    ProfileTarget,
    classify_http_status_error,
    fetch_intl_character_bundle,
    fetch_intl_characters,
    format_profile_markdown,
    import_intl_via_native_bridge,
    is_official_profile_url,
    normalize_account_name,
    parse_profile_source,
    resolve_target_character,
    sort_profile_characters,
    wrap_share_http_error,
)

__all__ = [
    "CN_PROFILE_RE",
    "INTL_PROFILE_RE",
    "POB_SITE_USER_AGENT",
    "ProfileTarget",
    "Zh2EnDictionary",
    "character_bundle_to_pob_xml",
    "classify_http_status_error",
    "fetch_cn_character_bundle",
    "fetch_cn_characters",
    "fetch_intl_character_bundle",
    "fetch_intl_characters",
    "fetch_profile_listing",
    "format_profile_markdown",
    "get_env_cn_cookie",
    "import_intl_via_native_bridge",
    "is_official_profile_url",
    "load_official_profile_into_worker",
    "normalize_account_name",
    "parse_profile_source",
    "resolve_target_character",
    "should_route_official_profile",
    "sort_profile_characters",
    "translate_cn_bundle",
    "wrap_share_http_error",
]


def should_route_official_profile(
    src: str,
    *,
    char_name: str | None = None,
    realm: str | None = None,
) -> bool:
    """Return True if `(src, char_name, realm)` should route through official profile import."""
    if is_official_profile_url(src):
        return True
    if realm and realm.strip().lower() == "cn":
        return True
    return bool(char_name and src and not src.startswith(("http://", "https://", "<")) and "#" in src)

_BANDIT_PANTHEON_NOTE = (
    "注意：官网角色导入接口不返回盗贼（Bandit）与万神殿（Pantheon）选择，分析前请确认配置。"
)


def _xml_to_share_code(xml_text: str) -> str:
    clean_xml = xml_text.lstrip("\ufeff")
    compressed = zlib.compress(clean_xml.encode("utf-8"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def fetch_profile_listing(
    source: str | None = None,
    *,
    realm: str | None = None,
    league: str | None = None,
    proxy: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Read-only official profile character listing (0 engine cold-start, supports Intl & CN)."""
    target = parse_profile_source(source, league=league, realm=realm)
    if target.server == "cn":
        resolved_acc, raw_chars = fetch_cn_characters(target.account_name or None, proxy=proxy, client=client)
        target = ProfileTarget(
            server="cn",
            account_name=resolved_acc,
            realm="cn",
            char_name=target.char_name,
            league=target.league,
            raw_source=target.raw_source or f"https://poe.game.qq.com/account/view-profile/{resolved_acc}",
        )
        used_cookie = True
    else:
        raw_chars, used_cookie = fetch_intl_characters(
            target.account_name, realm=target.realm, proxy=proxy, client=client
        )

    ordered = sort_profile_characters(raw_chars, league_filter=target.league)
    return {
        "server": target.server,
        "accountName": target.account_name,
        "realm": target.realm,
        "leagueFilter": target.league,
        "usedCookie": used_cookie,
        "count": len(ordered),
        "characters": ordered,
        "markdown": format_profile_markdown(target, ordered, used_cookie=used_cookie),
    }


def _import_english_bundle(
    worker: BridgeWorker,
    bundle: dict[str, Any],
    *,
    source_hint: str,
) -> tuple[str, dict[str, Any]]:
    """Import an English character bundle via POSIX `import_character_json` or pure-Python PoB XML."""
    if worker.caps.get("importCharacterJson"):
        worker.call("new_build", {}, timeout=10.0)
        res = worker.call(
            "import_character_json",
            {
                "char": bundle["char"],
                "passiveSkills": bundle["passiveSkills"],
                "items": bundle["items"],
                "deleteJewels": True,
                "clearItems": True,
                "clearSkills": True,
                "ignoreWeaponSwap": False,
            },
            timeout=30.0,
        )
        return "import_character_json", res

    xml_text = character_bundle_to_pob_xml(bundle, pob_root=getattr(worker, "pob_root", None))
    validate_build_xml(xml_text, expected_game="poe1", source_hint=source_hint)
    code = _xml_to_share_code(xml_text)
    res = worker.call("import_code", {"code": code, "mode": "replace"})
    return "pob_xml", res


def load_official_profile_into_worker(
    worker: BridgeWorker,
    source: str | None,
    *,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Import an international or CN official PoE1 character into `worker` (caller holds `worker.lock`)."""
    target = parse_profile_source(source, char_name=char_name, league=league, realm=realm)

    if target.server == "cn":
        resolved_acc, chars = fetch_cn_characters(target.account_name or None, proxy=proxy)
        target = ProfileTarget(
            server="cn",
            account_name=resolved_acc,
            realm="cn",
            char_name=target.char_name,
            league=target.league,
            raw_source=target.raw_source or f"https://poe.game.qq.com/account/view-profile/{resolved_acc}",
        )
        chosen = resolve_target_character(chars, target=target)
        cn_bundle = fetch_cn_character_bundle(resolved_acc, chosen, proxy=proxy)
        dictionary = Zh2EnDictionary.from_engine_dir(getattr(worker, "pob_root", None))
        en_bundle, translate_warnings = translate_cn_bundle(cn_bundle, dictionary)
        via, res = _import_english_bundle(worker, en_bundle, source_hint=target.raw_source)
        notes = [_BANDIT_PANTHEON_NOTE]
        if translate_warnings:
            notes.append(
                f"国服本地 3.30 词库反译完成，有 {len(translate_warnings)} 条未命中词缀（详见下方 [CN-TRANSLATE-WARN]），建议核对或用 `pob config set --append-mods` 补齐。"
            )
        return {
            "importMethod": f"official_profile_cn_{via}",
            "importResult": res,
            "character": {
                "accountName": resolved_acc,
                "name": chosen.get("name"),
                "league": chosen.get("league"),
                "class": chosen.get("class"),
                "level": chosen.get("level"),
                "server": "cn",
            },
            "importNotes": notes,
            "translateWarnings": translate_warnings,
        }

    # International official profile
    chars, used_cookie = fetch_intl_characters(target.account_name, realm=target.realm, proxy=proxy)
    chosen = resolve_target_character(chars, target=target)
    notes = [_BANDIT_PANTHEON_NOTE]

    if not used_cookie and not worker.caps.get("importCharacterJson"):
        try:
            res = import_intl_via_native_bridge(worker, target.account_name, chosen, realm=target.realm)
            return {
                "importMethod": "official_profile_site_native",
                "importResult": res,
                "character": {
                    "accountName": target.account_name,
                    "name": chosen.get("name"),
                    "league": chosen.get("league"),
                    "class": chosen.get("class"),
                    "level": chosen.get("level"),
                    "server": "intl",
                },
                "importNotes": notes,
                "translateWarnings": [],
            }
        except BridgeError:
            # Fall through cleanly to Python httpx JSON fetch + pure-Python XML import
            pass

    bundle = fetch_intl_character_bundle(
        target.account_name,
        chosen,
        realm=target.realm,
        proxy=proxy,
        use_cookie=used_cookie,
    )
    via, res = _import_english_bundle(worker, bundle, source_hint=target.raw_source)
    return {
        "importMethod": f"official_profile_intl_{via}",
        "importResult": res,
        "character": {
            "accountName": target.account_name,
            "name": chosen.get("name"),
            "league": chosen.get("league"),
            "class": chosen.get("class"),
            "level": chosen.get("level"),
            "server": "intl",
        },
        "importNotes": notes,
        "translateWarnings": [],
    }
