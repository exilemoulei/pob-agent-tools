"""Multi-source build importers (XML, Base64 share code, 8 share URLs, poe.ninja character URLs)."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from . import updater as _updater
from .bridge_client import BridgeError, BridgeWorker
from .errors import DaemonRequestError
from .json_io import atomic_write_json as _atomic_write_json
from .updater import get_runtime_dir

USER_AGENT = f"pob4agents/{__version__} (+https://github.com/exilemoulei/pob4agents)"
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=4.0, read=15.0, write=15.0, pool=4.0)

# poe.ninja puts the site language first when it is not English: poe.ninja/zh-tw/poe1/builds/...
_NINJA_HOST = r"^https?://poe\.ninja/(?:[a-z]{2}(?:-[a-z]{2})?/)?"

POE_NINJA_CHAR_RE = re.compile(
    _NINJA_HOST + r"poe1/builds/(?P<league>[^/?#]+)/character/(?P<account>[^/?#]+)/(?P<name>[^/?#]+)",
    re.IGNORECASE,
)

SHARE_RAW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^https?://pobb\.in/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pobb.in/{0}/raw"),
    (re.compile(r"^https?://poedb\.tw/(?:[a-z]{2}/)?pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poedb.tw/pob/{0}/raw"),
    (re.compile(_NINJA_HOST + r"pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poe.ninja/pob/{0}/raw"),
    (re.compile(_NINJA_HOST + r"poe1/pob/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://poe.ninja/poe1/pob/{0}/raw"),
    (re.compile(r"^https?://rentry\.co/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://rentry.co/{0}/raw"),
    (re.compile(r"^https?://pastebin\.com/(?:raw/)?([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pastebin.com/raw/{0}"),
    (re.compile(r"^https?://pastebinp\.com/(?:raw/)?([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pastebinp.com/raw/{0}"),
    (re.compile(r"^https?://pob\.codes/([A-Za-z0-9_-]+)(?:/.*)?$"), "https://pob.codes/{0}/raw"),
]

MAXROLL_POB_RE = re.compile(r"^https?://maxroll\.gg/poe/pob/([A-Za-z0-9_-]+)", re.IGNORECASE)


def xml_to_share_code(xml_text: str) -> str:
    """Compress raw PoB XML string into a URL-safe Base64 PoB share code."""
    clean_xml = xml_text.lstrip("\ufeff")
    compressed = zlib.compress(clean_xml.encode("utf-8"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def share_code_to_xml(code: str) -> str:
    """Decompress a URL-safe Base64 PoB share code into raw PoB XML text."""
    cleaned = re.sub(r"\s+", "", code.lstrip("\ufeff")).replace("-", "+").replace("_", "/")
    if not cleaned:
        raise ValueError("Invalid PoB share code: empty share code.")
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned += "=" * pad
    try:
        raw_bytes = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid PoB share code: {exc}") from exc
    try:
        try:
            decompressed = zlib.decompress(raw_bytes)
        except zlib.error:
            decompressed = zlib.decompress(raw_bytes, -15)
    except (zlib.error, ValueError) as exc:
        raise ValueError(f"Invalid PoB share code: {exc}") from exc
    return decompressed.decode("utf-8-sig", errors="replace").lstrip("\ufeff")


def _shared_cache_dir(runtime_dir: Path | None = None) -> Path:
    """Resolve shared engine-scoped cache directory, delegating to updater.get_shared_cache_dir if present."""
    try:
        fn = getattr(_updater, "get_shared_cache_dir", None)
        if callable(fn):
            cache_dir = Path(fn(runtime_dir))
        else:
            env_cache = os.environ.get("POB_CACHE_DIR", "").strip()
            if env_cache:
                cache_dir = Path(env_cache)
            else:
                rt = get_runtime_dir(runtime_dir)
                env_root = os.environ.get("POB_ROOT", "").strip()
                shared_candidate: Path | None = None
                if env_root:
                    try:
                        eng_parent = Path(env_root).resolve().parent
                        same_rt = eng_parent == rt.resolve()
                    except OSError:
                        eng_parent = Path(env_root).parent
                        same_rt = False
                    if not same_rt and (
                        (eng_parent / "cache").is_dir()
                        or eng_parent.name.lower() in (".runtime", "pob4agents", "pob-agent-tools")
                    ):
                        shared_candidate = eng_parent / "cache"
                cache_dir = shared_candidate or (rt / "cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except OSError:
        fallback = Path.home() / ".cache" / "pob-cli"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _get_cache_dir(runtime_dir: Path | None = None) -> Path:
    return _shared_cache_dir(runtime_dir)


def read_stale_cache(
    cache_file: Path,
    *,
    data_key: str = "data",
) -> tuple[Any | None, float | None]:
    """Read an expired/stale JSON cache entry if present and parseable.

    Returns ``(payload, age_seconds)`` or ``(None, None)`` if missing/unreadable.
    """
    if not cache_file.is_file():
        return None, None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cached, dict) or data_key not in cached:
            return None, None
        ts = cached.get("_ts")
        age = max(0.0, time.time() - float(ts)) if isinstance(ts, (int, float)) else None
        return cached[data_key], age
    except Exception:
        return None, None


def _resolve_proxy(proxy: str | None = None) -> str | None:
    """Resolve proxy URL from explicit arg or POB_PROXY/HTTPS_PROXY/HTTP_PROXY/ALL_PROXY."""
    if proxy and proxy.strip():
        return proxy.strip()
    for key in (
        "POB_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    return None


def _make_httpx_client(
    *,
    timeout: float | httpx.Timeout | None = None,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Create an httpx.Client honoring proxy configuration and standard User-Agent."""
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    resolved_proxy = _resolve_proxy(proxy)
    resolved_timeout = DEFAULT_HTTP_TIMEOUT if timeout is None else timeout
    kwargs: dict[str, Any] = {
        "timeout": resolved_timeout,
        "follow_redirects": True,
        "headers": merged_headers,
    }
    if resolved_proxy:
        kwargs["proxy"] = resolved_proxy
    return httpx.Client(**kwargs)


def _fetch_poe_ninja_index_state(
    ttl_sec: int = 900,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    cache_file = _shared_cache_dir() / "poe_ninja_index_state.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    url = "https://poe.ninja/poe1/api/data/index-state"
    try:
        if client is not None:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                resp = own_client.get(url)
                resp.raise_for_status()
                data = resp.json()
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            return stale_data
        raise

    _atomic_write_json(cache_file, {"_ts": now, "data": data})
    return data


def _fetch_poe_ninja_build_index_state(
    ttl_sec: int = 900,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch poe.ninja build-index-state (leagueBuilds + meta statistics) with 15m disk cache."""
    cache_file = _shared_cache_dir() / "poe_ninja_build_index_state.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    url = "https://poe.ninja/poe1/api/data/build-index-state"
    try:
        if client is not None:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                resp = own_client.get(url)
                resp.raise_for_status()
                data = resp.json()
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            return stale_data
        raise

    _atomic_write_json(cache_file, {"_ts": now, "data": data})
    return data


def _resolve_poe_ninja_overviews(
    league_slug: str,
    build_index_state: dict[str, Any] | None,
    index_state: dict[str, Any] | None,
    prefer_delve: bool,
) -> list[tuple[str, str]]:
    """Build ordered, deduplicated `(overview, type)` candidates for a poe.ninja league slug."""
    target = league_slug.strip().lower()
    raw_overviews: list[str] = []
    legacy_pairs: list[tuple[str, str]] = []

    def _matches_entry(entry: dict[str, Any]) -> bool:
        for k in ("leagueUrl", "url", "leagueName", "name", "snapshotName"):
            val = entry.get(k)
            if isinstance(val, str) and val.strip().lower() == target:
                return True
        return False

    if isinstance(build_index_state, dict):
        for entry in build_index_state.get("leagueBuilds", []) or []:
            if isinstance(entry, dict) and _matches_entry(entry):
                ov = entry.get("leagueUrl") or entry.get("url") or entry.get("leagueName")
                if isinstance(ov, str) and ov.strip():
                    raw_overviews.append(ov.strip())

    if isinstance(index_state, dict):
        for key in ("buildLeagues", "oldBuildLeagues"):
            for entry in index_state.get(key, []) or []:
                if isinstance(entry, dict) and _matches_entry(entry):
                    ov = entry.get("url") or entry.get("name")
                    if isinstance(ov, str) and ov.strip():
                        raw_overviews.append(ov.strip())

        for sv in index_state.get("snapshotVersions", []) or []:
            if isinstance(sv, dict) and _matches_entry(sv):
                ov = sv.get("snapshotName") or sv.get("url")
                snap_type = sv.get("type") or "exp"
                if isinstance(ov, str) and ov.strip():
                    legacy_pairs.append((ov.strip(), str(snap_type)))

    type_order = ("depthsolo", "exp") if prefer_delve else ("exp", "depthsolo")
    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str]] = []

    if prefer_delve:
        legacy_pairs.sort(key=lambda p: 0 if p[1] == "depthsolo" else 1)
    else:
        legacy_pairs.sort(key=lambda p: 0 if p[1] == "exp" else 1)

    for ov in raw_overviews:
        for snap_type in type_order:
            pair = (ov, snap_type)
            if pair not in seen:
                seen.add(pair)
                candidates.append(pair)

    for pair in legacy_pairs:
        if pair not in seen:
            seen.add(pair)
            candidates.append(pair)

    return candidates


def fetch_poe_ninja_character_code(url: str) -> str:
    """Resolve a poe.ninja character URL into a raw PoB share code."""
    from .guardrails import validate_url

    validate_url(url, expected_game="poe1", cli_subcommand="load")
    m = POE_NINJA_CHAR_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a valid poe.ninja character URL: {url}")

    league_slug = urllib.parse.unquote(m.group("league"))
    raw_account = urllib.parse.unquote(m.group("account"))
    char_name = urllib.parse.unquote(m.group("name"))
    hash_account = re.sub(r"-(\d{4})$", r"#\1", raw_account)

    parsed_url = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed_url.query)
    time_machine = qs.get("timemachine", [None])[0] or qs.get("timeMachine", [None])[0]
    url_type = (qs.get("type", [""])[0] or "").lower()
    prefer_delve = "delve" in league_slug.lower() or url_type == "depthsolo"

    build_index_state: dict[str, Any] | None = None
    index_state: dict[str, Any] | None = None
    fetch_errs: list[str] = []

    try:
        build_index_state = _fetch_poe_ninja_build_index_state()
    except Exception as exc:
        fetch_errs.append(f"build-index-state: {exc}")

    candidates = _resolve_poe_ninja_overviews(league_slug, build_index_state, None, prefer_delve)
    if not candidates:
        try:
            index_state = _fetch_poe_ninja_index_state()
        except Exception as exc:
            fetch_errs.append(f"index-state: {exc}")
        candidates = _resolve_poe_ninja_overviews(league_slug, build_index_state, index_state, prefer_delve)

    if not candidates:
        err_note = f" ({'; '.join(fetch_errs)})" if fetch_errs else ""
        msg = f"Could not find league slug '{league_slug}' in poe.ninja build-index-state or index-state{err_note}."
        if not fetch_errs:
            raise DaemonRequestError(msg)
        raise RuntimeError(msg)

    account_attempts = [raw_account]
    if hash_account != raw_account:
        account_attempts.append(hash_account)

    last_err: Exception | None = None
    with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        for overview, snap_type in candidates:
            for acc_idx, acc in enumerate(account_attempts):
                params: dict[str, str] = {
                    "overview": overview,
                    "type": snap_type,
                    "account": acc,
                    "name": char_name,
                }
                if time_machine:
                    params["timeMachine"] = time_machine
                try:
                    resp = client.get("https://poe.ninja/poe1/pob/raw/overview/code", params=params)
                    if resp.status_code == 200 and resp.text.strip():
                        code = resp.text.strip().strip('"')
                        share_code_to_xml(code)
                        return code
                    last_err = RuntimeError(f"HTTP {resp.status_code} for overview={overview}, type={snap_type}")
                    if resp.status_code != 404 and acc_idx == 0:
                        break
                except Exception as exc:
                    last_err = exc
                    break

    raise RuntimeError(f"Failed to fetch PoB code for poe.ninja character '{raw_account}/{char_name}': {last_err}")


def fetch_share_url_code_fallback(url: str) -> str:
    """Fallback Python httpx fetcher for share site URLs when Curl/Lua fails."""
    from .guardrails import validate_build_xml, validate_url

    clean_url = url.strip()
    validate_url(clean_url, expected_game="poe1", cli_subcommand="load")
    if POE_NINJA_CHAR_RE.match(clean_url):
        code = fetch_poe_ninja_character_code(clean_url)
        validate_build_xml(share_code_to_xml(code), expected_game="poe1", source_hint=clean_url)
        return code

    from .profile_import import wrap_share_http_error

    try:
        with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT) as client:
            for pattern, template in SHARE_RAW_PATTERNS:
                m = pattern.match(clean_url)
                if m:
                    resp = client.get(template.format(m.group(1)))
                    resp.raise_for_status()
                    code = resp.text.strip()
                    validate_build_xml(share_code_to_xml(code), expected_game="poe1", source_hint=clean_url)
                    return code

            m_maxroll = MAXROLL_POB_RE.match(clean_url)
            if m_maxroll:
                resp = client.get(
                    f"https://maxroll.gg/poe/api/pob/{m_maxroll.group(1)}",
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
                code = payload.get("build") or payload.get("code") or payload.get("data")
                if isinstance(code, str) and code.strip():
                    validate_build_xml(share_code_to_xml(code.strip()), expected_game="poe1", source_hint=clean_url)
                    return code.strip()

            resp = client.get(clean_url)
            resp.raise_for_status()
            text = resp.text.strip()
            validate_build_xml(share_code_to_xml(text), expected_game="poe1", source_hint=clean_url)
            return text
    except httpx.HTTPStatusError as exc:
        raise wrap_share_http_error(exc, clean_url) from exc


def load_build_into_worker(
    worker: BridgeWorker,
    *,
    source: str | None = None,
    xml_code: str | None = None,
    char_name: str | None = None,
    league: str | None = None,
    realm: str | None = None,
) -> dict[str, Any]:
    """Load a build into `worker` from XML, share URL, official profile, or Base64 code (R11)."""
    from .guardrails import validate_build_xml, validate_url
    from .profile_import import load_official_profile_into_worker, should_route_official_profile

    with worker.lock:
        import_method = "code"
        prof_extra: dict[str, Any] = {}
        if xml_code:
            clean_xml_code = xml_code.lstrip("\ufeff").strip()
            if clean_xml_code.startswith("<"):
                validate_build_xml(clean_xml_code, expected_game="poe1")
                clean_xml_code = xml_to_share_code(clean_xml_code)
            else:
                try:
                    decoded_xml = share_code_to_xml(clean_xml_code)
                except ValueError:
                    decoded_xml = ""
                if decoded_xml:
                    validate_build_xml(decoded_xml, expected_game="poe1")
            res = worker.call("import_code", {"code": clean_xml_code, "mode": "replace"})
            import_method = "xml_file"
        elif source or (realm and realm.strip().lower() == "cn"):
            src = (source or "").lstrip("\ufeff").strip()
            if src.startswith("<"):
                validate_build_xml(src, expected_game="poe1")
                res = worker.call("import_code", {"code": xml_to_share_code(src), "mode": "replace"})
                import_method = "xml_file"
            elif should_route_official_profile(src, char_name=char_name, realm=realm):
                prof_extra = load_official_profile_into_worker(
                    worker, src or None, char_name=char_name, league=league, realm=realm
                )
                import_method = str(prof_extra.get("importMethod") or "official_profile")
                res = prof_extra.get("importResult") or {}
            else:
                validate_url(src, expected_game="poe1", cli_subcommand="load")
                if POE_NINJA_CHAR_RE.match(src):
                    code = fetch_poe_ninja_character_code(src)
                    validate_build_xml(share_code_to_xml(code), expected_game="poe1", source_hint=src)
                    res = worker.call("import_code", {"code": code, "mode": "replace"})
                    import_method = "poe_ninja_character"
                elif src.startswith(("http://", "https://")):
                    try:
                        url_res = worker.call("import_from_url", {"url": src})
                        code = (url_res.get("code") or "").strip()
                        if not code:
                            raise BridgeError("import_from_url returned empty code")
                        try:
                            decoded_xml = share_code_to_xml(code)
                        except ValueError:
                            decoded_xml = ""
                        if decoded_xml:
                            validate_build_xml(decoded_xml, expected_game="poe1", source_hint=src)
                        res = worker.call("import_code", {"code": code, "mode": "replace"})
                        import_method = "url_native"
                    except BridgeError:
                        code = fetch_share_url_code_fallback(src)
                        res = worker.call("import_code", {"code": code, "mode": "replace"})
                        import_method = "url_fallback"
                else:
                    decoded_xml = share_code_to_xml(src)
                    validate_build_xml(decoded_xml, expected_game="poe1")
                    res = worker.call("import_code", {"code": src, "mode": "replace"})
                    import_method = "share_code"
        else:
            raise ValueError("Either 'source' or 'xml_code' must be provided.")

        exp = worker.call("export_code", {})
        worker.last_good_code = exp.get("code")
        ch = prof_extra.get("character")
        if isinstance(ch, dict):
            worker.source_label = f"profile:{ch.get('accountName')}/{ch.get('name')}"
        elif source:
            src_s = source.strip()
            if len(src_s) > 120 and not src_s.startswith(("http://", "https://", "<")):
                worker.source_label = f"share_code({len(src_s)} chars)"
            elif len(src_s) > 200:
                worker.source_label = f"{src_s[:120]}... ({len(src_s)} chars)"
            else:
                worker.source_label = src_s
        else:
            worker.source_label = "xml_file"
        worker.undo_stack.clear()
        worker.redo_stack.clear()
        worker.save_slot_state()

        out: dict[str, Any] = {
            "importMethod": import_method,
            "importResult": res,
            "buildInfo": worker.call("get_build_info", {}),
            "buildHeader": worker.call("get_build_header", {}),
        }
        for k in ("character", "importNotes", "translateWarnings"):
            if prof_extra.get(k):
                out[k] = prof_extra[k]
        return out
