"""Signature-compatible call helper and UTF-8 stream setup (L0 pure module).

Single canonical implementations shared by cli_common (L4), mutators.common (L2),
daemon.runtime (L3), formatters (L2), guardrails (L0), and cli_hook (L4).
"""

from __future__ import annotations

import inspect
from typing import Any


def call_compat(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` forwarding only keyword arguments accepted by its signature.

    Functions with ``**kwargs`` (or uninspectable signatures) receive all kwargs
    unchanged; this keeps callers forward-compatible when callee signatures grow
    optional parameters across versions.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)


def ensure_utf8(stream: Any) -> None:
    """Best-effort UTF-8 reconfiguration of a text stream (Windows consoles, pipes).

    Never raises: streams without ``reconfigure`` (or already-closed streams) are
    left as-is, matching the previous inline try/except blocks at every call site.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
