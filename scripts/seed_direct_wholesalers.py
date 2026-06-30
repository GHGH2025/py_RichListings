#!/usr/bin/env python3
"""Seed direct_wholesalers collection from direct_wholeseller.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.mongo_engine_conn import init_db
from services.direct_wholesaler_service import DEFAULT_JSON_PATH, import_from_json


def main() -> int:
    init_db()
    result = import_from_json(DEFAULT_JSON_PATH)
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
