"""Release assets for pob4agents: names, packaging, checksums, verification, Scoop manifest, notes.

Every asset name a release carries is defined here, so release.yml, the installers and the tests
agree on one list. Assets are `pob4agents-<version>-<platform>.<ext>`, with a version-free alias
`pob4agents-<platform>.<ext>` for `/releases/latest/download/<name>`. Windows also keeps the names
releases up to v0.8.0 used (`pob-v<version>-windows-x64.zip`, `pob-windows-x64.zip`), because
saved install scripts, pinned versions and README links still request them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO_SLUG = "exilemoulei/pob4agents"
PRODUCT = "pob4agents"
DIST_NAME = "pob_cli"
"""Normalized distribution name in wheel and sdist file names."""
SUMS_NAME = "SHA256SUMS.txt"
WINDOWS = "windows-x64"
PLATFORM_EXT = {WINDOWS: "zip"}
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


def python_asset_names(version: str) -> list[str]:
    return [f"{DIST_NAME}-{version}-py3-none-any.whl", f"{DIST_NAME}-{version}.tar.gz"]


def expected_assets(version: str, platforms: list[str]) -> set[str]:
    names = {SUMS_NAME, *python_asset_names(version)}
    for platform in platforms:
        names.update(platform_asset_names(version, platform))
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


def write_sums(asset_dir: Path) -> Path:
    """GNU coreutils format with LF endings, so `sha256sum -c` works on every OS."""
    asset_dir = Path(asset_dir)
    lines = [
        f"{_sha256(p)}  {p.name}"
        for p in sorted(asset_dir.iterdir())
        if p.is_file() and p.name != SUMS_NAME
    ]
    sums = asset_dir / SUMS_NAME
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sums


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


def verify(asset_dir: Path, version: str, platforms: list[str]) -> list[str]:
    """Problems with a release's files: exact file set, checksums, aliases, archive layout."""
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
    for name in sorted(present - {SUMS_NAME}):
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

Every file is listed in `{SUMS_NAME}`, and each has a build attestation: `gh attestation verify <file> -R {REPO_SLUG}`.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release asset helpers for pob4agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack-windows", help="zip dist/pob and write its aliases")
    p.add_argument("--dist", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("sums", help=f"write {SUMS_NAME} for a directory")
    p.add_argument("--dir", type=Path, required=True)
    p = sub.add_parser("verify", help="check a directory holds exactly one release's files")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--platforms", default=WINDOWS, help="comma-separated, e.g. windows-x64")
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
    elif args.cmd == "sums":
        print(write_sums(args.dir).read_text(encoding="utf-8"), end="")
    elif args.cmd == "verify":
        platforms = [p for p in args.platforms.split(",") if p]
        problems = verify(args.dir, check_version(args.version), platforms)
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
