"""Release helper utilities for packaging zip archives, SHA256SUMS.txt, and Scoop manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

REPO_SLUG = "exilemoulei/pob4agents"
STABLE_ZIP_NAME = "pob-windows-x64.zip"
"""Version-free alias mirrored from the tagged zip, served by /releases/latest/download/<name>."""


def pack_windows_release(dist_dir: Path, out_dir: Path, tag: str) -> tuple[Path, str]:
    """Zip `dist_dir` at archive root, mirror it as `STABLE_ZIP_NAME`, and write `SHA256SUMS.txt` for every asset."""
    dist_dir = Path(dist_dir)
    out_dir = Path(out_dir)
    if not (dist_dir / "pob.exe").is_file():
        raise FileNotFoundError(f"Expected pob.exe inside {dist_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"pob-{tag}-windows-x64.zip"
    zip_path = out_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(dist_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(dist_dir).as_posix())

    alias_path = out_dir / STABLE_ZIP_NAME
    if alias_path != zip_path:
        shutil.copyfile(zip_path, alias_path)

    lines: list[str] = []
    zip_sha = ""
    for asset in sorted(out_dir.iterdir()):
        if asset.is_file() and asset.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            lines.append(f"{digest}  {asset.name}")
            if asset.name == zip_name:
                zip_sha = digest

    # newline="\n" keeps SHA256SUMS.txt in GNU coreutils format: Windows text mode would otherwise emit
    # CRLF and break `sha256sum -c` verification of the published checksums.
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return zip_path, zip_sha


def update_scoop_manifest(manifest_path: Path, version: str, tag: str, sha256: str) -> None:
    """Update version, download URL, and SHA256 hash in `bucket/pob.json`."""
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    zip_name = f"pob-{tag}-windows-x64.zip"
    data["version"] = version
    data["architecture"]["64bit"]["url"] = (
        f"https://github.com/{REPO_SLUG}/releases/download/{tag}/{zip_name}"
    )
    data["architecture"]["64bit"]["hash"] = sha256.lower()
    manifest_path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Release packaging helpers for pob4agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--dist", type=Path, required=True)
    p_pack.add_argument("--out", type=Path, required=True)
    p_pack.add_argument("--tag", required=True)

    p_scoop = sub.add_parser("update-scoop")
    p_scoop.add_argument("--manifest", type=Path, required=True)
    p_scoop.add_argument("--version", required=True)
    p_scoop.add_argument("--tag", required=True)
    p_scoop.add_argument("--sha256", required=True)

    args = parser.parse_args()
    if args.cmd == "pack":
        zip_path, zip_sha = pack_windows_release(args.dist, args.out, args.tag)
        print(f"zip_name={zip_path.name}")
        print(f"zip_sha256={zip_sha}")
    elif args.cmd == "update-scoop":
        update_scoop_manifest(args.manifest, args.version, args.tag, args.sha256)


if __name__ == "__main__":
    main()
