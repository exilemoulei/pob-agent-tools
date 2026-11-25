"""Atomic staging/rollback gate and dual-upstream engine bootstrapper."""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import (
    _CHECK_TIMEOUT_SPEC,
    LEGACY_SEED_DIRS,
    LEGACY_SEED_FILES,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_ROLLBACK,
    STATE_UPDATED,
    EngineGateError,
    PobManifest,
    UpdateError,
    _is_version_gte,
    _to_httpx_timeout,
)
from .engine_lock import (
    BACKUP_DIRNAME,
    STAGING_DIRNAME,
    EngineLockError,
    backup_file,
    backup_in_the_way,
    discard_backup,
    engine_update_lock,
    restore_interrupted_apply,
)
from .manifest import _render_local_manifest_xml, diff_pob_manifest
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
    _build_user_hint,
    _write_pobtools_meta,
    detect_local_versions,
    probe_engine_gate,
    save_update_state,
)


def _pkg() -> Any:
    return sys.modules["pob_cli.updater"]


def _apply_staged_files_with_gate(
    engine_dir: Path,
    staged_files: dict[Path, Path],
    deleted_rel_paths: list[Path] | None = None,
    *,
    probe_fn: Callable[[Path], dict[str, Any]] = probe_engine_gate,
) -> dict[str, Any]:
    """Backup existing files to .bak/, atomically replace from staged_files, probe gate, and rollback on failure."""
    eng = Path(engine_dir)
    bak_dir = eng / BACKUP_DIRNAME
    staging_dir = eng / STAGING_DIRNAME

    # A backup folder still here belongs to a run that died mid-swap: its files go back first.
    restore_interrupted_apply(eng)
    blocked = backup_in_the_way(eng)
    if blocked:
        # Nothing was swapped, so this is a folder that cannot be taken, not an update that failed.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise EngineLockError(blocked)
    bak_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[tuple[Path, Path]] = []
    created: list[Path] = []
    # `.bak` is deleted only once nothing needs it: the swap is kept, or every file went back.
    kept = False
    rolled_back = False

    def _rollback() -> bool:
        whole = True
        for dst, bak in reversed(backed_up):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if bak.exists():
                    os.replace(bak, dst)
            except Exception:
                try:
                    if bak.exists():
                        shutil.copy2(bak, dst)
                except Exception:
                    whole = False
        for created_file in reversed(created):
            try:
                if created_file.is_file():
                    created_file.unlink()
            except Exception:
                whole = False  # a file of the failed update stays in the engine
        return whole

    try:
        for rel_path, staged_src in staged_files.items():
            dst = eng / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                bak = bak_dir / rel_path
                backup_file(dst, bak)
                backed_up.append((dst, bak))
            else:
                created.append(dst)
            os.replace(staged_src, dst)

        for rel_del in deleted_rel_paths or []:
            dst = eng / rel_del
            if dst.is_file():
                bak = bak_dir / rel_del
                backup_file(dst, bak)
                backed_up.append((dst, bak))
                dst.unlink()

        probe = probe_fn(eng)
        if not probe.get("ok"):
            rolled_back = _rollback()
            return {
                "ok": False,
                "rolledBack": rolled_back,
                "probe": probe,
                "error": probe.get("error") or "gate_result probe failed",
            }
        kept = True
        return {
            "ok": True,
            "rolledBack": False,
            "probe": probe,
        }
    except Exception as exc:
        rolled_back = _rollback()
        return {
            "ok": False,
            "rolledBack": rolled_back,
            "probe": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    except BaseException:
        # Ctrl-C or an exit mid-swap. A second one inside the rollback leaves `.bak` for the next run.
        rolled_back = _rollback()
        raise
    finally:
        if kept:
            discard_backup(eng)
        elif rolled_back:
            shutil.rmtree(bak_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


def _failed_apply_text(apply_res: dict[str, Any], eng_dir: Path) -> str:
    """The probe's error plus what became of the engine folder, which the probe's error does not say."""
    err = str(apply_res.get("error") or "gate_result probe failed").rstrip(". ")
    if apply_res.get("rolledBack"):
        return f"{err}. {eng_dir} was put back as it was."
    return f"{err}. Not every file of {eng_dir} could be put back; run `pob stop` and repeat the command."


def _bootstrap_from_dir(
    from_dir: Path,
    eng_dir: Path,
    rt_dir: Path,
    probe_callable: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    src = Path(from_dir)
    from ..guardrails import validate_engine_root

    validate_engine_root(src, expected_game="poe1", source_label="--from-dir")
    if not is_valid_engine_dir(src):
        req_desc = (
            "PoE1/lua51.dll + Data/bridge/bridge.lua (or legacy pob-zh.exe + PoE1/)"
            if _pkg().is_windows()
            else "PoE1/, Data/bridge/bridge.lua, and LuaJIT"
        )
        raise UpdateError(f"Offline source directory is invalid (requires {req_desc}): {src}")
    eng_dir.mkdir(parents=True, exist_ok=True)
    # Through staging and the gate, like a download: a source that fails the probe must not be
    # left copied over an engine that worked.
    staging_dir = eng_dir / STAGING_DIRNAME
    shutil.rmtree(staging_dir, ignore_errors=True)
    try:
        staging_dir.mkdir(parents=True)
        for fname in LEGACY_SEED_FILES:
            if (src / fname).is_file():
                shutil.copy2(src / fname, staging_dir / fname)
        for dname in (*LEGACY_SEED_DIRS, "bin"):
            if (src / dname).is_dir():
                shutil.copytree(src / dname, staging_dir / dname)
    except OSError:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    staged = {p.relative_to(staging_dir): p for p in staging_dir.rglob("*") if p.is_file()}
    apply_res = _apply_staged_files_with_gate(eng_dir, staged, probe_fn=probe_callable)
    if not apply_res["ok"]:
        raise EngineGateError(f"Offline bootstrap failed gate_result probe: {_failed_apply_text(apply_res, eng_dir)}")
    probe = apply_res["probe"]
    vers = detect_local_versions(eng_dir, rt_dir)
    state = {
        "state": STATE_UPDATED,
        "localPobVersion": probe.get("pobVersion") or vers["localPobVersion"],
        "remotePobVersion": probe.get("pobVersion") or vers["localPobVersion"],
        "pobToolsVersion": vers["pobToolsVersion"],
        "pobToolsTag": vers["pobToolsTag"],
        "dataVersion": vers["dataVersion"],
        "checkedAt": time.time(),
        "engineDir": str(eng_dir),
        "networkError": None,
        "userHint": None,
        "source": f"offline:{src}",
        "probe": probe,
    }
    save_update_state(state, rt_dir)
    return state


def _stage_bootstrap_assets(
    eng_dir: Path,
    staging_dir: Path,
    tools_info: dict[str, Any],
    pob_manifest: PobManifest,
    *,
    include_zh: bool,
    proxy: str | None,
    mirror: str | None,
) -> tuple[dict[Path, Path], str | None]:
    pkg = _pkg()
    staged_files: dict[Path, Path] = {}

    # Download & stage PobTools-zh zip (extract ONLY Data/bridge/, Data/poe1/zh-rCN/, and Data/poe1/zh-rTW/ on all OSes)
    tools_manifest = tools_info.get("manifest")
    if not isinstance(tools_manifest, dict):
        raise UpdateError("PobTools-zh release manifest JSON missing.")
    app_asset = _pick_pobtools_asset(tools_manifest, prefer_update=False)
    if not app_asset:
        raise UpdateError("No PobTools-*.zip asset found in release manifest.")
    staged_files.update(
        _download_and_stage_release_zip(
            tools_info["tag"],
            app_asset,
            staging_dir,
            proxy=proxy,
            mirror=mirror,
            skip_prefixes=("PoE1",),
            only_prefixes=POBTOOLS_APP_ONLY_PREFIXES,
        )
    )

    # Ensure native POSIX LuaJIT + lua-utf8.so runtime on macOS / Linux
    if not pkg.is_windows():
        staged_files.update(
            pkg.ensure_posix_runtime(
                eng_dir,
                staging_dir,
                proxy=proxy,
                mirror=mirror,
            )
        )

    # Optional bilingual DataV2 pack (honoring minApp)
    installed_data_ver: str | None = None
    data_manifest = tools_info.get("dataManifest")
    if include_zh and isinstance(data_manifest, dict):
        min_app = data_manifest.get("minApp")
        if _is_version_gte(tools_info["version"], min_app):
            data_asset = _pick_data_asset(data_manifest)
            if data_asset:
                data_tag = str(data_manifest.get("tag") or tools_manifest.get("dataTag") or "")
                staged_files.update(
                    _download_and_stage_release_zip(
                        data_tag,
                        data_asset,
                        staging_dir,
                        proxy=proxy,
                        mirror=mirror,
                        skip_prefixes=("PoE1",),
                        only_prefixes=POBTOOLS_DATA_ONLY_PREFIXES,
                    )
                )
                installed_data_ver = data_tag or None

    # Download & stage official PoB PoE1/ slim files
    to_download, _ = diff_pob_manifest(eng_dir / "PoE1", pob_manifest, verify_local_sha1=True, slim=True)
    staged_files.update(
        _download_pob_files_to_staging(
            pob_manifest,
            to_download,
            staging_dir,
            proxy=proxy,
            mirror=mirror,
        )
    )
    manifest_staged = staging_dir / "PoE1" / "manifest.xml"
    manifest_staged.parent.mkdir(parents=True, exist_ok=True)
    manifest_staged.write_text(_render_local_manifest_xml(pob_manifest), encoding="utf-8")
    staged_files[Path("PoE1") / "manifest.xml"] = manifest_staged
    return staged_files, installed_data_ver


def bootstrap_engine(
    *,
    target_dir: Path | None = None,
    runtime_dir: Path | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    include_zh: bool = True,
    from_dir: Path | None = None,
    legacy_dir: Path | None = None,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bootstrap a complete managed PoB engine from offline directory, legacy seed, or GitHub dual upstreams."""
    rt_dir = _get_runtime_dir(runtime_dir)
    eng_dir = Path(target_dir) if target_dir is not None else (rt_dir / "engine")
    if from_dir is not None:  # refuse a wrong source before the engine folder is created to lock it
        from ..guardrails import validate_engine_root

        validate_engine_root(Path(from_dir), expected_game="poe1", source_label="--from-dir")
    with engine_update_lock(eng_dir):
        return _bootstrap_engine_locked(
            eng_dir,
            rt_dir,
            proxy=proxy,
            mirror=mirror,
            include_zh=include_zh,
            from_dir=from_dir,
            legacy_dir=legacy_dir,
            probe_callable=probe_fn or probe_engine_gate,
        )


def _bootstrap_engine_locked(
    eng_dir: Path,
    rt_dir: Path,
    *,
    proxy: str | None,
    mirror: str | None,
    include_zh: bool,
    from_dir: Path | None,
    legacy_dir: Path | None,
    probe_callable: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    pkg = _pkg()

    if from_dir is not None:
        return _bootstrap_from_dir(from_dir, eng_dir, rt_dir, probe_callable)

    leg = _find_legacy_dir(legacy_dir) if legacy_dir is not None else None
    if leg is not None and not is_valid_engine_dir(eng_dir):
        seed_from_legacy_dir(leg, eng_dir)
        return pkg.check_and_apply_updates(
            engine_dir=eng_dir,
            runtime_dir=rt_dir,
            proxy=proxy,
            mirror=mirror,
            include_zh=include_zh,
            force=True,
            probe_fn=probe_callable,
        )

    import concurrent.futures

    import httpx

    staging_dir = eng_dir / STAGING_DIRNAME
    check_timeout = _to_httpx_timeout(_CHECK_TIMEOUT_SPEC)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tools = pool.submit(
                pkg.fetch_pobtools_latest_info,
                proxy=proxy,
                mirror=mirror,
                timeout=check_timeout,
                fetch_manifest=True,
            )
            fut_pob = pool.submit(
                pkg.fetch_pob_manifest_remote,
                etag=None,
                proxy=proxy,
                mirror=mirror,
                timeout=check_timeout,
                slim=True,
            )
            tools_info = fut_tools.result()
            _, pob_manifest, pob_etag = fut_pob.result()
        assert pob_manifest is not None

        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)
        eng_dir.mkdir(parents=True, exist_ok=True)

        staged_files, installed_data_ver = _stage_bootstrap_assets(
            eng_dir,
            staging_dir,
            tools_info,
            pob_manifest,
            include_zh=include_zh,
            proxy=proxy,
            mirror=mirror,
        )

        apply_res = _apply_staged_files_with_gate(eng_dir, staged_files, probe_fn=probe_callable)
        if not apply_res["ok"]:
            err = apply_res.get("error") or "gate_result failed"
            rolled_back = bool(apply_res.get("rolledBack"))
            state = {
                "state": STATE_DEGRADED_ROLLBACK,
                "localPobVersion": None,
                "remotePobVersion": pob_manifest.version,
                "pobToolsVersion": None,
                "dataVersion": None,
                "checkedAt": time.time(),
                "engineDir": str(eng_dir),
                "networkError": err,
                "userHint": _build_user_hint(STATE_DEGRADED_ROLLBACK, None, err, rolled_back=rolled_back),
            }
            if rolled_back:
                state["rolledBack"] = True
            save_update_state(state, rt_dir)
            raise EngineGateError(_failed_apply_text(apply_res, eng_dir))

        _write_pobtools_meta(
            eng_dir,
            pobtools_version=tools_info["version"],
            pobtools_tag=tools_info["tag"],
            data_version=installed_data_ver,
        )
        state = {
            "state": STATE_UPDATED,
            "localPobVersion": pob_manifest.version,
            "remotePobVersion": pob_manifest.version,
            "pobToolsVersion": tools_info["version"],
            "pobToolsTag": tools_info["tag"],
            "dataVersion": installed_data_ver or detect_local_versions(eng_dir, rt_dir)["dataVersion"],
            "pobManifestEtag": pob_etag,
            "checkedAt": time.time(),
            "engineDir": str(eng_dir),
            "networkError": None,
            "userHint": None,
            "updatedFilesCount": len(staged_files),
            "probe": apply_res["probe"],
        }
        save_update_state(state, rt_dir)
        return state
    except (httpx.HTTPError, OSError, UpdateError) as exc:
        if isinstance(exc, EngineGateError):
            raise
        shutil.rmtree(staging_dir, ignore_errors=True)
        err_msg = f"{type(exc).__name__}: {exc}"
        state = {
            "state": STATE_BOOTSTRAP_MISSING,
            "localPobVersion": None,
            "remotePobVersion": None,
            "pobToolsVersion": None,
            "dataVersion": None,
            "checkedAt": time.time(),
            "engineDir": str(eng_dir),
            "networkError": err_msg,
            "userHint": _build_user_hint(STATE_BOOTSTRAP_MISSING, None, err_msg),
        }
        save_update_state(state, rt_dir)
        return state
