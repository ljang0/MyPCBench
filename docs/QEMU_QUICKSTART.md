# MyPCBench QEMU Quickstart (Docker wrapper)

Run the Ubuntu 24.04 GNOME desktop inside a Docker container (QEMU-in-Docker).
For the no-Docker path (recommended on shared/compute nodes), see
[NO_DOCKER.md](NO_DOCKER.md).

## tl;dr

`run_mypcbench.py --backend docker` **starts and tears down its own
container per run, on auto-assigned free host ports** — nothing to boot
by hand, and no port collisions on shared/busy hosts. Runner-owned Docker
starts pass `docker run --pull always`, so the default `latest` tag is refreshed
before the VM boots.

```bash
# 1. Pull the current daily/OSS-polish Michael Scott image
docker pull ljang/mypcbench-qemu:latest

# 2. (optional) free no-API boot sanity — confirms VM + Control API
python3 agent-harness/run_mypcbench.py \
    --backend docker --docker_image ljang/mypcbench-qemu:latest \
    --agent_type dummy --model dummy \
    --tasks_dir tasks/smoke_one --max_steps 4 \
    --result_dir results/smoke-docker

# 3. Run an agent (one of the paper agents) — runner manages the container
python3 agent-harness/run_mypcbench.py \
    --backend docker --docker_image ljang/mypcbench-qemu:latest \
    --agent_type claude_cuabash --model claude-opus-4-6 \
    --tasks_dir tasks/final --max_steps 100 \
    --result_dir results/opus-docker

# 4. Grade the run (separate, offline step)
python3 agent-harness/judge_results.py --result_dir results/opus-docker
```

`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` stay host-side (the agent and the
judge run on the host); only `OPENAI_API_KEY` is injected into the VM, for
in-guest NPC chat auto-replies.

A single smoke task is ≈1–3 min plus a one-time ~90 s VM boot; the full
`tasks/final` set (184 tasks) is a long, API-cost-heavy run — start with
`tasks/smoke_one`.

## Reusing a pre-booted container (optional, advanced)

To keep one container warm across many runs, boot it yourself and point
the runner at it with `MYPCBENCH_REUSE_CONTAINER=1`. You now own the host
ports — the defaults below collide if already in use, so on a busy host
remap `5000` and set `MYPCBENCH_HOST_API_PORT` to match. Reuse mode does not
refresh an already-running container; recreate it when the daily image changes.

```bash
docker run -d --pull always --name mypcbench --privileged --device /dev/kvm \
  -p 5000:5000 -p 6080:6080 -p 2222:2222 -p 3001-3018:3001-3018 \
  -e OPENAI_API_KEY ljang/mypcbench-qemu:latest
# busy host? e.g. -p 27000:5000 …  and:  export MYPCBENCH_HOST_API_PORT=27000

MYPCBENCH_REUSE_CONTAINER=1 python3 agent-harness/run_mypcbench.py \
    --backend docker --docker_image ljang/mypcbench-qemu:latest \
    --container_name mypcbench \
    --agent_type claude_cuabash --model claude-opus-4-6 \
    --tasks_dir tasks/final --max_steps 100 \
    --result_dir results/opus-docker-reuse

python3 agent-harness/judge_results.py --result_dir results/opus-docker-reuse
```

## What you get

A real Ubuntu 24.04 VM seeded end-to-end for the single canonical persona
Michael Scott:

- **GNOME Shell** (Ubuntu Dock, Yaru theme), Firefox (auto-login cookies for
  every web app), LibreOffice, VS Code
- **17 MyPCBench web apps** as systemd services on ports 3001–3017
  (port 3018 is an additional in-VM service; the container maps the full
  `3001-3018` range)
- **Local mail** (Postfix + Dovecot, sandboxed) — no live-web traffic
- **OSWorld-compatible Control API** on port 5000 (`/screenshot`, `/execute`,
  `/accessibility`, `/file`, …); noVNC on 6080

Exact seeded-data counts are reported in the paper.

## Architecture

```
Host
 └── Docker container: mypcbench-qemu  (ubuntu:24.04 + qemu-system-x86 + noVNC)
       └── QEMU VM (Ubuntu 24.04, GNOME Shell, GDM auto-login)
             ├── systemd: mypcbench-apps.target (17 apps) + control-api :5000
             └── /data/*.sqlite                     (seeded app state)
                 /home/user/Maildir/                 (Dovecot mailbox)
                 /home/user/.mozilla/firefox/...     (seeded profile)
```

## Reset between tasks

`docker restart mypcbench` rebuilds a fresh CoW overlay from the pristine
baked qcow2 → clean VM state (~90 s). This is the canonical OSWorld-parity
hard reset; the harness does it automatically between tasks.

## Agent modalities

| Agent type | Model | Paper role |
|------------|-------|------------|
| `claude_cuabash` | `claude-opus-4-6` / `claude-sonnet-4-6` | Claude main (computer + bash + editor) |
| `openai_cuabash` | `gpt-5.5` / `gpt-5.4-mini` | GPT main (computer + OpenAI built-in shell) |
| `qwen_cuabash` | `Qwen/Qwen3.5-35B-A3B` / `-9B` | Qwen main (OSWorld-parity computer + bash) |
| `qwen_cua` | same | Qwen appendix ablation (computer only) |
| `dummy` | — | smoke / CI (no API cost) |

Paper agents only — no MCP, no multi-agent. Full per-agent commands:
[NO_DOCKER.md](NO_DOCKER.md).

## Troubleshooting

**"Could not open /dev/kvm"** — add your user to the `kvm` group
(`sudo usermod -aG kvm $USER`, then re-login).

**Control API not responding on :5000** — give the VM ~60–90 s to boot;
check `curl -s http://localhost:5000/health`.
