"""install.sh end to end against a fake release served on localhost through POB_GITHUB_MIRROR."""

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

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"), reason="POSIX installer"
)


def _platform() -> str:
    os_part = "macos" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"{os_part}-{arch}"


def _build_tar(runs: bool) -> bytes:
    script = b'#!/bin/sh\n[ "$1" = --version ] && echo "pob, version 9.9.9"\nexit 0\n' if runs else b"#!/bin/sh\nexit 1\n"
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

    def publish(self, *, runs: bool = True, sign: bool = True, tamper: bool = False) -> None:
        files = {
            f"pob4agents-9.9.9-{_platform()}.tar.gz": _build_tar(runs),
            "pob_cli-9.9.9-py3-none-any.whl": b"fake wheel",
        }
        sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in sorted(files.items())).encode()
        if tamper:
            files[f"pob4agents-9.9.9-{_platform()}.tar.gz"] += b"!"
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
            if url == f"{GH}/latest":
                self.send_response(302)
                self.send_header("Location", f"{GH}/tag/{TAG}")
                self.end_headers()
                return
            name = url.removeprefix(f"{GH}/download/{TAG}/")
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


@pytest.fixture
def run_installer(tmp_path: Path, release: tuple[_Release, str]):  # type: ignore[no-untyped-def]
    _, mirror = release
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    script, n = re.subn(r"^RELEASE_PUBLIC_KEYS=\(\)$", f"RELEASE_PUBLIC_KEYS=({TEST_PUBLIC_KEY})", script, flags=re.M)
    assert n == 1
    installer = tmp_path / "install.sh"
    installer.write_text(script, encoding="utf-8")
    # Stand-ins for uv and pipx: they log calls and own nothing, so the test never touches real installs.
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "shim.log"
    for tool in ("uv", "pipx"):
        (shims / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n', encoding="utf-8")
        (shims / tool).chmod(0o755)
    env = {
        k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY") and not k.startswith("POB_")
    }
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{shims}{os.pathsep}{env.get('PATH', '')}",
        POB_GITHUB_MIRROR=mirror,
        POB_INSTALL_DIR=str(tmp_path / "app"),
        POB_BIN_DIR=str(tmp_path / "bin"),
        NO_PROXY="*",
    )

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(installer)], env=env, capture_output=True, text=True, timeout=120)

    run.log = log  # type: ignore[attr-defined]
    return run


def test_installs_the_signed_build(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    proc = run_installer()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Release signature verified" in proc.stdout
    link = tmp_path / "bin" / "pob"
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "app" / "pob")
    assert subprocess.run([str(link), "--version"], capture_output=True, text=True).stdout.strip() == "pob, version 9.9.9"
    assert not (tmp_path / "app.new").exists()


def test_upgrade_replaces_an_earlier_python_install(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pob").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    assert run_installer().returncode == 0
    assert (tmp_path / "bin" / "pob").is_symlink()
    assert (tmp_path / "bin" / "pob.old").read_text(encoding="utf-8").endswith("echo old\n")
    # Run again over the existing build: the link and the directory are replaced in place.
    assert run_installer().returncode == 0
    assert (tmp_path / "app" / "pob").is_file()


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


def test_refuses_a_signature_by_another_key(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish()
    sums = release[0].files["SHA256SUMS.txt"]
    release[0].files["SHA256SUMS.txt.sig"] = sign_sums_text(sums, seed=b"\x09" * 32).encode()
    proc = run_installer()
    assert proc.returncode != 0
    assert "not signed by a pob4agents release key" in proc.stderr


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the fallback needs python3 >= 3.11 on PATH")
def test_falls_back_to_the_verified_wheel(release: tuple[_Release, str], run_installer, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    release[0].publish(runs=False)
    proc = run_installer()
    assert "does not run on this machine" in proc.stdout, proc.stdout + proc.stderr
    log = run_installer.log.read_text(encoding="utf-8")
    assert re.search(r"^uv tool install --force .*/pob_cli-9\.9\.9-py3-none-any\.whl$", log, re.M), log
    assert not (tmp_path / "app").exists()


def test_installer_carries_the_package_keys() -> None:
    from pob_cli.updater import signature

    text = (REPO / "install.sh").read_text(encoding="utf-8")
    m = re.search(r"^RELEASE_PUBLIC_KEYS=\(([^)]*)\)$", text, re.M)
    assert m, "install.sh must define RELEASE_PUBLIC_KEYS"
    assert tuple(m.group(1).split()) == signature.RELEASE_PUBLIC_KEYS


def test_self_test_passes() -> None:
    proc = subprocess.run(["bash", str(REPO / "install.sh"), "--self-test"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Ed25519 self-test passed" in proc.stdout
