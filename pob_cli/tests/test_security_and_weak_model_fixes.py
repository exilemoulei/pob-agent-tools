"""Unit tests for secret redaction, NO_SECRET_ENV_DUMP, reply_patterns, batch_ops aliases, and tar fallback."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import click
import pytest
from click.testing import CliRunner

import pob_cli.cli as cli_mod
from pob_cli import cli_common
from pob_cli.cli import main
from pob_cli.cli_hook.gates import handle_pre_tool
from pob_cli.daemon import DaemonRequestError
from pob_cli.daemon.runtime import _daemon_boot_error_file
from pob_cli.daemon.server import _serve_daemon, make_handler
from pob_cli.daemon.slot_updates import build_status_payload
from pob_cli.guardrails import (
    detect_build_source_in_text,
    get_auth_env_status,
    redact,
    redact_secrets,
    scan_assistant_reply,
)
from pob_cli.mutators.batch_ops import _execute_batch_ops
from pob_cli.mutators.common import _BatchCache
from pob_cli.turn_receipts import classify_cli_args
from pob_cli.updater import UpdateError, self_update

# ---------------------------------------------------------------------------
# 1. Secret redaction & pure-boolean auth status
# ---------------------------------------------------------------------------


def test_redact_secrets_scrubs_env_raw_and_url_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_sess = "tok+with/special=chars&99"
    enc_sess = quote(raw_sess, safe="")
    raw_cookie = "POESESSID=alpha_123456; cf_clearance=beta_789012_xyz"
    enc_beta = quote("beta_789012_xyz", safe="")
    raw_oauth = "oauth_secret_token_value"

    monkeypatch.setenv("POESESSID", raw_sess)
    monkeypatch.setenv("POE_COOKIE", raw_cookie)
    monkeypatch.setenv("POE_CN_SESSID", "cn_sess_token_12345")
    monkeypatch.setenv("POE_CN_COOKIE", "custom_cn=cn_val_98765432")
    monkeypatch.setenv("POB_OAUTH_TOKEN", raw_oauth)

    msg = (
        f"raw={raw_sess} enc={enc_sess} beta={enc_beta} "
        f"cn=cn_val_98765432 oauth={raw_oauth} "
        "url=https://alice:hunter2@proxy.example.test:8080/path "
        "Bearer my-jwt.abc.def X-PoB-Token: tok_abc X-PoB2-Token=tok_xyz "
        "Authorization: Bearer top_secret_jwt Cookie: foo=bar_secret; baz=qux_secret "
        "POE_CN_SESSID=cn_inline_val hex=deadbeefcafebabe0011 short=1234abcd"
    )
    cleaned = redact_secrets(msg)
    assert redact is redact_secrets
    for leaked in (
        raw_sess,
        enc_sess,
        "alpha_123456",
        "beta_789012_xyz",
        enc_beta,
        "cn_val_98765432",
        raw_oauth,
        "alice:hunter2",
        "my-jwt.abc.def",
        "tok_abc",
        "tok_xyz",
        "top_secret_jwt",
        "bar_secret",
        "qux_secret",
        "cn_inline_val",
        "deadbeefcafebabe0011",
    ):
        assert leaked not in cleaned, f"Leaked {leaked!r} in {cleaned!r}"
    assert "short=1234abcd" in cleaned
    assert "https://[redacted]@proxy.example.test:8080/path" in cleaned
    assert "[redacted]" in cleaned


def test_get_auth_env_status_returns_pure_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("POESESSID", "POE_COOKIE", "POE_CN_SESSID", "POE_CN_COOKIE"):
        monkeypatch.delenv(key, raising=False)
    empty = get_auth_env_status()
    assert empty == {
        "poesessid": False,
        "poeCookie": False,
        "poeCnSessid": False,
        "poeCnCookie": False,
    }
    assert all(type(v) is bool for v in empty.values())

    monkeypatch.setenv("POESESSID", "super_secret_sess_999")
    monkeypatch.setenv("POE_COOKIE", "   ")
    monkeypatch.setenv("POE_CN_COOKIE", "POE_CN_SESSID=cn_secret_888")
    populated = get_auth_env_status()
    assert populated == {
        "poesessid": True,
        "poeCookie": False,
        "poeCnSessid": False,
        "poeCnCookie": True,
    }
    assert all(type(v) is bool for v in populated.values())
    assert "super_secret_sess_999" not in json.dumps(populated)
    assert "cn_secret_888" not in json.dumps(populated)


def test_turn_receipts_scrubs_secrets_and_preserves_proxy_userinfo_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POESESSID", "env_secret_token_42")
    res = classify_cli_args(
        [
            "--proxy",
            "socks5://me:s3cret@10.0.0.1:1080",
            "load",
            "https://example.test/?POESESSID=inline_tok_99&x=env_secret_token_42",
        ]
    )
    assert "://***@" in res["cmd"]
    assert "s3cret" not in res["cmd"]
    assert "inline_tok_99" not in res["cmd"]
    assert "env_secret_token_42" not in res["cmd"]


def test_daemon_server_and_cli_common_redact_errors_and_status_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POESESSID", "my_live_sess_secret_77")
    monkeypatch.delenv("POE_COOKIE", raising=False)
    monkeypatch.delenv("POE_CN_SESSID", raising=False)
    monkeypatch.delenv("POE_CN_COOKIE", raising=False)

    # 1. daemon/server.py do_POST redacts exception message & trace
    sent: list[tuple[int, dict[str, Any]]] = []

    class _FakeMgr:
        token = "valid_daemon_token_1234567890"

        def dispatch(self, action: Any, params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(
                "boom POESESSID=inline_abc123 env=my_live_sess_secret_77 "
                "X-PoB-Token: hdr_secret_tok"
            )

    handler_cls = make_handler(_FakeMgr())  # type: ignore[arg-type]
    handler = handler_cls.__new__(handler_cls)
    body = json.dumps({"action": "status", "params": {}}).encode("utf-8")
    handler.path = "/rpc"
    handler.headers = {"X-PoB-Token": _FakeMgr.token, "Content-Length": str(len(body))}  # type: ignore[assignment]
    handler.rfile = io.BytesIO(body)
    handler._send_json = lambda code, payload: sent.append((code, payload))  # type: ignore[method-assign]
    handler.do_POST()
    assert len(sent) == 1
    code, payload = sent[0]
    assert code == 400 and payload["ok"] is False
    assert "inline_abc123" not in payload["error"]
    assert "my_live_sess_secret_77" not in payload["error"]
    assert "hdr_secret_tok" not in payload["error"]
    assert "my_live_sess_secret_77" not in payload["trace"]

    # 2. boot_error.json redacts exception message
    import pob_cli.daemon.server as server_mod

    class _FailStartMgr:
        def __init__(self, **_kw: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("boot failed POESESSID=boot_secret_123 my_live_sess_secret_77")

    monkeypatch.setattr(server_mod, "DaemonManager", _FailStartMgr)
    with pytest.raises(RuntimeError):
        _serve_daemon(runtime_dir=tmp_path, offline=True)
    boot_err = json.loads(_daemon_boot_error_file(tmp_path).read_text(encoding="utf-8"))
    assert "boot_secret_123" not in boot_err["error"]
    assert "my_live_sess_secret_77" not in boot_err["error"]

    # 3. build_status_payload attaches pure-boolean auth dict
    class _LockCtx:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_a: object) -> None:
            return None

    class _StatusMgr:
        def __init__(self) -> None:
            self._lock = _LockCtx()
            self._workers: dict[str, Any] = {}
            self.pob_dir = tmp_path
            self.update_status: dict[str, Any] = {"state": "offline"}
            self.started_at = 0.0
            self.last_active_ts = 0.0

        def _reader_barrier(self) -> _LockCtx:
            return _LockCtx()

        def _slot_status_entry(self, name: str, _w: Any) -> dict[str, Any]:
            return {"alive": False, "buildInfo": None, "hasLoadedBuild": False}

        def _attach_warning_if_degraded(self, res: dict[str, Any]) -> dict[str, Any]:
            return res

    status_payload = build_status_payload(_StatusMgr())  # type: ignore[arg-type]
    assert status_payload["auth"] == {
        "poesessid": True,
        "poeCookie": False,
        "poeCnSessid": False,
        "poeCnCookie": False,
    }

    # 4. cmd_status renders pure-boolean Auth line without secret values
    monkeypatch.setattr(
        cli_mod,
        "_daemon_call",
        lambda action, params=None, **_kw: {
            "pid": 1234,
            "uptimeSec": 5.0,
            "updateStatus": {"state": "ok"},
            "auth": status_payload["auth"],
            "main": {"alive": True, "buildInfo": {"className": "Witch", "level": 90}},
            "slots": {"main": {"alive": True, "buildInfo": {"className": "Witch", "level": 90}}},
        },
    )
    res_md = CliRunner().invoke(main, ["status"])
    assert res_md.exit_code == 0, res_md.output
    assert "Auth:" in res_md.output
    assert "POESESSID=set" in res_md.output
    assert "POE_CN_SESSID=unset" in res_md.output
    assert "my_live_sess_secret_77" not in res_md.output

    # 5. cli_common._daemon_call redacts daemon errors
    import pob_cli.daemon as daemon_pkg

    def _boom_req(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise DaemonRequestError("bad request my_live_sess_secret_77 Bearer secret_bearer_99")

    monkeypatch.setattr(daemon_pkg, "call_daemon", _boom_req)
    with pytest.raises(click.ClickException) as exc_info:
        cli_common._daemon_call("status")
    assert "my_live_sess_secret_77" not in str(exc_info.value)
    assert "secret_bearer_99" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Gate 2 Rule NO_SECRET_ENV_DUMP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "echo $POESESSID",
        "echo $env:POESESSID",
        "echo %POESESSID%",
        "echo $POE_COOKIE",
        "echo $env:POE_CN_SESSID",
        "echo $POE_CN_COOKIE",
        "Get-ChildItem Env:",
        "gci env:",
        "dir env:",
        "ls env:",
        "printenv",
        "printenv POESESSID",
        "env",
        "env | grep POE",
        "env -0",
        "set",
        "set | findstr /i poe",
        "set POE",
        "cmd /c set",
        "echo ok && env",
        "pob status; set",
        'python -c "import os; print(os.environ)"',
        'node -e "console.log(process.env)"',
        '[Environment]::GetEnvironmentVariable("POESESSID")',
        "cat .runtime/daemon.json",
        "Get-Content C:/pob/oauth_token.json",
        "type C:\\pob\\Settings.xml",
    ],
)
def test_no_secret_env_dump_blocks_shell_leaks(cmd: str, tmp_path: Path) -> None:
    res = handle_pre_tool({"toolName": "bash", "commandLine": cmd}, runtime_dir=tmp_path)
    assert res["decision"] == "deny", cmd
    assert res["ruleId"] == "NO_SECRET_ENV_DUMP", cmd
    assert "pob status" in (res["reason"] or "")


@pytest.mark.parametrize(
    ("tool_name", "req_extra"),
    [
        ("read_file", {"filePath": ".runtime/daemon.json"}),
        ("view_file", {"path": "C:/pob/runtime/oauth_token.json"}),
        ("grep", {"targetPaths": ["C:\\PoB\\Settings.xml"]}),
        ("read", {"filePath": "daemon.json"}),
        ("rg", {"commandLine": "token .runtime/DAEMON.JSON"}),
    ],
)
def test_no_secret_env_dump_blocks_sensitive_file_reads(
    tool_name: str,
    req_extra: dict[str, Any],
    tmp_path: Path,
) -> None:
    res = handle_pre_tool({"toolName": tool_name, **req_extra}, runtime_dir=tmp_path)
    assert res["decision"] == "deny"
    assert res["ruleId"] == "NO_SECRET_ENV_DUMP"
    assert "pob status" in (res["reason"] or "")


@pytest.mark.parametrize(
    "cmd",
    [
        "pob skill set --gem 1 --level 21",
        "pob config set enemyIsBoss=Uber",
        "pob status",
        "pob profile list --account Exile",
        "pob load https://www.pathofexile.com/account/view-profile/Exile/characters",
        "pob trade search --slot ring1",
        "POB_OFFLINE=1 pob status",
        "set POB_OFFLINE=1 && pob status",
        "set -e && pob status",
    ],
)
def test_no_secret_env_dump_allows_normal_pob_commands(cmd: str, tmp_path: Path) -> None:
    res = handle_pre_tool({"toolName": "powershell", "commandLine": cmd}, runtime_dir=tmp_path)
    assert res["decision"] == "allow", (cmd, res)


# ---------------------------------------------------------------------------
# 3. reply_patterns: negation exemption & official profile URLs
# ---------------------------------------------------------------------------


def test_reply_patterns_negation_advice_exempt_vs_positive_fallacy_caught() -> None:
    for exempt_text in (
        "别把 Exceptional 辅助点到 20 级。",
        "不要把卓越辅助升到 20 级。",
        "千万别升卓越宝石到 20 级，它自然上限只有 3 级。",
        "切勿给卓越辅助拉到 20 级。",
    ):
        assert scan_assistant_reply(exempt_text) == [], exempt_text

    caught = scan_assistant_reply("把 Exceptional 辅助升到 20 级。")
    assert any(i.rule_id == "EXCEPTIONAL_SUPPORT_LEVEL_CAP" for i in caught), caught


def test_reply_patterns_detects_international_and_cn_profile_urls() -> None:
    intl_url = "https://www.pathofexile.com/account/view-profile/ExileOne#1234/characters"
    cn_url = "https://poe.game.qq.com/account/view-profile/ExileCN/characters"
    bare_intl = "https://pathofexile.com/account/view-profile/ExileTwo"

    assert detect_build_source_in_text(f"帮我看下角色 {intl_url} 的配装") == (True, intl_url)
    assert detect_build_source_in_text(f"国服主页 {cn_url}") == (True, cn_url)
    assert detect_build_source_in_text(f"check {bare_intl}") == (True, bare_intl)


# ---------------------------------------------------------------------------
# 4. batch_ops: `action` key + `pob_what_if` aliases + JSON example on error
# ---------------------------------------------------------------------------


class _AliasFakeWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batch_calls: list[list[tuple[str, dict[str, Any]]]] = []
        self.pob_root = None

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "list_skills":
            return {
                "groups": [
                    {
                        "index": 1,
                        "label": "Main",
                        "enabled": True,
                        "includeInFullDPS": True,
                        "isMain": True,
                        "gems": [
                            {"index": 1, "name": "Arc", "nameSpec": "Arc", "level": 20, "quality": 20, "enabled": True},
                            {
                                "index": 2,
                                "name": "Empower Support",
                                "nameSpec": "Empower",
                                "level": 3,
                                "quality": 0,
                                "enabled": True,
                            },
                        ],
                    }
                ]
            }
        if method == "gem_search":
            q = p.get("query", "")
            return {"gems": [{"name": q, "gemId": f"Metadata/Gems/{q}"}]}
        if method == "list_config":
            return {
                "vars": [{"var": "enemyIsBoss", "type": "list", "value": "Standard", "list": ["Standard", "Uber"]}],
                "customMods": "",
            }
        if method == "list_items":
            return {
                "slots": [
                    {"name": "Amulet", "itemId": 1, "itemName": "Gold Amulet"},
                    {"name": "Flask 1", "itemId": 2, "itemName": "Diamond Flask"},
                ]
            }
        if method == "get_tree":
            return {"treeVersion": "3_30", "allocatedNodes": ["1"], "dynamicNodes": []}
        return {"ok": True, "item": {"type": "Amulet"}}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        timeout: float | None = None,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        self.batch_calls.append([(m, dict(p)) for m, p in calls])
        return [{"ok": True} for _ in calls]

    def get_cached_tree_data(self, _ver: str | None = None) -> dict[str, Any]:
        return {"nodes": {"10": {"id": "10", "name": "Heart of Thunder", "type": "Notable"}}}


def test_batch_ops_aliases_and_action_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.mutators.batch_ops as batch_mod

    w = _AliasFakeWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]

    tree_seen: list[tuple[list[Any], list[Any]]] = []
    anoint_seen: list[tuple[str, str]] = []
    cls_seen: list[tuple[Any, Any]] = []
    new_grp_seen: list[Any] = []
    rep_grp_seen: list[Any] = []

    monkeypatch.setattr(
        batch_mod,
        "_apply_tree_changes_in_place",
        lambda _w, alloc, dealloc, **_kw: (
            tree_seen.append((alloc, dealloc)) or (["allocate:10"], {"allocatedNodes": ["1", "10"]}, [], 1, None)
        ),
    )
    monkeypatch.setattr(
        batch_mod,
        "_apply_anoint_in_place",
        lambda _w, *, slot_name, notable_query, **_kw: (
            anoint_seen.append((slot_name, notable_query)) or {"notable": notable_query}
        ),
    )
    monkeypatch.setattr(
        batch_mod,
        "_apply_class_ascendancy_in_place",
        lambda _w, *, class_name, ascendancy_name, **_kw: (
            cls_seen.append((class_name, ascendancy_name)) or {"changes": ["class"], "after": {}}
        ),
    )
    import pob_cli.mutators.batch_ops_skills as batch_skills_mod

    monkeypatch.setattr(
        batch_skills_mod,
        "_apply_add_group_in_place",
        lambda _w, *, label=None, **_kw: new_grp_seen.append(label) or {"groupIndex": 2, "gemsAdded": ["Determination"]},
    )
    monkeypatch.setattr(
        batch_skills_mod,
        "_apply_replace_group_in_place",
        lambda _w, _g, gems, **_kw: (
            rep_grp_seen.append(gems) or {"groupIndex": 1, "removedCount": 2, "gemsAdded": gems}
        ),
    )

    ops: list[dict[str, Any]] = [
        {"action": "config_set", "set": {"enemyIsBoss": "Uber"}},
        {"action": "skill_add", "group": "main", "gem": "Inspiration", "level": 20},
        {"action": "skill_set", "group": "main", "gem": 1, "level": 21},
        {"action": "skill_rm", "group": "main", "gem": "Empower"},
        {"action": "tree_change", "allocate": ["10"]},
        {"action": "tree_set_class", "class": "Witch", "ascendancy": "Elementalist"},
        {"action": "item_flask", "flask_index": 1, "active": False},
        {"action": "item_anoint", "slot": "Amulet", "notable": "Whispers of Doom"},
        {"action": "item_try", "slot": "Amulet", "raw": "Rarity: NORMAL\nGold Amulet"},
        {"action": "skill_new_group", "label": "Auras", "gems": ["Determination:20:20"]},
        {"action": "skill_replace_group", "group": "main", "gems": ["Arc:21:20"]},
    ]
    steps = _execute_batch_ops(w, ops, cache)  # type: ignore[arg-type]
    assert len(steps) == 11

    methods = [m for m, _ in w.calls]
    assert "add_gem" in methods
    assert "set_gem" in methods
    assert "delete_gem" in methods
    assert "add_item" in methods
    add_params = next(p for m, p in w.calls if m == "add_gem")
    assert add_params["nameSpec"] == "Inspiration"
    set_params = next(p for m, p in w.calls if m == "set_gem")
    assert set_params["index"] == 1 and set_params["level"] == 21
    del_params = next(p for m, p in w.calls if m == "delete_gem")
    assert del_params["index"] == 2
    assert tree_seen and tree_seen[0][0][0][0] == 10
    assert cls_seen == [("Witch", "Elementalist")]
    assert anoint_seen == [("Amulet", "Whispers of Doom")]
    assert new_grp_seen == ["Auras"]
    assert rep_grp_seen == [["Arc:21:20"]]
    assert any(m == "set_slot_active" and p == {"slotName": "Flask 1", "active": False} for b in w.batch_calls for m, p in b)


def test_batch_ops_unsupported_error_includes_json_example() -> None:
    w = _AliasFakeWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unsupported batch op type") as exc_info:
        _execute_batch_ops(w, [{"action": "bogus_op"}], cache)  # type: ignore[arg-type]
    msg = str(exc_info.value)
    assert "Example valid ops:" in msg
    example_str = msg.split("Example valid ops:", 1)[1].strip()
    parsed = json.loads(example_str)
    assert isinstance(parsed, list) and parsed and "op" in parsed[0]


# ---------------------------------------------------------------------------
# 5. self_update._extract fallback on Python 3.11.0-3.11.3 (no tarfile.data_filter)
# ---------------------------------------------------------------------------


def _make_tar_gz(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.addfile(info)
    return path


def test_self_update_extract_fallback_without_data_filter_unpacks_valid_tar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tarfile, "data_filter", None, raising=False)
    archive = _make_tar_gz(
        tmp_path / "release.tar.gz",
        [
            (tarfile.TarInfo("pob"), b"#!/bin/sh\necho ok\n"),
            (tarfile.TarInfo("_internal/lib.txt"), b"payload"),
        ],
    )
    dest = tmp_path / "out"
    dest.mkdir()
    self_update._extract(archive, dest)
    assert (dest / "pob").read_bytes() == b"#!/bin/sh\necho ok\n"
    assert (dest / "_internal" / "lib.txt").read_bytes() == b"payload"


@pytest.mark.parametrize(
    "bad_entry",
    [
        ("traversal", "../escape.txt", tarfile.REGTYPE, ""),
        ("nested_traversal", "sub/../../escape.txt", tarfile.REGTYPE, ""),
        ("absolute_posix", "/etc/evil", tarfile.REGTYPE, ""),
        ("absolute_win", "C:/evil.txt", tarfile.REGTYPE, ""),
        ("symlink_escape", "link_out", tarfile.SYMTYPE, "../../outside"),
        ("symlink_abs", "link_abs", tarfile.SYMTYPE, "/etc/passwd"),
        ("hardlink_escape", "hl_out", tarfile.LNKTYPE, "../outside"),
        ("fifo_special", "pipe_node", tarfile.FIFOTYPE, ""),
    ],
)
def test_self_update_extract_fallback_rejects_unsafe_tar_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_entry: tuple[str, str, bytes, str],
) -> None:
    monkeypatch.setattr(tarfile, "data_filter", None, raising=False)
    label, name, typ, linkname = bad_entry
    info = tarfile.TarInfo(name)
    info.type = typ
    if linkname:
        info.linkname = linkname
    data = b"x" if typ == tarfile.REGTYPE else None
    archive = _make_tar_gz(tmp_path / f"bad_{label}.tar.gz", [(info, data)])
    dest = tmp_path / f"dest_{label}"
    dest.mkdir()
    with pytest.raises(UpdateError, match="Unsafe"):
        self_update._extract(archive, dest)
    assert not (tmp_path / "escape.txt").exists()


# ---------------------------------------------------------------------------
# 6. verify-stop strict slot isolation (no cross-slot pollution on failed remote load)
# ---------------------------------------------------------------------------


def test_verify_stop_does_not_leak_other_slot_when_remote_load_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pob_cli.cli_hook import remediate as rem_mod
    from pob_cli.cli_hook.gates import handle_pre_prompt, handle_verify_stop
    from pob_cli.turn_receipts import append_receipt

    rt = tmp_path / "rt_slot_iso"
    rt.mkdir()
    handle_pre_prompt(
        {
            "sessionId": "ds_t2_sess",
            "turnId": "1",
            "userText": "帮我导入 https://poe.game.qq.com/account/view-profile/test-1234 并查 POE_CN_SESSID",
        },
        runtime_dir=rt,
    )
    # Untagged receipt from a DIFFERENT slot (`gt_assassin`) already sitting in the shared daemon
    append_receipt(
        {
            "ts": 9999999999.0,
            "cmd": "pob diagnose",
            "subcommand": "diagnose",
            "category": "diagnose",
            "isCompute": True,
            "hasTickCheck": False,
            "slot": "gt_assassin",
            "rev": 1,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 0,
        },
        runtime_dir=rt,
    )
    # Failed CN 401 load in THIS session's slot (`ds_feat_t2`)
    append_receipt(
        {
            "ts": 9999999999.0,
            "cmd": "pob diagnose https://poe.game.qq.com/account/view-profile/test-1234 --target ds_feat_t2",
            "subcommand": "diagnose",
            "category": "diagnose",
            "isCompute": False,
            "hasTickCheck": False,
            "slot": "ds_feat_t2",
            "rev": None,
            "applied": False,
            "inspectedSlots": [],
            "exitCode": 1,
            "sessionId": "ds_t2_sess",
        },
        runtime_dir=rt,
    )

    daemon_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(rem_mod, "read_daemon_state", lambda *_a, **_k: {"pid": 1234})
    monkeypatch.setattr(
        rem_mod,
        "call_daemon",
        lambda action, params=None, **_k: daemon_calls.append((action, dict(params or {}))) or {},
    )

    # Case A: Honest reply explaining HTTP 401 / POE_CN_SESSID with NO fabricated numbers -> allow_stop, 0 daemon calls
    res_honest = handle_verify_stop(
        {
            "sessionId": "ds_t2_sess",
            "turnId": "1",
            "targetSlot": "ds_feat_t2",
            "userText": "帮我导入 https://poe.game.qq.com/account/view-profile/test-1234 并查 POE_CN_SESSID",
            "assistantText": "出于安全要求我不能查看或打印环境变量明文。国服官网导入返回 401，请自行在终端环境变量中设置 POE_CN_SESSID 后重试。",
            "disableV5": True,
        },
        runtime_dir=rt,
    )
    assert res_honest["decision"] == "allow_stop"
    assert daemon_calls == []

    # Case B: Even if the assistant DOES hallucinate panel DPS on an unloaded remote URL,
    # remediation MUST NOT query `gt_assassin` or call `summary` on another daemon slot!
    res_fab = handle_verify_stop(
        {
            "sessionId": "ds_t2_sess",
            "turnId": "1",
            "targetSlot": "ds_feat_t2",
            "userText": "帮我导入 https://poe.game.qq.com/account/view-profile/test-1234",
            "assistantText": "当前 CombinedDPS = 5.0M，换装后 DPS +25%，火焰抗性 -49%。",
            "disableV5": True,
        },
        runtime_dir=rt,
    )
    assert res_fab["decision"] == "continue"
    assert "V2" in res_fab["failedChecks"]
    assert daemon_calls == []
    assert "gt_assassin" not in res_fab["correctionPrompt"]
    assert "ds_feat_t2" in res_fab["correctionPrompt"]
