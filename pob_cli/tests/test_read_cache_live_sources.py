"""A build loaded from a URL or profile is fetched again once the read cache stops trusting its address."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import pob_cli.analyzer as analyzer_mod
import pob_cli.daemon as daemon_mod
import pob_cli.snapshot as snapshot_mod
from pob_cli.daemon.read_cache import (
    LIVE_SOURCE_RELOAD_AFTER_SEC,
    SlotReadCache,
    compute_source_hash,
    is_live_source,
)
from pob_cli.snapshot import history_of

PROFILE_URL = "https://www.pathofexile.com/account/view-profile/Someone-1234/characters"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _StubWorker:
    def __init__(self, slot_name: str = "main", pob_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.slot_name = slot_name
        self.pob_root = pob_dir
        self.runtime_dir = runtime_dir
        self.generation = 1
        self.last_active_ts = time.time()
        self.last_good_code: str | None = None
        self.lock = threading.RLock()

    def is_alive(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def ensure_alive(self) -> None:
        pass

    def call(self, _method: str, _params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        return {"ok": True}


@pytest.mark.parametrize(
    ("params", "live"),
    [
        ({"source": PROFILE_URL, "char_name": "Zed"}, True),
        ({"source": "https://pobb.in/abc123"}, True),
        ({"source": "SomeAccount", "realm": "pc", "char_name": "Zed"}, True),
        ({"realm": "cn", "char_name": "Zed"}, True),
        # The account form of `pob profile`: no URL, no realm, still an address.
        ({"source": "Someone#1234", "char_name": "Zed"}, True),
        ({"source": "Someone#1234"}, False),
        ({"source": "eNrtVVtv2jAU"}, False),
        ({"source": "<PathOfBuilding></PathOfBuilding>"}, False),
        ({"xml_code": "eNrtVVtv2jAU"}, False),
        # The CLI turns a build file into `xml_code`; a URL sent along with it is not what was loaded.
        ({"xml_code": "eNrtVVtv2jAU", "source": PROFILE_URL}, False),
        ({}, False),
    ],
)
def test_only_addresses_count_as_live_sources(params: dict[str, Any], live: bool) -> None:
    assert is_live_source(params) is live


def test_an_account_is_hashed_with_the_character_it_names() -> None:
    zed = compute_source_hash({"source": "Someone#1234", "char_name": "Zed"})
    amy = compute_source_hash({"source": "Someone#1234", "char_name": "Amy"})
    assert zed != amy
    assert zed == compute_source_hash({"source": " Someone#1234 ", "char_name": "Zed"})


def test_a_live_source_stops_matching_after_the_reload_window() -> None:
    clock = _Clock()
    cache = SlotReadCache(clock=clock)
    url_hash = compute_source_hash({"source": PROFILE_URL, "char_name": "Zed"})
    code_hash = compute_source_hash({"xml_code": "eNrtVVtv2jAU"})

    cache.record_slot_load("main", 3, url_hash, {"importNotes": ["n"]}, live=True)
    cache.record_slot_load("ref", 7, code_hash)

    clock.now += LIVE_SOURCE_RELOAD_AFTER_SEC
    assert cache.matching_slot_load("main", 3, True, url_hash) == {"importNotes": ["n"]}

    clock.now += 1.0
    assert cache.matching_slot_load("main", 3, True, url_hash) is None
    # A build identified by its content cannot change behind its hash, so it never expires.
    clock.now += 10 * LIVE_SOURCE_RELOAD_AFTER_SEC
    assert cache.matching_slot_load("ref", 7, True, code_hash) == {}


def test_diagnose_of_a_profile_url_refetches_after_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = tmp_path / "eng"
    (eng / "PoE1").mkdir(parents=True)
    monkeypatch.setattr(daemon_mod, "BridgeWorker", _StubWorker)
    mgr = daemon_mod.DaemonManager(
        pob_dir=eng, token="tok", runtime_dir=tmp_path / "rt", offline=True, skip_update_check=True
    )
    clock = _Clock()
    mgr._read_cache = SlotReadCache(clock=clock)

    gear = ["old ring"]
    loads: list[str] = []

    def _fake_load(w: Any, *, source: str | None = None, **_: Any) -> dict[str, Any]:
        loads.append(str(source))
        history_of(w).load_build(f"CODE:{gear[0]}", "profile")
        return {"character": {"name": "Zed"}}

    def _fake_diagnose(w: Any, **_: Any) -> dict[str, Any]:
        return {"gear": str(w.last_good_code)}

    monkeypatch.setattr(snapshot_mod, "load_build", _fake_load)
    monkeypatch.setattr(analyzer_mod, "diagnose_build", _fake_diagnose)
    params = {"target": "main", "source": PROFILE_URL, "char_name": "Zed", "no_rank": True}

    first = mgr.dispatch("diagnose", dict(params))
    assert first["gear"] == "CODE:old ring" and "cached" not in first

    # The player swaps a ring. An immediate repeat is an agent loop: no refetch, the cached answer.
    gear[0] = "new ring"
    clock.now += 5.0
    repeat = mgr.dispatch("diagnose", dict(params))
    assert loads == [PROFILE_URL] and repeat["cached"] is True and repeat["gear"] == "CODE:old ring"

    # Later the same command must show the character as it is now.
    clock.now += LIVE_SOURCE_RELOAD_AFTER_SEC + 1.0
    later = mgr.dispatch("diagnose", dict(params))
    assert loads == [PROFILE_URL, PROFILE_URL]
    assert later["gear"] == "CODE:new ring" and "cached" not in later
    assert later["rev"] == first["rev"] + 1

    # `pob load` of the same URL always fetches, and restarts the window.
    mgr.dispatch("load", {"target": "main", "source": PROFILE_URL, "char_name": "Zed"})
    assert len(loads) == 3
    clock.now += 5.0
    after_load = mgr.dispatch("diagnose", dict(params))
    assert len(loads) == 3 and "cached" not in after_load
