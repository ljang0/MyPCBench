# Task and Rubric Health Handoff

Status as of 2026-08-12.

## Executive summary

This branch contains a task/rubric-only remediation of MyPCBench. It addresses instruction coherence, task-to-rubric alignment, feasibility, hidden requirements, rubric atomicity, and date-anchor invariance.

- Branch: `fix/task-rubric-health`
- Base commit: `caf9c754`
- Local worktree: `/home/ljang/MyPCBench-task-fixes`
- Total benchmark tasks: 184
- Task contracts changed: 79
- Task-level rubric sets changed: 78
- Current rubric items: 1,135
- Current rubric items rewritten or newly introduced in changed tasks: 486
- Image, seed, judge, trajectory, and audit changes: none

All 184 tasks pass the static schema, synchronization, date-policy, and weight-tolerance checks described below. The complete static date-contract review has no confirmed residual anchor defect.

This is not an empirical 184-task GUI certification. Selected live data, application source, seeded inventory, and historical computer-use trajectories were inspected, but the changed tasks were not rerun end to end because the requested scope explicitly excluded reruns and judges.

**Update 2026-08-12 (later the same day):** a full live-VM per-rubric
feasibility audit of all 184 tasks / 1,135 rubric items was run against the
current daily image (no eval-agent runs, no judges — direct read-only probing
of app DBs, APIs, filesystem, and app source). 56 defective items across 40
tasks were found and fixed rubric-only; 9 tasks carry instruction- or
seed-level defects flagged for owner decision. See
`docs/LIVE_RUBRIC_AUDIT_2026-08-12.md`. Current rubric item count: 1,134.
Static checks are now committed as `scripts/validate_tasks.py` (green).

## Exact counts

| Measure | Before | Current | Notes |
|---|---:|---:|---|
| Tasks | 184 | 184 | 79 changed; 105 unchanged |
| Task-level rubric sets changed | — | 78 | `hard_app-f005` changed without changing its rubric set |
| Instructions changed | — | 76 | Other task changes include metadata, subtasks, or rubrics |
| Total rubric items | 1,192 | 1,135 | Net reduction of 57 |
| Rubric items in the 79 changed tasks | 564 | 507 | 486 current items are rewritten/new; 21 are retained exactly |
| Rubric items per task | 3–17 | 3–19 | Current mean: 6.2 |

The figure 486 means “current criteria that are rewritten or new,” not 486 independently adjudicated bugs. Several original problems were missing criteria, compound criteria, incoherent premises, or duplicate credit, so a one-to-one defective-item count would be misleading.

## What was fixed

The changes fall into six main groups.

### Instruction-to-rubric alignment

- Removed unasked screenshot and exact screenshot-path requirements.
- Removed or exposed hidden filenames, save paths, field lists, dates, recipients, and output-format requirements.
- Made optional evidence genuinely optional rather than mandatory in grading.
- Added grading for explicitly requested Writer, Calc, Impress, booking, sending, scheduling, and persistence outcomes.
- Aligned draft, send, publish, schedule, reserve, pre-fill, and place-order states.

Examples:

- `cua_only-f022` and `cua_only-f023` no longer require unasked screenshot artifacts.
- `retrieval-f002` and `retrieval-f005` no longer turn optional corroboration into multiple mandatory sources.
- `aggregation-f019`, `aggregation-f029`, `contradiction-f005`, and `preference_inference-f009` now grade their requested LibreOffice deliverables.
- `situated_action-f036` uses an immediate HooliWork planning kickoff instead of an unsupported scheduled post.

### Ambiguity and deterministic source selection

- Pinned ambiguous threads, files, entities, periods, candidate sets, and comparison windows.
- Added explicit empty-result and unavailable-result branches where the live state may legitimately contain no match.
- Defined previously subjective terms such as recent windows, stale records, selected dates, and comparison baselines.

Examples:

- `cua_only-f006` names the exact `THE DUNDIES ARE BACK BABY` thread and requires Toby's complete objection message.
- `aggregation-f031` uses two adjacent, equal three-calendar-month HangryDash windows anchored to the latest displayed order.
- `preference_inference-f012` uses an inclusive 90-day window ending on the latest displayed HangryDash order.
- `hard_app-f033` defines Thursday as the first Thursday strictly after the VM date.

### Feasibility and supported application behavior

- Removed unsupported scheduling behavior from LockedIn, HooliWork, and HangryDash.
- Replaced unavailable routes, cities, listings, products, and app features with seeded alternatives or explicit no-result branches.
- Reframed actions that were impossible because the relevant event or reservation was already past.
- Replaced all-or-nothing action bundles with more atomic criteria in the highest-risk tasks.

Examples:

- `hard_app-f016` now uses the seeded Scranton venue Cooper's Seafood House, with Alfredo's Pizza Cafe, Cugino's, and Cara Mia Bistro as same-city fallbacks.
- `long_horizon-f053` uses a normal dated HooliChat RSVP message, not a nonexistent poll feature, and orders the seeded World's Best Boss mug.
- `long_horizon-f070` verifies that the Jamaica hotel and flight are compatible and future before mutating state; it uses the seeded Men's Classic Fit Oxford Shirt and EU Travel Adapter products.
- `long_horizon-f060` uses the seeded AVP–JFK route and requires actual booking outcomes.
- `long_horizon-f071` and `long_horizon-f075` use pre-filled carts and drafts rather than unsupported weeks-ahead delivery or post scheduling.

### Date anchoring and rollover safety

Every canonical shard task has one of these policies:

| Date policy | Tasks | Meaning |
|---|---:|---|
| `relative` | 138 | Windows and actions derive from the VM date or a deterministic live-record anchor |
| `narrative_absolute` | 39 | Fixed years or quarters are historical artifact identities, not claims about the current period |
| `mixed` | 7 | The task explicitly separates rolling/live data from intentionally fixed history |

Within the 79 changed tasks, the split is 51 relative, 22 narrative-absolute, and 6 mixed. Seventeen tasks changed policy class.

Key patterns used in the rewrite:

- “Today” means the VM's local date, with since-local-midnight windows where applicable.
- Weekday phrases define their boundary, such as “the first Thursday strictly after the VM date.”
- Recent-history analysis can anchor to the latest displayed record when the application history is intentionally frozen.
- Fixed artifacts such as TY2025 returns, Q2 2026 forecasts, and Dundies 2026 files are explicitly historical.
- Cross-app tasks disclose each source's represented span or snapshot basis instead of pretending frozen and rebased apps share one rolling window.
- Future actions validate that their source booking is still future; a past, absent, or inconsistent source triggers a safe no-mutation report branch.

The exhaustive static date review covered all 184 tasks. No confirmed task-contract anchor defect remains after the final nine rollover fixes.

### Rubric quality and scoring behavior

- Reweighted substantive outcomes above navigation and formatting.
- Split unrelated actions that previously produced all-or-nothing scoring.
- Removed duplicate navigation/open/view credit.
- Added persistence only when persistence is requested and supported.
- Added negative-state checks for explicit “do not order,” “do not post,” or “leave as draft” instructions.

### Documentation and generated bundles

- Updated the top-level and task READMEs to the current rubric counts and range.
- Synchronized canonical task shards with both flat bundles:
  - `tasks/final/all_tasks_with_grading.json`
  - `tasks/final/all_tasks.json`
- The ungraded bundle is exactly the graded bundle with `grading` removed.

## Validation results

The final static validation result is:

- 184 unique task IDs in canonical shards, the graded flat bundle, and the ungraded flat bundle.
- Zero shard-to-flat mismatches in instruction, applications, category, difficulty, or grading.
- All 184 graded records pass `tasks/schema.json` with Draft 2020-12 validation.
- All 79 changed tasks have nonempty instructions, required subtasks, and rubric criteria.
- All 79 changed tasks have weights summing exactly to `1.0` using decimal arithmetic.
- All 184 tasks are within the repository's `0.001` serialization tolerance.
- 156 tasks sum exactly to `1.0`; 28 untouched legacy tasks have tiny pre-existing deviations, with maximum absolute deviation `0.000108`.
- No duplicate criterion text exists within a changed task.
- No unresolved `${VAR}` templates remain in changed task contracts.
- No screenshot, `.png`, `.jpg`, or `cart_preview` requirement remains in the changed contracts.
- All date-policy values are one of `relative`, `mixed`, or `narrative_absolute`.
- `git diff --check` passes.

Quick syntax and patch checks:

```bash
cd /home/ljang/MyPCBench-task-fixes
find tasks/final -name '*.json' -print0 | xargs -0 -n1 jq empty
git diff --check
git status --short
```

The deeper schema, decimal-weight, uniqueness, shard-sync, and date-policy checks were run with Python against the canonical shards, both flat bundles, and `tasks/schema.json`.

## Live VM and computer-use evidence

The feasibility work used three evidence layers:

1. Fresh-VM read-only spot checks of seeded SQLite data through the VM control API.
2. Static inspection of application source, seeded catalog entries, routes, cities, and supported UI/API actions.
3. Historical computer-use trajectories and their rubric decisions for representative mismatch cases.

This evidence confirmed several important defects before they were rewritten, including:

- Two plausible Toby/Dundies email threads in the live seed.
- A compliant cart-preview trajectory penalized for an unasked screenshot.
- An immediate HooliWork post receiving full credit for a task that asked for scheduled delivery.
- A non-booking flight trajectory receiving booking credit.
- A valid before-end-of-day delivery slot being penalized because the rubric secretly required the earliest slot.
- Optional email-or-document corroboration being graded as email-and-document.

The fresh checks and source review also verified specific replacement entities and features used by the final contracts.

What was not done:

- No end-to-end computer-use rerun of all 184 tasks.
- No end-to-end computer-use rerun of the 79 changed tasks.
- No new judge invocation or score generation.
- No trajectory regeneration.

Therefore the correct release statement is:

> All 184 tasks pass static task/rubric/date-contract validation, and every
> rubric item has been empirically verified against a live VM across three
> passes: the published image (2026-08-12), a rollover-stress bake with the
> clock advanced a day, and a cold-booted **rebaked** image built from
> `fix/bake-invariance` (2026-08-13). 138 rubric items across 73 tasks were
> repaired in total, rubric-only except for 8 owner-approved instruction
> fixes. The rebaked image passes the 58-probe data gate 58/0/0 on a cold
> boot, and `verify_bake_invariance.py` passes 1056/1056 across 22 bake
> anchors, so task feasibility is now proven invariant to the bake date
> rather than checked on one day's data. The benchmark is still not certified
> as 184/184 executable end-to-end by a computer-use agent, because no
> eval-agent runs or judge scoring were performed.

## Image and daily regeneration boundary

This branch deliberately does not modify the VM image or seed generators. That keeps the blast radius limited to task contracts.

The earlier audit found that the public publishing path could retag an existing image digest while freshness checks remained green. It did not establish a complete current-image, all-app boot-rebase proof. Some apps and historical artifacts are also intentionally frozen rather than rebased.

The task rewrites accommodate that model by distinguishing:

- VM-relative data,
- record-anchored frozen history,
- intentionally fixed narrative artifacts, and
- mixed tasks that must disclose each source's basis.

This makes the task contracts statically anchor-safe, but it does not prove that the image is rebuilt daily or that every application rebases successfully. Image provenance, embedded seed metadata, per-app rebase success, and actual daily artifact regeneration remain a separate infrastructure workstream.

## Files in scope

Task payload changes touch:

- `README.md`
- `tasks/final/README.md`
- `tasks/final/all_tasks.json`
- `tasks/final/all_tasks_with_grading.json`
- 14 canonical rubric shards under `tasks/final/`

This handoff adds `docs/TASK_RUBRIC_HEALTH_HANDOFF.md`.

No image, seed, app implementation, judge, trajectory, or audit artifact is part of the patch.

## Changed task inventory

- `aggregation` (8): `aggregation-f001`, `aggregation-f002`, `aggregation-f009`, `aggregation-f010`, `aggregation-f011`, `aggregation-f019`, `aggregation-f029`, `aggregation-f031`
- `contradiction` (9): `contradiction-f004`, `contradiction-f005`, `contradiction-f006`, `contradiction-f008`, `contradiction-f011`, `contradiction-f012`, `contradiction-f014`, `contradiction-f015`, `contradiction-f017`
- `counterfactual` (2): `counterfactual-f003`, `counterfactual-f013`
- `cua_only` (9): `cua_only-f002`, `cua_only-f006`, `cua_only-f007`, `cua_only-f008`, `cua_only-f011`, `cua_only-f021`, `cua_only-f022`, `cua_only-f023`, `cua_only-f024`
- `hard_app` (8): `hard_app-f004`, `hard_app-f005`, `hard_app-f011`, `hard_app-f016`, `hard_app-f017`, `hard_app-f025`, `hard_app-f027`, `hard_app-f033`
- `long_horizon` (22): `long_horizon-f014`, `long_horizon-f040`, `long_horizon-f044`, `long_horizon-f046`, `long_horizon-f047`, `long_horizon-f048`, `long_horizon-f050`, `long_horizon-f051`, `long_horizon-f053`, `long_horizon-f054`, `long_horizon-f055`, `long_horizon-f056`, `long_horizon-f057`, `long_horizon-f058`, `long_horizon-f060`, `long_horizon-f062`, `long_horizon-f065`, `long_horizon-f066`, `long_horizon-f070`, `long_horizon-f071`, `long_horizon-f074`, `long_horizon-f075`
- `preference_inference` (3): `preference_inference-f005`, `preference_inference-f009`, `preference_inference-f012`
- `retrieval` (5): `retrieval-f002`, `retrieval-f005`, `retrieval-f017`, `retrieval-f020`, `retrieval-f032`
- `situated_action` (13): `situated_action-f004`, `situated_action-f005`, `situated_action-f010`, `situated_action-f013`, `situated_action-f014`, `situated_action-f016`, `situated_action-f021`, `situated_action-f027`, `situated_action-f029`, `situated_action-f032`, `situated_action-f036`, `situated_action-f038`, `situated_action-f040`

## Recommended next steps

1. Review the semantic diff by task ID, not raw line count. The generated flat bundles and reformatted multi-app shard make the textual diff larger than the 79-task semantic scope.
2. Commit the task-only patch without mixing in image, seed, judge, or trajectory changes.
3. If empirical feasibility certification is later required, run a separate no-judge computer-use smoke pass and record execution failures independently from rubric quality.
4. Treat image provenance and daily regeneration as a separate release gate: verify embedded bake metadata, source SHA, per-app rebase completion, and distinct artifact creation rather than relying on tag timestamps.
5. Optionally normalize the 28 untouched legacy weight sums to exact decimal `1.0` in a separate mechanical change. They are currently valid under the documented tolerance and were intentionally left outside this semantic repair.

## Handoff cautions

- Do not rerun old temporary task-spec application helpers; their temporary spec files were removed after synchronization, and rerunning stale helpers could overwrite later fixes.
- Do not use historical aggregate scores as proof that the current contracts are valid. Some stored runs used older instructions or rubrics, and some judge records credited unsupported behavior.
- Do not use the stored legacy live-audit screenshots as proof of the current image. Their VM/image provenance differs from the fresh spot-check VM used during this review.
- Do not describe the task branch as proving daily image regeneration. It proves static task-contract health only.
