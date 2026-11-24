"""On-demand `TreeData/<ver>/` staging: completeness, all-or-nothing downloads, budgets, errors."""

from __future__ import annotations

import base64
import hashlib
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

import pob_cli.bridge_protocol as bp
from pob_cli.bridge_protocol import BridgeError, stage_missing_tree_versions
from pob_cli.updater.constants import UpdateError
from pob_cli.updater.remote import ensure_tree_data_files, missing_tree_data_files

TREE_319 = b"return { tree = '3_19' }\n" * 4
ASSETS_319 = b"return { assets = true }\n"
TREE_328 = b"return { tree = '3_28' }\n"
SPRITES_328 = b"return { sprites = true }\n"


def _manifest(files: dict[str, bytes]) -> str:
    lines = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        "<PoBVersion>",
        '\t<Version number="2.68.1" platform="win32" branch="master" />',
        '\t<Source part="tree" url="https://example.invalid/src/" />',
    ]
    for name, data in files.items():
        lines.append(f'\t<File name="{name}" part="tree" sha1="{hashlib.sha1(data).hexdigest()}" />')
    lines.append("</PoBVersion>")
    return "\n".join(lines) + "\n"


FILES = {
    "TreeData/3_19/Assets.lua": ASSETS_319,
    "TreeData/3_19/tree.lua": TREE_319,
    "TreeData/3_28/sprites.lua": SPRITES_328,
    "TreeData/3_28/tree.lua": TREE_328,
}


@pytest.fixture()
def poe1(tmp_path: Path) -> Path:
    root = tmp_path / "PoE1"
    (root / "TreeData" / "3_19").mkdir(parents=True)
    (root / "manifest.xml").write_text(_manifest(FILES), encoding="utf-8")
    # What the slim engine keeps: the shared 3.19 assets without any 3.19 tree
    (root / "TreeData" / "3_19" / "Assets.lua").write_bytes(ASSETS_319)
    return root


def _serve(monkeypatch: pytest.MonkeyPatch, fail: set[str] | None = None) -> list[str]:
    import pob_cli.updater as upd

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        for name, data in FILES.items():
            if url.endswith("/" + name):
                return httpx.Response(500 if name in (fail or set()) else 200, content=data)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def _client(*, timeout: Any, proxy: str | None = None, follow_redirects: bool = True, max_connections: int = 16) -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(upd, "_make_http_client", _client)
    monkeypatch.setattr("pob_cli.updater.remote.time.sleep", lambda _s: None)
    monkeypatch.delenv("POB_OFFLINE", raising=False)
    return seen


def _code(*tree_versions: str) -> str:
    specs = "".join(f'<Spec treeVersion="{tv}" classId="0" nodes=""/>' for tv in tree_versions)
    xml = f'<PathOfBuilding><Build level="1"/><Tree activeSpec="1">{specs}</Tree></PathOfBuilding>'
    return base64.urlsafe_b64encode(zlib.compress(xml.encode())).decode()


@pytest.fixture(autouse=True)
def _clear_failure_cache() -> Any:
    bp._FAILED_TREE_STAGE.clear()
    yield
    bp._FAILED_TREE_STAGE.clear()


def test_version_with_folder_but_no_tree_counts_as_missing(poe1: Path) -> None:
    assert missing_tree_data_files(poe1, "3_19") == ["TreeData/3_19/tree.lua"]
    (poe1 / "TreeData" / "3_19" / "tree.lua").write_bytes(TREE_319)
    assert missing_tree_data_files(poe1, "3_19") == []
    assert missing_tree_data_files(poe1, "9_99") == []  # unknown to the manifest
    with pytest.raises(UpdateError, match="Invalid tree_version"):
        missing_tree_data_files(poe1, "../x")


def test_large_text_file_with_other_line_endings_is_not_refetched(
    poe1: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Over 2 MiB and CRLF on disk while the manifest hashes LF: still intact
    big = b"-- line\n" * 400_000
    files = {"TreeData/3_19/Assets.lua": ASSETS_319, "TreeData/3_19/tree.lua": big}
    (poe1 / "manifest.xml").write_text(_manifest(files), encoding="utf-8")
    (poe1 / "TreeData" / "3_19" / "tree.lua").write_bytes(big.replace(b"\n", b"\r\n"))
    monkeypatch.setenv("POB_OFFLINE", "1")
    assert missing_tree_data_files(poe1, "3_19") == []
    assert len(ensure_tree_data_files(poe1, "3_19")) == 2


def test_partial_download_writes_nothing(poe1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, fail={"TreeData/3_28/tree.lua"})
    with pytest.raises(httpx.HTTPStatusError):
        ensure_tree_data_files(poe1, "3_28", timeout=5.0)
    assert not (poe1 / "TreeData" / "3_28").exists()
    assert not any(p.name.startswith(".pob-stage-") for p in poe1.iterdir())
    assert missing_tree_data_files(poe1, "3_28") == ["TreeData/3_28/sprites.lua", "TreeData/3_28/tree.lua"]


def test_stage_fetches_tree_for_existing_folder(poe1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch)
    assert stage_missing_tree_versions(_code("3_19", "3_30"), poe1) == ["3_19"]
    assert (poe1 / "TreeData" / "3_19" / "tree.lua").read_bytes() == TREE_319
    assert [u.rsplit("/", 1)[-1] for u in seen] == ["tree.lua"]
    seen.clear()
    assert stage_missing_tree_versions(_code("3_19"), poe1) == []
    assert seen == []


def test_stage_offline_raises_clean_hint(poe1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_OFFLINE", "1")
    with pytest.raises(BridgeError, match=r"'3_19'.*TreeData/3_19/tree\.lua.*POB_OFFLINE=1.*Run pob online"):
        stage_missing_tree_versions(_code("3_19"), poe1)


def test_stage_failure_is_cached_and_reported(poe1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, fail={"TreeData/3_19/tree.lua"})
    with pytest.raises(BridgeError, match="could not be fetched"):
        stage_missing_tree_versions(_code("3_19"), poe1)
    seen.clear()
    with pytest.raises(BridgeError, match="less than a minute ago"):
        stage_missing_tree_versions(_code("3_19"), poe1)
    assert seen == []


def test_stage_total_time_is_bounded(poe1: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {**FILES, "TreeData/3_20/tree.lua": b"return {}\n"}
    (poe1 / "manifest.xml").write_text(_manifest(files), encoding="utf-8")
    clock = [1000.0]
    budgets: list[float] = []

    def fake_ensure(_dir: Path, tv: str, *, timeout: float) -> list[Path]:
        budgets.append(timeout)
        clock[0] += timeout  # each version uses its whole budget
        (poe1 / "TreeData" / tv).mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            if name.startswith(f"TreeData/{tv}/"):
                (poe1 / name).write_bytes(data)
        return [poe1 / "x"]

    monkeypatch.delenv("POB_OFFLINE", raising=False)
    monkeypatch.setattr(bp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("pob_cli.updater.remote.ensure_tree_data_files", fake_ensure)
    with pytest.raises(BridgeError, match="time budget"):
        stage_missing_tree_versions(_code("3_19", "3_28", "3_19", "3_20"), poe1)
    assert budgets[0] == bp._TREE_STAGE_PER_VERSION_SEC
    assert len(budgets) == 2  # 3_20 is refused once the shared budget is spent
    assert sum(budgets) <= bp._TREE_STAGE_TOTAL_BUDGET_SEC
    # Well inside the 15 s import_code RPC timeout's caller budget (150 s CLI wait)
    assert bp._TREE_STAGE_TOTAL_BUDGET_SEC < 15.0
