"""Version spelling and archive checks for release_tools.py, with no third-party imports.

The release jobs run release_tools.py on a bare Python, so the PEP 440 normalisation that
`python -m build` applies to wheel and sdist names is reimplemented here for the forms a release tag
may take, instead of importing `packaging`.
"""

from __future__ import annotations

import posixpath
import re
import tarfile
import zipfile
from pathlib import Path

# The suffix spellings PEP 440 accepts after X.Y.Z, in its pre, post, dev order.
_PEP440_RE = re.compile(
    r"""^(?P<release>[0-9]+\.[0-9]+\.[0-9]+)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_implicit>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?$""",
    re.IGNORECASE | re.VERBOSE,
)
_PRE_SPELLING = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "c": "rc", "pre": "rc", "preview": "rc", "rc": "rc"}


def pep440_version(version: str) -> str:
    """The normalised PEP 440 form of a release version, as wheel and sdist file names carry it.

    `0.9.0-rc.1` -> `0.9.0rc1`, `1.0.0-beta` -> `1.0.0b0`, `1.0.0-post.2` -> `1.0.0.post2`,
    `1.0.0-dev3` -> `1.0.0.dev3`. A suffix PEP 440 cannot express raises ValueError, because
    `python -m build` would refuse that version too.
    """
    m = _PEP440_RE.match(version)
    if not m:
        raise ValueError(f"{version!r} is not a PEP 440 version (use X.Y.Z, X.Y.Z-rc.N, X.Y.Z-post.N, ...)")
    out = ".".join(str(int(part)) for part in m["release"].split("."))
    if m["pre_l"]:
        out += f"{_PRE_SPELLING[m['pre_l'].lower()]}{int(m['pre_n'] or 0)}"
    if m["post_implicit"] is not None:
        out += f".post{int(m['post_implicit'])}"
    elif m["post_l"]:
        out += f".post{int(m['post_n'] or 0)}"
    if m["dev_l"]:
        out += f".dev{int(m['dev_n'] or 0)}"
    return out


def _escapes(name: str) -> bool:
    """Whether an archive path is absolute or climbs out of the archive root."""
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return True
    norm = posixpath.normpath(name.replace("\\", "/"))
    return norm == ".." or norm.startswith("../")


def _link_escapes(member: tarfile.TarInfo) -> bool:
    if member.linkname.startswith(("/", "\\")):
        return True
    if member.issym():
        # A symlink target is relative to the link's own directory; a hard link's to the archive root.
        return _escapes(posixpath.join(posixpath.dirname(member.name), member.linkname))
    return _escapes(member.linkname)


def tar_problems(path: Path, required: tuple[str, ...]) -> list[str]:
    """Problems with a release .tar.gz: unreadable, missing executables, paths or links leaving the root."""
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
    problems += [f"{path.name} has an unsafe path: {n}" for n in members if _escapes(n)]
    problems += [
        f"{path.name} has a link leaving the archive: {m.name} -> {m.linkname}"
        for m in members.values()
        if (m.issym() or m.islnk()) and _link_escapes(m)
    ]
    return problems


def zip_problems(path: Path, required: str) -> list[str]:
    """Problems with a release .zip: unreadable, no `required` at its root, paths leaving the root."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return [f"{path.name} is not a readable .zip: {exc}"]
    problems = [] if required in names else [f"{path.name} has no {required} at its root"]
    problems += [f"{path.name} has an unsafe path: {n}" for n in names if _escapes(n)]
    return problems
