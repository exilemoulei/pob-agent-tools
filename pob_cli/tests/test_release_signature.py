"""Ed25519 release-signature checks (updater/signature.py) and the keys the installers carry."""

from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

import pytest
from ed25519_sign import TEST_PUBLIC_KEY, public_key, sign, sign_sums_text

from pob_cli.updater import signature

REPO = Path(__file__).resolve().parents[2]

# RFC 8032 section 7.1, TEST 1..3: (secret key, public key, message, signature), all hex.
RFC_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize(("secret", "pub", "msg", "sig"), RFC_VECTORS)
def test_rfc8032_vectors(secret: str, pub: str, msg: str, sig: str) -> None:
    pk, m, s = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
    assert signature.ed25519_verify(pk, m, s)
    # The test signer reproduces the RFC exactly, so signatures it makes in other tests are real.
    assert public_key(bytes.fromhex(secret)) == pk
    assert sign(bytes.fromhex(secret), m) == s


@pytest.mark.parametrize(("secret", "pub", "msg", "sig"), RFC_VECTORS)
def test_rejects_any_change(secret: str, pub: str, msg: str, sig: str) -> None:
    pk, m, s = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
    assert not signature.ed25519_verify(pk, m + b"\x00", s)
    assert not signature.ed25519_verify(pk, m, s[:-1] + bytes([s[-1] ^ 1]))
    assert not signature.ed25519_verify(pk, m, bytes([s[0] ^ 1]) + s[1:])
    other = bytes.fromhex(RFC_VECTORS[0][1] if pub != RFC_VECTORS[0][1] else RFC_VECTORS[1][1])
    assert not signature.ed25519_verify(other, m, s)


def test_rejects_malformed_inputs() -> None:
    _, pub, msg, sig = RFC_VECTORS[1]
    pk, m, s = bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)
    assert not signature.ed25519_verify(pk[:31], m, s)
    assert not signature.ed25519_verify(pk, m, s[:63])
    # S >= L is rejected (signature malleability), as RFC 8032 requires.
    s_plus_l = (int.from_bytes(s[32:], "little") + signature._L).to_bytes(32, "little")
    assert not signature.ed25519_verify(pk, m, s[:32] + s_plus_l)
    # A y coordinate with no point on the curve.
    assert not signature.ed25519_verify(b"\xff" * 32, m, s)


def test_verify_sums_uses_any_listed_key() -> None:
    sums = b"0" * 64 + b"  pob4agents-0.9.0-windows-x64.zip\n"
    sig_text = sign_sums_text(sums)
    other = base64.b64encode(public_key(b"\x07" * 32)).decode()
    assert signature.verify_sums(sums, sig_text, (other, TEST_PUBLIC_KEY))
    assert not signature.verify_sums(sums, sig_text, (other,))
    assert not signature.verify_sums(sums + b"x", sig_text, (TEST_PUBLIC_KEY,))
    assert not signature.verify_sums(sums, "not base64!", (TEST_PUBLIC_KEY,))
    assert not signature.verify_sums(sums, sig_text, ("short",))


def test_installer_carries_the_same_keys() -> None:
    text = (REPO / "install.ps1").read_text(encoding="utf-8")
    m = re.search(r"\$ReleasePublicKeys = @\(([^)]*)\)", text)
    assert m, "install.ps1 must define $ReleasePublicKeys"
    assert tuple(re.findall(r'"([A-Za-z0-9+/=]+)"', m.group(1))) == signature.RELEASE_PUBLIC_KEYS


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl (release.yml signs with it)")
def test_openssl_signatures_verify(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_tools", REPO / "pob_cli" / "packaging" / "release_tools.py")
    assert spec is not None and spec.loader is not None
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    key = tmp_path / "key.pem"
    pub = rt.keygen(key)
    with pytest.raises(FileExistsError):
        rt.keygen(key)
    (tmp_path / "SHA256SUMS.txt").write_bytes(b"abc  file\n")
    sig_path = rt.sign_sums(tmp_path, key)
    sums = (tmp_path / "SHA256SUMS.txt").read_bytes()
    assert signature.verify_sums(sums, sig_path.read_text(encoding="ascii"), (pub,))
    assert not signature.verify_sums(sums, sig_path.read_text(encoding="ascii"), (TEST_PUBLIC_KEY,))
