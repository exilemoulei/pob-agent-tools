"""Merge verified PoE1 map warnings into an EasyFarm scheme and encode it."""

import argparse
import base64
import gzip
import json
import re
import sys
import time
import uuid
import zlib
from pathlib import Path

DEFAULT_BASE = Path(__file__).with_name("easyfarm-system-default-poe1.json")
GENERATED_RULE = "pob4agents:map-affix-warning"
PREFIXES = {"skip": "[\u8df3\u8fc7] ", "caution": "[\u614e\u6253] "}
STAT_PATHS = {
    f"metadata/statdescriptions/{name}.csd"
    for name in ("stat_descriptions", "map_stat_descriptions")
}
STAT_ID = re.compile(r"map_[a-z0-9_+%]+")


def read_base(path):
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw.startswith("{"):
        raw = gzip.decompress(base64.b64decode(raw, validate=True)).decode("utf-8")
    base = json.loads(raw)
    if not isinstance(base, dict):
        raise TypeError("Expected an EasyFarm scheme object")
    for key in ("rules", "essentialColours"):
        entries = base.get(key)
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ValueError(f"Expected EasyFarm {key} to be a list of objects")
    if type(base.get("version")) is not int or base["version"] < 1:
        raise ValueError("Expected a positive EasyFarm scheme version")
    return base


def build_scheme(request, base):
    if not isinstance(request, dict):
        raise TypeError("Expected a JSON object")
    name, marks = request.get("name"), request.get("marks")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Expected a nonempty scheme name")
    if not isinstance(marks, list) or not marks:
        raise ValueError("Expected at least one verified map modifier")

    groups = {}
    seen = set()
    for mark in marks:
        if not isinstance(mark, dict):
            raise TypeError("Each mark must be an object")
        severity, stat_id, path, reason = (
            mark.get(key) for key in ("severity", "statId", "statPath", "reason")
        )
        if not all(isinstance(value, str) for value in (severity, stat_id, path, reason)):
            raise ValueError("Each mark requires severity, statId, statPath and reason")
        if severity not in PREFIXES or not STAT_ID.fullmatch(stat_id) or not reason.strip():
            raise ValueError(f"Invalid map mark: {stat_id}")
        path = path.lower()
        if path not in STAT_PATHS:
            raise ValueError(f"Unsupported map stat path: {path}")
        key = (path, stat_id)
        if key in seen:
            raise ValueError(f"Duplicate map mark: {stat_id} in {path}")
        seen.add(key)
        groups.setdefault((path, severity), []).append({
            "value": rf"(?:^|\s){re.escape(stat_id)}(?:\s|$)",
            "remark": reason.strip(),
        })

    rules = [
        {
            "type": "affix",
            "name": f"{PREFIXES[severity].strip()} {Path(path).stem}",
            "enabled": True,
            "matchType": "idLine",
            "matchValues": matches,
            "colorMatchValues": [],
            "textFix": {"prefix": PREFIXES[severity], "suffix": ""},
            "colorMode": "none",
            "colorId": "",
            "statPath": path,
            "remark": GENERATED_RULE,
        }
        for (path, severity), matches in groups.items()
    ]
    kept_rules = [
        rule for rule in base["rules"]
        if not (rule.get("type") == "affix" and rule.get("remark") == GENERATED_RULE)
    ]
    return {
        **base,
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": "PoE1 build-specific map modifier warnings",
        "author": "pob4agents",
        "createdAt": int(time.time() * 1000),
        "rules": kept_rules + rules,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON with verified map modifier marks")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE, help="EasyFarm JSON or share code")
    args = parser.parse_args()
    try:
        request = json.loads(args.input.read_text(encoding="utf-8-sig"))
        scheme = build_scheme(request, read_base(args.base))
    except (TypeError, ValueError, OSError, EOFError, zlib.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    data = json.dumps(scheme, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    print(base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
