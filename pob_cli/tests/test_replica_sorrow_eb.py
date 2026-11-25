"""Tests for Replica Sorrow of the Divine (Eldritch Battery flask) dewater notes and Mana sustain audit."""

from __future__ import annotations

import copy
import threading
from typing import Any

import pytest

from pob_cli import analyzer
from pob_cli.analyzer.dewater_auto import infer_auto_keep_and_flask_notes
from pob_cli.analyzer.eb_sustain import collect_mana_sustain_issues, find_equipped_eb_flask_slots
from pob_cli.formatters import format_diagnose_markdown


class _EbFlaskWorker:
    """Stateful fake BridgeWorker modeling Replica Sorrow of the Divine (`Eldritch Battery during Effect`)."""

    def __init__(
        self,
        *,
        flask_slot: str = "Flask 2",
        flask_active: bool = True,
        flask_item: dict[str, Any] | None = None,
        es: float = 1200.0,
        mana_unreserved: float = 35.0,
        mana_cost: float = 95.0,
        es_cost: float = 0.0,
        mana_regen: float = 4.0,
    ) -> None:
        self.lock = threading.RLock()
        self.slot_name = "main"
        self.last_good_code = "EB_INIT"
        self._flask_slot = flask_slot
        self.flasks: dict[str, bool] = {flask_slot: flask_active}
        self._flask_item = copy.deepcopy(
            flask_item
            or {
                "id": 10,
                "name": "Replica Sorrow of the Divine, Sulphur Flask",
                "title": "Replica Sorrow of the Divine",
                "titleZh": "神圣哀悼【仿品】",
                "rarity": "UNIQUE",
                "type": "Flask",
                "baseName": "Sulphur Flask",
                "raw": (
                    "Replica Sorrow of the Divine\n"
                    "Sulphur Flask\n"
                    "Creates Consecrated Ground on Use\n"
                    "Life Recovery from Flasks also applies to Energy Shield during Effect\n"
                    "Eldritch Battery during Effect"
                ),
            }
        )
        self.config: dict[str, Any] = {"enemyIsBoss": "None"}
        self._es = es
        self._mana_unres = mana_unreserved
        self._mana_cost = mana_cost
        self._es_cost = es_cost
        self._mana_regen = mana_regen
        self._saved: dict[str, dict[str, Any]] = {"EB_INIT": self._snap()}
        self._seq = 0

    def _snap(self) -> dict[str, Any]:
        return copy.deepcopy({"config": self.config, "flasks": self.flasks})

    def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
        return {"nodes": {}}

    def _stats(self) -> dict[str, Any]:
        eb_on = bool(self.flasks.get(self._flask_slot))
        dps = 500_000.0 * (0.9 if self.config.get("enemyIsBoss") == "Pinnacle" else 1.0)
        if eb_on:
            dps += 20_000.0
        st: dict[str, Any] = {
            "CombinedDPS": dps,
            "TotalEHP": 80_000.0,
            "Life": 4_200.0,
            "LifeUnreserved": 4_200.0,
            "EnergyShield": self._es,
            "Mana": 900.0,
            "ManaUnreserved": self._mana_unres,
            "ManaCost": self._mana_cost,
            "ESCost": self._es_cost,
            "ManaRegenRecovery": self._mana_regen,
            "Speed": 3.5,
            "PhysicalMaximumHitTaken": 18_000.0,
            "FireMaximumHitTaken": 35_000.0,
            "ColdMaximumHitTaken": 35_000.0,
            "LightningMaximumHitTaken": 35_000.0,
            "ChaosMaximumHitTaken": 22_000.0,
            "FireResist": 75.0,
            "FireResistOverCap": 30.0,
            "ColdResist": 75.0,
            "ColdResistOverCap": 30.0,
            "LightningResist": 75.0,
            "LightningResistOverCap": 30.0,
            "ChaosResist": 35.0,
        }
        if eb_on:
            st["EnergyShieldProtectsMana"] = True
        return st

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = params or {}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return {"level": 95, "className": "Templar", "ascendClassName": "Inquisitor"}
        if method == "get_build_header":
            return {}
        if method == "list_specs":
            return {"activeSpec": 1, "specs": [{"index": 1, "title": "Main"}]}
        if method == "list_config":
            return {
                "sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}],
                "customMods": [],
            }
        if method == "set_config":
            self.config[p["var"]] = p["value"]
            return {"ok": True}
        if method == "reset_config":
            self.config[p["var"]] = False
            return {"ok": True}
        if method == "set_custom_mods":
            return {"ok": True}
        if method == "list_items":
            return {
                "slots": [
                    {
                        "name": self._flask_slot,
                        "isFlask": True,
                        "active": self.flasks[self._flask_slot],
                        "selItemId": self._flask_item["id"],
                    }
                ],
                "items": [copy.deepcopy(self._flask_item)],
            }
        if method == "item_raw":
            return {"raw": str(self._flask_item.get("raw") or "")}
        if method == "set_slot_active":
            self.flasks[p["slotName"]] = bool(p["active"])
            return {"ok": True}
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "slot": "Body Armour",
                        "gems": [{"name": "Penance Brand of Dissipation", "level": 20, "quality": 20, "enabled": True, "support": False}],
                    }
                ],
            }
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_tree_state":
            return {"treeVersion": "3_30", "allocatedNodes": [], "overrides": {}, "dynamicNodes": [], "jewelSockets": []}
        if method == "export_code":
            snap = self._snap()
            for k, v in self._saved.items():
                if v == snap:
                    return {"code": k}
            self._seq += 1
            code = f"EB_SNAP_{self._seq}"
            self._saved[code] = snap
            return {"code": code}
        if method == "import_code":
            snap = copy.deepcopy(self._saved[p["code"]])
            self.config = dict(snap["config"])
            self.flasks = dict(snap["flasks"])
            return {"ok": True}
        if method == "list_warnings":
            return {"warnings": []}
        raise AssertionError(f"Unexpected RPC {method}({p})")


@pytest.mark.parametrize(
    "item_meta",
    [
        {"id": 1, "title": "Replica Sorrow of the Divine"},
        {"id": 1, "titleZh": "神圣哀悼【仿品】"},
        {"id": 1, "titleZh": "仿品·神圣哀悼"},
        {"id": 1, "titleZh": "贗品．神聖哀悼"},
        {"id": 1, "title": "Sulphur Flask", "raw": "Sulphur Flask\nEldritch Battery during Effect"},
        # The zh-CN and zh-TW wordings of the line, on an item text that carries no title.
        {"id": 1, "raw": "硫磺药剂\n生效期间获得异能魔力"},
        {"id": 1, "raw": "硫磺藥劑\n效果持續時間內獲得異能魔力"},
    ],
)
def test_infer_flask_notes_recognizes_replica_sorrow_variants(item_meta: dict[str, Any]) -> None:
    items_res = {
        "slots": [{"name": "Flask 3", "isFlask": True, "active": True, "selItemId": 1}],
        "items": [item_meta],
    }
    assert find_equipped_eb_flask_slots(items_res)[0][0] == "Flask 3"
    _, flask_notes = infer_auto_keep_and_flask_notes(
        stats={},
        skills_res={"groups": []},
        items_res=items_res,
    )
    assert "Flask 3" in flask_notes
    assert "Eldritch Battery during Effect" in flask_notes["Flask 3"]
    assert "--keep 'Flask 3'" in flask_notes["Flask 3"]


def test_active_replica_sorrow_dewater_emits_note_and_no_high_mana_false_positive() -> None:
    w = _EbFlaskWorker(flask_slot="Flask 2", flask_active=True, es=1200.0, mana_unreserved=35.0, mana_cost=95.0)
    aud = analyzer.audit_build(w, attribution=False)  # type: ignore[arg-type]

    stripped_flasks = aud["inflationStripped"]["flasks"]
    assert len(stripped_flasks) == 1
    assert "Eldritch Battery during Effect" in stripped_flasks[0]
    assert "--keep 'Flask 2'" in stripped_flasks[0]

    sustain = [g for g in aud["healthIssues"] if g["category"] == "Sustain"]
    assert all(g["severity"] != "HIGH" for g in sustain), f"Unexpected HIGH Sustain: {sustain}"
    assert len(sustain) == 1
    assert sustain[0]["severity"] == "LOW"
    assert "Eldritch Battery during Effect" in sustain[0]["message"]
    assert "--keep 'Flask 2'" in sustain[0]["message"]

    diag = analyzer.diagnose_build(w, no_rank=True)  # type: ignore[arg-type]
    diag_sustain = [g for g in diag["healthGaps"] if g["category"] == "Sustain"]
    assert all(g["severity"] != "HIGH" for g in diag_sustain)
    md = format_diagnose_markdown(diag)
    assert "**Flask Dewater Notes**:" in md
    assert "Eldritch Battery during Effect" in md

    # Keeping Flask 2 keeps EB active and produces zero Sustain warnings
    aud_kept = analyzer.audit_build(w, keep=["Flask 2"], attribution=False)  # type: ignore[arg-type]
    assert [g for g in aud_kept["healthIssues"] if g["category"] == "Sustain"] == []


def test_a_flask_that_is_on_is_never_called_off() -> None:
    # A bridge that does not report Eldritch Battery in the stats: the flask is on, so the mana
    # deficit is the plain one, not a note about a flask that is off.
    items_res = {
        "slots": [{"name": "Flask 2", "isFlask": True, "active": True, "selItemId": 1}],
        "items": [{"id": 1, "title": "Replica Sorrow of the Divine"}],
    }
    stats = {"ManaCost": 95.0, "ManaUnreserved": 35.0, "EnergyShield": 1200.0, "Life": 3000.0}
    issues = collect_mana_sustain_issues(stats, None, items_res=items_res)
    assert [(g["severity"], g["message"]) for g in issues] == [
        ("HIGH", "Skill Mana Cost (95) exceeds Unreserved Mana (35)")
    ]
    items_res["slots"][0]["active"] = False
    (off,) = collect_mana_sustain_issues(stats, None, items_res=items_res)
    assert off["severity"] == "LOW" and "Flask 2 (Replica Sorrow of the Divine) is off" in off["message"]


def test_inactive_replica_sorrow_downgrades_mana_deficit_when_es_covers_cost() -> None:
    # Models poe.ninja / profile import where flasks start inactive (`active=False`)
    w_inactive = _EbFlaskWorker(
        flask_slot="Flask 4",
        flask_active=False,
        es=1200.0,
        mana_unreserved=35.0,
        mana_cost=95.0,
        mana_regen=-2.0,
    )
    aud = analyzer.audit_build(w_inactive, attribution=False)  # type: ignore[arg-type]
    sustain = [g for g in aud["healthIssues"] if g["category"] == "Sustain"]
    assert len(sustain) == 1
    assert sustain[0]["severity"] == "LOW"
    assert "Flask 4" in sustain[0]["message"]
    assert "1200 ES + 35 Unreserved Mana = 1235 >= 95" in sustain[0]["message"]
    # The flask is off in the build: --keep keeps nothing here, turning it on is what models it.
    assert "`pob item flask 4 on --apply`" in sustain[0]["message"] and "--keep" not in sustain[0]["message"]

    # A flask that is merely equipped silences nothing: with mana to cast once and negative regen,
    # the regen warning stands, exactly as it does without the flask.
    w_regen = _EbFlaskWorker(
        flask_slot="Flask 4",
        flask_active=False,
        es=1200.0,
        mana_unreserved=200.0,
        mana_cost=95.0,
        mana_regen=-2.0,
    )
    regen = [g for g in analyzer.audit_build(w_regen, attribution=False)["healthIssues"] if g["category"] == "Sustain"]  # type: ignore[arg-type]
    assert [(g["severity"], g["message"][:26]) for g in regen] == [("MEDIUM", "Net Mana Regen is negative")]

    # When even ES + Unreserved Mana < ManaCost, HIGH Sustain MUST still fire
    w_broken = _EbFlaskWorker(
        flask_slot="Flask 4",
        flask_active=False,
        es=20.0,
        mana_unreserved=35.0,
        mana_cost=95.0,
    )
    aud_broken = analyzer.audit_build(w_broken, attribution=False)  # type: ignore[arg-type]
    broken_sustain = [g for g in aud_broken["healthIssues"] if g["category"] == "Sustain" and g["severity"] == "HIGH"]
    assert len(broken_sustain) == 1
    assert "20 ES + 35 Unreserved Mana = 55" in broken_sustain[0]["message"]
