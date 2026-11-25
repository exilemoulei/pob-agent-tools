"""Stage the `TreeData/<ver>/` files an RPC needs before the engine sees it.

The slim engine keeps only the newest trees. Every RPC that makes PoB load a tree version comes
through here first, on every OS: `import_code` (the versions its build names), `tree_data` and
`convert_tree` (their `version` param) and `import_tree_url` (the version its link implies).
The engine itself never fetches anything.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .bridge_protocol import BridgeError, _decode_share_code_xml, _extract_xml_tree_versions

_FAILED_TREE_STAGE: dict[tuple[str, str], float] = {}
_TREE_STAGE_RETRY_COOLDOWN_SEC = 60.0
# Staging runs under the worker lock before the RPC (15 s RPC timeout; the CLI waits 150 s).
_TREE_STAGE_PER_VERSION_SEC = 8.0
_TREE_STAGE_TOTAL_BUDGET_SEC = 12.0

# PoB's TreeTab import dialog (OpenImportPopup): the version a tree link names, else the latest.
_URL_VERSION_RE = re.compile(r"tree/([0-9]+)\.([0-9]+)\.([0-9]+)/")
_URL_ALT_RE = re.compile(r"tree/([a-z]+-?[a-z]*)")
_SKILLTREE_VERSION_RE = re.compile(r"/\?v=([0-9]+)\.([0-9]+)\.([0-9]+)-?\w?-?\w?#")
_SKILLTREE_ALT_RE = re.compile(r"-([a-z]+-?[a-z]*)#")
_TREE_VERSION_LIST_RE = re.compile(r"treeVersionList\s*=\s*\{([^}]+)\}")


def _tree_stage_error(tv: str, missing: list[str], reason: str) -> BridgeError:
    shown = ", ".join(missing[:3]) + (", ..." if len(missing) > 3 else "")
    return BridgeError(
        f"This build uses passive tree version '{tv}', whose data is not installed ({shown}), "
        f"and it could not be fetched: {reason}. Run pob online (without --offline / "
        "POB_OFFLINE=1; behind a proxy set POB_PROXY or HTTPS_PROXY) so it downloads the "
        "tree on demand, or run `pob update` if the engine itself is damaged."
    )


def stage_tree_versions(versions: list[str], game_dir: Path) -> list[str]:
    """Fetch the given tree versions where the slim engine left them out; returns those fetched.

    A version counts as present only when every manifest file for it is there and verified.
    All versions share one time budget below the RPC's own timeout, and a version that failed is
    not retried for a minute. Raises `BridgeError` with a fix-it hint when a needed version cannot
    be installed, instead of letting PoB fail on a missing `tree.lua`. A version the manifest does
    not know is left to PoB, which reports it itself.
    """
    requested = list(dict.fromkeys(v for v in versions if v))
    if not requested or not (Path(game_dir) / "manifest.xml").is_file():
        return []
    from .updater.constants import UpdateError
    from .updater.remote import ensure_tree_data_files, missing_tree_data_files

    gdir_key = str(Path(game_dir))
    deadline = time.monotonic() + _TREE_STAGE_TOTAL_BUDGET_SEC
    staged: list[str] = []
    for tv in requested:
        try:
            missing = missing_tree_data_files(Path(game_dir), tv)
        except (UpdateError, OSError, ValueError):
            continue
        if not missing:
            continue
        if os.environ.get("POB_OFFLINE") == "1":
            raise _tree_stage_error(tv, missing, "POB_OFFLINE=1 is set")
        ckey = (gdir_key, tv)
        last_fail = _FAILED_TREE_STAGE.get(ckey)
        if last_fail is not None and (time.monotonic() - last_fail) < _TREE_STAGE_RETRY_COOLDOWN_SEC:
            raise _tree_stage_error(tv, missing, "a download failed less than a minute ago")
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            raise _tree_stage_error(tv, missing, "the download time budget ran out")
        try:
            ensure_tree_data_files(Path(game_dir), tv, timeout=min(_TREE_STAGE_PER_VERSION_SEC, remaining))
        except Exception as exc:
            _FAILED_TREE_STAGE[ckey] = time.monotonic()
            raise _tree_stage_error(tv, missing, str(exc) or type(exc).__name__) from exc
        _FAILED_TREE_STAGE.pop(ckey, None)
        staged.append(tv)
    return staged


def stage_missing_tree_versions(code: str, game_dir: Path) -> list[str]:
    """`stage_tree_versions` for every tree version a build code names."""
    xml_text = _decode_share_code_xml(code)
    return stage_tree_versions(_extract_xml_tree_versions(xml_text), game_dir) if xml_text else []


def stage_tree_versions_hint(code: str, game_dir: Path) -> str | None:
    """`stage_missing_tree_versions`, returning its failure as a message for the caller."""
    try:
        stage_missing_tree_versions(code, game_dir)
    except BridgeError as exc:
        return str(exc)
    return None


def _tree_version_list(game_dir: Path) -> list[str]:
    try:
        text = (Path(game_dir) / "GameVersions.lua").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    m = _TREE_VERSION_LIST_RE.search(text)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def tree_url_version(url: str, game_dir: Path) -> str | None:
    """The tree version PoB's import dialog builds for `url` (None for poeplanner and poeurl links)."""
    link = (url or "").strip()
    if not link or "poeurl.com/" in link or "poeplanner.com/" in link:
        return None  # poeplanner encodes its version in the payload, which PoB decodes itself
    if "poeskilltree.com/" in link:
        ver_m, alt_m = _SKILLTREE_VERSION_RE.search(link), _SKILLTREE_ALT_RE.search(link)
    else:
        ver_m, alt_m = _URL_VERSION_RE.search(link), _URL_ALT_RE.search(link)
    alt = ("_" + alt_m.group(1).replace("-", "_")) if alt_m else ""
    known = _tree_version_list(game_dir)
    if not known:
        return None
    # In range between defaultTreeVersion (the list's second entry) and latestTreeVersion
    lo, hi = _version_num(known[1] if len(known) > 1 else known[0]), _version_num(known[-1])
    if ver_m and lo is not None and hi is not None:
        major, minor = ver_m.group(1), ver_m.group(2)
        if lo <= float(f"{int(major)}.{int(minor):02d}") <= hi:
            return f"{major}_{minor}{alt}"
    return known[-1] + alt


def _version_num(tv: str) -> float | None:
    m = re.match(r"^(\d+)_(\d+)", tv)
    return float(f"{int(m.group(1))}.{int(m.group(2)):02d}") if m else None


def rpc_tree_versions(method: str, params: dict[str, Any] | None, game_dir: Path) -> list[str]:
    """Tree versions an RPC other than import_code makes PoB load (empty when it names none)."""
    p = params or {}
    if method in ("tree_data", "convert_tree"):
        v = p.get("version")
        return [v] if isinstance(v, str) and v else []
    if method == "import_tree_url":
        v = tree_url_version(str(p.get("url") or ""), game_dir)
        return [v] if v else []
    return []


def rpc_tree_stage_error(method: str, params: dict[str, Any] | None, game_dir: Path) -> BridgeError | None:
    """Stage what `rpc_tree_versions` names; why the RPC cannot run when that fails, else None."""
    versions = rpc_tree_versions(method, params, game_dir)
    if not versions:
        return None
    try:
        stage_tree_versions(versions, game_dir)
    except BridgeError as exc:
        return BridgeError(f"RPC '{method}' failed: {exc}")
    return None
