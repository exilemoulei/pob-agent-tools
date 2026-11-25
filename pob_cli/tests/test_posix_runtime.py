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
from pob_cli.updater import constants, posix_runtime, releases, signature

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


def _runtime_tar(stamp: str | None = None) -> bytes:
    buf = io.BytesIO()
    files = {"bin/luajit": b"#!/bin/sh\nexit 0\n", "bin/lua-utf8.so": b"#!/bin/sh\nexit 0\n"}
    if stamp is not None:
        files["bin/runtime.stamp"] = stamp.encode() + b"\n"
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release(tag: str, *, sign: bool = True, tamper: bool = False, stamp: str | None = None) -> dict[str, _Resp]:
    name = posix_runtime.runtime_asset_name(tag.removeprefix("v"))
    tar = _runtime_tar(stamp)
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
    assert set(got) == {*posix_runtime.RUNTIME_FILES, posix_runtime.RUNTIME_STAMP}
    assert all(p.is_file() for p in got.values())
    assert got[posix_runtime.RUNTIME_STAMP].read_text(encoding="utf-8").strip() == posix_runtime.runtime_pin()


def test_falls_back_to_latest_release(fake_http: Any, tmp_path: Path) -> None:
    routes, seen = fake_http
    routes[LATEST] = _Resp(302, headers={"location": "https://github.com/exilemoulei/pob4agents/releases/tag/v9.9.9"})
    routes.update(_release("v9.9.9"))
    got = posix_runtime._try_download_prebuilt_posix_runtime(tmp_path)
    assert got is not None
    assert f"{BASE}/v9.9.9/pob4agents-runtime-9.9.9-linux-x64.tar.gz" in seen
    # Another release's runtime may be built from another pin: its stamp says so, and a refresh replaces it.
    assert got[posix_runtime.RUNTIME_STAMP].read_text(encoding="utf-8").strip() == "release v9.9.9"
    assert posix_runtime._try_download_prebuilt_posix_runtime(tmp_path, own_release_only=True) is None


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
    assert set(got) == {*posix_runtime.RUNTIME_FILES, posix_runtime.RUNTIME_STAMP}
    for rel, path in got.items():
        assert path.is_file() and tmp_path / "staging" in path.parents
        if rel != posix_runtime.RUNTIME_STAMP:
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
    # A hand-built or released runtime carries the stamp the updater compares against.
    stamp_line = 'printf \'luajit=%s lua-utf8=%s\\n\' "$LUAJIT_COMMIT" "$LUAUTF8_TAG" > "$OUT_DIR/bin/runtime.stamp"'
    assert stamp_line in script
    assert posix_runtime.runtime_pin() == f"luajit={pin('LUAJIT_COMMIT')} lua-utf8={pin('LUAUTF8_TAG')}"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("darwin-arm64", "macos-arm64"), ("darwin-x64", "macos-x64"), ("linux-x64", "linux-x64")],
)
def test_release_platform_names(monkeypatch: pytest.MonkeyPatch, tag: str, expected: str) -> None:
    monkeypatch.setattr(releases, "get_posix_platform_tag", lambda: tag)
    monkeypatch.setattr(releases, "is_windows", lambda: False)
    assert posix_runtime.release_platform_tag() == expected
    assert posix_runtime.runtime_asset_name("1.2.3") == f"pob4agents-runtime-1.2.3-{expected}.tar.gz"


def _installed(eng: Path, stamp: str | None) -> Path:
    for rel in posix_runtime.RUNTIME_FILES:
        (eng / rel).parent.mkdir(parents=True, exist_ok=True)
        (eng / rel).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if stamp is not None:
        (eng / posix_runtime.RUNTIME_STAMP).write_text(stamp + "\n", encoding="utf-8")
    return eng


@pytest.fixture
def posix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules["pob_cli.updater"], "is_windows", lambda: False)
    monkeypatch.delenv("POB_ENGINE_CMD", raising=False)
    monkeypatch.delenv("POB_LUAJIT", raising=False)


def _no_source_build(*_a: Any, **_kw: Any) -> Any:
    raise constants.UpdateError("no compiler here")


def test_current_stamp_needs_no_refresh(posix_env: None, fake_http: Any, tmp_path: Path) -> None:
    eng = _installed(tmp_path / "eng", posix_runtime.runtime_pin())
    _, seen = fake_http
    assert posix_runtime.posix_runtime_is_current(eng)
    assert posix_runtime.ensure_posix_runtime(eng, tmp_path / "staging") == {}
    assert seen == []


@pytest.mark.parametrize("stamp", [None, "luajit=0000000 lua-utf8=0.0.1", "release v9.9.9"])
def test_outdated_or_missing_stamp_refreshes_from_this_pobs_release(
    posix_env: None, fake_http: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str | None
) -> None:
    routes, _ = fake_http
    routes.update(_release(f"v{__version__}"))
    monkeypatch.setattr(posix_runtime, "_build_posix_runtime_from_source", _no_source_build)
    eng = _installed(tmp_path / "eng", stamp)
    assert not posix_runtime.posix_runtime_is_current(eng)
    got = posix_runtime.ensure_posix_runtime(eng, tmp_path / "staging")
    assert set(got) == {*posix_runtime.RUNTIME_FILES, posix_runtime.RUNTIME_STAMP}
    assert got[posix_runtime.RUNTIME_STAMP].read_text(encoding="utf-8").strip() == posix_runtime.runtime_pin()


def test_failed_refresh_keeps_the_installed_runtime(
    posix_env: None, fake_http: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes, seen = fake_http
    # Only another release has a runtime: a refresh must not take it, and the source build fails.
    routes[LATEST] = _Resp(302, headers={"location": "https://github.com/exilemoulei/pob4agents/releases/tag/v9.9.9"})
    routes.update(_release("v9.9.9"))
    monkeypatch.setattr(posix_runtime, "_build_posix_runtime_from_source", _no_source_build)
    eng = _installed(tmp_path / "eng", None)
    assert posix_runtime.ensure_posix_runtime(eng, tmp_path / "staging") == {}
    assert not any("v9.9.9/pob4agents-runtime" in url for url in seen)


def test_missing_runtime_still_fails_loudly(
    posix_env: None, fake_http: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posix_runtime, "_build_posix_runtime_from_source", _no_source_build)
    with pytest.raises(constants.UpdateError, match="no compiler"):
        posix_runtime.ensure_posix_runtime(tmp_path / "eng", tmp_path / "staging")


def test_overridden_runtime_is_left_alone(posix_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_LUAJIT", "/opt/pob/luajit")
    assert posix_runtime.posix_runtime_is_current(tmp_path / "eng")
    assert posix_runtime.ensure_posix_runtime(tmp_path / "eng", tmp_path / "staging") == {}


def test_engine_update_refreshes_the_runtime_but_never_fails_on_it(
    posix_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pob_cli.updater import sync

    staging = tmp_path / "eng" / ".staging"
    stamp = staging / "stamp"
    calls: list[Path] = []

    def refreshed(engine_dir: Path, *_a: Any, **_kw: Any) -> dict[Path, Path]:
        calls.append(engine_dir)
        return {posix_runtime.RUNTIME_STAMP: stamp}

    pkg = sys.modules["pob_cli.updater"]
    kwargs: dict[str, Any] = dict(
        remote_manifest=None,
        pob_needs_update=False,
        to_download=[],
        tools_needs_update=False,
        tools_manifest=None,
        remote_tools_tag="",
        data_needs_update=False,
        data_manifest=None,
        remote_data_tag=None,
        proxy=None,
        mirror=None,
    )
    monkeypatch.setattr(pkg, "ensure_posix_runtime", refreshed)
    assert sync._stage_incremental_updates(staging, **kwargs) == {posix_runtime.RUNTIME_STAMP: stamp}
    assert calls == [tmp_path / "eng"]

    monkeypatch.setattr(pkg, "ensure_posix_runtime", _no_source_build)
    assert sync._stage_incremental_updates(staging, **kwargs) == {}


def test_path_luajit_never_runs_the_engine(posix_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stock LuaJIT cannot run PoB >= 2.68: an engine without its own bin/luajit is not runnable."""
    import pob_cli.updater as upd
    from pob_cli.updater import paths

    stock = tmp_path / "usr" / "bin" / "luajit"
    stock.parent.mkdir(parents=True)
    stock.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(paths.shutil, "which", lambda name: str(stock) if name in ("luajit", str(stock)) else None)
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    (eng / "Data" / "bridge").mkdir(parents=True)
    (eng / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")

    assert upd.resolve_luajit_binary(eng) is None
    assert upd.is_valid_engine_dir(eng) is False
    with pytest.raises(constants.UpdateError, match="stock `luajit` cannot run"):
        upd.build_engine_command(eng)

    monkeypatch.setenv("POB_LUAJIT", str(stock))  # an explicit override is the user's choice
    assert upd.resolve_luajit_binary(eng) == stock
    monkeypatch.delenv("POB_LUAJIT")
    managed = _installed(eng, None) / "bin" / "luajit"
    assert upd.resolve_luajit_binary(eng) == managed
    assert upd.build_engine_command(eng)[0][0] == str(managed)


def test_latest_release_runtime_keeps_the_stamp_it_ships(fake_http: Any, tmp_path: Path) -> None:
    routes, _ = fake_http
    routes[LATEST] = _Resp(302, headers={"location": "https://github.com/exilemoulei/pob4agents/releases/tag/v9.9.9"})
    routes.update(_release("v9.9.9", stamp=posix_runtime.runtime_pin()))
    got = posix_runtime._try_download_prebuilt_posix_runtime(tmp_path)
    assert got is not None
    # Built from the same pins, so the next refresh leaves it alone.
    assert got[posix_runtime.RUNTIME_STAMP].read_text(encoding="utf-8").strip() == posix_runtime.runtime_pin()
