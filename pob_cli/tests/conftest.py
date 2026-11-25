from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest
from engine_paths import find_engine_data, find_runnable_engine

from pob_cli import cli_trade
from pob_cli.guardrails import reset_cli_warnings

# A plain `pytest` run must not write into the developer's real runtime dir: CLI tests write
# runtime state and build caches there. check.py already isolates both; do the same here when
# the caller has not, with one cache for the whole session.
if not os.environ.get("POB_RUNTIME_DIR", "").strip():
    _runtime = tempfile.mkdtemp(prefix="pob_tests_rt_")
    atexit.register(shutil.rmtree, _runtime, ignore_errors=True)
    os.environ["POB_RUNTIME_DIR"] = _runtime
    os.environ.setdefault("POB_CACHE_DIR", os.path.join(_runtime, "cache"))

# Real-engine tests also drive the CLI, which resolves its engine on its own. Point that lookup
# at the engine found for this checkout; tests that check the lookup order clear POB_ROOT themselves.
if not os.environ.get("POB_ROOT", "").strip():
    _engine = find_runnable_engine() or find_engine_data()
    if _engine is not None:
        os.environ["POB_ROOT"] = str(_engine)


@pytest.fixture(autouse=True)
def _no_live_currency_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`trade search` looks up currency rates online; tests must stay offline and TLS warnings clean."""
    reset_cli_warnings()
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0})
