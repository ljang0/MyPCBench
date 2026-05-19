#!/usr/bin/env python3
"""MyPCBench single-task runner.

This is MyPCBench's primary agent-loop entry point. The
`MyPCBenchEnv` contract mirrors OSWorld's `DesktopEnv` at the API
level (reset / step / _get_obs) so agents written for OSWorld can
plug in directly — but the environment and tasks are MyPCBench-native.

Flow:
  1. Create env (Docker / QEMU container with GNOME desktop)
  2. Create agent (Claude CUA, GPT CUA, Qwen VL, or generic pyautogui)
  3. For each task: reset → react loop (predict → step → screenshot),
     then write result.txt (completion) + rubric_bundle.json. Rubric
     scoring is a separate step: agent-harness/judge_results.py.

Usage:
    # Claude Opus 4.6 (default agent: claude_cuabash)
    python agent-harness/run_mypcbench.py --model claude-opus-4-6 \
        --agent_type claude_cuabash --tasks_dir tasks/final

    # GPT-5.5
    python agent-harness/run_mypcbench.py --model gpt-5.5 \
        --agent_type openai_cuabash --tasks_dir tasks/final

    # Qwen 3.5 35B (via vLLM at $OPENAI_BASE_URL)
    OPENAI_API_KEY=dummy OPENAI_BASE_URL=http://localhost:8000/v1 \
    python agent-harness/run_mypcbench.py --model Qwen/Qwen3.5-35B-A3B \
        --agent_type qwen_cuabash --tasks_dir tasks/final
"""

import argparse
import datetime
import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path so the agents package resolves
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from env import MyPCBenchEnv
# The runner builds the self-contained per-task rubric bundle only;
# rubric grading is a separate post-run step (judge_results.py).
from utils.rubric_judge import build_rubric_bundle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("mypcbench.run")


# ── --interface flag routing ───────────────────────────────────────────────


def _infer_provider(model: str) -> str:
    """Infer the model provider family from the model identifier.

    Returns one of {"claude", "openai", "qwen"}. Used by `--interface` to
    auto-pick the right agent_type.
    """
    m = (model or "").lower()
    if m.startswith("claude-") or "anthropic" in m:
        return "claude"
    if m.startswith(("gpt-5", "gpt-4", "o1", "o3")) or "openai" in m:
        return "openai"
    if "qwen" in m:
        return "qwen"
    # Fallback: assume the OpenAI family — the runner's default.
    return "openai"


def _resolve_agent_type_from_interface(interface: str, model: str) -> str:
    """Translate `--interface gui` + model → the paper agent_type.

    (The MCP `hybrid` interface was removed — this release ships paper
    agents only. Use `--agent_type` directly for the appendix ablations.)
    """
    provider = _infer_provider(model)
    if interface == "gui":
        return {
            "claude": "claude_cuabash",
            "openai": "openai_cuabash",
            "qwen": "qwen_cuabash",
        }[provider]
    raise ValueError(
        f"interface {interface!r} not supported (paper agents only); "
        "pass --agent_type explicitly."
    )


def get_agent(agent_type: str, model: str, screen_size, client_password: str, **kwargs):
    """Factory: create a paper agent.

    Keep-set (the paper + appendix; everything else was removed for the
    OSS release — no MCP, no multi-agent, no legacy/ported variants):

    Naming: <provider>_<input>. cuabash = computer + bash; cua = computer only.

      dummy            smoke / CI (no API cost)
      claude_cuabash   Claude — computer + bash + editor (paper main)
      openai_cuabash   GPT — computer + OpenAI built-in shell (paper main)
      qwen_cuabash     Qwen — OSWorld-parity computer + bash (paper main)
      qwen_cua         Qwen — OSWorld-parity computer only (appendix ablation)
    """
    no_computer = kwargs.get("no_computer", False)

    if agent_type == "dummy":
        from agents.dummy import DummyAgent
        return DummyAgent()

    if agent_type == "claude_cuabash":
        from agents.claude_cuabash import ClaudeCUAAgent
        return ClaudeCUAAgent(
            model=model,
            screen_size=screen_size,
            client_password=client_password,
            enable_computer=not no_computer,
            enable_bash=True,
            enable_editor=True,
            env=kwargs.get("env"),
        )

    if agent_type == "openai_cuabash":
        from agents.openai_cuabash import OpenAICUAToolsAgent
        return OpenAICUAToolsAgent(
            model=model,
            screen_size=screen_size,
            client_password=client_password,
            enable_computer=not no_computer,
            env=kwargs.get("env"),
        )

    if agent_type in ("qwen_cua", "qwen_cuabash"):
        from agents.qwen_cua import QwenOSWorldAgent
        return QwenOSWorldAgent(
            model=model,
            screen_size=screen_size,
            client_password=client_password,
            enable_bash=(agent_type == "qwen_cuabash"),
            env=kwargs.get("env"),
        )

    raise ValueError(
        f"Unknown agent_type: {agent_type!r}\n"
        "Available (paper agents only, <provider>_<input>):\n"
        "  dummy           -- smoke / CI (no API cost)\n"
        "  claude_cuabash  -- Claude: computer + bash + editor (paper main)\n"
        "  openai_cuabash  -- GPT: computer + OpenAI built-in shell (paper main)\n"
        "  qwen_cuabash    -- Qwen: OSWorld-parity computer + bash (paper main)\n"
        "  qwen_cua        -- Qwen: OSWorld-parity computer only (appendix)\n"
    )


def load_tasks(tasks_dir: str):
    """Load tasks from a file or a directory tree.

    `tasks_dir` may be a single task JSON (a list or one task dict) or a
    directory. For a directory, the canonical graded source is preferred:
    if `all_tasks_with_grading.json` is present at the top level it is the
    only file read (the 184-task paper set with rubrics inlined);
    otherwise the tree is walked for per-bucket `*.rubrics.json`.

    Non-task files (`schema.json`, `variables.json`, the ungraded
    `all_tasks.json`, READMEs) are skipped, and tasks are de-duplicated by
    `id` preferring the copy that carries rubrics — so pointing at
    `tasks/final` does not load every task three times.
    """
    _SKIP = {"schema.json", "variables.json", "all_tasks.json"}

    def _ok(t):
        # `id` becomes a path segment (result_dir/<id>); reject anything a
        # malicious community task JSON could use to escape the result dir.
        tid = t.get("id")
        return (isinstance(t, dict) and isinstance(tid, str) and tid
                and t.get("instruction")
                and "/" not in tid and "\\" not in tid and ".." not in tid
                and not tid.startswith(("~", ".")))

    def _norm(data):
        if isinstance(data, list):
            return [t for t in data if _ok(t)]
        if isinstance(data, dict) and _ok(data):
            return [data]
        return []

    paths = []
    if os.path.isfile(tasks_dir):
        # If pointed at the ungraded flat file, transparently prefer its
        # graded twin so a documented `--tasks_dir .../all_tasks.json`
        # invocation doesn't silently run an all-zero (no-rubric) eval.
        if os.path.basename(tasks_dir) in _SKIP:
            graded = os.path.join(os.path.dirname(tasks_dir),
                                  "all_tasks_with_grading.json")
            paths = [graded if os.path.isfile(graded) else tasks_dir]
        else:
            paths = [tasks_dir]
    else:
        canonical = os.path.join(tasks_dir, "all_tasks_with_grading.json")
        if os.path.isfile(canonical):
            paths = [canonical]
        else:
            for root, _dirs, files in os.walk(tasks_dir):
                for f in sorted(files):
                    if f.endswith(".json") and f not in _SKIP:
                        paths.append(os.path.join(root, f))

    by_id = {}
    for path in paths:
        with open(path) as fh:
            for t in _norm(json.load(fh)):
                tid = t["id"]
                prev = by_id.get(tid)
                has_rubrics = bool((t.get("grading") or {}).get("rubrics"))
                if prev is None or (has_rubrics and
                                    not (prev.get("grading") or {}).get("rubrics")):
                    by_id[tid] = t
    tasks = list(by_id.values())
    # Fail loudly rather than burn a full run that grades to all-zero:
    # every MyPCBench task is rubric-graded, so a set with no rubrics is
    # always a misconfiguration (almost always the ungraded all_tasks.json).
    if tasks and not any((t.get("grading") or {}).get("rubrics") for t in tasks):
        raise ValueError(
            f"Loaded {len(tasks)} tasks from {tasks_dir!r} but NONE carry "
            "grading.rubrics — this would grade to 0.0 for every task. "
            "Point --tasks_dir at tasks/final (a directory) or "
            "tasks/final/all_tasks_with_grading.json, not all_tasks.json."
        )
    return tasks


def _fsync_write(path: str, data, mode: str = "w") -> None:
    """Write+fsync an artifact so the inter-task VM reset can't lose it
    to page-cache buffering. A plain `with open(...)` flushes libc but
    leaves dirty blocks in the kernel page cache; the reset that fires
    right after the task can return before writeback commits them.
    fsync forces the data to the filesystem before we move on.
    """
    with open(path, mode) as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # Some filesystems (tmpfs under certain mounts) may not
            # support fsync on all fds. Best-effort: Python's close()
            # already flushed libc buffers, so we'd only lose the
            # extra kernel-level durability — not the write itself.
            pass


def run_single_example(agent, env, task, max_steps, result_dir, sleep_after=1.0, timeout=3600):
    """OSWorld-style react loop: predict → execute → screenshot → repeat."""
    instruction = task.get("instruction", "")
    if not instruction:
        task_map = task.get("task", {})
        instruction = task_map.get("en", "") if isinstance(task_map, dict) else str(task_map)

    # Resume-safety: clean any artifacts left behind by a prior crashed/
    # preempted attempt before starting fresh. We only reach this code
    # path when result.txt is missing — i.e. the task was never finished —
    # so it's always safe to wipe. Without this, traj.jsonl writes use
    # `append` mode and a retry's steps get appended to the partial
    # file, and step_<N>_<ts>.png files from multiple attempts coexist
    # with different timestamps, confusing the judge that walks the dir.
    traj_path = os.path.join(result_dir, "traj.jsonl")
    if os.path.exists(traj_path):
        open(traj_path, "w").close()
    for stale in glob.glob(os.path.join(result_dir, "step_*.png")):
        try:
            os.unlink(stale)
        except OSError:
            pass
    # Touch the file regardless so existence checks pass on 0-step runs
    # (dummy agent returns DONE on step 1 → exactly one traj line but it
    # gets written AFTER env.step; we want the path to exist even if
    # env.step raises on a cold first task).
    open(traj_path, "a").close()

    # 1. Reset environment
    obs = env.reset(task_config=task)
    try:
        agent.reset(logging.getLogger(f"mypcbench.task.{task.get('id', 'unknown')}"))
    except TypeError:
        agent.reset()

    # 2. Brief settle time after container ready (API already confirmed healthy)
    time.sleep(5)
    obs = env._get_obs()

    done = False
    step_idx = 0
    consecutive_empty = 0
    task_start_time = time.time()
    consecutive_api_errors = 0

    # 3. React loop
    while not done and step_idx < max_steps:
        # Agent predicts action(s) from current observation.
        # Wrapped in try/except so a single predict() crash doesn't
        # lose the entire trajectory — we log the error, append it to
        # traj.jsonl, and break out of the loop so eval still runs.
        try:
            response, actions = agent.predict(instruction, obs)
        except Exception as e:
            logger.error("Agent predict crashed at step %d: %s", step_idx, e, exc_info=True)
            error_line = json.dumps({
                "step_num": step_idx + 1,
                "action_timestamp": datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f"),
                "action": "PREDICT_CRASH",
                "response": str(e),
                "reward": 0,
                "done": True,
                "info": {"error": str(e)},
                "screenshot_file": "",
            }, ensure_ascii=False) + "\n"
            with open(traj_path, "a") as f:
                f.write(error_line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            break

        resp_str = response if isinstance(response, str) else str(response)

        if not actions:
            consecutive_empty += 1
            # Detect persistent API errors (e.g. context overflow after a
            # huge tool response) and short-circuit. Without this, a single
            # 400 from the endpoint makes the loop re-issue the same
            # failing request for the remaining max_steps iterations.
            if resp_str.startswith("Error code:") or "BadRequestError" in resp_str:
                consecutive_api_errors += 1
                if consecutive_api_errors >= 3:
                    logger.warning(
                        "Step %d: %d consecutive API errors — aborting task. Last: %s",
                        step_idx + 1, consecutive_api_errors, resp_str[:200],
                    )
                    break
            else:
                consecutive_api_errors = 0
            # Check if bash/editor tools were used (no GUI actions but model continues)
            has_pending = (
                (hasattr(agent, 'messages') and len(getattr(agent, 'messages', [])) > 0)
                or (hasattr(agent, 'pending_items') and len(getattr(agent, 'pending_items', [])) > 0)
            )
            if has_pending and consecutive_empty < max_steps:
                # Write a trajectory row for this tool-only step so the rubric
                # judge can see intermediate tool work. Without it, only the
                # final DONE step lands in traj.jsonl and the judge can't
                # credit the steps that did the real work.
                action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f")
                screenshot_file = f"step_{step_idx + 1}_{action_timestamp}.png"
                screenshot_saved = False
                if obs.get("screenshot"):
                    _fsync_write(
                        os.path.join(result_dir, screenshot_file),
                        obs["screenshot"],
                        mode="wb",
                    )
                    screenshot_saved = True
                tool_line = json.dumps({
                    "step_num": step_idx + 1,
                    "action_timestamp": action_timestamp,
                    "action": "TOOL_CALL",
                    "response": response if isinstance(response, str) else str(response),
                    "reward": 0.0,
                    "done": False,
                    "info": {"kind": "tool_call_round"},
                    "screenshot_file": screenshot_file if screenshot_saved else "",
                }, ensure_ascii=False) + "\n"
                with open(traj_path, "a") as f:
                    f.write(tool_line)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                logger.info("Step %d: (tool round captured, continuing)", step_idx + 1)
                obs = env._get_obs()
                step_idx += 1
                continue
            logger.warning("No actions returned at step %d", step_idx + 1)
            break
        else:
            consecutive_empty = 0

        for action in actions:
            action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f")
            logger.info("Step %d: %s", step_idx + 1, str(action)[:200])

            # Execute action
            obs, reward, done, info = env.step(action, sleep_after)

            # Save screenshot (fsync to survive the inter-task docker restart;
            # see _fsync_write docstring for the race details).
            screenshot_file = f"step_{step_idx + 1}_{action_timestamp}.png"
            screenshot_saved = False
            if obs.get("screenshot"):
                _fsync_write(
                    os.path.join(result_dir, screenshot_file),
                    obs["screenshot"],
                    mode="wb",
                )
                screenshot_saved = True
            else:
                # Cold-start fallback: if env.step's screenshot came back
                # None (the /screenshot HTTP endpoint sometimes lags the
                # /health endpoint on a freshly-booted container), retry
                # once synchronously before giving up — this prevents
                # task 0 from ending with 0 screenshots.
                retry_shot = env._get_screenshot()
                if retry_shot:
                    _fsync_write(
                        os.path.join(result_dir, screenshot_file),
                        retry_shot,
                        mode="wb",
                    )
                    screenshot_saved = True

            # Save trajectory line (append + fsync). Build the line first
            # so we never leave a half-written or empty traj.jsonl behind
            # if json.dumps would raise on non-serializable payload.
            line = json.dumps({
                "step_num": step_idx + 1,
                "action_timestamp": action_timestamp,
                "action": action if isinstance(action, str) else str(action),
                "response": response if isinstance(response, str) else str(response),
                "reward": reward,
                "done": done,
                "info": info,
                "screenshot_file": screenshot_file if screenshot_saved else "",
            }, ensure_ascii=False) + "\n"
            with open(traj_path, "a") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

            if done:
                logger.info("Episode done at step %d", step_idx + 1)
                break

        step_idx += 1

        # Global wall-clock timeout: abort if task has been running too long
        if time.time() - task_start_time > timeout:
            task_id = task.get("id", "unknown")
            logger.warning("Task %s hit global timeout (%ds)", task_id, timeout)
            break

    # Ensure final screenshot is captured. If the step count exceeds
    # the number of saved PNGs (e.g. screenshot endpoint was down on
    # the last step), capture one last screenshot so graders/humans
    # always have a final-state image to inspect.
    expected_pngs = step_idx  # steps that ran
    actual_pngs = len(glob.glob(os.path.join(result_dir, "step_*.png")))
    if actual_pngs < expected_pngs:
        logger.info("Screenshot gap: %d steps but only %d PNGs — capturing final screenshot", expected_pngs, actual_pngs)
        try:
            final_obs = env._get_obs()
            if final_obs.get("screenshot"):
                final_ts = datetime.datetime.now().strftime("%Y%m%d@%H%M%S%f")
                _fsync_write(
                    os.path.join(result_dir, f"step_final_{final_ts}.png"),
                    final_obs["screenshot"],
                    mode="wb",
                )
        except Exception:
            logger.debug("Final screenshot capture failed (non-fatal)", exc_info=True)

    # 4. Evaluate (brief settle before grading)
    time.sleep(5)

    # Persist full conversation messages (system/user/assistant/tool) so we can
    # inspect what the model saw and produced at each step — strictly more
    # information than traj.jsonl's per-step action/response summary.
    try:
        messages_path = os.path.join(result_dir, "messages.json")
        msgs = getattr(agent, "messages", None)
        if msgs:
            # Strip image bytes (base64 screenshots) to keep file small but
            # keep a placeholder so the structure is still readable.
            def _strip_images(x):
                if isinstance(x, list):
                    return [_strip_images(v) for v in x]
                if isinstance(x, dict):
                    if x.get("type") == "image_url" and isinstance(x.get("image_url"), dict):
                        return {"type": "image_url", "image_url": {"url": "<stripped:base64 image>"}}
                    if x.get("type") == "image" and "source" in x:
                        return {"type": "image", "source": "<stripped:base64 image>"}
                    return {k: _strip_images(v) for k, v in x.items()}
                return x
            stripped = _strip_images(msgs)
            _fsync_write(messages_path, json.dumps(stripped, ensure_ascii=False, indent=2), mode="w")
            logger.info("Wrote messages.json (%d messages)", len(msgs) if isinstance(msgs, list) else 0)
    except Exception as _e:
        logger.warning("Failed to write messages.json: %s", _e)

    # Write the agent's final response to a file in the VM so fuzzy_match
    # and response_contains checks can read it during grading. We source
    # the response from (in priority order):
    #   1. agent.messages[-1].content (assistant's last utterance)
    #   2. Last non-empty 'response' field in traj.jsonl
    #   3. Concatenation of all bash/tool outputs captured in traj.jsonl
    try:
        final_response = ""
        # (1) Agent's message buffer — works for runs that answer in
        # text without emitting a final GUI action.
        if hasattr(agent, "messages") and agent.messages:
            for _msg in reversed(agent.messages):
                role = _msg.get("role") if isinstance(_msg, dict) else None
                content = _msg.get("content") if isinstance(_msg, dict) else None
                if role == "assistant" and content:
                    if isinstance(content, str):
                        final_response = content
                    elif isinstance(content, list):
                        # Anthropic/OpenAI sometimes use content blocks
                        final_response = " ".join(
                            str(b.get("text") or b.get("content") or "")
                            for b in content if isinstance(b, dict)
                        )
                    if final_response.strip():
                        break
        # (2) Fall back to traj.jsonl
        if not final_response.strip() and os.path.exists(traj_path):
            with open(traj_path) as _tf:
                for _line in _tf:
                    try:
                        _entry = json.loads(_line)
                        _r = _entry.get("response", "")
                        if _r and _r != "None":
                            final_response = str(_r)
                    except (json.JSONDecodeError, ValueError):
                        pass
        import base64 as _b64
        _encoded = _b64.b64encode(final_response.encode("utf-8")).decode("ascii")
        env._execute_shell(f"echo {_encoded} | base64 -d > /tmp/mypcbench_agent_response.txt")
        logger.info("Wrote agent response file (%d chars)", len(final_response))
    except Exception as _e:
        logger.warning("Failed to write agent response file: %s", _e)
    grading = task.get("grading", {}) if isinstance(task.get("grading"), dict) else {}
    checks = grading.get("checks") if isinstance(grading.get("checks"), list) else []
    rubrics = grading.get("rubrics") if isinstance(grading.get("rubrics"), list) else []
    rubric_only = not checks and bool(rubrics)

    # result.txt is a completion marker, not a grade: 1.0 = episode ran to
    # completion (here = react loop returned without raising), 0.0 = errored
    # (recorded by the caller). Rubric scoring is a separate post-run step,
    # agent-harness/judge_results.py. See docs/NO_DOCKER.md "Grading".
    result = 1.0

    if rubric_only:
        # Build the self-contained rubric bundle now, while the env is live,
        # so the trajectory can be graded later from the result directory
        # alone (no VM/env needed at judge time). This is packaging only —
        # no judge model is invoked here.
        grading_manifest = {
            "task_id": task.get("id"),
            "category": task.get("category"),
            "grading_type": grading.get("type"),
            "has_programmatic_checks": False,
            "has_rubrics": True,
            "checks": [],
            "rubrics": rubrics,
            "notes": "Rubric bundle for offline grading (agent-harness/judge_results.py)",
        }
        bundle_err = None
        try:
            build_rubric_bundle(
                save_dir=result_dir,
                task_dict=task,
                grading_manifest=grading_manifest,
                task_language="en",
                env_language="en",
                snapshot_name=None,
                persona=env.persona,
                world=env.world,
                vm_id=env.persona,
                actor_id=None,
            )
        except Exception as e:
            bundle_err = e
        if bundle_err is None and not os.path.exists(
            os.path.join(result_dir, "rubric_bundle.json")
        ):
            bundle_err = RuntimeError("rubric_bundle.json not written")
        if bundle_err is not None:
            # A missing bundle would silently grade to 0.0 AND the
            # skip-already-completed guard (keyed on result.txt) would
            # never retry it. Mark the task errored so it is honestly
            # 0.0 in the denominator and a re-run regenerates it.
            logger.error("Rubric bundle build failed: %s", bundle_err,
                          exc_info=True)
            result = 0.0
            try:
                _fsync_write(
                    os.path.join(result_dir, "incomplete.txt"),
                    f"rubric bundle build failed: {bundle_err}\n",
                    mode="w",
                )
            except Exception:
                pass

    logger.info("Result (completed=%.1f) — rubric grading is a separate step", result)

    # Dump the agent's accumulated cache/token usage so prompt-cache
    # behaviour is visible from the run log. Best-effort; absent on
    # agents that don't track it.
    usage = getattr(agent, "total_usage", None)
    if usage:
        try:
            _fsync_write(
                os.path.join(result_dir, "usage.json"),
                json.dumps(dict(usage), indent=2),
                mode="w",
            )
        except Exception as _u_err:
            logger.debug("usage.json write failed: %s", _u_err)

    # result.txt is the "task completed" marker the skip-already-completed
    # logic keys on. fsync it so a crash / hard-reset between tasks can't
    # leave us with a present-but-empty file that re-runs forever.
    _fsync_write(os.path.join(result_dir, "result.txt"), f"{result}\n", mode="w")

    return result


def main():
    parser = argparse.ArgumentParser(description="MyPCBench OSWorld-compatible runner")

    # Agent
    parser.add_argument("--agent_type", type=str, default="claude_cuabash",
                        choices=[
                            "dummy",
                            "claude_cuabash",
                            "openai_cuabash",
                            "qwen_cuabash", "qwen_cua",
                        ],
                        help="Paper agents (<provider>_<input>): "
                             "claude_cuabash (Claude main), openai_cuabash "
                             "(GPT main), qwen_cuabash (Qwen main), qwen_cua "
                             "(Qwen appendix, computer only), dummy (CI).")
    parser.add_argument("--no-computer", action="store_true",
                        help="Disable the computer/GUI tool (bash/shell only)")
    parser.add_argument("--action_space", type=str, default="pyautogui",
                        choices=["pyautogui", "computer_13"],
                        help="Accepted for OSWorld CLI compatibility; no "
                             "effect (paper agents are fixed pyautogui)")
    parser.add_argument("--observation_type", type=str, default="screenshot",
                        choices=["screenshot", "a11y_tree", "screenshot_a11y_tree"],
                        help="Accepted for OSWorld CLI compatibility; no "
                             "effect (paper agents are fixed screenshot)")
    parser.add_argument("--model", type=str, default="claude-opus-4-6",
                        help="Model name/ID (default pairs with the default "
                             "--agent_type claude_cuabash)")

    # Environment
    parser.add_argument("--backend", choices=["docker", "qemu"], default="qemu",
                        help="VM backend: 'qemu' (default, direct QEMU) or 'docker' (QEMU-in-Docker wrapper). "
                             "QEMU-direct is recommended — faster boot, transparent CoW lifecycle, fewer moving "
                             "parts. Pass --backend docker for the qemu-in-docker fallback.")
    parser.add_argument("--qcow2_path", type=str, default=None,
                        help="Path to base qcow2 image (QEMU backend only)")
    parser.add_argument("--docker_image", type=str,
                        default="ljang/mypcbench-qemu:eval-round0",
                        help="QEMU-in-Docker image (used with --backend docker). "
                             "Default is the paper baseline ljang/mypcbench-qemu:"
                             "eval-round0; use :v1.2.16-oss-polish-michael_scott "
                             "for the latest build.")
    parser.add_argument("--container_name", type=str, default="mypcbench-agent")
    parser.add_argument("--persona", type=str, default="michael_scott")
    parser.add_argument("--world", type=str, default="scranton-office")
    parser.add_argument("--screen_width", type=int, default=1280)
    parser.add_argument("--screen_height", type=int, default=800)
    parser.add_argument("--client_password", type=str, default="password")
    parser.add_argument(
        "--interface",
        choices=["gui"],
        default=None,
        help="High-level mode: 'gui' picks the paper main agent for the "
             "provider inferred from --model (claude->claude_cuabash, "
             "openai->openai_cuabash, qwen->qwen_cuabash). Mutually "
             "exclusive with --agent_type; --agent_type wins (with a warning).",
    )

    # Tasks
    parser.add_argument("--tasks_dir", type=str, required=True, help="Directory with task JSON files")
    parser.add_argument("--max_steps", type=int, default=100)
    # Default 1.0s (OSWorld reference-runner value) — enough for the GUI
    # to redraw without burning minutes/task on long trajectories.
    parser.add_argument("--sleep_after", type=float, default=1.0)
    parser.add_argument("--soft-reset", action="store_true",
                        help="Keep container between tasks, just clean files (faster but less isolated)")
    parser.add_argument("--result_dir", type=str, default="./results")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="Global wall-clock timeout per task in seconds (default: 2 hours)")

    args = parser.parse_args()
    screen_size = (args.screen_width, args.screen_height)

    # Load tasks
    tasks = load_tasks(args.tasks_dir)
    if not tasks:
        logger.error("No tasks found in %s", args.tasks_dir)
        sys.exit(1)
    logger.info("Loaded %d tasks from %s", len(tasks), args.tasks_dir)

    # Create environment
    env = MyPCBenchEnv(
        docker_image=args.docker_image,
        container_name=args.container_name,
        persona=args.persona,
        world=args.world,
        screen_size=screen_size,
        client_password=args.client_password,
        backend=args.backend,
        qcow2_path=args.qcow2_path,
    )
    env.soft_reset = getattr(args, 'soft_reset', False)

    # Ensure container cleanup on Ctrl-C or crash
    import atexit
    atexit.register(env.close)

    # Resolve --interface against --agent_type. Power-user --agent_type wins
    # if both are set (warn so the user knows). Otherwise --interface picks
    # the concrete agent_type from the model name.
    effective_agent_type = args.agent_type
    if getattr(args, "interface", None):
        inferred = _resolve_agent_type_from_interface(args.interface, args.model)
        # `args.agent_type` has a default ("claude_cuabash"), so we only count
        # it as an explicit override if the user passed something non-default.
        user_explicit = (
            args.agent_type != parser.get_default("agent_type")
        ) if hasattr(parser, "get_default") else False
        if user_explicit and args.agent_type != inferred:
            print(
                f"[warn] both --interface {args.interface} and "
                f"--agent_type {args.agent_type} provided — using --agent_type.",
                file=sys.stderr,
            )
        else:
            effective_agent_type = inferred
            print(
                f"[interface] --interface {args.interface} + --model {args.model} "
                f"-> agent_type={effective_agent_type}",
                file=sys.stderr,
            )

    # Create agent (pass env for bash tool execution in container)
    agent = get_agent(
        agent_type=effective_agent_type,
        model=args.model,
        screen_size=screen_size,
        client_password=args.client_password,
        action_space=args.action_space,
        observation_type=args.observation_type,
        env=env,
        no_computer=getattr(args, 'no_computer', False),
    )

    # Run tasks (skip already-completed ones for resumability)
    scores = []
    skipped = 0
    for i, task in enumerate(tasks):
        task_id = task.get("id", f"task_{i}")
        task_result_dir = os.path.join(args.result_dir, task_id)
        # Defense in depth: never let a task id escape the result dir.
        if not os.path.realpath(task_result_dir).startswith(
            os.path.realpath(args.result_dir) + os.sep
        ):
            raise ValueError(f"unsafe task id (path escape): {task_id!r}")

        # Skip if result.txt already exists (task already completed)
        result_file = os.path.join(task_result_dir, "result.txt")
        if os.path.exists(result_file):
            try:
                with open(result_file) as f:
                    prev_score = float(f.read().strip())
                scores.append(prev_score)
                skipped += 1
                continue
            except (ValueError, IOError):
                pass  # Corrupted result file, re-run this task

        logger.info("=== Task %d/%d: %s ===", i + 1, len(tasks), task_id)
        os.makedirs(task_result_dir, exist_ok=True)

        try:
            score = run_single_example(
                agent=agent,
                env=env,
                task=task,
                max_steps=args.max_steps,
                result_dir=task_result_dir,
                sleep_after=args.sleep_after,
                timeout=args.timeout,
            )
            scores.append(score)
        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e, exc_info=True)
            scores.append(0.0)
            # The task errored before completing → result.txt = 0.0 (the
            # binary completion contract: 1.0 ran, 0.0 errored). A re-run
            # skips it like any completed task; delete its result.txt to
            # force a retry. incomplete.txt carries the failure detail.
            try:
                _fsync_write(
                    os.path.join(task_result_dir, "result.txt"), "0.0\n", mode="w",
                )
                _fsync_write(
                    os.path.join(task_result_dir, "incomplete.txt"),
                    f"errored: {type(e).__name__}: {str(e)[:200]}\n",
                    mode="w",
                )
            except Exception:
                pass

    if skipped:
        logger.info("Skipped %d already-completed tasks", skipped)

    # Summary
    if scores:
        avg = sum(scores) / len(scores)
        logger.info("=== Results: %d tasks, avg score %.2f ===", len(scores), avg)
        for i, (task, score) in enumerate(zip(tasks, scores)):
            logger.info("  %s: %.2f", task.get("id", f"task_{i}"), score)

    env.close()




if __name__ == "__main__":
    main()
