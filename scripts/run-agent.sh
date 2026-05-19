#!/usr/bin/env bash
# run-agent.sh — thin wrapper around agent-harness/run_mypcbench.py with
# sane defaults for the published QEMU image. Loads .env for API keys.
#
# Usage (fresh machine — --parallel spins its own container, no setup):
#   bash scripts/run-agent.sh --parallel 1                       # claude_cuabash, claude-opus-4-6, all tasks
#   bash scripts/run-agent.sh --parallel 1 --agent openai_cuabash --model gpt-5.5
#   bash scripts/run-agent.sh --parallel 1 --tasks tasks/final/gringotts
#   bash scripts/run-agent.sh --parallel 4                       # 4× parallel VMs
#
# Without --parallel this uses SINGLE-VM mode, which attaches to an
# ALREADY-booted container (default name `mypcbench`); see the
# "Reusing a pre-booted container" section of docs/QEMU_QUICKSTART.md.
# On a fresh host, prefer --parallel.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

AGENT="claude_cuabash"
MODEL="claude-opus-4-6"
TASKS="tasks/final"
RESULT="results/$(date +%Y%m%d_%H%M%S)"
CONTAINER="mypcbench"
IMAGE="ljang/mypcbench-qemu:eval-round0"
MAX_STEPS=100
PARALLEL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --agent)     AGENT="$2"; shift 2 ;;
    --model)     MODEL="$2"; shift 2 ;;
    --tasks)     TASKS="$2"; shift 2 ;;
    --result)    RESULT="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --image)     IMAGE="$2"; shift 2 ;;
    --max-steps) MAX_STEPS="$2"; shift 2 ;;
    --parallel)  PARALLEL="$2"; shift 2 ;;
    -h|--help)   sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Load .env if present so API keys flow to the agent + grader + NPC replies
if [ -f "$REPO_ROOT/.env" ]; then
  set -a; . "$REPO_ROOT/.env"; set +a
fi

mkdir -p "$RESULT"

if [ "$PARALLEL" -gt 0 ]; then
  # Parallel mode spins N VMs itself — no preexisting container needed
  TASKS_FILE="$TASKS"
  [ -d "$TASKS" ] && TASKS_FILE="$TASKS/all_tasks_with_grading.json"
  exec python3 agent-harness/run_parallel_tasks.py \
      --backend docker --docker-image "$IMAGE" \
      --tasks-file "$TASKS_FILE" \
      --num-vms "$PARALLEL" \
      --agent-type "$AGENT" \
      --model "$MODEL" \
      --max-steps "$MAX_STEPS" \
      --result-dir "$RESULT" \
      --timeout-per-vm 86400
fi

# Single-VM mode — assumes $CONTAINER is already booted
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "!! container '$CONTAINER' not found. Boot it first (see" >&2
  echo "   docs/QEMU_QUICKSTART.md for the docker run command)," >&2
  echo "   or use --parallel N (spins its own VMs)." >&2
  exit 1
fi

# $CONTAINER already booted — attach to it (don't recreate).
export MYPCBENCH_REUSE_CONTAINER=1
exec python3 agent-harness/run_mypcbench.py \
    --backend docker \
    --docker_image "$IMAGE" \
    --container_name "$CONTAINER" \
    --agent_type "$AGENT" \
    --model "$MODEL" \
    --tasks_dir "$TASKS" \
    --result_dir "$RESULT" \
    --max_steps "$MAX_STEPS"
