"""Reverse PoE2 -> PoE1 cross-game anti-hallucination guardrail tests (<0.5s, daemon-free)."""

from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path
from socketserver import TCPServer
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import (
    daemon,
    external_data,
    importers,
    mutators,
    poedb_client,
    updater,
)
from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import _prepare_load_source, main
from pob_cli.errors import CrossGameHallucinationError, DaemonRequestError
from pob_cli.formatters import to_json_str
from pob_cli.guardrails import (
    GuardrailScope,
    check_input,
    check_input_item_raw,
    extract_scannable_item_parts,
    validate_build_xml,
    validate_engine_root,
    validate_url,
)
from pob_cli.importers import xml_to_share_code


def _extract_json_from_cli_output(output: str) -> dict[str, Any]:
    """Extract the JSON object emitted to stdout when CliRunner mixes stderr + stdout."""
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = output.find("{", idx)
        if start == -1:
            break
        try:
            obj, _ = decoder.raw_decode(output[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = start + 1
    raise AssertionError(f"Expected JSON object in CLI output, got:\n{output}")


# ---------------------------------------------------------------------------
# 1. Error contract & formatters invariant
# ---------------------------------------------------------------------------


def test_error_contract_and_formatters_untouched() -> None:
    exc = CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger="Waystone",
        error="Waystones are PoE2 endgame items.",
        remediation="Use Maps in PoE1.",
        equivalent_command="pob price Map --category map",
        rule_id="POE2_WAYSTONE",
    )
    assert isinstance(exc, DaemonRequestError)
    payload = exc.to_dict()
    assert next(iter(payload.keys())) == "game"
    assert payload == {
        "game": "poe1",
        "ok": False,
        "errorKind": "cross_game_hallucination",
        "detectedGame": "poe2",
        "trigger": "Waystone",
        "error": "Waystones are PoE2 endgame items.",
        "remediation": "Use Maps in PoE1.",
        "equivalentCommand": "pob price Map --category map",
        "ruleId": "POE2_WAYSTONE",
    }
    restored = CrossGameHallucinationError.from_dict(payload)
    assert restored.to_dict() == payload
    assert "[POE1-CROSS-GAME-BLOCK]" in exc.format_human()

    # Normal PoE1 JSON outputs must NOT be stamped with "game"
    normal = json.loads(to_json_str({"ok": True, "dps": 12345}))
    assert "game" not in normal


# ---------------------------------------------------------------------------
# 2. Gate 1: Build XML & Share-Code Gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xml_body", "expected_rule"),
    [
        (
            '<?xml version="1.0" encoding="UTF-8"?><PathOfBuilding2><Build level="90" targetVersion="0_1" className="Witch"/><Tree><Spec treeVersion="0_5"/></Tree></PathOfBuilding2>',
            "XML_POE2_ROOT_IN_POB1",
        ),
        (
            '<PathOfBuilding><Build level="90" className="Witch"/><Tree><Spec treeVersion="0_5"/></Tree></PathOfBuilding>',
            "XML_POE2_TREE_IN_POB1",
        ),
        (
            '<PathOfBuilding><Build level="90" targetVersion="0_1" className="Witch"/></PathOfBuilding>',
            "XML_POE2_TREE_IN_POB1",
        ),
        (
            '<PathOfBuilding><Build level="90" className="Monk"/></PathOfBuilding>',
            "XML_POE2_CLASS_IN_POB1",
        ),
        (
            '<PathOfBuilding><Build level="90" className="Witch"/><Items><Slot name="Charm 1" itemId="1"/></Items></PathOfBuilding>',
            "XML_POE2_SLOT_IN_POB1",
        ),
    ],
)
def test_validate_build_xml_blocks_poe2_builds(xml_body: str, expected_rule: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as ei:
        validate_build_xml(xml_body, expected_game="poe1")
    assert ei.value.game == "poe1"
    assert ei.value.detected_game == "poe2"
    assert ei.value.rule_id == expected_rule
    assert ei.value.equivalent_command and ei.value.equivalent_command.startswith("pob2 load")


def test_validate_build_xml_allows_poe1_and_strips_notes() -> None:
    poe1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<PathOfBuilding>'
        '<Build level="98" targetVersion="3_0" className="Witch" ascendClassName="Necromancer"/>'
        '<Tree><Spec treeVersion="3_29"/></Tree>'
        '<Items><Slot name="Flask 4" itemId="1"/></Items>'
        '<Notes>Compared against &lt;PathOfBuilding2&gt; treeVersion="0_5" className="Monk" Slot name="Charm 1"</Notes>'
        "</PathOfBuilding>"
    )
    validate_build_xml(poe1_xml, expected_game="poe1")


def test_cli_load_blocks_poe2_xml_file_and_share_code_with_json_exit_1(tmp_path: Path) -> None:
    poe2_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<PathOfBuilding2><Build level="92" targetVersion="0_1" className="Sorceress"/>'
        '<Tree><Spec treeVersion="0_5"/></Tree></PathOfBuilding2>'
    )
    xml_file = tmp_path / "poe2_build.xml"
    xml_file.write_text(poe2_xml, encoding="utf-8")

    # Local XML file -> CLI exits 1 with structured JSON
    res = CliRunner().invoke(main, ["load", str(xml_file), "--json"])
    assert res.exit_code == 1, res.output
    assert "[POE1-CROSS-GAME-BLOCK]" in res.output
    payload = _extract_json_from_cli_output(res.output)
    assert payload["game"] == "poe1"
    assert payload["detectedGame"] == "poe2"
    assert payload["errorKind"] == "cross_game_hallucination"
    assert payload["ruleId"] == "XML_POE2_ROOT_IN_POB1"

    # Base64 share code containing PoE2 XML -> blocked at _prepare_load_source
    poe2_code = xml_to_share_code(poe2_xml)
    with pytest.raises(CrossGameHallucinationError) as ei:
        _prepare_load_source(poe2_code)
    assert ei.value.rule_id == "XML_POE2_ROOT_IN_POB1"


def test_bridge_worker_and_importers_block_poe2_share_code_before_engine() -> None:
    poe2_xml = '<PathOfBuilding2><Build level="85" className="Warrior"/><Tree><Spec treeVersion="0_5"/></Tree></PathOfBuilding2>'
    poe2_code = xml_to_share_code(poe2_xml)

    worker = BridgeWorker(slot_name="test")

    class _FakeProc:
        stdin = object()

        def poll(self) -> None:
            return None

    worker._proc = _FakeProc()  # type: ignore[assignment]

    with pytest.raises(CrossGameHallucinationError) as ei1:
        worker.call("import_code", {"code": poe2_code, "mode": "replace"})
    assert ei1.value.rule_id == "XML_POE2_ROOT_IN_POB1"

    with pytest.raises(CrossGameHallucinationError) as ei2:
        worker.call_batch([("import_code", {"code": poe2_code, "mode": "replace"})])
    assert ei2.value.rule_id == "XML_POE2_ROOT_IN_POB1"

    # Domain-neutral pobb.in returning PoE2 share code -> blocked before worker.call("import_code")
    class _FakeUrlWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.called_methods: list[str] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.called_methods.append(method)
            if method == "import_from_url":
                return {"code": poe2_code}
            raise AssertionError(f"import_code must NOT be reached for PoE2 XML (got {method})")

    fake_w = _FakeUrlWorker()
    with pytest.raises(CrossGameHallucinationError) as ei3:
        importers.load_build_into_worker(fake_w, source="https://pobb.in/abc123")  # type: ignore[arg-type]
    assert ei3.value.rule_id == "XML_POE2_ROOT_IN_POB1"
    assert fake_w.called_methods == ["import_from_url"]


# ---------------------------------------------------------------------------
# 3. Gate 2: URL Domain & Path Gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected_rule"),
    [
        ("https://poe2db.tw/cn/Quarterstaff", "URL_POE2_POE2DB"),
        ("https://cdn.poe2db.tw/json/test.json", "URL_POE2_POE2DB"),
        ("https://poe.ninja/poe2/builds/dawn/character/Acc-1234/Hero", "URL_POE2_NINJA"),
        ("https://poe2.ninja/economy", "URL_POE2_NINJA"),
        ("https://www.pathofexile.com/trade2/search/poe2/Standard", "URL_POE2_TRADE2"),
        ("https://www.pathofexile.com/api/trade2/search/poe2/Standard", "URL_POE2_TRADE2"),
        ("https://www.poe2wiki.net/wiki/Spirit", "URL_POE2_WIKI"),
        ("https://maxroll.gg/poe2/pob/abc123", "URL_POE2_MAXROLL"),
        ("https://mobalytics.gg/poe-2/builds/monk", "URL_POE2_MOBALYTICS"),
        ("pob2://pobbin/abc123", "URL_POE2_URI_SCHEME"),
    ],
)
def test_validate_url_blocks_poe2_urls(url: str, expected_rule: str) -> None:
    with pytest.raises(CrossGameHallucinationError) as ei:
        validate_url(url, expected_game="poe1")
    assert ei.value.rule_id == expected_rule
    assert ei.value.game == "poe1"
    assert ei.value.detected_game == "poe2"


@pytest.mark.parametrize(
    "url",
    [
        "https://poedb.tw/cn/Mageblood",
        "https://poe.ninja/poe1/builds/mirage/character/Exile-1234/Hero",
        "https://poe.ninja/builds/mirage",
        "https://www.pathofexile.com/trade/search/Standard",
        "https://www.poewiki.net/wiki/Armour",
        "https://maxroll.gg/poe/pob/abc123",
        "https://mobalytics.gg/poe/builds/necro",
        "pob://pobbin/abc123",
        "https://pobb.in/abc123",
    ],
)
def test_validate_url_allows_poe1_and_neutral_urls(url: str) -> None:
    validate_url(url, expected_game="poe1")


def test_cli_load_and_external_clients_block_poe2_urls() -> None:
    res = CliRunner().invoke(main, ["load", "https://poe.ninja/poe2/builds/dawn", "--json"])
    assert res.exit_code == 1, res.output
    payload = _extract_json_from_cli_output(res.output)
    assert payload["errorKind"] == "cross_game_hallucination"
    assert payload["ruleId"] == "URL_POE2_NINJA"

    with pytest.raises(CrossGameHallucinationError):
        poedb_client.query_poedb("https://poe2db.tw/us/Quarterstaff")
    with pytest.raises(CrossGameHallucinationError):
        poedb_client.query_poedb_multi(["https://poe2db.tw/us/Quarterstaff"])
    with pytest.raises(CrossGameHallucinationError):
        external_data.query_wiki("https://www.poe2wiki.net/wiki/Spirit")
    with pytest.raises(CrossGameHallucinationError):
        external_data.query_wiki_multi(["https://www.poe2wiki.net/wiki/Spirit"])
    with pytest.raises(CrossGameHallucinationError):
        external_data.query_price("https://poe.ninja/poe2/economy")


# ---------------------------------------------------------------------------
# 4. Gate 3: Engine Root & Triple-Lock Daemon Isolation
# ---------------------------------------------------------------------------


def test_engine_root_guard_blocks_poe2_only_allows_dual_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poe2_only = tmp_path / "poe2_only"
    (poe2_only / "PoE2").mkdir(parents=True)

    with pytest.raises(CrossGameHallucinationError) as ei:
        validate_engine_root(poe2_only, expected_game="poe1")
    assert ei.value.rule_id == "ENV_POE2_ENGINE_IN_POB1"

    monkeypatch.setenv("POB_ROOT", str(poe2_only))
    with pytest.raises(CrossGameHallucinationError):
        updater.resolve_engine_root(runtime_dir=tmp_path / "rt")

    with pytest.raises(CrossGameHallucinationError):
        updater.bootstrap_engine(from_dir=poe2_only, runtime_dir=tmp_path / "rt")

    # Dual-game dir (PoE1 + PoE2) is valid
    dual = tmp_path / "dual_game"
    (dual / "PoE1").mkdir(parents=True)
    (dual / "PoE2").mkdir(parents=True)
    validate_engine_root(dual, expected_game="poe1")


def test_daemon_non_destructive_poe2_guard_and_legacy_none_compat(tmp_path: Path) -> None:
    rt = tmp_path / "rt"
    rt.mkdir()
    state_file = rt / "daemon.json"

    # PoE2 daemon.json MUST raise CrossGameHallucinationError AND NOT delete daemon.json
    poe2_state = {"game": "poe2", "pid": 999_999_999, "port": 59999, "token": "poe2-tok"}
    state_file.write_text(json.dumps(poe2_state), encoding="utf-8")

    with pytest.raises(CrossGameHallucinationError) as ei1:
        daemon.call_daemon("status", auto_start=False, runtime_dir=rt)
    assert ei1.value.rule_id == "DAEMON_STATE_GAME_MISMATCH"
    assert state_file.exists(), "Foreign poe2 daemon.json must NEVER be unlinked!"

    with pytest.raises(CrossGameHallucinationError) as ei2:
        daemon.stop_daemon(runtime_dir=rt)
    assert ei2.value.rule_id == "DAEMON_STATE_GAME_MISMATCH"
    assert state_file.exists(), "stop_daemon must NEVER unlink foreign poe2 daemon.json!"

    # Legacy PoE1 daemon.json omitting "game" (None) is treated as "poe1" (NOT blocked!)
    legacy_state = {"pid": 999_999_999, "port": 59999, "token": "legacy-tok"}
    state_file.write_text(json.dumps(legacy_state), encoding="utf-8")
    stop_res = daemon.stop_daemon(runtime_dir=rt)
    assert stop_res == {"stopped": False, "staleCleaned": True}
    assert not state_file.exists()


def test_daemon_rpc_cross_header_and_error_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _GuardrailManager:
        token = "poe1-secret"

        def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            if action == "status":
                return {"game": "poe1", "pid": os.getpid()}
            raise CrossGameHallucinationError(
                game="poe1",
                detected_game="poe2",
                trigger="Spirit",
                error="Spirit does not exist in PoE1.",
                remediation="Query Mana Reservation instead.",
                equivalent_command='pob calc "Mana Reservation"',
                rule_id="POE2_SPIRIT_RESERVATION",
            )

    server = TCPServer(("127.0.0.1", 0), daemon.make_handler(_GuardrailManager()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]

        # 1. X-PoB2-Token cross-header -> HTTP 400 CrossGameHallucinationError (not 403)
        body_bytes = json.dumps({"action": "status", "params": {}}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            conn.request(
                "POST",
                "/rpc",
                body=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body_bytes)),
                    "X-PoB2-Token": "some-poe2-token",
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        assert resp.status == 400
        assert data["errorKind"] == "cross_game_hallucination"
        assert data["game"] == "poe1"
        assert data["detectedGame"] == "poe2"
        assert data["ruleId"] == "DAEMON_CROSS_GAME_TOKEN_HEADER"

        # 2. CrossGameHallucinationError serialized over HTTP & deserialized by call_daemon
        monkeypatch.setattr(
            daemon,
            "read_daemon_state",
            lambda *a, **k: {"game": "poe1", "pid": os.getpid(), "port": port, "token": "poe1-secret"},
        )
        with pytest.raises(CrossGameHallucinationError) as ei:
            daemon.call_daemon("calc", {"keyword": "Spirit"}, auto_start=False, runtime_dir=tmp_path)
        assert ei.value.rule_id == "POE2_SPIRIT_RESERVATION"
        assert ei.value.equivalent_command == 'pob calc "Mana Reservation"'

        # 3. Normal status returns game="poe1"
        st = daemon.call_daemon("status", {}, auto_start=False, runtime_dir=tmp_path, emit_cold_start_alert=False)
        assert st["game"] == "poe1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 5. Gate 4: Reverse CLI Trap Commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_rule", "expected_equiv"),
    [
        (["rune", "list", "--slot", "helmet", "--json"], "CLI_TRAP_RUNE_IN_POB1", "pob bench list --slot helmet"),
        (
            ["rune", "suggest", "--slot", "ring1", "--stat", "Cold Resistance", "--json"],
            "CLI_TRAP_RUNE_IN_POB1",
            'pob bench suggest --slot ring1 --stat "Cold Resistance"',
        ),
        (
            ["instill", "--notable", "Charisma", "--json"],
            "CLI_TRAP_INSTILL_IN_POB1",
            'pob item anoint --notable "Charisma"',
        ),
        (
            ["item", "instill", "--slot", "Amulet", "--notable", "Whispers of Doom", "--json"],
            "CLI_TRAP_ITEM_INSTILL_IN_POB1",
            'pob item anoint --slot Amulet --notable "Whispers of Doom"',
        ),
        (
            ["item", "charm", "1", "on", "--json"],
            "CLI_TRAP_ITEM_CHARM_IN_POB1",
            "pob item flask 1 on",
        ),
    ],
)
def test_reverse_cli_trap_commands(argv: list[str], expected_rule: str, expected_equiv: str) -> None:
    res = CliRunner().invoke(main, argv)
    assert res.exit_code == 1, res.output
    assert "[POE1-CROSS-GAME-BLOCK]" in res.output
    payload = _extract_json_from_cli_output(res.output)
    assert payload["game"] == "poe1"
    assert payload["detectedGame"] == "poe2"
    assert payload["errorKind"] == "cross_game_hallucination"
    assert payload["ruleId"] == expected_rule
    assert payload["equivalentCommand"] == expected_equiv


# ---------------------------------------------------------------------------
# 6. Table D Hard-Blocks across CLI & Modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_rule"),
    [
        (["calc", "Spirit", "--json"], "POE2_SPIRIT_RESERVATION"),
        (["calc", "精魂保留", "--json"], "POE2_SPIRIT_RESERVATION"),
        (["items", "--slot", "Charm 1", "--json"], "POE2_CHARM_BELT_SLOTS"),
        (["items", "--slot", "Focus", "--json"], "POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"),
        (["items", "--slot", "Quarterstaff", "--json"], "POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"),
        (["skill", "add", "--gem", "Uncut Skill Gem", "--json"], "POE2_UNCUT_GEMS"),
        (["item", "anoint", "--notable", "Distilled Isolation", "--json"], "POE2_DISTILLED_EMOTIONS"),
        (["item", "anoint", "--notable", "涂灌", "--json"], "POE2_DISTILLED_EMOTIONS"),
        (["data", "gem", "Uncut Spirit Gem", "--json"], "POE2_UNCUT_GEMS"),
        (["data", "gem", "未切割的技能宝石", "--json"], "POE2_UNCUT_GEMS"),
        (["data", "base", "Waystone", "--json"], "POE2_WAYSTONE"),
        (["data", "unique", "引路石", "--json"], "POE2_WAYSTONE"),
        (["data", "base", "Wrapped Quarterstaff", "--json"], "POE2_WEAPONS_AND_FOCUS_OFFHAND"),
        (["data", "base", "Crossbow", "--json"], "POE2_WEAPONS_AND_FOCUS_OFFHAND"),
        (["trade", "search", "--slot", "focus", "--json"], "POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"),
        (["trade", "search", "--slot", "quarterstaff", "--json"], "POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"),
        (["trade", "search", "--slot", "buckler", "--json"], "POE2_BUCKLER_SLOT_ONLY"),
        (["trade", "search", "--category", "armour.buckler", "--json"], "POE2_WEAPONS_AND_FOCUS_OFFHAND"),
        (["trade", "search", "--slot", "charm1", "--json"], "POE2_CHARM_BELT_SLOTS"),
        (["trade", "search", "--category", "armour.focus", "--json"], "POE2_WEAPONS_AND_FOCUS_OFFHAND"),
        (["trade", "stats", "maximum Spirit", "--json"], "POE2_SPIRIT_RESERVATION"),
        (["bench", "list", "--slot", "focus", "--json"], "POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY"),
        (["config", "set", "totalSpirit=100", "--json"], "POE2_SPIRIT_RESERVATION"),
        (["price", "Waystone", "--json"], "POE2_WAYSTONE"),
        (["poedb", "Soul Core", "--json"], "POE2_SOUL_CORES_AND_RUNE_MODS"),
        (["wiki", "Distilled Fear", "--json"], "POE2_DISTILLED_EMOTIONS"),
    ],
)
def test_table_d_cli_hard_blocks(argv: list[str], expected_rule: str) -> None:
    res = CliRunner().invoke(main, argv)
    assert res.exit_code == 1, res.output
    payload = _extract_json_from_cli_output(res.output)
    assert payload["game"] == "poe1"
    assert payload["detectedGame"] == "poe2"
    assert payload["errorKind"] == "cross_game_hallucination"
    assert payload["ruleId"] == expected_rule


def test_item_raw_blocks_poe2_rune_tag_soul_core_and_charm_mods() -> None:
    rune_item = (
        "Rarity: RARE\n"
        "Storm Crest\n"
        "Hubris Circlet\n"
        "Implicits: 0\n"
        "{rune}+12% to Fire Resistance\n"
        "+80 to maximum Life\n"
    )
    with pytest.raises(CrossGameHallucinationError) as ei:
        check_input_item_raw(rune_item, expected_game="poe1")
    assert ei.value.rule_id == "POE2_SOUL_CORES_AND_RUNE_MODS"

    belt_with_charms = (
        "Rarity: RARE\n"
        "Dread Buckle\n"
        "Heavy Belt\n"
        "Implicits: 1\n"
        "Has 2 Charm Slots\n"
        "+70 to maximum Life\n"
    )
    with pytest.raises(CrossGameHallucinationError) as ei2:
        check_input_item_raw(belt_with_charms, expected_game="poe1")
    assert ei2.value.rule_id == "POE2_CHARM_BELT_SLOTS"

    # CLI `pob item try` blocks `{rune}` item text before touching daemon
    res = CliRunner().invoke(main, ["item", "try", "--slot", "Helmet", "--raw", rune_item, "--json"])
    assert res.exit_code == 1, res.output
    assert _extract_json_from_cli_output(res.output)["ruleId"] == "POE2_SOUL_CORES_AND_RUNE_MODS"


# ---------------------------------------------------------------------------
# 7. Zero False-Positive Invariants (CRITICAL INVARIANT #3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        # Focus mechanics, gems & jewels in PoE1
        ("Elemental Focus", GuardrailScope.GEM_NAME),
        ("Elemental Focus Support", GuardrailScope.GEM_NAME),
        ("Elemental Focus", GuardrailScope.DATA_QUERY),
        ("Combat Focus", GuardrailScope.DATA_QUERY),
        ("Combat Focus", GuardrailScope.ITEM_RAW),
        ("while Focused", GuardrailScope.CALC_QUERY),
        ("while Focused", GuardrailScope.STAT_QUERY),
        ("while Focused", GuardrailScope.CONFIG_VAR),
        ("专注", GuardrailScope.CALC_QUERY),
        ("专注", GuardrailScope.STAT_QUERY),
        ("专注", GuardrailScope.CONFIG_VAR),
        ("Focus", GuardrailScope.CALC_QUERY),
        ("Focus", GuardrailScope.GEM_NAME),
        ("Focus", GuardrailScope.STAT_QUERY),
        # Spirit Shield / Summon Raging Spirit / Spirit Offering in PoE1
        ("Titanium Spirit Shield", GuardrailScope.DATA_QUERY),
        ("Titanium Spirit Shield", GuardrailScope.ITEM_RAW),
        ("Titanium Spirit Shield", GuardrailScope.STAT_QUERY),
        ("Titanium Spirit Shield", GuardrailScope.CALC_QUERY),
        ("灵盾", GuardrailScope.DATA_QUERY),
        ("精魂之盾", GuardrailScope.CALC_QUERY),
        ("精魂之盾", GuardrailScope.STAT_QUERY),
        ("Summon Raging Spirit", GuardrailScope.GEM_NAME),
        ("Summon Raging Spirit", GuardrailScope.CALC_QUERY),
        ("Summon Raging Spirit", GuardrailScope.STAT_QUERY),
        ("Summon Raging Spirit", GuardrailScope.DATA_QUERY),
        ("Spirit Offering", GuardrailScope.GEM_NAME),
        ("Spirit Offering", GuardrailScope.CALC_QUERY),
        ("Spirit Offering", GuardrailScope.STAT_QUERY),
        ("+2 to Level of all Summon Raging Spirit Gems", GuardrailScope.STAT_QUERY),
        # Distilled Perfection notable & Ancient Waystones
        ("Distilled Perfection", GuardrailScope.NOTABLE_OR_OIL),
        ("Distilled Perfection", GuardrailScope.DATA_QUERY),
        ("Distilled Perfection", GuardrailScope.ITEM_RAW),
        ("Ancient Waystones", GuardrailScope.DATA_QUERY),
        ("Ancient Waystones", GuardrailScope.STAT_QUERY),
        ("Ancient Waystones", GuardrailScope.ITEM_RAW),
        # PoE1 Lv45 Staff base "Quarterstaff" outside SLOT / CATEGORY
        ("Quarterstaff", GuardrailScope.DATA_QUERY),
        ("Quarterstaff", GuardrailScope.ITEM_RAW),
        ("Quarterstaff", GuardrailScope.STAT_QUERY),
        ("Quarterstaff", GuardrailScope.CALC_QUERY),
        # PoE1 shield bases (Goathide, Crusader ... Buckler) outside SLOT / CATEGORY
        ("buckler", GuardrailScope.DATA_QUERY),
        ("Crusader Buckler", GuardrailScope.DATA_QUERY),
        ("Buckler", GuardrailScope.ITEM_RAW),
    ],
)
def test_zero_false_positives_for_poe1_terms(text: str, scope: GuardrailScope) -> None:
    check_input(text, scope=scope, expected_game="poe1")


def test_item_raw_allows_poe1_quarterstaff_spirit_shield_and_random_rare_title() -> None:
    poe1_spirit_shield = (
        "Rarity: RARE\n"
        "Waystone Ward\n"
        "Titanium Spirit Shield\n"
        "Item Level: 86\n"
        "Implicits: 1\n"
        "10% increased Spell Damage\n"
        "+120 to maximum Energy Shield\n"
        "+40% to Cold Resistance\n"
        "Trigger a Socketed Spell when you Focus\n"
    )
    scannable, base_line = extract_scannable_item_parts(poe1_spirit_shield)
    assert "Waystone Ward" not in scannable
    assert base_line == "Titanium Spirit Shield"
    check_input_item_raw(poe1_spirit_shield, expected_game="poe1")

    poe1_quarterstaff = (
        "Rarity: RARE\n"
        "Crossbow Bane\n"
        "Quarterstaff\n"
        "Implicits: 1\n"
        "+18% Chance to Block Attack Damage while wielding a Staff\n"
        "+120% increased Physical Damage\n"
    )
    check_input_item_raw(poe1_quarterstaff, expected_game="poe1")


# ---------------------------------------------------------------------------
# 8. Wave 4 Review Regressions (timeless, bootstrap, run_sweep, classes, shutdown_all)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jewel_name",
    [
        "Undying Hate",
        "不死之恨",
        "不朽之恨",
        "Controlled Metamorphosis",
        "受控的蜕变",
        "Against the Darkness",
        "力抗黑暗",
        "Prism of Belief",
        "信仰棱镜",
        "From Nothing",
        "无中生有",
        "英雄的悲歌",
    ],
)
def test_timeless_search_cli_blocks_poe2_jewels_daemon_free(jewel_name: str) -> None:
    runner = CliRunner()
    res = runner.invoke(
        main,
        ["timeless", "search", "--jewel", jewel_name, "--socket", "26725", "--json"],
    )
    assert res.exit_code == 1, res.output
    data = _extract_json_from_cli_output(res.output)
    assert data["errorKind"] == "cross_game_hallucination"
    assert data["game"] == "poe1"
    assert data["detectedGame"] == "poe2"
    assert data["ruleId"] == "POE2_TIMELESS_JEWEL_IN_POB1"


def test_timeless_search_cli_blocks_poe2_rune_mod() -> None:
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "timeless",
            "search",
            "--jewel",
            "Lethal Pride",
            "--socket",
            "26725",
            "--mod",
            "{rune}+12% to Fire Resistance",
            "--json",
        ],
    )
    assert res.exit_code == 1, res.output
    data = _extract_json_from_cli_output(res.output)
    assert data["errorKind"] == "cross_game_hallucination"
    assert data["ruleId"] == "POE2_SOUL_CORES_AND_RUNE_MODS"


@pytest.mark.parametrize(
    "poe1_jewel",
    [
        "Heroic Tragedy",
        "英勇悲剧",
        "英勇的悲剧",
        "Lethal Pride",
        "Brutal Restraint",
        "Militant Faith",
        "Elegant Hubris",
        "Glorious Vanity",
    ],
)
def test_timeless_guard_zero_false_positive_for_poe1_jewels(poe1_jewel: str) -> None:
    from pob_cli.guardrails import check_poe2_timeless_jewel

    check_poe2_timeless_jewel(poe1_jewel, expected_game="poe1")


def test_timeless_cli_propagates_cross_game_error_from_foreign_daemon_and_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "daemon.json"
    state_file.write_text(
        json.dumps({"game": "poe2", "pid": 999999, "port": 59999, "token": "poe2-tok"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))

    runner = CliRunner()
    res_list = runner.invoke(main, ["timeless", "list", "--json"])
    assert res_list.exit_code == 1, res_list.output
    data_list = _extract_json_from_cli_output(res_list.output)
    assert data_list["errorKind"] == "cross_game_hallucination"
    assert data_list["ruleId"] == "DAEMON_STATE_GAME_MISMATCH"
    assert state_file.exists()

    res_search = runner.invoke(
        main,
        ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725", "--json"],
    )
    assert res_search.exit_code == 1, res_search.output
    data_search = _extract_json_from_cli_output(res_search.output)
    assert data_search["errorKind"] == "cross_game_hallucination"
    assert data_search["ruleId"] == "DAEMON_STATE_GAME_MISMATCH"

    # Also verify `search()` itself propagating `ENV_POE2_ENGINE_IN_POB1` through `cmd_timeless_search`
    from pob_cli import cli_timeless as cli_timeless_mod

    poe2_only_root = tmp_path / "poe2_only"
    (poe2_only_root / "PoE2" / "Data").mkdir(parents=True)
    monkeypatch.setattr(
        cli_timeless_mod,
        "_daemon_call",
        lambda _op, _params: {
            "code": "DUMMY_CODE",
            "xml": "<PathOfBuilding/>",
            "pobDir": str(poe2_only_root),
        },
    )
    res_engine = runner.invoke(
        main,
        ["timeless", "search", "--jewel", "Lethal Pride", "--socket", "26725", "--json"],
    )
    assert res_engine.exit_code == 1, res_engine.output
    data_engine = _extract_json_from_cli_output(res_engine.output)
    assert data_engine["errorKind"] == "cross_game_hallucination"
    assert data_engine["ruleId"] == "ENV_POE2_ENGINE_IN_POB1"


def test_timeless_resolve_game_dir_blocks_poe2_only_and_poe2_leaf(tmp_path: Path) -> None:
    from pob_cli.timeless import _resolve_game_dir

    poe2_root = tmp_path / "pob2_only"
    (poe2_root / "PoE2" / "Data").mkdir(parents=True)
    with pytest.raises(CrossGameHallucinationError) as ei1:
        _resolve_game_dir(pob_dir=poe2_root)
    assert ei1.value.rule_id == "ENV_POE2_ENGINE_IN_POB1"

    with pytest.raises(CrossGameHallucinationError) as ei2:
        _resolve_game_dir(pob_dir=poe2_root / "PoE2")
    assert ei2.value.rule_id == "ENV_POE2_ENGINE_IN_POB1"


def test_bootstrap_cli_propagates_foreign_poe2_daemon_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "daemon.json"
    state_file.write_text(
        json.dumps({"game": "poe2", "pid": 999999, "port": 59999, "token": "poe2-tok"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path))

    runner = CliRunner()
    res = runner.invoke(main, ["bootstrap", "--json"])
    assert res.exit_code == 1, res.output
    data = _extract_json_from_cli_output(res.output)
    assert data["errorKind"] == "cross_game_hallucination"
    assert data["ruleId"] == "DAEMON_STATE_GAME_MISMATCH"
    assert state_file.exists()


def test_run_sweep_propagates_cross_game_error_and_restores_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored_codes: list[str] = []

    class _FakeSweepWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.last_good_code = "ORIG_BUILD_CODE"

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "export_code":
                return {"code": "ORIG_BUILD_CODE"}
            if method == "import_code":
                restored_codes.append(str((params or {}).get("code") or ""))
                return {"ok": True}
            return {}

    monkeypatch.setattr(
        mutators,
        "capture_digest",
        lambda _w: {"offence": {}, "defence": {}, "warnings": []},
    )
    worker = _FakeSweepWorker()
    with pytest.raises(CrossGameHallucinationError) as ei:
        mutators.run_sweep(
            worker,  # type: ignore[arg-type]
            variants=[
                {
                    "name": "poe2-charm",
                    "ops": [{"op": "item", "slot": "Charm 1", "raw": "Rarity: MAGIC\nThawing Charm"}],
                }
            ],
        )
    assert ei.value.rule_id == "POE2_CHARM_BELT_SLOTS"
    assert "ORIG_BUILD_CODE" in restored_codes


@pytest.mark.parametrize(
    "args",
    [
        ["new", "--class", "Monk", "--json"],
        ["new", "--class", "武僧", "--json"],
        ["new", "--asc", "Stormweaver", "--json"],
        ["new", "--asc", "血法师", "--json"],
        ["tree", "set-class", "Sorceress", "--json"],
        ["tree", "set-class", "Witch", "Blood Mage", "--json"],
        ["tree", "set-class", "--secondary-asc", "Chronomancer", "--json"],
    ],
)
def test_cli_blocks_poe2_classes_and_ascendancies_daemon_free(args: list[str]) -> None:
    runner = CliRunner()
    res = runner.invoke(main, args)
    assert res.exit_code == 1, res.output
    data = _extract_json_from_cli_output(res.output)
    assert data["errorKind"] == "cross_game_hallucination"
    assert data["game"] == "poe1"
    assert data["detectedGame"] == "poe2"
    assert data["ruleId"] == "POE2_CLASS_IN_POB1"


def test_mutators_block_poe2_class_and_allow_poe1_shared_classes() -> None:
    from pob_cli.guardrails import check_poe2_class_or_ascendancy

    class _ExplodingWorker:
        def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Worker must not be called for PoE2 class/ascendancy")

    with pytest.raises(CrossGameHallucinationError) as ei1:
        mutators.set_class_ascendancy(_ExplodingWorker(), class_name="Druid")  # type: ignore[arg-type]
    assert ei1.value.rule_id == "POE2_CLASS_IN_POB1"

    with pytest.raises(CrossGameHallucinationError) as ei2:
        mutators.create_new_build(
            _ExplodingWorker(),  # type: ignore[arg-type]
            class_name="Witch",
            ascendancy="Infernalist",
        )
    assert ei2.value.rule_id == "POE2_CLASS_IN_POB1"

    # Zero false positives for PoE1 and shared PoE1/PoE2 classes & ascendancies
    for legal in (
        "Witch",
        "Ranger",
        "Deadeye",
        "Pathfinder",
        "Necromancer",
        "Occultist",
        "Elementalist",
        "Marauder",
        "Duelist",
        "Shadow",
        "Templar",
        "Scion",
        "Hierophant",
        "Saboteur",
        "女巫",
        "游侠",
        "锐眼",
        "追猎者",
    ):
        check_poe2_class_or_ascendancy(legal, field="class")


def test_daemon_shutdown_all_preserves_foreign_or_other_pid_state_file(tmp_path: Path) -> None:
    import contextlib

    def _make_stub_manager(rt_dir: Path) -> daemon.DaemonManager:
        mgr = daemon.DaemonManager.__new__(daemon.DaemonManager)
        mgr.runtime_dir = rt_dir
        mgr._lock = threading.RLock()
        mgr._shutdown_lock = threading.Lock()
        mgr._shutdown_started = False
        mgr._shutdown_done = threading.Event()
        mgr._idle_event = threading.Event()
        mgr._workers = {}
        mgr.server = None
        mgr._exclusive_barrier = contextlib.nullcontext  # type: ignore[method-assign]
        mgr._new_worker = lambda _slot: None  # type: ignore[method-assign,assignment]
        return mgr

    state_file = tmp_path / "daemon.json"

    # 1. Foreign PoE2 daemon.json MUST NOT be unlinked even if pid matches
    state_file.write_text(
        json.dumps({"game": "poe2", "pid": os.getpid(), "port": 12345, "token": "t"}),
        encoding="utf-8",
    )
    _make_stub_manager(tmp_path).shutdown_all()
    assert state_file.exists()

    # 2. Another PID's PoE1 daemon.json MUST NOT be unlinked
    state_file.write_text(
        json.dumps({"game": "poe1", "pid": os.getpid() + 999999, "port": 12345, "token": "t"}),
        encoding="utf-8",
    )
    _make_stub_manager(tmp_path).shutdown_all()
    assert state_file.exists()

    # 3. Own PoE1 daemon.json IS unlinked
    state_file.write_text(
        json.dumps({"game": "poe1", "pid": os.getpid(), "port": 12345, "token": "t"}),
        encoding="utf-8",
    )
    _make_stub_manager(tmp_path).shutdown_all()
    assert not state_file.exists()

    # 4. Legacy PoE1 daemon.json (no "game" field) owned by current PID IS unlinked
    state_file.write_text(
        json.dumps({"pid": os.getpid(), "port": 12345, "token": "t"}),
        encoding="utf-8",
    )
    _make_stub_manager(tmp_path).shutdown_all()
    assert not state_file.exists()
