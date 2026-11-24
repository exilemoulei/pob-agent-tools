"""Remote metadata fetching and asset staging from PobTools-zh and PathOfBuilding."""

from __future__ import annotations

import functools
import hashlib
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

from .constants import (
    _ASSET_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    ASSET_MAX_RETRIES,
    POB_MANIFEST_URL,
    POBTOOLS_DOWNLOAD_BASE,
    POBTOOLS_LATEST_URL,
    ManifestFileEntry,
    PobManifest,
    UpdateError,
    _to_httpx_timeout,
    resolve_mirror,
    rewrite_github_url,
)
from .manifest import build_manifest_file_url, parse_pob_manifest


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def _extract_tag_from_location(location: str) -> str:
    """Extract release tag from GitHub 302 Location header."""
    if "/tag/" in location:
        tag = location.rsplit("/tag/", 1)[-1].split("?", 1)[0].strip("/")
        if tag:
            return urllib.parse.unquote(tag)
    raise UpdateError(f"Could not extract release tag from Location header: {location!r}")


def _github_first(fetch: Any, fallback: Any) -> Any:
    """Take hash-bearing metadata from GitHub itself; `fallback()` only when GitHub does not answer.

    Downloads are checked against these hashes, so a mirror that also served them could swap a
    file and its hash together. With no route to GitHub the mirror's copy is used, and the engine
    files are then only as trustworthy as that mirror.
    """
    from xml.etree.ElementTree import ParseError  # a cut-off manifest.xml; not a ValueError

    import httpx

    try:
        return fetch(lambda url: url)
    except (httpx.HTTPError, UpdateError, ValueError, ParseError):
        return fallback()


def fetch_pobtools_latest_info(
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: Any = None,
    fetch_manifest: bool = True,
) -> dict[str, Any]:
    """Resolve latest PobTools-zh tag via 302 redirect (NEVER api.github.com) and optional manifest JSON."""
    eff_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if timeout is None else timeout)
    via_mirror = functools.partial(rewrite_github_url, mirror=mirror)

    def _fetch(via: Any) -> dict[str, Any]:
        return _fetch_pobtools_latest_info(via, proxy, eff_timeout, fetch_manifest)

    if not fetch_manifest or not resolve_mirror(mirror):
        # The tag alone gates no download, so the routine check stays on the mirror.
        return _fetch(via_mirror)
    return _github_first(_fetch, lambda: _fetch(via_mirror))


def _fetch_pobtools_latest_info(
    via: Any, proxy: str | None, eff_timeout: Any, fetch_manifest: bool
) -> dict[str, Any]:
    latest_url = via(POBTOOLS_LATEST_URL)
    make_client = _pkg()._make_http_client

    with make_client(timeout=eff_timeout, proxy=proxy, follow_redirects=False) as client:
        resp = client.get(latest_url)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            tag = _extract_tag_from_location(loc)
        elif resp.status_code == 200 and "/tag/" in str(resp.url):
            tag = _extract_tag_from_location(str(resp.url))
        else:
            raise UpdateError(f"Unexpected status {resp.status_code} from PobTools-zh releases/latest")

    info: dict[str, Any] = {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "manifest": None,
        "dataManifest": None,
    }
    if not fetch_manifest:
        return info

    manifest_url = via(f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/PobTools-manifest-{tag}.json")
    with make_client(timeout=eff_timeout, proxy=proxy, follow_redirects=True) as client:
        mresp = client.get(manifest_url)
        mresp.raise_for_status()
        manifest_data = mresp.json()
        info["manifest"] = manifest_data
        if isinstance(manifest_data, dict):
            if manifest_data.get("version"):
                info["version"] = str(manifest_data["version"]).strip()
            data_tag = manifest_data.get("dataTag")
            if data_tag:
                data_manifest_url = via(
                    f"{POBTOOLS_DOWNLOAD_BASE}/{data_tag}/PobTools-manifest-{data_tag}.json"
                )
                dresp = client.get(data_manifest_url)
                if dresp.status_code == 200:
                    info["dataManifest"] = dresp.json()
    return info


def fetch_pob_manifest_remote(
    *,
    etag: str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: Any = None,
) -> tuple[str, PobManifest | None, str | None]:
    """Fetch official PathOfBuilding manifest.xml with optional If-None-Match ETag.

    Returns ("not_modified", None, etag) on 304, or ("ok", PobManifest, new_etag) on 200.
    """
    eff_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if timeout is None else timeout)
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag

    def _fetch(via: Any) -> tuple[str, PobManifest | None, str | None]:
        with _pkg()._make_http_client(timeout=eff_timeout, proxy=proxy, follow_redirects=True) as client:
            resp = client.get(via(POB_MANIFEST_URL), headers=headers)
            if resp.status_code == 304:
                return ("not_modified", None, resp.headers.get("etag") or etag)
            resp.raise_for_status()
            new_etag = resp.headers.get("etag") or etag
            return ("ok", parse_pob_manifest(resp.text), new_etag)

    result = _fetch(functools.partial(rewrite_github_url, mirror=mirror))
    if result[0] != "ok" or not resolve_mirror(mirror):
        return result
    # The mirror reports a new manifest: it names the files to replace and their hashes.
    return _github_first(_fetch, lambda: result)


def _download_bytes_with_retry(
    client: httpx.Client,
    url: str,
    retries: int = ASSET_MAX_RETRIES,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(0.25)
    assert last_exc is not None
    raise last_exc


def _download_pob_files_to_staging(
    manifest: PobManifest,
    entries: list[ManifestFileEntry],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    max_workers: int = 12,
) -> dict[Path, Path]:
    """Concurrently download PoB manifest entries to staging_dir and verify every SHA1."""
    staged: dict[Path, Path] = {}
    if not entries:
        return staged

    pkg = _pkg()
    verify_fn = pkg.verify_pob_sha1

    with pkg._make_http_client(
        timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC),
        proxy=proxy,
        follow_redirects=True,
        max_connections=max(max_workers, 8),
    ) as client:

        def _fetch_one(entry: ManifestFileEntry) -> tuple[Path, Path]:
            url = build_manifest_file_url(manifest, entry, mirror=mirror)
            data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
            if not verify_fn(data, entry.sha1):
                raise UpdateError(
                    f"SHA1 mismatch for '{entry.local_rel}': expected {entry.sha1}, "
                    f"got {hashlib.sha1(data).hexdigest()}"
                )
            rel_path = Path("PoE1") / entry.local_rel
            out_path = staging_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            return (rel_path, out_path)

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as pool:
            futures = [pool.submit(_fetch_one, e) for e in entries]
            for fut in concurrent.futures.as_completed(futures):
                rel_path, out_path = fut.result()
                staged[rel_path] = out_path

    return staged


def _extract_zip_safely(
    zip_path: Path,
    dest_dir: Path,
    *,
    skip_prefixes: tuple[str, ...] = (),
    only_prefixes: tuple[str, ...] = (),
) -> dict[Path, Path]:
    """Extract a zip safely into dest_dir (guarding against Zip Slip and single wrapper dir)."""
    import zipfile

    extracted: dict[Path, Path] = {}
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos:
            return extracted

        # Detect single top-level wrapper folder (e.g. PobTools-1.8.12/pob-zh.exe)
        top_levels = {Path(info.filename).parts[0] for info in infos if Path(info.filename).parts}
        strip_top = False
        if len(top_levels) == 1:
            only_top = next(iter(top_levels))
            if only_top.lower() not in {"data", "engine", "fonts", "poe1", "ui", "pobtools", "filters"}:
                strip_top = True

        for info in infos:
            parts = Path(info.filename).parts
            if strip_top and len(parts) > 1:
                parts = parts[1:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe zip member path: {info.filename}")
            first_lower = rel.parts[0].lower()
            if only_prefixes:
                if not any(first_lower == op.lower() for op in only_prefixes):
                    continue
            if any(first_lower == sp.lower() for sp in skip_prefixes):
                continue
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Zip Slip detected: {info.filename}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            extracted[rel] = out

    return extracted


def _pick_pobtools_asset(manifest_data: dict[str, Any], prefer_update: bool) -> dict[str, Any] | None:
    assets = manifest_data.get("assets") or []
    if not isinstance(assets, list):
        return None
    zips = [a for a in assets if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip")]
    if not zips:
        return None
    if prefer_update:
        for a in zips:
            if "update" in str(a.get("name", "")).lower():
                return a
    for a in zips:
        name_l = str(a.get("name", "")).lower()
        if "update" not in name_l and "datav2" not in name_l:
            return a
    return zips[0]


def _pick_data_asset(data_manifest: dict[str, Any]) -> dict[str, Any] | None:
    assets = data_manifest.get("assets") or []
    if not isinstance(assets, list):
        return None
    for a in assets:
        if (
            isinstance(a, dict)
            and "datav2" in str(a.get("name", "")).lower()
            and str(a.get("name", "")).lower().endswith(".zip")
        ):
            return a
    for a in assets:
        if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip"):
            return a
    return None


def _download_and_stage_release_zip(
    tag: str,
    asset: dict[str, Any],
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_prefixes: tuple[str, ...] = ("PoE1",),
    only_prefixes: tuple[str, ...] = (),
) -> dict[Path, Path]:
    name = str(asset["name"])
    expected_sha256 = str(asset.get("sha256") or "").strip().lower()
    url = rewrite_github_url(f"{POBTOOLS_DOWNLOAD_BASE}/{tag}/{urllib.parse.quote(name)}", mirror=mirror)

    zip_tmp = staging_dir / "_downloads" / name
    zip_tmp.parent.mkdir(parents=True, exist_ok=True)
    with _pkg()._make_http_client(
        timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True
    ) as client:
        data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
    # No hash in the manifest is a refusal, not a pass: this zip holds the engine that pob runs.
    actual = hashlib.sha256(data).hexdigest().lower()
    if actual != expected_sha256:
        raise UpdateError(
            f"SHA256 mismatch for '{name}': expected {expected_sha256 or 'a sha256 in the manifest'}, got {actual}"
        )
    zip_tmp.write_bytes(data)

    extract_root = staging_dir / "_extract" / Path(name).stem
    extract_root.mkdir(parents=True, exist_ok=True)
    return _extract_zip_safely(
        zip_tmp,
        extract_root,
        skip_prefixes=skip_prefixes,
        only_prefixes=only_prefixes,
    )
