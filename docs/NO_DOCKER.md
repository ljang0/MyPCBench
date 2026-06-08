# Running MyPCBench WITHOUT Docker (QEMU-direct)

This is the **recommended path on a shared GPU/compute node** (no docker
daemon, no root). The benchmark harness boots the VM image directly with
`qemu-system-x86_64` + KVM — Docker is only an optional convenience
wrapper, never a requirement. `--backend qemu` is the runner default.

> Validated end-to-end on a no-docker, no-sudo node: image fetched with
> `skopeo` (no docker daemon), booted with a user-space QEMU, agents driven
> via the Control API; rubrics scored post-run with
> `agent-harness/judge_results.py`.

---

## 1. The two image sets

| Set | What it is | Docker Hub `ljang/mypcbench-qemu` (image digest) | No-Docker image source |
|-----|-----------|--------------------------------------------------|------------------------|
| **`latest`** | **Default current benchmark VM.** Tracks the daily/OSS-polish image so fresh tasks use the freshest published environment. | `:latest` (≡ `:v1.2.47-oss-polish`, `:demo`, `:michael_scott`, today's `:michael_scott-YYYY-MM-DD`) · `sha256:471e10e2d5d32608c594896bd159d3ca55c4c60e72d3dc877997b5226f75f82a` | `michael_scott.qcow2` · `sha256:c970a526e1ce2192ff4fca2fa415f5736f2cc291d2be9e90e545a8c0f58a3d84` |
| **`eval-round0`** | **Archived v0.0 paper baseline** (`v1.2.15-round78e`). Use only to reproduce the paper numbers. | `:eval-round0` (≡ `:eval-round0-michael_scott`) · `sha256:86d4da6575ebf5adbfa3229389f341445245d6d7f2bee6340150858b9a8dbdcf` | `michael_scott_round78e.qcow2` · `sha256:c7209624dfae24ecf2cde90233097ec19797ae770c2ae4e201892046c51d1fb6` |

The qcow2 baked inside each Docker `-qemu` image is **byte-identical** to the
file with the same sha256 on HuggingFace. Verify Docker-image contents with
`docker run --rm --entrypoint sha256sum ljang/mypcbench-qemu:<tag> /baseline/mypcbench.qcow2`.

Both are the **Michael Scott** persona, pre-baked for instant boot. The
in-VM `mypcbench-date-rebase` service shifts all seeded dates by
`now − bake_time` on every boot, so the data always reads as "today".

Pick `latest` for normal runs and release checks. Pick `eval-round0` only for
paper reproduction.

The runner-only release repo does not build the VM image. It verifies the
daily publisher by requiring a current dated Docker tag, requiring `latest` to
point at the same digest, and comparing Docker's embedded qcow2 with the
HuggingFace `michael_scott.qcow2` LFS sha:

```bash
python3 scripts/check-release-image-freshness.py \
  --require-date-tag today \
  --max-latest-age-hours 36 \
  --check-docker-embedded
```

The scheduled publisher workflow is
`.github/workflows/release-image-publisher.yml`. It requires repository
secrets `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and `HF_TOKEN`.

---

## 2. Prerequisites

- Linux host, **`/dev/kvm`** accessible (TCG software emulation works but
  is ~20× slower — fine for a single smoke, not for a full run).
- `qemu-system-x86_64` and `qemu-img` (QEMU ≥ 6).
- **OVMF** UEFI firmware (the qcow2 is GPT/UEFI and will not boot
  without it). The `skopeo` fetch method below **extracts OVMF from the
  image for you** — nothing to install. Otherwise:
  `apt install ovmf` / `dnf install edk2-ovmf` / `pacman -S edk2-ovmf`.
- **Python ≥ 3.9**; deps: `pip install -r requirements.txt`.
- `skopeo` (for Docker-image extraction) **or** `huggingface_hub` (for the
  HuggingFace qcow2 fallback).

No root needed for any of the above. If QEMU itself isn't installed and
you have no sudo, extract it from RPMs — see the snippet at the bottom.

---

## 3. Fetch an image (no docker daemon required)

```bash
# Current daily/OSS-polish image (default; ≈6 GB compressed pull → expands to
# a ~12 GB qcow2; extracts qcow2 + OVMF, no docker, no root):
bash scripts/get-eval-image.sh --out ./mypcbench-vm

# Archived v0.0 paper baseline, only for reproducing paper numbers:
bash scripts/get-eval-image.sh --set eval-round0 --out ./mypcbench-vm-paper

source ./mypcbench-vm/env.sh   # exports MYPCBENCH_QCOW2 / MYPCBENCH_OVMF_*
```

When `skopeo` is available, `get-eval-image.sh` pulls the published QEMU image
with **no docker daemon**, then extracts the baked `mypcbench.qcow2` and the
`OVMF_CODE/VARS.fd` firmware from its layers. Without `skopeo`, the script
downloads the raw qcow2 from the HuggingFace dataset
`ljang0/mypcbench-qemu-baseline`; this does not ship OVMF, so install OVMF or
set `MYPCBENCH_OVMF_CODE`.

If `run_mypcbench.py --backend qemu` or `run_parallel_tasks.py --backend qemu`
uses the managed default cache, the runner automatically refreshes `latest`
into `./mypcbench-vm` before booting. This also applies when
`MYPCBENCH_QCOW2` points at `./mypcbench-vm/mypcbench.qcow2`, so the default
cache cannot silently fall behind the daily/current image. Explicit non-default
qcow2 paths are respected as pinned images and are not replaced.

On a busy host where the default direct-QEMU ports are already occupied,
choose an unused port window before running the benchmark:

```bash
export MYPCBENCH_HOST_API_PORT=43000
export MYPCBENCH_HOST_VNC_PORT=43001
export MYPCBENCH_HOST_SSH_PORT=43002
for cp in $(seq 3001 3018); do
  export MYPCBENCH_HOST_APP_PORT_${cp}=$((43010 + cp - 3001))
done
```

---

## 4. API keys

```bash
cp .env.example .env
# edit .env:
#   ANTHROPIC_API_KEY=<your Anthropic key>   # Claude agents
#   OPENAI_API_KEY=<your OpenAI key>         # GPT agents
#   GEMINI_API_KEY=<your Gemini key>         # rubric judge (judge_results.py, Gemini gemini-3.1-flash-lite-preview)
```

The runner reads keys from the **process environment**, not `.env`
directly — load it into your shell before any bare `python3
agent-harness/...` command: `set -a; source .env; set +a`. (The
`scripts/*.sh` wrappers source `.env` for you.) `.env` is git-ignored —
never commit keys.

---

## 5. Run the agents

All four paper agents, against the canonical task set. With the default image
fetch above, these run against the current `latest` VM. To reproduce paper
scores exactly, first fetch `--set eval-round0` and use that env file.

```bash
set -a; source .env; set +a        # API keys into the environment
source ./mypcbench-vm/env.sh       # MYPCBENCH_QCOW2 / OVMF

# Claude Opus 4.6  (paper main)
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type claude_cuabash --model claude-opus-4-6 \
  --tasks_dir tasks/final --max_steps 100 --result_dir results/opus

# Claude Sonnet 4.6
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type claude_cuabash --model claude-sonnet-4-6 \
  --tasks_dir tasks/final --max_steps 100 --result_dir results/sonnet

# GPT-5.5  (computer + OpenAI built-in shell)
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type openai_cuabash --model gpt-5.5 \
  --tasks_dir tasks/final --max_steps 100 --result_dir results/gpt55

# Qwen3.5-35B-A3B  (OSWorld-parity, computer+bash; needs local vLLM, see §6)
OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=dummy \
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" \
  --agent_type qwen_cuabash --model Qwen/Qwen3.5-35B-A3B \
  --tasks_dir tasks/final --max_steps 100 --result_dir results/qwen35
```

Paper agent map (`agent_type` → vendor surface):

| Model | `--agent_type` | `--model` | Surface |
|-------|----------------|-----------|---------|
| Claude Opus 4.6 | `claude_cuabash` | `claude-opus-4-6` | computer + bash + editor (native) |
| Claude Sonnet 4.6 | `claude_cuabash` | `claude-sonnet-4-6` | computer + bash + editor (native) |
| GPT-5.5 / GPT-5.4-mini | `openai_cuabash` | `gpt-5.5` / `gpt-5.4-mini` | computer + OpenAI built-in `shell` |
| Qwen3.5-35B-A3B / 9B | `qwen_cuabash` | `Qwen/Qwen3.5-35B-A3B` / `-9B` | OSWorld-parity (computer + bash) |

`qwen_cua` is the appendix cua_only ablation (Qwen, computer only). This
OSS release ships **paper agents only** — no MCP-hybrid, no multi-agent.

---

## 6. Qwen via local vLLM (2× 40 GB GPUs)

`qwen_cuabash` / `qwen_cua` talk to any OpenAI-compatible endpoint. To
serve Qwen3.5-35B-A3B locally:

```bash
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
vllm serve Qwen/Qwen3.5-35B-A3B \
  --served-model-name Qwen/Qwen3.5-35B-A3B \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.92 \
  --max-model-len 12288 --max-num-seqs 1 \
  --enforce-eager \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --port 8000 --trust-remote-code
```

> **`--enforce-eager` is required on 2× 40 GB.** The bf16 35B weights are
> ≈33.5 GB/GPU; full CUDA-graph capture spikes past 40 GB and OOMs. Eager
> mode removes that spike (small latency cost, irrelevant for eval). On
> 2× 80 GB you can drop `--enforce-eager` and raise `--max-model-len`.
> `9B` fits comfortably on a single 40 GB GPU (`--tensor-parallel-size 1`,
> no `--enforce-eager` needed).

Wait for `Application startup complete`, then point the runner at
`OPENAI_BASE_URL=http://localhost:8000/v1` with any dummy `OPENAI_API_KEY`.

---

## 7. One-command smoke (do this on a new host first)

```bash
bash scripts/get-eval-image.sh --out ./mypcbench-vm
set -a; source .env; set +a                # API keys into the env
source ./mypcbench-vm/env.sh

# No-API sanity: one task, dummy agent — confirms VM boot + Control API
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" --agent_type dummy --model dummy \
  --tasks_dir tasks/smoke_one --max_steps 4 --result_dir results/smoke

# Real-agent sanity (needs the key): same one task, a paper agent
python3 agent-harness/run_mypcbench.py --backend qemu \
  --qcow2_path "$MYPCBENCH_QCOW2" --agent_type claude_cuabash \
  --model claude-opus-4-6 --tasks_dir tasks/smoke_one --max_steps 30 \
  --result_dir results/smoke-claude
```

`rc=0` with a written `result.txt` means the agent + key + the VM path
all work (`result.txt` is a completion marker, not a score).

Then grade the run — a separate step, same for the docker and qemu backends:

```bash
python3 agent-harness/judge_results.py --result_dir results/smoke-claude
```

This writes `rubric_judge_result.json` per task plus an aggregate
`scores.json`. It needs `GEMINI_API_KEY` (paper judge:
`gemini-3.1-flash-lite-preview`) and no live VM.

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `OVMF UEFI firmware not found` | Use the `skopeo` fetch (ships OVMF), or set `MYPCBENCH_OVMF_CODE=/path/OVMF_CODE.fd`. |
| `QEMU exited immediately` | Check `/tmp/mypcbench-<name>-stderr.log` & `-serial.log`. Usually missing KVM perms or a bad OVMF path. |
| Control API never ready | First boot is slow on TCG (no `/dev/kvm`). Give it 3–5 min, or get KVM. |
| vLLM `CUDA out of memory` | Add `--enforce-eager`, lower `--max-model-len`, `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (see §6). |
| Overlay disk fills `/tmp` | The runner writes `/tmp/mypcbench-*-overlay.qcow2`. Point `TMPDIR` / free space at a big volume. |
| No `qemu` / no sudo | Extract from RPMs: `mkdir -p ~/qemu-local/extracted` → `rpm2cpio qemu-kvm-core*.rpm \| cpio -idm` → wrap `usr/libexec/qemu-kvm` as `~/.local/bin/qemu-system-x86_64`. |

---

## 9. Docker path (optional)

If you *do* have Docker + `--privileged` + `/dev/kvm`, the QEMU-in-Docker
wrapper is still supported and gives identical task results. See
**[QEMU_QUICKSTART.md](QEMU_QUICKSTART.md)** for the one-line `docker run`,
then let the parallel runner spin its own container:

```bash
bash scripts/run-agent.sh --parallel 1 --agent claude_cuabash --model claude-opus-4-6
```

Everything in this doc works the same with
`--backend docker --docker_image ljang/mypcbench-qemu:latest`. Runner-owned
Docker starts pull the tag before booting. For paper reproduction, pass
`--docker_image ljang/mypcbench-qemu:eval-round0` explicitly.
