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

| Set | What it is | Docker Hub `ljang/mypcbench-qemu` (image digest) | HF qcow2 (file sha256) |
|-----|-----------|--------------------------------------------------|------------------------|
| **`eval-round0`** | **Canonical paper baseline** (`v1.2.15-round78e`). Reproduce the paper numbers with this. | `:eval-round0` (≡ `:eval-round0-michael_scott`) · `sha256:86d4da6575ebf5adbfa3229389f341445245d6d7f2bee6340150858b9a8dbdcf` | `michael_scott_round78e.qcow2` · `sha256:c7209624dfae24ecf2cde90233097ec19797ae770c2ae4e201892046c51d1fb6` |
| **`latest`** | More fleshed-out, polished build (`v1.2.16-oss-polish`) with expanded seeded catalogs (HangryDash, Kwik-E-Mart, Dinoco). Use for development and exploration; not the paper baseline. | `:v1.2.16-oss-polish-michael_scott` (≡ `:demo`, `:michael_scott`) · `sha256:eb99138a12487c09e48c1b31a05bcf8811cc48e28b1c8cb99b57c736a027cdea` | `michael_scott.qcow2` · `sha256:facabd91778b79a621c3d33d4b4b73c9c13c52cfdea8e9d3db34bd216806dc0a` |

The qcow2 baked inside each Docker `-qemu` image is **byte-identical** to
the file with the same sha256 on HuggingFace — both fetch paths produce
the same VM disk. Verify with
`docker run --rm --entrypoint sha256sum ljang/mypcbench-qemu:<tag> /baseline/mypcbench.qcow2`.

Both are the **Michael Scott** persona, pre-baked for instant boot. The
in-VM `mypcbench-date-rebase` service shifts all seeded dates by
`now − bake_time` on every boot, so the data always reads as "today".

Pick `eval-round0` for paper reproduction; `latest` for everything else.

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
- `skopeo` (for the no-docker image fetch) **or** `huggingface_hub`.

No root needed for any of the above. If QEMU itself isn't installed and
you have no sudo, extract it from RPMs — see the snippet at the bottom.

---

## 3. Fetch an image (no docker daemon required)

```bash
# Paper baseline (≈6 GB compressed pull → expands to a ~12 GB qcow2;
# extracts qcow2 + OVMF, no docker, no root):
bash scripts/get-eval-image.sh --set eval-round0 --out ./mypcbench-vm

# …or the latest OSS image:
bash scripts/get-eval-image.sh --set latest --out ./mypcbench-vm

source ./mypcbench-vm/env.sh   # exports MYPCBENCH_QCOW2 / MYPCBENCH_OVMF_*
```

`get-eval-image.sh` uses `skopeo` to pull the published QEMU image with
**no docker daemon**, then extracts the baked `mypcbench.qcow2` and the
`OVMF_CODE/VARS.fd` firmware from its layers. (`--method hf` instead
downloads the raw qcow2 from the HuggingFace dataset
`ljang0/mypcbench-qemu-baseline`, but does not ship OVMF.)

---

## 4. API keys

```bash
cp .env.example .env
# edit .env:
#   ANTHROPIC_API_KEY=sk-ant-...   # Claude agents
#   OPENAI_API_KEY=sk-...          # GPT agents
#   GEMINI_API_KEY=AIza...         # rubric judge (judge_results.py, Gemini gemini-3.1-flash-lite-preview)
```

The runner reads keys from the **process environment**, not `.env`
directly — load it into your shell before any bare `python3
agent-harness/...` command: `set -a; source .env; set +a`. (The
`scripts/*.sh` wrappers source `.env` for you.) `.env` is git-ignored —
never commit keys.

---

## 5. Run the paper agents (exact commands)

All four paper agents, against the canonical task set:

```bash
source ./mypcbench-vm/env.sh       # MYPCBENCH_QCOW2 / OVMF
set -a; source .env; set +a        # API keys into the environment

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

## 7. One-command smoke (do this on the handoff node first)

```bash
bash scripts/get-eval-image.sh --set eval-round0 --out ./mypcbench-vm
source ./mypcbench-vm/env.sh
set -a; source .env; set +a                # API keys into the env

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
`--backend docker --docker_image ljang/mypcbench-qemu:eval-round0`.
