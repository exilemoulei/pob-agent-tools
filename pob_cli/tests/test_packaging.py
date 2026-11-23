"""Tests for standalone packaging, frozen daemon spawning, runtime dir resolution, and CLI version."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import pob_cli
from pob_cli import daemon, updater
from pob_cli.cli import main


def test_version_option_matches_package_version() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["--version"])
    assert res.exit_code == 0
    assert pob_cli.__version__ in res.output


def test_internal_daemon_command_is_hidden_and_parseable() -> None:
    runner = CliRunner()
    top_help = runner.invoke(main, ["--help"])
    assert top_help.exit_code == 0
    assert "_daemon" not in top_help.output

    sub_help = runner.invoke(main, ["_daemon", "--help"])
    assert sub_help.exit_code == 0
    assert "--runtime-dir" in sub_help.output
    assert "--offline" in sub_help.output


def test_runtime_dir_resolution_installed_and_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POB_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("POB_HOME", raising=False)

    # 1. Explicit runtime_dir wins over everything
    explicit = tmp_path / "explicit_rt"
    assert updater.get_runtime_dir(explicit) == explicit

    # 2. POB_RUNTIME_DIR env wins when explicit is omitted
    env_rt = tmp_path / "env_rt"
    monkeypatch.setenv("POB_RUNTIME_DIR", str(env_rt))
    assert updater.get_runtime_dir() == env_rt
    monkeypatch.delenv("POB_RUNTIME_DIR")

    # 3. POB_HOME env resolves to <POB_HOME>/.runtime
    env_home = tmp_path / "env_home"
    monkeypatch.setenv("POB_HOME", str(env_home))
    assert updater.get_runtime_dir() == env_home / ".runtime"
    monkeypatch.delenv("POB_HOME")

    # 4. Frozen binary (PyInstaller) never uses source checkout parents[3]; uses OS user data dir
    fake_appdata = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(fake_appdata))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(fake_appdata))
    assert updater.detect_source_checkout_root() is None
    assert updater.get_runtime_dir() == fake_appdata / "pob4agents"

    # 5. Seamless legacy fallback: when new dir has no runtime data (even if `bin/` exists from install.ps1)
    #    and legacy `pob-agent-tools` already holds runtime state, reuse the legacy dir.
    (fake_appdata / "pob4agents" / "bin").mkdir(parents=True)
    (fake_appdata / "pob-agent-tools" / "engine").mkdir(parents=True)
    assert updater.get_runtime_dir() == fake_appdata / "pob-agent-tools"

    # 6. Once the new `pob4agents` dir itself has runtime data, prefer `pob4agents` over legacy.
    (fake_appdata / "pob4agents" / "engine").mkdir(parents=True)
    assert updater.get_runtime_dir() == fake_appdata / "pob4agents"


def test_build_daemon_spawn_cmd_frozen_vs_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "rt"

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert daemon._build_daemon_spawn_cmd() == [sys.executable, "-m", "pob_cli.daemon"]
    assert daemon._build_daemon_spawn_cmd(rt) == [
        sys.executable,
        "-m",
        "pob_cli.daemon",
        "--runtime-dir",
        str(rt),
    ]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert daemon._build_daemon_spawn_cmd() == [sys.executable, "_daemon"]
    assert daemon._build_daemon_spawn_cmd(rt) == [
        sys.executable,
        "_daemon",
        "--runtime-dir",
        str(rt),
    ]


def test_release_tools_pack_and_scoop_manifest_update(tmp_path: Path) -> None:
    tool_path = Path(__file__).resolve().parents[1] / "packaging" / "release_tools.py"
    spec = importlib.util.spec_from_file_location("release_tools", tool_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dist_dir = tmp_path / "dist" / "pob"
    (dist_dir / "_internal").mkdir(parents=True)
    (dist_dir / "pob.exe").write_bytes(b"MZ-fake-pob")
    (dist_dir / "_internal" / "cert.pem").write_text("dummy", encoding="utf-8")

    out_dir = tmp_path / "release-assets"
    zip_path, zip_sha = mod.pack_windows_release(dist_dir, out_dir, "v0.2.0")

    assert zip_path.name == "pob-v0.2.0-windows-x64.zip"
    assert len(zip_sha) == 64
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "pob.exe" in names
        assert "_internal/cert.pem" in names

    alias_path = out_dir / "pob-windows-x64.zip"
    assert alias_path.is_file()
    assert alias_path.read_bytes() == zip_path.read_bytes()

    sums = (out_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert f"{zip_sha}  pob-v0.2.0-windows-x64.zip" in sums
    assert f"{zip_sha}  pob-windows-x64.zip" in sums

    repo_manifest = Path(__file__).resolve().parents[2] / "bucket" / "pob.json"
    copied = tmp_path / "pob.json"
    copied.write_text(repo_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    mod.update_scoop_manifest(copied, "0.2.0", "v0.2.0", zip_sha)

    updated = json.loads(copied.read_text(encoding="utf-8"))
    assert updated["version"] == "0.2.0"
    assert updated["architecture"]["64bit"]["hash"] == zip_sha
    assert updated["architecture"]["64bit"]["url"] == (
        "https://github.com/exilemoulei/pob4agents/releases/download/v0.2.0/pob-v0.2.0-windows-x64.zip"
    )


def test_lazy_subcommands_listed_in_pyinstaller_hiddenimports() -> None:
    import pkgutil

    from pob_cli.cli import _LAZY_SUBCOMMANDS

    spec_path = Path(__file__).resolve().parents[1] / "packaging" / "pob.spec"
    spec_text = spec_path.read_text(encoding="utf-8")
    uses_collect = 'collect_submodules("pob_cli")' in spec_text or "collect_submodules('pob_cli')" in spec_text
    discovered = {
        m.name
        for m in pkgutil.walk_packages(pob_cli.__path__, prefix="pob_cli.")
    }
    for cmd_name, (mod_path, _attr, _help) in _LAZY_SUBCOMMANDS.items():
        full_mod = f"pob_cli{mod_path}" if mod_path.startswith(".") else mod_path
        if uses_collect:
            assert full_mod in discovered, (
                f"Lazy subcommand {cmd_name!r} module {full_mod!r} not discoverable by collect_submodules('pob_cli')"
            )
        else:
            assert f'"{full_mod}"' in spec_text or f"'{full_mod}'" in spec_text, (
                f"Lazy subcommand {cmd_name!r} module {full_mod!r} must be listed in pob.spec hiddenimports"
            )


def test_pob_spec_includes_errors_and_timeless_hiddenimports() -> None:
    import pkgutil

    spec_path = Path(__file__).resolve().parents[1] / "packaging" / "pob.spec"
    spec_text = spec_path.read_text(encoding="utf-8")
    uses_collect = 'collect_submodules("pob_cli")' in spec_text or "collect_submodules('pob_cli')" in spec_text
    assert uses_collect, "pob.spec should use collect_submodules('pob_cli') to bundle all modular subpackages"
    discovered = {
        m.name
        for m in pkgutil.walk_packages(pob_cli.__path__, prefix="pob_cli.")
    }
    for mod_name in (
        "pob_cli.errors",
        "pob_cli.ticks",
        "pob_cli.timeless",
        "pob_cli.cli_timeless",
        "pob_cli.cli_trade",
        "pob_cli.daemon",
    ):
        assert mod_name in discovered, f"Required module {mod_name!r} missing from pob_cli package discovery"


def test_cli_timeless_has_zero_cli_trade_or_httpx_imports() -> None:
    import ast

    src_path = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "cli_timeless.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("httpx", "pob_cli.cli_trade", "pob_cli.bench"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(("httpx", "cli_trade", "pob_cli.cli_trade", "bench", "pob_cli.bench"))


def test_timeless_list_cli_json_compact_out_and_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pob_cli import cli_timeless
    from pob_cli.bridge_client import BridgeError
    from pob_cli.errors import DaemonRequestError

    sample_info = {
        "jewels": [
            {
                "name": "Lethal Pride",
                "nameLocal": "致命的骄傲",
                "seeds": 8001,
                "conquerors": ["Kaom", "Rakiata", "Akoya"],
                "grouped": True,
            },
            {
                "name": "Glorious Vanity",
                "nameLocal": "光彩夺目",
                "seeds": 7901,
                "conquerors": ["Doryani", "Xibaqua", "Mazcotl"],
                "grouped": False,
            },
        ],
        "sockets": [
            {"nodeId": 26725, "label": "Near Duelist", "labelLocal": "决斗者附近"},
        ],
    }
    calls: list[tuple[str, dict[str, object] | None]] = []

    def fake_daemon_call(action: str, params: dict[str, object] | None = None) -> dict[str, object]:
        calls.append((action, params))
        return sample_info

    monkeypatch.setattr(cli_timeless, "_daemon_call", fake_daemon_call)
    runner = CliRunner()

    # 1. --target + --json + --compact + --out
    out_file = tmp_path / "timeless_list.json"
    res_json = runner.invoke(
        main,
        ["timeless", "list", "--target", "alt", "--json", "--compact", "--out", str(out_file)],
    )
    assert res_json.exit_code == 0, res_json.output
    assert calls[-1] == ("timeless_list", {"target": "alt"})
    raw_written = out_file.read_text(encoding="utf-8").strip()
    assert "\n" not in raw_written
    assert json.loads(raw_written) == sample_info

    # 2. Markdown mode + --slot alias
    res_md = runner.invoke(main, ["timeless", "list", "--slot", "ref"])
    assert res_md.exit_code == 0, res_md.output
    assert calls[-1] == ("timeless_list", {"target": "ref"})
    assert "### Timeless Jewels" in res_md.output
    assert "Lethal Pride" in res_md.output
    assert "致命的骄傲" in res_md.output
    assert "### Allocated Jewel Sockets (`ref`)" in res_md.output
    assert "26725" in res_md.output

    # 3. ValueError & DaemonRequestError -> exit 1; BridgeError -> exit 5
    def raise_val(_action: str, _params: dict[str, object] | None = None) -> dict[str, object]:
        raise ValueError("bad timeless request")

    monkeypatch.setattr(cli_timeless, "_daemon_call", raise_val)
    res_val = runner.invoke(main, ["timeless", "list"])
    assert res_val.exit_code == 1
    assert "bad timeless request" in res_val.output

    def raise_req(_action: str, _params: dict[str, object] | None = None) -> dict[str, object]:
        raise DaemonRequestError("slot empty")

    monkeypatch.setattr(cli_timeless, "_daemon_call", raise_req)
    res_req = runner.invoke(main, ["timeless", "list"])
    assert res_req.exit_code == 1
    assert "slot empty" in res_req.output

    def raise_bridge(_action: str, _params: dict[str, object] | None = None) -> dict[str, object]:
        raise BridgeError("engine pipe closed")

    monkeypatch.setattr(cli_timeless, "_daemon_call", raise_bridge)
    res_bridge = runner.invoke(main, ["timeless", "list"])
    assert res_bridge.exit_code == 5
    assert "engine pipe closed" in res_bridge.output


def test_timeless_search_cli_output_inf_none_safety_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pob_cli import cli_timeless
    from pob_cli.bridge_client import BridgeError

    daemon_calls: list[tuple[str, dict[str, object] | None]] = []
    search_calls: list[dict[str, object]] = []
    idle_called: list[bool] = []

    def fake_daemon_call(action: str, params: dict[str, object] | None = None) -> dict[str, object]:
        daemon_calls.append((action, params))
        return {"code": "FAKE_CODE", "xml": "<PathOfBuilding/>", "pobDir": "C:/fake/pob"}

    sample_result: dict[str, object] = {
        "cutoff": 1800000.0,
        "jewel": "Lethal Pride",
        "conqueror": "Kaom",
        "socket": 26725,
        "mods": ["+20 to Strength"],
        "removed": [{"jewel": "Brutal Restraint", "socket": 7960}],
        "stat": "CombinedDPS",
        "seedCount": 8001,
        "groupCount": 400,
        "calculated": 120,
        "estimate": {"calculated": 120, "maxError": 0.0125, "relative": True},
        "seedNodes": [101, 102],
        "workers": 2,
        "baseline": {
            "CombinedDPS": 1500000.0,
            "TotalEHP": "inf",
            "Life": None,
            "Armour": 25000.0,
        },
        "outcomes": [
            {
                "stats": {
                    "CombinedDPS": 1800000.0,
                    "TotalEHP": "inf",
                    "Life": None,
                    "Armour": 31000.0,
                },
                "seeds": [10001, 10002, 10003, 10004, 10005, 10006, 10007],
            },
        ],
        "selfCheck": {"checked": 5, "mismatches": [10002]},
        "seconds": 4.2,
    }

    def fake_search(
        code: str,
        xml: str,
        socket: int,
        jewel_name: str,
        **kwargs: object,
    ) -> dict[str, object]:
        search_calls.append({"code": code, "xml": xml, "socket": socket, "jewel_name": jewel_name, **kwargs})
        prog = kwargs.get("progress")
        if callable(prog):
            prog("sampled 120")
        return sample_result

    monkeypatch.setattr(cli_timeless, "_daemon_call", fake_daemon_call)
    monkeypatch.setattr(cli_timeless, "search", fake_search)
    monkeypatch.setattr(cli_timeless, "set_idle_priority", lambda: idle_called.append(True))
    runner = CliRunner()

    # 1. --slot alias must NOT exist on timeless search (avoids --slot vs --socket confusion)
    res_slot_err = runner.invoke(
        main,
        ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725", "--slot", "alt"],
    )
    assert res_slot_err.exit_code == 2
    assert "--slot" in res_slot_err.output

    # 2. Markdown mode with "inf" TotalEHP, None Life, removed jewel, estimate note, mismatch warning & --idle
    res_md = runner.invoke(
        main,
        [
            "timeless",
            "search",
            "--jewel",
            "致命的骄傲",
            "--socket",
            "26725",
            "--conqueror",
            "Kaom",
            "--mod",
            "+20 to Strength",
            "--top",
            "5",
            "--workers",
            "2",
            "--idle",
            "--target",
            "alt",
        ],
    )
    assert res_md.exit_code == 0, res_md.output
    assert idle_called == [True]
    assert daemon_calls[-1] == ("export", {"target": "alt", "include_xml": True})
    last_call = search_calls[-1]
    assert last_call["code"] == "FAKE_CODE"
    assert last_call["xml"] == "<PathOfBuilding/>"
    assert last_call["socket"] == 26725
    assert last_call["jewel_name"] == "致命的骄傲"
    assert last_call["mods"] == ("+20 to Strength",)
    assert last_call["pob_dir"] == "C:/fake/pob"
    assert callable(last_call["progress"])
    assert "Timeless Search: `Lethal Pride` (Kaom) at socket `26725`" in res_md.output
    assert "inf" in res_md.output
    assert "Brutal Restraint" in res_md.output
    assert "1.25%" in res_md.output
    assert "[WARN] Seeds [10002]" in res_md.output
    assert "10006 ..." in res_md.output

    # 3. --json --compact --out suppresses progress callback and writes single-line JSON
    out_file = tmp_path / "timeless_search.json"
    res_json = runner.invoke(
        main,
        [
            "timeless",
            "search",
            "--jewel",
            "Lethal Pride",
            "--socket",
            "26725",
            "--json",
            "--compact",
            "--out",
            str(out_file),
        ],
    )
    assert res_json.exit_code == 0, res_json.output
    assert search_calls[-1]["progress"] is None
    raw_written = out_file.read_text(encoding="utf-8").strip()
    assert "\n" not in raw_written
    assert json.loads(raw_written)["jewel"] == "Lethal Pride"

    # 4. --out in Markdown mode also suppresses progress callback + custom --stat + exact mode
    exact_result = {**sample_result, "stat": "Armour", "estimate": None, "calculated": 400, "selfCheck": {"checked": 5, "mismatches": []}}
    monkeypatch.setattr(cli_timeless, "search", lambda *a, **kw: (search_calls.append(kw), exact_result)[1])
    out_md = tmp_path / "timeless_search.md"
    res_exact_md = runner.invoke(
        main,
        [
            "timeless",
            "search",
            "--jewel",
            "Lethal Pride",
            "--socket",
            "26725",
            "--stat",
            "Armour",
            "--exact",
            "--out",
            str(out_md),
        ],
    )
    assert res_exact_md.exit_code == 0, res_exact_md.output
    assert search_calls[-1]["progress"] is None
    md_text = out_md.read_text(encoding="utf-8")
    assert "Armour" in md_text
    assert "31,000" in md_text
    assert "Exact search: all 400 of 400" in md_text

    # 5. Validation errors (--top < 1, --workers < 1) -> exit 1
    assert runner.invoke(main, ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725", "--top", "0"]).exit_code == 1
    assert runner.invoke(main, ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725", "--workers", "0"]).exit_code == 1

    # 6. ValueError -> exit 1; BridgeError -> exit 5
    def raise_val(*_a: object, **_kw: object) -> dict[str, object]:
        raise ValueError("Node 999 is not an allocated jewel socket")

    monkeypatch.setattr(cli_timeless, "search", raise_val)
    res_val = runner.invoke(main, ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "999"])
    assert res_val.exit_code == 1
    assert "not an allocated jewel socket" in res_val.output

    def raise_bridge(*_a: object, **_kw: object) -> dict[str, object]:
        raise BridgeError("headless worker crashed")

    monkeypatch.setattr(cli_timeless, "search", raise_bridge)
    res_bridge = runner.invoke(main, ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725"])
    assert res_bridge.exit_code == 5
    assert "headless worker crashed" in res_bridge.output



