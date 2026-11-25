"""Daemon handlers call the domain functions directly: a renamed or removed parameter must fail loudly.

The handlers used to go through `call_compat`, which dropped any keyword the callee did not accept,
so signature drift silently turned a flag into a no-op. Each callee is replaced by a stub that binds
the arguments against the real signature.
"""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from pob_cli import analyzer, comparator, game_data, mutators
from pob_cli.daemon import rpc_handlers, rpc_mutate

_TARGETS: list[tuple[Any, str]] = [
    (game_data, "query_multi"),
    (analyzer, "extract_summary"),
    (analyzer, "audit_build"),
    (analyzer, "inspect_tree"),
    (analyzer, "inspect_config"),
    (analyzer, "search_calcs_breakdown"),
    (comparator, "run_matrix"),
    (mutators, "create_new_build"),
    (mutators, "unequip_item"),
    (mutators, "search_and_try_item_db"),
    (mutators, "add_skill_group"),
    (mutators, "replace_skill_group"),
    (mutators, "change_tree"),
    (mutators, "run_batch"),
    (mutators, "run_sweep"),
]

_HANDLERS: list[tuple[Any, dict[str, Any]]] = [
    (rpc_handlers._h_summary, {}),
    (rpc_handlers._h_audit, {"keep": "Onslaught"}),
    (rpc_handlers._h_tree, {"search": "life", "maxCost": 3, "socketRadius": 1}),
    (rpc_handlers._h_config_list, {"includeDefaults": True, "search": "boss"}),
    (rpc_handlers._h_calc, {"keywords": ["Life"], "limit": 5}),
    (rpc_handlers._h_matrix, {"scenarios": ["boss"], "full": True}),
    (rpc_mutate._h_new_build, {"className": "Witch", "level": 90}),
    (rpc_mutate._h_item_unequip, {"slot": "Helmet"}),
    (rpc_mutate._h_item_db, {"query": "Mageblood", "index": 1}),
    (rpc_mutate._h_skill_new_group, {"gems": ["Arc"], "makeMain": True}),
    (rpc_mutate._h_skill_replace_group, {"group": 2, "gems": ["Arc"]}),
    (rpc_mutate._h_tree_change, {"allocate": ["1"]}),
    (rpc_mutate._h_batch, {"ops": [], "returnSections": ["summary"]}),
    (rpc_mutate._h_sweep, {"variants": [], "calcKeywords": ["Life"]}),
]


@pytest.fixture
def strict_callees(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    called: list[str] = []
    for module, name in _TARGETS:
        sig = inspect.signature(getattr(module, name))

        def stub(*args: Any, _sig: inspect.Signature = sig, _name: str = name, **kwargs: Any) -> dict[str, Any]:
            _sig.bind(*args, **kwargs)  # TypeError on any argument the real function does not take
            called.append(_name)
            return {}

        monkeypatch.setattr(module, name, stub)
    return called


@pytest.mark.parametrize(("handler", "params"), _HANDLERS, ids=lambda v: getattr(v, "__name__", ""))
def test_handler_arguments_bind_to_the_real_signature(
    strict_callees: list[str], handler: Any, params: dict[str, Any]
) -> None:
    manager = SimpleNamespace(_attach_warning_if_degraded=lambda res: res)
    assert handler(manager, params, object()) == {}
    assert len(strict_callees) == 1


def test_game_data_multi_query_binds(strict_callees: list[str], tmp_path: Any) -> None:
    manager = SimpleNamespace(_lock=threading.Lock(), pob_dir=tmp_path, runtime_dir=tmp_path)
    params = {"kind": "gem", "queries": ["Arc", "Spark"], "ilvl": 84, "limit": 3}
    assert rpc_handlers.dispatch_game_data(manager, params) == {}
    assert strict_callees == ["query_multi"]
