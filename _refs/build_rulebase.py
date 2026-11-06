"""
Copy the authored rulebase into `_refs/rulebase.json` and report data issues.

Run:  python _refs/build_rulebase.py

The copy is VERBATIM. The planning decision was "lint report, don't patch", so
contradictions in the source file (PP-01 deadline before jadwal, sheet-map
conflicts) are reported here and surfaced by `python -m src.main lint`, never
silently rewritten.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

REFS_DIR = Path(__file__).resolve().parent
SOURCE = REFS_DIR.parent / "planning" / "rulebase.json"
TARGET = REFS_DIR / "rulebase.json"


def main() -> int:
    if not SOURCE.exists():
        print(f"Source rulebase not found: {SOURCE}")
        return 1

    shutil.copyfile(SOURCE, TARGET)
    payload = TARGET.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()

    rulebase = json.loads(payload.decode("utf-8"))
    pp_tasks = rulebase.get("tugas_pp", []) or []
    bz_tasks = rulebase.get("tugas_buzzer", []) or []
    checks = (rulebase.get("checks_pp", []) or []) + (rulebase.get("checks_buzzer", []) or [])

    print(f"Copied  : {SOURCE} -> {TARGET}")
    print(f"SHA-256 : {digest}")
    print(f"Version : {(rulebase.get('meta') or {}).get('versi', '?')}")
    print(f"Tasks   : {len(pp_tasks)} PP + {len(bz_tasks)} Buzzer")
    print(f"Checks  : {len(checks)}")

    sys.path.insert(0, str(REFS_DIR.parent / "agent"))
    try:
        from src.rulebase_engine import lint_rulebase
    except Exception as exc:  # pragma: no cover
        print(f"(lint skipped: {exc})")
        return 0

    issues = lint_rulebase(rulebase)
    if not issues:
        print("Lint    : clean")
        return 0

    print(f"Lint    : {len(issues)} issue(s) — rulebase NOT modified")
    for issue in issues:
        print(f"  {issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
