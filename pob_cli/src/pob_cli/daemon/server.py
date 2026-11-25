"""Loopback HTTP server, authenticated RPC handler, and server entrypoints."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeError, BridgeRejectedError
from ..bridge_protocol import ENGINE_REJECTED_HINT
from ..errors import CrossGameHallucinationError, DaemonRequestError, LegacyPatchTrapError
from ..guardrails import redact_secrets
from .runtime import (
    GAME_ID,
    BootstrapMissingError,
    _daemon_boot_error_file,
    _daemon_state_file,
    _get_runtime_dir,
)
from .slots import DaemonManager

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


def make_handler(manager: DaemonManager) -> type[BaseHTTPRequestHandler]:
    import traceback
    from http.server import BaseHTTPRequestHandler

    class RpcHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            token = self.headers.get("X-PoB-Token", "")
            foreign_token = self.headers.get("X-PoB2-Token", "")
            if foreign_token and not token:
                exc = CrossGameHallucinationError(
                    game=GAME_ID,
                    detected_game="poe2",
                    trigger="Header X-PoB2-Token",
                    error="Received PoE2 auth header `X-PoB2-Token` on a PoE1 (`pob`) daemon.",
                    remediation=(
                        "Use `pob2` with its own `POB2_RUNTIME_DIR` for PoE2 requests, "
                        "or send `X-PoB-Token` for PoE1."
                    ),
                    equivalent_command="pob2 status",
                    rule_id="DAEMON_CROSS_GAME_TOKEN_HEADER",
                )
                self._send_json(400, {**exc.to_dict(), "type": "CrossGameHallucinationError"})
                return
            if not secrets.compare_digest(token, manager.token):
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid or missing X-PoB-Token"})
                return

            if self.path != "/rpc":
                self._send_json(404, {"ok": False, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "Invalid Content-Length header",
                        "type": "ValueError",
                        "errorKind": "request",
                    },
                )
                return
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                req = json.loads(raw_body) if raw_body else {}
                if not isinstance(req, dict):
                    raise ValueError("RPC request payload must be a JSON object.")
                action = req.get("action")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("RPC 'params' must be a JSON object.")
                result = manager.dispatch(action, params)
                self._send_json(200, {"ok": True, "result": result})
            except CrossGameHallucinationError as exc:
                self._send_json(400, {**exc.to_dict(), "type": "CrossGameHallucinationError"})
            except LegacyPatchTrapError as exc:
                self._send_json(400, {**exc.to_dict(), "type": "LegacyPatchTrapError"})
            except (DaemonRequestError, ValueError) as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": redact_secrets(str(exc)),
                        "type": type(exc).__name__,
                        "errorKind": "request",
                    },
                )
            except BridgeError as exc:
                # A healthy engine that refused bad input is a request error (exit 1); everything
                # else, timeouts and crashes included, is a fault (exit 5). See ENGINE_ERROR_KINDS.
                kind = exc.kind if isinstance(exc, BridgeRejectedError) else "daemon"
                message = redact_secrets(str(exc))
                payload: dict[str, Any] = {
                    "ok": False,
                    "error": f"{message}\n{ENGINE_REJECTED_HINT}" if kind == "request" else message,
                    "type": type(exc).__name__,
                    "errorKind": kind,
                }
                if isinstance(exc, BridgeRejectedError) and exc.code:
                    payload["engineCode"] = exc.code
                self._send_json(400, payload)
            except Exception as exc:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": redact_secrets(str(exc)),
                        "type": exc.__class__.__name__,
                        "errorKind": "daemon",
                        "trace": redact_secrets(traceback.format_exc(limit=4)),
                    },
                )

    return RpcHandler


def _serve_daemon(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
    disable_global_idle: bool = False,
    announce_foreground: bool = False,
) -> None:
    """Start the local daemon HTTP server, write `<runtime_dir>/daemon.json`, and serve until stopped."""
    import pob_cli.daemon as _daemon_pkg

    from .client import _guard_foreign_daemon_state

    rt_dir = _get_runtime_dir(runtime_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    boot_err_file = _daemon_boot_error_file(rt_dir)
    state_file = _daemon_state_file(rt_dir)
    existing_state = _daemon_pkg.read_daemon_state(rt_dir)
    if existing_state is not None:
        _guard_foreign_daemon_state(existing_state, state_file, rt_dir)

    try:
        if boot_err_file.exists():
            boot_err_file.unlink()
    except OSError:
        pass

    token = secrets.token_hex(32)
    manager = DaemonManager(
        pob_dir=pob_dir,
        token=token,
        runtime_dir=rt_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=disable_global_idle,
    )
    try:
        manager.start()
    except Exception as exc:
        upd_status: dict[str, Any] | None = None
        if isinstance(exc, BootstrapMissingError) and isinstance(exc.update_status, dict):
            upd_status = dict(exc.update_status)
            if isinstance(upd_status.get("networkError"), str):
                upd_status["networkError"] = redact_secrets(upd_status["networkError"])
        err_payload = {
            "error": redact_secrets(str(exc)),
            "type": exc.__class__.__name__,
            "state": (
                exc.update_status.get("state")
                if isinstance(exc, BootstrapMissingError)
                else "boot_error"
            ),
            "updateStatus": upd_status,
            "ts": time.time(),
        }
        try:
            tmp_err = boot_err_file.with_suffix(f".{os.getpid()}.tmp")
            tmp_err.write_text(json.dumps(err_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_err, boot_err_file)
        except OSError:
            pass
        raise

    import socketserver
    from http.server import ThreadingHTTPServer

    class _LoopbackHTTPServer(ThreadingHTTPServer):
        def server_bind(self) -> None:
            # HTTPServer.server_bind calls socket.getfqdn(host), a reverse DNS lookup that can stall
            # daemon cold start on hosts with slow resolvers; a loopback daemon never uses server_name.
            socketserver.TCPServer.server_bind(self)
            host, bound_port = self.server_address[:2]
            self.server_name = str(host)
            self.server_port = bound_port

    server = _LoopbackHTTPServer(("127.0.0.1", port), make_handler(manager))
    manager.server = server
    actual_port = server.server_address[1]

    state_payload = {
        "game": GAME_ID,
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "started_at": manager.started_at,
        "pob_dir": str(manager.pob_dir),
    }
    tmp_state_file = state_file.with_suffix(f".{os.getpid()}.tmp")
    tmp_state_file.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    os.replace(tmp_state_file, state_file)

    if announce_foreground:
        sys.stderr.write(
            f"pob daemon listening on 127.0.0.1:{actual_port} (pid={os.getpid()}, runtime={rt_dir})\n"
        )
        sys.stderr.flush()

    def _watchdog() -> None:
        while not manager._stopping:
            time.sleep(15.0)
            if manager.check_idle():
                manager.shutdown_all()
                break

    threading.Thread(target=_watchdog, name="pob-daemon-watchdog", daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown_all()


def run_daemon_server(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Start the background daemon server (auto-exits after 30m global idle)."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=False,
        announce_foreground=False,
    )


def run_daemon_forever(
    pob_dir: Path | None = None,
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    offline: bool | None = None,
    proxy: str | None = None,
    mirror: str | None = None,
    skip_update_check: bool = False,
) -> None:
    """Run the PoB daemon persistently in the foreground (used by `pob serve`)."""
    _serve_daemon(
        pob_dir=pob_dir,
        port=port,
        runtime_dir=runtime_dir,
        offline=offline,
        proxy=proxy,
        mirror=mirror,
        skip_update_check=skip_update_check,
        disable_global_idle=True,
        announce_foreground=True,
    )
