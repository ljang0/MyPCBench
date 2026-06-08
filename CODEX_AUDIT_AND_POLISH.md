# CODEX_AUDIT_AND_POLISH.md

Self-contained release-audit runbook for Codex agents working on MyPCBench.

## 0. Ground Truth

- Repo: runner-only OSS release. Build sources, app monorepos, generated DBs,
  VM bake scripts, and local audit artifacts do not ship from this branch.
- Canonical task set: `tasks/final/all_tasks_with_grading.json` plus bucket
  files under `tasks/final/*/*.rubrics.json`.
- Current public benchmark persona: Michael Scott.
- Default image: `ljang/mypcbench-qemu:latest`, the current daily/OSS-polish
  benchmark VM. `eval-round0` is an archived v0.0 paper baseline.
- Hard rules:
  - Stage by path; never broad `git add .`.
  - Commit only when explicitly asked.
  - Prefer read-mostly probes against live VMs.
  - Never kill guest/container processes you did not start.
  - Use NO-BIAS auditing: report release blockers plainly.

## 1. Prerequisites

- Linux with KVM, Docker for Docker-wrapper checks, QEMU/OVMF for direct-QEMU
  checks, and Python dependencies from `requirements.txt`.
- Pick unique container names, result dirs, and direct-QEMU port windows for
  each audit run.

## 2. Image Identity

Verify freshness, Docker/HF identity, and Docker/qcow2 hashes before claiming
image equivalence:

```bash
python3 scripts/check-release-image-freshness.py \
  --require-date-tag today \
  --max-latest-age-hours 36 \
  --check-docker-embedded \
  --output /tmp/mypcbench-image-freshness.json
```

The release repo is runner-only: it does not build VM images. The external VM
publisher must create a dated Docker tag such as
`michael_scott-YYYY-MM-DD`, move `latest` to the same digest, and publish the
matching HuggingFace `michael_scott.qcow2`. Treat a missing current date tag,
a stale `latest`, a date-tag/latest digest mismatch, or a Docker/HF qcow2 hash
mismatch as a release blocker.

This branch owns the release-facing publish/verify automation:

- `.github/workflows/release-image-publisher.yml` runs daily/manual, publishes
  Docker tags from a selected source image, uploads the embedded qcow2 to
  HuggingFace, and then runs the freshness verifier.
- `.github/workflows/release-image-freshness.yml` runs after the publisher and
  fails if the daily tag/upload is missing or inconsistent.

Required repository secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and
`HF_TOKEN`.

Manual identity fallback:

```bash
docker pull ljang/mypcbench-qemu:latest
docker run --rm --entrypoint sha256sum \
  ljang/mypcbench-qemu:latest /baseline/mypcbench.qcow2

bash scripts/get-eval-image.sh --set latest --out /tmp/mypcbench-vm
sha256sum /tmp/mypcbench-vm/mypcbench.qcow2
```

`latest` Docker and HF qcow2 should match for the current release image. Treat
any mismatch in Docker tags, HF files, or HF metadata as a release finding.
Use `--set eval-round0` only when explicitly reproducing the paper baseline.

## 3. Smoke Gates

Run both backend smokes when the host supports them:

```bash
python3 agent-harness/run_mypcbench.py --backend docker \
  --docker_image ljang/mypcbench-qemu:latest \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 \
  --result_dir /tmp/mypcbench-smoke-docker

python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 \
  --result_dir /tmp/mypcbench-smoke-qemu
```

Use explicit `MYPCBENCH_HOST_*_PORT` values on busy hosts. Cold boot and
runtime fixups can take minutes; distinguish slow boot from Control API
reset/crash loops.

## 4. Per-Task Feasibility

- Load `tasks/final/all_tasks_with_grading.json`.
- Verify all 184 IDs are unique, all tasks have instructions and rubrics, and
  bucket files match the compiled flat file.
- Boot a fresh live VM, collect app port health, SQLite table presence, and
  seeded desktop/download/document files.
- Mark a task `PASS` when required live apps and referenced input artifacts
  exist. Treat output paths as things the agent creates, not as required seed
  files.
- Record a per-task JSON artifact with one row per task.

For the current live, mutation-ok audit pass, run the browser-facing shard
sweep against a direct-QEMU image with unique port windows:

```bash
for shard in 0 1 2 3; do
  python3 tools/live_mutating_task_audit.py \
    --backend qemu \
    --qcow2 "$MYPCBENCH_QCOW2" \
    --port-base "$((53300 + shard * 100))" \
    --out-dir "results/live_mutating_task_audit_$(date +%Y%m%d)_shard${shard}" \
    --shard-count 4 \
    --shard-index "$shard" &
done
wait
```

This tool logs one row per task, resets between rows, opens each task's app
surfaces through Playwright with an autologin browser context, and attempts a
generic low-risk UI mutation when the task apps expose one. Treat host-port
bind failures as harness artifacts; rerun the affected task IDs on a clean
port window using `--task-ids` or `--task-ids-file`.

Then run app-specific browser-origin mutation/reset checks for known safe
mutable surfaces:

```bash
python3 tools/live_mutation_ok_audit.py \
  --backend qemu \
  --qcow2 "$MYPCBENCH_QCOW2" \
  --port-base 53800 \
  --out-dir results/live_mutation_ok_audit_$(date +%Y%m%d)
```

Release readiness requires the per-task union to cover all 184 IDs with zero
unresolved `FAIL`, and the app-specific mutation/reset audit to pass for every
included app. A conservative per-task `RISK` is acceptable when it reflects a
desktop-only surface or a mutation that should not be completed generically.

### Strict All-Green PASS Gate

Do not convert conservative `RISK` rows to `PASS` by relabeling. An all-green
claim requires task-specific evidence:

- Reset the VM before the task.
- Walk the task's relevant UI surfaces from the agent/browser perspective.
- Verify the required seeded entities and controls are visible or discoverable.
- For safe mutations, perform the mutation, verify the post-condition in the UI
  or app API, reset, and verify the marker/result is gone.
- For dangerous flows (payments, transfers, trades, orders, bookings,
  cancellations, tax filing), stop at the review/confirmation screen unless the
  benchmark task explicitly requires a reset-isolated final submit.
- For desktop/file/LibreOffice tasks, use CUA-visible desktop evidence; DOM or
  filesystem-only checks are supporting evidence, not a complete PASS.

Aggregate one row per task. The strict ship gate is exactly `184 PASS / 0 RISK
/ 0 FAIL`. Route crawls, seed checks, and generic app mutation checks support
this gate but cannot by themselves promote a task to `PASS`.

## 5. Seeded Data / Duplication

For Docker and direct-QEMU of the same image set, compare:

- qcow2 sha256.
- Live post-reset DB/table-count fingerprint.
- Duplicate-prone identifier columns where available.

Only compare image paths within the same image set. `eval-round0` and an
expanded development image can intentionally differ.

## 6. Triage

- P0: runner cannot boot, Control API never stabilizes, task corpus cannot
  load, image paths are mislabeled, or secrets are tracked.
- P1: seeded data mismatch within one image set, stale public metadata,
  missing process docs, confusing setup path, or backend-specific failures.
- P2: wording polish, non-shipping scratch files, or optional convenience gaps.

Separate real defects from harness artifacts: host-port collisions,
desktop contention, first-boot delays, and generated output paths should not
be counted as task infeasibility.

## 7. Fix Loop

Fix at source, then re-run the narrowest meaningful validation. Common fixes:

- Deduplicate race-prone or seed-derived records without biasing task answers.
- Remove stale overrides and stale public hashes.
- Clamp future dates through the VM date-rebase path.
- Widen gates when they were too brittle; do not weaken real validation.

## 8. OSS Release Polish

Before release:

- Secret scan and scratch-file scan.
- Verify `README.md`, `docs/NO_DOCKER.md`, `docs/QEMU_QUICKSTART.md`, and
  `RELEASE_MANIFEST.md` agree.
- Verify task counts and result/stat claims.
- Verify both Docker and no-Docker smokes for the release default.
- Confirm no build-source directories are tracked.
- Confirm licenses/notices for vendored code.

## 9. Definition Of Done

- 184-task corpus integrity passes.
- Live per-task feasibility has 184 rows and no unresolved FAIL.
- Release-default image is boot-smoked on supported backends.
- Docker/HF image identity is explicitly verified or documented as not
  applicable for that selector.
- No secrets or local scratch files are tracked.
- All code/docs checks pass.
