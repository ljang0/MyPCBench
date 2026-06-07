# MyPCBench Codex Notes

This is the runner-only OSS release repository. Keep changes scoped to the
public harness, tasks, docs, scripts, persona metadata, and release manifest.

## Guardrails

- Read `CODEX_AUDIT_AND_POLISH.md` before release/audit work.
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

The paper/release default is `ljang/mypcbench-qemu:eval-round0`. Use
`scripts/get-eval-image.sh --set eval-round0` for paper reproduction. The
expanded OSS-polish image is `ljang/mypcbench-qemu:v1.2.47-oss-polish`
(`latest`, `demo`, `michael_scott`, and `michael_scott-2026-06-06` are aliases)
with baked qcow2 sha256
`c970a526e1ce2192ff4fca2fa415f5736f2cc291d2be9e90e545a8c0f58a3d84`.
