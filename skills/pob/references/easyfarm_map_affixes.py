"""Encode verified PoE1 map warnings as a standalone EasyFarm scheme."""

import argparse
import base64
import gzip
import json
import re
import sys
import time
import uuid
from pathlib import Path

PREFIXES = {"skip": "[\u8df3\u8fc7] ", "caution": "[\u614e\u6253] "}
STAT_PATHS = {
    f"metadata/statdescriptions/{name}.csd"
    for name in ("stat_descriptions", "map_stat_descriptions")
}
STAT_ID = re.compile(r"map_[a-z0-9_+%]+")


def build_scheme(request):
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
            "remark": "",
        }
        for (path, severity), matches in groups.items()
    ]
    return {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": "PoE1 build-specific map modifier warnings",
        "author": "pob4agents",
        "version": 1,
        "createdAt": int(time.time() * 1000),
        "rules": rules,
        "essentialColours": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON with verified map modifier marks")
    args = parser.parse_args()
    try:
        scheme = build_scheme(json.loads(args.input.read_text(encoding="utf-8-sig")))
    except (TypeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    data = json.dumps(scheme, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    print(base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
