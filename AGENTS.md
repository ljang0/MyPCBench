# MyPCBench Codex Notes

This is the runner-only OSS release repository. Keep changes scoped to the
public harness, tasks, docs, scripts, persona metadata, and release manifest.

## Guardrails

- Read `docs/RELEASE_AUDIT.md` before public runner release work. The full
  internal workspace may have a deeper VM/app audit runbook, but do not require
  internal-only files for runner release checks.
- Stage by path. Do not use broad `git add .`.
- Commit only when the user explicitly asks.
- Use read-mostly probes for live VMs. Do not kill guest processes you did not
  start; only clean up audit-owned containers/processes.
- Treat audits as NO-BIAS: report blockers clearly instead of editing docs to
  hide them.

## Layout

- `agent-harness/` - runner, environment wrapper, agents, rubric judge.
- `tasks/final/` - canonical 184-task set and rubrics.
- `tasks/smoke_one/`, `tasks/smoke/` - low-cost runner smoke tasks.
- `personas/` - public persona metadata.
- `scripts/` - image fetch and runner wrapper scripts.
- `docs/` - Docker and no-Docker run guides.

## Basic Checks

```bash
bash -n scripts/get-eval-image.sh scripts/run-agent.sh
python3 -m py_compile $(git ls-files '*.py')
PYTHONPATH=agent-harness python3 agent-harness/run_mypcbench.py --help >/tmp/mypcbench-help.txt
git diff --check
```

## Default Image

The runner default is `ljang/mypcbench-qemu:latest`, which tracks the current
daily/OSS-polish benchmark VM. Runner-owned Docker starts use
`docker run --pull always`; default direct-QEMU starts refresh the managed
`./mypcbench-vm` cache from `latest`; and `scripts/get-eval-image.sh` defaults
to `--set latest` for the no-Docker path. Use
`ljang/mypcbench-qemu:eval-round0` / `scripts/get-eval-image.sh --set
eval-round0` only for the archived v0.0 paper baseline.

Before release, run:

```bash
python3 scripts/check-release-image-freshness.py --require-date-tag today \
  --max-latest-age-hours 36 --check-docker-embedded
```

This branch is runner-only and does not build VM images; the freshness check
verifies that the external daily publisher created the dated Docker tag,
pointed `latest` at it, and published the matching HuggingFace qcow2.

The release-facing daily publisher lives in
`.github/workflows/release-image-publisher.yml`. It requires
`DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and `HF_TOKEN`, and publishes from a
selected source image into `latest`, `michael_scott-YYYY-MM-DD`,
`michael_scott`, `demo`, and HuggingFace `michael_scott.qcow2`.
Do not mark scheduled publishing ready until those repository secrets exist
and the first manual or scheduled publisher run has passed.
