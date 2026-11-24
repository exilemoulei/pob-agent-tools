"""International official PoE1 profile URL parsing, character listing, disambiguation, and import (L1)."""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from ..bridge_client import BridgeError, BridgeWorker
from ..errors import DaemonRequestError
from ..guardrails import validate_url

POB_SITE_USER_AGENT = "Path of Building/2.68.1"
INTL_HOST = "https://www.pathofexile.com"
CN_HOST = "https://poe.game.qq.com"

INTL_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?pathofexile\.com/account/view-profile/(?P<account>[^/?#]+)(?:/characters)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
CN_PROFILE_RE = re.compile(
    r"^https?://poe\.game\.qq\.com/account/view-profile/(?P<account>[^/?#]+)(?:/characters)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_REALM_BRIDGE_ID: dict[str, str] = {"pc": "PC", "xbox": "XBOX", "sony": "SONY"}
_PERMANENT_LEAGUES = frozenset(
    {
        "standard",
        "hardcore",
        "ssf standard",
        "ssf hardcore",
        "solo self-found",
        "hardcore solo self-found",
        "ruthless",
        "hc ruthless",
        "ssf ruthless",
        "hc ssf ruthless",
    }
)


@dataclass(frozen=True)
class ProfileTarget:
    server: str  # "intl" | "cn"
    account_name: str  # Normalized "Name#1234" or "" when cn auto-profile
    realm: str  # "pc" | "xbox" | "sony" | "cn"
    char_name: str | None = None
    league: str | None = None
    raw_source: str = ""


def normalize_account_name(raw: str) -> str:
    """Normalize URL/user account identifier (`Name-1234` or `Name%231234` -> `Name#1234`)."""
    decoded = urllib.parse.unquote(str(raw or "")).strip()
    if not decoded:
        return ""
    if "#" in decoded:
        return decoded
    return re.sub(r"-(\d{4})$", r"#\1", decoded)


def is_official_profile_url(source: str) -> bool:
    """Return True if `source` is an international or CN official PoE1 view-profile URL."""
    s = str(source or "").strip()
    return bool(INTL_PROFILE_RE.match(s) or CN_PROFILE_RE.match(s))


def parse_profile_source(
    source: str | None,
    *,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
) -> ProfileTarget:
    """Parse an official profile URL or bare account name into a `ProfileTarget`."""
    raw = str(source or "").strip()
    if raw.startswith(("http://", "https://")):
        validate_url(raw, expected_game="poe1", cli_subcommand="load")

    eff_realm = (realm or "").strip().lower()
    qs_char: str | None = None
    qs_realm: str | None = None
    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        qs_char = (qs.get("characterName") or qs.get("character") or [None])[0]
        qs_realm = (qs.get("realm") or [None])[0]
        if qs_char:
            qs_char = urllib.parse.unquote(qs_char).strip() or None

    eff_char = (char_name or "").strip() or qs_char
    eff_league = (league or "").strip() or None

    m_cn = CN_PROFILE_RE.match(raw)
    if m_cn or eff_realm == "cn" or raw.lower().startswith("https://poe.game.qq.com"):
        acc = normalize_account_name(m_cn.group("account")) if m_cn else normalize_account_name(raw)
        if raw.lower().startswith("https://poe.game.qq.com") and not m_cn:
            acc = ""
        return ProfileTarget(
            server="cn",
            account_name=acc,
            realm="cn",
            char_name=eff_char,
            league=eff_league,
            raw_source=raw or acc,
        )

    m_intl = INTL_PROFILE_RE.match(raw)
    if m_intl:
        acc = normalize_account_name(m_intl.group("account"))
        resolved_realm = (eff_realm or (qs_realm or "pc")).lower()
        if resolved_realm not in _REALM_BRIDGE_ID:
            raise DaemonRequestError(f"Unsupported realm '{resolved_realm}'. Expected one of: pc, xbox, sony, cn.")
        return ProfileTarget(
            server="intl",
            account_name=acc,
            realm=resolved_realm,
            char_name=eff_char,
            league=eff_league,
            raw_source=raw,
        )

    resolved_realm = (eff_realm or "pc").lower()
    if resolved_realm not in _REALM_BRIDGE_ID:
        raise DaemonRequestError(f"Unsupported realm '{resolved_realm}'. Expected one of: pc, xbox, sony, cn.")
    acc = normalize_account_name(raw)
    if not acc:
        raise DaemonRequestError(
            "Provide an official profile URL (e.g. `https://www.pathofexile.com/account/view-profile/Name-1234`), "
            "an account name (`Name#1234`), or `--realm cn` (with `POE_CN_SESSID` set in environment)."
        )
    return ProfileTarget(
        server="intl",
        account_name=acc,
        realm=resolved_realm,
        char_name=eff_char,
        league=eff_league,
        raw_source=raw,
    )


def _normalize_cookie_header(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    val = raw.strip()
    return val if "=" in val else f"POESESSID={val}"


def _env_intl_cookie() -> str | None:
    for key in ("POE_COOKIE", "POESESSID"):
        c = _normalize_cookie_header(os.environ.get(key))
        if c:
            return c
    return None


def _resolve_proxy(proxy: str | None = None) -> str | None:
    if proxy and proxy.strip():
        return proxy.strip()
    for key in ("POB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    return None


def make_site_client(
    *,
    proxy: str | None = None,
    cookie: str | None = None,
    timeout: float = 15.0,
) -> httpx.Client:
    """Create an httpx.Client configured with PoB's standard User-Agent and optional proxy/cookie."""
    headers: dict[str, str] = {
        "User-Agent": POB_SITE_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=5.0),
        "follow_redirects": True,
        "headers": headers,
    }
    resolved_proxy = _resolve_proxy(proxy)
    if resolved_proxy:
        kwargs["proxy"] = resolved_proxy
    return httpx.Client(**kwargs)


def classify_http_status_error(
    status_code: int,
    *,
    url: str,
    server: str = "intl",
    account_name: str = "",
    retry_after: str | None = None,
) -> DaemonRequestError:
    """Convert upstream HTTP status codes into clear, actionable exit-1 `DaemonRequestError`."""
    if status_code in (401, 403):
        if server == "cn":
            return DaemonRequestError(
                f"国服官网返回 HTTP {status_code}（未登录或登录态失效）。\n"
                "请在环境变量中设置 `POE_CN_SESSID`（或 `POE_CN_COOKIE`）后重试；"
                "严禁在命令行参数、终端打印或报告中输出 Cookie 明文。"
            )
        return DaemonRequestError(
            f"PoE 官网返回 HTTP {status_code}（资料设为私密或触发 Cloudflare 拦截）：{url}\n"
            "处理方式：\n"
            "1. 若为本人账号，建议在 pathofexile.com 隐私设置取消勾选「隐藏角色列表」（公开资料无需 Cookie）。\n"
            "2. 若需导入私密账号，请在环境变量设置 `POESESSID`（或 `POE_COOKIE`）后重试；"
            "严禁通过命令行传参或在终端打印 `POESESSID`。"
        )
    if status_code == 404:
        hint = (
            f"账号 `{account_name}` 不存在或缺少四位数字后缀（新版 GGG 账号需带 `#1234`，例如 `Name#1234` 或 `view-profile/Name-1234`）。"
            if account_name
            else f"目标页面不存在（HTTP 404）：{url}"
        )
        return DaemonRequestError(hint)
    if status_code == 429:
        wait_note = f"约 {retry_after}s 后" if retry_after else "稍后"
        return DaemonRequestError(f"PoE 官网触发限流（HTTP 429），请等待{wait_note}再重试。")
    return DaemonRequestError(f"PoE 官网请求失败（HTTP {status_code}）：{url}")


def wrap_share_http_error(exc: httpx.HTTPError, url: str) -> DaemonRequestError:
    """Turn any httpx error during share/profile URL fetch into an exit-1 `DaemonRequestError`."""
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status_error(
            exc.response.status_code,
            url=url,
            retry_after=exc.response.headers.get("Retry-After"),
        )
    return DaemonRequestError(f"拉取构筑链接失败（{url}）：{exc}（如需代理请设置 POB_PROXY / HTTPS_PROXY）。")


def sort_profile_characters(
    chars: list[dict[str, Any]],
    *,
    league_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Sort characters: current challenge leagues first, then level desc, then experience desc."""
    filtered = [c for c in chars if isinstance(c, dict) and c.get("name")]
    if league_filter:
        lf = league_filter.strip().lower()
        exact = [c for c in filtered if str(c.get("league") or "").strip().lower() == lf]
        if exact:
            filtered = exact
        else:
            partial = [c for c in filtered if lf in str(c.get("league") or "").strip().lower()]
            if partial:
                filtered = partial

    def _sort_key(c: dict[str, Any]) -> tuple[int, int, int, str]:
        lg = str(c.get("league") or "").strip().lower()
        is_perm = 1 if (not lg or lg in _PERMANENT_LEAGUES) else 0
        level = int(c.get("level") or 0)
        exp = int(c.get("experience") or 0)
        last_login = int(c.get("lastLoginTime") or 0)
        return (is_perm, -level, -(last_login or exp), str(c.get("name") or "").lower())

    return sorted(filtered, key=_sort_key)


def format_profile_markdown(
    target: ProfileTarget,
    chars: list[dict[str, Any]],
    *,
    limit: int = 20,
    used_cookie: bool = False,
) -> str:
    """Format character list as compact Markdown with a copy-pasteable `pob diagnose` command."""
    server_label = "CN (poe.game.qq.com)" if target.server == "cn" else f"Intl ({target.realm.upper()})"
    auth_label = "env cookie" if used_cookie else "public (no cookie)"
    lines = [
        f"### PoE1 Official Profile: `{target.account_name or '(current CN session)'}` ({server_label} | {auth_label})",
        f"Found **{len(chars)}** character(s) (challenge leagues listed first):",
        "",
        "| # | Character | League | Class | Level |",
        "|---:|---|---|---|---:|",
    ]
    for idx, c in enumerate(chars[:limit], start=1):
        lines.append(
            f"| {idx} | `{c.get('name', '-')}` | {c.get('league') or '-'} | {c.get('class') or '-'} | {c.get('level') or '-'} |"
        )
    if len(chars) > limit:
        lines.append(f"\n*(Showing top {limit} of {len(chars)}; pass `--league <name>` to narrow.)*")
    if chars:
        top_name = str(chars[0].get("name") or "")
        src_arg = target.raw_source or target.account_name
        realm_flag = f" --realm {target.realm}" if target.server == "cn" or target.realm != "pc" else ""
        if src_arg:
            lines.append(f'\nNext: `pob diagnose "{src_arg}" --char "{top_name}"{realm_flag}`')
        else:
            lines.append(f'\nNext: `pob diagnose "https://poe.game.qq.com/" --realm cn --char "{top_name}"`')
    return "\n".join(lines)


def resolve_target_character(
    chars: list[dict[str, Any]],
    *,
    target: ProfileTarget,
) -> dict[str, Any]:
    """Select exact requested character, auto-select single character, or fail with structured disambiguation."""
    ordered = sort_profile_characters(chars, league_filter=target.league)
    if not ordered:
        raise DaemonRequestError(
            f"账号 `{target.account_name}` 未找到任何符合条件的角色"
            + (f"（赛季筛选：`{target.league}`）" if target.league else "")
            + "。"
        )

    if target.char_name:
        want = target.char_name.strip().lower()
        for c in ordered:
            if str(c.get("name") or "").strip().lower() == want:
                return c
        # Also search unfiltered full list in case --league excluded the explicitly named character
        for c in chars:
            if isinstance(c, dict) and str(c.get("name") or "").strip().lower() == want:
                return c
        table = format_profile_markdown(target, ordered, limit=10)
        raise DaemonRequestError(
            f"在账号 `{target.account_name}` 下未找到角色 `{target.char_name}`。\n\n{table}"
        )

    if len(ordered) == 1:
        return ordered[0]

    table = format_profile_markdown(target, ordered, limit=12)
    raise DaemonRequestError(
        f"账号 `{target.account_name}` 下有 {len(ordered)} 个角色，未指定 `--char`，为避免误导入永久区/旧赛季角色已中止导入。\n"
        f"请从下表选择目标角色后加上 `--char \"<角色名>\"` 重试：\n\n{table}"
    )


def fetch_intl_characters(
    account_name: str,
    *,
    realm: str = "pc",
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch international character list (public without cookie first; falls back to env cookie on 401/403)."""
    url = f"{INTL_HOST}/character-window/get-characters"
    params = {"accountName": account_name, "realm": realm}

    def _do_get(c: httpx.Client, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        return c.get(url, params=params, headers=extra_headers or {})

    used_cookie = False
    try:
        if client is not None:
            resp = _do_get(client)
            if resp.status_code in (401, 403):
                cookie = _env_intl_cookie()
                if cookie:
                    used_cookie = True
                    resp = _do_get(client, {"Cookie": cookie})
        else:
            with make_site_client(proxy=proxy) as own:
                resp = _do_get(own)
                if resp.status_code in (401, 403):
                    cookie = _env_intl_cookie()
                    if cookie:
                        used_cookie = True
                        resp = _do_get(own, {"Cookie": cookie})
    except httpx.HTTPError as exc:
        raise DaemonRequestError(
            f"连接 PoE 官网失败：{exc}（如需代理请设置 `POB_PROXY` / `HTTPS_PROXY` 或使用 `--proxy`）。"
        ) from exc

    if resp.status_code != 200:
        raise classify_http_status_error(
            resp.status_code,
            url=url,
            server="intl",
            account_name=account_name,
            retry_after=resp.headers.get("Retry-After"),
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise DaemonRequestError("PoE 官网返回了非 JSON 响应（可能触发 Cloudflare 验证）。") from exc

    if not isinstance(data, list):
        raise DaemonRequestError("PoE 官网角色列表格式异常或账号已隐藏角色。")
    return [c for c in data if isinstance(c, dict)], used_cookie


def _resolve_canonical_account_case(
    client: httpx.Client,
    account_name: str,
    *,
    cookie: str | None = None,
) -> str:
    """Resolve exact account-name casing from view-profile page (mirrors PoB ImportTab.lua:947)."""
    encoded = urllib.parse.quote(account_name, safe="")
    headers = {"Cookie": cookie} if cookie else {}
    try:
        resp = client.get(f"{INTL_HOST}/account/view-profile/{encoded}", headers=headers)
        if resp.status_code == 200:
            m = re.search(r"/view-profile/([^/\"']+)/characters", resp.text)
            if m:
                return normalize_account_name(m.group(1))
    except httpx.HTTPError:
        pass
    return account_name


def fetch_intl_character_bundle(
    account_name: str,
    char_entry: dict[str, Any],
    *,
    realm: str = "pc",
    proxy: str | None = None,
    use_cookie: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch passive skills + equipped items JSON for an international character."""
    cookie = _env_intl_cookie() if use_cookie else None
    char_name = str(char_entry.get("name") or "")

    def _fetch_pair(c: httpx.Client, acc: str) -> tuple[httpx.Response, httpx.Response]:
        base_params = {"accountName": acc, "character": char_name, "realm": realm}
        r_tree = c.get(f"{INTL_HOST}/character-window/get-passive-skills", params=base_params)
        r_items = c.get(f"{INTL_HOST}/character-window/get-items", params=base_params)
        return r_tree, r_items

    try:
        if client is not None:
            r_tree, r_items = _fetch_pair(client, account_name)
            if r_tree.status_code == 404 or r_items.status_code == 404:
                canonical = _resolve_canonical_account_case(client, account_name, cookie=cookie)
                if canonical and canonical != account_name:
                    r_tree, r_items = _fetch_pair(client, canonical)
        else:
            with make_site_client(proxy=proxy, cookie=cookie) as own:
                r_tree, r_items = _fetch_pair(own, account_name)
                if r_tree.status_code == 404 or r_items.status_code == 404:
                    canonical = _resolve_canonical_account_case(own, account_name, cookie=cookie)
                    if canonical and canonical != account_name:
                        r_tree, r_items = _fetch_pair(own, canonical)
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"拉取角色 `{char_name}` 数据失败：{exc}") from exc

    for label, resp in (("get-passive-skills", r_tree), ("get-items", r_items)):
        if resp.status_code != 200:
            raise classify_http_status_error(
                resp.status_code,
                url=f"{INTL_HOST}/character-window/{label}",
                server="intl",
                account_name=account_name,
                retry_after=resp.headers.get("Retry-After"),
            )

    try:
        tree_json = r_tree.json()
        items_json = r_items.json()
    except ValueError as exc:
        raise DaemonRequestError(f"角色 `{char_name}` 返回数据不是有效 JSON。") from exc
    if not isinstance(tree_json, dict) or not isinstance(items_json, dict):
        raise DaemonRequestError(f"角色 `{char_name}` 数据不可读取（账号或角色可能已设为私密）。")
    return {"char": char_entry, "passiveSkills": tree_json, "items": items_json}


def _poll_import_status(
    worker: BridgeWorker,
    *,
    predicate: Any,
    timeout: float = 25.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = worker.call("import_status", {}, timeout=10.0)
        if predicate(last):
            return last
        time.sleep(0.12)
    status_txt = last.get("status") or last.get("mode") or "timeout"
    raise DaemonRequestError(f"PoB 引擎官网角色导入超时（最后状态：{status_txt}）。")


def import_intl_via_native_bridge(
    worker: BridgeWorker,
    account_name: str,
    char_entry: dict[str, Any],
    *,
    realm: str = "pc",
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Import a public international character through PobTools' native `bridge.lua` site importer."""
    realm_id = _REALM_BRIDGE_ID.get(realm.lower(), "PC")
    char_name = str(char_entry.get("name") or "")
    league = char_entry.get("league")

    worker.call("new_build", {}, timeout=10.0)
    worker.call("fetch_characters", {"source": "site", "realm": realm_id, "accountName": account_name}, timeout=15.0)
    st = _poll_import_status(
        worker,
        predicate=lambda s: s.get("mode") != "DOWNLOADCHARLIST",
        timeout=timeout,
    )
    if st.get("mode") != "SELECTCHAR":
        raise BridgeError(f"Native site character list failed: {st.get('status') or st.get('mode')}")

    before_cnt = int(st.get("importedCount") or 0)
    worker.call(
        "import_account_character",
        {
            "source": "site",
            "realm": realm_id,
            "name": char_name,
            "league": league,
            "what": "tree",
            "deleteJewels": True,
        },
        timeout=15.0,
    )
    st_tree = _poll_import_status(
        worker,
        predicate=lambda s: int(s.get("importedCount") or 0) > before_cnt or s.get("mode") != "IMPORTING",
        timeout=timeout,
    )
    if int(st_tree.get("importedCount") or 0) <= before_cnt:
        raise BridgeError(f"Native site passive tree import failed: {st_tree.get('status')}")

    tree_cnt = int(st_tree.get("importedCount") or 0)
    worker.call(
        "import_account_character",
        {
            "source": "site",
            "realm": realm_id,
            "name": char_name,
            "league": league,
            "what": "items",
            "clearItems": True,
            "clearSkills": True,
            "ignoreWeaponSwap": False,
        },
        timeout=15.0,
    )
    st_items = _poll_import_status(
        worker,
        predicate=lambda s: int(s.get("importedCount") or 0) > tree_cnt or s.get("mode") != "IMPORTING",
        timeout=timeout,
    )
    if int(st_items.get("importedCount") or 0) <= tree_cnt:
        raise BridgeError(f"Native site items import failed: {st_items.get('status')}")
    return {"nativeSiteImport": True, "importedCount": int(st_items.get("importedCount") or 0)}
