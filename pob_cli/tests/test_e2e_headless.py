"""End-to-end integration tests against real pob-zh.exe --engine-headless using isolated POB_RUNTIME_DIR."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine
from support import dead_proxy_url

from pob_cli.game_data import fold_han
from pob_cli.importers import xml_to_share_code

GOLDEN_URL = "https://poedb.tw/pob/AWqOLSSmyG"
LEGACY_ENGINE_DIR = find_runnable_engine()
SRC_DIR = Path(__file__).resolve().parents[1] / "src"

pytestmark = pytest.mark.skipif(
    LEGACY_ENGINE_DIR is None,
    reason="No runnable PoB engine found (set POB_ROOT or run pob bootstrap)",
)


def _build_env(runtime_dir: Path, *, extra: dict[str, str | None] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["POB_RUNTIME_DIR"] = str(runtime_dir)
    env["POB_ROOT"] = str(LEGACY_ENGINE_DIR)
    env["POB_OFFLINE"] = "1"
    # Clear proxy vars by default so test suite is deterministic
    for k in ("POB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    if extra:
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return env


def _run_pob_proc(
    *args: str,
    runtime_dir: Path,
    extra_env: dict[str, str | None] | None = None,
    timeout: float = 180.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pob_cli.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
        env=_build_env(runtime_dir, extra=extra_env),
    )


def _run_pob(
    *args: str,
    runtime_dir: Path,
    extra_env: dict[str, str | None] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    proc = _run_pob_proc(*args, "--json", runtime_dir=runtime_dir, extra_env=extra_env, timeout=timeout, check=True)
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def e2e_runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    rt = tmp_path_factory.mktemp("pob_e2e_rt")
    try:
        yield rt
    finally:
        try:
            _run_pob_proc("stop", runtime_dir=rt, timeout=15.0)
        except Exception:
            pass


def test_e2e_m1_load_summary_audit_calc(e2e_runtime_dir: Path, tmp_path: Path) -> None:
    """Verify M1: load AWqOLSSmyG, summary, audit 4-dimension dewatering, items/skills/tree/calc, XML roundtrip."""
    load_res = _run_pob("load", GOLDEN_URL, runtime_dir=e2e_runtime_dir)
    info = load_res["buildInfo"]
    assert info["level"] == 94
    assert info["className"] == "Templar"
    assert info["ascendClassName"] == "Hierophant"

    sum_res = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    assert sum_res["offence"]["CombinedDPS"] > 100000
    assert sum_res["defence"]["Life"] == 3501
    # Golden value from PoB 2.67.2; mana scaling shifted slightly in later PoB releases.
    assert sum_res["defence"]["Mana"] == pytest.approx(9075, rel=0.01)
    assert sum_res["defence"]["TotalEHP"] > 50000

    # Verify audit non-destructiveness
    audit_res = _run_pob("audit", runtime_dir=e2e_runtime_dir)
    assert "inflationStripped" in audit_res
    assert audit_res["original"]["primaryDps"] == pytest.approx(sum_res["offence"]["CombinedDPS"], rel=1e-4)

    # Verify tree #1 jewel socket search
    tree_res = _run_pob("tree", "--search", "#1", runtime_dir=e2e_runtime_dir)
    assert tree_res["totalMatches"] >= 1
    assert tree_res["matches"][0]["socketIndex"] == 1

    # Verify calc breakdown (including v0.2 bilingual Average Hit query)
    calc_res = _run_pob("calc", "Mana", runtime_dir=e2e_runtime_dir)
    assert calc_res["totalMatches"] >= 1
    calc_avg = _run_pob("calc", "Average Hit", runtime_dir=e2e_runtime_dir)
    assert calc_avg["totalMatches"] >= 1

    # Verify save & load local XML (R11)
    xml_file = tmp_path / "golden_saved.xml"
    save_res = _run_pob("save", str(xml_file), runtime_dir=e2e_runtime_dir)
    assert xml_file.is_file()
    assert save_res["bytes"] > 1000

    reload_res = _run_pob("load", str(xml_file), runtime_dir=e2e_runtime_dir)
    assert reload_res["importMethod"] == "xml_file"
    assert reload_res["buildInfo"]["level"] == 94


def test_e2e_m2_what_if_apply_undo_redo_and_anoint(e2e_runtime_dir: Path) -> None:
    """Verify M2: zero-residue what-if, --apply, undo/redo, idempotent tree change (R12), and anoint validation."""
    sum_before = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    dps_before = sum_before["offence"]["CombinedDPS"]
    ehp_before = sum_before["defence"]["TotalEHP"]

    # 1. What-if flask off (single & 'all' per M2 acceptance test, must not mutate state)
    flask_whatif = _run_pob("item", "flask", "1", "off", runtime_dir=e2e_runtime_dir)
    assert flask_whatif["applied"] is False

    flask_all_whatif = _run_pob("item", "flask", "all", "off", runtime_dir=e2e_runtime_dir)
    assert flask_all_whatif["applied"] is False
    assert len(flask_all_whatif["detail"]["slots"]) == 5

    # Verify `pob item try` non-JSON tooltip lines and multi-slot Ring comparison (P1-1)
    sample_ring = "Rarity: RARE\nTest Ring\nRuby Ring\n+30% to Fire Resistance\n+70 to maximum Life\n+50 to maximum Mana"
    try_ring_json = _run_pob("item", "try", "--raw", sample_ring, "--tooltip-only", runtime_dir=e2e_runtime_dir)
    slot_names = [st["slotName"] for st in try_ring_json["slotTooltips"]]
    assert "Ring 1" in slot_names and "Ring 2" in slot_names

    try_ring_md = _run_pob_proc(
        "item", "try", "--raw", sample_ring, "--tooltip-only", runtime_dir=e2e_runtime_dir, check=True
    ).stdout
    # A zh engine renders the tooltip in its display locale (Traditional by default).
    assert "Ruby Ring" in try_ring_md or "红玉戒指" in fold_han(try_ring_md)
    assert "- id\n" not in try_ring_md

    sum_mid = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    assert sum_mid["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)
    assert sum_mid["defence"]["TotalEHP"] == pytest.approx(ehp_before, rel=1e-6)

    # 2. Apply config change -> undo -> redo -> undo
    cfg_apply = _run_pob("config", "set", "enemyIsBoss=Uber", "--apply", runtime_dir=e2e_runtime_dir)
    assert cfg_apply["applied"] is True
    assert cfg_apply["undoDepth"] >= 1

    undo_res = _run_pob("undo", runtime_dir=e2e_runtime_dir)
    assert undo_res["action"] == "undo"
    sum_after_undo = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    assert sum_after_undo["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)

    redo_res = _run_pob("redo", runtime_dir=e2e_runtime_dir)
    assert redo_res["action"] == "redo"
    _run_pob("undo", runtime_dir=e2e_runtime_dir)

    # 3. Anoint validation: non-existent notable must fail cleanly and leave zero residue
    proc_bad = _run_pob_proc(
        "item", "anoint", "--slot", "Amulet", "--notable", "NonExistentNotableXYZ999", "--json",
        runtime_dir=e2e_runtime_dir,
    )
    assert proc_bad.returncode != 0

    # Valid anoint what-if on Amulet (Charisma / 魅力)
    anoint_ok = _run_pob("item", "anoint", "--slot", "Amulet", "--notable", "Charisma", runtime_dir=e2e_runtime_dir)
    assert anoint_ok["applied"] is False
    assert anoint_ok["detail"]["slotName"] == "Amulet"

    # 4. Idempotent tree change (allocating an already-allocated node is a safe no-op)
    tree_summary = _run_pob("tree", runtime_dir=e2e_runtime_dir)
    allocated_ks = tree_summary["keystones"][0]["id"]
    tree_idemp = _run_pob("tree", "change", "--allocate", str(allocated_ks), runtime_dir=e2e_runtime_dir)
    assert tree_idemp["detail"]["actions"][0]["action"] == "skip_already_allocated"

    # 5. Batch R12 validation: unallocated Mastery node without :effectId must fail cleanly
    unalloc_mastery = _run_pob("tree", "--search", "Mastery", runtime_dir=e2e_runtime_dir)
    unalloc_m_ids = [m["id"] for m in unalloc_mastery["matches"] if m.get("type") == "Mastery" and not m.get("allocated")]
    if unalloc_m_ids:
        bad_batch_ops = json.dumps([{"op": "tree", "allocate": [str(unalloc_m_ids[0])]}])
        proc_batch_bad = _run_pob_proc(
            "batch", "--json-ops", bad_batch_ops, "--json", runtime_dir=e2e_runtime_dir
        )
        assert proc_batch_bad.returncode != 0
        assert "Mastery" in proc_batch_bad.stdout or "Mastery" in proc_batch_bad.stderr


def test_e2e_m3_rankings_compare_and_borrow(e2e_runtime_dir: Path) -> None:
    """Verify M3: rank-gems, rank-nodes, 3-baseline compare, and borrow."""
    rg_res = _run_pob("rank-gems", "--group", "main", "--top", "5", runtime_dir=e2e_runtime_dir)
    assert len(rg_res["candidates"]) >= 1

    rn_res = _run_pob("rank-nodes", "--stat", "CombinedDPS", "--max-dist", "2", "--top", "5", runtime_dir=e2e_runtime_dir)
    assert len(rn_res["byPerPoint"]) >= 1

    # Load same build into ref, tweak ref config to create artificial inflation, then run compare
    _run_pob("load", GOLDEN_URL, "--target", "ref", runtime_dir=e2e_runtime_dir)
    _run_pob("config", "set", "enemyIsBoss=None", "--target", "ref", "--apply", runtime_dir=e2e_runtime_dir)

    cmp_res = _run_pob("compare", runtime_dir=e2e_runtime_dir)
    assert "original" in cmp_res["baselines"]
    assert "envAlignedToMain" in cmp_res["baselines"]
    assert "pinnacleDewatered" in cmp_res["baselines"]

    # In pinnacleDewatered (Baseline 3), both builds are identical so CombinedDPS diff must be 0
    b3_rows = {r["stat"]: r for r in cmp_res["baselines"]["pinnacleDewatered"]["rows"]}
    assert b3_rows["CombinedDPS"]["main"] == pytest.approx(b3_rows["CombinedDPS"]["ref"], rel=1e-5)

    # Borrow Amulet and main skill group from ref into main (what-if, P0-3)
    borrow_res = _run_pob("borrow", "--item-slot", "Amulet", "--skill-group", "main", runtime_dir=e2e_runtime_dir)
    assert borrow_res["applied"] is False
    assert len(borrow_res["detail"]["borrowedItems"]) == 1
    assert borrow_res["detail"]["borrowedSkillGroup"]["newGroupIndex"] is not None
    assert borrow_res["detail"]["borrowedSkillGroup"]["switchedMain"] is True

    # Verify Vaal hybrid main skill dewatering keeps base skill active (P0-2)
    _run_pob("skill", "set", "--group", "main", "--gem", "1", "--new-gem", "Vaal Ice Nova", "--apply", runtime_dir=e2e_runtime_dir)
    try:
        vaal_audit = _run_pob("audit", runtime_dir=e2e_runtime_dir)
        assert vaal_audit["dewateredPinnacle"]["primaryDps"] > 10000
    finally:
        _run_pob("undo", runtime_dir=e2e_runtime_dir)


def test_e2e_v02_build_ops_deadlock_recovery_and_local_data(e2e_runtime_dir: Path) -> None:
    """Verify v0.2 CLI integration: config list/--append-mods, items --all --raw, item edit/db, tree set-class, matrix, deadlock self-heal, local data."""
    sum_before = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    dps_before = sum_before["offence"]["CombinedDPS"]

    # 1. config list & config set --append-mods (0-residue)
    cfg_list = _run_pob("config", "list", runtime_dir=e2e_runtime_dir)
    assert cfg_list["totalCount"] > 0
    assert isinstance(cfg_list["variables"], list)

    cfg_append = _run_pob("config", "set", "--append-mods", "+150 to maximum Life", runtime_dir=e2e_runtime_dir)
    assert cfg_append["applied"] is False
    assert cfg_append["after"]["life"] > cfg_append["before"]["life"]

    # 2. items --all --raw
    items_all = _run_pob("items", "--all", "--raw", runtime_dir=e2e_runtime_dir)
    assert items_all["allRaw"] is True
    assert items_all["equippedCount"] >= 5
    equipped_with_raw = [s for s in items_all["equippedSlots"] if s.get("raw")]
    assert len(equipped_with_raw) >= 5

    # 3. item edit (affix guardrail blocks illegal overflow; --allow-illegal-affixes runs 0-residue what-if)
    blocked = _run_pob_proc(
        "item", "edit", "--slot", "Ring 1", "--add-mod", "+80 to maximum Life",
        runtime_dir=e2e_runtime_dir,
    )
    assert blocked.returncode == 1
    assert "3/3" in (blocked.stdout + blocked.stderr) or "capacity" in (blocked.stdout + blocked.stderr)

    edit_res = _run_pob(
        "item", "edit", "--slot", "Ring 1", "--add-mod", "+80 to maximum Life",
        "--allow-illegal-affixes",
        runtime_dir=e2e_runtime_dir,
    )
    assert edit_res["applied"] is False
    assert edit_res["mutation"]["after"]["life"] > edit_res["mutation"]["before"]["life"]

    # 4. item db search + what-if equip
    db_search = _run_pob("item", "db", "Mageblood", "--limit", "5", runtime_dir=e2e_runtime_dir)
    assert db_search["equipped"] is False
    assert len(db_search["matches"]) >= 1

    db_equip = _run_pob("item", "db", "Mageblood", "--equip", "--limit", "5", runtime_dir=e2e_runtime_dir)
    assert db_equip["equipped"] is True
    assert db_equip["applied"] is False

    # 5. tree set-class (0-residue what-if)
    cls_res = _run_pob("tree", "set-class", "Templar", "Inquisitor", runtime_dir=e2e_runtime_dir)
    assert cls_res["applied"] is False
    assert cls_res["detail"]["after"]["ascendClassName"] == "Inquisitor"

    # 6. matrix (default 4 scenarios, 0-residue)
    mat_res = _run_pob("matrix", runtime_dir=e2e_runtime_dir)
    sc_names = [s["name"] for s in mat_res["scenarios"]]
    assert sc_names == ["Original", "Pinnacle", "Uber", "FlasksOff"]

    # Verify zero residue after all what-if operations
    sum_after_ops = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    assert sum_after_ops["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)

    # 7. Bad build deadlock self-healing (Gate 4: treeVersion="9_99" must fail cleanly and preserve existing build)
    bad_xml = '<PathOfBuilding><Build level="90" className="Witch"/><Tree activeSpec="1"><Spec treeVersion="9_99"/></Tree></PathOfBuilding>'
    bad_code = xml_to_share_code(bad_xml)
    proc_bad_load = _run_pob_proc("load", bad_code, runtime_dir=e2e_runtime_dir)
    assert proc_bad_load.returncode != 0
    assert "9_99" in (proc_bad_load.stderr + proc_bad_load.stdout)

    sum_healed = _run_pob("summary", runtime_dir=e2e_runtime_dir)
    assert sum_healed["offence"]["CombinedDPS"] == pytest.approx(dps_before, rel=1e-6)

    # 8. Local 0ms game data CLI commands (Gate 5)
    mod_res = _run_pob("data", "mod", "Life Converted to Energy Shield", runtime_dir=e2e_runtime_dir)
    assert mod_res["count"] >= 1
    gem_res = _run_pob("data", "gem", "Dark Bargain", runtime_dir=e2e_runtime_dir)
    assert gem_res["count"] >= 1
    tr_res = _run_pob("data", "translate", "喋血之刃", runtime_dir=e2e_runtime_dir)
    assert tr_res["count"] >= 1


def test_e2e_v02_degraded_network_alert_and_bootstrap_missing(tmp_path: Path) -> None:
    """Verify Gate 1: a dead POB_PROXY degrades fast with dual-channel warnings, and bootstrap_missing fails fast."""
    degraded_rt = tmp_path / "degraded_rt"
    dead_proxy_env: dict[str, str | None] = {
        "POB_PROXY": dead_proxy_url(),
        "POB_OFFLINE": None,
    }
    try:
        t0 = time.monotonic()
        proc_json = _run_pob_proc("status", "--json", runtime_dir=degraded_rt, extra_env=dead_proxy_env, check=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, f"Degraded daemon cold start took too long: {elapsed:.2f}s"

        data = json.loads(proc_json.stdout)
        assert data["updateStatus"]["state"] == "degraded_network_error"
        assert data.get("updateWarning")
        assert "--proxy" in data["updateWarning"] or "HTTPS_PROXY" in data["updateWarning"]
        # Stderr alert printed once cleanly on cold start + status
        assert proc_json.stderr.count("[pob 告警") == 1
        assert "--proxy" in proc_json.stderr

        # Warm markdown status must also show > [WARN] banner and single stderr alert
        proc_md = _run_pob_proc("status", runtime_dir=degraded_rt, extra_env=dead_proxy_env, check=True)
        assert proc_md.stdout.startswith("> [WARN]")
        assert proc_md.stderr.count("[pob 告警") == 1

        # Summary JSON must include updateWarning
        sum_data = _run_pob("summary", runtime_dir=degraded_rt, extra_env=dead_proxy_env)
        assert sum_data.get("updateWarning")

        # Load failure while degraded must append agent guidance and print stderr alert
        bad_code = xml_to_share_code('<PathOfBuilding><Tree><Spec treeVersion="9_99"/></Tree></PathOfBuilding>')
        proc_load_fail = _run_pob_proc("load", bad_code, runtime_dir=degraded_rt, extra_env=dead_proxy_env)
        assert proc_load_fail.returncode != 0
        combined_err = proc_load_fail.stderr + proc_load_fail.stdout
        assert "pob update" in combined_err
        assert "R5" in combined_err
        assert "[pob 告警" in proc_load_fail.stderr
    finally:
        try:
            _run_pob_proc("stop", runtime_dir=degraded_rt, extra_env=dead_proxy_env, timeout=10.0)
        except Exception:
            pass

    # Verify bootstrap_missing fast-fails cleanly without hanging 15s
    missing_rt = tmp_path / "missing_rt"
    missing_env: dict[str, str | None] = {
        "POB_ROOT": "",
        "POB_LEGACY_DIR": "",
        "POB_OFFLINE": "1",
    }
    t0 = time.monotonic()
    proc_missing = _run_pob_proc("status", runtime_dir=missing_rt, extra_env=missing_env, timeout=15.0)
    elapsed_missing = time.monotonic() - t0
    assert proc_missing.returncode == 1
    assert elapsed_missing < 8.0
    assert "bootstrap" in proc_missing.stderr.lower()
    assert "--from-dir" in proc_missing.stderr
