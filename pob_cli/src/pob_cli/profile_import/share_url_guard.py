"""Outbound checks for the generic share-URL fallback: public https hosts only, on every redirect hop.

The fallback GETs whatever URL a caller passes to `pob load`, so without these checks it could be
pointed at the local daemon, a LAN service, or a cloud metadata endpoint (169.254.169.254).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

import httpx

from ..errors import DaemonRequestError

MAX_REDIRECTS = 5
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
_BLOCKED_HOSTS = frozenset({"api.github.com", "localhost", "localhost.localdomain"})


def _resolve_host(host: str) -> list[str]:
    """Addresses `host` resolves to; empty when it does not resolve (the GET then fails on its own)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _FAKE_IP_NET:
        # Clash / sing-box "fake-ip" DNS answers every name from 198.18.0.0/15; the proxy resolves the
        # real host, so treat it as public or every share link breaks for those users.
        return True
    return ip.is_global and not ip.is_multicast


def check_public_https_url(url: str) -> None:
    """Refuse (exit 1) anything but an https URL whose host is public and is not api.github.com."""
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or not host:
        raise DaemonRequestError(
            f"Refusing to fetch {url!r}: only https share links are supported. "
            "Paste the share code itself, or use a pobb.in / poe.ninja / pastebin link."
        )
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise DaemonRequestError(f"Refusing to fetch {url!r}: host {host!r} is not allowed.")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = _resolve_host(host)
    for addr in addresses:
        if not _is_public_ip(addr):
            raise DaemonRequestError(
                f"Refusing to fetch {url!r}: host {host!r} resolves to a private, loopback or link-local address."
            )


def get_public_https(client: httpx.Client, url: str) -> httpx.Response:
    """GET `url`, following up to MAX_REDIRECTS redirects by hand so every hop is checked first."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_public_https_url(current)
        resp = client.get(current, follow_redirects=False)
        if not resp.is_redirect:
            return resp
        location = resp.headers.get("location") or ""
        if not location:
            return resp
        current = urllib.parse.urljoin(str(resp.url), location)
    raise DaemonRequestError(f"Refusing to fetch {url!r}: more than {MAX_REDIRECTS} redirects.")
