"""Release asset names, packaging, checksums and verification (pob_cli/packaging/release_tools.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
from ed25519_sign import TEST_PUBLIC_KEY, sign_sums_text

REPO = Path(__file__).resolve().parents[2]
VERSION = "0.9.0"
KEYS = (TEST_PUBLIC_KEY,)


@pytest.fixture(scope="module")
def rt() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_tools", REPO / "pob_cli" / "packaging" / "release_tools.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_dist(root: Path) -> Path:
    dist = root / "dist" / "pob"
    (dist / "_internal").mkdir(parents=True)
    (dist / "pob.exe").write_bytes(b"MZ-fake-pob")
    (dist / "_internal" / "cert.pem").write_text("dummy", encoding="utf-8")
    return dist


def _fake_posix(root: Path) -> tuple[Path, Path]:
    dist = root / "posix" / "pob"
    (dist / "_internal" / "Python.framework").mkdir(parents=True)
    (dist / "pob").write_bytes(b"\x7fELF-fake-pob")
    (dist / "pob").chmod(0o755)
    (dist / "_internal" / "Python.framework" / "Python").write_bytes(b"lib")
    os.symlink("Python.framework/Python", dist / "_internal" / "Python")
    runtime = root / "posix" / "runtime"
    (runtime / "bin").mkdir(parents=True)
    for name in ("luajit", "lua-utf8.so"):
        (runtime / "bin" / name).write_bytes(name.encode())
        (runtime / "bin" / name).chmod(0o755)
    return dist, runtime


def _full_release(rt: ModuleType, root: Path, platforms: tuple[str, ...] = ("windows-x64",)) -> Path:
    out = root / "release-assets"
    rt.pack_windows(_fake_dist(root), out, VERSION)
    for platform in platforms[1:]:
        dist, runtime = _fake_posix(root / platform)
        rt.pack_posix(dist, runtime, out, VERSION, platform)
    for name in rt.python_asset_names(VERSION):
        (out / name).write_bytes(b"python-dist " + name.encode())
    sums = rt.write_sums(out)
    (out / "SHA256SUMS.txt.sig").write_text(sign_sums_text(sums.read_bytes()), encoding="ascii")
    return out


def test_windows_names_keep_the_legacy_ones(rt: ModuleType) -> None:
    assert rt.platform_asset_names(VERSION, "windows-x64") == [
        "pob4agents-0.9.0-windows-x64.zip",
        "pob4agents-windows-x64.zip",
        "pob-v0.9.0-windows-x64.zip",  # releases up to v0.8.0; install.ps1 pins and saved scripts
        "pob-windows-x64.zip",  # README /releases/latest/download links
    ]
    with pytest.raises(ValueError):
        rt.check_version("v0.9.0")


def test_pack_windows_is_reproducible_and_aliases_match(rt: ModuleType, tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path)
    zip_a, sha_a = rt.pack_windows(dist, tmp_path / "a", VERSION)
    _, sha_b = rt.pack_windows(dist, tmp_path / "b", VERSION)
    assert sha_a == sha_b
    with zipfile.ZipFile(zip_a) as zf:
        assert set(zf.namelist()) == {"pob.exe", "_internal/cert.pem"}
    for alias in rt.platform_asset_names(VERSION, "windows-x64")[1:]:
        assert (tmp_path / "a" / alias).read_bytes() == zip_a.read_bytes()


def test_sums_are_lf_coreutils_format(rt: ModuleType, tmp_path: Path) -> None:
    out = _full_release(rt, tmp_path)
    raw = (out / "SHA256SUMS.txt").read_bytes()
    assert b"\r" not in raw
    sums = rt.parse_sums(raw.decode())
    assert set(sums) == rt.expected_assets(VERSION, ["windows-x64"]) - {"SHA256SUMS.txt", "SHA256SUMS.txt.sig"}


def test_verify_accepts_a_complete_release(rt: ModuleType, tmp_path: Path) -> None:
    assert rt.verify(_full_release(rt, tmp_path), VERSION, ["windows-x64"], KEYS) == []


def test_verify_needs_a_known_key(rt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _full_release(rt, tmp_path)
    mod = rt.signature_module()
    monkeypatch.setattr(rt, "signature_module", lambda: mod)
    # No shipped key at all: nothing can verify this release.
    monkeypatch.setattr(mod, "RELEASE_PUBLIC_KEYS", ())
    assert any("no release public key" in p for p in rt.verify(out, VERSION, ["windows-x64"]))
    # The key that signed it: no problems. A shipped key that did not sign it: the signature is rejected.
    monkeypatch.setattr(mod, "RELEASE_PUBLIC_KEYS", KEYS)
    assert rt.verify(out, VERSION, ["windows-x64"]) == []


@pytest.mark.parametrize("damage", ["missing", "extra", "tampered", "alias", "unsigned", "resigned-sums"])
def test_verify_reports_damage(rt: ModuleType, tmp_path: Path, damage: str) -> None:
    out = _full_release(rt, tmp_path)
    if damage == "missing":
        (out / "pob-windows-x64.zip").unlink()
    elif damage == "extra":
        (out / "notes.txt").write_text("x", encoding="utf-8")
    elif damage == "tampered":
        (out / "pob4agents-0.9.0.tar.gz").write_bytes(b"swapped")
    elif damage == "alias":
        (out / "pob4agents-windows-x64.zip").write_bytes(b"other")
        sums = rt.write_sums(out)
        (out / "SHA256SUMS.txt.sig").write_text(sign_sums_text(sums.read_bytes()), encoding="ascii")
    elif damage == "unsigned":
        (out / "SHA256SUMS.txt.sig").unlink()
    else:
        # A swapped file with a matching, but unsigned, SHA256SUMS.txt: what a hostile mirror can do.
        (out / "pob4agents-0.9.0.tar.gz").write_bytes(b"swapped")
        rt.write_sums(out)
    problems = rt.verify(out, VERSION, ["windows-x64"], KEYS)
    assert problems, damage


def test_scoop_manifest_points_at_new_name(rt: ModuleType, tmp_path: Path) -> None:
    manifest = tmp_path / "pob.json"
    manifest.write_text((REPO / "bucket" / "pob.json").read_text(encoding="utf-8"), encoding="utf-8")
    rt.update_scoop_manifest(manifest, VERSION, "AB" * 32)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    base = "https://github.com/exilemoulei/pob4agents/releases/download"
    assert data["version"] == VERSION
    assert data["bin"] == "pob.exe"
    assert data["architecture"]["64bit"] == {
        "url": f"{base}/v0.9.0/pob4agents-0.9.0-windows-x64.zip",
        "hash": "ab" * 32,
    }
    assert data["autoupdate"]["architecture"]["64bit"]["url"] == (
        f"{base}/v$version/pob4agents-$version-windows-x64.zip"
    )


def test_cli_verify_exit_codes(rt: ModuleType, tmp_path: Path) -> None:
    out = _full_release(rt, tmp_path)
    args = ["verify", "--dir", str(out), "--version", VERSION, "--platforms", "windows-x64"]
    args += ["--public-key", TEST_PUBLIC_KEY]
    assert rt.main(args) == 0
    (out / "pob4agents-0.9.0-windows-x64.zip").unlink()
    assert rt.main(args) == 1


def test_notes_name_the_wheel_and_attestations(rt: ModuleType) -> None:
    notes = rt.release_notes(VERSION)
    assert "releases/download/v0.9.0/pob4agents-0.9.0-py3-none-any.whl" in notes
    assert "gh attestation verify" in notes


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_pack_posix_is_reproducible_and_keeps_modes_and_links(rt: ModuleType, tmp_path: Path) -> None:
    dist, runtime = _fake_posix(tmp_path)
    tar_a, sha_a = rt.pack_posix(dist, runtime, tmp_path / "a", VERSION, "macos-arm64")
    _, sha_b = rt.pack_posix(dist, runtime, tmp_path / "b", VERSION, "macos-arm64")
    assert sha_a == sha_b
    assert tar_a.name == "pob4agents-0.9.0-macos-arm64.tar.gz"
    assert (tmp_path / "a" / "pob4agents-macos-arm64.tar.gz").read_bytes() == tar_a.read_bytes()
    with tarfile.open(tar_a) as tf:
        members = {m.name: m for m in tf.getmembers()}
    assert members["pob"].mode == 0o755 and members["pob"].uid == 0 and members["pob"].mtime == 0
    assert members["_internal/Python"].issym()
    assert members["_internal/Python.framework/Python"].mode == 0o644
    with tarfile.open(tmp_path / "a" / "pob4agents-runtime-0.9.0-macos-arm64.tar.gz") as tf:
        assert sorted(tf.getnames()) == ["bin", "bin/lua-utf8.so", "bin/luajit"]

    # build_posix_runtime.sh writes a stamp; the runtime archive ships it so installs keep it.
    (runtime / "bin" / "runtime.stamp").write_text("luajit=abc lua-utf8=0.1.6\n", encoding="utf-8")
    rt.pack_posix(dist, runtime, tmp_path / "c", VERSION, "macos-arm64")
    with tarfile.open(tmp_path / "c" / "pob4agents-runtime-0.9.0-macos-arm64.tar.gz") as tf:
        assert sorted(tf.getnames()) == ["bin", "bin/lua-utf8.so", "bin/luajit", "bin/runtime.stamp"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_verify_accepts_every_platform(rt: ModuleType, tmp_path: Path) -> None:
    out = _full_release(rt, tmp_path, rt.PLATFORMS)
    assert rt.verify(out, VERSION, list(rt.PLATFORMS), KEYS) == []
    assert "pob4agents-runtime-0.9.0-linux-arm64.tar.gz" in rt.expected_assets(VERSION, list(rt.PLATFORMS))


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_verify_rejects_a_build_without_executable_pob(rt: ModuleType, tmp_path: Path) -> None:
    dist, runtime = _fake_posix(tmp_path)
    (dist / "pob").chmod(0o644)
    out = tmp_path / "out"
    rt.pack_posix(dist, runtime, out, VERSION, "linux-x64")
    sums = rt.write_sums(out)
    (out / "SHA256SUMS.txt.sig").write_text(sign_sums_text(sums.read_bytes()), encoding="ascii")
    problems = rt.verify(out, VERSION, ["linux-x64"], KEYS)
    assert "pob4agents-0.9.0-linux-x64.tar.gz has no executable pob" in problems


def test_shim_installs_pob4agents_under_the_old_name(rt: ModuleType, tmp_path: Path) -> None:
    import tomllib

    project = rt.write_shim_project(tmp_path / "shim", VERSION)
    data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert data["name"] == "pob-cli"
    assert data["version"] == VERSION
    assert data["dependencies"] == ["pob4agents==0.9.0"]
    main = tomllib.loads((REPO / "pob_cli" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert main["name"] == "pob4agents"
    # pob4agents owns the `pob` launcher; uninstalling the alias must not delete it.
    assert "pob" in main["scripts"] and "scripts" not in data
    assert data["requires-python"] == main["requires-python"]


@pytest.mark.parametrize(
    ("version", "normalized"),
    [
        ("0.9.0", "0.9.0"),
        ("0.9.0-rc.1", "0.9.0rc1"),
        ("0.9.0-rc1", "0.9.0rc1"),
        ("1.0.0-beta", "1.0.0b0"),
        ("1.0.0-alpha.2", "1.0.0a2"),
        ("1.0.0-pre.3", "1.0.0rc3"),
        ("1.0.0-c4", "1.0.0rc4"),
        ("1.0.0-RC.1", "1.0.0rc1"),
        ("1.0.0-post.2", "1.0.0.post2"),
        ("1.0.0-1", "1.0.0.post1"),
        ("1.0.0-dev3", "1.0.0.dev3"),
        ("1.0.0-rc.1.dev2", "1.0.0rc1.dev2"),
        ("01.02.003", "1.2.3"),
    ],
)
def test_python_names_use_the_pep440_version(rt: ModuleType, version: str, normalized: str) -> None:
    assert rt.check_version(version) == version
    assert rt.pep440_version(version) == normalized
    assert rt.python_asset_names(version) == [
        f"pob4agents-{normalized}-py3-none-any.whl",
        f"pob4agents-{normalized}.tar.gz",
    ]
    # Tags and standalone archives keep the version as written.
    assert rt.versioned_name(version, "linux-x64") == f"pob4agents-{version}-linux-x64.tar.gz"


@pytest.mark.parametrize("version", ["1.0.0-foo", "1.0.0-rc.x", "1.0.0-dev.rc1"])
def test_pep440_rejects_what_build_would_reject(rt: ModuleType, version: str) -> None:
    with pytest.raises(ValueError):
        rt.pep440_version(version)


def test_prerelease_notes_and_verify_use_the_built_wheel_name(rt: ModuleType, tmp_path: Path) -> None:
    assert "releases/download/v0.9.0-rc.1/pob4agents-0.9.0rc1-py3-none-any.whl" in rt.release_notes("0.9.0-rc.1")
    out = tmp_path / "release-assets"
    rt.pack_windows(_fake_dist(tmp_path), out, "0.9.0-rc.1")
    for name in ("pob4agents-0.9.0rc1-py3-none-any.whl", "pob4agents-0.9.0rc1.tar.gz"):
        (out / name).write_bytes(name.encode())
    sums = rt.write_sums(out)
    (out / "SHA256SUMS.txt.sig").write_text(sign_sums_text(sums.read_bytes()), encoding="ascii")
    assert rt.verify(out, "0.9.0-rc.1", ["windows-x64"], KEYS) == []


def test_verify_reports_a_truncated_zip(rt: ModuleType, tmp_path: Path) -> None:
    out = _full_release(rt, tmp_path)
    zip_path = out / "pob4agents-0.9.0-windows-x64.zip"
    zip_path.write_bytes(zip_path.read_bytes()[:20])
    problems = rt.verify(out, VERSION, ["windows-x64"], KEYS)
    assert any("is not a readable .zip" in p for p in problems)


def test_verify_reports_unsafe_zip_paths(rt: ModuleType, tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("pob.exe", b"MZ")
        zf.writestr("../evil.dll", b"x")
    assert rt._formats.zip_problems(bad, "pob.exe") == ["bad.zip has an unsafe path: ../evil.dll"]


def _tar_with(path: Path, *members: tarfile.TarInfo) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        exe = tarfile.TarInfo("pob")
        exe.mode = 0o755
        tf.addfile(exe, io.BytesIO(b""))
        for member in members:
            tf.addfile(member)
    return path


def _link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


@pytest.mark.parametrize(
    ("name", "target", "kind", "safe"),
    [
        ("_internal/Python", "Python.framework/Python", tarfile.SYMTYPE, True),
        ("_internal/lib/a", "../b", tarfile.SYMTYPE, True),
        ("_internal/x", "/etc/passwd", tarfile.SYMTYPE, False),
        ("_internal/x", "../../etc/passwd", tarfile.SYMTYPE, False),
        ("x", "..", tarfile.SYMTYPE, False),
        ("_internal/h", "pob", tarfile.LNKTYPE, True),
        ("_internal/h", "../outside", tarfile.LNKTYPE, False),
        ("_internal/h", "/etc/passwd", tarfile.LNKTYPE, False),
    ],
)
def test_tar_links_must_stay_inside_the_archive(
    rt: ModuleType, tmp_path: Path, name: str, target: str, kind: bytes, safe: bool
) -> None:
    problems = rt._formats.tar_problems(_tar_with(tmp_path / "b.tar.gz", _link(name, target, kind)), ("pob",))
    assert (problems == []) is safe, problems


def test_package_data_ships_every_bundled_data_and_lua_file() -> None:
    """Wheels and sdists must carry what pob.spec bundles: a missing timeless meta broke PRNG synthesis."""
    import fnmatch
    import tomllib

    pkg = REPO / "pob_cli" / "src" / "pob_cli"
    cfg = tomllib.loads((REPO / "pob_cli" / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = cfg["tool"]["setuptools"]["package-data"]["pob_cli"]
    shipped = [
        f"{sub}/{p.name}"
        for sub in ("data", "lua")
        for p in sorted((pkg / sub).iterdir())
        if p.is_file() and not p.name.startswith(".")
    ]
    missing = [rel for rel in shipped if not any(fnmatch.fnmatch(rel, pat) for pat in patterns)]
    assert not missing, f"package-data misses {missing}"
