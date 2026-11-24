"""CN PoE1 server (`poe.game.qq.com`) character-window client (L1, env-only `POE_CN_SESSID`)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..errors import DaemonRequestError
from .site import CN_HOST, classify_http_status_error, make_site_client

_CN_NON_POB_INVENTORY_IDS = frozenset({"MainInventory", "ExpandedMainInventory", "Trinket"})
_CN_NON_POB_BASE_TYPES = frozenset({"赏金猎人饰品", "Thief's Trinket"})


def _normalize_cn_cookie(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    val = raw.strip()
    return val if "=" in val else f"POESESSID={val}"


def get_env_cn_cookie() -> str | None:
    """Read CN PoE session cookie strictly from `POE_CN_COOKIE` or `POE_CN_SESSID` env vars."""
    for env_key in ("POE_CN_COOKIE", "POE_CN_SESSID"):
        cookie = _normalize_cn_cookie(os.environ.get(env_key))
        if cookie:
            return cookie
    return None


def _require_cn_cookie() -> str:
    cookie = get_env_cn_cookie()
    if not cookie:
        raise DaemonRequestError(
            "国服官网（poe.game.qq.com）所有角色接口均要求登录态（无 Cookie 会返回 HTTP 401）。\n"
            "请在环境变量中设置 `POE_CN_SESSID`（或完整 `POE_CN_COOKIE`）后重试；"
            "严禁通过命令行参数传 Cookie 或在终端打印环境变量。"
        )
    return cookie


def _cn_form_headers(cookie: str) -> dict[str, str]:
    return {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{CN_HOST}/",
        "Origin": CN_HOST,
    }


def fetch_cn_account_name(
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Resolve current logged-in CN account name via `GET https://poe.game.qq.com/api/profile`."""
    cookie = _require_cn_cookie()
    url = f"{CN_HOST}/api/profile"
    try:
        if client is not None:
            resp = client.get(url, headers={"Cookie": cookie})
        else:
            with make_site_client(proxy=proxy, cookie=cookie) as own:
                resp = own.get(url)
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"连接国服官网 `/api/profile` 失败：{exc}") from exc

    if resp.status_code != 200:
        raise classify_http_status_error(
            resp.status_code,
            url=url,
            server="cn",
            retry_after=resp.headers.get("Retry-After"),
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise DaemonRequestError("国服 `/api/profile` 返回了非 JSON 响应，请检查 `POE_CN_SESSID` 是否有效。") from exc
    name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
    if not name:
        raise DaemonRequestError("无法从国服 `/api/profile` 解析当前账号名，请显式传入国服官网资料链接或账号名。")
    return name


def fetch_cn_characters(
    account_name: str | None = None,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch CN server character list via `POST /character-window/get-characters`."""
    cookie = _require_cn_cookie()
    acc = (account_name or "").strip()
    if not acc:
        acc = fetch_cn_account_name(proxy=proxy, client=client)

    url = f"{CN_HOST}/character-window/get-characters"
    form = {"accountName": acc, "realm": "pc"}
    try:
        if client is not None:
            resp = client.post(url, data=form, headers=_cn_form_headers(cookie))
        else:
            with make_site_client(proxy=proxy) as own:
                resp = own.post(url, data=form, headers=_cn_form_headers(cookie))
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"连接国服官网角色列表失败：{exc}") from exc

    if resp.status_code != 200:
        raise classify_http_status_error(
            resp.status_code,
            url=url,
            server="cn",
            account_name=acc,
            retry_after=resp.headers.get("Retry-After"),
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise DaemonRequestError("国服角色列表返回非 JSON，请确认 `POE_CN_SESSID` 未过期。") from exc

    if not isinstance(data, list):
        raise DaemonRequestError(f"国服账号 `{acc}` 角色列表不可读取（返回格式异常或未公开）。")
    return acc, [c for c in data if isinstance(c, dict)]


def _strip_cn_non_pob_items(items_payload: dict[str, Any]) -> dict[str, Any]:
    """Drop backpack/trinket items (`MainInventory`, `ExpandedMainInventory`, Thief's Trinket)."""
    raw_items = items_payload.get("items")
    if not isinstance(raw_items, list):
        return items_payload
    filtered = [
        it
        for it in raw_items
        if isinstance(it, dict)
        and str(it.get("inventoryId") or "") not in _CN_NON_POB_INVENTORY_IDS
        and str(it.get("baseType") or "") not in _CN_NON_POB_BASE_TYPES
    ]
    return {**items_payload, "items": filtered}


def fetch_cn_character_bundle(
    account_name: str,
    char_entry: dict[str, Any],
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch CN equipped items & passive skills JSON via `POST` form calls."""
    cookie = _require_cn_cookie()
    char_name = str(char_entry.get("name") or "").strip()
    if not char_name:
        raise DaemonRequestError("国服角色名不能为空。")
    form = {"accountName": account_name, "character": char_name, "realm": "pc"}
    headers = _cn_form_headers(cookie)
    items_url = f"{CN_HOST}/character-window/get-items"
    tree_url = f"{CN_HOST}/character-window/get-passive-skills"

    try:
        if client is not None:
            r_items = client.post(items_url, data=form, headers=headers)
            r_tree = client.post(tree_url, data=form, headers=headers)
        else:
            with make_site_client(proxy=proxy) as own:
                r_items = own.post(items_url, data=form, headers=headers)
                r_tree = own.post(tree_url, data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"拉取国服角色 `{char_name}` 数据失败：{exc}") from exc

    for url, resp in ((items_url, r_items), (tree_url, r_tree)):
        if resp.status_code != 200:
            raise classify_http_status_error(
                resp.status_code,
                url=url,
                server="cn",
                account_name=account_name,
                retry_after=resp.headers.get("Retry-After"),
            )

    try:
        items_json = r_items.json()
        tree_json = r_tree.json()
    except ValueError as exc:
        raise DaemonRequestError(f"国服角色 `{char_name}` 返回数据不是有效 JSON。") from exc
    if not isinstance(items_json, dict) or not isinstance(tree_json, dict):
        raise DaemonRequestError(f"国服角色 `{char_name}` 数据不可读取（可能被隐藏或登录态过期）。")
    return {
        "char": char_entry,
        "items": _strip_cn_non_pob_items(items_json),
        "passiveSkills": tree_json,
    }
