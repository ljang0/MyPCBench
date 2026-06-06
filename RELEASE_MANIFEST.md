# MyPCBench OSS Release Manifest

This repository is the runner-only OSS benchmark release. It intentionally
contains the evaluation harness, task definitions, rubrics, persona metadata,
docs, and license files only.

## Included

- `agent-harness/` — OSWorld-style single-task runner, parallel runner, paper
  agents, and offline rubric judge.
- `tasks/final/` — canonical 184-task benchmark set with rubrics.
- `tasks/smoke_one/`, `tasks/smoke/` — low-cost runner smoke tasks.
- `personas/` — public persona metadata used by the runner and rubrics.
- `scripts/get-eval-image.sh`, `scripts/run-agent.sh` — image fetch and runner
  convenience wrappers.
- `docs/`, `README.md`, `LICENSE`, `NOTICE`, `.env.example`,
  `requirements.txt`, `RELEASE_MANIFEST.md`.

## Excluded

The public release does not include the environment build source or local audit
state: app monorepos, bake scripts, generated databases, VM images, run
results, cookies, local `.env`, cache directories, handoff notes, and audit
artifacts are ignored. Build-source CI and daily republish workflows belong on
the separate build-source branch, not in this runner-only release.

The VM is distributed separately as a pre-baked qcow2 and Docker QEMU wrapper;
see `README.md`, `docs/NO_DOCKER.md`, and `docs/QEMU_QUICKSTART.md`.
