"""pob4agents releases on GitHub: the latest tag, signed checksums, platform names.

The latest tag comes from the redirect of /releases/latest (no api.github.com call). Files are
trusted only through the release's SHA256SUMS.txt whose Ed25519 signature verifies (signature.py).
"""

from __future__ import annotations

import re
from typing import Any

from .constants import POB_AGENT_DOWNLOAD_BASE, POB_AGENT_RELEASES_LATEST_URL, rewrite_github_url
from .paths import get_posix_platform_tag, is_windows
from .remote import _extract_tag_from_location
from .signature import SIG_NAME, SUMS_NAME, verify_sums

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def release_platform_tag() -> str:
    """Platform part of release asset names: windows-x64, macos-arm64, macos-x64, linux-x64, linux-arm64."""
    if is_windows():
        return "windows-x64"
    return get_posix_platform_tag().replace("darwin-", "macos-", 1)


def parse_version(text: str) -> tuple[int, int, int, int] | None:
    """Sortable key for X.Y.Z or X.Y.Z-pre (a prerelease sorts before its release); None if malformed."""
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def latest_release_tag(client: Any, mirror: str | None) -> str | None:
    """Tag of the latest (non-prerelease) release, from a client that does not follow redirects."""
    try:
        resp = client.get(rewrite_github_url(POB_AGENT_RELEASES_LATEST_URL, mirror=mirror))
        if resp.status_code in (301, 302, 303, 307, 308):
            return _extract_tag_from_location(resp.headers.get("location", ""))
        if resp.status_code == 200 and "/tag/" in str(resp.url):
            return _extract_tag_from_location(str(resp.url))  # a mirror that follows redirects itself
    except Exception:
        return None
    return None


def asset_url(tag: str, name: str, mirror: str | None) -> str:
    return rewrite_github_url(f"{POB_AGENT_DOWNLOAD_BASE}/{tag}/{name}", mirror=mirror)


def signed_sums(client: Any, tag: str, mirror: str | None) -> dict[str, str] | None:
    """`{file name: sha256}` from a release's SHA256SUMS.txt, only if its signature verifies."""
    sums_resp = client.get(asset_url(tag, SUMS_NAME, mirror))
    sig_resp = client.get(asset_url(tag, SIG_NAME, mirror))
    if sums_resp.status_code != 200 or sig_resp.status_code != 200:
        return None
    if not verify_sums(sums_resp.content, sig_resp.text):
        return None
    out: dict[str, str] = {}
    for line in sums_resp.text.splitlines():
        digest, _, name = line.partition("  ")
        if len(digest) == 64 and name:
            out[name.lstrip("*").strip()] = digest.lower()
    return out
