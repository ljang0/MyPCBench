#!/usr/bin/env python3
"""Validate the published MyPCBench task corpus and its derived files."""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "tasks" / "final"
EXPECTED_TASKS = 184
EXPECTED_RUBRICS = 1_129
WEIGHT_TOLERANCE = Decimal("0.001")

failures: list[str] = []


def load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def fail(message: str) -> None:
    failures.append(message)
    print(f"FAIL: {message}")


def compare_ids(label: str, expected: set[str], actual: set[str]) -> None:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        fail(f"{label} id mismatch: missing={missing[:5]} extra={extra[:5]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-jsonschema",
        action="store_true",
        help="fail instead of skipping schema validation when jsonschema is unavailable",
    )
    args = parser.parse_args()

    graded = load(FINAL / "all_tasks_with_grading.json")
    clean = load(FINAL / "mypcbench_clean.json")
    ungraded = load(FINAL / "all_tasks.json")
    legacy = load(FINAL / "mypcbench_legacy.json")
    task_types = load(FINAL / "task_types.json")

    if len(graded) != EXPECTED_TASKS:
        fail(f"graded bundle has {len(graded)} tasks; expected {EXPECTED_TASKS}")

    graded_ids = [task.get("id") for task in graded]
    if len(graded_ids) != len(set(graded_ids)):
        fail("duplicate ids in graded bundle")
    id_set = set(graded_ids)

    if clean != graded:
        fail("mypcbench_clean.json != all_tasks_with_grading.json")

    stripped = [{key: value for key, value in task.items() if key != "grading"} for task in graded]
    if ungraded != stripped:
        fail("all_tasks.json != all_tasks_with_grading.json minus grading")

    compare_ids("ungraded bundle", id_set, {task.get("id") for task in ungraded})
    compare_ids("legacy bundle", id_set, {task.get("id") for task in legacy})
    compare_ids("task_types.json", id_set, set(task_types))

    graded_by_id = {task["id"]: task for task in graded}
    legacy_by_id = {task["id"]: task for task in legacy}
    for task_id, task in graded_by_id.items():
        if legacy_by_id.get(task_id, {}).get("instruction") != task.get("instruction"):
            fail(f"{task_id}: instruction differs from frozen legacy instruction")

    shard_ids: set[str] = set()
    shard_tasks: list[dict] = []
    for raw_path in sorted(glob.glob(str(FINAL / "*" / "*.rubrics.json"))):
        path = Path(raw_path)
        expected_app = path.parent.name
        for shard_task in load(path):
            task_id = shard_task.get("id")
            if task_id in shard_ids:
                fail(f"{task_id} appears in more than one shard")
            shard_ids.add(task_id)
            shard_tasks.append(shard_task)

            bundled = graded_by_id.get(task_id)
            if bundled is None:
                fail(f"{task_id} in {path.relative_to(ROOT)} is missing from graded bundle")
                continue
            if bundled.get("app") != expected_app:
                fail(
                    f"{task_id}: bundle app {bundled.get('app')!r} does not match "
                    f"shard directory {expected_app!r}"
                )
            for key in ("instruction", "apps_involved", "category", "difficulty", "grading"):
                if shard_task.get(key) != bundled.get(key):
                    fail(f"{task_id}: shard/bundle mismatch on {key} ({path.relative_to(ROOT)})")
    compare_ids("rubric shards", id_set, shard_ids)

    try:
        import jsonschema
    except ImportError:
        if args.require_jsonschema:
            fail("jsonschema is required but not installed")
        else:
            print("note: jsonschema not installed; schema validation skipped")
    else:
        validator = jsonschema.Draft202012Validator(load(ROOT / "tasks" / "schema.json"))
        for task in [*graded, *shard_tasks]:
            errors = sorted(validator.iter_errors(task), key=lambda error: list(error.path))
            if errors:
                fail(f"{task.get('id')}: schema violation: {errors[0].message[:160]}")

    rubric_counts: list[int] = []
    for task in graded:
        task_id = task["id"]
        rubrics = task.get("grading", {}).get("rubrics", [])
        rubric_counts.append(len(rubrics))
        total = sum((Decimal(str(rubric.get("weight", 0))) for rubric in rubrics), Decimal(0))
        if abs(total - 1) > WEIGHT_TOLERANCE:
            fail(f"{task_id}: weights sum to {total}")

        criteria = [rubric.get("criterion") for rubric in rubrics]
        if len(criteria) != len(set(criteria)):
            fail(f"{task_id}: duplicate criterion text")
        if any(not isinstance(criterion, str) or not criterion.strip() for criterion in criteria):
            fail(f"{task_id}: blank or non-string criterion")
        if any(rubric.get("type") != "llm_judge" for rubric in rubrics):
            fail(f"{task_id}: non-llm_judge rubric type")

        agent_text = json.dumps(
            {"instruction": task.get("instruction"), "grading": task.get("grading")}
        )
        if re.search(r"\$\{[A-Za-z_]+\}", agent_text):
            fail(f"{task_id}: unresolved ${{VAR}} template in instruction/grading")

    rubric_total = sum(rubric_counts)
    if rubric_total != EXPECTED_RUBRICS:
        fail(f"graded bundle has {rubric_total} rubric items; expected {EXPECTED_RUBRICS}")

    minimum = min(rubric_counts)
    maximum = max(rubric_counts)
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    task_readme = (FINAL / "README.md").read_text(encoding="utf-8")
    if f"{minimum}–{maximum} weighted rubric criteria" not in root_readme:
        fail("root README rubric range is stale")
    if f"{rubric_total:,} total" not in root_readme:
        fail("root README rubric total is stale")
    if f"mean {rubric_total / len(graded):.1f}" not in root_readme:
        fail("root README mean rubric count is stale")
    if f"{minimum}–{maximum} rubrics per task" not in task_readme:
        fail("tasks/final/README.md rubric range is stale")

    verdict = "GREEN" if not failures else f"{len(failures)} FAILURES"
    print(
        f"\nchecked {len(graded)} tasks, {rubric_total} rubric items "
        f"({minimum}–{maximum} per task): {verdict}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
