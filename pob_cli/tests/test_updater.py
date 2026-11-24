"""Unit and headless integration tests for updater.py and bridge_client.py (v0.2 engine-updater)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from engine_paths import find_legacy_snapshot
from support import dead_proxy_url

from pob_cli.bridge_client import (
    FORBIDDEN_METHODS,
    BridgeError,
    BridgeWorker,
)
from pob_cli.updater import (
    POB_MANIFEST_URL,
    POBTOOLS_DOWNLOAD_BASE,
    POBTOOLS_LATEST_URL,
    STATE_BOOTSTRAP_MISSING,
    STATE_DEGRADED_NETWORK,
    STATE_DEGRADED_ROLLBACK,
    STATE_UP_TO_DATE,
    STATE_UPDATED,
    bootstrap_engine,
    build_manifest_file_url,
    check_and_apply_updates,
    find_local_manifest_mismatches,
    format_stderr_update_alert,
    get_update_warning,
    load_update_state,
    parse_pob_manifest,
    probe_engine_gate,
    resolve_engine_root,
    resolve_proxy,
    rewrite_github_url,
    seed_from_legacy_dir,
    verify_pob_sha1,
)

REAL_LEGACY_DIR = find_legacy_snapshot()


def _encode_xml(xml_text: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml_text.encode("utf-8"), level=9)).decode("ascii")


def _make_fake_engine(root: Path, version: str = "2.67.2", files: dict[str, bytes] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pob-zh.exe").write_bytes(b"MZ-fake-exe")
    (root / "pob-zh.ini").write_text("[PobTools]\nGame=poe1\n", encoding="utf-8")
    (root / "engine").mkdir(exist_ok=True)
    (root / "engine" / "lua51.dll").write_bytes(b"fake-dll")
    (root / "bin").mkdir(exist_ok=True)
    fake_lj = root / "bin" / "luajit"
    fake_lj.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    try:
        fake_lj.chmod(0o755)
    except OSError:
        pass
    (root / "Data").mkdir(exist_ok=True)
    (root / "Data" / "bridge").mkdir(parents=True, exist_ok=True)
    (root / "Data" / "bridge" / "bridge.lua").write_text("-- fake bridge\n", encoding="utf-8")
    (root / "Data" / "translations_version.json").write_text(
        json.dumps({"dataVersion": "data-19"}), encoding="utf-8"
    )
    (root / "Fonts").mkdir(exist_ok=True)
    poe1 = root / "PoE1"
    poe1.mkdir(exist_ok=True)

    payloads = files or {
        "UpdateCheck.lua": b"return 'official'\n",
        "Modules/Main.lua": b"local x = 1\n",
    }
    file_lines: list[str] = []
    for rel, content in payloads.items():
        fp = poe1 / rel.replace("{space}", " ")
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
        sha = hashlib.sha1(content).hexdigest()
        file_lines.append(f'\t<File name="{rel}" part="program" sha1="{sha}" />')

    manifest_xml = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<PoBVersion>\n"
        f'\t<Version number="{version}" platform="win32" branch="master" />\n'
        '\t<Source part="default" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/" />\n'
        '\t<Source part="runtime" platform="win32" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/runtime/" />\n'
        '\t<Source part="program" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/src/" />\n'
        '\t<Source part="tree" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/src/" />\n'
        + "\n".join(file_lines)
        + "\n</PoBVersion>\n"
    )
    (poe1 / "manifest.xml").write_text(manifest_xml, encoding="utf-8")
    return root


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Resolution, legacy seeding, proxy & mirror helpers
# ---------------------------------------------------------------------------


def test_resolve_engine_root_priority_and_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "runtime"
    env_eng = _make_fake_engine(tmp_path / "env_engine")
    managed_eng = rt / "engine"
    legacy_eng = _make_fake_engine(tmp_path / "legacy_engine")

    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.delenv("POB_ROOT", raising=False)

    # Priority 3: legacy fallback when neither POB_ROOT nor managed exists
    assert resolve_engine_root(runtime_dir=rt, legacy_dir=legacy_eng) == legacy_eng

    # Seed from legacy -> copies clean assets without modifying legacy
    assert seed_from_legacy_dir(legacy_eng, managed_eng) is True
    assert (managed_eng / "pob-zh.exe").is_file()
    assert (managed_eng / "PoE1" / "UpdateCheck.lua").is_file()
    # Second call is a no-op
    assert seed_from_legacy_dir(legacy_eng, managed_eng) is False

    # Priority 2: managed runtime beats legacy
    assert resolve_engine_root(runtime_dir=rt, legacy_dir=legacy_eng) == managed_eng

    # Priority 1: explicit POB_ROOT beats managed
    monkeypatch.setenv("POB_ROOT", str(env_eng))
    assert resolve_engine_root(runtime_dir=rt, legacy_dir=legacy_eng) == env_eng


def test_proxy_and_mirror_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("POB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("POB_GITHUB_MIRROR", raising=False)

    assert resolve_proxy() is None
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    assert resolve_proxy() == "socks5://127.0.0.1:1080"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert resolve_proxy() == "http://127.0.0.1:7890"
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    assert resolve_proxy() == "http://127.0.0.1:7891"
    monkeypatch.setenv("POB_PROXY", "http://127.0.0.1:7892")
    assert resolve_proxy() == "http://127.0.0.1:7892"
    assert resolve_proxy("http://127.0.0.1:9999") == "http://127.0.0.1:9999"

    rewritten = rewrite_github_url(POB_MANIFEST_URL, mirror="https://mirror.example.com/")
    assert rewritten == f"https://mirror.example.com/{POB_MANIFEST_URL}"
    assert rewrite_github_url("https://poe.ninja/api", mirror="https://mirror.example.com") == "https://poe.ninja/api"


# ---------------------------------------------------------------------------
# 2. Invalid proxy fast degradation (< 4s) & dual-channel alerts
# ---------------------------------------------------------------------------


def test_invalid_proxy_fast_degradation_under_4s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "runtime"
    eng = _make_fake_engine(rt / "engine", version="2.67.2")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("POB_PROXY", dead_proxy_url())
    monkeypatch.delenv("POB_ROOT", raising=False)

    t0 = time.monotonic()
    state = check_and_apply_updates(engine_dir=eng, runtime_dir=rt)
    elapsed = time.monotonic() - t0

    assert elapsed < 4.0, f"Invalid proxy check took too long: {elapsed:.2f}s"
    assert state["state"] == STATE_DEGRADED_NETWORK
    assert state["localPobVersion"] == "2.67.2"
    assert state["networkError"]
    assert state["userHint"]
    assert "--proxy" in state["userHint"]
    assert "HTTPS_PROXY" in state["userHint"]

    warning = get_update_warning(state)
    assert warning == state["userHint"]
    stderr_alert = format_stderr_update_alert(state)
    assert stderr_alert is not None
    assert "2.67.2" in stderr_alert
    assert "--proxy" in stderr_alert

    persisted = load_update_state(rt)
    assert persisted["state"] == STATE_DEGRADED_NETWORK
    assert persisted["localPobVersion"] == "2.67.2"


def test_bootstrap_missing_offline_when_no_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "runtime"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("POB_LEGACY_DIR", "")
    monkeypatch.delenv("POB_ROOT", raising=False)

    state = check_and_apply_updates(runtime_dir=rt, offline=True)
    assert state["state"] == STATE_BOOTSTRAP_MISSING
    assert get_update_warning(state) is not None
    assert "pob bootstrap" in (state["userHint"] or "")


# ---------------------------------------------------------------------------
# 3. Real manifest.xml SHA1 verification & tampered UpdateCheck.lua detection
# ---------------------------------------------------------------------------


@pytest.mark.skipif(REAL_LEGACY_DIR is None, reason="Legacy PobTools 2.67.2 snapshot not present (POB_LEGACY_DIR)")
def test_real_manifest_sha1_detects_exact_tampered_update_check() -> None:
    poe1 = REAL_LEGACY_DIR / "PoE1"
    manifest = parse_pob_manifest((poe1 / "manifest.xml").read_text(encoding="utf-8"))
    assert manifest.version == "2.67.2"
    assert len(manifest.files) > 1300

    # Verify {space} URL escaping preserves literal `{space}` for GitHub raw while local_rel uses space
    exe_entry = manifest.files["Path{space}of{space}Building.exe"]
    assert exe_entry.local_rel == "Path of Building.exe"
    exe_url = build_manifest_file_url(manifest, exe_entry)
    assert exe_url.endswith("/runtime/Path%7Bspace%7Dof%7Bspace%7DBuilding.exe")

    # Across all 1,389 manifest files, ONLY tampered UpdateCheck.lua should fail SHA1!
    mismatches = find_local_manifest_mismatches(poe1)
    mismatch_names = [m.local_rel for m in mismatches]
    assert mismatch_names == ["UpdateCheck.lua"]


def test_verify_pob_sha1_raw_and_crlf_tolerance() -> None:
    lf_bytes = b"line1\nline2\n"
    crlf_bytes = b"line1\r\nline2\r\n"
    sha_crlf = hashlib.sha1(crlf_bytes).hexdigest()
    sha_lf = hashlib.sha1(lf_bytes).hexdigest()
    assert verify_pob_sha1(crlf_bytes, sha_crlf) is True
    assert verify_pob_sha1(lf_bytes, sha_crlf) is True
    assert verify_pob_sha1(crlf_bytes, sha_lf) is True
    assert verify_pob_sha1(b"tampered\n", sha_crlf) is False


# ---------------------------------------------------------------------------
# 4. Atomic staging + .bak/ swap + gate_result failure rollback & 304 repair
# ---------------------------------------------------------------------------


def _patch_updater_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    import pob_cli.updater as upd

    transport = httpx.MockTransport(handler)

    def _fake_client(
        *,
        timeout: httpx.Timeout,
        proxy: str | None = None,
        follow_redirects: bool = True,
        max_connections: int = 16,
    ) -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(upd, "_make_http_client", _fake_client)


def test_atomic_swap_and_gate_rollback_restores_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "runtime"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    orig_update_lua = b"-- tampered local version\n"
    orig_main_lua = b"-- v2.67.2 main\n"
    orig_obsolete = b"-- to be deleted in 2.68.1\n"
    eng = _make_fake_engine(
        rt / "engine",
        version="2.67.2",
        files={
            "UpdateCheck.lua": orig_update_lua,
            "Modules/Main.lua": orig_main_lua,
            "Modules/OldFile.lua": orig_obsolete,
        },
    )

    new_update_lua = b"-- official 2.68.1 UpdateCheck\n"
    new_main_lua = b"-- official 2.68.1 Main with count += 1\n"
    new_added_lua = b"-- new 3_30 module\n"

    remote_manifest_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<PoBVersion>\n"
        '\t<Version number="2.68.1" />\n'
        '\t<Source part="program" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/src/" />\n'
        f'\t<File name="UpdateCheck.lua" part="program" sha1="{hashlib.sha1(new_update_lua).hexdigest()}" />\n'
        f'\t<File name="Modules/Main.lua" part="program" sha1="{hashlib.sha1(new_main_lua).hexdigest()}" />\n'
        f'\t<File name="Modules/New330.lua" part="program" sha1="{hashlib.sha1(new_added_lua).hexdigest()}" />\n'
        "</PoBVersion>\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/Hsiung-Shao/PobTools-zh/releases/tag/v1.8.12"})
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/v1.8.12/PobTools-manifest-v1.8.12.json":
            return httpx.Response(
                200,
                json={"version": "1.8.12", "tag": "v1.8.12", "dataTag": "data-19", "assets": []},
            )
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/data-19/PobTools-manifest-data-19.json":
            return httpx.Response(200, json={"tag": "data-19", "minApp": "1.8.0", "assets": []})
        if url == POB_MANIFEST_URL:
            return httpx.Response(200, text=remote_manifest_xml, headers={"ETag": 'W/"etag-2681"'})
        if url.endswith("/src/UpdateCheck.lua"):
            return httpx.Response(200, content=new_update_lua)
        if url.endswith("/src/Modules/Main.lua"):
            return httpx.Response(200, content=new_main_lua)
        if url.endswith("/src/Modules/New330.lua"):
            return httpx.Response(200, content=new_added_lua)
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)

    # First attempt: broken gate_result -> must roll back EVERY modified, added, and deleted file!
    def failing_probe(_path: Path) -> dict[str, Any]:
        return {"ok": False, "error": "gate_result failed: ['Build.Init']", "failed": ["Build.Init"]}

    res_fail = check_and_apply_updates(
        engine_dir=eng,
        runtime_dir=rt,
        include_zh=False,
        probe_fn=failing_probe,
    )
    assert res_fail["state"] == STATE_DEGRADED_ROLLBACK
    assert res_fail.get("rolledBack") is True
    assert get_update_warning(res_fail) is not None
    assert (eng / "PoE1" / "UpdateCheck.lua").read_bytes() == orig_update_lua
    assert (eng / "PoE1" / "Modules" / "Main.lua").read_bytes() == orig_main_lua
    assert (eng / "PoE1" / "Modules" / "OldFile.lua").read_bytes() == orig_obsolete
    assert not (eng / "PoE1" / "Modules" / "New330.lua").exists()
    assert not (eng / ".staging").exists()
    assert not (eng / ".bak").exists()

    # Second attempt: passing gate_result -> must apply cleanly & persist ETag
    def passing_probe(_path: Path) -> dict[str, Any]:
        return {"ok": True, "pobVersion": "2.68.1", "bridgeVersion": "0.2.0", "caps": {"sidebarBreakdown": True}}

    res_ok = check_and_apply_updates(
        engine_dir=eng,
        runtime_dir=rt,
        include_zh=False,
        probe_fn=passing_probe,
    )
    assert res_ok["state"] == STATE_UPDATED
    assert res_ok["localPobVersion"] == "2.68.1"
    assert res_ok["pobManifestEtag"] == 'W/"etag-2681"'
    assert get_update_warning(res_ok) is None
    assert format_stderr_update_alert(res_ok) is None
    assert (eng / "PoE1" / "UpdateCheck.lua").read_bytes() == new_update_lua
    assert (eng / "PoE1" / "Modules" / "New330.lua").read_bytes() == new_added_lua
    assert not (eng / "PoE1" / "Modules" / "OldFile.lua").exists()


def test_tampered_file_repaired_even_when_etag_returns_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "runtime"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    clean_lua = b"return 'clean'\n"
    eng = _make_fake_engine(rt / "engine", version="2.68.1", files={"UpdateCheck.lua": clean_lua})
    (eng / ".pobtools_version.json").write_text(
        json.dumps({"pobToolsVersion": "1.8.12", "pobToolsTag": "v1.8.12", "dataVersion": "data-19"}),
        encoding="utf-8",
    )
    # Tamper local UpdateCheck.lua after manifest was written
    (eng / "PoE1" / "UpdateCheck.lua").write_bytes(b"if true then return 'none' end\n")

    remote_manifest_xml = (eng / "PoE1" / "manifest.xml").read_text(encoding="utf-8")
    etag_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/Hsiung-Shao/PobTools-zh/releases/tag/v1.8.12"})
        if url == POB_MANIFEST_URL:
            inm = request.headers.get("if-none-match")
            etag_seen.append(inm)
            if inm == 'W/"etag-same"':
                return httpx.Response(304, headers={"ETag": 'W/"etag-same"'})
            return httpx.Response(200, text=remote_manifest_xml, headers={"ETag": 'W/"etag-same"'})
        if url.endswith("/src/UpdateCheck.lua"):
            return httpx.Response(200, content=clean_lua)
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)

    # Seed update_state.json with persisted ETag
    (rt / "update_state.json").write_text(
        json.dumps(
            {
                "state": STATE_UP_TO_DATE,
                "localPobVersion": "2.68.1",
                "pobToolsVersion": "1.8.12",
                "pobToolsTag": "v1.8.12",
                "dataVersion": "data-19",
                "pobManifestEtag": 'W/"etag-same"',
            }
        ),
        encoding="utf-8",
    )

    res = check_and_apply_updates(
        engine_dir=eng,
        runtime_dir=rt,
        include_zh=False,
        probe_fn=lambda _p: {"ok": True, "pobVersion": "2.68.1", "caps": {}},
    )
    assert etag_seen == ['W/"etag-same"', None]
    assert res["state"] == STATE_UPDATED
    assert (eng / "PoE1" / "UpdateCheck.lua").read_bytes() == clean_lua


def test_clean_bootstrap_from_github_dual_upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = tmp_path / "runtime"
    target = rt / "engine"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    app_zip = _make_zip_bytes(
        {
            "PobTools-1.8.12/pob-zh.exe": b"MZ-new-host",
            "PobTools-1.8.12/engine/lua51.dll": b"host-lua51",
            "PobTools-1.8.12/Data/bridge/bridge.lua": b"-- bridge",
        }
    )
    data_zip = _make_zip_bytes(
        {
            "Data/translations_version.json": json.dumps({"dataVersion": "data-24"}).encode("utf-8"),
            "Data/poe1/zh-rCN/gems.json": b"{}",
            "Data/poe1/zh-rTW/gems.json": b"{}",
            "Data/poe1/ko-KR/gems.json": b"{}",
        }
    )
    pob_main = b"-- official PoB 2.68.1\n"
    remote_manifest_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<PoBVersion>\n"
        '\t<Version number="2.68.1" />\n'
        '\t<Source part="program" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/src/" />\n'
        f'\t<File name="Modules/Main.lua" part="program" sha1="{hashlib.sha1(pob_main).hexdigest()}" />\n'
        "</PoBVersion>\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "api.github.com" not in url, "Must NEVER call api.github.com!"
        if url == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/Hsiung-Shao/PobTools-zh/releases/tag/v1.8.12"})
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/v1.8.12/PobTools-manifest-v1.8.12.json":
            return httpx.Response(
                200,
                json={
                    "version": "1.8.12",
                    "tag": "v1.8.12",
                    "dataTag": "data-24",
                    "assets": [
                        {"name": "PobTools-1.8.12.zip", "sha256": hashlib.sha256(app_zip).hexdigest()},
                    ],
                },
            )
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/data-24/PobTools-manifest-data-24.json":
            return httpx.Response(
                200,
                json={
                    "tag": "data-24",
                    "minApp": "1.8.9",
                    "assets": [
                        {"name": "PobTools-DataV2-24.zip", "sha256": hashlib.sha256(data_zip).hexdigest()},
                    ],
                },
            )
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/v1.8.12/PobTools-1.8.12.zip":
            return httpx.Response(200, content=app_zip)
        if url == f"{POBTOOLS_DOWNLOAD_BASE}/data-24/PobTools-DataV2-24.zip":
            return httpx.Response(200, content=data_zip)
        if url == POB_MANIFEST_URL:
            return httpx.Response(200, text=remote_manifest_xml, headers={"ETag": 'W/"boot-2681"'})
        if url.endswith("/src/Modules/Main.lua"):
            return httpx.Response(200, content=pob_main)
        return httpx.Response(404)

    _patch_updater_http(monkeypatch, handler)
    import pob_cli.updater as upd_mod

    if not upd_mod.is_windows():
        monkeypatch.setattr(upd_mod, "ensure_posix_runtime", lambda *_a, **_kw: {})

    res = bootstrap_engine(
        target_dir=target,
        runtime_dir=rt,
        include_zh=True,
        probe_fn=lambda _p: {"ok": True, "pobVersion": "2.68.1", "bridgeVersion": "0.2.0", "caps": {}},
    )
    assert res["state"] == STATE_UPDATED
    assert res["localPobVersion"] == "2.68.1"
    assert res["pobToolsVersion"] == "1.8.12"
    assert res["dataVersion"] == "data-24"
    assert not (target / "pob-zh.exe").exists()
    assert (target / "Data" / "bridge" / "bridge.lua").read_bytes() == b"-- bridge"
    assert (target / "Data" / "poe1" / "zh-rCN" / "gems.json").is_file()
    assert (target / "Data" / "poe1" / "zh-rTW" / "gems.json").is_file()
    assert not (target / "Data" / "poe1" / "ko-KR").exists()
    assert (target / "PoE1" / "Modules" / "Main.lua").read_bytes() == pob_main


def test_mirror_serves_the_routine_check_but_hashes_come_from_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashes read through a mirror prove nothing about files that mirror serves."""
    from pob_cli.updater import UpdateError, fetch_pob_manifest_remote, fetch_pobtools_latest_info
    from pob_cli.updater.remote import _download_and_stage_release_zip

    mirror = "https://mirror.example.com"
    manifest_url = f"{POBTOOLS_DOWNLOAD_BASE}/v1.8.12/PobTools-manifest-v1.8.12.json"

    def pob_xml(version: str) -> str:
        return (
            "<?xml version='1.0' encoding='UTF-8'?><PoBVersion>"
            f'<Version number="{version}" />'
            '<Source part="program" url="https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{branch}/src/" />'
            f'<File name="Modules/Main.lua" part="program" sha1="{hashlib.sha1(b"main").hexdigest()}" />'
            "</PoBVersion>"
        )

    seen: list[str] = []
    github_up = True
    github_cut_off = False

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        via_mirror = url.startswith(mirror)
        if not via_mirror and not github_up:
            raise httpx.ConnectError("GitHub unreachable")
        plain = url.removeprefix(mirror + "/")
        if plain == POBTOOLS_LATEST_URL:
            return httpx.Response(302, headers={"Location": "https://github.com/x/y/releases/tag/v1.8.12"})
        if plain == manifest_url:
            return httpx.Response(200, json={"version": "1.8.12", "assets": []})
        if plain == POB_MANIFEST_URL:
            if request.headers.get("If-None-Match"):
                return httpx.Response(304)
            if github_cut_off and not via_mirror:
                return httpx.Response(200, text=pob_xml("2.68.1")[:60])
            return httpx.Response(200, text=pob_xml("9.9.9" if via_mirror else "2.68.1"))
        return httpx.Response(200, content=b"not the zip the manifest hashed")

    _patch_updater_http(monkeypatch, handler)

    def requests_of(call: Any) -> tuple[Any, list[str]]:
        seen.clear()
        return call(), list(seen)

    # The routine check (a tag, or a 304) stays on the mirror: no extra round trip, no GitHub timeout.
    assert requests_of(lambda: fetch_pobtools_latest_info(mirror=mirror, fetch_manifest=False))[1] == [
        f"{mirror}/{POBTOOLS_LATEST_URL}"
    ]
    assert requests_of(lambda: fetch_pob_manifest_remote(mirror=mirror, etag="abc")) == (
        ("not_modified", None, "abc"),
        [f"{mirror}/{POB_MANIFEST_URL}"],
    )

    # Anything that carries hashes comes from GitHub itself, whatever the mirror says.
    assert requests_of(lambda: fetch_pobtools_latest_info(mirror=mirror))[1] == [POBTOOLS_LATEST_URL, manifest_url]
    (status, manifest, _etag), urls = requests_of(lambda: fetch_pob_manifest_remote(mirror=mirror))
    assert (status, manifest.version) == ("ok", "2.68.1")
    assert urls == [f"{mirror}/{POB_MANIFEST_URL}", POB_MANIFEST_URL]

    # A cut-off answer from GitHub is no answer: the mirror's copy stands.
    github_cut_off = True
    assert fetch_pob_manifest_remote(mirror=mirror)[1].version == "9.9.9"
    github_cut_off = False

    # Without a route to GitHub the mirror's copy is used, so those users can still update.
    github_up = False
    info, urls = requests_of(lambda: fetch_pobtools_latest_info(mirror=mirror))
    assert info["version"] == "1.8.12"
    assert urls[-2:] == [f"{mirror}/{POBTOOLS_LATEST_URL}", f"{mirror}/{manifest_url}"]
    assert fetch_pob_manifest_remote(mirror=mirror)[1].version == "9.9.9"

    # The engine zip itself may come through the mirror, but never without a hash to check it by.
    for asset in ({"name": "PobTools-1.8.12.zip"}, {"name": "PobTools-1.8.12.zip", "sha256": "00" * 32}):
        with pytest.raises(UpdateError, match="SHA256 mismatch"):
            _download_and_stage_release_zip("v1.8.12", asset, tmp_path, mirror=mirror)


def test_posix_engine_validation_command_and_manifest_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pob_cli.updater as upd_mod

    # 1. Bundled headless_runner.lua must always be shipped inside package
    runner_path = upd_mod.get_bundled_headless_runner_path()
    assert runner_path.is_file()
    assert "PobToolsBridgeSetDispatcher" in runner_path.read_text(encoding="utf-8")

    # 2. Manifest filtering on POSIX skips win32 runtime DLLs while keeping pure Lua files
    manifest_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<PoBVersion>\n"
        '\t<Version number="2.68.1" />\n'
        '\t<Source part="program" url="https://example.invalid/src/" />\n'
        '\t<Source part="runtime" url="https://example.invalid/runtime/" />\n'
        '\t<File name="Modules/Main.lua" part="program" sha1="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" />\n'
        '\t<File name="lua/dkjson.lua" part="runtime" sha1="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" />\n'
        '\t<File name="SimpleGraphic.dll" part="runtime" runtime="win32" sha1="cccccccccccccccccccccccccccccccccccccccc" />\n'
        "</PoBVersion>\n"
    )
    win_man = parse_pob_manifest(manifest_xml, include_win32_runtime=True)
    posix_man = parse_pob_manifest(manifest_xml, include_win32_runtime=False)
    assert "SimpleGraphic.dll" in win_man.files
    assert "SimpleGraphic.dll" not in posix_man.files
    assert "Modules/Main.lua" in posix_man.files
    assert "lua/dkjson.lua" in posix_man.files

    # 3. Simulate macOS/Linux engine validation and command construction
    monkeypatch.setattr(upd_mod, "is_windows", lambda: False)
    monkeypatch.delenv("POB_ENGINE_CMD", raising=False)
    monkeypatch.delenv("POB_LUAJIT", raising=False)
    monkeypatch.setattr(upd_mod.shutil, "which", lambda _name: None)

    eng = tmp_path / "posix_eng"
    (eng / "PoE1").mkdir(parents=True)
    assert upd_mod.is_valid_engine_dir(eng) is False

    (eng / "Data" / "bridge").mkdir(parents=True)
    (eng / "Data" / "bridge" / "bridge.lua").write_text("-- bridge\n", encoding="utf-8")
    assert upd_mod.is_valid_engine_dir(eng) is False

    (eng / "bin").mkdir(parents=True)
    lj = eng / "bin" / "luajit"
    lj.write_text("#!/bin/sh\n", encoding="utf-8")
    assert upd_mod.is_valid_engine_dir(eng) is True

    cmd, extra_env = upd_mod.build_engine_command(eng)
    assert cmd == [
        str(lj),
        str(runner_path),
        str(eng / "PoE1"),
        str(eng / "Data" / "bridge" / "bridge.lua"),
    ]
    assert extra_env["POB_ZH_HEADLESS"] == "1"
    assert extra_env["POB_GAME"] == "poe1"
    assert extra_env["POB_ENGINE_ROOT"] == str(eng)


# ---------------------------------------------------------------------------
# 5. Real headless engine integration: probe_engine_gate, dynamic R2, import_code deadlock heal
# ---------------------------------------------------------------------------


@pytest.mark.skipif(REAL_LEGACY_DIR is None, reason="Legacy PobTools 2.67.2 snapshot not present (POB_LEGACY_DIR)")
def test_real_probe_engine_gate() -> None:
    res = probe_engine_gate(REAL_LEGACY_DIR, timeout_sec=20.0)
    assert res["ok"] is True
    assert res["pobVersion"] == "2.67.2"
    assert res["bridgeVersion"]
    assert isinstance(res["caps"], dict)


@pytest.mark.skipif(REAL_LEGACY_DIR is None, reason="Legacy PobTools 2.67.2 snapshot not present (POB_LEGACY_DIR)")
def test_bridge_worker_dynamic_r2_and_import_code_deadlock_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "runtime"))

    # Dynamic R2: sidebar_breakdown must NOT be in static FORBIDDEN_METHODS
    assert "sidebar_breakdown" not in FORBIDDEN_METHODS

    worker = BridgeWorker(slot_name="test", pob_dir=REAL_LEGACY_DIR)
    try:
        worker.start()

        # On 2.67.2, caps.sidebarBreakdown is False -> dynamic R2 must reject it
        assert not worker.caps.get("sidebarBreakdown")
        with pytest.raises(BridgeError, match="R2"):
            worker.call("sidebar_breakdown", {})

        # Verify dynamic R2 ALLOWS sidebar_breakdown when caps.sidebarBreakdown is True
        worker.caps["sidebarBreakdown"] = True
        try:
            # 2.67.2 bridge doesn't implement sidebar_breakdown, so it reaches bridge (NOT blocked by R2)
            with pytest.raises(BridgeError) as exc_sb:
                worker.call("sidebar_breakdown", {})
            assert "R2:" not in str(exc_sb.value)
        finally:
            worker.caps["sidebarBreakdown"] = False

        # Build a genuine PoB XML with level=77 (disable auto-level) and checkpoint last_good_code
        import re as _re

        base_xml = worker.call("save_xml", {})["xml"]
        valid_xml = _re.sub(r'(<Build\b[^>]*\blevel=")[^"]+(")', r"\g<1>77\g<2>", base_xml, count=1)
        valid_xml = valid_xml.replace('characterLevelAutoMode="true"', 'characterLevelAutoMode="false"')
        assert 'level="77"' in valid_xml
        worker.call("import_code", {"code": _encode_xml(valid_xml), "mode": "replace"})
        checkpoint = worker.update_checkpoint()
        assert checkpoint
        info_before = worker.call("get_build_info", {})
        assert info_before["level"] == 77

        # Case A: Future treeVersion="9_99" -> clear error citing 9_99, 3_29, and `pob update`; slot remains intact
        future_xml = _re.sub(r'treeVersion="[^"]+"', 'treeVersion="9_99"', valid_xml)
        if 'treeVersion="9_99"' not in future_xml:
            future_xml = valid_xml.replace("<Spec ", '<Spec treeVersion="9_99" ', 1)
        assert 'treeVersion="9_99"' in future_xml
        with pytest.raises(BridgeError) as exc_999:
            worker.call("import_code", {"code": _encode_xml(future_xml), "mode": "replace"})
        msg_999 = str(exc_999.value)
        assert "9_99" in msg_999
        assert "3_29" in msg_999
        assert "pob update" in msg_999

        # Slot must still be alive with the original level 77 build
        assert worker.call("get_build_info", {})["level"] == 77
        assert worker.call("get_stats", {})["count"] > 100

        # Case B: Corrupt XML -> raises BridgeError AND preserves/restores original level 77 build
        corrupt_code = _encode_xml("<PathOfBuilding><Broken")
        with pytest.raises(BridgeError):
            worker.call("import_code", {"code": corrupt_code, "mode": "replace"})
        assert worker.call("get_build_info", {})["level"] == 77
        assert worker.call("get_stats", {})["count"] > 100

        # Case C: Real post-b:Shutdown() Lua crash inside b:Init() (genuine XML with treeVersion="9_99"
        # and allocated nodes bypassing pre-check triggers PoB's exact `no build is loaded` deadlock!)
        deadlock_xml = _re.sub(
            r"<Spec\b[^>]*>",
            '<Spec classId="3" ascendClassId="1" treeVersion="9_99" nodes="54447,12345" masteryEffects="{40196,50906}">',
            valid_xml,
            count=1,
        )
        monkeypatch.setattr(worker, "_check_tree_version_mismatch", lambda _code: None)
        with pytest.raises(BridgeError):
            worker.call("import_code", {"code": _encode_xml(deadlock_xml), "mode": "replace"})

        # Without healing, PoB would permanently error with `no build is loaded`; verify full restoration!
        info_after = worker.call("get_build_info", {})
        assert info_after["level"] == 77
        stats_after = worker.call("get_stats", {})
        assert stats_after["count"] > 100
    finally:
        worker.stop()


def test_daemon_hot_reload_serializes_concurrent_get_worker_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify DLL-unlock concurrency safety: get_worker/status/periodic check cannot touch workers mid-hot-reload."""
    import threading

    import pob_cli.daemon as daemon_mod

    rt = tmp_path / "runtime"
    eng = _make_fake_engine(rt / "engine", version="2.67.2")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    events: list[str] = []
    reload_entered = threading.Event()
    allow_reload_finish = threading.Event()
    check_calls = 0

    class _FakeWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.alive = False
            self.stopped = False
            self.last_good_code: str | None = None
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, Any] = {"pobVersion": "2.67.2"}

        def is_alive(self) -> bool:
            return self.alive

        def start(self) -> None:
            if self.stopped:
                raise AssertionError("Old stopped worker restarted during hot-reload!")
            self.alive = True

        def stop(self) -> None:
            self.alive = False
            self.stopped = True

        def ensure_alive(self) -> None:
            if self.stopped:
                raise AssertionError("ensure_alive() called on stopped worker mid-hot-reload!")
            self.alive = True

        def update_checkpoint(self) -> str:
            return "fake-code"

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if self.stopped:
                raise AssertionError(f"RPC {method} called on stopped worker!")
            return {"level": 90}

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _FakeWorker)

    def _fake_update(**kwargs: Any) -> dict[str, Any]:
        nonlocal check_calls
        check_calls += 1
        if kwargs.get("check_only"):
            return {"state": STATE_UP_TO_DATE, "updateAvailable": True, "checkedAt": time.time()}
        events.append("reload_updating")
        reload_entered.set()
        assert allow_reload_finish.wait(timeout=5.0)
        events.append("reload_done")
        return {"state": STATE_UPDATED, "localPobVersion": "2.68.1", "checkedAt": time.time()}

    monkeypatch.setattr(daemon_mod, "check_and_apply_updates", _fake_update)

    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=False)
    mgr.main_worker.start()
    old_worker = mgr.main_worker

    # Trigger _hot_reload_update in thread 1
    errs: list[BaseException] = []

    def _run_reload() -> None:
        try:
            mgr._hot_reload_update()
        except BaseException as exc:
            errs.append(exc)

    t_reload = threading.Thread(target=_run_reload)
    t_reload.start()
    assert reload_entered.wait(timeout=3.0)

    # While old_worker is stopped and DLLs are unlocked, concurrent get_worker & status must block cleanly
    def _run_get_worker() -> None:
        try:
            w = mgr.get_worker("main")
            assert w is not old_worker
            events.append("get_worker_after_reload")
        except BaseException as exc:
            errs.append(exc)

    def _run_status() -> None:
        try:
            st = mgr.dispatch("status", {})
            assert st["main"]["alive"] is True
            events.append("status_after_reload")
        except BaseException as exc:
            errs.append(exc)

    t_gw = threading.Thread(target=_run_get_worker)
    t_st = threading.Thread(target=_run_status)
    t_gw.start()
    t_st.start()
    time.sleep(0.1)

    # Neither concurrent thread could proceed before reload_done
    assert events == ["reload_updating"]
    allow_reload_finish.set()

    t_reload.join(timeout=3.0)
    t_gw.join(timeout=3.0)
    t_st.join(timeout=3.0)
    assert not errs, f"Unexpected thread errors: {errs}"
    assert events[0] == "reload_updating"
    assert events[1] == "reload_done"
    assert set(events[2:]) == {"get_worker_after_reload", "status_after_reload"}

    # Double-checked _maybe_periodic_update_check: stale timestamp triggers check_only=True once across concurrent threads
    mgr.update_status = {"state": STATE_UP_TO_DATE, "checkedAt": 0.0}
    check_calls = 0
    t_p1 = threading.Thread(target=mgr._maybe_periodic_update_check)
    t_p2 = threading.Thread(target=mgr._maybe_periodic_update_check)
    t_p1.start()
    t_p2.start()
    t_p1.join(timeout=3.0)
    t_p2.join(timeout=3.0)
    # 1 check_only + 1 _hot_reload_update (second thread sees fresh checkedAt after lock release)
    assert check_calls == 2


def test_get_shared_cache_dir_priority_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POB_CACHE_DIR override, shared engine parent cache for swarm workers, and tmp_path isolation."""
    from pob_cli.updater import get_shared_cache_dir

    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    monkeypatch.delenv("POB_ROOT", raising=False)
    monkeypatch.setenv("POB_LEGACY_DIR", "")

    # 1. Isolated runtime with its own managed engine -> uses <runtime_dir>/cache
    rt_iso = tmp_path / "iso_rt"
    _make_fake_engine(rt_iso / "engine")
    assert get_shared_cache_dir(rt_iso) == rt_iso / "cache"

    # 2. Swarm worker with separate POB_RUNTIME_DIR sharing engine under `.runtime/engine` -> shares `.runtime/cache`
    shared_root = tmp_path / ".runtime"
    shared_eng = _make_fake_engine(shared_root / "engine")
    swarm_rt = shared_root / "swarm_worker_1"
    swarm_rt.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POB_ROOT", str(shared_eng))
    assert get_shared_cache_dir(swarm_rt) == shared_root / "cache"

    # 2b. Engine under `pob4agents/engine` or legacy `pob-agent-tools/engine` also shares parent cache
    for app_dir_name in ("pob4agents", "pob-agent-tools"):
        app_root = tmp_path / app_dir_name
        app_eng = _make_fake_engine(app_root / "engine")
        monkeypatch.setenv("POB_ROOT", str(app_eng))
        assert get_shared_cache_dir(swarm_rt) == app_root / "cache"

    # 3. External engine whose parent is NOT `.runtime`/`pob4agents`/`pob-agent-tools` and has no `cache/` -> stays isolated in rt/cache
    other_eng = _make_fake_engine(tmp_path / "custom_parent" / "my_eng")
    monkeypatch.setenv("POB_ROOT", str(other_eng))
    assert get_shared_cache_dir(swarm_rt) == swarm_rt / "cache"

    # 4. A folder next to the engine that is only *named* cache is somebody else's: leave it alone
    (tmp_path / "custom_parent" / "cache").mkdir()
    (tmp_path / "custom_parent" / "cache" / "thumbnails.db").write_bytes(b"not ours")
    assert get_shared_cache_dir(swarm_rt) == swarm_rt / "cache"

    # 4a. Once pob has written to it (an index or a trade cache file) -> shares it
    (tmp_path / "custom_parent" / "cache" / "trade_ratelimit.json").write_text("{}", encoding="utf-8")
    assert get_shared_cache_dir(swarm_rt) == tmp_path / "custom_parent" / "cache"

    # 4b. Explicit pob_root parameter takes precedence over env POB_ROOT (isolates legacy/custom engines)
    monkeypatch.setenv("POB_ROOT", str(shared_eng))
    legacy_eng = _make_fake_engine(tmp_path / "legacy_parent" / "PobTools[20261102]")
    iso_test_rt = tmp_path / "iso_test_rt"
    assert get_shared_cache_dir(iso_test_rt, pob_root=legacy_eng) == iso_test_rt / "cache"

    # 5. Explicit POB_CACHE_DIR always wins
    explicit_cache = tmp_path / "explicit_cache"
    monkeypatch.setenv("POB_CACHE_DIR", str(explicit_cache))
    assert get_shared_cache_dir(swarm_rt) == explicit_cache
    assert explicit_cache.is_dir()


def test_find_local_manifest_mismatches_fast_stat_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify fast_stat skips re-hashing unchanged files while catching edits, deletions, and force full-SHA1."""
    import pob_cli.updater as upd_mod

    eng = _make_fake_engine(
        tmp_path / "eng",
        version="2.68.1",
        files={
            "UpdateCheck.lua": b"return 'official'\n",
            "Modules/Main.lua": b"local x = 1\n",
        },
    )
    poe1 = eng / "PoE1"
    cache_dir = tmp_path / "cache"

    # First call populates manifest_stat_cache.json
    assert find_local_manifest_mismatches(poe1, fast_stat=True, cache_dir=cache_dir) == []
    assert (cache_dir / "manifest_stat_cache.json").is_file()

    # Second call with unchanged files must NOT call verify_pob_sha1 at all (pure stat fast path)
    sha_calls = 0
    orig_verify = upd_mod.verify_pob_sha1

    def _counting_verify(data: bytes, expected: str) -> bool:
        nonlocal sha_calls
        sha_calls += 1
        return orig_verify(data, expected)

    monkeypatch.setattr(upd_mod, "verify_pob_sha1", _counting_verify)
    assert find_local_manifest_mismatches(poe1, fast_stat=True, cache_dir=cache_dir) == []
    assert sha_calls == 0

    # Explicit fast_stat=False always performs full SHA1 verification
    assert find_local_manifest_mismatches(poe1, fast_stat=False, cache_dir=cache_dir) == []
    assert sha_calls == 2

    # Modifying a file invalidates its stat signature and triggers detection even with fast_stat=True
    sha_calls = 0
    (poe1 / "UpdateCheck.lua").write_bytes(b"return 'tampered_payload'\n")
    mismatches = find_local_manifest_mismatches(poe1, fast_stat=True, cache_dir=cache_dir)
    assert [m.local_rel for m in mismatches] == ["UpdateCheck.lua"]
    assert sha_calls == 1


def test_daemon_start_non_blocking_background_update_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify DaemonManager.start() starts serving immediately when engine exists and checks updates in background."""
    import threading

    import pob_cli.daemon as daemon_mod

    rt = tmp_path / "rt"
    eng = _make_fake_engine(rt / "engine", version="2.68.1")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    bg_started = threading.Event()
    bg_release = threading.Event()
    recorded_kwargs: dict[str, Any] = {}

    class _FastFakeWorker:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.alive = False
            self.last_good_code: str | None = None
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, Any] = {"pobVersion": "2.68.1"}

        def is_alive(self) -> bool:
            return self.alive

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.alive = False

    def _slow_check(**kwargs: Any) -> dict[str, Any]:
        recorded_kwargs.update(kwargs)
        bg_started.set()
        bg_release.wait(timeout=5.0)
        return {
            "state": STATE_DEGRADED_NETWORK,
            "localPobVersion": "2.68.1",
            "checkedAt": time.time(),
            "userHint": "网络不可达，请配置代理",
        }

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _FastFakeWorker)
    monkeypatch.setattr(daemon_mod, "check_and_apply_updates", _slow_check)

    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=False)
    t0 = time.monotonic()
    mgr.start()
    elapsed = time.monotonic() - t0

    # Must return immediately without waiting for slow network update check!
    assert elapsed < 0.5
    assert mgr.main_worker.is_alive() is True
    assert bg_started.wait(timeout=3.0)
    assert recorded_kwargs.get("check_only") is True

    # P1-1: status dispatch (both normal and skip_periodic_check) must NOT block on active _boot_update_thread!
    t_st = time.monotonic()
    st_res = mgr.dispatch("status", {})
    assert (time.monotonic() - t_st) < 0.5
    assert st_res["main"]["alive"] is True
    t_pr = time.monotonic()
    st_pr = mgr.dispatch("status", {"skip_periodic_check": True})
    assert (time.monotonic() - t_pr) < 0.5
    assert st_pr["main"]["alive"] is True

    bg_release.set()
    assert mgr._boot_update_thread is not None
    mgr._boot_update_thread.join(timeout=3.0)
    assert mgr.update_status["state"] == STATE_DEGRADED_NETWORK
    assert mgr._current_update_warning() == "网络不可达，请配置代理"


def test_daemon_boot_bg_update_triggers_hot_reload_and_fresh_ok_checks_update_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P1-1: boot check_only with updateAvailable=True invokes _hot_reload_update and fresh_ok rejects updateAvailable."""
    import threading

    import pob_cli.daemon as daemon_mod

    rt = tmp_path / "rt"
    eng = _make_fake_engine(rt / "engine", version="2.68.1")
    monkeypatch.setenv("POB_RUNTIME_DIR", str(rt))

    hot_reload_called = threading.Event()
    check_calls: list[bool] = []

    class _FakeW:
        def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
            self.slot_name = slot_name
            self.pob_root = pob_dir or eng
            self.alive = False
            self.last_good_code: str | None = None
            self.undo_stack: list[str] = []
            self.redo_stack: list[str] = []
            self.source_label: str | None = None
            self.hello_info: dict[str, Any] = {}

        def is_alive(self) -> bool:
            return self.alive

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.alive = False

        def update_checkpoint(self) -> str:
            return ""

    def _fake_check(**kwargs: Any) -> dict[str, Any]:
        is_check_only = bool(kwargs.get("check_only", False))
        check_calls.append(is_check_only)
        if is_check_only:
            return {
                "state": STATE_UP_TO_DATE,
                "updateAvailable": True,
                "localPobVersion": "2.68.1",
                "remotePobVersion": "2.69.0",
                "checkedAt": time.time(),
                "engineDir": str(eng),
            }
        hot_reload_called.set()
        return {
            "state": STATE_UPDATED,
            "updateAvailable": False,
            "localPobVersion": "2.69.0",
            "remotePobVersion": "2.69.0",
            "checkedAt": time.time(),
            "engineDir": str(eng),
        }

    monkeypatch.setattr(daemon_mod, "BridgeWorker", _FakeW)
    monkeypatch.setattr(daemon_mod, "check_and_apply_updates", _fake_check)

    # Even if previous persisted state has fresh checkedAt + STATE_UP_TO_DATE, updateAvailable=True must NOT skip boot check
    mgr = daemon_mod.DaemonManager(pob_dir=eng, token="tok", runtime_dir=rt, offline=False)
    mgr.update_status = {
        "state": STATE_UP_TO_DATE,
        "updateAvailable": True,
        "checkedAt": time.time(),
        "engineDir": str(eng),
    }
    mgr.start()
    assert mgr._boot_update_thread is not None
    mgr._boot_update_thread.join(timeout=3.0)
    assert hot_reload_called.is_set()
    assert check_calls == [True, False]
    assert mgr.update_status["state"] == STATE_UPDATED
    assert mgr.update_status["updateAvailable"] is False


def test_updater_edge_hardening_cache_pob_root_readonly_and_tmp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P2-4: check_and_apply_updates forwards pob_root=active_dir, get_shared_cache_dir handles OSError, and tmp is unlinked."""
    import os

    import pob_cli.updater as upd_mod

    # 1. check_and_apply_updates passes pob_root=active_dir to get_shared_cache_dir
    rt = tmp_path / "rt"
    explicit_eng = _make_fake_engine(tmp_path / "explicit_eng", version="2.68.1")
    seen_pob_roots: list[Path | None] = []
    orig_get_cache = upd_mod.get_shared_cache_dir

    def _spy_get_cache(runtime_dir: Path | None = None, *, pob_root: Path | None = None) -> Path:
        seen_pob_roots.append(pob_root)
        return orig_get_cache(runtime_dir, pob_root=pob_root)

    monkeypatch.setattr(upd_mod, "get_shared_cache_dir", _spy_get_cache)
    monkeypatch.setattr(
        upd_mod,
        "fetch_pob_manifest_remote",
        lambda **_kw: ("not_modified", None, '"etag-1"'),
    )
    monkeypatch.setattr(
        upd_mod,
        "fetch_pobtools_latest_info",
        lambda **_kw: {"tag": "v1.0.0", "version": "1.0.0"},
    )
    (explicit_eng / upd_mod.POBTOOLS_META_FILENAME).write_text(
        json.dumps({"pobToolsVersion": "1.0.0", "pobToolsTag": "v1.0.0", "dataVersion": "d1"}),
        encoding="utf-8",
    )
    upd_mod.check_and_apply_updates(engine_dir=explicit_eng, runtime_dir=rt, offline=False, check_only=True)
    assert seen_pob_roots == [explicit_eng]

    # 2. get_shared_cache_dir catches OSError on mkdir() and falls back cleanly
    monkeypatch.setattr(upd_mod, "get_shared_cache_dir", orig_get_cache)
    ro_env_cache = tmp_path / "ro_env_cache"
    ro_rt = tmp_path / "ro_rt"
    monkeypatch.setenv("POB_CACHE_DIR", str(ro_env_cache))
    orig_mkdir = Path.mkdir

    def _failing_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self in (ro_env_cache, ro_rt / "cache"):
            raise PermissionError("read-only filesystem")
        return orig_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)
    res_dir = orig_get_cache(ro_rt, pob_root=ro_rt / "engine")
    assert res_dir != ro_env_cache
    assert res_dir != ro_rt / "cache"
    monkeypatch.delenv("POB_CACHE_DIR", raising=False)
    monkeypatch.setattr(Path, "mkdir", orig_mkdir)

    # 3. _save_manifest_stat_cache cleans up tmp file if os.replace fails
    cache_file = tmp_path / "stat_cache_dir" / "manifest_stat_cache.json"
    monkeypatch.setattr(os, "replace", lambda _src, _dst: (_ for _ in ()).throw(PermissionError("locked")))
    upd_mod._save_manifest_stat_cache(cache_file, "key", "sha1", {"a.lua": [1, 2, "abc"]})
    leftovers = list(cache_file.parent.glob("*.tmp"))
    assert leftovers == [], f"Leftover tmp files were not cleaned up: {leftovers}"
