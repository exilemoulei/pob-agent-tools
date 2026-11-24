"""Fail-safe JSON object file reading (L0 pure module).

Canonical helper shared by bridge_protocol (slot state) and daemon.client
(daemon state / boot error). Never raises: missing files, unreadable content,
and non-object JSON all yield None.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``; None when missing, unreadable, or not a dict."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
