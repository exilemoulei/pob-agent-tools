"""Strict argument types for batch steps and mutation RPCs: wrong types exit 1, never coerce."""

from __future__ import annotations

from typing import Any

import pytest

import pob_cli.mutators.batch_ops as batch_ops
import pob_cli.mutators.batch_sweep as batch_sweep
from pob_cli.daemon import rpc_mutate
from pob_cli.mutators.batch_validate import validate_batch_op
from pob_cli.mutators.strict_args import parse_bool, parse_int, parse_replace_mods, parse_token_list


@pytest.mark.parametrize(
    ("raw", "want"),
    [(True, True), (False, False), ("false", False), ("TRUE", True), ("off", False), ("on", True), (0, False), (1, True)],
)
def test_parse_bool_accepts_explicit_spellings(raw: Any, want: bool) -> None:
    assert parse_bool(raw, "f") is want


@pytest.mark.parametrize("raw", ["nope", "", 2, [True], {"a": 1}, 0.5])
def test_parse_bool_rejects_everything_else(raw: Any) -> None:
    with pytest.raises(ValueError, match="'f' must be a boolean"):
        parse_bool(raw, "f")


def test_parse_int_rejects_bools_and_containers() -> None:
    assert parse_int("12", "n") == 12
    assert parse_int(3, "n") == 3
    for bad in (True, [2], {"x": 1}, "1.5", 1.5, "abc"):
        with pytest.raises(ValueError, match="'n' must be an integer"):
            parse_int(bad, "n")


def test_parse_token_list_never_iterates_characters() -> None:
    assert parse_token_list("Elemental Equilibrium", "allocate") == ["Elemental Equilibrium"]
    assert parse_token_list(12345, "allocate") == ["12345"]
    assert parse_token_list(["1", 2], "allocate") == ["1", "2"]
    for bad in (["  "], [""], [True], [["1"]], {"a": 1}, True):
        with pytest.raises(ValueError):
            parse_token_list(bad, "allocate")


def test_parse_replace_mods_rejects_malformed_entries() -> None:
    assert parse_replace_mods([["a", "b"]]) == [("a", "b")]
    for bad in ([{"old": "a", "new": "b"}], [["a", "b", "c"]], [["a"]], "a->b", [["a", 1]]):
        with pytest.raises(ValueError, match="replaceMods"):
            parse_replace_mods(bad)


@pytest.mark.parametrize(
    ("kind", "op", "match"),
    [
        ("tree", {"op": "tree"}, "at least one node"),
        ("tree", {"op": "tree", "allocate": [], "deallocate": []}, "at least one node"),
        ("tree", {"op": "tree", "allocate": ["  "]}, "empty node token"),
        ("tree", {"op": "tree", "allocate": [{"id": 1}]}, "allocate"),
        ("tree", {"op": "tree", "allocate": ["1"], "allowCascade": "maybe"}, "allowCascade"),
        ("switch_main", {"op": "switch_main"}, "switches nothing"),
        ("switch_main", {"op": "switch_main", "skillPart": [1]}, "skillPart"),
        ("switch_main", {"op": "switch_main", "mainSkill": True}, "mainSkill"),
        ("anoint", {"op": "anoint", "notable": "Whispers of Doom", "index": [2]}, "index"),
        ("new_build", {"op": "new_build", "class": "Witch", "level": True}, "level"),
        ("set_level", {"op": "set_level", "level": True}, "level"),
        ("flask", {"op": "flask", "index": 1, "active": "sometimes"}, "active"),
        ("item_edit", {"op": "item_edit", "slot": "Ring 1", "replaceMods": [{"a": "b"}]}, "replaceMods"),
        ("item_edit", {"op": "item_edit", "slot": "Ring 1", "replaceMods": [["a", "b", "c"]]}, "replaceMods"),
        ("skill_add", {"op": "skill_add", "gem": "Empower", "level": [4]}, "level"),
        ("set_group", {"op": "set_group", "group": "main", "mainActiveSkill": True}, "mainActiveSkill"),
        ("set_group", {"op": "set_group", "group": "main", "main_active_skill": [2]}, "main_active_skill"),
    ],
)
def test_validate_batch_op_rejects_bad_types_and_empty_steps(kind: str, op: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_batch_op(1, kind, op)


def test_a_single_string_is_one_mod_line_or_one_gem_never_its_characters() -> None:
    norm = validate_batch_op(1, "item_edit", {"op": "item_edit", "slot": "Helmet", "addMods": "+50 to maximum Life"})
    assert norm["addMods"] == ["+50 to maximum Life"]
    norm = validate_batch_op(1, "item_edit", {"op": "item_edit", "slot": "Helmet", "remove_mods": "Rarity"})
    assert norm["remove_mods"] == ["Rarity"]
    norm = validate_batch_op(1, "add_group", {"op": "add_group", "gems": "Arc:20:20"})
    assert norm["gems"] == ["Arc:20:20"]
    norm = validate_batch_op(1, "replace_group", {"op": "replace_group", "group": "main", "gems": {"name": "Arc"}})
    assert norm["gems"] == [{"name": "Arc"}]
    for kind, op, match in (
        ("item_edit", {"op": "item_edit", "slot": "Helmet", "addMods": ["a", 1]}, "addMods"),
        ("item_edit", {"op": "item_edit", "slot": "Helmet", "removeMods": {"a": 1}}, "removeMods"),
        ("add_group", {"op": "add_group", "gems": ["Arc", 3]}, "gems"),
        ("add_group", {"op": "add_group", "gems": 3}, "gems"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_batch_op(1, kind, op)


def test_gem_objects_are_parsed_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.mutators import skills_resolve

    monkeypatch.setattr(skills_resolve, "_validate_gem_level", lambda _n, lvl, **_k: (lvl, []))
    spec = skills_resolve._normalize_gem_spec({"name": "Arc", "level": "20", "quality": 0, "enabled": "false"})
    assert (spec["level"], spec["quality"], spec["enabled"]) == (20, 0, False)
    for bad, match in (
        ({"name": "Arc", "level": True}, "level"),
        ({"name": "Arc", "quality": [20]}, "quality"),
        ({"name": "Arc", "enabled": "sometimes"}, "enabled"),
        ({"name": "Arc", "count": 1.5}, "count"),
        ({"name": "Arc", "enableGlobal1": "maybe"}, "enableGlobal1"),
    ):
        with pytest.raises(ValueError, match=match):
            skills_resolve._normalize_gem_spec(bad)


def test_a_null_bool_reads_as_false_not_as_the_default() -> None:
    from pob_cli.mutators.strict_args import opt_bool

    assert opt_bool({"apply": None}, "apply", default=True) is False
    assert opt_bool({}, "apply", default=True) is True
    assert opt_bool({"allow_cascade": None, "allowCascade": True}, "allow_cascade", "allowCascade", default=True) is False
    assert opt_bool({"allowCascade": "off"}, "allow_cascade", "allowCascade", default=True) is False


def test_node_id_csv_is_split_but_a_name_with_a_comma_is_not() -> None:
    assert parse_token_list("4397,27929", "allocate") == ["4397", "27929"]
    assert parse_token_list(" 4397 , 27929:1234 ", "allocate") == ["4397", "27929:1234"]
    assert parse_token_list("Heart, Soul and 123", "allocate") == ["Heart, Soul and 123"]
    assert parse_token_list("4397", "allocate") == ["4397"]


def test_validate_batch_op_canonicalizes_bool_and_int_strings() -> None:
    norm = validate_batch_op(1, "flask", {"op": "flask", "index": "all", "active": "false"})
    assert norm["active"] is False
    norm = validate_batch_op(1, "tree", {"op": "tree", "allocate": "Point Blank", "allowCascade": "off"})
    assert norm["allocate"] == ["Point Blank"] and norm["deallocate"] == []
    assert norm["allowCascade"] is False
    norm = validate_batch_op(1, "set_level", {"op": "set_level", "level": "90", "auto": "true"})
    assert norm["level"] == 90 and norm["auto"] is True


class _TreeCache:
    def get_tree(self) -> dict[str, Any]:
        return {"treeVersion": "3_26", "allocatedNodes": [1], "dynamicNodes": []}

    def invalidate(self, *_tabs: str) -> None:
        pass


class _TreeWorker:
    def get_cached_tree_data(self, _ver: Any) -> dict[str, Any]:
        return {"nodes": {}}


def test_batch_tree_step_resolves_whole_tokens_and_forwards_noop_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[str] = []
    seen: dict[str, Any] = {}

    def _fake_resolve(_w: Any, tok: str, _db: Any, _dyn: Any) -> tuple[int, None]:
        resolved.append(tok)
        return (1, None)

    def _fake_apply(_w: Any, alloc: Any, dealloc: Any, **kw: Any) -> tuple[Any, ...]:
        seen["allow_cascade"] = kw["allow_cascade"]
        kw["noop_warnings_out"].append("Node 1 is already allocated (skipped allocate).")
        return [], {"allocatedNodes": [1]}, [], 0, None

    monkeypatch.setattr(batch_ops, "_resolve_node_token", _fake_resolve)
    monkeypatch.setattr(batch_ops, "_apply_tree_changes_in_place", _fake_apply)
    monkeypatch.setattr(batch_ops, "_check_agnostic_dealloc_caution", lambda *a, **k: [])
    steps = batch_ops._execute_batch_ops(
        _TreeWorker(),  # type: ignore[arg-type]
        [{"op": "tree", "allocate": "Point Blank", "allowCascade": "false"}],
        _TreeCache(),  # type: ignore[arg-type]
    )
    assert resolved == ["Point Blank"]
    assert seen["allow_cascade"] is False
    assert steps[0]["noopWarnings"] == ["Node 1 is already allocated (skipped allocate)."]
    _, noops = batch_sweep._step_warnings(steps)
    assert noops == ["step 1: Node 1 is already allocated (skipped allocate)."]


@pytest.mark.parametrize("apply", [False, True])
def test_batch_with_a_malformed_later_step_runs_no_step_and_leaves_the_build(apply: bool) -> None:
    from test_cow_snapshot_sweep import _CowFakeWorker

    from pob_cli.snapshot import history_of

    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.last_good_code = w._serialize()
    hist = history_of(w)
    before = (w._serialize(), hist.committed, hist.rev, len(hist.undo))
    ran: list[int] = []

    def _edit(worker: Any, _op: dict[str, Any], _cache: Any, idx: int) -> dict[str, Any]:
        ran.append(idx)
        worker.dps += 15000.0
        return {"op": "config"}

    good = {"op": "config", "set": {"enemyIsBoss": "Pinnacle"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(batch_ops._BATCH_OP_HANDLERS, "config", _edit)
        with pytest.raises(ValueError, match=r"step #2 \(set_level\).*'level' must be an integer"):
            batch_sweep.run_batch(w, [good, {"op": "set_level", "level": True}], apply=apply)
        assert ran == []
        assert (w._serialize(), hist.committed, hist.rev, len(hist.undo)) == before
        assert hist.active is None and hist.stale is False

        res = batch_sweep.run_batch(w, [good], apply=apply)  # the slot still takes a valid batch
        assert ran == [1] and res["applied"] is apply
    assert (w._serialize() != before[0]) is apply


def test_rpc_item_edit_rejects_malformed_replace_mods(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.mutators as mutators

    monkeypatch.setattr(mutators, "edit_item_mods", lambda *a, **k: {"ok": True})
    with pytest.raises(ValueError, match="replace_mods"):
        rpc_mutate._h_item_edit(None, {"slot": "Ring 1", "replace_mods": [["a", "b", "c"]]}, None)  # type: ignore[arg-type]


def test_rpc_tree_change_parses_bools_and_tokens_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.mutators as mutators

    captured: dict[str, Any] = {}
    monkeypatch.setattr(mutators, "change_tree", lambda _w, **kw: captured.update(kw) or {})
    rpc_mutate._h_tree_change(None, {"allocate": "Point Blank", "allow_cascade": "false"}, None)  # type: ignore[arg-type]
    assert captured["allocate"] == ["Point Blank"]
    assert captured["allow_cascade"] is False
    with pytest.raises(ValueError, match="allow_cascade"):
        rpc_mutate._h_tree_change(None, {"allocate": ["1"], "allow_cascade": "nah"}, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="index"):
        rpc_mutate._h_item_anoint(None, {"notable": "x", "index": [2]}, None)  # type: ignore[arg-type]


def test_borrow_reads_apply_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`borrow` is the one two-slot handler that writes to the build."""
    from types import SimpleNamespace

    from pob_cli import comparator
    from pob_cli.daemon.rpc_dual import dispatch_dual_slot_action

    seen: list[bool] = []
    monkeypatch.setattr(comparator, "borrow_from_ref", lambda _m, _r, **kw: seen.append(kw["apply"]) or {})
    manager = SimpleNamespace(_attach_state_meta=lambda res, _w: res)
    ref = SimpleNamespace(last_good_code="CODE")
    for raw, want in (("false", False), ("0", False), (None, False), (True, True), ("true", True)):
        dispatch_dual_slot_action(manager, "borrow", {"apply": raw, "item_slots": ["Amulet"]}, SimpleNamespace(), ref)  # type: ignore[arg-type]
        assert seen[-1] is want, raw
    with pytest.raises(ValueError, match="apply"):
        dispatch_dual_slot_action(manager, "borrow", {"apply": "maybe", "item_slots": ["Amulet"]}, SimpleNamespace(), ref)  # type: ignore[arg-type]
