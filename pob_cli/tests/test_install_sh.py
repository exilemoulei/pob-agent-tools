"""install.sh end to end against a fake release served on localhost.

The release is reached through POB_GITHUB_MIRROR, or, for runs that must look like a download
straight from GitHub, through a curl stand-in that sends github.com URLs to the same server.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from ed25519_sign import TEST_PUBLIC_KEY, sign_sums_text

REPO = Path(__file__).resolve().parents[2]
TAG = "v9.9.9"
GH = "https://github.com/exilemoulei/pob4agents/releases"

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"), reason="POSIX installer"
)


def _platform() -> str:
    os_part = "macos" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"{os_part}-{arch}"


def _build_tar(runs: bool, version: str) -> bytes:
    script = (
        f'#!/bin/sh\n[ "$1" = --version ] && echo "pob, version {version}"\nexit 0\n'.encode()
        if runs
        else b"#!/bin/sh\nexit 1\n"
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in (("pob", script, 0o755), ("_internal/base_library.zip", b"zip", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Release:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.tag = TAG
        self.latest = TAG  # the tag /releases/latest redirects to
        self.requests: list[str] = []

    def publish(
        self,
        *,
        version: str = "9.9.9",
        runs: bool = True,
        sign: bool = True,
        tamper: bool = False,
        wheel: bool = True,
        sdist: bool = False,
    ) -> None:
        self.tag = self.latest = f"v{version}"
        build = f"pob4agents-{version}-{_platform()}.tar.gz"
        files = {build: _build_tar(runs, version)}
        if wheel:
            files[f"pob4agents-{version}-py3-none-any.whl"] = b"fake wheel"
        if sdist:
            files[f"pob4agents-{version}.tar.gz"] = b"fake sdist"
        sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in sorted(files.items())).encode()
        if tamper:
            files[build] += b"!"
        files["SHA256SUMS.txt"] = sums
        if sign:
            files["SHA256SUMS.txt.sig"] = sign_sums_text(sums).encode()
        self.files = files


@pytest.fixture
def release() -> Iterator[tuple[_Release, str]]:
    rel = _Release()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = self.path.lstrip("/")
            rel.requests.append(url)
            if url == f"{GH}/latest":
                self.send_response(302)
                self.send_header("Location", f"{GH}/tag/{rel.latest}")
                self.end_headers()
                return
            name = url.removeprefix(f"{GH}/download/{rel.tag}/")
            data = rel.files.get(name) if name != url else None
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield rel, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _shim(path: Path, body: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def run_installer(tmp_path: Path, release: tuple[_Release, str]):  # type: ignore[no-untyped-def]
    _, mirror = release
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    # Trust only the test key, so a release signed by it installs and nothing else does.
    script, n = re.subn(r"^RELEASE_PUBLIC_KEYS=\(.*\)$", f"RELEASE_PUBLIC_KEYS=({TEST_PUBLIC_KEY})", script, flags=re.M)
    assert n == 1
    installer = tmp_path / "install.sh"
    installer.write_text(script, encoding="utf-8")
    # Stand-ins for uv and pipx: they log calls and own nothing, so the test never touches real installs.
    shims = tmp_path / "shims"
    log = tmp_path / "shim.log"
    for tool in ("uv", "pipx"):
        _shim(shims / tool, f'echo "{tool} $*" >> "{log}"\n')
    # python3 runs the signature check for real but never reaches the user's pip.
    _shim(
        shims / "python3",
        f'if [ "$1" = -m ] && [ "$2" = pip ]; then echo "python3 $*" >> "{log}"; exit 1; fi\nexec "{sys.executable}" "$@"\n',
    )
    # curl that serves github.com from the fake release, so a run needs no POB_GITHUB_MIRROR.
    _shim(
        tmp_path / "direct" / "curl",
        f'for a; do shift; case "$a" in https://github.com/*) a="{mirror}/$a" ;; esac; set -- "$@" "$a"; done\n'
        f'exec "{shutil.which("curl")}" "$@"\n',
    )
    # mv that cannot move the unpacked build into place, as on a full or read-only disk.
    _shim(tmp_path / "failmv" / "mv", f'case "$1" in *.new) exit 1 ;; esac\nexec "{shutil.which("mv")}" "$@"\n')
    # ln that cannot create the pob link.
    _shim(tmp_path / "failln" / "ln", "exit 1\n")
    base_env = {
        k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY") and not k.startswith("POB_")
    }
    base_env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{shims}{os.pathsep}{base_env.get('PATH', '')}",
        POB_GITHUB_MIRROR=mirror,
        POB_INSTALL_DIR=str(tmp_path / "app"),
        POB_BIN_DIR=str(tmp_path / "bin"),
        NO_PROXY="*",
    )

    def run(
        *, direct: bool = False, fail_mv: bool = False, fail_ln: bool = False, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {**base_env, **extra}
        if direct:
            del env["POB_GITHUB_MIRROR"]
            env["PATH"] = f"{tmp_path / 'direct'}{os.pathsep}{env['PATH']}"
        if fail_mv:
            env["PATH"] = f"{tmp_path / 'failmv'}{os.pathsep}{env['PATH']}"
        if fail_ln:
            env["PATH"] = f"{tmp_path / 'failln'}{os.pathsep}{env['PATH']}"
        return subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True, timeout=120)

    run.log = log  # type: ignore[attr-defined]
    return run


@posix_only
def test_installs_the_signed_build(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    proc = run_installer(POB_LANG="zh-CN")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Release signature verified" in proc.stdout
    assert "--- BEGIN USER WELCOME TIPS ---" in proc.stdout
    assert "@流放降临-某雷" in proc.stdout
    link = tmp_path / "bin" / "pob"
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "app" / "pob")
    assert subprocess.run([str(link), "--version"], capture_output=True, text=True).stdout.strip() == "pob, version 9.9.9"
    assert not (tmp_path / "app.new").exists()


@posix_only
def test_upgrade_replaces_an_earlier_python_install(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pob").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    assert run_installer(POB_LANG="en").returncode == 0
    assert (tmp_path / "bin" / "pob").is_symlink()
    assert (tmp_path / "bin" / "pob.old").read_text(encoding="utf-8").endswith("echo old\n")
    # Run again over the existing build (same version): no fresh-install welcome tip.
    reinstall = run_installer(POB_LANG="en")
    assert reinstall.returncode == 0
    assert "BEGIN USER WELCOME TIPS" not in reinstall.stdout
    assert (tmp_path / "app" / "pob").is_file()
    # Upgrade from an older version (9.9.9 -> 10.0.0): no welcome tip; falls back to release URL when changelog is absent.
    release[0].publish(version="10.0.0")
    upgrade = run_installer(POB_LANG="en")
    assert upgrade.returncode == 0
    assert "BEGIN USER WELCOME TIPS" not in upgrade.stdout
    assert "releases/tag/v10.0.0" in upgrade.stdout


@posix_only
def test_a_failed_swap_keeps_the_previous_build(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    assert run_installer().returncode == 0
    (tmp_path / "app" / "marker").write_text("previous", encoding="utf-8")
    proc = run_installer(fail_mv=True)
    assert proc.returncode != 0
    assert "previous install was left as it was" in proc.stderr
    assert (tmp_path / "app" / "marker").read_text(encoding="utf-8") == "previous"
    assert not (tmp_path / "app.old").exists() and not (tmp_path / "app.new").exists()
    assert subprocess.run([str(tmp_path / "bin" / "pob"), "--version"], capture_output=True).returncode == 0


@posix_only
def test_a_failed_link_restores_the_previous_command(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pob").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    proc = run_installer(fail_ln=True)
    assert proc.returncode != 0
    assert (tmp_path / "bin" / "pob").read_text(encoding="utf-8").endswith("echo old\n")
    assert not (tmp_path / "bin" / "pob.old").exists()


@posix_only
@pytest.mark.parametrize(
    ("damage", "message"),
    [("tamper", "does not match SHA256SUMS.txt"), ("unsigned", "refusing to install an unsigned release")],
)
def test_refuses_a_changed_or_unsigned_release(
    release: tuple[_Release, str], run_installer, tmp_path: Path, damage: str, message: str  # type: ignore[no-untyped-def]
) -> None:
    release[0].publish(tamper=damage == "tamper", sign=damage != "unsigned")
    proc = run_installer()
    assert proc.returncode != 0
    assert message in proc.stderr
    assert not (tmp_path / "bin" / "pob").exists()


@posix_only
def test_refuses_a_signature_by_another_key(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    sums = release[0].files["SHA256SUMS.txt"]
    release[0].files["SHA256SUMS.txt.sig"] = sign_sums_text(sums, seed=b"\x09" * 32).encode()
    proc = run_installer()
    assert proc.returncode != 0
    assert "not signed by a pob4agents release key" in proc.stderr


@posix_only
def test_a_mirror_cannot_pick_an_unsigned_old_release(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish(version="0.8.0", sign=False)
    proc = run_installer()
    assert proc.returncode != 0
    assert "installs only straight from GitHub" in proc.stderr
    assert not (tmp_path / "bin" / "pob").exists()


@posix_only
def test_an_old_unsigned_release_installs_from_github(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish(version="0.8.0", sign=False)
    proc = run_installer(direct=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "predates signed releases" in proc.stdout
    assert (tmp_path / "bin" / "pob").is_symlink()


@posix_only
@pytest.mark.parametrize(("latest", "pinned"), [("vlatest", None), ("v0.8", None), ("v9.9.9", "abc.def")])
def test_refuses_a_tag_that_is_not_a_version(
    release: tuple[_Release, str], run_installer, tmp_path: Path, latest: str, pinned: str | None  # type: ignore[no-untyped-def]
) -> None:
    release[0].publish(sign=False)
    release[0].latest = latest
    proc = run_installer(direct=True, **({"POB_VERSION": pinned} if pinned else {}))
    assert proc.returncode != 0
    assert "is not a release tag" in proc.stderr
    assert not any("/download/" in r for r in release[0].requests)


@posix_only
@pytest.mark.parametrize(
    ("direct", "skip", "message"),
    [
        (True, False, "xcode-select --install"),
        (True, True, None),
        (False, True, "a mirror is set"),
    ],
)
def test_without_a_verifier(
    release: tuple[_Release, str], run_installer, tmp_path: Path, direct: bool, skip: bool, message: str | None  # type: ignore[no-untyped-def]
) -> None:
    release[0].publish()
    env = {"POB_VERIFY_WITH": "none", **({"POB_SKIP_SIGNATURE": "1"} if skip else {})}
    proc = run_installer(direct=direct, **env)
    if message is None:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "signature of v9.9.9 is NOT checked" in proc.stdout
        assert (tmp_path / "bin" / "pob").is_symlink()
    else:
        assert proc.returncode != 0
        assert message in proc.stderr
        assert not any("/download/" in r for r in release[0].requests)  # stopped before any download


@posix_only
def test_an_oversized_version_still_needs_a_signature(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # Bash arithmetic wraps 2**64 to 0, which would read as a release from before signing.
    release[0].publish(sign=False)
    release[0].latest = "v18446744073709551616.0.0"
    proc = run_installer(direct=True, POB_VERIFY_WITH="none")
    assert proc.returncode != 0
    assert "No Ed25519 verifier" in proc.stderr
    assert not any("/download/" in r for r in release[0].requests)


@posix_only
def test_falls_back_to_the_verified_wheel(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish(runs=False)
    proc = run_installer()
    assert "does not run on this machine" in proc.stdout, proc.stdout + proc.stderr
    log = run_installer.log.read_text(encoding="utf-8")
    assert re.search(r"^uv tool install --force .*/pob4agents-9\.9\.9-py3-none-any\.whl$", log, re.M), log
    assert "python3 -m pip show pob-cli" in log  # the pre-0.9 name is cleared first
    assert not (tmp_path / "app").exists()


@posix_only
@pytest.mark.parametrize("sdist", [True, False])
def test_without_a_wheel_installs_only_the_listed_sdist(
    release: tuple[_Release, str], run_installer, tmp_path: Path, sdist: bool  # type: ignore[no-untyped-def]
) -> None:
    release[0].publish(runs=False, wheel=False, sdist=sdist)
    proc = run_installer()
    log = run_installer.log.read_text(encoding="utf-8") if run_installer.log.exists() else ""
    if sdist:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert re.search(r"^uv tool install --force .*/pob4agents-9\.9\.9\.tar\.gz$", log, re.M), log
    else:
        assert proc.returncode != 0
        assert "lists no wheel or sdist" in proc.stderr
        assert "uv tool install" not in log and "git+" not in log


def test_installer_carries_the_package_keys() -> None:
    from pob_cli.updater import signature

    text = (REPO / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"^RELEASE_PUBLIC_KEYS=\(([^)]*)\)$", text, re.M)
    assert m, "install.sh must define RELEASE_PUBLIC_KEYS"
    assert tuple(m.group(1).split()) == signature.RELEASE_PUBLIC_KEYS


def test_both_installers_accept_the_same_tags() -> None:
    sh = re.search(r"^TAG_PATTERN='([^']+)'$", (REPO / "install.sh").read_text(encoding="utf-8"), re.M)
    ps = re.search(r"^\$TagPattern = '([^']+)'$", (REPO / "install.ps1").read_text(encoding="utf-8"), re.M)
    assert sh and ps, "both installers must define their release-tag pattern"
    # Bash's ERE `$` is the end of the string; in .NET `$` also matches before a final newline.
    assert ps.group(1) == sh.group(1).removesuffix("$") + r"\z"
    tag = re.compile(sh.group(1))
    assert tag.match("v0.9.0") and tag.match("v1.2.3-rc.1")
    assert not any(tag.match(t) for t in ("vlatest", "v0.8", "v1.2.3/../x", "vabc.def"))


@posix_only
def test_self_test_passes() -> None:
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--self-test"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Ed25519 self-test passed" in proc.stdout
