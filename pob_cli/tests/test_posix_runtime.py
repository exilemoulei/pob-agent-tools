"""macOS/Linux runtime sources (updater/posix_runtime.py): bundled, signed prebuilt download, pinned source."""

from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
from ed25519_sign import TEST_PUBLIC_KEY, sign_sums_text

from pob_cli import __version__
from pob_cli.updater import constants, posix_runtime, signature

REPO = Path(__file__).resolve().parents[2]
BASE = "https://github.com/exilemoulei/pob4agents/releases/download"
LATEST = "https://github.com/exilemoulei/pob4agents/releases/latest"


class _Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None, url: str = "") -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, routes: dict[str, _Resp], seen: list[str]) -> None:
        self.routes = routes
        self.seen = seen

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> _Resp:
        self.seen.append(url)
        return self.routes.get(url, _Resp(404))


def _runtime_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("bin/luajit", "bin/lua-utf8.so"):
            data = b"#!/bin/sh\nexit 0\n"
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release(tag: str, *, sign: bool = True, tamper: bool = False) -> dict[str, _Resp]:
    name = posix_runtime.runtime_asset_name(tag.removeprefix("v"))
    tar = _runtime_tar()
    sums = f"{hashlib.sha256(tar).hexdigest()}  {name}\n".encode()
    routes = {
        f"{BASE}/{tag}/SHA256SUMS.txt": _Resp(200, sums),
        f"{BASE}/{tag}/{name}": _Resp(200, tar + (b"x" if tamper else b"")),
    }
    if sign:
        routes[f"{BASE}/{tag}/SHA256SUMS.txt.sig"] = _Resp(200, sign_sums_text(sums).encode())
    return routes


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, _Resp], list[str]]:
    routes: dict[str, _Resp] = {}
    seen: list[str] = []
    monkeypatch.setattr(signature, "RELEASE_PUBLIC_KEYS", (TEST_PUBLIC_KEY,))
    monkeypatch.setattr(sys.modules["pob_cli.updater"], "_make_http_client", lambda **_kw: _Client(routes, seen))
    monkeypatch.setattr(posix_runtime, "_self_test_runtime", lambda _bin: None)
    monkeypatch.setattr(posix_runtime, "release_platform_tag", lambda: "linux-x64")
    return routes, seen


def test_downloads_own_release_runtime(fake_http: Any, tmp_path: Path) -> None:
    routes, _ = fake_http
    routes.update(_release(f"v{__version__}"))
    got = posix_runtime._try_download_prebuilt_posix_runtime(tmp_path)
    assert got is not None
    assert set(got) == set(posix_runtime.RUNTIME_FILES)
    assert all(p.is_file() for p in got.values())


def test_falls_back_to_latest_release(fake_http: Any, tmp_path: Path) -> None:
    routes, seen = fake_http
    routes[LATEST] = _Resp(302, headers={"location": "https://github.com/exilemoulei/pob4agents/releases/tag/v9.9.9"})
    routes.update(_release("v9.9.9"))
    assert posix_runtime._try_download_prebuilt_posix_runtime(tmp_path) is not None
    assert f"{BASE}/v9.9.9/pob4agents-runtime-9.9.9-linux-x64.tar.gz" in seen


@pytest.mark.parametrize("damage", ["unsigned", "tampered", "wrong-key"])
def test_refuses_unverified_runtime(
    fake_http: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    routes, _ = fake_http
    routes.update(_release(f"v{__version__}", sign=damage != "unsigned", tamper=damage == "tampered"))
    if damage == "wrong-key":
        monkeypatch.setattr(signature, "RELEASE_PUBLIC_KEYS", ("A" * 43 + "=",))
    assert posix_runtime._try_download_prebuilt_posix_runtime(tmp_path) is None


def test_never_asks_for_the_unchecked_legacy_name(fake_http: Any, tmp_path: Path) -> None:
    _, seen = fake_http
    posix_runtime._try_download_prebuilt_posix_runtime(tmp_path)
    assert not any("/pob-runtime-" in url for url in seen)


def test_bundled_runtime_is_staged_as_a_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    for rel in posix_runtime.RUNTIME_FILES:
        (bundle / "runtime" / rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle / "runtime" / rel).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(posix_runtime, "_self_test_runtime", lambda _bin: None)
    got = posix_runtime._bundled_posix_runtime(tmp_path / "staging")
    assert got is not None
    for rel, path in got.items():
        assert path.is_file() and tmp_path / "staging" in path.parents
        assert (bundle / "runtime" / rel).is_file()


def test_not_frozen_means_no_bundle(tmp_path: Path) -> None:
    assert posix_runtime._bundled_posix_runtime(tmp_path) is None


def test_source_tarball_must_match_its_pin(tmp_path: Path) -> None:
    class _Bytes:
        def get(self, _url: str, **_kw: Any) -> _Resp:
            return _Resp(200, b"not the pinned tarball")

    with pytest.raises(constants.UpdateError, match="pinned SHA-256"):
        posix_runtime._download_pinned(_Bytes(), "https://example.invalid/x.tar.gz", "0" * 64)


def test_build_script_pins_match_the_updater() -> None:
    script = (REPO / "pob_cli" / "packaging" / "build_posix_runtime.sh").read_text(encoding="utf-8")

    def pin(name: str) -> str:
        m = re.search(rf'^{name}="([^"]+)"$', script, re.M)
        assert m, name
        return m.group(1)

    assert pin("LUAJIT_COMMIT") == constants.POSIX_LUAJIT_COMMIT
    assert pin("LUAJIT_SHA256") == constants.POSIX_LUAJIT_TARBALL_SHA256
    assert pin("LUAUTF8_TAG") == constants.POSIX_LUAUTF8_TAG
    assert pin("LUAUTF8_SHA256") == constants.POSIX_LUAUTF8_TARBALL_SHA256


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("darwin-arm64", "macos-arm64"), ("darwin-x64", "macos-x64"), ("linux-x64", "linux-x64")],
)
def test_release_platform_names(monkeypatch: pytest.MonkeyPatch, tag: str, expected: str) -> None:
    monkeypatch.setattr(posix_runtime, "get_posix_platform_tag", lambda: tag)
    assert posix_runtime.release_platform_tag() == expected
    assert posix_runtime.runtime_asset_name("1.2.3") == f"pob4agents-runtime-1.2.3-{expected}.tar.gz"
