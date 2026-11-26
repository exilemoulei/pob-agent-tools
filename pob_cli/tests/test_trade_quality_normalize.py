"""Offline unit tests and real-engine invariants for Layer 2 (`--normalize-quality` +20%Q normalization)."""

from __future__ import annotations

import base64
import json
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine
from test_cow_snapshot_sweep import _CowFakeWorker

from pob_cli import analyzer, cli_trade
from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import main
from pob_cli.daemon import rpc_handlers
from pob_cli.snapshot import history_of
from pob_cli.trade_client import TradeClient
from pob_cli.trade_eval import (
    Candidate,
    PobEval,
    eval_trade_items,
    format_report,
    report_json,
)
from pob_cli.trade_eval.evaluator import _prefilter_raw_items_for_eval
from pob_cli.trade_eval.scoring import score_candidate_surrogate
from pob_cli.trade_item import parse_item_text

MOCK_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_4052037485", "text": "#% increased Energy Shield", "type": "explicit"},
]


def _mock_trade_stats(*_a: Any, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
    return MOCK_STATS, "mock"


def _encode_xml(xml_str: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml_str.encode("utf-8"), 9)).decode("ascii")


def test_cli_normalize_quality_flag_wiring_and_unmodified_item_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    row = {
        "id": "chest_0q",
        "item": {
            "name": "Fate Shell",
            "typeLine": "Vaal Regalia",
            "baseType": "Vaal Regalia",
            "frameType": 2,
            "ilvl": 86,
            "identified": True,
            "properties": [{"name": "Energy Shield", "values": [["388", 1]]}],
            "explicitMods": ["+100 to maximum Life", "100% increased Energy Shield"],
        },
        "listing": {"fee": 2500, "price": {"type": "~b/o", "amount": 20, "currency": "chaos"}},
    }

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 1, "result": ["chest_0q"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [row]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    seen_params: list[dict[str, Any]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if action == "trade_eval_items":
            p = dict(params or {})
            seen_params.append(p)
            nq = bool(p.get("normalizeQuality"))
            return {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": 10000.0, "life": 4000.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "after": {
                            "primaryDpsKey": "CombinedDPS",
                            "primaryDps": 1000.0,
                            "totalEhp": 12000.0 if nq else 11000.0,
                            "life": 4100.0,
                        },
                        "diff": {},
                        "listedQuality": 0,
                        "evaluatedQuality": 20 if nq else 0,
                        "qualityNormalized": nq,
                    }
                    for entry in p.get("items") or []
                ],
            }
        raise AssertionError(f"unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)
    base_args = [
        "trade", "search", "--slot", "body_armour", "--stat", "maximum Life=80",
        "--no-compare", "--pob", "on", "--json", "--compact",
    ]

    # Default --normalize-quality auto -> active when PoB runs
    res_auto = CliRunner().invoke(main, base_args)
    assert res_auto.exit_code == 0, res_auto.output
    assert seen_params[-1]["normalizeQuality"] is True
    cand_auto = json.loads(res_auto.output)["candidates"][0]
    assert cand_auto["pob"]["listedQuality"] == 0
    assert cand_auto["pob"]["evaluatedQuality"] == 20
    assert cand_auto["pob"]["qualityNormalized"] is True
    assert "Quality: +20%" not in cand_auto["item_text"]  # original item_text stays untouched

    # --normalize-quality off -> disabled
    res_off = CliRunner().invoke(main, [*base_args, "--normalize-quality", "off"])
    assert res_off.exit_code == 0, res_off.output
    assert seen_params[-1]["normalizeQuality"] is False
    cand_off = json.loads(res_off.output)["candidates"][0]
    assert cand_off["pob"]["qualityNormalized"] is False
    assert cand_off["pob"]["evaluatedQuality"] == 0

    # --pob off -> never calls daemon even with --normalize-quality on
    before_calls = len(seen_params)
    res_nopob = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "body_armour", "--stat", "maximum Life=80", "--no-compare", "--pob", "off", "--normalize-quality", "on"],
    )
    assert res_nopob.exit_code == 0, res_nopob.output
    assert len(seen_params) == before_calls


def test_markdown_and_json_quality_tag_combines_with_bench_craft() -> None:
    text_a = "Rarity: RARE\nFate Shell\nVaal Regalia\nImplicits: 0\n+80 to maximum Life\n"
    text_b = "Rarity: RARE\nSoul Shroud\nVaal Regalia\nImplicits: 0\n+75 to maximum Life\n"
    text_c = "Rarity: RARE\nNative Shell\nVaal Regalia\nQuality: +20%\nImplicits: 0\n+80 to maximum Life\n"
    pob_norm_a = PobEval(
        dps_key="CombinedDPS", dps_before=1000.0, dps_after=1000.0,
        ehp_before=10000.0, ehp_after=12000.0, life_before=4000.0, life_after=4080.0,
        listed_quality=0, evaluated_quality=20, quality_normalized=True,
    )
    pob_norm_b = PobEval(
        dps_key="CombinedDPS", dps_before=1000.0, dps_after=1000.0,
        ehp_before=10000.0, ehp_after=11800.0, life_before=4000.0, life_after=4075.0,
        listed_quality=8, evaluated_quality=20, quality_normalized=True,
    )
    pob_plain = PobEval(
        dps_key="CombinedDPS", dps_before=1000.0, dps_after=1000.0,
        ehp_before=10000.0, ehp_after=12000.0, life_before=4000.0, life_after=4080.0,
        listed_quality=20, evaluated_quality=20, quality_normalized=False,
    )
    cand_a = Candidate(
        raw={}, item=parse_item_text(text_a), item_text=text_a, price_amount=15, price_currency="chaos",
        trade_mode="securable", gold_fee=2100, pob=pob_norm_a,
        bench_craft={
            "action": "open", "lines": ["{crafted}+55 to maximum Life"],
            "removed_lines": [], "cost": "4x Chaos Orb",
        },
    )
    cand_b = Candidate(
        raw={}, item=parse_item_text(text_b), item_text=text_b, price_amount=12, price_currency="chaos",
        trade_mode="securable", gold_fee=1900, pob=pob_norm_b,
    )
    cand_c = Candidate(
        raw={}, item=parse_item_text(text_c), item_text=text_c, price_amount=18, price_currency="chaos",
        trade_mode="securable", gold_fee=2300, pob=pob_plain,
    )
    candidates = [cand_a, cand_b, cand_c]
    md = format_report(build_label="b", slot="body_armour", current=None, candidates=candidates, league="Covens")
    assert "Fate Shell (Vaal Regalia) [+20%Q] [bench: {crafted}+55 to maximum Life; +4x Chaos Orb]" in md
    assert "PoB [+20%Q]:" in md

    # Mandatory row badge invariant: EVERY candidate row with qualityNormalized==True carries [+20%Q],
    # and rows with qualityNormalized==False never carry [+20%Q].
    table_data_rows = [ln for ln in md.splitlines() if ln.startswith("| ") and not ln.startswith("| # ")][0:3]
    assert len(table_data_rows) == 3
    for cand, row_line in zip(candidates, table_data_rows, strict=True):
        assert cand.pob is not None
        if cand.pob.quality_normalized:
            assert "[+20%Q]" in row_line
        else:
            assert "[+20%Q]" not in row_line

    js = report_json(candidates)
    assert js[0]["pob"]["listedQuality"] == 0
    assert js[0]["pob"]["evaluatedQuality"] == 20
    assert js[0]["pob"]["qualityNormalized"] is True
    assert js[2]["pob"]["qualityNormalized"] is False


def test_eval_trade_items_routes_trade_add_item_and_rpc_handler_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NormFakeWorker(_CowFakeWorker):
        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
            if method == "trade_add_item":
                self.calls.append((method, dict(params or {})))
                super().call("add_item", params, timeout)
                nq = bool((params or {}).get("normalizeQuality"))
                return {
                    "item": {"id": 99, "type": "Body Armour"},
                    "listedQuality": 0,
                    "evaluatedQuality": 20 if nq else 0,
                    "qualityNormalized": nq,
                }
            return super().call(method, params, timeout)

    raw_chest = "Rarity: RARE\nFate Shell\nVaal Regalia\nImplicits: 0\n+80 to maximum Life"
    w_cap = _NormFakeWorker(caps={"cowSnapshot": True, "directCalc": True, "tradeQualityNormalize": True})
    w_cap.slot_items["Body Armour"] = "Rarity: RARE\nOld Shell\nVaal Regalia"
    out = eval_trade_items(w_cap, slot_name="Body Armour", items=[{"id": "c1", "raw": raw_chest}], normalize_quality=True)
    assert out["results"][0]["ok"] is True
    assert out["results"][0]["qualityNormalized"] is True
    assert out["results"][0]["listedQuality"] == 0
    assert out["results"][0]["evaluatedQuality"] == 20
    trade_add_calls = [p for m, p in w_cap.calls if m == "trade_add_item"]
    assert len(trade_add_calls) == 1
    assert trade_add_calls[0]["normalizeQuality"] is True
    assert trade_add_calls[0]["rebuild"] is False

    # Worker without tradeQualityNormalize cap falls back to plain add_item
    w_legacy = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w_legacy.slot_items["Body Armour"] = "Rarity: RARE\nOld Shell\nVaal Regalia"
    out_legacy = eval_trade_items(w_legacy, slot_name="Body Armour", items=[{"id": "c1", "raw": raw_chest}], normalize_quality=True)
    assert out_legacy["results"][0]["ok"] is True
    assert not any(m == "trade_add_item" for m, _ in w_legacy.calls)
    assert any(m == "add_item" for m, _ in w_legacy.calls)

    # Daemon handler forwards normalizeQuality / normalize_quality
    from pob_cli import trade_eval

    seen: list[bool] = []
    monkeypatch.setattr(
        trade_eval,
        "eval_trade_items",
        lambda _w, **kw: seen.append(bool(kw.get("normalize_quality"))) or {"results": []},
    )
    rpc_handlers._h_trade_eval_items(SimpleNamespace(), {"slot": "Body Armour", "items": [], "normalizeQuality": True}, object())  # type: ignore[arg-type]
    rpc_handlers._h_trade_eval_items(SimpleNamespace(), {"slot": "Body Armour", "items": [], "normalize_quality": False}, object())  # type: ignore[arg-type]
    assert seen == [True, False]


def test_surrogate_prefilter_local_provenance_cap_gating_and_topk_parity() -> None:
    weights = {"weights": {"flat_es": 1.0, "inc_es": 1.0, "flat_phys": 1.0, "flat_life": 1.0}}
    chest_0q = parse_item_text(
        "Rarity: RARE\nShell A\nVaal Regalia\nEnergy Shield: 350\nImplicits: 0\n+60 to maximum Energy Shield\n80% increased Energy Shield"
    )
    chest_20q = parse_item_text(
        "Rarity: RARE\nShell B\nVaal Regalia\nQuality: +20%\nEnergy Shield: 420\nImplicits: 0\n+60 to maximum Energy Shield\n80% increased Energy Shield"
    )
    chest_corrupted_0q = parse_item_text(
        "Rarity: RARE\nShell C\nVaal Regalia\nEnergy Shield: 350\nImplicits: 0\n+60 to maximum Energy Shield\n80% increased Energy Shield\nCorrupted"
    )

    # With normalize_quality=True: eligible 0%Q ties 20%Q and beats corrupted 0%Q
    s0 = score_candidate_surrogate(chest_0q, weights, normalize_quality=True)
    s20 = score_candidate_surrogate(chest_20q, weights, normalize_quality=True)
    s_corr = score_candidate_surrogate(chest_corrupted_0q, weights, normalize_quality=True)
    assert s0 == pytest.approx(s20)
    assert s0 > s_corr

    # With normalize_quality=False: 20%Q strictly beats 0%Q
    assert score_candidate_surrogate(chest_20q, weights, normalize_quality=False) > score_candidate_surrogate(
        chest_0q, weights, normalize_quality=False
    )

    # Local vs global provenance: armour quality must NOT scale global attack phys on gloves!
    gloves_0q = parse_item_text(
        "Rarity: RARE\nGrip A\nSlink Gloves\nEvasion Rating: 240\nImplicits: 0\nAdds 10 to 20 Physical Damage to Attacks"
    )
    gloves_20q = parse_item_text(
        "Rarity: RARE\nGrip B\nSlink Gloves\nQuality: +20%\nEvasion Rating: 288\nImplicits: 0\nAdds 10 to 20 Physical Damage to Attacks"
    )
    assert score_candidate_surrogate(gloves_0q, weights, normalize_quality=False) == pytest.approx(15.0)
    assert score_candidate_surrogate(gloves_20q, weights, normalize_quality=False) == pytest.approx(15.0)
    assert score_candidate_surrogate(gloves_0q, weights, normalize_quality=True) == pytest.approx(15.0)

    # Weapon quality DOES scale local weapon flat phys (without "to attacks"/"to spells")
    wpn_20q = parse_item_text(
        "Rarity: RARE\nEdge A\nJewelled Foil\nQuality: +20%\nPhysical Damage: 50-100\nImplicits: 0\nAdds 10 to 20 Physical Damage"
    )
    assert score_candidate_surrogate(wpn_20q, weights, normalize_quality=False) == pytest.approx(18.0)

    # Top-k parity on mixed 0%Q high-tier + 20%Q lower-tier items under --surrogate-k
    class _GradFakeWorker(_CowFakeWorker):
        def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
            if method == "compute_grad_build":
                return weights
            if method == "trade_add_item":
                return super().call("add_item", params, timeout)
            return super().call(method, params, timeout)

    mixed_pool = [
        {
            "id": "low_20q_a",
            "raw": "Rarity: RARE\nLow20A\nVaal Regalia\nQuality: +20%\nEnergy Shield: 360\nImplicits: 0\n+10 to maximum Energy Shield\n+30 to maximum Life",
        },
        {
            "id": "high_0q_a",
            "raw": "Rarity: RARE\nHigh0A\nVaal Regalia\nEnergy Shield: 350\nImplicits: 0\n+36 to maximum Energy Shield",
        },
        {
            "id": "high_0q_b",
            "raw": "Rarity: RARE\nHigh0B\nVaal Regalia\nEnergy Shield: 355\nImplicits: 0\n+40 to maximum Energy Shield",
        },
        {
            "id": "mid_20q_b",
            "raw": "Rarity: RARE\nMid20B\nVaal Regalia\nQuality: +20%\nEnergy Shield: 370\nImplicits: 0\n+30 to maximum Energy Shield",
        },
    ]
    w_supported = _GradFakeWorker(
        caps={"cowSnapshot": True, "directCalc": True, "agentProbes": True, "tradeQualityNormalize": True}
    )
    w_supported.slot_items["Body Armour"] = "Rarity: RARE\nOld\nVaal Regalia"
    # Full normalized ordering selects high_0q_b (48.0) and high_0q_a (43.2) over low_20q_a (42.0) and mid_20q_b (36.0)
    full_norm_top2 = [
        entry["id"]
        for entry in sorted(
            mixed_pool,
            key=lambda e: -score_candidate_surrogate(parse_item_text(e["raw"]), weights, normalize_quality=True),
        )[:2]
    ]
    prefiltered_top2 = [
        entry["id"]
        for entry in _prefilter_raw_items_for_eval(w_supported, mixed_pool, prefilter_k=50, pob_limit=2, normalize_quality=True)
    ]
    assert prefiltered_top2 == full_norm_top2 == ["high_0q_b", "high_0q_a"]

    # Capability-gated fallback: worker WITHOUT tradeQualityNormalize must NOT assume normalization in prefilter!
    # Without normalization, low_20q_a scores 10*1.2 + 30 = 42.0 > high_0q_a (36.0), so pob_limit=1 picks low_20q_a.
    w_unsupported = _GradFakeWorker(caps={"cowSnapshot": True, "directCalc": True, "agentProbes": True})
    w_unsupported.slot_items["Body Armour"] = "Rarity: RARE\nOld\nVaal Regalia"
    res_unsup = eval_trade_items(
        w_unsupported,
        slot_name="Body Armour",
        items=mixed_pool[:2],
        prefilter_k=50,
        pob_limit=1,
        normalize_quality=True,
    )
    assert [r["id"] for r in res_unsup["results"]] == ["low_20q_a"]


def test_real_engine_quality_normalization_invariants() -> None:
    """Real-engine proof: lossless 2-step +20%Q normalization preserves Sacred Orb BasePercentile and guards ineligible items."""
    eng = find_runnable_engine()
    if eng is None:
        pytest.skip("No runnable PoB engine found")

    build_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Duelist" ascendClassName="Slayer" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="4" ascendClassId="1" nodes="50986"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Heavy Strike" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""
    # High-percentile Vaal Regalia (100% inc ES): listed at 0%Q vs native +20%Q.
    # Back-solving BasePercentile at 0%Q then raising quality to 20% must match native +20%Q identically!
    chest_0q = (
        "Rarity: RARE\nSoul Shroud\nVaal Regalia\n"
        "Energy Shield: 388\nItem Level: 86\nImplicits: 0\n100% increased Energy Shield"
    )
    chest_20q = (
        "Rarity: RARE\nSoul Shroud\nVaal Regalia\n"
        "Quality: +20%\nEnergy Shield: 466\nItem Level: 86\nImplicits: 0\n100% increased Energy Shield"
    )
    chest_split_0q = chest_0q + "\nSplit"
    chest_corrupted_0q = chest_0q + "\nCorrupted"
    chest_mirrored_0q = chest_0q + "\nMirrored"
    chest_unmod_0q = chest_0q + "\nUnmodifiable"

    weapon_0q = (
        "Rarity: RARE\nDoom Edge\nJewelled Foil\n"
        "Item Level: 86\nImplicits: 0\n150% increased Physical Damage\nAdds 20 to 40 Physical Damage"
    )
    weapon_20q = (
        "Rarity: RARE\nDoom Edge\nJewelled Foil\n"
        "Quality: +20%\nItem Level: 86\nImplicits: 0\n150% increased Physical Damage\nAdds 20 to 40 Physical Damage"
    )
    weapon_28q = (
        "Rarity: RARE\nDoom Edge\nJewelled Foil\n"
        "Quality: +28%\nItem Level: 86\nImplicits: 0\n150% increased Physical Damage\nAdds 20 to 40 Physical Damage"
    )
    ring_0q = "Rarity: RARE\nPlain Loop\nCoral Ring\nItem Level: 84\nImplicits: 0\n+70 to maximum Life"

    with tempfile.TemporaryDirectory() as td:
        w = BridgeWorker(slot_name="test_qnorm", pob_dir=eng, runtime_dir=Path(td))
        try:
            w.start(restore_code="")
            if not w.caps.get("tradeQualityNormalize"):
                pytest.skip("Engine does not advertise tradeQualityNormalize capability")
            w.call("import_code", {"code": _encode_xml(build_xml), "mode": "replace"})

            # Capture baseline slot rev, summary stats, and items to prove 0-residue rollback around wipeGlobalCache()
            rev_before = history_of(w).rev
            sum_before = analyzer.extract_summary(w)
            items_before = w.call("list_items", {})

            # 1. Body Armour: 0%Q normalized == native +20%Q (Sacred Orb BasePercentile preserved!)
            chest_items = [
                {"id": "c0", "raw": chest_0q},
                {"id": "c20", "raw": chest_20q},
                {"id": "csplit", "raw": chest_split_0q},
                {"id": "ccorr", "raw": chest_corrupted_0q},
                {"id": "cmir", "raw": chest_mirrored_0q},
                {"id": "cunmod", "raw": chest_unmod_0q},
            ]
            res_norm = {
                r["id"]: r
                for r in eval_trade_items(w, slot_name="Body Armour", items=chest_items, normalize_quality=True)["results"]
            }
            res_raw = {
                r["id"]: r
                for r in eval_trade_items(w, slot_name="Body Armour", items=chest_items, normalize_quality=False)["results"]
            }

            assert res_norm["c0"]["qualityNormalized"] is True
            assert res_norm["c0"]["listedQuality"] == 0 and res_norm["c0"]["evaluatedQuality"] == 20
            assert res_norm["c20"]["qualityNormalized"] is False
            assert res_norm["c0"]["after"]["totalEhp"] == pytest.approx(res_norm["c20"]["after"]["totalEhp"], rel=1e-4)
            assert res_norm["c0"]["after"]["totalEhp"] > res_raw["c0"]["after"]["totalEhp"]

            # Split 0%Q IS eligible and normalized
            assert res_norm["csplit"]["qualityNormalized"] is True
            assert res_norm["csplit"]["after"]["totalEhp"] == pytest.approx(res_norm["c20"]["after"]["totalEhp"], rel=1e-4)

            # Corrupted, Mirrored, Unmodifiable remain untouched at 0%Q
            for key in ("ccorr", "cmir", "cunmod"):
                assert res_norm[key]["qualityNormalized"] is False
                assert res_norm[key]["evaluatedQuality"] == 0
                assert res_norm[key]["after"]["totalEhp"] == pytest.approx(res_raw[key]["after"]["totalEhp"], rel=1e-4)

            # 2. Weapon: 0%Q normalized == native +20%Q (~1.20x DPS vs 0%Q); 28%Q over-quality untouched!
            wpn_items = [
                {"id": "w0", "raw": weapon_0q},
                {"id": "w20", "raw": weapon_20q},
                {"id": "w28", "raw": weapon_28q},
            ]
            w_norm = {
                r["id"]: r
                for r in eval_trade_items(w, slot_name="Weapon 1", items=wpn_items, normalize_quality=True)["results"]
            }
            w_raw = {
                r["id"]: r
                for r in eval_trade_items(w, slot_name="Weapon 1", items=wpn_items, normalize_quality=False)["results"]
            }

            assert w_norm["w0"]["qualityNormalized"] is True
            assert w_norm["w0"]["listedQuality"] == 0 and w_norm["w0"]["evaluatedQuality"] == 20
            assert w_norm["w0"]["after"]["primaryDps"] == pytest.approx(w_norm["w20"]["after"]["primaryDps"], rel=1e-4)
            assert w_norm["w0"]["after"]["primaryDps"] / w_raw["w0"]["after"]["primaryDps"] == pytest.approx(1.20, rel=0.02)

            assert w_norm["w28"]["qualityNormalized"] is False
            assert w_norm["w28"]["listedQuality"] == 28 and w_norm["w28"]["evaluatedQuality"] == 28
            assert w_norm["w28"]["after"]["primaryDps"] == pytest.approx(w_raw["w28"]["after"]["primaryDps"], rel=1e-4)
            assert w_norm["w28"]["after"]["primaryDps"] > w_norm["w20"]["after"]["primaryDps"]

            # 3. Ring: untouched (never guess Catalysts)
            r_norm = eval_trade_items(w, slot_name="Ring 1", items=[{"id": "r0", "raw": ring_0q}], normalize_quality=True)["results"][0]
            assert r_norm["qualityNormalized"] is False
            assert r_norm["listedQuality"] is None and r_norm["evaluatedQuality"] is None

            # 4. Zero-residue rollback around wipeGlobalCache(): rev, summary stats, and items are bit-for-bit equal
            rev_after = history_of(w).rev
            sum_after = analyzer.extract_summary(w)
            items_after = w.call("list_items", {})
            assert rev_after == rev_before
            assert items_after["items"] == items_before["items"]
            assert items_after["slots"] == items_before["slots"]
            assert {k: v for k, v in sum_after["buildInfo"].items() if k != "rev"} == {
                k: v for k, v in sum_before["buildInfo"].items() if k != "rev"
            }
            for stat_key in ("CombinedDPS", "FullDPS"):
                assert sum_after["offence"].get(stat_key) == sum_before["offence"].get(stat_key)
            for def_key in ("TotalEHP", "Life", "EnergyShield"):
                assert sum_after["defence"].get(def_key) == sum_before["defence"].get(def_key)
        finally:
            w.stop()
