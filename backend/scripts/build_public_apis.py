"""Rebuild jarvis_backend/data/public_apis.json from the public-apis list (https://github.com/public-apis/public-apis, MIT).

Usage: python backend/scripts/build_public_apis.py [path/to/README.md]
Without a path it downloads the current README. Only the community tables (API | Description | Auth | HTTPS | CORS)
are kept; the sponsored section at the top is skipped."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
OUT = Path(__file__).resolve().parents[1] / "jarvis_backend" / "data" / "public_apis.json"
ROW = re.compile(r"^\|\s*\[(?P<name>[^\]]+)\]\((?P<link>[^)]+)\)\s*\|\s*(?P<desc>[^|]*)\|\s*(?P<auth>[^|]*)\|\s*(?P<https>[^|]*)\|\s*(?P<cors>[^|]*)\|")


def parse(md: str) -> list[dict[str, str | bool]]:
    apis, category = [], ""
    for line in md.splitlines():
        if line.startswith("### "):
            category = line[4:].strip()
            continue
        m = ROW.match(line)
        if not m or not category or "APILayer" in category:
            continue
        auth = m["auth"].strip().strip("`")
        apis.append({
            "name": m["name"].strip(),
            "category": category,
            "description": m["desc"].strip(),
            "auth": "" if auth.lower() == "no" else auth,
            "https": m["https"].strip().lower() == "yes",
            "cors": m["cors"].strip().lower(),
            "link": m["link"].strip(),
        })
    return apis


def main() -> None:
    md = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else \
        urllib.request.urlopen(SOURCE, timeout=60).read().decode("utf-8")
    apis = parse(md)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": "https://github.com/public-apis/public-apis", "license": "MIT, (c) 2022 public-apis",
                               "apis": apis}, ensure_ascii=False, indent=0), encoding="utf-8")
    cats = {a["category"] for a in apis}
    print(f"{len(apis)} APIs in {len(cats)} categories, {sum(1 for a in apis if not a['auth'])} need no key -> {OUT}")


if __name__ == "__main__":
    main()
