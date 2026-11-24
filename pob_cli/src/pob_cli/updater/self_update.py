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
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

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


def _checked_at(value: Any) -> float:
    """The cache's check time. Anything but a finite number counts as never checked, so a damaged
    cache is refreshed instead of breaking the command that reads it."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    try:
        return float(value) if math.isfinite(value) else 0.0
    except OverflowError:  # an int too large for a float
        return 0.0


def pending_notice(runtime_dir: Path, now: float | None = None) -> tuple[str | None, bool]:
    """(a newer release tag to announce or None, whether the cached check is due for a refresh)."""
    data = read_check(runtime_dir)
    checked = _checked_at(data.get("checkedAt"))
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


def _is_abs_archive_path(raw: str) -> bool:
    if not raw:
        return False
    if raw.startswith(("/", "\\")):
        return True
    return len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()


def _validate_tar_members(archive_name: str, members: list[tarfile.TarInfo], dest_resolved: Path) -> None:
    seen_symlinks: set[Path] = set()
    for member in members:
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise UpdateError(f"Unsafe special entry in {archive_name}: {member.name}")
        norm_name = member.name.replace("\\", "/")
        if _is_abs_archive_path(member.name) or ".." in Path(norm_name).parts:
            raise UpdateError(f"Unsafe path in {archive_name}: {member.name}")
        target = (dest_resolved / member.name).resolve()
        if not target.is_relative_to(dest_resolved):
            raise UpdateError(f"Unsafe path in {archive_name}: {member.name}")
        if any(parent in seen_symlinks for parent in target.parents):
            raise UpdateError(f"Unsafe symlink traversal in {archive_name}: {member.name}")
        if member.issym():
            if _is_abs_archive_path(member.linkname):
                raise UpdateError(f"Unsafe symlink in {archive_name}: {member.name} -> {member.linkname}")
            if not (target.parent / member.linkname).resolve().is_relative_to(dest_resolved):
                raise UpdateError(f"Unsafe symlink in {archive_name}: {member.name} -> {member.linkname}")
            seen_symlinks.add(target)
        elif member.islnk():
            norm_link = member.linkname.replace("\\", "/")
            if _is_abs_archive_path(member.linkname) or ".." in Path(norm_link).parts:
                raise UpdateError(f"Unsafe hardlink in {archive_name}: {member.name} -> {member.linkname}")
            if not (dest_resolved / member.linkname).resolve().is_relative_to(dest_resolved):
                raise UpdateError(f"Unsafe hardlink in {archive_name}: {member.name} -> {member.linkname}")
        member.mode &= 0o777


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
        if getattr(tarfile, "data_filter", None) is not None:
            tf.extractall(dest, filter="data")
            return
        members = tf.getmembers()
        _validate_tar_members(archive.name, members, dest_resolved)
        tf.extractall(dest, members=members)


@contextmanager
def _network_errors(what: str) -> Iterator[None]:
    """Turn transport failures (refused, timed out, bad proxy) into one clean UpdateError line."""
    import httpx

    try:
        yield
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise UpdateError(f"{what} failed: {detail} (offline? try --proxy or --mirror).") from exc


def _unpack_and_check(staging: Path, name: str, data: bytes, target: str, version: str) -> Path:
    """Unpack a verified archive into `staging/new` and make sure its pob runs and is `version`."""
    new_dir = staging / "new"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        archive = staging / name
        archive.write_bytes(data)
        shutil.rmtree(new_dir, ignore_errors=True)
        new_dir.mkdir()
        _extract(archive, new_dir)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise UpdateError(f"Could not unpack {name} into {staging}: {exc}") from exc
    exe = new_dir / ("pob.exe" if is_windows() else "pob")
    if not exe.is_file():
        raise UpdateError(f"{name} has no {exe.name} at its root; not installing it.")
    try:
        proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"The downloaded {target} build does not run here: {exc}") from exc
    # `pob, version X`: compare the last word exactly, so 0.1.1 does not pass for 0.1.10.
    if proc.returncode != 0 or proc.stdout.split()[-1:] != [version]:
        raise UpdateError(f"The downloaded {target} build does not run here: {(proc.stderr or proc.stdout)[-300:]}")
    return new_dir


def download_release(
    staging: Path,
    *,
    tag: str | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[str, Any]:
    """Download, verify and unpack a release build into `staging/new`; return what was installed.

    `{"tag", "version", "dir"}`, or `{"tag", "upToDate": True}` when nothing newer exists. Every
    failure, network and disk included, is an UpdateError.
    """
    make_client = _pkg()._make_http_client
    with (
        _network_errors("Looking up the latest pob4agents release"),
        make_client(timeout=_to_httpx_timeout(_AUTO_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=False) as c,
    ):
        target = tag or latest_release_tag(c, mirror)
    if not target:
        raise UpdateError("Could not find the latest pob4agents release (offline? try --proxy or --mirror).")
    target = f"v{target.lstrip('v')}"
    version = target[1:]
    if tag is None and not is_newer(version, __version__):
        return {"tag": target, "upToDate": True}
    name = build_asset_name(version)
    with (
        _network_errors(f"Downloading {target}"),
        make_client(timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as c,
    ):
        sums = signed_sums(c, target, mirror)
        if sums is None:
            raise UpdateError(f"{target} has no SHA256SUMS.txt signed by a pob4agents release key; not installing it.")
        if name not in sums:
            raise UpdateError(f"{target} has no standalone build for {release_platform_tag()} ({name}).")
        resp = c.get(asset_url(target, name, mirror))
        if resp.status_code != 200:
            raise UpdateError(f"Could not download {name} (HTTP {resp.status_code}).")
        data = resp.content
    if hashlib.sha256(data).hexdigest() != sums[name]:
        raise UpdateError(f"{name} does not match the signed SHA256SUMS.txt; not installing it.")
    new_dir = _unpack_and_check(staging, name, data, target, version)
    return {"tag": target, "version": version, "dir": new_dir}


def new_staging(current: Path) -> Path:
    """A staging directory of this run's own next to `current`, so two updates never share files."""
    try:
        return Path(tempfile.mkdtemp(prefix=current.name + ".update-", dir=current.parent))
    except OSError as exc:
        raise UpdateError(f"Cannot write next to {current}: {exc}") from exc


def _lock(fh: IO[bytes], *, release: bool = False) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def update_lock(current: Path) -> Iterator[None]:
    """Hold an exclusive lock next to `current` for one update's download, verify and swap.

    The OS drops the lock when the process ends, so a crashed update never leaves it stuck. The lock
    file stays in place: deleting it would let a third run lock a new file while a second holds the old one.
    """
    try:
        fh = open(current.with_name(current.name + ".update.lock"), "a+b")
    except OSError as exc:
        raise UpdateError(f"Cannot write next to {current}: {exc}") from exc
    with fh:
        try:
            _lock(fh)
        except OSError as exc:
            raise UpdateError(f"Another pob self-update is running for {current}; let it finish first.") from exc
        try:
            yield
        finally:
            try:
                _lock(fh, release=True)
            except OSError:
                pass


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


# --- Windows: swap after exit --------------------------------------------------------------------


def swap_result_path(current: Path) -> Path:
    """Where the Windows swap helper records how the swap went, for the next `pob` run to report."""
    return current.with_name(current.name + ".swap-result.json")


def take_swap_result(current: Path) -> dict[str, Any] | None:
    """Read and remove the helper's result; None when there is none."""
    path = swap_result_path(current)
    try:
        text = path.read_text(encoding="utf-8-sig")  # Windows PowerShell 5.1 writes a BOM
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {"ok": False, "error": f"unreadable {path.name}"}


def cwd_is_inside(current: Path) -> bool:
    """Windows cannot move a folder that is some process's working directory."""
    try:
        return Path.cwd().resolve().is_relative_to(current.resolve())
    except OSError:
        return False


def windows_swap_script(pid: int, current: Path, new_dir: Path, version: str) -> str:
    """PowerShell (5.1 and 7) that swaps the build directories once no pob from `current` runs.

    Windows keeps a running pob.exe's directory locked, so the swap waits for this process and every
    other process started from the folder (the daemon replies to `stop` before it exits), retries
    the move with backoff, and records the outcome in swap_result_path() because nobody sees its output.
    """

    def q(p: Path | str) -> str:
        return "'" + str(p).replace("'", "''") + "'"

    staging = new_dir.parent
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$current = {q(current)}; $old = {q(current.with_name(current.name + '.old'))}",
            f"$new = {q(new_dir)}; $staging = {q(staging)}; $result = {q(swap_result_path(current))}",
            "$prefix = $current.TrimEnd('\\') + '\\'",
            "function Write-Result($ok, $msg) {",
            f"  @{{ ok = $ok; version = {q(version)}; error = $msg }} | ConvertTo-Json -Compress |"
            " Set-Content -LiteralPath $result -Encoding UTF8",
            "}",
            "function Get-Busy {",
            "  @(Get-Process -ErrorAction SilentlyContinue | Where-Object {",
            "    $proc = $_; $path = $null",
            "    try { $path = $proc.Path } catch {}",
            f"    $proc.Id -eq {int(pid)} -or ($path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))",
            "  })",
            "}",
            "try {",
            "  $deadline = (Get-Date).AddSeconds(120)",
            "  while ((Get-Busy).Count -gt 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }",
            "  $delay = 250",
            "  for ($i = 1; ; $i++) {",
            "    try {",
            "      if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }",
            "      Move-Item -LiteralPath $current -Destination $old",
            "      break",
            "    } catch {",
            "      if ($i -ge 7) { throw }",
            "      Start-Sleep -Milliseconds $delay; $delay *= 2",
            "    }",
            "  }",
            "  try { Move-Item -LiteralPath $new -Destination $current }",
            "  catch { Move-Item -LiteralPath $old -Destination $current; throw }",
            "  Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue",
            "  Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue",
            "  Write-Result $true ''",
            "} catch {",
            "  Write-Result $false $_.Exception.Message",
            "  # Keep the download only when the rollback failed too, so the user can recover from it.",
            "  if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }",
            "  exit 1",
            "}",
        ]
    )


def schedule_windows_swap(current: Path, new_dir: Path, version: str) -> None:
    import base64

    script = windows_swap_script(os.getpid(), current, new_dir, version)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            # Outside the folder it moves; a working directory inside it would lock it.
            cwd=str(current.parent),
        )
    except OSError as exc:
        raise UpdateError(f"Could not start PowerShell to finish the update: {exc}") from exc
