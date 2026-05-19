"""Helpers for rubric-based task judging.

The harness does not ship a built-in LLM judge. This module standardizes the
payload we hand to an external judge command and parses its score output.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RubricBundle:
    path: Path
    payload: dict[str, Any]


def _resolve_instruction(task_dict: dict[str, Any], task_language: str) -> str | None:
    task_value = task_dict.get("task")
    if isinstance(task_value, dict):
        candidate = task_value.get(task_language)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    instruction = task_dict.get("instruction")
    if isinstance(instruction, dict):
        candidate = instruction.get(task_language)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if isinstance(instruction, str) and instruction.strip():
        return instruction.strip()
    return None


# Match `step_001.png`, `step_42_20260411@123456.png`, etc. — the step number
# is the first run of digits after `step_`.
_STEP_NUM_RE = re.compile(r"step_(\d+)")


def _step_num_from_path(path: Path) -> int | None:
    m = _STEP_NUM_RE.search(path.name)
    return int(m.group(1)) if m else None


def _index_steps_context_layout(save_path: Path) -> list[dict[str, Any]]:
    """Legacy context/ layout: save_dir/context/step_NNN.png + sibling files."""
    context_dir = save_path / "context"
    if not context_dir.exists():
        return []

    buckets: dict[int, dict[str, str]] = {}

    def _bucket(step_num: int) -> dict[str, str]:
        if step_num not in buckets:
            buckets[step_num] = {}
        return buckets[step_num]

    for png in context_dir.glob("step_*.png"):
        n = _step_num_from_path(png)
        if n is not None:
            _bucket(n)["screenshot"] = png.relative_to(save_path).as_posix()
    for txt in context_dir.glob("step_*_raw_response.txt"):
        n = _step_num_from_path(txt)
        if n is not None:
            _bucket(n)["raw_response"] = txt.relative_to(save_path).as_posix()
    for pj in context_dir.glob("step_*_parsed_actions.json"):
        n = _step_num_from_path(pj)
        if n is not None:
            _bucket(n)["parsed_actions"] = pj.relative_to(save_path).as_posix()

    return [{"step_num": n, **buckets[n]} for n in sorted(buckets.keys())]


def _index_steps_traj_layout(save_path: Path) -> list[dict[str, Any]]:
    """run_mypcbench.py layout: flat save_dir with step_N_<ts>.png + traj.jsonl.

    For this layout there are no `_raw_response.txt` or `_parsed_actions.json`
    files — the raw model output and the action are fields on each line of
    `traj.jsonl`. We inline them into each step dict as `raw_response_text`
    and `parsed_action_obj` so downstream consumers don't have to know
    about the layout.
    """
    traj_path = save_path / "traj.jsonl"
    if not traj_path.exists():
        return []

    # Map step_num → screenshot rel-path by scanning the flat directory.
    screenshot_by_step: dict[int, str] = {}
    for png in save_path.glob("step_*.png"):
        n = _step_num_from_path(png)
        if n is not None and n not in screenshot_by_step:
            screenshot_by_step[n] = png.relative_to(save_path).as_posix()

    out: list[dict[str, Any]] = []
    try:
        for line in traj_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            step_num = row.get("step_num")
            if not isinstance(step_num, int):
                continue
            # Prefer the screenshot_file named in the traj row (authoritative);
            # fall back to whatever we found on disk.
            screenshot_rel = row.get("screenshot_file")
            if isinstance(screenshot_rel, str) and screenshot_rel:
                # Normalize to POSIX relative path
                screenshot_rel = screenshot_rel.replace("\\", "/").lstrip("./")
            else:
                screenshot_rel = screenshot_by_step.get(step_num)
            entry = {
                "step_num": step_num,
                "raw_response_text": row.get("response") or "",
                "parsed_action_obj": {
                    "action": row.get("action"),
                    "reward": row.get("reward"),
                    "done": row.get("done"),
                },
            }
            if screenshot_rel:
                entry["screenshot"] = screenshot_rel
            out.append(entry)
    except Exception:
        return []

    out.sort(key=lambda s: s.get("step_num") or 0)
    return out


def _index_steps(save_dir: str) -> list[dict[str, Any]]:
    """Group per-step files by step number. Handles both layouts:
    - legacy context/ layout: `save_dir/context/step_NNN.{png,_raw_response.txt,_parsed_actions.json}`
    - run_mypcbench.py: flat `save_dir/step_N_<ts>.png` + `traj.jsonl`

    Returns a list of `{step_num, screenshot?, raw_response?|raw_response_text?,
    parsed_actions?|parsed_action_obj?}` dicts, sorted by step number. All
    file paths are POSIX-relative to `save_dir`.
    """
    save_path = Path(save_dir)

    ctx = _index_steps_context_layout(save_path)
    if ctx:
        return ctx
    return _index_steps_traj_layout(save_path)


def _collect_context_artifacts(save_dir: str) -> dict[str, Any]:
    save_path = Path(save_dir)
    context_dir = save_path / "context"
    screenshot_paths = sorted(path.relative_to(save_path).as_posix() for path in context_dir.glob("step_*.png"))
    raw_response_paths = sorted(path.relative_to(save_path).as_posix() for path in context_dir.glob("step_*_raw_response.txt"))
    parsed_action_paths = sorted(path.relative_to(save_path).as_posix() for path in context_dir.glob("step_*_parsed_actions.json"))

    # New: per-step index that zips the flat lists by step number so the
    # per-step max-reduce judge can iterate step-by-step without re-parsing
    # filenames. The flat lists above stay for back-compat.
    steps = _index_steps(save_dir)

    return {
        "context_dir": context_dir.relative_to(save_path).as_posix() if context_dir.exists() else None,
        "chat_log": "context/chat_log.json" if (context_dir / "chat_log.json").exists() else None,
        "token_usage": "context/token_usage.json" if (context_dir / "token_usage.json").exists() else None,
        "screenshots": screenshot_paths,
        "raw_responses": raw_response_paths,
        "parsed_actions": parsed_action_paths,
        "steps": steps,
        "counts": {
            "screenshots": len(screenshot_paths),
            "raw_responses": len(raw_response_paths),
            "parsed_actions": len(parsed_action_paths),
            "steps": len(steps),
        },
    }


def default_rubric_judge_script_path() -> Path:
    """Pick the judge script. Default = OSWorld-style full-trajectory per-rubric.

    Override with `MYPCBENCH_JUDGE_FLAVOR=per_step` to fall back to the legacy
    `default_rubric_judge.py` (per-step max-reduce). Any other value uses the
    OSWorld full-trajectory judge.
    """
    flavor = (os.environ.get("MYPCBENCH_JUDGE_FLAVOR") or "").strip().lower()
    if flavor == "per_step":
        return Path(__file__).with_name("default_rubric_judge.py")
    return Path(__file__).with_name("osworld_full_traj_judge.py")


def resolve_rubric_judge_command(explicit_command: str | None = None) -> str:
    candidate = (explicit_command or os.environ.get("MYPCBENCH_RUBRIC_JUDGE_COMMAND") or "").strip()
    if candidate:
        return candidate

    script_path = default_rubric_judge_script_path()
    return shlex.join(["python3", str(script_path)])


def build_rubric_bundle(
    *,
    save_dir: str,
    task_dict: dict[str, Any],
    grading_manifest: dict[str, Any],
    task_language: str,
    env_language: str,
    snapshot_name: str | None,
    persona: str,
    world: str,
    vm_id: str,
    actor_id: str | None,
) -> RubricBundle:
    payload = {
        "task": {
            "id": task_dict.get("id"),
            "category": task_dict.get("category"),
            "language": task_language,
            "instruction": _resolve_instruction(task_dict, task_language),
            "persona": task_dict.get("persona"),
            "apps_involved": task_dict.get("apps_involved") if isinstance(task_dict.get("apps_involved"), list) else [],
            "difficulty": task_dict.get("difficulty"),
        },
        "environment": {
            "env_language": env_language,
            "snapshot_name": snapshot_name,
            "persona": persona,
            "world": world,
            "vm_id": vm_id,
            "actor_id": actor_id,
        },
        "grading_manifest": grading_manifest,
        "artifacts": _collect_context_artifacts(save_dir),
    }
    bundle_path = Path(save_dir) / "rubric_bundle.json"
    bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return RubricBundle(path=bundle_path, payload=payload)


def _parse_judge_score(stdout: str) -> int | None:
    text = stdout.strip()
    if not text:
        # Empty stdout from a judge that exited 0 (killed/flush-lost/swallowed)
        # is a judge failure, not a genuine "all rubrics failed" 0 — return
        # None so judge_results classifies it judge_error, not a real grade.
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, bool):
        return 100 if parsed else 0
    if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
        return int(parsed)
    if isinstance(parsed, dict):
        for key in ("score", "result", "value"):
            if key in parsed:
                candidate = parsed[key]
                if isinstance(candidate, bool):
                    return 100 if candidate else 0
                if isinstance(candidate, (int, float)):
                    return int(candidate)
                if isinstance(candidate, str):
                    match = re.search(r"-?\d+", candidate.strip())
                    if match:
                        return int(match.group(0))
        if "passed" in parsed and isinstance(parsed["passed"], bool):
            return 100 if parsed["passed"] else 0

    lowered = text.lower()
    if lowered in {"true", "pass", "passed", "yes"}:
        return 100
    if lowered in {"false", "fail", "failed", "no"}:
        return 0

    match = re.search(r"-?\d+", text)
    if match:
        return int(match.group(0))
    return None


def run_rubric_judge_command(
    command: str,
    *,
    bundle_path: Path,
    save_dir: str,
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
) -> int | list[str]:
    env = os.environ.copy()
    env.update(extra_env or {})
    # Resolve to absolute paths before forking the judge — the subprocess
    # runs with cwd=save_dir, so a relative bundle_path interpreted from
    # there fails (FileNotFoundError when launcher passed --result-dir as
    # a relative path).
    bundle_path_abs = bundle_path if bundle_path.is_absolute() else bundle_path.resolve()
    save_dir_abs = save_dir if os.path.isabs(save_dir) else os.path.abspath(save_dir)
    env["MYPCBENCH_RUBRIC_BUNDLE_PATH"] = str(bundle_path_abs)
    env["MYPCBENCH_RUBRIC_SAVE_DIR"] = save_dir_abs
    # NOTE: the bundle is passed by PATH only. Do not inject the full
    # bundle JSON into the environment — a long trajectory makes the env
    # block exceed ARG_MAX and subprocess fork dies with E2BIG, aborting
    # the whole grading run. The judges re-open the file via _PATH.

    try:
        result = subprocess.run(
            command if isinstance(command, str) else shlex.join(command),
            shell=True,
            cwd=save_dir_abs,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [command, "timeout", f"Judge timed out after {timeout}s", ""]

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return [
            command,
            str(result.returncode),
            stdout,
            stderr,
        ]

    parsed_score = _parse_judge_score(stdout)
    if parsed_score is None:
        return [command, "parse_error", stdout, stderr]
    return parsed_score


def default_rubric_judge_env(agent_model: str = "") -> dict[str, str]:
    """Pick the rubric-judge model + provider for the spawned judge subprocess.

    Used by the standalone offline grader (agent-harness/judge_results.py).
    The runner itself no longer grades inline.

    Priority:
      1. Explicit ``MYPCBENCH_RUBRIC_JUDGE_MODEL`` — keep as-is.
      2. ``MYPCBENCH_JUDGE_FLAVOR=per_step`` — legacy per-step max-reduce
         judge; defer to its existing Anthropic / OpenAI defaults.
      3. ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``) — OSWorld-style
         full-trajectory judge on Gemini 3.1 Flash Lite Preview (the
         configuration used for every number in the paper).
      4. ``ANTHROPIC_API_KEY`` only — legacy per-step judge on Sonnet.
      5. Otherwise — local vLLM via ``OPENAI_BASE_URL`` + the agent's own
         model as the judge model (final fallback for offline/CI runs).
    """
    if os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MODEL"):
        return {}

    flavor = (os.environ.get("MYPCBENCH_JUDGE_FLAVOR") or "").strip().lower()
    if flavor == "per_step":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {}
        base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
        if base_url and "api.openai.com" not in base_url and agent_model.strip():
            return {"MYPCBENCH_RUBRIC_JUDGE_MODEL": agent_model}
        return {}

    # Default flavor = OSWorld full-trajectory.
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return {"MYPCBENCH_RUBRIC_JUDGE_MODEL": "gemini-3.1-flash-lite-preview"}

    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"MYPCBENCH_JUDGE_FLAVOR": "per_step"}

    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    if base_url and "api.openai.com" not in base_url and agent_model.strip():
        return {
            "MYPCBENCH_JUDGE_FLAVOR": "per_step",
            "MYPCBENCH_RUBRIC_JUDGE_MODEL": agent_model,
        }
    if os.environ.get("OPENAI_API_KEY"):
        return {"MYPCBENCH_JUDGE_FLAVOR": "per_step"}
    return {}
