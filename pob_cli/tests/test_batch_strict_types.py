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
    ],
)
def test_validate_batch_op_rejects_bad_types_and_empty_steps(kind: str, op: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_batch_op(1, kind, op)


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
