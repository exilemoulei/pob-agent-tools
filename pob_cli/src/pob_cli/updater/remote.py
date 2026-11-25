"""Remote metadata fetching and asset staging from PobTools-zh and PathOfBuilding."""

from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
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

POBTOOLS_APP_ONLY_PREFIXES: tuple[str, ...] = (
    "Data/bridge",
    "Data/poe1/zh-rCN",
    "Data/poe1/zh-rTW",
)
POBTOOLS_DATA_ONLY_PREFIXES: tuple[str, ...] = (
    "Data/poe1/zh-rCN",
    "Data/poe1/zh-rTW",
    "Data/translations_version.json",
)
_SAFE_TREE_VER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    slim: bool = False,
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
            return ("ok", parse_pob_manifest(resp.text, slim=slim), new_etag)

    result = _fetch(functools.partial(rewrite_github_url, mirror=mirror))
    if result[0] != "ok" or not resolve_mirror(mirror):
        return result
    # The mirror reports a new manifest: it names the files to replace and their hashes.
    return _github_first(_fetch, lambda: result)


def _download_bytes_with_retry(
    client: httpx.Client,
    url: str,
    retries: int = ASSET_MAX_RETRIES,
    *,
    deadline: float | None = None,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req_kw: dict[str, Any] = {}
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0.05:
                    raise UpdateError(f"Timed out fetching '{url}' within budget")
                req_kw["timeout"] = _to_httpx_timeout(min(5.0, max(0.5, rem)))
            resp = client.get(url, **req_kw)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            if attempt >= retries or (deadline is not None and (time.monotonic() + 0.35) >= deadline):
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


def _rel_matches_prefix(rel_posix_lower: str, prefix: str) -> bool:
    p = prefix.replace("\\", "/").strip("/").lower()
    if not p:
        return False
    return rel_posix_lower == p or rel_posix_lower.startswith(p + "/")


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
            rel_posix_lower = rel.as_posix().lower()
            if only_prefixes:
                if not any(_rel_matches_prefix(rel_posix_lower, op) for op in only_prefixes):
                    continue
            if any(_rel_matches_prefix(rel_posix_lower, sp) for sp in skip_prefixes):
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


_MANIFEST_CACHE: dict[str, tuple[tuple[int, int], PobManifest]] = {}
_VERIFIED_CACHE: dict[tuple[str, int, int, str], bool] = {}
_CACHE_LOCK = threading.Lock()


def _full_manifest(manifest_path: Path) -> PobManifest:
    """`manifest.xml` parsed with `slim=False`, cached until the file changes."""
    st = manifest_path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    key = str(manifest_path)
    with _CACHE_LOCK:
        hit = _MANIFEST_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    manifest = parse_pob_manifest(manifest_path.read_text(encoding="utf-8", errors="replace"), slim=False)
    with _CACHE_LOCK:
        _MANIFEST_CACHE[key] = (stamp, manifest)
    return manifest


def _is_current(dst: Path, sha1: str) -> bool:
    """Whether `dst` matches its manifest SHA-1, cached per (size, mtime).

    Always the newline-tolerant check: PoB's own updater writes text files with the platform's
    line endings, so most `TreeData/*/tree.lua` fail a raw SHA-1 although they are intact.
    """
    try:
        st = dst.stat()
    except OSError:
        return False
    key = (str(dst), st.st_size, st.st_mtime_ns, sha1.lower())
    with _CACHE_LOCK:
        hit = _VERIFIED_CACHE.get(key)
    if hit is None:
        try:
            hit = bool(_pkg().verify_pob_sha1(dst.read_bytes(), sha1))
        except OSError:
            return False
        with _CACHE_LOCK:
            _VERIFIED_CACHE[key] = hit
    return hit


def _plan_manifest_subset(
    poe1: Path, predicate: Callable[[ManifestFileEntry], bool], target_root: Path
) -> tuple[PobManifest | None, list[Path], list[ManifestFileEntry]]:
    manifest_path = poe1 / "manifest.xml"
    if not manifest_path.is_file():
        return None, [], []
    manifest = _full_manifest(manifest_path)
    ready: list[Path] = []
    to_fetch: list[ManifestFileEntry] = []
    for entry in manifest.files.values():
        if not predicate(entry):
            continue
        dst = target_root / entry.local_rel
        if _is_current(dst, entry.sha1):
            ready.append(dst)
        else:
            to_fetch.append(entry)
    return manifest, ready, to_fetch


def fetch_pob_manifest_subset(
    poe1_dir: Path,
    predicate: Callable[[ManifestFileEntry], bool],
    *,
    dest_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    max_workers: int = 6,
    timeout: Any = None,
) -> list[Path]:
    """Fetch SHA1-verified files matching `predicate` from local `PoE1/manifest.xml` on demand.

    Parses `manifest.xml` with `slim=False` so skipped optional assets (legacy `TreeData/<ver>/`
    or single-jewel `Data/TimelessJewelData/*.zip*`) can be materialized on demand without
    leaving a full ~500 MiB install on disk. All or nothing: every file lands in a staging folder
    first and is renamed into place only once all of them downloaded and verified, so a timeout
    never leaves half a tree version behind.
    """
    poe1 = Path(poe1_dir)
    target_root = Path(dest_dir) if dest_dir is not None else poe1
    manifest, ready, to_fetch = _plan_manifest_subset(poe1, predicate, target_root)
    if manifest is None or not to_fetch:
        return ready
    if os.environ.get("POB_OFFLINE") == "1":
        raise UpdateError("POB_OFFLINE=1: cannot fetch missing optional PoB assets on demand")

    pkg = _pkg()
    verify_fn = pkg.verify_pob_sha1
    has_budget = isinstance(timeout, (int, float)) and timeout > 0
    deadline = (time.monotonic() + float(timeout)) if has_budget else None
    eff_timeout = _to_httpx_timeout(
        min(5.0, float(timeout)) if has_budget else (_ASSET_TIMEOUT_SPEC if timeout is None else timeout)
    )
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pob-stage-", dir=target_root))
    try:
        with pkg._make_http_client(
            timeout=eff_timeout,
            proxy=proxy,
            follow_redirects=True,
            max_connections=max(max_workers, 4),
        ) as client:

            def _fetch_one(entry: ManifestFileEntry) -> tuple[Path, Path]:
                url = build_manifest_file_url(manifest, entry, mirror=mirror)
                data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES, deadline=deadline)
                if not verify_fn(data, entry.sha1):
                    raise UpdateError(
                        f"SHA1 mismatch for '{entry.local_rel}': expected {entry.sha1}, "
                        f"got {hashlib.sha1(data).hexdigest()}"
                    )
                staged = staging / entry.local_rel
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(data)
                return staged, target_root / entry.local_rel

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch))) as pool:
                moves = [f.result() for f in [pool.submit(_fetch_one, e) for e in to_fetch]]
        for staged, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, dst)
            ready.append(dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return ready


def _tree_version_predicate(tree_version: str) -> Callable[[ManifestFileEntry], bool]:
    ver = tree_version.strip()
    if not ver or not _SAFE_TREE_VER_RE.match(ver):
        raise UpdateError(f"Invalid tree_version: {tree_version!r}")
    prefix = f"treedata/{ver.lower()}/"

    def _want(entry: ManifestFileEntry) -> bool:
        low = entry.local_rel.replace("\\", "/").lower()
        return low.startswith(prefix) and low.endswith(".lua")

    return _want


def missing_tree_data_files(poe1_dir: Path, tree_version: str) -> list[str]:
    """Manifest paths of `TreeData/<tree_version>/` that are absent or fail their SHA-1.

    A tree version counts as installed only when every file the manifest lists for it is, not
    when its folder exists: the slim engine keeps `TreeData/3_19/Assets.lua` without `tree.lua`.
    Empty when the manifest does not know the version (or there is no manifest).
    """
    poe1 = Path(poe1_dir)
    _, _, to_fetch = _plan_manifest_subset(poe1, _tree_version_predicate(tree_version), poe1)
    return [e.local_rel for e in to_fetch]


def ensure_tree_data_files(
    poe1_dir: Path,
    tree_version: str,
    *,
    dest_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    timeout: Any = None,
) -> list[Path]:
    """Ensure `TreeData/<tree_version>/*.lua` (tree.lua, sprites.lua, Assets.lua) exist via manifest SHA1."""
    ver = tree_version.strip()
    if not ver or not _SAFE_TREE_VER_RE.match(ver):
        raise UpdateError(f"Invalid tree_version: {tree_version!r}")
    prefix = f"treedata/{ver.lower()}/"

    def _want(entry: ManifestFileEntry) -> bool:
        low = entry.local_rel.replace("\\", "/").lower()
        return low.startswith(prefix) and low.endswith(".lua")

    target_root = Path(dest_dir) if dest_dir is not None else Path(poe1_dir)
    ver_dir = target_root / "TreeData" / ver
    inc_marker = ver_dir / ".incomplete"
    had_dir = ver_dir.is_dir()
    if had_dir and not (ver_dir / "tree.lua").is_file():
        inc_marker.write_text("1\n", encoding="utf-8")
    try:
        out = fetch_pob_manifest_subset(
            poe1_dir, _want, dest_dir=dest_dir, proxy=proxy, mirror=mirror, timeout=timeout
        )
    except Exception:
        if ver_dir.is_dir():
            inc_marker.write_text("1\n", encoding="utf-8")
        raise
    if out:
        inc_marker.unlink(missing_ok=True)
    return out
