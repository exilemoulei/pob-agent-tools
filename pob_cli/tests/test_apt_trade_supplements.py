"""Offline unit tests for APT trade stat polarity (`better`), Chinese stat supplements, and size caps."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pob_cli import cli_trade as ct
from pob_cli import trade_query as tq
from pob_cli.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "pob_cli" / "src" / "pob_cli"
DATA_DIR = PKG_ROOT / "data"
SCRIPTS_DIR = REPO_ROOT / "pob_cli" / "scripts"


def test_apt_bundle_total_size_cap_and_notices() -> None:
    notices = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    assert notices.is_file()
    assert "SnosMe/awakened-poe-trade" in notices.read_text(encoding="utf-8")

    files = (
        DATA_DIR / "apt_item_drops.json.gz",
        DATA_DIR / "apt_non_gear_items.json.gz",
        DATA_DIR / "apt_trade_supplements.json.gz",
    )
    sizes = [f.stat().st_size for f in files]
    assert all(sz > 0 for sz in sizes)
    assert sizes[0] < 15_000
    assert sizes[1] <= 50_000
    assert sizes[2] <= 125_000
    assert sum(sizes) <= 190_000, f"APT data bundle exceeded 190KB cap: {sum(sizes)} bytes"


def test_stat_polarity_lookup() -> None:
    # Default polarity is 1 (higher is better)
    assert tq.get_stat_polarity("explicit.stat_3299347043") == 1
    assert tq.get_stat_polarity("") == 1
    assert tq.get_stat_polarity("explicit.stat_unknown_id") == 1
    # Lower is better (-1), e.g., increased Attribute Requirements / increased Charges per use
    assert tq.get_stat_polarity("explicit.stat_3639275092") == -1
    assert tq.get_stat_polarity("explicit.stat_388617051") == -1
    # Neutral / boolean option (0), e.g., temple room option stat
    assert tq.get_stat_polarity("pseudo.pseudo_temple_breach_room_1") == 0


def test_chinese_trade_stat_translation_strips_brackets_and_resolves_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Force engine-less offline environment so resolution MUST come from ZH_STAT_ALIASES / APT supplements
    monkeypatch.setattr(tq, "resolve_engine_root", lambda: tmp_path / "no_engine")

    # Watcher's Eye aura stats with CN brackets 【】 / 「」 or without brackets
    en_det_armour = tq.translate_stat_query("受到【坚定】影响时 +# 护甲")
    assert "Armour" in en_det_armour and "Determination" in en_det_armour

    en_det_pdr = tq.translate_stat_query("受到「坚定」影响时，有 #% 额外物理伤害减免")
    assert "Physical Damage Reduction" in en_det_pdr and "Determination" in en_det_pdr

    en_det_plain = tq.translate_stat_query("受到坚定影响时")
    assert "Determination" in en_det_plain

    # Pseudo / crafted stat templates
    en_crafted = tq.translate_stat_query("# 工艺词缀")
    assert "Crafted Modifiers" in en_crafted

    # Fixed-number templates keep literal digits distinct while placeholder templates match numeric values
    assert tq.translate_stat_query("1 个附加天赋为保镳") == "1 Added Passive Skill is Bodyguards"
    assert tq.translate_stat_query("2 个附加天赋为保镳") != "1 Added Passive Skill is Bodyguards"
    en_det_num = tq.translate_stat_query("受到【坚定】影响时 +150 护甲")
    assert "Armour" in en_det_num and "Determination" in en_det_num

    # Existing aliases still work and brackets are stripped cleanly
    assert tq.translate_stat_query("【火焰抗性】") == "+#% to Fire Resistance"


def test_cli_trade_stats_json_and_markdown_expose_better_source_and_polarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_entries = [
        {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
        {"id": "explicit.stat_3639275092", "text": "#% increased Attribute Requirements", "type": "explicit"},
        {"id": "pseudo.pseudo_temple_breach_room_1", "text": "Has Room: Anomaly Research Lab (Tier 1)", "type": "pseudo"},
        {"id": "explicit.stat_3742808908", "text": "+# to Armour while affected by Determination", "type": "explicit"},
    ]

    class _DummyClient:
        def __init__(self, proxy: str | None = None) -> None:
            pass

        def __enter__(self) -> _DummyClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stats(self, force_refresh: bool = False) -> list[dict[str, str]]:
            return sample_entries

    monkeypatch.setattr(ct, "TradeClient", _DummyClient)
    monkeypatch.setattr(ct, "load_trade_stats", lambda **kwargs: (sample_entries, "local_engine"))

    runner = CliRunner()

    r_neg = runner.invoke(main, ["trade", "stats", "#% increased Attribute Requirements", "--json"])
    assert r_neg.exit_code == 0, r_neg.output
    p_neg = json.loads(r_neg.stdout)
    assert p_neg["match"]["id"] == "explicit.stat_3639275092"
    assert p_neg["match"]["better"] == -1
    assert p_neg["match"]["betterSource"] == "apt"
    assert p_neg["suggestions"][0]["better"] == -1
    assert p_neg["suggestions"][0]["betterSource"] == "apt"

    r_neg_md = runner.invoke(main, ["trade", "stats", "#% increased Attribute Requirements"])
    assert r_neg_md.exit_code == 0, r_neg_md.output
    assert "↓ lower-raw (APT)" in r_neg_md.stdout
    assert "↑ (APT)" in r_neg_md.stdout

    r_pos = runner.invoke(main, ["trade", "stats", "受到【坚定】影响时 +# 护甲", "--json"])
    assert r_pos.exit_code == 0, r_pos.output
    p_pos = json.loads(r_pos.stdout)
    assert p_pos["match"]["id"] == "explicit.stat_3742808908"
    assert p_pos["match"]["better"] == 1
    assert p_pos["match"]["betterSource"] == "apt"
    assert any(
        s["id"] == "pseudo.pseudo_temple_breach_room_1"
        and s["better"] == 0
        and s["betterSource"] == "apt"
        for s in p_pos["suggestions"]
    )


def test_better_minus_one_preserves_min_and_weight_without_sign_inversion() -> None:
    """Upstream APT better == -1 is informational only: trade query builder must never invert min or weight."""
    assert tq.get_stat_polarity("explicit.stat_68410701") == -1
    assert tq.get_stat_polarity("explicit.stat_2255585376") == -1

    entries = [
        {
            "id": "explicit.stat_68410701",
            "text": "You take #% reduced Extra Damage from Critical Strikes",
            "type": "explicit",
        },
        {
            "id": "explicit.stat_2255585376",
            "text": "#% reduced Mana Cost of Skills",
            "type": "explicit",
        },
        {
            "id": "explicit.stat_3299347043",
            "text": "+# to maximum Life",
            "type": "explicit",
        },
    ]

    req_stat = tq.parse_stat_argument("You take #% reduced Extra Damage from Critical Strikes=30")
    req_mana = tq.parse_stat_argument("#% reduced Mana Cost of Skills=10")
    req_sum = tq.parse_sum_group_argument(
        "You take #% reduced Extra Damage from Critical Strikes*2|+# to maximum Life*1>=80"
    )
    plan = tq.plan_stats(
        [req_stat, req_mana],
        entries,
        slot="body_armour",
        weighted_groups=[req_sum],
    )
    payload = tq.build_query(plan=plan, category="armour.chest", rarity="rare")
    stat_groups = payload["query"]["stats"]

    # --stat group preserves exact positive min=30 and min=10 (no sign flip to negative / max)
    and_group = next(g for g in stat_groups if g["type"] == "and")
    crit_filter = next(f for f in and_group["filters"] if f["id"] == "explicit.stat_68410701")
    mana_filter = next(f for f in and_group["filters"] if f["id"] == "explicit.stat_2255585376")
    assert crit_filter["value"] == {"min": 30}
    assert mana_filter["value"] == {"min": 10}

    # --sum group preserves exact positive weight=2 and group min=80 (no sign inversion)
    weight_group = next(g for g in stat_groups if g["type"] == "weight")
    assert weight_group["value"] == {"min": 80}
    w_crit = next(f for f in weight_group["filters"] if f["id"] == "explicit.stat_68410701")
    assert w_crit["value"] == {"weight": 2}


def test_update_apt_data_handles_resolve_entries_deterministically(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("update_apt_data", SCRIPTS_DIR / "update_apt_data.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    data_root = tmp_path / "apt" / "renderer" / "public" / "data"
    (data_root / "en").mkdir(parents=True)
    (data_root / "cmn-Hant").mkdir(parents=True)
    (data_root / "item-drop.json").write_text("[]", encoding="utf-8")
    (data_root / "en" / "items.ndjson").write_text("", encoding="utf-8")
    (data_root / "cmn-Hant" / "items.ndjson").write_text("", encoding="utf-8")

    en_stats = [
        {
            "resolve": {"strat": "trivial-merge"},
            "stats": [
                {
                    "ref": "#% increased Attribute Requirements",
                    "better": -1,
                    "matchers": [{"string": "#% increased Attribute Requirements"}],
                    "trade": {"ids": {"explicit": ["explicit.stat_3639275092"]}},
                }
            ],
        },
        {
            "ref": "Has Room: Test",
            "better": 0,
            "matchers": [{"string": "Test"}],
            "trade": {"ids": {"pseudo": ["pseudo.test_room"]}, "option": True},
        },
    ]
    tw_stats = [
        {
            "ref": "+# to Armour while affected by Determination",
            "better": 1,
            "matchers": [{"string": "被堅定影響時 # 護甲"}],
            "trade": {"ids": {"explicit": ["explicit.stat_3742808908"]}},
        }
    ]
    (data_root / "en" / "stats.ndjson").write_text(
        "\n".join(json.dumps(x) for x in en_stats) + "\n", encoding="utf-8"
    )
    (data_root / "cmn-Hant" / "stats.ndjson").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in tw_stats) + "\n", encoding="utf-8"
    )

    out1, out2 = tmp_path / "out1", tmp_path / "out2"
    mod.generate_apt_data(tmp_path / "apt", out_dir=out1)
    mod.generate_apt_data(tmp_path / "apt", out_dir=out2)
    b1 = (out1 / "apt_trade_supplements.json.gz").read_bytes()
    b2 = (out2 / "apt_trade_supplements.json.gz").read_bytes()
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()
