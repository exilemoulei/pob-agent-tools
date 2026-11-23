"""Release asset names, packaging, checksums and verification (pob_cli/packaging/release_tools.py)."""

from __future__ import annotations

import importlib.util
import json
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


def _full_release(rt: ModuleType, root: Path) -> Path:
    out = root / "release-assets"
    rt.pack_windows(_fake_dist(root), out, VERSION)
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
    assert any("no release public key" in p for p in rt.verify(out, VERSION, ["windows-x64"]))
    other = rt.signature_module()
    monkeypatch.setattr(rt, "signature_module", lambda: other)
    monkeypatch.setattr(other, "RELEASE_PUBLIC_KEYS", KEYS)
    assert rt.verify(out, VERSION, ["windows-x64"]) == []


@pytest.mark.parametrize("damage", ["missing", "extra", "tampered", "alias", "unsigned", "resigned-sums"])
def test_verify_reports_damage(rt: ModuleType, tmp_path: Path, damage: str) -> None:
    out = _full_release(rt, tmp_path)
    if damage == "missing":
        (out / "pob-windows-x64.zip").unlink()
    elif damage == "extra":
        (out / "notes.txt").write_text("x", encoding="utf-8")
    elif damage == "tampered":
        (out / "pob_cli-0.9.0.tar.gz").write_bytes(b"swapped")
    elif damage == "alias":
        (out / "pob4agents-windows-x64.zip").write_bytes(b"other")
        sums = rt.write_sums(out)
        (out / "SHA256SUMS.txt.sig").write_text(sign_sums_text(sums.read_bytes()), encoding="ascii")
    elif damage == "unsigned":
        (out / "SHA256SUMS.txt.sig").unlink()
    else:
        # A swapped file with a matching, but unsigned, SHA256SUMS.txt: what a hostile mirror can do.
        (out / "pob_cli-0.9.0.tar.gz").write_bytes(b"swapped")
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
    args = ["verify", "--dir", str(out), "--version", VERSION, "--public-key", TEST_PUBLIC_KEY]
    assert rt.main(args) == 0
    (out / "pob4agents-0.9.0-windows-x64.zip").unlink()
    assert rt.main(args) == 1


def test_notes_name_the_wheel_and_attestations(rt: ModuleType) -> None:
    notes = rt.release_notes(VERSION)
    assert "releases/download/v0.9.0/pob_cli-0.9.0-py3-none-any.whl" in notes
    assert "gh attestation verify" in notes
