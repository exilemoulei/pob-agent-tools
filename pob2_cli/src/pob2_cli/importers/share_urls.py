# SPDX-License-Identifier: GPL-3.0-only
"""Multi-source PoE2 build URL fetcher & resolver (L1).

Supports PoB2 share codes (zlib & #2502 gzip), local XML/JSON files, CN WeGame links,
`pobb.in/{id}`, `poe2db.tw/pob/{id}`, `poe.ninja/poe2/pob/{id}`, `maxroll.gg/poe2/pob/{id}`,
`pastebin.com`, `rentry.co`, and `pob2://` protocol URIs. Rejects PoE1 URLs/XML via guardrails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..bridge_protocol import decode_share_code_xml, encode_xml_share_code
from ..errors import DaemonRequestError
from ..guardrails import validate_build_xml, validate_url
from ..updater import resolve_proxy
from .wegame_convert import convert_wegame_bundle_to_pob2_xml
from .wegame_fetch import fetch_wegame_share, is_wegame_share_url, parse_wegame_share_url

TextFetcher = Callable[[str, float], str]

_DEFAULT_HEADERS = {
    "User-Agent": "pob24agents/0.1 (PathOfBuilding-PoE2 compatible)",
    "Accept": "text/plain, application/json, */*;q=0.8",
}

_SITE_SPECS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("maxroll", re.compile(r"^https?://(?:www\.)?maxroll\.gg/poe2/(?:api/)?pob/([A-Za-z0-9._-]+?)/?(?:[?#].*)?$", re.I), "https://maxroll.gg/poe2/api/pob/{id}"),
    ("pobbin", re.compile(r"^https?://(?:www\.)?pobb\.in/(?:pob/)?([A-Za-z0-9._-]+?)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://pobb.in/pob/{id}"),
    ("poeninja", re.compile(r"^https?://(?:www\.)?(?:poe\.ninja/poe2|poe2\.ninja)/pob/(?:raw/)?([A-Za-z0-9._-]+?)/?(?:[?#].*)?$", re.I), "https://poe.ninja/poe2/pob/raw/{id}"),
    ("poe2db", re.compile(r"^https?://(?:www\.)?poe2db\.tw/pob/([A-Za-z0-9._-]+?)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://poe2db.tw/pob/{id}/raw"),
    ("pastebin", re.compile(r"^https?://(?:www\.)?pastebin\.com/(?:raw/)?([A-Za-z0-9]+)/?(?:[?#].*)?$", re.I), "https://pastebin.com/raw/{id}"),
    ("rentry", re.compile(r"^https?://(?:www\.)?rentry\.co/(?:paste/)?([A-Za-z0-9_-]+)(?:/raw)?/?(?:[?#].*)?$", re.I), "https://rentry.co/paste/{id}/raw"),
)

_POB2_URI_RE = re.compile(r"^pob2:[/\\]*([a-z0-9]+)[/\\]+([A-Za-z0-9._-]+)$", re.I)
_URI_DOWNLOAD_TEMPLATES: dict[str, tuple[str, str]] = {
    "pobbin": ("pobbin", "https://pobb.in/pob/{id}"),
    "poeninja": ("poeninja", "https://poe.ninja/poe2/pob/raw/{id}"),
    "poe2db": ("poe2db", "https://poe2db.tw/pob/{id}/raw"),
    "maxroll": ("maxroll", "https://maxroll.gg/poe2/api/pob/{id}"),
    "pastebin": ("pastebin", "https://pastebin.com/raw/{id}"),
    "rentry": ("rentry", "https://rentry.co/paste/{id}/raw"),
}


@dataclass(frozen=True)
class ResolvedBuildSource:
    """Normalized PoE2 build payload ready to load into BridgeWorker or save to disk."""

    xml: str
    code: str
    source_kind: str
    source_label: str
    download_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def parse_share_url(url: str) -> tuple[str, str] | None:
    """Return ``(site_kind, raw_download_url)`` for supported PoE2 share links, else None."""
    raw = (url or "").strip()
    if not raw:
        return None
    m_uri = _POB2_URI_RE.match(raw)
    if m_uri:
        site_key = m_uri.group(1).lower()
        bid = m_uri.group(2).strip("/")
        if site_key in _URI_DOWNLOAD_TEMPLATES:
            kind, tmpl = _URI_DOWNLOAD_TEMPLATES[site_key]
            return kind, tmpl.format(id=bid)
        return None
    for kind, pat, tmpl in _SITE_SPECS:
        m = pat.match(raw)
        if m:
            return kind, tmpl.format(id=m.group(1))
    return None


def _default_fetch_text(
    url: str,
    timeout: float,
    *,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
) -> str:
    kwargs: dict[str, Any] = {
        "headers": _DEFAULT_HEADERS,
        "timeout": httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        "follow_redirects": True,
    }
    if transport is not None:
        kwargs["transport"] = transport
    else:
        eff_proxy = resolve_proxy(proxy)
        if eff_proxy:
            kwargs["proxy"] = eff_proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise DaemonRequestError(f"Failed fetching PoE2 build URL `{url}`: {exc}") from exc
    if resp.status_code != 200:
        raise DaemonRequestError(f"PoE2 build URL `{url}` returned HTTP {resp.status_code}.")
    return resp.text


def _extract_code_or_xml_from_body(body: str, site_kind: str) -> tuple[str, str]:
    text = (body or "").strip()
    if not text:
        raise DaemonRequestError(f"Empty response body from `{site_kind}` build link.")
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for key in ("pobCode", "code", "buildCode", "data", "xml"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                    break
    if text.lstrip().startswith("<"):
        validate_build_xml(text, expected_game="poe2", source_hint=site_kind)
        return text, encode_xml_share_code(text)
    decoded = decode_share_code_xml(text)
    if decoded is None or "<PathOfBuilding" not in decoded:
        raise DaemonRequestError(f"Response from `{site_kind}` did not contain a valid PoB2 share code or XML.")
    validate_build_xml(decoded, expected_game="poe2", source_hint=site_kind)
    return decoded, text


def resolve_poe2_build_source(
    source: str | None = None,
    *,
    xml_code: str | None = None,
    data_dir: Path | None = None,
    timeout: float = 15.0,
    fetcher: TextFetcher | None = None,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
) -> ResolvedBuildSource:
    """Resolve any PoE2 build source (share URL, WeGame link, local file, XML, or code)."""
    raw = (xml_code or source or "").strip()
    if not raw:
        raise DaemonRequestError("Provide a PoE2 share URL, WeGame link, XML file path, or PoB2 share code.")

    if "://" in raw or raw.lower().startswith(("pob:", "pob2:", "www.", "poe", "maxroll.", "pobb.")):
        validate_url(raw, expected_game="poe2", cli_subcommand="import")

    # 1. CN WeGame cookie-free share link
    if is_wegame_share_url(raw):
        parse_wegame_share_url(raw)
        bundle = fetch_wegame_share(raw, timeout=timeout)
        res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
        validate_build_xml(res.xml, expected_game="poe2", source_hint=raw)
        return ResolvedBuildSource(
            xml=res.xml,
            code=encode_xml_share_code(res.xml),
            source_kind="wegame_url",
            source_label=raw,
            download_url=raw,
            warnings=list(res.warnings),
            stats=dict(res.stats),
            summary=res.to_summary_dict(),
        )

    # 2. Supported remote PoE2 build share URLs (pobb.in, poe2db.tw, poe.ninja/poe2, maxroll.gg/poe2, ...)
    parsed_site = parse_share_url(raw)
    if parsed_site is not None:
        site_kind, dl_url = parsed_site
        if fetcher is not None:
            body = fetcher(dl_url, timeout)
        else:
            body = _default_fetch_text(dl_url, timeout, transport=transport, proxy=proxy)
        xml_text, code_str = _extract_code_or_xml_from_body(body, site_kind)
        return ResolvedBuildSource(
            xml=xml_text,
            code=code_str,
            source_kind=f"{site_kind}_url",
            source_label=raw,
            download_url=dl_url,
        )

    # 3. Local file (XML or sanitized WeGame JSON bundle)
    if not raw.startswith("<") and len(raw) < 260 and "\n" not in raw:
        cand = Path(raw)
        if cand.is_file():
            content = cand.read_text(encoding="utf-8")
            if cand.suffix.lower() == ".json" or content.lstrip().startswith("{"):
                try:
                    bundle = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise DaemonRequestError(f"Invalid JSON bundle `{cand}`: {exc}") from exc
                res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
                validate_build_xml(res.xml, expected_game="poe2", source_hint=str(cand))
                return ResolvedBuildSource(
                    xml=res.xml,
                    code=encode_xml_share_code(res.xml),
                    source_kind="wegame_bundle",
                    source_label=cand.name,
                    warnings=list(res.warnings),
                    stats=dict(res.stats),
                    summary=res.to_summary_dict(),
                )
            xml_text, code_str = _extract_code_or_xml_from_body(content, cand.name)
            return ResolvedBuildSource(
                xml=xml_text,
                code=code_str,
                source_kind="pob2_xml_file",
                source_label=cand.name,
            )

    # 4. Inline PoB2 XML or base64url zlib/gzip share code
    if raw.lstrip().startswith("<"):
        validate_build_xml(raw, expected_game="poe2")
        return ResolvedBuildSource(
            xml=raw,
            code=encode_xml_share_code(raw),
            source_kind="pob2_xml",
            source_label="inline_xml",
        )
    decoded = decode_share_code_xml(raw)
    if decoded is not None and "<PathOfBuilding" in decoded:
        validate_build_xml(decoded, expected_game="poe2")
        return ResolvedBuildSource(
            xml=decoded,
            code=raw,
            source_kind="pob2_code",
            source_label="inline_code",
        )

    raise DaemonRequestError(
        "Unsupported PoE2 build source. Provide a PoB2 code, local XML/WeGame JSON file, "
        "`pobb.in`, `poe2db.tw/pob`, `poe.ninja/poe2/pob`, `maxroll.gg/poe2/pob`, or WeGame share URL."
    )


def load_source_label(source: str | None, _character: Any = None) -> str:
    s = (source or "").strip()
    if not s:
        return "inline_build"
    if s.startswith("<") or len(s) > 200:
        return "inline_build"
    return s


def load_build_into_worker(
    worker: Any,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
    auto_fix: bool = True,
    fetcher: TextFetcher | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Resolve a PoE2 build source, import it into ``worker``, run #2401/#2536 fixups, and return notes."""
    from .post_load_fixups import apply_post_load_fixups

    _ = (char_name, league, realm)
    data_dir = getattr(worker, "data_dir", None)
    resolved = resolve_poe2_build_source(
        source=source,
        xml_code=xml_code,
        data_dir=data_dir,
        fetcher=fetcher,
        transport=transport,
    )
    imp_res = worker.call("import_code", {"code": resolved.code, "mode": "replace"})
    fixups = apply_post_load_fixups(worker, auto_fix=auto_fix)
    info = worker.call("get_build_info", {})

    out: dict[str, Any] = {
        "game": "poe2",
        "loaded": True,
        "sourceKind": resolved.source_kind,
        "sourceLabel": resolved.source_label,
        "buildInfo": info,
        "postLoadFixups": fixups["fixups"],
        "notes": fixups["notes"],
    }
    if resolved.warnings:
        out["importWarnings"] = list(resolved.warnings)
    if fixups["warnings"]:
        out["fixupWarnings"] = list(fixups["warnings"])
    if isinstance(imp_res, dict):
        for k in ("droppedItems", "engineWarnings", "mainOutputShield"):
            if imp_res.get(k):
                out[k] = imp_res[k]
    return out


__all__ = [
    "ResolvedBuildSource",
    "TextFetcher",
    "load_build_into_worker",
    "load_source_label",
    "parse_share_url",
    "resolve_poe2_build_source",
]
