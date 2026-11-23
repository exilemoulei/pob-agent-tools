"""Release assets for pob4agents: names, packaging, checksums, verification, Scoop manifest, notes.

Every asset name a release carries is defined here, so release.yml, the installers and the tests
agree on one list. Assets are `pob4agents-<version>-<platform>.<ext>`, with a version-free alias
`pob4agents-<platform>.<ext>` for `/releases/latest/download/<name>`. Windows also keeps the names
releases up to v0.8.0 used (`pob-v<version>-windows-x64.zip`, `pob-windows-x64.zip`), because
saved install scripts, pinned versions and README links still request them. macOS and Linux get
`.tar.gz` builds plus `pob4agents-runtime-<version>-<platform>.tar.gz`, the LuaJIT + lua-utf8 runtime
pob downloads during `pob bootstrap` when it runs from Python rather than a standalone binary.

`SHA256SUMS.txt.sig` is the Ed25519 signature of `SHA256SUMS.txt` (base64), made with OpenSSL from the
`RELEASE_SIGNING_KEY` secret and checked against the public keys in
`pob_cli/src/pob_cli/updater/signature.py`, the same check pob and install.ps1 run.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

REPO_SLUG = "exilemoulei/pob4agents"
PRODUCT = "pob4agents"
DIST_NAME = "pob_cli"
"""Normalized distribution name in wheel and sdist file names."""
SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"
SIGNATURE_MODULE = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "updater" / "signature.py"
WINDOWS = "windows-x64"
POSIX_PLATFORMS = ("macos-arm64", "macos-x64", "linux-x64", "linux-arm64")
PLATFORMS = (WINDOWS, *POSIX_PLATFORMS)
PLATFORM_EXT = {WINDOWS: "zip", **dict.fromkeys(POSIX_PLATFORMS, "tar.gz")}
RUNTIME_FILES = ("bin/luajit", "bin/lua-utf8.so")
LEGACY_WINDOWS_ALIAS = "pob-windows-x64.zip"

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
# Fixed entry timestamp so the same build always zips to the same bytes.
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def check_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise ValueError(f"Not a release version: {version!r} (expected X.Y.Z or X.Y.Z-pre)")
    return version


def versioned_name(version: str, platform: str) -> str:
    return f"{PRODUCT}-{version}-{platform}.{PLATFORM_EXT[platform]}"


def stable_name(platform: str) -> str:
    return f"{PRODUCT}-{platform}.{PLATFORM_EXT[platform]}"


def legacy_windows_name(version: str) -> str:
    return f"pob-v{version}-windows-x64.zip"


def platform_asset_names(version: str, platform: str) -> list[str]:
    """The versioned name first; the rest are byte-identical aliases."""
    names = [versioned_name(version, platform), stable_name(platform)]
    if platform == WINDOWS:
        names += [legacy_windows_name(version), LEGACY_WINDOWS_ALIAS]
    return names


def runtime_name(version: str, platform: str) -> str:
    """Same name pob's updater/posix_runtime.py asks for."""
    return f"{PRODUCT}-runtime-{version}-{platform}.tar.gz"


def python_asset_names(version: str) -> list[str]:
    return [f"{DIST_NAME}-{version}-py3-none-any.whl", f"{DIST_NAME}-{version}.tar.gz"]


def expected_assets(version: str, platforms: list[str]) -> set[str]:
    names = {SUMS_NAME, SIG_NAME, *python_asset_names(version)}
    for platform in platforms:
        names.update(platform_asset_names(version, platform))
        if platform in POSIX_PLATFORMS:
            names.add(runtime_name(version, platform))
    return names


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pack_windows(dist_dir: Path, out_dir: Path, version: str) -> tuple[Path, str]:
    """Zip a PyInstaller onedir build with `pob.exe` at the archive root, then write its aliases."""
    check_version(version)
    dist_dir, out_dir = Path(dist_dir), Path(out_dir)
    if not (dist_dir / "pob.exe").is_file():
        raise FileNotFoundError(f"Expected pob.exe inside {dist_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = platform_asset_names(version, WINDOWS)
    zip_path = out_dir / names[0]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in dist_dir.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(dist_dir).as_posix(), date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    for alias in names[1:]:
        shutil.copyfile(zip_path, out_dir / alias)
    return zip_path, _sha256(zip_path)


def _reproducible_tar_gz(out: Path, root: Path, arcnames: list[str]) -> None:
    """Tar `arcnames` under `root` with fixed owners, times and modes, so a build tars to the same bytes.

    Symlinks stay symlinks (macOS PyInstaller builds link inside `_internal`).
    """
    with (
        open(out, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf,
    ):
        for arcname in arcnames:
            path = root / arcname
            info = tf.gettarinfo(str(path), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if info.isreg():
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                with open(path, "rb") as fh:
                    tf.addfile(info, fh)
            else:
                info.mode = 0o755 if info.isdir() else info.mode
                tf.addfile(info)


def _tree(root: Path) -> list[str]:
    """Every directory, file and symlink under `root`, sorted, without following symlinks."""
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        names += [(rel / n).as_posix() for n in dirnames + filenames]
    return sorted(names)


def pack_posix(dist_dir: Path, runtime_dir: Path, out_dir: Path, version: str, platform: str) -> tuple[Path, str]:
    """Tar a PyInstaller onedir build (`pob` at the root) and its aliases, plus the runtime archive."""
    check_version(version)
    if platform not in POSIX_PLATFORMS:
        raise ValueError(f"Not a macOS/Linux platform: {platform}")
    dist_dir, runtime_dir, out_dir = Path(dist_dir), Path(runtime_dir), Path(out_dir)
    if not (dist_dir / "pob").is_file():
        raise FileNotFoundError(f"Expected pob inside {dist_dir}")
    if not all((runtime_dir / rel).is_file() for rel in RUNTIME_FILES):
        raise FileNotFoundError(f"Expected {', '.join(RUNTIME_FILES)} inside {runtime_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = platform_asset_names(version, platform)
    tar_path = out_dir / names[0]
    _reproducible_tar_gz(tar_path, dist_dir, _tree(dist_dir))
    for alias in names[1:]:
        shutil.copyfile(tar_path, out_dir / alias)
    _reproducible_tar_gz(out_dir / runtime_name(version, platform), runtime_dir, ["bin", *RUNTIME_FILES])
    return tar_path, _sha256(tar_path)


def _tar_problems(path: Path, required: tuple[str, ...]) -> list[str]:
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = {m.name: m for m in tf.getmembers()}
    except (tarfile.TarError, OSError) as exc:
        return [f"{path.name} is not a readable .tar.gz: {exc}"]
    problems = []
    for name in required:
        member = members.get(name)
        if member is None or not member.isreg() or not member.mode & 0o111:
            problems.append(f"{path.name} has no executable {name}")
    unsafe = [n for n in members if n.startswith("/") or ".." in Path(n).parts]
    problems += [f"{path.name} has an unsafe path: {n}" for n in unsafe]
    return problems


def write_sums(asset_dir: Path) -> Path:
    """GNU coreutils format with LF endings, so `sha256sum -c` works on every OS."""
    asset_dir = Path(asset_dir)
    lines = [
        f"{_sha256(p)}  {p.name}"
        for p in sorted(asset_dir.iterdir())
        if p.is_file() and p.name not in (SUMS_NAME, SIG_NAME)
    ]
    sums = asset_dir / SUMS_NAME
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sums


def signature_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pob_release_signature", SIGNATURE_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {SIGNATURE_MODULE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _openssl(*args: str, data: bytes | None = None) -> bytes:
    proc = subprocess.run(["openssl", *args], input=data, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def public_key_of(key_pem: Path) -> str:
    """Base64 raw 32-byte public key of an Ed25519 private key PEM (the last 32 bytes of its SPKI DER)."""
    der = _openssl("pkey", "-in", str(key_pem), "-pubout", "-outform", "DER")
    return base64.b64encode(der[-32:]).decode()


def sign_sums(asset_dir: Path, key_pem: Path) -> Path:
    """Write SHA256SUMS.txt.sig: the base64 Ed25519 signature of SHA256SUMS.txt's exact bytes."""
    sums = Path(asset_dir) / SUMS_NAME
    raw = _openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(key_pem), "-in", str(sums))
    if len(raw) != 64:
        raise RuntimeError(f"Expected a 64-byte Ed25519 signature, got {len(raw)} bytes")
    sig = Path(asset_dir) / SIG_NAME
    sig.write_text(base64.b64encode(raw).decode() + "\n", encoding="ascii", newline="\n")
    return sig


def keygen(out: Path) -> str:
    """Create an Ed25519 private key PEM (mode 600) and return its base64 public key."""
    if out.exists():
        raise FileExistsError(f"{out} exists; refusing to overwrite a key")
    _openssl("genpkey", "-algorithm", "ed25519", "-out", str(out))
    out.chmod(0o600)
    return public_key_of(out)


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = re.match(r"^([0-9a-f]{64}) [ *](\S.*)$", raw)
        if not m:
            raise ValueError(f"Malformed {SUMS_NAME} line: {raw!r}")
        if m.group(2) in out:
            raise ValueError(f"{m.group(2)} is listed twice in {SUMS_NAME}")
        out[m.group(2)] = m.group(1)
    return out


def verify(
    asset_dir: Path, version: str, platforms: list[str], keys: tuple[str, ...] | None = None
) -> list[str]:
    """Problems with a release's files: exact file set, signature, checksums, aliases, archive layout.

    `keys` defaults to the public keys pob ships; dry runs pass their throwaway key.
    """
    asset_dir = Path(asset_dir)
    problems: list[str] = []
    present = {p.name for p in asset_dir.iterdir() if p.is_file()}
    expected = expected_assets(version, platforms)
    problems += [f"missing asset: {n}" for n in sorted(expected - present)]
    problems += [f"unexpected asset: {n}" for n in sorted(present - expected)]
    if SUMS_NAME not in present:
        return problems
    try:
        sums = parse_sums((asset_dir / SUMS_NAME).read_text(encoding="utf-8"))
    except ValueError as exc:
        return [*problems, str(exc)]
    sig = asset_dir / SIG_NAME
    if sig.is_file():
        sums_bytes = (asset_dir / SUMS_NAME).read_bytes()
        mod = signature_module()
        if keys is None and not mod.RELEASE_PUBLIC_KEYS:
            problems.append("no release public key in updater/signature.py; nothing can verify this release")
        elif not mod.verify_sums(sums_bytes, sig.read_text(encoding="ascii"), keys):
            problems.append(f"{SIG_NAME} does not verify {SUMS_NAME} against the release public keys")
    for name in sorted(present - {SUMS_NAME, SIG_NAME}):
        if name not in sums:
            problems.append(f"{name} has no {SUMS_NAME} entry")
        elif sums[name] != _sha256(asset_dir / name):
            problems.append(f"{name} does not match its {SUMS_NAME} entry")
    problems += [f"{SUMS_NAME} lists a file the release lacks: {n}" for n in sorted(set(sums) - present)]
    for platform in platforms:
        names = [n for n in platform_asset_names(version, platform) if n in sums]
        if len({sums[n] for n in names}) > 1:
            problems.append(f"{platform} aliases differ from {names[0]}")
    win = asset_dir / versioned_name(version, WINDOWS)
    if WINDOWS in platforms and win.is_file():
        with zipfile.ZipFile(win) as zf:
            if "pob.exe" not in zf.namelist():
                problems.append(f"{win.name} has no pob.exe at its root")
    for platform in platforms:
        if platform not in POSIX_PLATFORMS:
            continue
        build, runtime = asset_dir / versioned_name(version, platform), asset_dir / runtime_name(version, platform)
        if build.is_file():
            problems += _tar_problems(build, ("pob",))
        if runtime.is_file():
            problems += _tar_problems(runtime, RUNTIME_FILES)
    return problems


def update_scoop_manifest(manifest_path: Path, version: str, sha256: str) -> None:
    """Point `bucket/pob.json` at a release; the app stays `pob` so `scoop update pob` keeps working."""
    check_version(version)
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = f"https://github.com/{REPO_SLUG}/releases/download"
    data["version"] = version
    data["architecture"]["64bit"]["url"] = f"{base}/v{version}/{versioned_name(version, WINDOWS)}"
    data["architecture"]["64bit"]["hash"] = sha256.lower()
    data["autoupdate"]["architecture"]["64bit"]["url"] = (
        f"{base}/v$version/{PRODUCT}-$version-{WINDOWS}.zip"
    )
    text = json.dumps(data, indent=4, ensure_ascii=False) + "\n"
    manifest_path.write_text(text, encoding="utf-8", newline="\n")


def release_notes(version: str) -> str:
    """Install instructions placed above GitHub's generated notes."""
    tag = f"v{version}"
    wheel = python_asset_names(version)[0]
    return f"""### Install

**Windows x64** (no Python needed), install or upgrade:
```powershell
irm https://raw.githubusercontent.com/{REPO_SLUG}/main/install.ps1 | iex
```

**macOS and Linux** (arm64 and x64; no Python needed), install or upgrade:
```bash
curl -fsSL https://raw.githubusercontent.com/{REPO_SLUG}/main/install.sh | bash
```

**Scoop:**
```powershell
scoop bucket add pob https://github.com/{REPO_SLUG}
scoop install pob   # later: scoop update pob
```

**Python 3.11+** (`uv` / `pipx` / `pip`):
```bash
uv tool install "https://github.com/{REPO_SLUG}/releases/download/{tag}/{wheel}"
```

Then run `pob bootstrap` once (add `--proxy http://127.0.0.1:7890` if GitHub needs a proxy) to install the PoB engine.

Every file is listed in `{SUMS_NAME}`, signed in `{SIG_NAME}` (Ed25519), and each has a build attestation: `gh attestation verify <file> -R {REPO_SLUG}`.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release asset helpers for pob4agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack-windows", help="zip dist/pob and write its aliases")
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("pack-posix", help="tar dist/pob and the LuaJIT runtime for macOS or Linux")
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument("--runtime", type=Path, required=True, help="directory holding bin/luajit, bin/lua-utf8.so")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platform", required=True, choices=POSIX_PLATFORMS)
    p = sub.add_parser("sums", help=f"write {SUMS_NAME} for a directory")
    p.add_argument("--dir", type=Path, required=True)
    p = sub.add_parser("sign", help=f"write {SIG_NAME} with an Ed25519 private key PEM")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--key", type=Path, required=True)
    p = sub.add_parser("pubkey", help="print the base64 public key of a private key PEM")
    p.add_argument("--key", type=Path, required=True)
    p = sub.add_parser("keygen", help="create a release signing key (for the repository owner)")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("verify", help="check a directory holds exactly one release's files")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platforms", default=",".join(PLATFORMS), help="comma-separated (default: all)")
    p.add_argument("--public-key", action="append", help="base64 key to verify with instead of pob's")
    p = sub.add_parser("update-scoop", help="point bucket/pob.json at a release")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("notes", help="print the install section of the release notes")
    p.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "pack-windows":
        zip_path, zip_sha = pack_windows(args.dist, args.out, args.version)
        print(f"zip_name={zip_path.name}")
        print(f"zip_sha256={zip_sha}")
    elif args.cmd == "pack-posix":
        tar_path, tar_sha = pack_posix(args.dist, args.runtime, args.out, args.version, args.platform)
        print(f"tar_name={tar_path.name}")
        print(f"tar_sha256={tar_sha}")
    elif args.cmd == "sums":
        print(write_sums(args.dir).read_text(encoding="utf-8"), end="")
    elif args.cmd == "sign":
        print(sign_sums(args.dir, args.key))
    elif args.cmd == "pubkey":
        print(public_key_of(args.key))
    elif args.cmd == "keygen":
        public_key = keygen(args.out)
        print(f"Private key: {args.out} (keep it offline; never commit it)")
        print(f"Public key:  {public_key}")
        print("1. Add the PEM file's full text as the repository secret RELEASE_SIGNING_KEY.")
        print("2. Add the public key to RELEASE_PUBLIC_KEYS in pob_cli/src/pob_cli/updater/signature.py")
        print("   and to the key lists in install.ps1 and install.sh, in a commit released before the key signs.")
    elif args.cmd == "verify":
        platforms = [p for p in args.platforms.split(",") if p]
        keys = tuple(args.public_key) if args.public_key else None
        problems = verify(args.dir, check_version(args.version), platforms, keys)
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"OK {len(expected_assets(args.version, platforms))} files for {args.version}")
    elif args.cmd == "update-scoop":
        update_scoop_manifest(args.manifest, args.version, args.sha256)
    elif args.cmd == "notes":
        print(release_notes(check_version(args.version)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
