"""Offline tests for import, trade and updater hardening (share codes, SSRF, league fallback, assets)."""

from __future__ import annotations

import base64
import io
import json
import sys
import tarfile
import warnings
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade, importers, importers_codec
from pob_cli.cli import main
from pob_cli.errors import DaemonRequestError
from pob_cli.profile_import import fetch_intl_character_bundle, share_url_guard
from pob_cli.profile_import.pob_xml import character_bundle_to_pob_xml, resolve_import_tree_version
from pob_cli.trade_client import CookieRequired, RateLimited, TradeClient
from pob_cli.updater import UpdateError, remote, self_update

POE1_XML = '<?xml version="1.0"?><PathOfBuilding><Build level="90" className="Witch"/></PathOfBuilding>'

# --- share codes -------------------------------------------------------------------------------


def test_share_code_decoder_drops_non_alphabet_chars_like_pob() -> None:
    code = importers.xml_to_share_code(POE1_XML)
    noisy = code[:10] + "!*" + code[10:20] + "\t\n" + code[20:]
    assert importers.share_code_to_xml(noisy) == POE1_XML


@pytest.mark.parametrize("noise", ["\uff01", "!", " \u3002\n", "=\uff1d"])
def test_share_code_decoder_pads_after_dropping_noise(noise: str) -> None:
    # Every unpadded length mod 4, so the padding has to be counted on the cleaned code.
    for filler in ("", "a", "ab", "abc"):
        xml = POE1_XML.replace("Witch", "Witch" + filler)
        code = importers.xml_to_share_code(xml).rstrip("=")
        assert importers.share_code_to_xml(code + noise) == xml
        assert importers.share_code_to_xml(code[:7] + noise + code[7:]) == xml


def test_share_code_bomb_is_refused_as_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers_codec, "MAX_SHARE_XML_BYTES", 1024)
    bomb = base64.urlsafe_b64encode(zlib.compress(b"<" + b"A" * 50_000)).decode()
    with pytest.raises(ValueError, match="exceeds"):
        importers.share_code_to_xml(bomb)
    raw_bomb = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = raw_bomb.compress(b"B" * 50_000) + raw_bomb.flush()
    with pytest.raises(ValueError, match="exceeds"):
        importers.share_code_to_xml(base64.urlsafe_b64encode(raw).decode())
    # A truncated stream still fails as invalid, not silently partial.
    good = zlib.compress(POE1_XML.encode())
    with pytest.raises(ValueError, match="Invalid PoB share code"):
        importers.share_code_to_xml(base64.urlsafe_b64encode(good[:-6]).decode())


def test_cli_side_decoder_is_capped_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.bridge_protocol import _decode_share_code_xml

    code = importers.xml_to_share_code(POE1_XML)
    assert _decode_share_code_xml(code) == POE1_XML
    assert _decode_share_code_xml(POE1_XML) == POE1_XML
    assert _decode_share_code_xml("not a share code") is None
    monkeypatch.setattr(importers_codec, "MAX_SHARE_XML_BYTES", 1024)
    bomb = base64.urlsafe_b64encode(zlib.compress(b"<" + b"A" * 50_000)).decode()
    assert _decode_share_code_xml(bomb) is None


class _Lock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: Any) -> None:
        return None


class _Worker:
    def __init__(self, url_code: str = "") -> None:
        self.lock = _Lock()
        self.caps: dict[str, Any] = {}
        self.calls: list[str] = []
        self.url_code = url_code

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(method)
        if method == "import_from_url":
            return {"code": self.url_code}
        return {}


def test_undecodable_code_never_reaches_engine() -> None:
    worker = _Worker()
    with pytest.raises(ValueError, match="Invalid PoB share code"):
        importers.load_build_into_worker(worker, xml_code="@@@@")
    assert "import_code" not in worker.calls

    worker2 = _Worker(url_code="not-a-real-code")
    with pytest.raises(ValueError, match="Invalid PoB share code"):
        importers.load_build_into_worker(worker2, source="https://pobb.in/abcdef")
    assert "import_code" not in worker2.calls


# --- poe.ninja character 404 -------------------------------------------------------------------


def _ninja_client(status: int) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="")

    return lambda **_k: httpx.Client(transport=httpx.MockTransport(handler))


def test_poe_ninja_character_all_404_is_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importers, "_fetch_poe_ninja_build_index_state", lambda **_k: {"leagueBuilds": [{"leagueUrl": "keepers"}]})
    monkeypatch.setattr(importers, "_make_httpx_client", _ninja_client(404))
    url = "https://poe.ninja/poe1/builds/keepers/character/Nobody-1234/Ghost"
    with pytest.raises(DaemonRequestError, match="account name"):
        importers.fetch_poe_ninja_character_code(url)
    monkeypatch.setattr(importers, "_make_httpx_client", _ninja_client(503))
    with pytest.raises(RuntimeError) as exc_info:
        importers.fetch_poe_ninja_character_code(url)
    assert not isinstance(exc_info.value, DaemonRequestError)


# --- generic share URL SSRF guard --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/build",
        "https://127.0.0.1:8765/rpc",
        "https://localhost/x",
        "https://[::1]/x",
        "https://169.254.169.254/latest/meta-data",
        "https://10.1.2.3/x",
        "https://api.github.com/repos/x/y",
        "file:///etc/passwd",
    ],
)
def test_share_url_guard_refuses_non_public_targets(url: str) -> None:
    with pytest.raises(DaemonRequestError, match="Refusing"):
        share_url_guard.check_public_https_url(url)


def test_share_url_guard_resolves_hostnames_and_checks_each_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = {"evil.example": ["192.168.1.5"], "good.example": ["93.184.216.34"]}
    monkeypatch.setattr(share_url_guard, "_resolve_host", lambda host: resolved.get(host, []))
    with pytest.raises(DaemonRequestError, match="private"):
        share_url_guard.check_public_https_url("https://evil.example/x")

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "good.example":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, text="SECRET")

    monkeypatch.setattr(importers, "_make_httpx_client", lambda **_k: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(DaemonRequestError, match="private"):
        importers.fetch_share_url_code_fallback("https://good.example/build")
    assert seen == ["https://good.example/build"]


def test_share_url_guard_allows_proxy_fake_ip_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clash/sing-box fake-ip mode resolves every host into 198.18.0.0/15; that must not block share links."""
    monkeypatch.setattr(share_url_guard, "_resolve_host", lambda host: ["198.18.0.42"])
    share_url_guard.check_public_https_url("https://pobb.in/abc")


def test_share_url_guard_follows_public_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(share_url_guard, "_resolve_host", lambda host: ["93.184.216.34"])
    code = importers.xml_to_share_code(POE1_XML)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, text=code)

    monkeypatch.setattr(importers, "_make_httpx_client", lambda **_k: httpx.Client(transport=httpx.MockTransport(handler)))
    assert importers.fetch_share_url_code_fallback("https://share.example/a") == code


# --- trade league fallback, usage exit codes, rate-limit reservation ---------------------------


def _trade_client(tmp_path: Path, handler: Any) -> TradeClient:
    return TradeClient(cache_dir=tmp_path, sleep=lambda _s: None, transport=httpx.MockTransport(handler))


def test_default_league_propagates_rate_limit_and_cookie(tmp_path: Path) -> None:
    with pytest.raises(RateLimited):
        _trade_client(tmp_path, lambda _r: httpx.Response(429, headers={"Retry-After": "5"})).default_league()
    with pytest.raises(CookieRequired):
        _trade_client(tmp_path, lambda _r: httpx.Response(403, text="no")).default_league()
    client = _trade_client(tmp_path, lambda _r: httpx.Response(500, text="boom"))
    assert client.default_league() == "Standard"
    assert client.league_warning and "--league" in client.league_warning


def test_resolve_league_rate_limited_exits_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", lambda **_k: "")

    class _Limited(TradeClient):
        def default_league(self) -> str:
            raise RateLimited(7)

    monkeypatch.setattr(cli_trade, "TradeClient", _Limited)
    res = CliRunner().invoke(main, ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--pob", "off", "--no-compare"])
    assert res.exit_code == 4, res.output


def test_resolve_league_fallback_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_trade, "resolve_current_trade_league", lambda **_k: "")
    shown: list[str] = []
    monkeypatch.setattr("pob_cli.cli_trade.common._err_utf8", shown.append)

    def _broken(**_k: Any) -> TradeClient:
        return _trade_client(tmp_path, lambda _r: httpx.Response(500, text="boom"))

    monkeypatch.setattr(cli_trade, "TradeClient", _broken)
    assert cli_trade._resolve_league(None) == "Standard"
    assert shown and "League lookup failed" in shown[0] and "--league" in shown[0]


@pytest.mark.parametrize(
    "args",
    [["trade", "search", "--slott", "x"], ["trade", "serch"], ["trade", "stats"], ["trade", "--bogus"]],
)
def test_trade_usage_errors_exit_1(args: list[str]) -> None:
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 1, res.output


def test_transport_error_releases_rate_limit_reservation(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={"result": []},
                headers={
                    "x-rate-limit-policy": "trade-data-request-limit",
                    "x-rate-limit-rules": "Ip",
                    "x-rate-limit-ip": "30:10:60",
                    "x-rate-limit-ip-state": "1:10:0",
                },
            )
        raise httpx.ConnectTimeout("timed out", request=req)

    client = _trade_client(tmp_path, handler)
    client.leagues()
    with pytest.raises(Exception, match="timed out"):
        client.leagues()
    state = json.loads((tmp_path / "trade_ratelimit.json").read_text(encoding="utf-8"))
    assert all(not entry.get("in_flight") for entry in state["policies"].values())


# --- updater -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../evil.zip", "a/b.zip", "..\\evil.zip", "", "C:evil.zip", ".hidden.zip", "asset.zip\n", "NUL.zip", "com1.tar.gz", "aux"],
)
def test_release_asset_name_must_be_one_safe_component(name: str, tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="unsafe name"):
        remote._download_and_stage_release_zip("v1", {"name": name, "sha256": "0" * 64}, tmp_path)


def test_release_asset_name_accepts_pobtools_names() -> None:
    assert remote._safe_asset_name("PobTools-DataV2-24.zip") == "PobTools-DataV2-24.zip"
    assert remote._safe_asset_name("PobTools[20261102].zip") == "PobTools[20261102].zip"
    assert remote._safe_asset_name("console-1.0.zip") == "console-1.0.zip"  # only a whole device name is refused


def test_network_error_detail_redacts_proxy_credentials() -> None:
    with pytest.raises(UpdateError) as exc_info:
        with self_update._network_errors("Downloading"):
            raise httpx.ProxyError("Cannot connect to proxy http://alice:hunter2@proxy.local:8080")
    assert "hunter2" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="the extraction filter warning is 3.12+")
def test_tar_fallback_path_emits_no_deprecation_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(self_update, "_supports_tar_data_filter", lambda: False)
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("pob")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"ok"))
    dest = tmp_path / "out"
    dest.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        self_update._extract(archive, dest)
    assert (dest / "pob").read_bytes() == b"ok"


# --- official profile import -------------------------------------------------------------------


def test_bundle_retries_with_env_cookie_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POE_COOKIE", raising=False)
    monkeypatch.setenv("POESESSID", "abcdef0123456789abcdef")
    seen: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        has_cookie = "POESESSID=abcdef0123456789abcdef" in req.headers.get("cookie", "")
        seen.append(has_cookie)
        if not has_cookie:
            return httpx.Response(403, text="private")
        return httpx.Response(200, json={"hashes": [1], "items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = fetch_intl_character_bundle("alice#1234", {"name": "Hero"}, client=client, use_cookie=False)
    assert bundle["passiveSkills"]["hashes"] == [1]
    assert seen == [False, False, True, True]


def test_bundle_403_without_env_cookie_stays_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POE_COOKIE", raising=False)
    monkeypatch.delenv("POESESSID", raising=False)
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403))) as client:
        with pytest.raises(DaemonRequestError):
            fetch_intl_character_bundle("alice#1234", {"name": "Hero"}, client=client)


def test_import_tree_version_follows_engine_and_ruthless(tmp_path: Path) -> None:
    assert resolve_import_tree_version(None, "Keepers") == "3_30"
    for ver in ("3_29", "3_30", "3_30_ruthless", "3_30_alternate"):
        d = tmp_path / "PoE1" / "TreeData" / ver
        d.mkdir(parents=True)
        (d / "tree.lua").write_text("return {}", encoding="utf-8")
    assert resolve_import_tree_version(tmp_path, "Keepers") == "3_30"
    assert resolve_import_tree_version(tmp_path, "Ruthless Keepers") == "3_30_ruthless"
    xml_text = character_bundle_to_pob_xml(
        {"char": {"name": "R", "class": "Witch", "level": 10, "league": "Ruthless Keepers"}, "passiveSkills": {}, "items": {}},
        pob_root=tmp_path,
    )
    assert 'treeVersion="3_30_ruthless"' in xml_text
