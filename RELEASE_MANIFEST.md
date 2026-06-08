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
- `.github/workflows/release-image-publisher.yml` and
  `.github/workflows/release-image-freshness.yml` — daily release image
  publish/verify automation.
- `release-files.txt` and `scripts/export-runner-release.sh` — the explicit
  file allowlist and export command for creating the clean runner-only repo
  tree.

## Excluded

The public release does not include the environment build source or local audit
state: app monorepos, bake scripts, generated databases, VM images, run
results, cookies, local `.env`, cache directories, handoff notes, and audit
artifacts are ignored. Build-source CI and daily republish workflows belong on
the separate build-source branch, not in this runner-only release.

The VM is distributed separately as a pre-baked qcow2 and Docker QEMU wrapper;
see `README.md`, `docs/NO_DOCKER.md`, and `docs/QEMU_QUICKSTART.md`.

## Export

Create the clean runner-only tree and tarball with:

```bash
bash scripts/export-runner-release.sh
```

The export is allowlist-based. It fails if a listed release path is missing and
checks that build/audit/source-only directories such as `web-apps/`,
`vm-setup/`, `generated_data/`, `results/`, `node_modules/`, paper folders, and
local VM images are not present in the exported file list.

The generated tarball is deterministic when `SOURCE_DATE_EPOCH` is fixed and
always uses `mypcbench-runner/` as its archive root, independent of the local
output directory name.

## Verification

Use `docs/RELEASE_AUDIT.md` as the public release gate. It covers workflow
syntax, Docker source-ref resolution, Docker/HuggingFace image identity,
runner-only export hygiene, deterministic tarball generation, exported CLI
startup, and the 184-task corpus integrity check.

The scheduled publisher is reliable only after the GitHub repository has
`DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and `HF_TOKEN` configured. Missing
secrets are treated as workflow failures before publish/upload steps run.
