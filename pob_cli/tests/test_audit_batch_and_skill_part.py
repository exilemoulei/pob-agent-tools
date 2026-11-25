"""Tests for strict `pob batch` schema validation, aliases, and `skillPart` preservation."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli import mutators
from pob_cli.bridge_client import BridgeError, BridgeTimeoutError, BridgeWorker
from pob_cli.mutators.batch_ops import _BATCH_OP_HANDLERS, _execute_batch_ops
from pob_cli.mutators.batch_validate import _OP_VALID_KEYS, validate_batch_op
from pob_cli.mutators.common import _BatchCache
from pob_cli.mutators.skills import _try_call_ext
from pob_cli.mutators.skills_resolve import _gem_name_cautions, _normalize_gem_spec
from pob_cli.updater import is_valid_engine_dir


def _find_engine() -> Path | None:
    eng = find_runnable_engine()
    if eng is not None:
        return eng
    rt_env = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if rt_env:
        cand = Path(rt_env) / "engine"
        if (cand / "PoE1" / "Data" / "Gems.lua").is_file() and is_valid_engine_dir(cand):
            return cand
    return None


_ENGINE_DIR = _find_engine()


class _FakeSkillExtWorker:
    """In-memory BridgeWorker double implementing `get_group_gem_states` / `apply_group_gem_states`."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {"skillCalcsExt": True}
        self.last_good_code: str | None = "BASE_CODE"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.groups: list[dict[str, Any]] = [
            {
                "index": 1,
                "label": "Main",
                "enabled": True,
                "includeInFullDPS": True,
                "mainActiveSkill": 2,
                "mainActiveSkillCalcs": 2,
                "mainActiveGemId": "Metadata/Items/Gems/SkillGemBodyswapOfSacrifice",
                "mainActiveNameSpec": "Bodyswap of Sacrifice",
                "mainActiveSkillId": "BodyswapOfSacrifice",
                "gems": [
                    {
                        "index": 1,
                        "name": "Desecrate",
                        "nameSpec": "Desecrate",
                        "gemId": "Metadata/Items/Gems/SkillGemDesecrate",
                        "skillId": "Desecrate",
                        "level": 20,
                        "quality": 20,
                        "enabled": True,
                        "count": 1,
                    },
                    {
                        "index": 2,
                        "name": "Bodyswap of Sacrifice",
                        "nameSpec": "Bodyswap of Sacrifice",
                        "gemId": "Metadata/Items/Gems/SkillGemBodyswapOfSacrifice",
                        "skillId": "BodyswapOfSacrifice",
                        "level": 20,
                        "quality": 20,
                        "enabled": True,
                        "count": 1,
                        "skillPart": 2,
                        "skillPartCalcs": 2,
                        "skillMinion": "RaisedSpectre",
                        "skillMinionCalcs": "RaisedSpectre",
                        "statSet": {"BodyswapOfSacrifice": 1},
                    },
                ],
            }
        ]
        self.db_items: list[dict[str, Any]] = [
            {
                "id": 1,
                "name": "Mageblood",
                "title": "Mageblood",
                "type": "Belt",
                "rarity": "UNIQUE",
                "raw": "Rarity: UNIQUE\nMageblood\nHeavy Belt",
            },
            {
                "id": 2,
                "name": "Headhunter",
                "title": "Headhunter",
                "type": "Belt",
                "rarity": "UNIQUE",
                "raw": "Rarity: UNIQUE\nHeadhunter\nLeather Belt",
            },
        ]

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        raise_on_error: bool = True,
        **_: Any,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for method, params in calls:
            try:
                out.append(self.call(method, params))
            except Exception as exc:
                if raise_on_error:
                    raise
                out.append({"_error": str(exc)})
        return out

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))
        if method == "export_code":
            return {"code": "SNAP_CODE"}
        if method == "import_code":
            return {"ok": True}
        if method in ("get_build_info", "get_build_header"):
            return {"level": 95, "className": "Witch", "ascendClassName": "Necromancer"}
        if method == "get_stats":
            return {"CombinedDPS": 250_000.0, "Life": 5000.0, "TotalEHP": 80_000.0}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "list_items":
            return {"slots": [{"name": "Belt"}, {"name": "Body Armour"}], "items": []}
        if method == "item_db":
            q = str(p.get("query") or "").lower()
            matched = [it for it in self.db_items if q in it["name"].lower()]
            return {"items": matched, "total": len(matched)}
        if method == "add_item":
            return {"ok": True, "item": {"name": "Equipped", "type": "Belt", "primarySlot": p.get("slotName")}}
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": g["index"],
                        "label": g.get("label", ""),
                        "enabled": g.get("enabled", True),
                        "includeInFullDPS": g.get("includeInFullDPS", False),
                        "gems": [dict(gm) for gm in g["gems"]],
                    }
                    for g in self.groups
                ],
                "skillSets": [],
            }
        if method == "gem_search":
            q = str(p.get("query") or "").strip()
            slug = "".join(ch for ch in q.title() if ch.isalnum())
            return {"gems": [{"name": q, "gemId": f"Metadata/Items/Gems/SkillGem{slug}", "skillId": slug}]}
        if method == "add_group":
            new_idx = len(self.groups) + 1
            self.groups.append(
                {
                    "index": new_idx,
                    "label": p.get("label", ""),
                    "enabled": True,
                    "includeInFullDPS": False,
                    "mainActiveSkill": 1,
                    "mainActiveSkillCalcs": 1,
                    "gems": [],
                }
            )
            return {"index": new_idx}
        if method == "set_group":
            grp = next(g for g in self.groups if g["index"] == int(p["index"]))
            for k in ("label", "enabled", "includeInFullDPS", "slot", "mainActiveSkill"):
                if k in p:
                    grp[k] = p[k]
            return {"ok": True}
        if method == "delete_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            grp["gems"].pop(int(p["index"]) - 1)
            for i, gm in enumerate(grp["gems"], start=1):
                gm["index"] = i
            if not grp["gems"]:
                grp["mainActiveSkill"] = 1
                grp["mainActiveSkillCalcs"] = 1
            return {"ok": True}
        if method == "add_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            idx = len(grp["gems"]) + 1
            gem_obj = {
                "index": idx,
                "name": p.get("nameSpec") or "Gem",
                "nameSpec": p.get("nameSpec") or "Gem",
                "gemId": p.get("gemId"),
                "level": p.get("level", 20),
                "quality": p.get("quality", 20),
                "enabled": p.get("enabled", True),
                "count": p.get("count", 1),
            }
            grp["gems"].append(gem_obj)
            return {"gem": dict(gem_obj)}
        if method == "set_gem":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            gm = grp["gems"][int(p["index"]) - 1]
            if "nameSpec" in p:
                gm["name"] = p["nameSpec"]
                gm["nameSpec"] = p["nameSpec"]
            for k in ("gemId", "level", "quality", "enabled", "count", "enableGlobal1", "enableGlobal2"):
                if k in p:
                    gm[k] = p[k]
            return {"gem": dict(gm)}
        if method == "get_group_gem_states":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            return {
                "group": grp["index"],
                "mainActiveSkill": grp.get("mainActiveSkill", 1),
                "mainActiveSkillCalcs": grp.get("mainActiveSkillCalcs", 1),
                "mainActiveGemId": grp.get("mainActiveGemId"),
                "mainActiveNameSpec": grp.get("mainActiveNameSpec"),
                "mainActiveSkillId": grp.get("mainActiveSkillId"),
                "gems": [dict(gm) for gm in grp["gems"]],
            }
        if method == "apply_group_gem_states":
            grp = next(g for g in self.groups if g["index"] == int(p["group"]))
            for entry in p.get("gemStates") or []:
                gm = grp["gems"][int(entry["index"]) - 1]
                for k, v in entry.items():
                    if k != "index":
                        gm[k] = v
            if "mainActiveSkill" in p:
                grp["mainActiveSkill"] = p["mainActiveSkill"]
            if "mainActiveSkillCalcs" in p:
                grp["mainActiveSkillCalcs"] = p["mainActiveSkillCalcs"]
            return {"ok": True, "group": grp["index"]}
        if method == "set_build_field":
            return {"ok": True}
        raise AssertionError(f"Unexpected RPC '{method}' with params {p}")


# ---------------------------------------------------------------------------
# 1. Batch schema validation & ergonomic aliases
# ---------------------------------------------------------------------------


def test_every_batch_handler_is_covered_by_schema_table() -> None:
    assert set(_BATCH_OP_HANDLERS.keys()) == set(_OP_VALID_KEYS.keys())


def test_config_unknown_top_level_keys_give_actionable_wrap_in_set_hint() -> None:
    w = _FakeSkillExtWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as exc1:
        _execute_batch_ops(w, [{"op": "config", "useEnduranceCharges": True}], cache)  # type: ignore[arg-type]
    assert str(exc1.value) == (
        "Batch step #1 (config): unknown top-level key(s) ['useEnduranceCharges']; "
        'wrap PoB config variables inside "set": {"useEnduranceCharges": true} '
        "(valid top-level keys: set, reset, assignments, customMods, appendCustomMods)."
    )

    with pytest.raises(ValueError) as exc2:
        _execute_batch_ops(
            w,  # type: ignore[arg-type]
            [{"op": "config", "custom_mods": "+50 to maximum Life", "enemyIsBoss": "Pinnacle"}],
            cache,
        )
    msg2 = str(exc2.value)
    assert "Batch step #1 (config): unknown top-level key(s) ['enemyIsBoss'];" in msg2
    assert '"set": {"enemyIsBoss": "Pinnacle"}' in msg2


def test_config_var_value_form_is_pointed_at_the_one_variable_it_names() -> None:
    with pytest.raises(ValueError) as caught:
        validate_batch_op(1, "config", {"op": "config", "var": "enemyIsBoss", "value": "Pinnacle"})
    msg = str(caught.value)
    assert '"set": {"enemyIsBoss": "Pinnacle"}' in msg
    assert '"var"' not in msg.split('"set":')[1]  # not offered as a config variable called "var"


def test_a_remark_on_an_op_and_a_gem_as_pob_reports_it_are_accepted() -> None:
    for remark in ("why", "desc", "description", "reason", "_todo"):
        validate_batch_op(1, "set_level", {"op": "set_level", "level": 95, remark: "leveling breakpoint"})
    with pytest.raises(ValueError, match="unknown key"):
        validate_batch_op(1, "set_level", {"op": "set_level", "level": 95, "remark": "not a remark name"})

    # One gem exactly as `pob skills --json` prints it (the bridge's gem_summary fields).
    reported = {
        "index": 2, "nameSpec": "Added Cold Damage", "name": "Added Cold Damage", "nameZh": "附加冰霜伤害",
        "gemId": "Metadata/Items/Gems/SupportGemAddedColdDamage", "skillId": "SupportAddedColdDamage",
        "level": 20, "quality": 20, "enabled": True, "enableGlobal1": True, "enableGlobal2": False, "count": 1,
        "errMsg": None, "color": 2, "support": True, "hasGlobalEffect": False, "naturalMaxLevel": 20,
        "reqLevel": 70, "matchesSocket": True, "fromItem": False, "fromNode": False,
        "tooltipDps": None,  # `pob skills --group N --json` adds this one
    }  # fmt: skip
    spec = _normalize_gem_spec(reported)
    assert (spec["name"], spec["level"], spec["quality"]) == ("Added Cold Damage", 20, 20)
    assert (spec["enableGlobal1"], spec["enableGlobal2"]) == (True, False)

    # A key nothing reports and nothing applies is still a typo.
    for stray in ("qualityId", "variant", "lvl"):
        with pytest.raises(ValueError, match="Unknown key"):
            _normalize_gem_spec({"name": "Arc", stray: 1})


def test_a_gems_own_allow_legacy_lets_that_gem_through_and_no_other() -> None:
    w = _FakeSkillExtWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]
    for op in ("add_group", "replace_group"):
        base: dict[str, Any] = {"op": op, **({"group": 1} if op == "replace_group" else {})}
        with pytest.raises(Exception, match="POE1-PATCH-BLOCK"):
            _execute_batch_ops(w, [{**base, "gems": [{"name": "Anomalous Arc"}]}], cache)  # type: ignore[arg-type]
        with pytest.raises(Exception, match="POE1-PATCH-BLOCK"):
            _execute_batch_ops(  # type: ignore[arg-type]
                w, [{**base, "gems": [{"name": "Anomalous Arc", "allowLegacy": True}, "Divergent Arc"]}], cache
            )
    assert _gem_name_cautions([{"name": "Anomalous Arc"}], True)  # allowed: a warning, not a block
    assert _gem_name_cautions([{"name": "Anomalous Arc", "allowLegacy": True}, "Arc"], False)
    assert _gem_name_cautions(["Arc"], False) == []


def test_global_effect_flags_named_in_a_gem_spec_are_applied_not_overwritten() -> None:
    w = _FakeSkillExtWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]
    ops = [
        {
            "op": "add_group",
            "gems": [
                {"name": "Vaal Righteous Fire", "enableGlobal1": True, "enableGlobal2": False},
                {"name": "Vaal Discipline"},
                {"name": "Arc", "enableGlobal1": False},
            ],
        }
    ]
    _execute_batch_ops(w, ops, cache)  # type: ignore[arg-type]
    flag_calls = [p for m, p in w.calls if m == "set_gem" and ("enableGlobal1" in p or "enableGlobal2" in p)]
    by_index = {p["index"]: {k: p[k] for k in ("enableGlobal1", "enableGlobal2") if k in p} for p in flag_calls}
    assert by_index[1] == {"enableGlobal1": True, "enableGlobal2": False}  # as asked, not the Vaal default
    assert by_index[2] == {"enableGlobal1": False, "enableGlobal2": True}  # the Vaal default
    assert by_index[3] == {"enableGlobal1": False}


def test_a_skill_part_asked_of_a_host_without_the_extension_is_an_error_not_a_no_op() -> None:
    w = _FakeSkillExtWorker()
    real_call = w.call

    def _legacy_host(method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        if method in ("apply_group_gem_states", "get_group_gem_states"):
            raise RuntimeError(f"unknown method: {method}")  # what a pob-zh.exe pack answers
        return real_call(method, params, **kw)

    w.call = _legacy_host  # type: ignore[method-assign]
    cache = _BatchCache(w)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot set a gem's skill part"):
        _execute_batch_ops(w, [{"op": "skill", "group": 1, "gem": 2, "skillPart": 2}], cache)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot set a gem's skill part"):
        _execute_batch_ops(w, [{"op": "add_group", "gems": [{"name": "Arc", "skillPart": 2}]}], cache)  # type: ignore[arg-type]
    # Nothing asked for: replacing a group still works, the old gems' state is simply not carried.
    _execute_batch_ops(w, [{"op": "replace_group", "group": 1, "gems": ["Arc"]}], cache)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("op_dict", "bad_key"),
    [
        ({"op": "flask", "slot": "Flask 1", "active": False}, "slot"),
        ({"op": "item", "slot": "Belt", "raw": "Rarity: NORMAL\nHeavy Belt", "mods": []}, "mods"),
        ({"op": "item_db", "name": "Mageblood", "rarity": "UNIQUE"}, "rarity"),
        ({"op": "item_edit", "slot": "Belt", "mods": ["+20 to Strength"]}, "mods"),
        ({"op": "unequip", "item": "Belt"}, "item"),
        ({"op": "anoint", "notable": "Charisma", "oil": "Golden"}, "oil"),
        ({"op": "tree", "nodes": ["12345"]}, "nodes"),
        ({"op": "tree_reset", "all": True}, "all"),
        ({"op": "set_level", "lvl": 95}, "lvl"),
        ({"op": "set_class", "class": "Witch", "tree": "default"}, "tree"),
        ({"op": "switch_main", "mainGroup": 1}, "mainGroup"),
        ({"op": "add_group", "gems": ["Arc"], "main": True}, "main"),
        ({"op": "delete_group", "label": "Main"}, "label"),
        ({"op": "set_group", "group": 1, "gems": ["Arc"]}, "gems"),
        ({"op": "replace_group", "group": 1, "gems": ["Arc"], "makeMain": True}, "makeMain"),
        ({"op": "skill_add", "group": 1, "gem": "Arc", "newGem": "Spark"}, "newGem"),
        ({"op": "skill_rm", "group": 1, "gem": 1, "skillPart": 2}, "skillPart"),
    ],
)
def test_unknown_keys_rejected_across_all_other_batch_ops(op_dict: dict[str, Any], bad_key: str) -> None:
    kind = str(op_dict["op"])
    with pytest.raises(ValueError) as exc_info:
        validate_batch_op(3, kind, op_dict)
    msg = str(exc_info.value)
    assert f"Batch step #3 ({kind}):" in msg
    assert repr(bad_key) in msg
    assert "valid keys:" in msg


def test_skill_ambiguous_name_without_gem_and_zero_mutation_rejected() -> None:
    w = _FakeSkillExtWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as exc_amb:
        _execute_batch_ops(w, [{"op": "skill", "group": "main", "name": "Empower"}], cache)  # type: ignore[arg-type]
    assert str(exc_amb.value) == (
        "Batch step #1 (skill): 'name' without 'gem' is ambiguous; "
        'use {"op": "skill_add", "group": ..., "name": "..."} (or "addGem") to add a gem, '
        'or pass both "gem" and "newGem" (or "name") to replace an existing gem.'
    )

    with pytest.raises(ValueError, match="ambiguous"):
        _execute_batch_ops(w, [{"op": "skill_set", "group": "main", "name": "Empower"}], cache)  # type: ignore[arg-type]

    # Zero-mutation skill / skill_set ops must fail loudly rather than returning a silent no-op
    for noop_op in (
        {"op": "skill", "group": "main"},
        {"op": "skill", "group": "main", "level": 21},
        {"op": "skill_set", "group": "main", "enabled": False},
    ):
        with pytest.raises(ValueError, match="zero mutations"):
            _execute_batch_ops(w, [noop_op], cache)  # type: ignore[arg-type]


def test_item_db_name_alias_and_skill_gem_plus_name_alias() -> None:
    w = _FakeSkillExtWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]

    steps = _execute_batch_ops(
        w,  # type: ignore[arg-type]
        [
            {"op": "item_db", "name": "Mageblood", "slot": "Belt", "comment": "equip MB via name alias"},
            {"op": "skill", "group": "main", "gem": "Desecrate", "name": "Unearth", "level": 21},
            {"op": "skill_add", "group": "main", "name": "Combustion Support", "skillPart": 2},
            {"op": "skill_set", "group": "main", "gem": "Bodyswap of Sacrifice", "skill_part": 1, "stage_count": 5},
        ],
        cache,
    )
    assert steps[0]["op"] == "item_db"
    assert steps[0]["query"] == "Mageblood"
    assert steps[0]["slotName"] == "Belt"
    assert w.groups[0]["gems"][0]["nameSpec"] == "Unearth"
    assert w.groups[0]["gems"][0]["level"] == 21
    assert w.groups[0]["gems"][2]["nameSpec"] == "Combustion Support"
    assert w.groups[0]["gems"][2]["skillPart"] == 2
    assert w.groups[0]["gems"][2]["skillPartCalcs"] == 2
    assert w.groups[0]["gems"][1]["skillPart"] == 1
    assert w.groups[0]["gems"][1]["skillPartCalcs"] == 1
    assert w.groups[0]["gems"][1]["skillStageCount"] == 5
    assert w.groups[0]["gems"][1]["skillStageCountCalcs"] == 5


# ---------------------------------------------------------------------------
# 2. Gem spec normalization, `_try_call_ext`, and duplicate gem state carry-over
# ---------------------------------------------------------------------------


def test_normalize_gem_spec_skill_part_and_stage_count_validation() -> None:
    sp1 = _normalize_gem_spec({"name": "Bodyswap of Sacrifice", "skill_part": 2, "stage_count": 6})
    assert sp1["skill_part"] == 2
    assert sp1["stage_count"] == 6

    sp2 = _normalize_gem_spec({"gem": "Divine Ire", "skillPart": 2, "skillStageCount": 20})
    assert sp2["skill_part"] == 2
    assert sp2["stage_count"] == 20

    for bad_spec in (
        {"name": "Arc", "skillPart": 0},
        {"name": "Arc", "skill_part": -1},
        {"name": "Arc", "skillPart": True},
        {"name": "Arc", "stageCount": 1.5},
        {"name": "Arc", "stage_count": "max"},
        {"name": "Arc", "unknownBogusKey": 1},
    ):
        with pytest.raises(ValueError):
            _normalize_gem_spec(bad_spec)


def test_try_call_ext_distinguishes_missing_rpc_from_fatal_errors() -> None:
    class _UnmockedWorker:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def call(self, _m: str, _p: dict[str, Any]) -> dict[str, Any]:
            raise self._exc

    for benign in (
        AssertionError("Unexpected mock RPC 'get_group_gem_states' with params {'group': 1}"),
        AssertionError("get_group_gem_states"),
        NotImplementedError(),
        ValueError("Unknown method: get_group_gem_states"),
        BridgeError("RPC 'get_group_gem_states' failed: [unknown_method] Unknown method"),
    ):
        assert _try_call_ext(_UnmockedWorker(benign), "get_group_gem_states", {"group": 1}) is None  # type: ignore[arg-type]

    for fatal in (
        BridgeTimeoutError("RPC 'get_group_gem_states' timed out"),
        BridgeError("RPC 'get_group_gem_states' failed: [lua_error] attempt to index a nil value"),
        BridgeError("Headless engine crashed during 'get_group_gem_states': EOF"),
    ):
        with pytest.raises(BridgeError):
            _try_call_ext(_UnmockedWorker(fatal), "get_group_gem_states", {"group": 1})  # type: ignore[arg-type]


def test_replace_group_preserves_state_and_duplicate_order_and_allows_override() -> None:
    w = _FakeSkillExtWorker()
    # Set up two duplicate Bodyswap gems with distinct skillPart values (2 and 1)
    w.groups[0]["gems"] = [
        {
            "index": 1,
            "name": "Bodyswap of Sacrifice",
            "nameSpec": "Bodyswap of Sacrifice",
            "gemId": "Metadata/Items/Gems/SkillGemBodyswapOfSacrifice",
            "level": 20,
            "quality": 20,
            "enabled": True,
            "count": 1,
            "skillPart": 2,
            "skillPartCalcs": 2,
            "skillMinion": "RaisedSpectre",
        },
        {
            "index": 2,
            "name": "Bodyswap of Sacrifice",
            "nameSpec": "Bodyswap of Sacrifice",
            "gemId": "Metadata/Items/Gems/SkillGemBodyswapOfSacrifice",
            "level": 20,
            "quality": 20,
            "enabled": True,
            "count": 1,
            "skillPart": 1,
            "skillPartCalcs": 1,
        },
    ]

    # 1. String specs preserve ordered per-instance states (first gets part 2 + minion, second gets part 1)
    mutators.replace_skill_group(
        w,  # type: ignore[arg-type]
        "main",
        [
            "Bodyswap of Sacrifice:21:20",
            "Minion Life Support:20:20",
            {"name": "Bodyswap of Sacrifice", "level": 20, "skill_part": 3},
        ],
        apply=True,
    )
    g0 = w.groups[0]
    assert g0["gems"][0]["skillPart"] == 2
    assert g0["gems"][0]["skillPartCalcs"] == 2
    assert g0["gems"][0]["skillMinion"] == "RaisedSpectre"
    assert g0["gems"][2]["skillPart"] == 3
    assert g0["gems"][2]["skillPartCalcs"] == 3
    assert g0["mainActiveSkill"] == 2
    assert g0["mainActiveSkillCalcs"] == 2


# ---------------------------------------------------------------------------
# 3. Real headless PoB engine integration test (`Bodyswap of Sacrifice`)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB headless engine available")
def test_real_engine_bodyswap_of_sacrifice_skill_part_preserved_and_dps_verified(
    tmp_path: Path,
) -> None:
    assert _ENGINE_DIR is not None
    w = BridgeWorker(slot_name="test", pob_dir=_ENGINE_DIR, runtime_dir=tmp_path / "rt")
    try:
        ver = w.call("version", {})
        assert (ver.get("caps") or {}).get("skillCalcsExt") is True
        assert w.caps.get("skillCalcsExt") is True

        # Fresh Necromancer build with a high-life minion skill so Part 2 (Minion explosion) differs sharply from Part 1
        mutators.create_new_build(w, class_name="Witch", ascendancy="Necromancer", level=90, apply=True)
        mutators.add_skill_group(
            w,
            label="Minions",
            gems=["Raise Zombie:20:20", "Minion Life Support:20:20"],
            apply=True,
        )

        # 1. Add group with explicit `skill_part=1` vs `skill_part=2` and verify DPS & gem state
        mutators.add_skill_group(
            w,
            label="Main Bodyswap",
            gems=[{"name": "Bodyswap of Sacrifice", "level": 20, "quality": 20, "skill_part": 1}],
            make_main=True,
            apply=True,
        )
        skills_info = w.call("list_skills", {})
        main_idx = int(skills_info["mainSocketGroup"])
        st_part1 = w.call("get_group_gem_states", {"group": main_idx})
        assert st_part1["gems"][0]["skillPart"] == 1
        dps_part1 = float((w.call("get_stats", {}).get("stats") or {}).get("CombinedDPS") or 0.0)
        assert dps_part1 > 0.0

        # Switch to skill_part=2 via batch `skill_set`
        batch_part2 = mutators.run_batch(
            w,
            [{"op": "skill_set", "group": "main", "gem": "Bodyswap of Sacrifice", "skillPart": 2}],
            apply=True,
        )
        dps_part2 = float(batch_part2["after"]["primaryDps"])
        st_part2 = w.call("get_group_gem_states", {"group": main_idx})
        assert st_part2["gems"][0]["skillPart"] == 2
        assert st_part2["gems"][0]["skillPartCalcs"] == 2
        assert dps_part2 != pytest.approx(dps_part1, rel=1e-3)

        # 2. `replace_group` WITHOUT specifying `skill_part` on Bodyswap MUST preserve `skillPart=2` & explosion DPS!
        rep_res = mutators.run_batch(
            w,
            [
                {
                    "op": "replace_group",
                    "group": "main",
                    "gems": [
                        "Bodyswap of Sacrifice:20:20",
                        "Minion Life Support:20:20",
                        "Elemental Focus Support:20:20",
                    ],
                }
            ],
            apply=True,
        )
        dps_after_replace = float(rep_res["after"]["primaryDps"])
        st_after_replace = w.call("get_group_gem_states", {"group": main_idx})
        assert st_after_replace["gems"][0]["skillPart"] == 2
        assert st_after_replace["gems"][0]["skillPartCalcs"] == 2
        # Minion Life + Elemental Focus both scale Part 2 Minion explosion DPS significantly above bare Part 2!
        assert dps_after_replace > dps_part2 * 1.2

        # 3. `add_group` with `"skill_part": 2` in a multi-active-gem group + `replace_group` active-skill preservation
        add_grp_res = mutators.run_batch(
            w,
            [
                {
                    "op": "add_group",
                    "label": "Dual Active Bodyswap",
                    "makeMain": True,
                    "gems": [
                        "Desecrate:20:20",
                        {"name": "Bodyswap of Sacrifice", "level": 20, "quality": 20, "skill_part": 2},
                    ],
                },
                {"op": "set_group", "group": "main", "mainActiveSkill": 2},
            ],
            apply=True,
        )
        dual_idx = int(add_grp_res["detail"]["steps"][0]["groupIndex"])
        dual_base_dps = float(add_grp_res["after"]["primaryDps"])
        assert dual_base_dps == pytest.approx(dps_part2, rel=1e-3)

        # Replace gems in Dual Active group -> preserves BOTH `mainActiveSkill=2` (Bodyswap) AND `skillPart=2`!
        dual_rep = mutators.replace_skill_group(
            w,
            dual_idx,
            ["Desecrate:20:20", "Bodyswap of Sacrifice:20:20", "Elemental Focus Support:20:20"],
            apply=True,
        )
        st_dual = w.call("get_group_gem_states", {"group": dual_idx})
        assert st_dual["mainActiveSkill"] == 2
        assert st_dual["gems"][1]["skillPart"] == 2
        assert st_dual["gems"][1]["skillPartCalcs"] == 2
        assert float(dual_rep["after"]["primaryDps"]) > dual_base_dps * 1.2
    finally:
        w.stop()
