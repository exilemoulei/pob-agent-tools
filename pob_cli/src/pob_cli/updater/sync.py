"""Incremental dual-upstream update checker and atomic applier."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bootstrap import _apply_staged_files_with_gate
from .constants import (
    _AUTO_CHECK_TIMEOUT_SPEC,
    _CHECK_TIMEOUT_SPEC,
    STATE_DEGRADED_NETWORK,
    STATE_DEGRADED_ROLLBACK,
    STATE_UPDATED,
    ManifestFileEntry,
    PobManifest,
    _is_version_gte,
    _is_version_newer,
    _to_httpx_timeout,
)
from .engine_lock import STAGING_DIRNAME, EngineBusyError, engine_update_lock, recover_idle_engine
from .manifest import (
    _render_local_manifest_xml,
    diff_pob_manifest,
    find_local_manifest_mismatches,
)
from .paths import _find_legacy_dir, _get_runtime_dir, is_valid_engine_dir, seed_from_legacy_dir
from .remote import (
    POBTOOLS_APP_ONLY_PREFIXES,
    POBTOOLS_DATA_ONLY_PREFIXES,
    _download_and_stage_release_zip,
    _download_pob_files_to_staging,
    _pick_data_asset,
    _pick_pobtools_asset,
)
from .state import (
    _save_bootstrap_missing_state,
    _save_check_result_state,
    _save_degraded_state,
    _save_offline_state,
    _write_pobtools_meta,
    detect_local_versions,
    probe_engine_gate,
    save_update_state,
)


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def _resolve_update_target(
    engine_dir: Path | None,
    managed_dir: Path,
    legacy_dir: Path | None,
) -> tuple[Path | None, Path | None, bool]:
    """Return `(active_dir, needs_seed_from_legacy, explicit_engine)`."""
    if engine_dir is not None:
        active_dir = Path(engine_dir)
        from ..guardrails import validate_engine_root

        validate_engine_root(active_dir, expected_game="poe1", source_label="engine_dir")
        return active_dir, None, True

    env_root = os.environ.get("POB_ROOT", "").strip()
    if env_root:
        from ..guardrails import validate_engine_root

        validate_engine_root(Path(env_root), expected_game="poe1", source_label="POB_ROOT")
    if env_root and is_valid_engine_dir(Path(env_root)):
        return Path(env_root), None, True
    if is_valid_engine_dir(managed_dir):
        return managed_dir, None, False
    leg = _find_legacy_dir(legacy_dir)
    if leg is not None:
        return leg, leg, False
    return None, None, False


def _fetch_remote_metadata(
    *,
    prev_etag: str | None,
    proxy: str | None,
    mirror: str | None,
    check_timeout: Any,
) -> tuple[str | None, PobManifest | None, str | None, dict[str, Any] | None, list[str]]:
    import concurrent.futures

    pkg = _pkg()
    tools_info: dict[str, Any] | None = None
    pob_status: str | None = None
    remote_manifest: PobManifest | None = None
    new_etag: str | None = prev_etag
    net_errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_pob = pool.submit(
            pkg.fetch_pob_manifest_remote,
            etag=prev_etag,
            proxy=proxy,
            mirror=mirror,
            timeout=check_timeout,
            slim=True,
        )
        fut_tools = pool.submit(
            pkg.fetch_pobtools_latest_info,
            proxy=proxy,
            mirror=mirror,
            timeout=check_timeout,
            fetch_manifest=False,
        )
        try:
            pob_status, remote_manifest, new_etag = fut_pob.result()
        except Exception as exc:
            net_errors.append(f"PoB manifest: {type(exc).__name__}: {exc}")
        try:
            tools_info = fut_tools.result()
        except Exception as exc:
            net_errors.append(f"PobTools-zh: {type(exc).__name__}: {exc}")

    return pob_status, remote_manifest, new_etag, tools_info, net_errors


def _repair_and_diff_manifest(
    *,
    active_dir: Path,
    rt_dir: Path,
    pob_status: str | None,
    remote_manifest: PobManifest | None,
    new_etag: str | None,
    local_pob_ver: str | None,
    force: bool,
    check_only: bool,
    proxy: str | None,
    mirror: str | None,
    check_timeout: Any,
) -> tuple[PobManifest | None, str | None, str | None, list[ManifestFileEntry], list[Path]]:
    poe1_dir = active_dir / "PoE1"
    to_download: list[ManifestFileEntry] = []
    to_delete: list[Path] = []
    remote_pob_ver = local_pob_ver
    cache_dir = _pkg().get_shared_cache_dir(rt_dir, pob_root=active_dir)
    use_fast_stat = not force

    if pob_status == "not_modified":
        tampered = find_local_manifest_mismatches(poe1_dir, fast_stat=use_fast_stat, cache_dir=cache_dir, slim=True)
        if tampered:
            _, remote_manifest, new_etag = _pkg().fetch_pob_manifest_remote(
                etag=None, proxy=proxy, mirror=mirror, timeout=check_timeout, slim=True
            )

    if remote_manifest is not None:
        remote_pob_ver = remote_manifest.version
        to_download, to_delete = diff_pob_manifest(
            poe1_dir, remote_manifest, verify_local_sha1=True, fast_stat=use_fast_stat and check_only,
            cache_dir=cache_dir, slim=True,
        )
    return remote_manifest, new_etag, remote_pob_ver, to_download, to_delete


def _evaluate_tools_and_data_updates(
    tools_info: dict[str, Any],
    local_vers: dict[str, str | None],
    *,
    include_zh: bool,
    force: bool,
    proxy: str | None,
    mirror: str | None,
    check_timeout: Any,
) -> tuple[dict[str, Any], bool, bool, str | None]:
    local_tools_ver = local_vers["pobToolsVersion"]
    local_tools_tag = local_vers["pobToolsTag"]
    local_data_ver = local_vers["dataVersion"]

    remote_tools_tag = tools_info["tag"]
    remote_tools_ver = tools_info["version"]
    tools_needs_update = force or (
        remote_tools_tag != local_tools_tag and _is_version_newer(remote_tools_ver, local_tools_ver)
    )
    data_needs_update = False
    remote_data_tag: str | None = None

    if tools_needs_update or (include_zh and not local_data_ver) or force or (remote_tools_tag != local_tools_tag):
        full_tools = _pkg().fetch_pobtools_latest_info(
            proxy=proxy,
            mirror=mirror,
            timeout=check_timeout,
            fetch_manifest=True,
        )
        tools_info = full_tools
        tools_manifest = full_tools.get("manifest")
        data_manifest = full_tools.get("dataManifest")
        remote_tools_ver = full_tools["version"]
        if isinstance(tools_manifest, dict) and tools_manifest.get("dataTag"):
            remote_data_tag = str(tools_manifest["dataTag"])
        if include_zh and isinstance(data_manifest, dict) and remote_data_tag:
            target_app_ver = remote_tools_ver if tools_needs_update else (local_tools_ver or remote_tools_ver)
            if (force or remote_data_tag != local_data_ver) and _is_version_gte(
                target_app_ver, data_manifest.get("minApp")
            ):
                data_needs_update = True

    return tools_info, tools_needs_update, data_needs_update, remote_data_tag


def _stage_incremental_updates(
    staging_dir: Path,
    *,
    remote_manifest: PobManifest | None,
    pob_needs_update: bool,
    to_download: list[ManifestFileEntry],
    tools_needs_update: bool,
    tools_manifest: Any,
    remote_tools_tag: str,
    data_needs_update: bool,
    data_manifest: Any,
    remote_data_tag: str | None,
    proxy: str | None,
    mirror: str | None,
) -> dict[Path, Path]:
    staged_files: dict[Path, Path] = {}
    if remote_manifest is not None and pob_needs_update:
        staged_files.update(
            _download_pob_files_to_staging(
                remote_manifest,
                to_download,
                staging_dir,
                proxy=proxy,
                mirror=mirror,
            )
        )
        manifest_staged = staging_dir / "PoE1" / "manifest.xml"
        manifest_staged.parent.mkdir(parents=True, exist_ok=True)
        manifest_staged.write_text(_render_local_manifest_xml(remote_manifest), encoding="utf-8")
        staged_files[Path("PoE1") / "manifest.xml"] = manifest_staged

    if tools_needs_update and isinstance(tools_manifest, dict):
        asset = _pick_pobtools_asset(tools_manifest, prefer_update=True)
        if asset:
            staged_files.update(
                _download_and_stage_release_zip(
                    remote_tools_tag,
                    asset,
                    staging_dir,
                    proxy=proxy,
                    mirror=mirror,
                    skip_prefixes=("PoE1",),
                    only_prefixes=POBTOOLS_APP_ONLY_PREFIXES,
                )
            )

    if data_needs_update and isinstance(data_manifest, dict) and remote_data_tag:
        d_asset = _pick_data_asset(data_manifest)
        if d_asset:
            staged_files.update(
                _download_and_stage_release_zip(
                    remote_data_tag,
                    d_asset,
                    staging_dir,
                    proxy=proxy,
                    mirror=mirror,
                    skip_prefixes=("PoE1",),
                    only_prefixes=POBTOOLS_DATA_ONLY_PREFIXES,
                )
            )
    if not _pkg().is_windows():
        # Best effort: a runtime stamped with an older pin is refreshed along with an update, never blocks it.
        with contextlib.suppress(Exception):  # staging_dir is <engine>/.staging
            staged_files.update(_pkg().ensure_posix_runtime(staging_dir.parent, staging_dir, proxy=proxy, mirror=mirror))
    return staged_files


def _apply_and_finalize_update(
    *,
    write_dir: Path,
    rt_dir: Path,
    prev_state: dict[str, Any],
    local_vers: dict[str, str | None],
    remote_manifest: PobManifest | None,
    remote_pob_ver: str | None,
    new_etag: str | None,
    pob_needs_update: bool,
    to_download: list[ManifestFileEntry],
    to_delete: list[Path],
    tools_info: dict[str, Any],
    tools_needs_update: bool,
    data_needs_update: bool,
    remote_data_tag: str | None,
    proxy: str | None,
    mirror: str | None,
    probe_callable: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    staging_dir = write_dir / STAGING_DIRNAME
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    remote_tools_tag = tools_info["tag"]
    remote_tools_ver = tools_info["version"]
    try:
        staged_files = _stage_incremental_updates(
            staging_dir,
            remote_manifest=remote_manifest,
            pob_needs_update=pob_needs_update,
            to_download=to_download,
            tools_needs_update=tools_needs_update,
            tools_manifest=tools_info.get("manifest"),
            remote_tools_tag=remote_tools_tag,
            data_needs_update=data_needs_update,
            data_manifest=tools_info.get("dataManifest"),
            remote_data_tag=remote_data_tag,
            proxy=proxy,
            mirror=mirror,
        )
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return _save_degraded_state(
            STATE_DEGRADED_NETWORK,
            local_vers=local_vers,
            remote_pob_ver=remote_pob_ver,
            prev_state=prev_state,
            engine_dir=write_dir,
            rt_dir=rt_dir,
            err_msg=f"Download/stage failed: {type(exc).__name__}: {exc}",
        )

    apply_res = _apply_staged_files_with_gate(
        write_dir, staged_files, deleted_rel_paths=to_delete, probe_fn=probe_callable
    )
    if not apply_res["ok"]:
        return _save_degraded_state(
            STATE_DEGRADED_ROLLBACK,
            local_vers=local_vers,
            remote_pob_ver=remote_pob_ver,
            prev_state=prev_state,
            engine_dir=write_dir,
            rt_dir=rt_dir,
            err_msg=apply_res.get("error") or "gate_result probe failed",
            rolled_back=True,
        )

    final_tools_ver = remote_tools_ver if tools_needs_update else (local_vers["pobToolsVersion"] or remote_tools_ver)
    final_tools_tag = remote_tools_tag if tools_needs_update else (local_vers["pobToolsTag"] or remote_tools_tag)
    final_data_ver = remote_data_tag if data_needs_update else (local_vers["dataVersion"] or remote_data_tag)
    if tools_needs_update or data_needs_update:
        _write_pobtools_meta(
            write_dir,
            pobtools_version=final_tools_ver,
            pobtools_tag=final_tools_tag,
            data_version=final_data_ver,
        )

    state = {
        "state": STATE_UPDATED,
        "localPobVersion": remote_pob_ver if pob_needs_update else local_vers["localPobVersion"],
        "remotePobVersion": remote_pob_ver,
        "pobToolsVersion": final_tools_ver,
        "pobToolsTag": final_tools_tag,
        "dataVersion": final_data_ver,
        "pobManifestEtag": new_etag,
        "checkedAt": time.time(),
        "engineDir": str(write_dir),
        "updatedFilesCount": len(staged_files),
        "deletedFilesCount": len(to_delete),
        "networkError": None,
        "userHint": None,
        "probe": apply_res["probe"],
    }
    save_update_state(state, rt_dir)
    return state


def check_and_apply_updates(
    *,
    engine_dir: Path | None = None,
    runtime_dir: Path | None = None,
    legacy_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    offline: bool = False,
    check_only: bool = False,
    force: bool = False,
    include_zh: bool = True,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check GitHub upstreams (with fast-fail timeout) and atomically apply incremental updates.

    Never invokes PoB's internal UpdateCheck.lua / UpdateApply.lua or api.github.com.
    Never modifies legacy PobTools[20261102]; seeds into <runtime_dir>/engine before writing.
    """
    pkg = _pkg()
    rt_dir = _get_runtime_dir(runtime_dir)
    prev_state = pkg.load_update_state(rt_dir)
    probe_callable = probe_fn or probe_engine_gate
    managed_dir = rt_dir / "engine"

    active_dir, needs_seed_from_legacy, explicit_engine = _resolve_update_target(
        engine_dir, managed_dir, legacy_dir
    )
    if active_dir is None or not is_valid_engine_dir(active_dir):
        target = managed_dir if active_dir is None else active_dir
        if offline:
            err = (
                "offline mode enabled and no local PoB engine found"
                if active_dir is None
                else f"engine directory invalid: {active_dir}"
            )
            return _save_bootstrap_missing_state(target, rt_dir, err)
        try:
            return pkg.bootstrap_engine(
                target_dir=target,
                runtime_dir=rt_dir,
                proxy=proxy,
                mirror=mirror,
                include_zh=include_zh,
                probe_fn=probe_callable,
            )
        except EngineBusyError as exc:  # callers get a state, never an exception, for "not installed yet"
            return _save_bootstrap_missing_state(target, rt_dir, str(exc))

    recover_idle_engine(active_dir)
    local_vers = detect_local_versions(active_dir, rt_dir)
    local_pob_ver = local_vers["localPobVersion"]

    if offline:
        return _save_offline_state(local_vers, prev_state, active_dir, rt_dir)

    check_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC if force else _AUTO_CHECK_TIMEOUT_SPEC)
    prev_etag = None if force else prev_state.get("pobManifestEtag")
    if prev_etag and prev_state.get("localPobVersion") != local_pob_ver:
        prev_etag = None

    pob_status, remote_manifest, new_etag, tools_info, net_errors = _fetch_remote_metadata(
        prev_etag=prev_etag, proxy=proxy, mirror=mirror, check_timeout=check_timeout
    )
    if net_errors:
        return _save_degraded_state(
            STATE_DEGRADED_NETWORK,
            local_vers=local_vers,
            remote_pob_ver=remote_manifest.version if remote_manifest else None,
            prev_state=prev_state,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            err_msg="; ".join(net_errors),
        )

    try:
        remote_manifest, new_etag, remote_pob_ver, to_download, to_delete = _repair_and_diff_manifest(
            active_dir=active_dir,
            rt_dir=rt_dir,
            pob_status=pob_status,
            remote_manifest=remote_manifest,
            new_etag=new_etag,
            local_pob_ver=local_pob_ver,
            force=force,
            check_only=check_only,
            proxy=proxy,
            mirror=mirror,
            check_timeout=check_timeout,
        )
    except Exception as exc:
        return _save_degraded_state(
            STATE_DEGRADED_NETWORK,
            local_vers=local_vers,
            remote_pob_ver=None,
            prev_state=prev_state,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            err_msg=f"PoB manifest repair fetch: {type(exc).__name__}: {exc}",
        )

    assert tools_info is not None
    try:
        tools_info, tools_needs_update, data_needs_update, remote_data_tag = _evaluate_tools_and_data_updates(
            tools_info,
            local_vers,
            include_zh=include_zh,
            force=force,
            proxy=proxy,
            mirror=mirror,
            check_timeout=check_timeout,
        )
    except Exception as exc:
        return _save_degraded_state(
            STATE_DEGRADED_NETWORK,
            local_vers=local_vers,
            remote_pob_ver=remote_pob_ver,
            prev_state=prev_state,
            engine_dir=active_dir,
            rt_dir=rt_dir,
            err_msg=f"PobTools-zh manifest: {type(exc).__name__}: {exc}",
        )

    remote_tools_tag = tools_info["tag"]
    remote_tools_ver = tools_info["version"]
    pob_needs_update = bool(to_download or to_delete or (remote_manifest and remote_pob_ver != local_pob_ver))
    if not (pob_needs_update or tools_needs_update or data_needs_update):
        return _save_check_result_state(
            local_vers=local_vers,
            remote_pob_ver=remote_pob_ver,
            remote_tools_ver=remote_tools_ver,
            remote_tools_tag=remote_tools_tag,
            remote_data_tag=remote_data_tag,
            pob_etag=new_etag,
            active_dir=active_dir,
            rt_dir=rt_dir,
            update_available=False,
        )

    def _still_pending(extra: dict[str, Any]) -> dict[str, Any]:
        return _save_check_result_state(
            local_vers=local_vers,
            remote_pob_ver=remote_pob_ver,
            remote_tools_ver=remote_tools_ver,
            remote_tools_tag=remote_tools_tag,
            remote_data_tag=remote_data_tag,
            pob_etag=prev_state.get("pobManifestEtag"),
            active_dir=active_dir,
            rt_dir=rt_dir,
            update_available=True,
            extra=extra,
        )

    if check_only:
        return _still_pending(
            {
                "remotePobToolsVersion": remote_tools_ver,
                "pobFilesChanged": len(to_download),
                "pobFilesDeleted": len(to_delete),
                "pobToolsUpdateAvailable": tools_needs_update,
                "dataUpdateAvailable": data_needs_update,
            }
        )

    seeds_managed = needs_seed_from_legacy is not None and not explicit_engine
    write_dir = managed_dir if seeds_managed else active_dir
    try:
        with engine_update_lock(write_dir):
            if seeds_managed:
                seed_from_legacy_dir(needs_seed_from_legacy, managed_dir)
            return _apply_and_finalize_update(
                write_dir=write_dir,
                rt_dir=rt_dir,
                prev_state=prev_state,
                local_vers=local_vers,
                remote_manifest=remote_manifest,
                remote_pob_ver=remote_pob_ver,
                new_etag=new_etag,
                pob_needs_update=pob_needs_update,
                to_download=to_download,
                to_delete=to_delete,
                tools_info=tools_info,
                tools_needs_update=tools_needs_update,
                data_needs_update=data_needs_update,
                remote_data_tag=remote_data_tag,
                proxy=proxy,
                mirror=mirror,
                probe_callable=probe_callable,
            )
    except EngineBusyError as exc:
        # The other process is applying this same update; report it as still pending.
        return _still_pending({"updateSkipped": str(exc)})
