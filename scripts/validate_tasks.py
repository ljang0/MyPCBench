#!/usr/bin/env python3
"""Static validation of MyPCBench task contracts.

Checks (all must pass):
  - graded + ungraded bundles contain the same 184 unique task ids
  - ungraded bundle == graded bundle minus `grading`
  - every canonical shard task matches its bundle copy (instruction,
    grading, category, difficulty)
  - every task validates against tasks/schema.json (if jsonschema is
    installed; skipped otherwise)
  - rubric weights sum to 1.0 within the 0.001 serialization tolerance
  - no duplicate criterion text within a task
  - no unresolved ${VAR} templates

Run: python3 scripts/validate_tasks.py   (exit 0 = green)
"""
import glob
import json
import os
import re
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINAL = os.path.join(ROOT, "tasks", "final")
TOLERANCE = Decimal("0.001")

failures = []


def fail(msg):
    failures.append(msg)
    print(f"FAIL: {msg}")


def main():
    graded = json.load(open(os.path.join(FINAL, "all_tasks_with_grading.json")))
    ungraded = json.load(open(os.path.join(FINAL, "all_tasks.json")))

    gids = [t["id"] for t in graded]
    if len(gids) != len(set(gids)):
        fail("duplicate ids in graded bundle")
    if len(graded) != len(ungraded):
        fail(f"bundle size mismatch: graded={len(graded)} ungraded={len(ungraded)}")

    stripped = [{k: v for k, v in t.items() if k != "grading"} for t in graded]
    if stripped != ungraded:
        fail("ungraded bundle != graded bundle minus grading")

    gmap = {t["id"]: t for t in graded}
    shard_ids = set()
    for path in sorted(glob.glob(os.path.join(FINAL, "*", "*.rubrics.json"))):
        for st in json.load(open(path)):
            tid = st["id"]
            if tid in shard_ids:
                fail(f"{tid} appears in more than one shard")
            shard_ids.add(tid)
            g = gmap.get(tid)
            if g is None:
                fail(f"{tid} in shard {path} missing from graded bundle")
                continue
            for key in ("instruction", "grading", "category", "difficulty"):
                if key in st and st[key] != g.get(key):
                    fail(f"{tid}: shard/bundle mismatch on {key} ({os.path.relpath(path, ROOT)})")
    missing = set(gmap) - shard_ids
    if missing:
        fail(f"bundle tasks missing from shards: {sorted(missing)[:5]}...")

    try:
        import jsonschema

        schema = json.load(open(os.path.join(ROOT, "tasks", "schema.json")))
        validator = jsonschema.Draft202012Validator(schema)
        for t in graded:
            errs = list(validator.iter_errors(t))
            for e in errs[:1]:
                fail(f"{t['id']}: schema violation: {e.message[:120]}")
    except ImportError:
        print("note: jsonschema not installed — schema check skipped")

    for t in graded:
        rubrics = t["grading"]["rubrics"]
        total = sum(Decimal(str(r["weight"])) for r in rubrics)
        if abs(total - 1) > TOLERANCE:
            fail(f"{t['id']}: weights sum to {total}")
        texts = [r["criterion"] for r in rubrics]
        if len(texts) != len(set(texts)):
            fail(f"{t['id']}: duplicate criterion text")
        # Only agent/judge-facing text may not carry unresolved templates;
        # metadata fields like date_policy_notes legitimately mention them.
        blob = json.dumps({"instruction": t.get("instruction"), "grading": t.get("grading")})
        if re.search(r"\$\{[A-Za-z_]+\}", blob):
            fail(f"{t['id']}: unresolved ${{VAR}} template in instruction/grading")

    print(f"\nchecked {len(graded)} tasks, "
          f"{sum(len(t['grading']['rubrics']) for t in graded)} rubric items: "
          f"{'GREEN' if not failures else f'{len(failures)} FAILURES'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
