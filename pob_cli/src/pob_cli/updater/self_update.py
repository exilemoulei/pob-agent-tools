"""Newer-release notice and `pob self-update` for pob itself (not the PoB engine).

The notice reads a cache in the runtime dir that a detached `pob _release-check` refreshes at most
once a day, so no command waits on the network for it. `pob self-update` replaces a standalone
build in place with a newer release, after the release's Ed25519-signed SHA256SUMS.txt and the
file's SHA-256 check out. Other installs (Scoop, uv, pipx, pip, a source checkout) get the command
that updates them instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import __version__
from .constants import _ASSET_TIMEOUT_SPEC, _AUTO_CHECK_TIMEOUT_SPEC, UpdateError, _to_httpx_timeout
from .paths import detect_source_checkout_root, is_windows
from .releases import asset_url, is_newer, latest_release_tag, release_platform_tag, signed_sums

CHECK_FILENAME = "pob_release_check.json"
CHECK_INTERVAL_SECONDS = 24 * 3600
REPO_RAW = "https://raw.githubusercontent.com/exilemoulei/pob4agents/main"


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def install_method() -> str:
    """standalone, scoop, source, uv, pipx or pip."""
    if getattr(sys, "frozen", False):
        parts = [p.lower() for p in Path(sys.executable).resolve().parts]
        return "scoop" if "scoop" in parts and "apps" in parts else "standalone"
    if detect_source_checkout_root() is not None:
        return "source"
    prefix = Path(sys.prefix).as_posix().lower()
    if "/uv/tools/" in prefix:
        return "uv"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    return "pip"


def update_command(method: str | None = None) -> str:
    """What updates this install. Installs from before the pob4agents rename are best re-installed."""
    method = method or install_method()
    if method == "standalone":
        return "pob self-update"
    if method == "scoop":
        return "scoop update pob"
    if method == "source":
        return "git pull"
    if is_windows():
        return f"irm {REPO_RAW}/install.ps1 | iex"
    return f"curl -fsSL {REPO_RAW}/install.sh | bash"


# --- Daily check and notice ----------------------------------------------------------------------


def read_check(runtime_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(runtime_dir) / CHECK_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh_check(runtime_dir: Path, *, proxy: str | None = None, mirror: str | None = None) -> str | None:
    """Look up the latest release and cache it with the time of the check."""
    timeout = _to_httpx_timeout(_AUTO_CHECK_TIMEOUT_SPEC)
    with _pkg()._make_http_client(timeout=timeout, proxy=proxy, follow_redirects=False) as client:
        tag = latest_release_tag(client, mirror)
    path = Path(runtime_dir) / CHECK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"checkedAt": time.time(), "latest": tag}), encoding="utf-8")
    os.replace(tmp, path)
    return tag


def notice_enabled() -> bool:
    return not (os.environ.get("POB_OFFLINE", "").strip() or os.environ.get("POB_NO_UPDATE_NOTICE", "").strip())


def pending_notice(runtime_dir: Path, now: float | None = None) -> tuple[str | None, bool]:
    """(a newer release tag to announce or None, whether the cached check is due for a refresh)."""
    data = read_check(runtime_dir)
    checked = float(data.get("checkedAt") or 0.0)
    due = (now or time.time()) - checked >= CHECK_INTERVAL_SECONDS
    latest = data.get("latest")
    newer = latest if isinstance(latest, str) and is_newer(latest, __version__) else None
    return newer, due


def notice_text(tag: str) -> str:
    return f"pob {tag.lstrip('v')} is available (this is {__version__}). Update: {update_command()}"


def spawn_background_check(runtime_dir: Path) -> None:
    """Refresh the cache in a detached `pob _release-check`, so the current command never waits."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_release-check"]
    else:
        cmd = [sys.executable, "-c", "from pob_cli.cli import main; main()", "_release-check"]
    cmd += ["--runtime-dir", str(runtime_dir)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if is_windows():
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError:
        pass


# --- Self-update of a standalone build -----------------------------------------------------------


def build_asset_name(version: str, platform: str | None = None) -> str:
    platform = platform or release_platform_tag()
    ext = "zip" if platform.startswith("windows") else "tar.gz"
    return f"pob4agents-{version}-{platform}.{ext}"


def app_dir() -> Path:
    """Directory of the running standalone build (`pob` / `pob.exe` and `_internal/`)."""
    return Path(sys.executable).resolve().parent


def _extract(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not (dest / name).resolve().is_relative_to(dest_resolved):
                    raise UpdateError(f"Unsafe path in {archive.name}: {name}")
            zf.extractall(dest)
        return
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest, filter="data")


def download_release(
    staging: Path,
    *,
    tag: str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[str, Any]:
    """Download, verify and unpack a release build into `staging/new`; return what was installed.

    `{"tag", "version", "dir"}`, or `{"tag", "upToDate": True}` when nothing newer exists.
    """
    make_client = _pkg()._make_http_client
    with make_client(timeout=_to_httpx_timeout(_AUTO_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=False) as c:
        target = tag or latest_release_tag(c, mirror)
    if not target:
        raise UpdateError("Could not find the latest pob4agents release (offline? try --proxy or --mirror).")
    target = f"v{target.lstrip('v')}"
    version = target[1:]
    if tag is None and not is_newer(version, __version__):
        return {"tag": target, "upToDate": True}
    name = build_asset_name(version)
    with make_client(timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as c:
        sums = signed_sums(c, target, mirror)
        if sums is None:
            raise UpdateError(f"{target} has no SHA256SUMS.txt signed by a pob4agents release key; not installing it.")
        if name not in sums:
            raise UpdateError(f"{target} has no standalone build for {release_platform_tag()} ({name}).")
        resp = c.get(asset_url(target, name, mirror))
        if resp.status_code != 200:
            raise UpdateError(f"Could not download {name} (HTTP {resp.status_code}).")
        if hashlib.sha256(resp.content).hexdigest() != sums[name]:
            raise UpdateError(f"{name} does not match the signed SHA256SUMS.txt; not installing it.")
    archive = staging / name
    staging.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(resp.content)
    new_dir = staging / "new"
    shutil.rmtree(new_dir, ignore_errors=True)
    new_dir.mkdir()
    _extract(archive, new_dir)
    exe = new_dir / ("pob.exe" if is_windows() else "pob")
    proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or version not in proc.stdout:
        raise UpdateError(f"The downloaded {target} build does not run here: {(proc.stderr or proc.stdout)[-300:]}")
    return {"tag": target, "version": version, "dir": new_dir}


def swap_posix(current: Path, new_dir: Path) -> None:
    """Replace `current` with `new_dir`; running processes keep their already-open files."""
    old = current.with_name(current.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    os.replace(current, old)
    try:
        os.replace(new_dir, current)
    except OSError:
        os.replace(old, current)
        raise
    shutil.rmtree(old, ignore_errors=True)


def windows_swap_script(pid: int, current: Path, new_dir: Path) -> str:
    """PowerShell that waits for this process to exit, then swaps the build directories.

    Windows keeps a running pob.exe's directory locked, so the swap happens after exit.
    """

    def q(p: Path) -> str:
        return "'" + str(p).replace("'", "''") + "'"

    old = current.with_name(current.name + ".old")
    return (
        f"$ErrorActionPreference = 'Stop'; "
        f"try {{ Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}; "
        f"if (Test-Path -LiteralPath {q(old)}) {{ Remove-Item -LiteralPath {q(old)} -Recurse -Force }}; "
        f"Move-Item -LiteralPath {q(current)} -Destination {q(old)}; "
        f"try {{ Move-Item -LiteralPath {q(new_dir)} -Destination {q(current)} }} "
        f"catch {{ Move-Item -LiteralPath {q(old)} -Destination {q(current)}; throw }}; "
        f"Remove-Item -LiteralPath {q(old)} -Recurse -Force -ErrorAction SilentlyContinue"
    )


def schedule_windows_swap(current: Path, new_dir: Path) -> None:
    import base64

    script = windows_swap_script(os.getpid(), current, new_dir)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
