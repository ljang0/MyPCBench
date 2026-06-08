# Release Audit Checklist

Use this checklist for the public runner-only release. The full VM build source
and app source live outside the runner export; this repo verifies that the
published artifacts are current, identical, and runnable.

## Image Freshness

```bash
python3 scripts/check-release-image-freshness.py \
  --require-date-tag today \
  --max-latest-age-hours 36 \
  --check-docker-embedded
```

Release is blocked if the current dated Docker tag is missing, `latest` does
not point at the same digest, the Docker embedded qcow2 differs from
HuggingFace `michael_scott.qcow2`, or the publish proof is stale.

## Scheduled Publishing

The release branch ships two GitHub Actions workflows:

- `.github/workflows/release-image-publisher.yml` runs daily at `08:00 UTC`
  and can also be triggered manually. It publishes the selected source image
  into Docker tags `latest`, `michael_scott-YYYY-MM-DD`, `michael_scott`, and
  `demo`, extracts the same digest-pinned image's `/baseline/mypcbench.qcow2`,
  uploads it to HuggingFace as `michael_scott.qcow2`, then verifies identity.
- `.github/workflows/release-image-freshness.yml` runs daily at `09:30 UTC`
  and verifies that the scheduled publisher produced the current dated tag and
  matching HuggingFace qcow2.

Before relying on the schedule, configure these repository secrets:

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
HF_TOKEN
```

The publisher fails before any mutation if a required secret is missing. The
freshness workflow also requires Docker Hub credentials because it pulls the
large image to hash the embedded qcow2 and should not depend on anonymous
Docker Hub rate limits.

By default the scheduled publisher republishes from
`ljang/mypcbench-qemu:latest`. If the VM bake happens in another pipeline,
that upstream source must update before `08:00 UTC`, or the manual
`workflow_dispatch` input `source_image` should be set to the freshly baked
image digest/tag.

## Runner Smoke

Run at least one Docker smoke and one direct-QEMU smoke before publishing docs
or tags:

```bash
python3 agent-harness/run_mypcbench.py \
  --backend docker \
  --docker_image ljang/mypcbench-qemu:latest \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one \
  --max_steps 4 \
  --result_dir results/smoke-docker

python3 agent-harness/run_mypcbench.py \
  --backend qemu \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one \
  --max_steps 4 \
  --result_dir results/smoke-qemu
```

Docker runner-owned starts pull `latest` before boot. Direct-QEMU default runs
refresh the managed `./mypcbench-vm/mypcbench.qcow2` cache before boot.
Explicit non-default qcow2 paths are pinned inputs.

## Runner-Only Export

```bash
bash scripts/export-runner-release.sh
```

The export must contain the harness, tasks, personas, docs, runner scripts,
image freshness tooling, workflows, license files, and no VM build source,
web-app source, generated database state, local results, paper folders, caches,
or fetched qcow2 images.

## Full Release Verification

Before merging or tagging a runner-only release branch, run the full verification
suite from the repo root:

```bash
bash -n scripts/export-runner-release.sh scripts/get-eval-image.sh scripts/run-agent.sh
python3 -m py_compile \
  scripts/check-release-image-freshness.py \
  agent-harness/run_mypcbench.py \
  agent-harness/run_parallel_tasks.py \
  agent-harness/env.py
git diff --check
```

Validate workflow syntax and the Docker source-ref logic used by the daily
publisher:

```bash
python3 - <<'PY'
import yaml
from pathlib import Path
for path in Path(".github/workflows").glob("*.yml"):
    yaml.safe_load(path.read_text())
    print(path, "ok")
PY

source="ljang/mypcbench-qemu:latest"
digest="$(docker buildx imagetools inspect "$source" --format '{{json .}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["manifest"]["digest"])')"
source_name="$(python3 -c 'import sys; s=sys.argv[1].split("@",1)[0]; slash=s.rfind("/"); colon=s.rfind(":"); print(s[:colon] if colon > slash else s)' "$source")"
source_ref="$source_name@$digest"
docker buildx imagetools inspect "$source_ref" >/dev/null
cid="$(docker create "$source_ref")"
docker rm -f "$cid" >/dev/null
```

Validate the export and task corpus:

```bash
rm -rf /tmp/mypcbench-release-a /tmp/mypcbench-release-b \
  /tmp/mypcbench-release-a.tar.gz /tmp/mypcbench-release-b.tar.gz
SOURCE_DATE_EPOCH=0 bash scripts/export-runner-release.sh \
  /tmp/mypcbench-release-a /tmp/mypcbench-release-a.tar.gz
SOURCE_DATE_EPOCH=0 bash scripts/export-runner-release.sh \
  /tmp/mypcbench-release-b /tmp/mypcbench-release-b.tar.gz
cmp /tmp/mypcbench-release-a.tar.gz /tmp/mypcbench-release-b.tar.gz

tar -tzf /tmp/mypcbench-release-a.tar.gz \
  | rg '__pycache__|\.pyc|web-apps|vm-setup|generated_data|node_modules|MyPCBenchHUMAN|COLM|mypcbench-vm|CODEX_AUDIT|\.sqlite|\.qcow2' \
  && exit 1 || true

cd /tmp/mypcbench-release-a
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile $(find . -name '*.py' | sort)
bash -n scripts/*.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=agent-harness \
  python3 agent-harness/run_mypcbench.py --help >/tmp/mypcbench-run-help.txt
PYTHONDONTWRITEBYTECODE=1 \
  python3 agent-harness/run_parallel_tasks.py --help >/tmp/mypcbench-parallel-help.txt
PYTHONDONTWRITEBYTECODE=1 \
  python3 agent-harness/judge_results.py --help >/tmp/mypcbench-judge-help.txt

python3 - <<'PY'
import json
from pathlib import Path
tasks = json.loads(Path("tasks/final/all_tasks_with_grading.json").read_text())
ids = [task.get("id") for task in tasks]
missing = [
    task.get("id")
    for task in tasks
    if not task.get("id")
    or not task.get("instruction")
    or not (task.get("grading") or {}).get("rubrics")
]
assert len(tasks) == 184, len(tasks)
assert len(set(ids)) == 184, len(set(ids))
assert not missing, missing[:10]
print({"tasks": len(tasks), "unique_ids": len(set(ids))})
PY
```

The release is ready only when all commands above pass and the image freshness
check reports `"verdict": "PASS"`.

## Latest Recorded Verification

For PR branch `codex/runner-only-release-publisher`, the full suite above was
rerun on 2026-06-08 after the every-file release export audit.

- Exported files: 69 before verification-generated bytecode.
- Task corpus: 184 tasks, 184 unique IDs, no missing instructions/rubrics.
- Deterministic export: identical tarball SHA256 across two output dirs.
- Docker source ref resolved to
  `ljang/mypcbench-qemu@sha256:471e10e2d5d32608c594896bd159d3ca55c4c60e72d3dc877997b5226f75f82a`.
- Docker embedded qcow2 SHA and HuggingFace qcow2 SHA both resolved to
  `c970a526e1ce2192ff4fca2fa415f5736f2cc291d2be9e90e545a8c0f58a3d84`.
- Required dated tag `michael_scott-2026-06-08` matched `latest`.
- Freshness verdict: `PASS`.
- Clean exported repo setup passed: virtualenv creation, `pip install -r
  requirements.txt`, Python compile, shell parse, runner help, parallel-runner
  help, judge help, and task corpus assertions.
- Clean exported Docker single-run smoke passed:
  `run_mypcbench.py --backend docker --agent_type dummy --model dummy
  --tasks_dir tasks/smoke_one --max_steps 4` wrote `result.txt = 1.0`.
- Clean exported Docker parallel smoke passed:
  `run_parallel_tasks.py --backend docker --num-vms 1 --agent-type dummy
  --model dummy --tasks-file tasks/smoke_one/one.json --max-steps 4` reported
  `1 / 1` tasks completed and wrote `summary.json`.
- Clean exported QEMU-direct smoke passed with no preexisting image cache:
  the runner fetched `michael_scott.qcow2` from HuggingFace, auto-detected
  OVMF at `/usr/share/OVMF/OVMF_CODE_4M.fd`, booted QEMU, reached the Control
  API, and wrote `result.txt = 1.0`.
