# MyPCBench

**A benchmark for personally intelligent computer-use agents.**

Project page: <https://mypcbench.pages.dev/> · Code: <https://github.com/ljang0/MyPCBench>

![MyPCBench overview](docs/assets/hero.png)

> A reproducible Linux-desktop benchmark seeded end-to-end from one canonical
> persona (Michael Scott, *The Office*). The image hosts 17 pre-logged-in web
> apps mirroring real consumer products plus the full LibreOffice suite; the
> persona's records (1,812 bank txns · 2,398 emails · 679 calendar events ·
> 2,526 chat messages · 586 retail/grocery/food orders · 10,746 web visits ·
> 126 rides) are **cross-linked**, so one trip leaves correlated records
> across every app that would plausibly book it.

## Why

Computer-use benchmarks evaluate agents in **impersonal** environments — empty
desktops, generic app state, minimally-seeded DBs — and web evals skip any
site behind a login. But a personal assistant has to work across a user's
*whole digital life*. MyPCBench closes that gap: a deterministic generator
populates a coherent user identity at the scale of a real personal computer,
so standard desktop-agent loops can be evaluated on tasks that
require **knowing who the user is** ("order my usual Friday DoorDash", "what
do I normally tip?", "pay Jim back what I owe him").

## At a glance

- **17 web apps** (banking, travel, food delivery, calendar, messaging, work, tax, …) + Firefox + LibreOffice on a real QEMU/KVM Ubuntu 24.04 + GNOME VM
- **184 tasks**, each adapted from a real OpenClaw personal-assistant request and author-audited end-to-end, with a natural-language **rubric**
- **6 behavioural task types** — 4 analysis (personal lookup, aggregation & reporting, pattern inference, cross-source reconciliation) + 2 action (bounded, orchestrated); 72 analysis / 112 action; **68 % multi-app**
- **1 canonical persona**, cross-consistent across every app; deterministic snapshot reset (OSWorld-style)
- **Decoupled grading**: the runner records completion; an offline LLM-as-judge scores per rubric
- **Runs with or without Docker** — boots directly under QEMU/KVM (no daemon, no root)

## Results

Eight closed- and open-weight models under
each provider's native CUA agent (Claude uses computer + bash + editor; OpenAI
uses computer + built-in shell; Qwen main uses computer + bash — see Agents).
Ranked by **Perfect** = % of tasks where every rubric passes; **Rubric** = mean
rubric pass-rate (partial credit). Updated 2026-07-15 (judge-error rescore +
GPT-5.6); live table: <https://mypcbench.pages.dev/leaderboard.html>.

| Model | Perfect % | Rubric % |
|---|--:|--:|
| Claude Opus 4.6 | **58.2** | 85.1 |
| GPT-5.6-sol | 55.4 | **85.3** |
| GPT-5.6-luna | 55.4 | 83.2 |
| Claude Sonnet 4.6 | 50.5 | 78.5 |
| GPT-5.5 | 45.1 | 83.6 |
| GPT-5.4 mini | 23.9 | 53.5 |
| Qwen 3.5 35B-A3B | 7.6 | 42.4 |
| Qwen 3.5 9B | 6.5 | 24.1 |

No model clears 60 % perfect, and the gap opens on complexity: on tasks that
span 7+ apps Claude Opus 4.6 leads with ~36 % perfect, GPT-5.6 (sol/luna) and
Sonnet hold 18 %, GPT-5.5 9 %, and GPT-5.4 mini and both Qwen models drop
to 0 %. Failures cluster on long, multi-app trajectories — exactly where
personalization stresses an assistant most.

## Quick start

Start with the no-API sanity check. It proves the runner can fetch/boot the current
VM, reach the Control API, reset state, and write results before you spend API
budget on a real agent.

Host prerequisites: Linux with `/dev/kvm` and `qemu-system-x86_64`
(full list in [docs/NO_DOCKER.md](docs/NO_DOCKER.md) §2).

```bash
git clone https://github.com/ljang0/MyPCBench && cd MyPCBench
python3 -m venv .venv && source .venv/bin/activate   # Python ≥ 3.10; Ubuntu 24.04 is PEP-668
pip install -r requirements.txt

# Recommended first run: no API calls, no keys needed — current QEMU image,
# auto-refreshes ./mypcbench-vm
python3 agent-harness/run_mypcbench.py --backend qemu \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 --result_dir results/sanity-qemu

cp .env.example .env     # then add ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY for real runs
```

**No Docker** (recommended; works on GPU/compute nodes — no daemon, no root).
When `skopeo` is available, `get-eval-image.sh` fetches the Docker image and
extracts the bundled qcow2 + OVMF. Without `skopeo`, it falls back to the
matching HuggingFace qcow2; install `ovmf` separately or set
`MYPCBENCH_OVMF_CODE` if your distro does not ship it in a standard location.
`run_mypcbench.py --backend qemu` refreshes `latest` into the managed
`./mypcbench-vm` cache before default QEMU boots. To prefetch manually:

```bash
bash scripts/get-eval-image.sh --out ./mypcbench-vm
set -a; source .env; set +a           # API keys
source ./mypcbench-vm/env.sh          # exports MYPCBENCH_QCOW2 / MYPCBENCH_OVMF_*

# sanity: one task, no API cost — confirms the VM boots + Control API works
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 --result_dir results/sanity

python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type claude_cuabash --model claude-opus-4-6 \
  --tasks_dir tasks/final --max_steps 100 --result_dir results/opus

python3 agent-harness/judge_results.py --result_dir results/opus   # offline grade
```

A single sanity-check task is ≈1–3 min plus a one-time ~90 s VM boot; the full
`tasks/final` set (184 tasks) is a long, API-cost-heavy run — start with
`tasks/smoke_one`.

**Docker** alternative (QEMU-in-Docker wrapper): see
**[docs/DOCKER.md](docs/DOCKER.md)**, or
`bash scripts/run-agent.sh --parallel 1 --agent claude_cuabash --model claude-opus-4-6`
(spins its own container).

Full guide (every agent, local-vLLM Qwen, troubleshooting):
**[docs/NO_DOCKER.md](docs/NO_DOCKER.md)**.

### What Ships Here

This is an OSWorld-style runner-only repo. It ships the harness, paper agents,
184 tasks, rubrics, persona metadata, docs, and image-fetch/publish checks. It
does not ship the VM build source, generated app databases, local audit results,
paper drafts, or fetched qcow2 images. The runnable VM is distributed as the
Docker/QEMU image and matching HuggingFace qcow2 described below.

### Sanity-Check Setup Paths

Use `dummy` first on a new host; it costs no API calls and verifies boot,
Control API, app ports, and result writing.

```bash
# Docker wrapper, current image. Runner-owned starts pull latest before boot.
python3 agent-harness/run_mypcbench.py --backend docker \
  --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 --result_dir results/sanity-docker

# Parallel wrapper sanity check, one VM. Use --backend qemu or --backend docker.
python3 agent-harness/run_parallel_tasks.py --backend qemu \
  --tasks-file tasks/smoke_one/one.json --num-vms 1 \
  --agent-type dummy --model dummy --max-steps 4 \
  --result-dir results/sanity-parallel-qemu
```

## Image

The benchmark VM (v0.1, persona Michael Scott) ships as a Docker image and a
standalone qcow2 that are byte-identical. It is rebuilt and republished daily
so the seeded data always reads as "today". Fetch with **no docker/root** via
`scripts/get-eval-image.sh`.

| Docker Hub `ljang/mypcbench-qemu` | HF **dataset** [`ljang0/mypcbench-qemu-baseline`](https://huggingface.co/datasets/ljang0/mypcbench-qemu-baseline) |
|---|---|
| `:latest` (≡ `:michael_scott`, `:demo`, today's `:michael_scott-YYYY-MM-DD`) | `michael_scott.qcow2` |

The qcow2 lives in a HuggingFace **dataset** repo — note the `/datasets/` in the
URL above (`huggingface.co/datasets/ljang0/...`); `get-eval-image.sh` fetches it
with `repo_type="dataset"`. The daily freshness workflow verifies that `latest`,
the qcow2 embedded in the Docker image, and that dataset qcow2 all share one
digest.

Docker-backed runner-owned starts use `docker run --pull always`, so mutable
tags such as `latest` are refreshed before the VM starts. If you manually reuse
a pre-booted container, recreate it yourself to pick up a new daily image.
To run against a locally built image tag that has no registry to pull from,
set `MYPCBENCH_SKIP_PULL=1` (skips `--pull always`; everything else is
unchanged).
Direct-QEMU starts refresh the managed `./mypcbench-vm/mypcbench.qcow2` cache
from `latest` before booting, including when `MYPCBENCH_QCOW2` points at that
managed cache. If you point `--qcow2_path` or `MYPCBENCH_QCOW2` at a different
file, that explicit non-default qcow2 is treated as a pinned image and used
as-is. The runner logs the booted image's bake date and warns when it is
older than the daily-rebuild window (`MYPCBENCH_MAX_IMAGE_AGE_HOURS`,
default 36; set 0 to silence for intentionally pinned runs).

This release repo is runner-only and does not build the VM image. CI verifies
that the expected dated Docker tag exists, that `latest` points at that same
digest, and that HuggingFace has a byte-identical qcow2:

```bash
python3 scripts/check-release-image-freshness.py \
  --require-date-tag today \
  --max-latest-age-hours 36 \
  --check-docker-embedded
```

The image is rebuilt, republished, and re-verified daily by CI
(`.github/workflows/`); the freshness check above is the same gate CI runs.

Requirements: Linux + KVM (`/dev/kvm`) + QEMU, ~8 GB RAM and 4 vCPUs per VM (the default `env.py` launch line). Docker optional.

## Agents

Each agent uses its family's native tool-calling. `cuabash` = computer + bash, `cua` = computer only.

| `--agent_type` | Model | Tools | Paper role |
|---|---|---|---|
| `claude_cuabash` | `claude-opus-4-6` / `claude-sonnet-4-6` | computer + bash + editor | Claude main |
| `openai_cuabash` | `gpt-5.6-sol` / `gpt-5.6-luna` / `gpt-5.5` / `gpt-5.4-mini` | computer + OpenAI built-in shell | GPT main |
| `qwen_cuabash` | `Qwen/Qwen3.5-35B-A3B` / `-9B` | computer + bash (OSWorld-parity) | Qwen main |
| `qwen_cua` | same | computer only | Qwen appendix ablation |
| `dummy` | — | none | sanity / CI (no API cost) |

The runner injects `OPENAI_API_KEY` into the VM's chat apps so NPC personas
reply in-character (many tasks depend on it); without a key those apps still
work but auto-replies are disabled.

## Grading (decoupled, two-step)

1. **Run** — `run_mypcbench.py` writes `result.txt` (1.0 ran to completion / 0.0 errored) + `rubric_bundle.json` per task. It does *not* grade.
2. **Judge** — `python3 agent-harness/judge_results.py --result_dir <dir>` scores each bundle offline (no live VM) → `rubric_judge_result.json` per task + aggregate `scores.json`; idempotent (`--force` re-judges).

Each task has 3–18 weighted rubric criteria (per-task weights sum to 1; mean 6.1; 1,129 total). Default judge:
full-trajectory per-rubric on Gemini `gemini-3.1-flash-lite-preview` (the
paper config); override via `MYPCBENCH_RUBRIC_JUDGE_MODEL`.

## Citation & license

```bibtex
@misc{jang2026mypcbench,
  title  = {MyPCBench: A Benchmark for Personally Intelligent Computer-Use Agents},
  author = {Jang, Lawrence Keunho and Jang, Andrew Keunwoo and Koh, Jing Yu and Salakhutdinov, Ruslan},
  year   = {2026},
  url    = {https://mypcbench.com}
}
```

MIT License — see [LICENSE](LICENSE). The agent harness builds on
**OSWorld** (Apache-2.0, see [NOTICE](NOTICE)); the full-trajectory rubric
judge is ported from **[Odysseys](https://github.com/ljang0/Odysseys)**
(`scripts/python/run_full_trajectory_per_rubric.py`, CC-BY-4.0).
