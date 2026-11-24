"""Locate real PoB engines for integration tests without machine-specific paths.

Real-engine tests skip unless an engine is found. Lookup order:

1. `POB_ROOT`, when it points at an engine directory.
2. `<checkout>/.runtime/engine`, the managed engine of a source checkout (`pob bootstrap`).
3. `<checkout>/PobTools[20261102]`, the legacy PobTools seed next to the checkout.

The frozen legacy snapshot (PoB 2.67.2 with a patched UpdateCheck.lua) is only used through
`POB_LEGACY_DIR` or the checkout-relative seed, because its assertions are version specific.
"""

from __future__ import annotations

import os
from pathlib import Path

from pob_cli.updater import is_valid_engine_dir

CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_SEED_NAME = "PobTools[20261102]"


def _has_poe1_data(path: Path) -> bool:
    return (path / "PoE1" / "Data" / "Gems.lua").is_file()


def _candidates() -> list[Path]:
    out: list[Path] = []
    env_root = os.environ.get("POB_ROOT", "").strip()
    if env_root:
        out.append(Path(env_root))
    out.append(CHECKOUT_ROOT / ".runtime" / "engine")
    out.append(CHECKOUT_ROOT / LEGACY_SEED_NAME)
    return out


def find_engine_data() -> Path | None:
    """First engine with PoE1 game data (enough for SQLite index and data queries)."""
    return next((p for p in _candidates() if _has_poe1_data(p)), None)


def find_runnable_engine() -> Path | None:
    """First engine that can launch the headless bridge on this platform."""
    return next((p for p in _candidates() if _has_poe1_data(p) and is_valid_engine_dir(p)), None)


def find_legacy_snapshot() -> Path | None:
    """The frozen PobTools[20261102] seed, if this machine has it."""
    env_legacy = os.environ.get("POB_LEGACY_DIR", "").strip()
    cand = Path(env_legacy) if env_legacy else CHECKOUT_ROOT / LEGACY_SEED_NAME
    return cand if (cand / "PoE1" / "manifest.xml").is_file() else None


def has_zh_data(engine: Path | None) -> bool:
    return engine is not None and (engine / "Data" / "poe1" / "zh-rCN").is_dir()
