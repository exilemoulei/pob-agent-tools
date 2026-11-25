"""`update_state.json` is per runtime dir: it must not describe another engine, and two writers must not collide."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from pob_cli import updater
from pob_cli.updater import state as state_mod
from pob_cli.updater.state import detect_local_versions, load_update_state, save_update_state

MANAGED_STATE = {
    "localPobVersion": "2.69.0",
    "pobToolsVersion": "1.4.0",
    "pobToolsTag": "v1.4.0",
    "dataVersion": "data-26",
}


def _bare_engine(root: Path) -> Path:
    (root / "PoE1").mkdir(parents=True)
    return root


def test_versions_are_not_borrowed_from_another_engines_state(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    managed = _bare_engine(rt / "engine")
    other = _bare_engine(tmp_path / "pob_root_engine")
    save_update_state({**MANAGED_STATE, "engineDir": str(managed)}, rt)

    assert detect_local_versions(other, rt) == dict.fromkeys(MANAGED_STATE)
    # The engine the state was recorded for still falls back to it, as does a state without an engine dir.
    assert detect_local_versions(managed, rt) == MANAGED_STATE
    save_update_state(dict(MANAGED_STATE), rt)
    assert detect_local_versions(other, rt) == MANAGED_STATE


def test_an_engines_own_version_files_win_over_the_state(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    other = _bare_engine(tmp_path / "pob_root_engine")
    (other / updater.POBTOOLS_META_FILENAME).write_text(
        json.dumps({"pobToolsVersion": "1.3.0", "pobToolsTag": "v1.3.0", "dataVersion": "data-20"}), encoding="utf-8"
    )
    save_update_state({**MANAGED_STATE, "engineDir": str(rt / "engine")}, rt)

    found = detect_local_versions(other, rt)
    assert (found["pobToolsTag"], found["dataVersion"], found["localPobVersion"]) == ("v1.3.0", "data-20", None)


def test_concurrent_saves_never_collide_on_the_temp_file(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def _writer(tag: int) -> None:
        try:
            start.wait(timeout=10)
            for i in range(150):
                save_update_state({"writer": tag, "seq": i, "pad": "x" * 4000}, rt)
        except BaseException as exc:  # a shared temp name raised FileNotFoundError here
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    final = load_update_state(rt)
    assert final["seq"] == 149 and len(final["pad"]) == 4000
    assert sorted(p.name for p in rt.iterdir()) == [updater.UPDATE_STATE_FILENAME]


def test_a_save_waits_out_a_replace_windows_refuses_for_a_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"
    real_replace = os.replace
    refusals = {"left": 3}

    def _busy_then_free(src: Any, dst: Any) -> None:
        # What Windows answers while another writer is replacing the same file.
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", _busy_then_free)
    monkeypatch.setattr(state_mod.time, "sleep", lambda _s: None)
    save_update_state({"seq": 1}, rt)
    assert load_update_state(rt)["seq"] == 1 and refusals["left"] == 0

    # A folder that never lets go still fails, and leaves no temp file behind.
    refusals["left"] = 10_000
    with pytest.raises(PermissionError):
        save_update_state({"seq": 2}, rt)
    monkeypatch.undo()
    assert load_update_state(rt)["seq"] == 1
    assert sorted(p.name for p in rt.iterdir()) == [updater.UPDATE_STATE_FILENAME]
