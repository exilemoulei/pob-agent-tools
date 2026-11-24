"""POSIX (macOS/Linux) native LuaJIT + lua-utf8.so: bundled, verified prebuilt download, or source build.

Order of preference:
1. The runtime inside a standalone pob binary (`<bundle>/runtime/bin`, added by pob.spec).
2. `pob4agents-runtime-<version>-<platform>.tar.gz` from this pob's release (then the latest one),
   trusted only through that release's Ed25519-signed SHA256SUMS.txt and a self-test.
3. Compiling LuaJIT and lua-utf8 from source tarballs pinned by SHA-256.

Releases up to v0.8.0 published no runtime, and pre-0.9 pob asked for `pob-runtime-<platform>.tar.gz`
without any check; neither name is published, so those versions keep compiling from source.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import __version__
from .constants import (
    _ASSET_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    ASSET_MAX_RETRIES,
    POSIX_LUAJIT_TARBALL_SHA256,
    POSIX_LUAJIT_TARBALL_URL,
    POSIX_LUAUTF8_TARBALL_SHA256,
    POSIX_LUAUTF8_TARBALL_URL,
    UpdateError,
    _to_httpx_timeout,
    rewrite_github_url,
)
from .releases import asset_url, latest_release_tag, release_platform_tag, signed_sums
from .remote import _download_bytes_with_retry

RUNTIME_FILES = (Path("bin") / "luajit", Path("bin") / "lua-utf8.so")
# Compound assignment is what stock LuaJIT lacks and PoB >= 2.68 needs; lua-utf8 must load from bin/.
_SELF_TEST_LUA = (
    'local c = 1; c += 41; assert(c == 42); local s = "po"; s ..= "b"; assert(s == "pob"); '
    'local u = require("lua-utf8"); assert(u.len("流放") == 2)'
)


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def runtime_asset_name(version: str, platform: str | None = None) -> str:
    return f"pob4agents-runtime-{version}-{platform or release_platform_tag()}.tar.gz"


def _extract_tar_gz_safely(tar_path: Path, dest_dir: Path, *, strip_components: int = 0) -> None:
    import tarfile

    dest_resolved = dest_dir.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if strip_components > 0:
                if len(parts) <= strip_components:
                    continue
                parts = parts[strip_components:]
            if not parts or any(p in ("..", "") for p in parts):
                continue
            rel = Path(*parts)
            if rel.is_absolute():
                raise UpdateError(f"Unsafe tar member path: {member.name}")
            out = (dest_dir / rel).resolve()
            try:
                out.relative_to(dest_resolved)
            except ValueError as exc:
                raise UpdateError(f"Tar Slip detected: {member.name}") from exc
            out.parent.mkdir(parents=True, exist_ok=True)
            src_f = tf.extractfile(member)
            if src_f is None:
                continue
            with src_f, open(out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            if member.mode & 0o111:
                os.chmod(out, 0o755)


def _self_test_runtime(bin_dir: Path) -> str | None:
    """None when `bin_dir`'s luajit runs PoB's syntax and loads lua-utf8, else the failure text."""
    env = os.environ.copy()
    env["LUA_CPATH"] = f"{bin_dir}/?.so;;"
    try:
        proc = subprocess.run(
            [str(bin_dir / "luajit"), "-e", _SELF_TEST_LUA],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or f"exit {proc.returncode}")[-400:]
    return None


def _bundled_posix_runtime(staging_dir: Path) -> dict[Path, Path] | None:
    """A staged copy of the runtime a standalone pob binary carries, if this is one and it runs here.

    A copy, because bootstrap moves staged files into the engine and the bundle must stay whole.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not (getattr(sys, "frozen", False) and meipass):
        return None
    root = Path(meipass) / "runtime"
    if not all((root / rel).is_file() for rel in RUNTIME_FILES):
        return None
    out = staging_dir / "_posix_bundled"
    shutil.rmtree(out, ignore_errors=True)
    for rel in RUNTIME_FILES:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, out / rel)
        os.chmod(out / rel, 0o755)
    if _self_test_runtime(out / "bin") is not None:
        return None
    return {rel: out / rel for rel in RUNTIME_FILES}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_tags(client: Any, mirror: str | None) -> list[str]:
    """This pob's own release first, then the latest one (a source checkout may be ahead of both)."""
    tags = [f"v{__version__}"]
    latest = latest_release_tag(client, mirror)
    if latest and latest not in tags:
        tags.append(latest)
    return tags


def _try_download_prebuilt_posix_runtime(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path] | None:
    """Download and verify a prebuilt runtime from a pob4agents release; None means build from source."""
    make_client = _pkg()._make_http_client
    try:
        with make_client(timeout=_to_httpx_timeout(_CHECK_TIMEOUT_SPEC), proxy=proxy, follow_redirects=False) as c:
            tags = _release_tags(c, mirror)
        with make_client(timeout=_to_httpx_timeout(_ASSET_TIMEOUT_SPEC), proxy=proxy, follow_redirects=True) as c:
            for tag in tags:
                sums = signed_sums(c, tag, mirror)
                name = runtime_asset_name(tag.removeprefix("v"))
                if not sums or name not in sums:
                    continue
                resp = c.get(asset_url(tag, name, mirror))
                if resp.status_code != 200 or _sha256(resp.content) != sums[name]:
                    continue
                tar_tmp = staging_dir / "_downloads" / name
                tar_tmp.parent.mkdir(parents=True, exist_ok=True)
                tar_tmp.write_bytes(resp.content)
                extract_root = staging_dir / "_posix_prebuilt"
                shutil.rmtree(extract_root, ignore_errors=True)
                extract_root.mkdir(parents=True)
                _extract_tar_gz_safely(tar_tmp, extract_root)
                if not all((extract_root / rel).is_file() for rel in RUNTIME_FILES):
                    continue
                for rel in RUNTIME_FILES:
                    os.chmod(extract_root / rel, 0o755)
                if _self_test_runtime(extract_root / "bin") is not None:
                    # Built for a newer libc than this machine has, for example: compile instead.
                    return None
                return {rel: extract_root / rel for rel in RUNTIME_FILES}
    except Exception:
        return None
    return None


def _download_pinned(client: Any, url: str, sha256: str) -> bytes:
    data = _download_bytes_with_retry(client, url, retries=ASSET_MAX_RETRIES)
    if _sha256(data) != sha256:
        raise UpdateError(f"Source tarball {url} does not match its pinned SHA-256; refusing to build it.")
    return data


def _build_posix_runtime_from_source(
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    """Compile minimal native LuaJIT 2.1 (PoB 2.68+ commit) and lua-utf8.so locally on macOS/Linux."""
    cc_bin = os.environ.get("CC", "").strip() or shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    make_bin = shutil.which("make")
    if not cc_bin or not make_bin:
        hint = (
            "Run `xcode-select --install` to install Apple Command Line Tools."
            if sys.platform == "darwin"
            else "Install build tools (`sudo apt install build-essential` or equivalent)."
        )
        raise UpdateError(
            f"Cannot build native POSIX LuaJIT runtime (missing C compiler or make). {hint}"
        )

    build_timeout = _to_httpx_timeout((10.0, 60.0, 10.0, 10.0))
    lj_url = rewrite_github_url(POSIX_LUAJIT_TARBALL_URL, mirror=mirror)
    u8_url = rewrite_github_url(POSIX_LUAUTF8_TARBALL_URL, mirror=mirror)

    with tempfile.TemporaryDirectory(prefix="pob-posix-build-") as tmp_str:
        tmp = Path(tmp_str)
        lj_tar = tmp / "luajit.tar.gz"
        u8_tar = tmp / "luautf8.tar.gz"
        lj_src = tmp / "luajit-src"
        u8_src = tmp / "luautf8-src"
        lj_src.mkdir()
        u8_src.mkdir()

        with _pkg()._make_http_client(timeout=build_timeout, proxy=proxy, follow_redirects=True) as client:
            lj_tar.write_bytes(_download_pinned(client, lj_url, POSIX_LUAJIT_TARBALL_SHA256))
            u8_tar.write_bytes(_download_pinned(client, u8_url, POSIX_LUAUTF8_TARBALL_SHA256))

        _extract_tar_gz_safely(lj_tar, lj_src, strip_components=1)
        _extract_tar_gz_safely(u8_tar, u8_src, strip_components=1)

        jobs = str(max(1, os.cpu_count() or 4))
        env = os.environ.copy()
        if sys.platform == "darwin":
            env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")

        lj_proc = subprocess.run(
            [make_bin, "-C", str(lj_src / "src"), f"-j{jobs}", "BUILDMODE=static", "amalg"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if lj_proc.returncode != 0:
            tail = (lj_proc.stderr or lj_proc.stdout or "")[-600:]
            raise UpdateError(f"Failed to compile LuaJIT 2.1 (exit {lj_proc.returncode}): {tail}")

        out_bin_dir = staging_dir / "bin"
        out_bin_dir.mkdir(parents=True, exist_ok=True)
        luajit_out = out_bin_dir / "luajit"
        utf8_out = out_bin_dir / "lua-utf8.so"

        shutil.copy2(lj_src / "src" / "luajit", luajit_out)
        os.chmod(luajit_out, 0o755)

        link = ["-bundle", "-undefined", "dynamic_lookup"] if sys.platform == "darwin" else ["-shared"]
        cc_cmd = [
            cc_bin,
            "-O2",
            "-fPIC",
            *link,
            f"-I{lj_src / 'src'}",
            str(u8_src / "lutf8lib.c"),
            "-o",
            str(utf8_out),
        ]
        u8_proc = subprocess.run(cc_cmd, env=env, capture_output=True, text=True, timeout=120)
        if u8_proc.returncode != 0:
            tail = (u8_proc.stderr or u8_proc.stdout or "")[-600:]
            raise UpdateError(f"Failed to compile lua-utf8.so (exit {u8_proc.returncode}): {tail}")
        os.chmod(utf8_out, 0o755)

        failure = _self_test_runtime(out_bin_dir)
        if failure is not None:
            raise UpdateError(f"Compiled POSIX LuaJIT runtime failed self-test: {failure}")

        return {rel: staging_dir / rel for rel in RUNTIME_FILES}


def ensure_posix_runtime(
    engine_dir: Path,
    staging_dir: Path,
    *,
    proxy: str | None = None,
    mirror: str | None = None,
) -> dict[Path, Path]:
    """Ensure macOS/Linux native LuaJIT + lua-utf8.so exist: bundled, verified download, or source build."""
    if _pkg().is_windows():
        return {}
    if os.environ.get("POB_ENGINE_CMD", "").strip() or os.environ.get("POB_LUAJIT", "").strip():
        return {}
    eng = Path(engine_dir)
    if all((eng / rel).is_file() for rel in RUNTIME_FILES):
        return {}

    bundled = _bundled_posix_runtime(staging_dir)
    if bundled:
        return bundled
    prebuilt = _try_download_prebuilt_posix_runtime(staging_dir, proxy=proxy, mirror=mirror)
    if prebuilt:
        return prebuilt
    return _build_posix_runtime_from_source(staging_dir, proxy=proxy, mirror=mirror)
