"""PoB manifest XML parsing, SHA1 verification, stat caching, and diffing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .constants import (
    MANIFEST_STAT_CACHE_FILENAME,
    ManifestFileEntry,
    PobManifest,
    UpdateError,
    rewrite_github_url,
)


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def verify_pob_sha1(data: bytes, expected_sha1: str) -> bool:
    """Verify bytes against PoB manifest SHA1 (raw bytes, with official LF/CRLF tolerance)."""
    exp = expected_sha1.strip().lower()
    if hashlib.sha1(data).hexdigest() == exp:
        return True
    # Mirror official PoB UpdateCheck.lua line 194/276 tolerance for text files from GitHub raw
    if b"\n" in data and b"\r\n" not in data:
        if hashlib.sha1(data.replace(b"\n", b"\r\n")).hexdigest() == exp:
            return True
    if b"\r\n" in data:
        if hashlib.sha1(data.replace(b"\r\n", b"\n")).hexdigest() == exp:
            return True
    return False


def parse_pob_manifest(xml_text: str, *, include_win32_runtime: bool | None = None) -> PobManifest:
    """Parse a PathOfBuilding manifest.xml string into structured metadata & file entries."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    if root.tag != "PoBVersion":
        raise UpdateError(f"Invalid PoB manifest root element: {root.tag}")

    ver_elem = root.find("Version")
    if ver_elem is None or not ver_elem.attrib.get("number"):
        raise UpdateError("PoB manifest is missing <Version number=...>")

    version = ver_elem.attrib["number"].strip()
    branch = (ver_elem.attrib.get("branch") or "master").strip()
    platform_str = (ver_elem.attrib.get("platform") or "win32").strip()
    keep_win32 = bool(_pkg().is_windows()) if include_win32_runtime is None else bool(include_win32_runtime)

    sources: dict[str, dict[str, str]] = {}
    for src_elem in root.findall("Source"):
        part = (src_elem.attrib.get("part") or "").strip()
        url = (src_elem.attrib.get("url") or "").strip()
        plat = (src_elem.attrib.get("platform") or "any").strip()
        if part and url:
            sources.setdefault(part, {})[plat] = url

    files: dict[str, ManifestFileEntry] = {}
    for f_elem in root.findall("File"):
        raw_name = (f_elem.attrib.get("name") or "").strip()
        part = (f_elem.attrib.get("part") or "program").strip()
        sha1 = (f_elem.attrib.get("sha1") or "").strip().lower()
        plat = f_elem.attrib.get("platform")
        runtime = f_elem.attrib.get("runtime")
        if not raw_name or not sha1:
            continue
        if keep_win32:
            if plat and plat != "win32":
                continue
            if runtime and runtime != "win32":
                continue
        else:
            # POSIX (macOS/Linux) uses native LuaJIT + libz; skip Windows DLL/EXE runtime files
            if plat and plat == "win32":
                continue
            if runtime and runtime == "win32":
                continue
            if plat and plat not in ("any", ""):
                continue
        local_rel = raw_name.replace("{space}", " ")
        # Path traversal guard
        norm_parts = Path(local_rel).parts
        if Path(local_rel).is_absolute() or ".." in norm_parts:
            raise UpdateError(f"Unsafe manifest path: {raw_name}")
        files[raw_name] = ManifestFileEntry(
            raw_name=raw_name,
            local_rel=local_rel,
            part=part,
            sha1=sha1,
            runtime=runtime,
            platform=plat,
        )

    if not sources or not files:
        raise UpdateError("PoB manifest contains no sources or files.")

    return PobManifest(
        version=version,
        branch=branch,
        platform=platform_str,
        sources=sources,
        files=files,
        raw_xml=xml_text,
    )


def build_manifest_file_url(manifest: PobManifest, entry: ManifestFileEntry, mirror: str | None = None) -> str:
    """Build the raw download URL for a PoB manifest file entry."""
    part_sources = manifest.sources.get(entry.part)
    if not part_sources:
        raise UpdateError(f"No <Source> defined in manifest for part='{entry.part}'")
    base = part_sources.get(manifest.platform) or part_sources.get("any")
    if not base:
        raise UpdateError(f"No matching platform source for part='{entry.part}'")
    base = base.replace("{branch}", manifest.branch)
    # Official PoB GitHub raw keeps `{space}` literal in runtime filenames (URL-escaped)
    quoted = urllib.parse.quote(entry.raw_name, safe="/")
    url = base + quoted
    return rewrite_github_url(url, mirror=mirror)


def _stat_cache_path(cache_dir: Path | None) -> Path:
    base = Path(cache_dir) if cache_dir is not None else _pkg().get_shared_cache_dir()
    return base / MANIFEST_STAT_CACHE_FILENAME


def _load_manifest_stat_cache(
    cache_path: Path,
    poe1_key: str,
    manifest_sha1: str,
) -> dict[str, list[Any]]:
    if not cache_path.is_file():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        entry = raw.get(poe1_key)
        if not isinstance(entry, dict) or entry.get("manifestSha1") != manifest_sha1:
            return {}
        files = entry.get("files")
        return files if isinstance(files, dict) else {}
    except Exception:
        return {}


def _save_manifest_stat_cache(
    cache_path: Path,
    poe1_key: str,
    manifest_sha1: str,
    files_map: dict[str, list[Any]],
) -> None:
    tmp: Path | None = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if cache_path.is_file():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        existing[poe1_key] = {
            "manifestSha1": manifest_sha1,
            "updatedAt": time.time(),
            "files": files_map,
        }
        tmp = cache_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(existing, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, cache_path)
        tmp = None
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def find_local_manifest_mismatches(
    poe1_dir: Path,
    *,
    fast_stat: bool = False,
    cache_dir: Path | None = None,
) -> list[ManifestFileEntry]:
    """Verify local PoE1/ files against local PoE1/manifest.xml and return missing/tampered entries.

    When `fast_stat=True`, uses a `(size, mtime_ns, sha1)` cache (`manifest_stat_cache.json`)
    keyed by `manifest.xml` SHA1 so unchanged files skip re-hashing (<20ms vs ~1s), while any
    missing, resized, or mtime-changed file is still SHA1-verified.
    """
    poe1 = Path(poe1_dir)
    manifest_path = poe1 / "manifest.xml"
    if not manifest_path.is_file():
        return []
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha1 = hashlib.sha1(manifest_bytes).hexdigest()
    manifest = parse_pob_manifest(manifest_bytes.decode("utf-8", errors="replace"))

    use_cache = fast_stat or cache_dir is not None
    cache_path = _stat_cache_path(cache_dir) if use_cache else None
    try:
        poe1_key = str(poe1.resolve())
    except OSError:
        poe1_key = str(poe1)

    cached_files = (
        _load_manifest_stat_cache(cache_path, poe1_key, manifest_sha1)
        if (fast_stat and cache_path is not None)
        else {}
    )
    updated_cache: dict[str, list[Any]] = {}
    cache_dirty = not bool(cached_files)
    verify_fn = _pkg().verify_pob_sha1

    mismatched: list[ManifestFileEntry] = []
    for entry in manifest.files.values():
        fp = poe1 / entry.local_rel
        try:
            st = fp.stat()
        except OSError:
            mismatched.append(entry)
            cache_dirty = True
            continue
        if not stat.S_ISREG(st.st_mode):
            mismatched.append(entry)
            cache_dirty = True
            continue

        sig = [st.st_size, st.st_mtime_ns, entry.sha1]
        if fast_stat and cached_files.get(entry.local_rel) == sig:
            updated_cache[entry.local_rel] = sig
            continue

        try:
            data = fp.read_bytes()
        except OSError:
            mismatched.append(entry)
            cache_dirty = True
            continue
        if not verify_fn(data, entry.sha1):
            mismatched.append(entry)
            cache_dirty = True
        else:
            updated_cache[entry.local_rel] = sig
            cache_dirty = True

    if cache_path is not None and cache_dirty:
        _save_manifest_stat_cache(cache_path, poe1_key, manifest_sha1, updated_cache)

    return mismatched


def diff_pob_manifest(
    poe1_dir: Path,
    remote_manifest: PobManifest,
    *,
    verify_local_sha1: bool = True,
    fast_stat: bool = False,
    cache_dir: Path | None = None,
) -> tuple[list[ManifestFileEntry], list[Path]]:
    """Compute PoE1/ files to download and obsolete files to delete against remote_manifest.

    When verify_local_sha1=True (default), hashes actual files on disk against remote_manifest
    so tampered files (such as PoE1/UpdateCheck.lua) are always detected and repaired.
    When fast_stat=True is also set, unchanged files matching the cached `(size, mtime_ns, sha1)`
    tuple avoid re-reading/re-hashing disk bytes.
    """
    poe1 = Path(poe1_dir)
    local_manifest: PobManifest | None = None
    local_manifest_sha1: str | None = None
    local_manifest_path = poe1 / "manifest.xml"
    if local_manifest_path.is_file():
        try:
            local_bytes = local_manifest_path.read_bytes()
            local_manifest_sha1 = hashlib.sha1(local_bytes).hexdigest()
            local_manifest = parse_pob_manifest(local_bytes.decode("utf-8", errors="replace"))
        except Exception:
            local_manifest = None

    cached_files: dict[str, list[Any]] = {}
    cache_path: Path | None = None
    poe1_key = ""
    if verify_local_sha1 and fast_stat and local_manifest_sha1 is not None:
        cache_path = _stat_cache_path(cache_dir)
        try:
            poe1_key = str(poe1.resolve())
        except OSError:
            poe1_key = str(poe1)
        cached_files = _load_manifest_stat_cache(cache_path, poe1_key, local_manifest_sha1)

    updated_cache: dict[str, list[Any]] = dict(cached_files)
    cache_dirty = False
    verify_fn = _pkg().verify_pob_sha1

    to_download: list[ManifestFileEntry] = []
    for entry in remote_manifest.files.values():
        local_path = poe1 / entry.local_rel
        try:
            st = local_path.stat()
        except OSError:
            to_download.append(entry)
            continue
        if not stat.S_ISREG(st.st_mode):
            to_download.append(entry)
            continue
        if verify_local_sha1:
            sig = [st.st_size, st.st_mtime_ns, entry.sha1]
            if fast_stat and cached_files.get(entry.local_rel) == sig:
                continue
            try:
                data = local_path.read_bytes()
            except OSError:
                to_download.append(entry)
                continue
            if not verify_fn(data, entry.sha1):
                to_download.append(entry)
            elif fast_stat and local_manifest is not None:
                local_entry = local_manifest.files.get(entry.raw_name)
                if local_entry is not None and local_entry.sha1 == entry.sha1:
                    updated_cache[entry.local_rel] = sig
                    cache_dirty = True
        elif local_manifest is not None:
            local_entry = local_manifest.files.get(entry.raw_name)
            if local_entry is None or local_entry.sha1 != entry.sha1:
                to_download.append(entry)
        else:
            to_download.append(entry)

    if cache_path is not None and local_manifest_sha1 is not None and cache_dirty:
        _save_manifest_stat_cache(cache_path, poe1_key, local_manifest_sha1, updated_cache)

    to_delete: list[Path] = []
    if local_manifest is not None:
        remote_rels = {e.local_rel.lower() for e in remote_manifest.files.values()}
        for old_entry in local_manifest.files.values():
            if old_entry.local_rel.lower() not in remote_rels:
                if (poe1 / old_entry.local_rel).is_file():
                    to_delete.append(Path("PoE1") / old_entry.local_rel)

    return to_download, to_delete


def _render_local_manifest_xml(remote_manifest: PobManifest) -> str:
    """Serialize remote manifest XML ensuring platform/branch attributes exist on <Version>."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(remote_manifest.raw_xml)
    ver_elem = root.find("Version")
    if ver_elem is not None:
        ver_elem.set("platform", remote_manifest.platform)
        ver_elem.set("branch", remote_manifest.branch)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
