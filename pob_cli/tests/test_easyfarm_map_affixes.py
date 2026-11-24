"""The EasyFarm scheme must not apply two classifications to one map stat."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/pob/references/easyfarm_map_affixes.py"
spec = importlib.util.spec_from_file_location("easyfarm_map_affixes", SCRIPT)
easyfarm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(easyfarm)


def mark(stat_id="map_player_no_regeneration", severity="skip"):
    return {
        "severity": severity,
        "statId": stat_id,
        "statPath": "Metadata/StatDescriptions/stat_descriptions.csd",
        "reason": "test warning",
    }


def request(*marks):
    return {"name": "test scheme", "marks": list(marks)}


def test_exact_base_match_moves_to_warning_and_keeps_colour():
    base = easyfarm.read_base(easyfarm.DEFAULT_BASE)
    scheme = easyfarm.build_scheme(request(mark()), base)

    assert base["rules"][1]["matchValues"] != scheme["rules"][1]["matchValues"]
    assert sum(v["value"] == "map_player_no_regeneration"
               for rule in scheme["rules"][:-1] for v in rule.get("matchValues", [])) == 0
    assert len(scheme["rules"][1]["matchValues"]) == len(base["rules"][1]["matchValues"]) - 1
    warning = scheme["rules"][-1]
    assert warning["textFix"]["prefix"] == "[跳过] "
    assert (warning["colorMode"], warning["colorId"]) == ("line", "efarmVeryDangerous")
    assert scheme["essentialColours"] == base["essentialColours"]
    assert scheme["version"] == base["version"]


def test_next_generation_restores_removed_base_match():
    base = easyfarm.read_base(easyfarm.DEFAULT_BASE)
    reflected = mark("map_monsters_reflect_%_elemental_damage")
    first = easyfarm.build_scheme(request(mark(), reflected), base)
    changed = easyfarm.build_scheme(request(mark(severity="caution"), reflected), first)
    assert changed["rules"][-2]["textFix"]["prefix"] == "[慎打] "
    assert changed["rules"][-2]["colorId"] == "efarmVeryDangerous"

    removed = easyfarm.build_scheme(
        request(mark("map_monsters_additional_elemental_resistance", "caution")), changed
    )
    assert removed["rules"][:len(base["rules"])] == base["rules"]
    assert len(removed["rules"]) == len(base["rules"]) + 1


def test_broad_base_pattern_fails_instead_of_overlapping():
    base = easyfarm.read_base(easyfarm.DEFAULT_BASE)
    with pytest.raises(ValueError, match="Broad base pattern"):
        easyfarm.build_scheme(request(mark("map_boss_damage_foo")), base)


def test_cws_example_builds_on_default_scheme_without_duplicate_marks():
    example = SCRIPT.parents[3] / "examples/easyfarm/cws-chieftain-330-marks.json"
    marks = json.loads(example.read_text(encoding="utf-8"))
    base = easyfarm.read_base(easyfarm.DEFAULT_BASE)
    scheme = easyfarm.build_scheme(marks, base)

    generated = [rule for rule in scheme["rules"] if easyfarm._is_generated(rule)]
    assert len(generated) == len(marks["marks"]) == 8
    assert sum(rule["textFix"]["prefix"].startswith("[跳过]") for rule in generated) == 7
    assert sum(rule["textFix"]["prefix"].startswith("[慎打]") for rule in generated) == 1
    assert len(scheme["rules"]) == len(base["rules"]) + len(generated)
    assert not any(
        value.get("value") == "map_player_no_regeneration"
        for rule in scheme["rules"][:len(base["rules"])]
        for value in rule.get("matchValues", [])
    )
