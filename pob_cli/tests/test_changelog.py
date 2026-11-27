"""Tests for bundled bilingual CHANGELOG.md, `pob changelog`, and installer welcome tips."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob_cli import __version__
from pob_cli.cli import main
from pob_cli.locale_cfg import set_active_lang
from pob_cli.updater import changelog

REPO = Path(__file__).resolve().parents[2]

EXPECTED_WELCOME_ZH = """本插件由b站@流放降临-某雷 维护，永久免费开源，喜欢的话来点个star或者关注吧！
项目地址：https://github.com/exilemoulei/pob4agents
这个插件能让你一句话：分析流放之路的角色链接/生成所有装备购买链接/估算角色造价，甚至还能帮你找永恒珠宝！
它会强制要求AI用PoB里可以验证的真实数据说话，让它不能胡编乱造
插件目前还不太完善，记得两件事：聊多了就开个新窗口，最好不要超过6轮对话，以及多给它一些补充信息，比如你玩的是什么bd/抄的谁的作业/有什么特殊机制，会很有帮助！
还有更多强大功能等待你解锁，可以多问问它！"""

EXPECTED_WELCOME_EN = """pob4agents is maintained by @moulei (https://www.youtube.com/@moulei) and is permanently free and open-source — if you find it helpful, please drop a Star or subscribe!
GitHub: https://github.com/exilemoulei/pob4agents
With one prompt, it can analyze Path of Exile character/PoB links, generate trade shopping links for your gear, estimate build cost, and even hunt Timeless Jewels for you!
It forces AI to back every claim with verifiable Path of Building calculations instead of making numbers up.
It's still evolving, so remember two things: start a fresh chat window before going past ~6 turns, and share extra context (what build you're playing, whose guide/PoB you're following, and any special mechanics) — it helps a ton!
There are plenty more powerful features waiting for you — just ask your agent!"""


def test_repo_and_bundled_changelog_are_byte_identical() -> None:
    root_md = REPO / "CHANGELOG.md"
    bundled_md = changelog.bundled_changelog_path()
    assert root_md.is_file()
    assert bundled_md.is_file()
    assert root_md.read_bytes() == bundled_md.read_bytes()


def test_bundled_v011_changelog_contains_exact_author_texts() -> None:
    entries = changelog.load_changelog()
    assert any(e.version == "0.11.0" for e in entries)
    v011 = next(e for e in entries if e.version == "0.11.0")
    assert v011.zh.startswith("0.11 新增功能\n1.大幅优化了购物体验")
    assert "由b站@流放降临-某雷 维护" in v011.zh
    assert v011.zh.endswith("项目地址：https://github.com/exilemoulei/pob4agents")
    assert v011.en.startswith("0.11 What's New\n1. Massively improved the shopping experience")
    assert "https://www.youtube.com/@moulei" in v011.en
    assert v011.en.endswith("https://github.com/exilemoulei/pob4agents")


def test_pob_changelog_cli_lang_resolution_and_relay_framing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB_LANG", raising=False)
    runner = CliRunner()

    # Default zh-CN
    res_zh = runner.invoke(main, ["changelog"])
    assert res_zh.exit_code == 0, res_zh.output
    assert "--- BEGIN USER RELEASE NOTES ---" in res_zh.output
    assert "--- END USER RELEASE NOTES ---" in res_zh.output
    assert "0.11 新增功能" in res_zh.output

    # Explicit --lang en overrides config/default
    res_en = runner.invoke(main, ["changelog", "--lang", "en"])
    assert res_en.exit_code == 0, res_en.output
    assert "Below are the author's release notes for the user." in res_en.output
    assert "0.11 What's New" in res_en.output

    # Persisted `pob lang en` config is respected
    set_active_lang("en", tmp_path)
    res_cfg = runner.invoke(main, ["changelog"])
    assert res_cfg.exit_code == 0, res_cfg.output
    assert "0.11 What's New" in res_cfg.output

    # POB_LANG overrides config.json
    monkeypatch.setenv("POB_LANG", "zh-TW")
    res_env = runner.invoke(main, ["changelog", "--json"])
    assert res_env.exit_code == 0, res_env.output
    data = json.loads(res_env.stdout)
    assert data["lang"] == "zh-TW"
    assert data["installed"] == __version__
    assert data["since"] is None
    assert data["versions"] == ["0.11.0"]
    assert data["truncated"] is False
    assert data["releasesUrl"] == changelog.RELEASES_URL
    assert "0.11 新增功能" in data["text"]


def test_pob_changelog_since_filtering_limit_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_md = """# Changelog

## v0.14.0
### 中文
v0.14 中文说明
### English
v0.14 English notes

## v0.13.0
### 中文
v0.13 中文说明
### English
v0.13 English notes

## v0.12.0
### 中文
v0.12 中文说明
### English
v0.12 English notes

## v0.11.0
### 中文
v0.11 中文说明
### English
v0.11 English notes
"""
    entries = changelog.parse_changelog(sample_md)
    monkeypatch.setattr(changelog, "load_changelog", lambda _path=None: entries)
    runner = CliRunner()

    # Without --since, default limit is 1
    res_default = runner.invoke(main, ["changelog", "--lang", "en", "--json"])
    assert res_default.exit_code == 0
    d_default = json.loads(res_default.stdout)
    assert d_default["versions"] == ["0.14.0"]
    assert d_default["truncated"] is True
    assert changelog.RELEASES_URL in d_default["text"]

    # With --since 0.10.0, default limit is 3 (4 matches -> truncated)
    res_since = runner.invoke(main, ["changelog", "--since", "0.10.0", "--lang", "en", "--json"])
    assert res_since.exit_code == 0
    d_since = json.loads(res_since.stdout)
    assert d_since["since"] == "0.10.0"
    assert d_since["versions"] == ["0.14.0", "0.13.0", "0.12.0"]
    assert d_since["truncated"] is True
    assert changelog.RELEASES_URL in d_since["text"]

    # With --since 0.12.0, 2 matches -> not truncated
    res_two = runner.invoke(main, ["changelog", "--since", "v0.12.0", "--lang", "zh-CN", "--json"])
    assert res_two.exit_code == 0
    d_two = json.loads(res_two.stdout)
    assert d_two["since"] == "0.12.0"
    assert d_two["versions"] == ["0.14.0", "0.13.0"]
    assert d_two["truncated"] is False
    assert [n["version"] for n in d_two["notes"]] == ["0.14.0", "0.13.0"]

    # With --since equal to latest -> up-to-date message without relay markers
    res_none = runner.invoke(main, ["changelog", "--since", "0.14.0", "--lang", "en"])
    assert res_none.exit_code == 0
    assert "No release notes newer than v0.14.0" in res_none.output
    assert "BEGIN USER RELEASE NOTES" not in res_none.output

    # Invalid --since and --lang fail cleanly
    assert runner.invoke(main, ["changelog", "--since", "not-a-ver"]).exit_code != 0
    assert runner.invoke(main, ["changelog", "--lang", "fr"]).exit_code != 0


def test_install_ps1_is_pure_ascii_and_welcome_tips_match_exact_text() -> None:
    ps1_raw = (REPO / "install.ps1").read_bytes()
    non_ascii = [b for b in ps1_raw if b >= 128]
    assert non_ascii == [], "install.ps1 must stay 100% pure ASCII for PowerShell 5.1 irm|iex safety"

    ps1_text = ps1_raw.decode("ascii")
    m = re.search(r'\$WelcomeTipZhB64\s*=\s*"([A-Za-z0-9+/=]+)"', ps1_text)
    assert m is not None
    decoded_zh = base64.b64decode(m.group(1)).decode("utf-8")
    assert "--- BEGIN USER WELCOME TIPS ---" in decoded_zh
    assert EXPECTED_WELCOME_ZH in decoded_zh
    assert "--- END USER WELCOME TIPS ---" in decoded_zh
    assert "[Console]::OutputEncoding" in ps1_text
    assert "--- BEGIN USER RELEASE NOTES ---" in ps1_text

    sh_text = (REPO / "install.sh").read_text(encoding="utf-8")
    assert EXPECTED_WELCOME_ZH in sh_text
    assert EXPECTED_WELCOME_EN in sh_text
    assert "--- BEGIN USER RELEASE NOTES ---" in sh_text
