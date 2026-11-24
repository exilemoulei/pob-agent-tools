from __future__ import annotations

import os

import pytest
from engine_paths import find_engine_data, find_runnable_engine

from pob_cli import cli_trade

# Real-engine tests also drive the CLI, which resolves its engine on its own. Point that lookup
# at the engine found for this checkout; tests that check the lookup order clear POB_ROOT themselves.
if not os.environ.get("POB_ROOT", "").strip():
    _engine = find_runnable_engine() or find_engine_data()
    if _engine is not None:
        os.environ["POB_ROOT"] = str(_engine)


@pytest.fixture(autouse=True)
def _no_live_currency_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`trade search` looks up currency rates online; tests must stay offline."""
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0})
