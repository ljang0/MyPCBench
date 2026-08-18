# MyPCBench task set

**184 tasks**, all rubric-graded (LLM-as-judge). This directory is the
canonical source of truth.

## Layout

```
tasks/final/<bucket>/<bucket>.rubrics.json   # 20 buckets (19 per-app + multi_app) — source of truth (184 tasks)
tasks/final/all_tasks_with_grading.json      # flat eval input, grading.rubrics inlined (pre-built)
tasks/final/all_tasks.json                   # convenience flat list WITHOUT grading — do NOT grade with this
tasks/final/variables.json                   # ground-truth values reference (in-VM paths, balances, …)
tasks/final/task_types.json                  # per-task behavioral type mapping (paper Table 1 taxonomy)
tasks/schema.json                            # task JSON schema
```

`task_types.json` maps every task id to its behavioral type from the paper's
six-type taxonomy — `bounded_action` (64), `multi_step_orchestration` (48),
`cross_source_reconciliation` (25), `aggregation_reporting` (23),
`personal_lookup` (13), `pattern_inference` (11).

**Run against `tasks/final/` (a directory) or
`tasks/final/all_tasks_with_grading.json`.** Do *not* point the runner at
`all_tasks.json` — it has no rubrics and would grade every task to 0.0
(the runner now detects this: it transparently redirects the flat file to
its graded twin and otherwise aborts with an error rather than running a
silent all-zero eval). The flat files are regenerated from the buckets by
the environment-build tooling (not part of this public release); this
repository ships them ready to use.

## Task schema

Each task has `id`, `category`, `instruction`, `apps_involved`,
`difficulty`, and a `grading` block. The per-bucket source files also
carry `persona` (always `michael_scott`) and `horizon`; the flat
`all_tasks*.json` eval files the runner actually loads omit them — the
persona is supplied to the runner at run time via `--persona` (default
`michael_scott`), not per task. Grading is **rubric-only**:

```json
"grading": {
  "type": "llm_judge",
  "rubrics": [
    { "criterion": "<natural-language criterion>", "type": "llm_judge", "weight": 0.5 }
  ]
}
```

Each rubric is `{ "criterion", "type": "llm_judge", "weight" }`; weights
sum to 1.0 (3–13 rubrics per task).

Programmatic checks were retired in an earlier revision; there is no `grading.checks`.

## Grading

The runner does **not** grade. It writes a binary `result.txt`
(1.0 = the episode ran to completion, 0.0 = errored) plus a
self-contained `rubric_bundle.json` per task. Rubric scoring is the
separate post-run step:

```bash
python3 agent-harness/judge_results.py --result_dir results/<agent>
```

which runs the Gemini full-trajectory per-rubric judge
(`gemini-3.1-flash-lite-preview`) and writes `scores.json`. See the root
[README](../../README.md) "Grading" section and
[docs/NO_DOCKER.md](../../docs/NO_DOCKER.md).

## `mypcbench_clean.json` (rubric alignment, August 2026)

`tasks/final/mypcbench_clean.json` is the full 184-task set with the same frozen instructions and the
rubrics re-aligned after a live, per-item feasibility verification against the daily image (every rubric
item checked on a fresh bake; items that graded something the instruction never asked were dropped, and
criteria that contradicted the instruction, a sibling, or the live world were reworded — instructions were
never edited). Use it directly: `--tasks_dir tasks/final/mypcbench_clean.json`.
