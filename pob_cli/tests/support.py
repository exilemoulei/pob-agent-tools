"""Environment-independent helpers for timing and network-failure tests."""

from __future__ import annotations

import os
import socket


def perf_budget(limit: float) -> float:
    """Scale a timing limit by POB_TEST_PERF_SLACK; slow shared CI runners set it above 1."""
    try:
        slack = float(os.environ.get("POB_TEST_PERF_SLACK", "1") or "1")
    except ValueError:
        slack = 1.0
    return limit * max(slack, 1.0)


def dead_proxy_url() -> str:
    """Proxy URL on a just-closed loopback port, so connects are refused instantly.

    A fixed port such as 9 (discard) can be served on some hosts, which turns a fast refusal
    into a read timeout.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"
