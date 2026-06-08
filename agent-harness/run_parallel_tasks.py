#!/usr/bin/env python3
"""Parallel task runner for MyPCBench.

Splits a JSON of N tasks across X VM replicas (same persona) and runs them
concurrently — each replica processes N/X tasks. Each replica is a separate
Docker container with its own CoW overlay, so state is fully isolated.

Usage
-----

    # NOTE: --backend defaults to qemu. Default QEMU runs refresh the managed
    # ./mypcbench-vm cache from the current latest image. Pass --backend docker
    # to use --docker-image
    # (QEMU-in-Docker wrapper).

    # Run the 184-task set with Claude Opus 4.6 on 4 parallel VMs
    python3 agent-harness/run_parallel_tasks.py \
        --backend docker --docker-image ljang/mypcbench-qemu:latest \
        --tasks-file tasks/final/all_tasks_with_grading.json \
        --num-vms 4 \
        --agent-type claude_cuabash --model claude-opus-4-6 \
        --result-dir ./results/opus

    # Qwen 3.5 35B via vLLM
    OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:8000/v1 \
    python3 agent-harness/run_parallel_tasks.py \
        --backend docker --docker-image ljang/mypcbench-qemu:latest \
        --tasks-file tasks/final/all_tasks_with_grading.json \
        --num-vms 4 \
        --agent-type qwen_cuabash \
        --model Qwen/Qwen3.5-35B-A3B

The task JSON must be a list of task objects compatible with agent-harness's
single-task schema (id, instruction, grading, etc.). Each VM gets roughly
ceil(N / num_vms) tasks.

Each VM replica:
  - Gets a unique container name:  <base-name>-vm{idx}
  - Gets isolated host port range:  <base-api>+idx*100 for API, VNC, etc.
  - Writes its results to:  <result-dir>/vm{idx}/...
  - Uses a hard-reset (docker restart) between tasks for clean state

Isolation invariant (important!)
--------------------------------
This runner is for **throughput** — N independent tasks on the same
persona, fanned out across X VMs. Each replica MUST be fully isolated:
a message posted in VM0's buzzchat must NOT appear in VM1. We get this
for free as long as we don't bind-mount any shared host directory into
the containers: each replica gets its own filesystem, its own
`/data/worlds/<world>/*.sqlite`, its own Maildir, its own Firefox
profile.

Do **not** add `-v /some/host/dir:/data/worlds` or any similar bind
mount to the `docker run` command below — each replica must stay
isolated.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Ensure the project root is on sys.path so the agents package resolves
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

logger = logging.getLogger("mypcbench.parallel")


def _managed_qcow2_path() -> Path:
    return Path(__file__).resolve().parent.parent / "mypcbench-vm" / "mypcbench.qcow2"


def _ensure_current_qcow2(qcow2_path: str | None) -> str | None:
    """Refresh the managed default QEMU image before default QEMU runs.

    Explicit non-default qcow2 paths are pinned inputs. The managed cache is
    refreshed even when MYPCBENCH_QCOW2 points at it, so a stale local default
    cannot silently shadow the daily/latest image.
    """
    repo_root = Path(__file__).resolve().parent.parent
    managed = _managed_qcow2_path().resolve()
    requested = Path(qcow2_path).expanduser().resolve() if qcow2_path else None
    if requested and requested != managed:
        logger.info("Using explicit qcow2-path %s; skipping latest auto-refresh for pinned image", requested)
        return str(requested)

    out_dir = managed.parent
    fetch = repo_root / "scripts" / "get-eval-image.sh"
    logger.info("Refreshing current latest QEMU image in managed cache %s", out_dir)
    subprocess.run(
        ["bash", str(fetch), "--set", "latest", "--out", str(out_dir)],
        check=True,
    )
    return str(managed)


# ── LLM auto-reply injection ────────────────────────────────────────────

def _load_openai_key() -> str | None:
    """Read OPENAI_API_KEY from process env, with project .env fallback."""
    env_key = (os.environ.get("OPENAI_API_KEY") or "").strip().strip('"')
    if env_key:
        return env_key
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _inject_llm_env(api_port: int, container_name: str) -> None:
    """Inject OPENAI_API_KEY into chat app services inside the VM.

    This enables LLM-powered auto-replies in BuzzChat and WorkBuzz so
    that non-agent personas (Jim, Dwight, etc.) respond to messages
    sent by the agent.  Uses gpt-5.4-mini by default (the shared
    @mypcbench/llm client default).
    """
    import base64
    import urllib.request

    api_key = _load_openai_key()
    if not api_key:
        logger.warning("No OPENAI_API_KEY in environment or .env — chat auto-replies disabled")
        return

    # 1. Write a shell script via base64 to avoid escaping issues with
    #    API keys that contain special characters.
    script = (
        "#!/bin/bash\n"
        "set -e\n"
        f"KEY='{api_key}'\n"
        # Write to /etc/mypcbench-env so the in-VM grader
        # (llm_fuzzy_match) can source it at eval time.
        "grep -q OPENAI_API_KEY /etc/mypcbench-env 2>/dev/null && "
        '  sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$KEY|" /etc/mypcbench-env || '
        '  echo "OPENAI_API_KEY=$KEY" >> /etc/mypcbench-env\n'
        # Also inject into chat app systemd services for NPC replies.
        "for svc in mypcbench-buzzchat mypcbench-workbuzz; do\n"
        "  F=/etc/systemd/system/${svc}.service\n"
        '  [ -f "$F" ] || continue\n'
        '  sed -i "/OPENAI_API_KEY/d" "$F"\n'
        '  sed -i "/^ExecStart/i Environment=OPENAI_API_KEY=$KEY" "$F"\n'
        "done\n"
        "systemctl daemon-reload\n"
        "systemctl restart mypcbench-buzzchat mypcbench-workbuzz 2>/dev/null || true\n"
    )
    b64_script = base64.b64encode(script.encode()).decode()
    try:
        cmd = f"echo {b64_script} | base64 -d > /tmp/_llm_setup.sh && sudo bash /tmp/_llm_setup.sh"
        payload = json.dumps({"command": cmd}).encode()
        req = urllib.request.Request(
            f"http://localhost:{api_port}/execute",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode())
        if result.get("returncode", 1) == 0:
            logger.info("VM %s: LLM auto-reply key injected", container_name)
        else:
            logger.warning("VM %s: LLM inject error: %s", container_name, result.get("error", "")[:200])
    except Exception as e:
        logger.warning("VM %s: LLM inject failed: %s", container_name, e)


# ── Free-port-window discovery ─────────────────────────────────────────

def _is_port_free(port: int) -> bool:
    """True if port is bindable on 0.0.0.0 right now."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _allocate_port_windows(start: int, num_vms: int, stride: int = 30,
                           max_advance: int = 50) -> list[int]:
    """Pick `num_vms` non-overlapping 30-port windows starting at `start`.

    Each VM needs ports {base, base+1, base+2, base+3, base+10..base+27}.
    We probe every port in the 30-wide window; if any is held, we slide
    the entire batch forward by `stride` and retry. This way two
    parallel-runner invocations on the same host stay deterministic
    within their own launch but never silently collide with a prior job.
    """
    bases = [start + i * stride for i in range(num_vms)]
    for advance in range(max_advance):
        offset = advance * stride * num_vms
        candidates = [b + offset for b in bases]
        # Only ports we actually bind: api, vnc, novnc, ssh, plus app slots
        used_offsets = [0, 1, 2, 3] + list(range(10, 28))
        all_free = all(
            _is_port_free(c + o)
            for c in candidates
            for o in used_offsets
        )
        if all_free:
            if advance > 0:
                logger.info("Port window %d-%d busy; using %d-%d instead",
                            start, bases[-1] + stride - 1,
                            candidates[0], candidates[-1] + stride - 1)
            return candidates
    raise RuntimeError(
        f"Could not find {num_vms} free {stride}-port windows starting at "
        f"{start} after {max_advance} attempts. Pass --port-base with a "
        f"different range, or stop conflicting QEMU/Docker processes."
    )


# ── Worker: runs a subset of tasks on one VM ────────────────────────────

def run_vm_batch(
    vm_idx: int,
    tasks: list[dict],
    args_dict: dict,
    port_base: int,
) -> dict:
    """Run a batch of tasks against one VM replica. Returns per-task scores."""
    import os
    import subprocess

    result_dir = Path(args_dict["result_dir"]) / f"vm{vm_idx}"
    result_dir.mkdir(parents=True, exist_ok=True)

    # Write the tasks into a per-VM directory run_mypcbench.py can pick up
    tasks_dir = result_dir / "_tasks"
    tasks_dir.mkdir(exist_ok=True)
    tasks_file = tasks_dir / "batch.json"
    tasks_file.write_text(json.dumps(tasks))

    container_name = f"{args_dict['container_base']}-vm{vm_idx}"
    api_port = port_base  # host port that maps to container's 5000
    vnc_port = port_base + 1
    novnc_port = port_base + 2
    ssh_port = port_base + 3
    app_port_start = port_base + 10  # host: app_port_start .. +17 -> guest 3001..3018
    # (3001-3017 are the 17 Next.js web apps; 3018 is the LibreOffice service)

    backend = args_dict.get("backend", "qemu")

    if backend == "qemu":
        # --- QEMU-direct: create overlay + launch QEMU ---
        qcow2_path = args_dict.get("qcow2_path") or os.environ.get("MYPCBENCH_QCOW2")
        if not qcow2_path:
            return {"vm_idx": vm_idx,
                    "error": "QEMU backend did not receive a qcow2 path. The "
                             "parent runner should refresh latest unless "
                             "--qcow2-path/MYPCBENCH_QCOW2 was misconfigured.",
                    "scores": []}
        overlay_path = f"/tmp/mypcbench-{container_name}-overlay.qcow2"
        pidfile = f"/tmp/mypcbench-{container_name}.pid"
        vars_qcow2 = f"/tmp/mypcbench-{container_name}-vars.qcow2"

        # Clean up any stale state
        for f in [overlay_path, pidfile, vars_qcow2]:
            if os.path.exists(f):
                os.unlink(f)

        # Create CoW overlay
        proc = subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-b", qcow2_path, "-F", "qcow2", overlay_path],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return {"vm_idx": vm_idx, "error": f"qemu-img create failed: {proc.stderr}", "scores": []}

        # Build hostfwd
        vnc_display = vnc_port - 5900
        hostfwd = f"hostfwd=tcp::{api_port}-:5000,hostfwd=tcp::{vnc_port}-:5901"
        hostfwd += f",hostfwd=tcp::{ssh_port}-:22"
        for i in range(18):
            hostfwd += f",hostfwd=tcp::{app_port_start + i}-:{3001 + i}"

        # KVM detection
        kvm_args = ["-enable-kvm", "-cpu", "host"] if os.path.exists("/dev/kvm") else ["-cpu", "qemu64"]

        # UEFI firmware — shared discovery with env.py. Honors
        # MYPCBENCH_OVMF_CODE / _VARS overrides, then auto-globs standard
        # distro locations. Raises a clear error (caught below) if no
        # OVMF is present on the host.
        try:
            from env import _discover_ovmf
            code_path, vars_src = _discover_ovmf()
        except RuntimeError as e:
            return {"vm_idx": vm_idx, "error": f"OVMF discovery failed: {e}",
                    "scores": []}
        subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-f", "raw", vars_src, vars_qcow2],
            capture_output=True, check=True,
        )
        uefi_args = [
            "-drive", f"if=pflash,format=raw,readonly=on,file={code_path}",
            "-drive", f"if=pflash,format=qcow2,file={vars_qcow2}",
        ]

        # Serial log defaults to local /tmp; set MYPCBENCH_DIAG_DIR to
        # persist it elsewhere for post-mortem inspection.
        diag_dir = os.environ.get("MYPCBENCH_DIAG_DIR")
        if diag_dir:
            os.makedirs(diag_dir, exist_ok=True)
            serial_log = f"{diag_dir}/{container_name}-serial.log"
        else:
            serial_log = f"/tmp/mypcbench-{container_name}-serial.log"
        qemu_cmd = [
            "qemu-system-x86_64", "-name", container_name,
            "-machine", "q35,accel=kvm:tcg", *kvm_args,
            "-m", "8G", "-smp", "4",
            *uefi_args,
            "-drive", f"file={overlay_path},format=qcow2,if=virtio,cache=unsafe,aio=threads",
            "-netdev", f"user,id=net0,{hostfwd}",
            "-device", "virtio-net-pci,netdev=net0",
            "-vga", "virtio", "-vnc", f":{vnc_display}",
            "-display", "none",
            "-serial", f"file:{serial_log}",
            "-monitor", "none",
        ]
        # Use Popen (daemonize is incompatible with display none)
        stderr_log = f"/tmp/mypcbench-{container_name}-stderr.log"
        with open(stderr_log, "w") as sf:
            qemu_proc = subprocess.Popen(
                qemu_cmd,
                stdout=subprocess.DEVNULL,
                stderr=sf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        # Write PID file
        with open(pidfile, "w") as pf:
            pf.write(str(qemu_proc.pid))
        time.sleep(2)
        if qemu_proc.poll() is not None:
            stderr_msg = open(stderr_log).read().strip() if os.path.exists(stderr_log) else ""
            return {"vm_idx": vm_idx, "error": f"QEMU start failed: {stderr_msg}", "scores": []}

    else:
        # --- Docker backend (unchanged) ---
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        cmd = [
            "docker", "run", "-d",
            "--pull", "always",
            "--name", container_name,
            "--privileged",
            "--device", "/dev/kvm",
            "-p", f"{api_port}:5000",
            "-p", f"{vnc_port}:5901",
            "-p", f"{novnc_port}:6080",
            "-p", f"{ssh_port}:2222",
        ]
        # Forward 17 Next.js app ports (3001-3017) + LibreOffice service (3018)
        for i in range(18):
            cmd.extend(["-p", f"{app_port_start + i}:{3001 + i}"])
        cmd.append(args_dict["docker_image"])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return {
                "vm_idx": vm_idx,
                "error": f"docker run failed: {proc.stderr}",
                "scores": [],
            }

    # Wait for Control API. v1.2.16+ qcow2 ships canon-patch.service that
    # blocks mypcbench-apps.target (Flask backend + all web apps) while it
    # runs 28 patchers including importing 1556 Maildir files. On KVM this
    # takes 8-12 min; on TCG software emulation 60-90 min. Default 900s
    # (180×5) covers the KVM case with headroom; bump to 1200+ if you ever
    # see TCG-emulated nodes. Override via MYPCBENCH_API_READY_ATTEMPTS env.
    api_ready_attempts = int(os.environ.get("MYPCBENCH_API_READY_ATTEMPTS", "180"))
    for _ in range(api_ready_attempts):
        time.sleep(5)
        try:
            import urllib.request
            urllib.request.urlopen(f"http://localhost:{api_port}/health", timeout=3)
            break
        except Exception:
            continue
    else:
        if backend == "qemu":
            # Kill QEMU process
            if os.path.exists(pidfile):
                try:
                    pid = int(open(pidfile).read().strip())
                    os.kill(pid, 9)
                except Exception:
                    pass
        else:
            subprocess.run(["docker", "rm", "-f", container_name])
        return {"vm_idx": vm_idx, "error": "API never ready", "scores": []}

    # ── Inject LLM auto-reply support ──────────────────────────────
    # Chat apps (buzzchat, workbuzz) generate NPC replies via an
    # OpenAI-compatible LLM.  We read OPENAI_API_KEY from the process
    # environment (with .env fallback) and inject it into the VM's systemd
    # services so gpt-5.4-mini
    # (the default model in @mypcbench/llm) responds on behalf of
    # non-agent personas.
    _inject_llm_env(api_port, container_name)

    # Invoke run_mypcbench.py for this VM's task batch, pointing it at the
    # already-running container by setting the DOCKER_CONTAINER env vars
    env = os.environ.copy()
    env["MYPCBENCH_HOST_API_PORT"] = str(api_port)
    env["MYPCBENCH_HOST_VNC_PORT"] = str(vnc_port)
    env["MYPCBENCH_HOST_SSH_PORT"] = str(ssh_port)
    env["MYPCBENCH_REUSE_CONTAINER"] = "1"
    # Round-robin VMs across multiple vLLM pods. `--vllm-base-urls`
    # accepts a comma-separated list; VM i gets URL[i % len(urls)].
    # Lets a single parallel run drive >max-num-seqs concurrent
    # decoding by spreading across pods. Falls back to inherited
    # OPENAI_BASE_URL when only one URL is provided.
    vllm_urls = args_dict.get("vllm_base_urls") or []
    if vllm_urls:
        url_for_vm = vllm_urls[vm_idx % len(vllm_urls)]
        env["OPENAI_BASE_URL"] = url_for_vm
        logger.info("VM %d pinned to vLLM %s", vm_idx, url_for_vm)
    # QEMU backend: tell the child the base qcow2 path so its inter-task
    # _reset_qemu can rebuild the CoW overlay. Without this the first
    # task attaches fine (reuse=1 just discovers ports) but the second
    # task crashes with TypeError(NoneType) inside qemu-img create.
    if backend == "qemu":
        qcow2_path = args_dict.get("qcow2_path") or os.environ.get("MYPCBENCH_QCOW2")
        if qcow2_path:
            env["MYPCBENCH_QCOW2"] = qcow2_path
            env["MYPCBENCH_SKIP_QCOW2_REFRESH"] = "1"
    # Forward the per-app host ports to the run_mypcbench.py child so its
    # in-VM port discovery resolves the right host->guest mappings.
    for i in range(18):
        env[f"MYPCBENCH_HOST_APP_PORT_{3001 + i}"] = str(app_port_start + i)

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "run_mypcbench.py"),
        "--backend", backend,
        "--docker_image", args_dict["docker_image"],
        "--container_name", container_name,
        "--persona", args_dict["persona"],
        "--world", args_dict["world"],
        "--agent_type", args_dict["agent_type"],
        "--model", args_dict["model"],
        "--tasks_dir", str(tasks_dir),
        "--max_steps", str(args_dict["max_steps"]),
        "--result_dir", str(result_dir),
    ]
    if args_dict.get("screen_width") is not None:
        cmd.extend(["--screen_width", str(args_dict["screen_width"])])
    if args_dict.get("screen_height") is not None:
        cmd.extend(["--screen_height", str(args_dict["screen_height"])])
    if args_dict.get("sleep_after") is not None:
        cmd.extend(["--sleep_after", str(args_dict["sleep_after"])])
    if args_dict.get("soft_reset"):
        cmd.append("--soft-reset")

    # Run the per-VM agent in its own session/process group. On the
    # in-worker timeout/KeyboardInterrupt paths below we os.killpg this
    # group so a stuck run_mypcbench.py is reaped (without
    # start_new_session a killed parallel runner would leave orphan
    # run_mypcbench processes alive for the rest of the per-VM timeout —
    # observed ~13 min). Whole-run interruption is additionally covered by
    # the SIGINT/SIGTERM/atexit reaper in main() (it SIGKILLs each VM's
    # own /tmp/mypcbench-<name>.pid QEMU — scoped, never a blanket kill).
    proc_obj = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        proc_stdout, proc_stderr = proc_obj.communicate(timeout=args_dict.get("timeout", 3600))
        class _ProcResult:
            pass
        proc = _ProcResult()
        proc.returncode = proc_obj.returncode
        proc.stdout = proc_stdout
        proc.stderr = proc_stderr
    except subprocess.TimeoutExpired:
        # Kill the entire process group — Popen.kill() only kills the
        # leader, leaving any KVM/qemu helpers it spawned alive
        try:
            os.killpg(os.getpgid(proc_obj.pid), 15)
            time.sleep(2)
            os.killpg(os.getpgid(proc_obj.pid), 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc_obj.wait()
        class _ProcResult:
            pass
        proc = _ProcResult()
        proc.returncode = -1
        proc.stdout = ""
        proc.stderr = "timeout exceeded — process group killed"
    except KeyboardInterrupt:
        try:
            os.killpg(os.getpgid(proc_obj.pid), 15)
        except (ProcessLookupError, PermissionError):
            pass
        raise

    # Clean up
    if backend == "qemu":
        # Kill QEMU process and clean overlay
        pidfile = f"/tmp/mypcbench-{container_name}.pid"
        if os.path.exists(pidfile):
            try:
                pid = int(open(pidfile).read().strip())
                os.kill(pid, 15)  # SIGTERM
                time.sleep(2)
                try:
                    os.kill(pid, 9)  # SIGKILL if still alive
                except ProcessLookupError:
                    pass
            except Exception:
                pass
        for f in [pidfile,
                  f"/tmp/mypcbench-{container_name}-overlay.qcow2",
                  f"/tmp/mypcbench-{container_name}-vars.qcow2"]:
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
    else:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # Collect scores from result_dir/<task_id>/result.txt
    scores = []
    for task in tasks:
        tid = task.get("id", "?")
        result_file = result_dir / tid / "result.txt"
        if result_file.exists():
            try:
                scores.append({"id": tid, "score": float(result_file.read_text().strip())})
            except ValueError:
                scores.append({"id": tid, "score": 0.0, "error": "invalid score"})
        else:
            scores.append({"id": tid, "score": 0.0, "error": "no result"})

    return {
        "vm_idx": vm_idx,
        "scores": scores,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel MyPCBench task runner")
    parser.add_argument("--tasks-file", type=str, required=True,
                        help="Path to JSON file with a list of task objects")
    parser.add_argument("--num-vms", type=int, default=2,
                        help="Number of parallel VM replicas to spin up")
    parser.add_argument("--backend", choices=["docker", "qemu"], default="qemu",
                        help="VM backend: 'qemu' (default, direct QEMU) or 'docker' (QEMU-in-Docker fallback). "
                             "QEMU-direct is recommended for parallel runs — deterministic ports per replica, "
                             "transparent overlay rebuild on reset.")
    parser.add_argument("--qcow2-path", type=str, default=None,
                        help="Path to base qcow2 image (QEMU backend only). "
                             "Default QEMU runs refresh the managed "
                             "./mypcbench-vm cache from latest before "
                             "booting. Non-default explicit paths are pinned.")
    parser.add_argument("--docker-image", type=str, default="ljang/mypcbench-qemu:latest",
                        help="Docker image (QEMU-in-Docker wrapper). Default "
                             "tracks the current daily/OSS-polish build. Each "
                             "`docker restart` rebuilds the CoW overlay for a "
                             "true hard reset between tasks; pass --soft-reset "
                             "for in-place iteration instead.")
    parser.add_argument("--persona", type=str, default="michael_scott")
    parser.add_argument("--world", type=str, default="scranton-office")
    parser.add_argument("--agent-type", type=str, default="claude_cuabash")
    parser.add_argument("--model", type=str, default="claude-opus-4-6")
    # 100 matches run_mypcbench.py's default and the paper's step budget;
    # a lower default would silently truncate trajectories and depress scores.
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--result-dir", type=str, default="./results/parallel")
    # Default is unique per invocation (PID-suffixed) so two concurrent
    # default runs on a shared host don't collide on container names,
    # /tmp overlay/pidfile scratch, or force-remove each other's VMs.
    parser.add_argument("--container-base", type=str,
                        default=f"mypcbench-parallel-{os.getpid()}")
    parser.add_argument("--port-base", type=int, default=25000,
                        help="Starting host port for VM0 (each VM reserves 30 ports). "
                             "If the requested window is occupied, the runner slides "
                             "the entire batch forward by stride*num_vms and retries.")
    parser.add_argument("--soft-reset", action="store_true",
                        help="Use in-place soft reset instead of container restart")
    parser.add_argument("--timeout-per-vm", type=int, default=3600,
                        help="Wall-clock timeout per VM batch (seconds)")
    parser.add_argument("--screen-width", type=int, default=None,
                        help="Override VM screen width (forwarded to run_mypcbench.py). "
                             "Default lets run_mypcbench.py pick (1280).")
    parser.add_argument("--screen-height", type=int, default=None,
                        help="Override VM screen height (forwarded to run_mypcbench.py). "
                             "Default lets run_mypcbench.py pick (800).")
    parser.add_argument("--sleep-after", type=float, default=None,
                        help="Override sleep-after-action seconds (forwarded to run_mypcbench.py). "
                             "Default (None) lets run_mypcbench.py pick its current default (1.0).")
    parser.add_argument("--vllm-base-urls", type=str, default=None,
                        help="Comma-separated list of vLLM endpoint URLs to round-robin across. "
                             "VM i gets URL[i %% len(urls)]. Lets multi-pod setups exceed a single "
                             "pod's --max-num-seqs concurrency cap. "
                             "Example: http://127.0.0.1:8001/v1,http://127.0.0.1:8002/v1")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
    )

    # Load tasks
    tasks_path = Path(args.tasks_file)
    if not tasks_path.exists():
        logger.error("Tasks file not found: %s", tasks_path)
        return 1
    all_tasks = json.loads(tasks_path.read_text())
    # Accept either a JSON list (the canonical form) or a single task dict.
    # Smoke tests routinely pass a one-task file as a top-level object;
    # wrapping it here saves users from a confusing error on N=1.
    if isinstance(all_tasks, dict):
        all_tasks = [all_tasks]
    if not isinstance(all_tasks, list):
        logger.error("Task file must contain a JSON list (or single dict) of tasks")
        return 1
    # `id` becomes a path segment per VM (result_dir/<id>); reject any task
    # whose id could escape the result dir before fanning out.
    for _t in all_tasks:
        _tid = _t.get("id") if isinstance(_t, dict) else None
        if (not isinstance(_tid, str) or not _tid or "/" in _tid
                or "\\" in _tid or ".." in _tid or _tid.startswith(("~", "."))):
            logger.error("Unsafe or missing task id: %r", _tid)
            return 1
    # Refuse the silent all-zero eval: every task is rubric-graded, so a
    # file with no rubrics anywhere is the ungraded all_tasks.json by mistake.
    if all_tasks and not any(
        (t.get("grading") or {}).get("rubrics") for t in all_tasks
        if isinstance(t, dict)
    ):
        logger.error(
            "No task in %s carries grading.rubrics — this grades to 0.0 "
            "for every task. Use all_tasks_with_grading.json, not "
            "all_tasks.json.", tasks_path,
        )
        return 1
    logger.info("Loaded %d tasks from %s", len(all_tasks), tasks_path)

    if args.backend == "qemu":
        args.qcow2_path = _ensure_current_qcow2(args.qcow2_path or os.environ.get("MYPCBENCH_QCOW2"))

    # Split tasks across VMs: ceil(N / num_vms) per VM
    num_vms = max(1, args.num_vms)
    per_vm = math.ceil(len(all_tasks) / num_vms)
    batches = [all_tasks[i * per_vm : (i + 1) * per_vm] for i in range(num_vms)]
    batches = [b for b in batches if b]  # drop empty
    actual_vms = len(batches)
    logger.info("Splitting into %d VMs, %d tasks each", actual_vms, per_vm)

    args_dict = {
        "backend": args.backend,
        "qcow2_path": args.qcow2_path,
        "docker_image": args.docker_image,
        "persona": args.persona,
        "world": args.world,
        "agent_type": args.agent_type,
        "model": args.model,
        "max_steps": args.max_steps,
        "result_dir": args.result_dir,
        "container_base": args.container_base,
        "soft_reset": args.soft_reset,
        "timeout": args.timeout_per_vm,
        "screen_width": args.screen_width,
        "screen_height": args.screen_height,
        "sleep_after": args.sleep_after,
        "vllm_base_urls": [u.strip() for u in (args.vllm_base_urls or "").split(",") if u.strip()],
    }

    result_root = Path(args.result_dir)
    result_root.mkdir(parents=True, exist_ok=True)

    start = time.time()
    all_results: list[dict] = []

    # Orphan-VM reaper: on Ctrl-C / Slurm SIGTERM, kill the QEMU each VM
    # worker spawned (scoped to OUR own per-VM pidfiles only — never a
    # blanket qemu kill) so preemption doesn't leak 8 GB VMs on a shared
    # node. atexit covers the normal-exit path; the signal handler covers
    # SIGINT/SIGTERM. Idempotent.
    import signal as _signal, atexit as _atexit
    _own_names = [f"{args.container_base}-vm{i}" for i in range(actual_vms)]
    _reaped = {"done": False}

    def _reap_vms(*_a):
        if _reaped["done"]:
            return
        _reaped["done"] = True
        for nm in _own_names:
            pf = f"/tmp/mypcbench-{nm}.pid"
            try:
                if os.path.exists(pf):
                    pid = int(open(pf).read().strip())
                    os.kill(pid, _signal.SIGKILL)
            except (ValueError, OSError):
                pass
            for f in (pf,
                      f"/tmp/mypcbench-{nm}-overlay.qcow2",
                      f"/tmp/mypcbench-{nm}-vars.qcow2"):
                try:
                    os.path.exists(f) and os.unlink(f)
                except OSError:
                    pass

    _atexit.register(_reap_vms)
    for _sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            _signal.signal(_sig, lambda *_a: (_reap_vms(), sys.exit(130)))
        except (ValueError, OSError):
            pass

    # Auto-find a free port window so two concurrent parallel-runner
    # invocations on the same host don't silently collide.
    port_windows = _allocate_port_windows(args.port_base, actual_vms)
    logger.info("VM port windows: %s",
                ", ".join(f"vm{i}={p}-{p+29}" for i, p in enumerate(port_windows)))

    with ProcessPoolExecutor(max_workers=actual_vms) as executor:
        futures = {
            executor.submit(
                run_vm_batch,
                idx,
                batches[idx],
                args_dict,
                port_windows[idx],
            ): idx
            for idx in range(actual_vms)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                result = fut.result()
                all_results.append(result)
                logger.info("VM%d complete (rc=%s, scored=%d)",
                            idx, result.get("returncode"), len(result.get("scores", [])))
            except Exception as e:
                logger.exception("VM%d failed with exception", idx)
                all_results.append({"vm_idx": idx, "error": str(e), "scores": []})

    elapsed = time.time() - start

    # Aggregate scores
    flat_scores = [s for r in all_results for s in r.get("scores", [])]
    total = len(flat_scores)
    # NOT a grade. result.txt is the binary completion marker (1.0 = the
    # episode ran to completion, 0.0 = errored). Rubric scoring is the
    # separate post-run step: agent-harness/judge_results.py.
    completed = sum(1 for s in flat_scores if s.get("score", 0.0) >= 1.0)

    summary = {
        "total_tasks": len(all_tasks),
        "num_vms": actual_vms,
        "elapsed_seconds": elapsed,
        "ran": total,
        "completed": completed,
        "completion_rate": completed / total if total else 0.0,
        "note": "completion only — score with agent-harness/judge_results.py --result_dir <dir>",
        "per_vm": all_results,
    }

    summary_path = result_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    logger.info("=" * 60)
    logger.info("DONE in %.1fs across %d VMs", elapsed, actual_vms)
    logger.info("%d / %d tasks ran to completion (not graded — run "
                "judge_results.py)", completed, total)
    logger.info("Summary: %s", summary_path)
    return 0 if completed == total else 1


if __name__ == "__main__":
    sys.exit(main())
