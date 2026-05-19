#!/usr/bin/env python3
"""Default rubric judge — per-step scoring with max-reduce across trajectory.

Pipeline (one call to this script per task):

1. Load the bundle emitted by `rubric_judge.build_rubric_bundle()`.
2. Iterate over `bundle.artifacts.steps` — each step has a screenshot, the
   raw LLM response (reasoning + tool call), and the parsed action.
3. For each step, ask the judge LLM to score *every* rubric item on
   whether that one step satisfies the criterion. The judge returns a
   binary vector `{idx: 0 or 1}`.
4. Across all steps, reduce per-rubric via `max(...)`. A rubric item
   scores 1 iff *any* step satisfied it.
5. Compute the final weighted score = sum(weight_i * max_i) / sum(weight_i).

Output:
- Writes `rubric_result.json` to the save dir with the full per-step breakdown
  so debugging can see which step each rubric item passed on.
- Prints a minimal `{"score": <int 0..100>, "passed": <bool>}` JSON to stdout
  so `rubric_judge.run_rubric_judge_command()` can parse it back into an int.

Cost guardrails:
- `MYPCBENCH_JUDGE_STEP_SAMPLE` (int, default 1): if > 1, only score every Nth
  step plus the final step. Example: N=5 on a 100-step trajectory scores 21
  steps instead of 100.
- `MYPCBENCH_JUDGE_MAX_STEPS` (int, default 0 = no cap): hard cap on the number
  of steps scored. Keeps a 500-step run from blowing out the API budget.
- `MYPCBENCH_JUDGE_CONCURRENCY` (int, default 4): parallel LLM calls. Judge
  calls are independent so concurrency is safe.
- `MYPCBENCH_RUBRIC_JUDGE_MOCK_SCORE`: overrides everything with a mock score,
  same as the legacy judge. Kept for test harnesses.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import mimetypes
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover — optional dependency
    httpx = None


# ---- bundle loading ------------------------------------------------------

def _load_bundle() -> tuple[Path, dict[str, Any]]:
    bundle_path_value = os.environ.get("MYPCBENCH_RUBRIC_BUNDLE_PATH", "").strip()
    if not bundle_path_value:
        print(json.dumps({"error": "missing MYPCBENCH_RUBRIC_BUNDLE_PATH"}))
        raise SystemExit(2)
    bundle_path = Path(bundle_path_value)
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    return bundle_path, payload


def _safe_read_text(path: Path, limit: int = 2500) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def _safe_read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _encode_image(path: Path) -> str | None:
    if not path.exists():
        return None
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    except Exception:
        return None


# ---- step selection ------------------------------------------------------

def _select_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply `MYPCBENCH_JUDGE_STEP_SAMPLE` + `MYPCBENCH_JUDGE_MAX_STEPS`.

    Default behavior: score every step. With sampling N, keep every Nth step
    plus the final step (so the terminal state always gets scored, since some
    rubric items only fire at the end — "agent delivers final brief").
    """
    if not steps:
        return []

    try:
        sample = int(os.environ.get("MYPCBENCH_JUDGE_STEP_SAMPLE", "1") or "1")
    except ValueError:
        sample = 1
    try:
        max_steps = int(os.environ.get("MYPCBENCH_JUDGE_MAX_STEPS", "0") or "0")
    except ValueError:
        max_steps = 0

    if sample <= 1:
        selected = list(steps)
    else:
        selected = [
            s for i, s in enumerate(steps)
            if (i + 1) % sample == 0 or i == len(steps) - 1
        ]
        # De-dupe in case the last step aligned with the sample stride
        seen = set()
        deduped = []
        for s in selected:
            n = s.get("step_num")
            if n in seen:
                continue
            seen.add(n)
            deduped.append(s)
        selected = deduped

    # Hard cap: keep the earliest + last steps if we have to truncate
    if max_steps and len(selected) > max_steps:
        head_room = max(max_steps - 1, 0)
        selected = selected[:head_room] + selected[-1:]

    return selected


# ---- prompt construction -------------------------------------------------

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict benchmark evaluator for a desktop-agent task. "
    "You will see ONE step of an agent trajectory — its screenshot, the raw "
    "LLM response that produced the step (containing any reasoning + tool call), "
    "and the parsed pyautogui/action payload. "
    "For each numbered rubric criterion, decide whether THIS SINGLE STEP "
    "satisfies the criterion, based only on the evidence shown. Return strict "
    "JSON with a single key `criterion_scores` mapping the criterion index "
    "(as a string) to 1 (satisfied by this step) or 0 (not satisfied). "
    "Do not invent evidence. If a criterion describes a final terminal state "
    "that is not yet visible, score it 0 for this step."
)


def _build_rubric_lines(rubrics: list[dict[str, Any]]) -> str:
    lines = []
    for i, r in enumerate(rubrics):
        if not isinstance(r, dict):
            continue
        criterion = str(r.get("criterion") or "").strip()
        weight = r.get("weight")
        if criterion:
            lines.append(f"[{i}] {criterion} (weight={weight})")
    return "\n".join(lines)


def _build_step_prompt(
    step: dict[str, Any],
    rubric_lines: str,
    task_meta: dict[str, Any],
    save_root: Path,
) -> tuple[str, list[dict[str, Any]]]:
    screenshot_rel = step.get("screenshot")
    raw_response_rel = step.get("raw_response")
    parsed_actions_rel = step.get("parsed_actions")
    step_num = step.get("step_num")

    # Two layouts: a legacy context/ layout writes per-step text to disk;
    # run_mypcbench.py inlines the text into `traj.jsonl`. `rubric_judge._index_steps()`
    # normalizes both into this dict shape — we just check which one is
    # present.
    raw_response_text = step.get("raw_response_text") or ""
    if not raw_response_text and raw_response_rel:
        raw_response_text = _safe_read_text(save_root / raw_response_rel)
    parsed_actions: Any = step.get("parsed_action_obj")
    if parsed_actions is None and parsed_actions_rel:
        parsed_actions = _safe_read_json(save_root / parsed_actions_rel)

    prompt_text = "\n".join(
        [
            f"Task ID: {task_meta.get('id')}",
            f"Instruction: {task_meta.get('instruction')}",
            f"Apps involved: {', '.join(task_meta.get('apps_involved') or []) or 'unknown'}",
            "",
            f"STEP {step_num}",
            "",
            "Raw model output for this step (reasoning + tool call):",
            "---",
            raw_response_text.strip() or "(no raw response captured)",
            "---",
            "",
            "Parsed action for this step:",
            json.dumps(parsed_actions, indent=2) if parsed_actions is not None else "(none)",
            "",
            "Rubric criteria — score whether THIS STEP satisfies each one:",
            rubric_lines or "(empty rubric)",
            "",
            "Return strict JSON: {\"criterion_scores\": {\"<idx>\": 0_or_1, ...}}. "
            "No other keys. No prose.",
        ]
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    if screenshot_rel:
        image_url = _encode_image(save_root / screenshot_rel)
        if image_url:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url, "detail": "high"},
                }
            )
    return prompt_text, content


# ---- LLM backends --------------------------------------------------------

def _create_openai_client(api_key: str, proxy_url: str | None):
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"error": f"openai_client_import_failed: {exc}"}))
        raise SystemExit(2)
    if proxy_url and httpx is not None:
        return OpenAI(api_key=api_key, http_client=httpx.Client(proxy=proxy_url))
    return OpenAI(api_key=api_key)


def _judge_step_openai_with_client(
    client: Any,
    model: str,
    content: list[dict[str, Any]],
    timeout: int,
    max_tokens: int,
) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        timeout=timeout,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "non-json judge response", "raw": raw[:500]}


def _judge_step_anthropic(
    api_key: str,
    model: str,
    content: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    try:
        from anthropic import Anthropic
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"error": f"anthropic_client_import_failed: {exc}"}))
        raise SystemExit(2)

    client = Anthropic(api_key=api_key)
    anthropic_content: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "text":
            anthropic_content.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image_url":
            url = block.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                header, data = url.split(",", 1)
                media_type = header.split(":")[1].split(";")[0]
                anthropic_content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    }
                )

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": anthropic_content}],
        timeout=timeout,
    )
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    try:
        # Anthropic sometimes wraps JSON in ```json blocks; strip them.
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        return json.loads(stripped or "{}")
    except Exception:
        pass
    # Fallback: find the first balanced top-level JSON object in the response.
    # Sonnet sometimes emits prose around the JSON when the agent's answer
    # contained markdown tables / dollar amounts / etc. that confused the
    # template. Extract via brace-depth scan.
    try:
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        start = -1
                        continue
    except Exception:
        pass
    return {"error": "non-json judge response", "raw": text[:500]}


# ---- scoring + reduction -------------------------------------------------

def _normalize_step_scores(
    parsed: dict[str, Any],
    n_rubrics: int,
) -> list[int]:
    """Extract a length-N_rubrics binary vector from the judge's response."""
    vec = [0] * n_rubrics
    if not isinstance(parsed, dict):
        return vec
    raw = parsed.get("criterion_scores")
    if not isinstance(raw, dict):
        # Some models may return a list — allow that too.
        if isinstance(raw, list):
            for i, v in enumerate(raw[:n_rubrics]):
                vec[i] = 1 if _truthy(v) else 0
        return vec
    for k, v in raw.items():
        try:
            idx = int(k)
        except Exception:
            continue
        if 0 <= idx < n_rubrics:
            vec[idx] = 1 if _truthy(v) else 0
    return vec


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v >= 0.5
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "pass", "passed", "y", "t"}
    return False


def _reduce_max(
    per_step_scores: list[dict[str, Any]],
    n_rubrics: int,
) -> list[int]:
    per_rubric_max = [0] * n_rubrics
    for step in per_step_scores:
        vec = step.get("scores") or []
        for i, v in enumerate(vec):
            if i >= n_rubrics:
                break
            if v > per_rubric_max[i]:
                per_rubric_max[i] = v
    return per_rubric_max


def _weighted_score(
    per_rubric_max: list[int],
    rubrics: list[dict[str, Any]],
) -> float:
    total_w = 0.0
    total_s = 0.0
    for i, r in enumerate(rubrics):
        if not isinstance(r, dict):
            continue
        try:
            w = float(r.get("weight") or 0)
        except Exception:
            w = 0.0
        if w <= 0:
            w = 1.0
        total_w += w
        if i < len(per_rubric_max) and per_rubric_max[i]:
            total_s += w
    if total_w <= 0:
        return 0.0
    return total_s / total_w


# ---- main ----------------------------------------------------------------

def main() -> None:
    mock_score = (
        os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MOCK_SCORE", "").strip()
        or os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MOCK_RESULT", "").strip()
    )
    if mock_score:
        score = int(mock_score)
        print(json.dumps({"score": score, "passed": score >= 50, "reasoning": "mock score"}))
        return

    timeout = int(os.environ.get("MYPCBENCH_RUBRIC_JUDGE_TIMEOUT", "120"))
    concurrency = max(1, int(os.environ.get("MYPCBENCH_JUDGE_CONCURRENCY", "4") or "4"))

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not anthropic_api_key and not openai_api_key:
        print(json.dumps({"error": "Either ANTHROPIC_API_KEY or OPENAI_API_KEY required"}))
        raise SystemExit(2)

    bundle_path, bundle = _load_bundle()
    save_root = bundle_path.parent
    save_dir_env = os.environ.get("MYPCBENCH_RUBRIC_SAVE_DIR", "").strip()
    if save_dir_env:
        save_root = Path(save_dir_env)

    task_meta = bundle.get("task") or {}
    rubrics = (
        bundle.get("grading_manifest", {}).get("rubrics")
        if isinstance(bundle.get("grading_manifest"), dict)
        else None
    )
    if not isinstance(rubrics, list):
        rubrics = []
    n_rubrics = len(rubrics)

    if n_rubrics == 0:
        print(json.dumps({"score": 0, "passed": False, "reasoning": "no rubrics in bundle"}))
        return

    artifacts = bundle.get("artifacts") if isinstance(bundle.get("artifacts"), dict) else {}
    steps = artifacts.get("steps") if isinstance(artifacts.get("steps"), list) else []

    if not steps:
        # No step data — fall back to an empty score. The caller should have
        # ensured per-step artifacts exist; if they don't, every rubric is 0.
        result = {
            "score": 0,
            "passed": False,
            "reasoning": "no per-step artifacts (context/ empty)",
            "n_steps_total": 0,
            "n_steps_scored": 0,
            "per_rubric_max": [0] * n_rubrics,
            "per_step_scores": [],
            "rubrics": rubrics,
        }
        _write_rubric_result(save_root, result)
        print(json.dumps({"score": 0, "passed": False, "reasoning": result["reasoning"]}))
        return

    selected_steps = _select_steps(steps)
    rubric_lines = _build_rubric_lines(rubrics)

    # Pick provider + model once
    if anthropic_api_key:
        model = os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MODEL", "claude-sonnet-4-6")
        judge_fn = lambda content: _judge_step_anthropic(anthropic_api_key, model, content, timeout)
    else:
        model = os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MODEL", "gpt-4o-mini")
        max_tokens = int(os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MAX_TOKENS", "256"))
        client = _create_openai_client(openai_api_key, os.environ.get("OPENAI_PROXY_URL"))
        judge_fn = lambda content: _judge_step_openai_with_client(
            client, model, content, timeout, max_tokens
        )

    def score_one_step(step: dict[str, Any]) -> dict[str, Any]:
        try:
            _prompt_text, content = _build_step_prompt(step, rubric_lines, task_meta, save_root)
            parsed = judge_fn(content)
            vec = _normalize_step_scores(parsed, n_rubrics)
            return {
                "step_num": step.get("step_num"),
                "screenshot": step.get("screenshot"),
                "scores": vec,
                "error": parsed.get("error") if isinstance(parsed, dict) else None,
            }
        except Exception as exc:  # pragma: no cover — best-effort per-step
            return {
                "step_num": step.get("step_num"),
                "screenshot": step.get("screenshot"),
                "scores": [0] * n_rubrics,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=2),
            }

    per_step_results: list[dict[str, Any]] = []
    if concurrency > 1 and len(selected_steps) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(score_one_step, s) for s in selected_steps]
            # Preserve input order when collecting results
            per_step_results = [f.result() for f in futures]
    else:
        per_step_results = [score_one_step(s) for s in selected_steps]

    per_rubric_max = _reduce_max(per_step_results, n_rubrics)
    final_fraction = _weighted_score(per_rubric_max, rubrics)
    final_score_int = int(round(final_fraction * 100))

    result = {
        "score": final_score_int,
        # `passed` = perfect rubric satisfaction. Partial credit shows up
        # in `score` (0-100); the pass-rate counter only counts
        # trajectories that satisfied EVERY weighted criterion.
        "passed": final_score_int >= 100,
        "reasoning": f"per-step max-reduce over {len(per_step_results)} of {len(steps)} steps",
        "n_steps_total": len(steps),
        "n_steps_scored": len(per_step_results),
        "concurrency": concurrency,
        "model": model,
        "final_fraction": round(final_fraction, 4),
        "per_rubric_max": per_rubric_max,
        "per_step_scores": per_step_results,
        "rubrics": rubrics,
    }
    _write_rubric_result(save_root, result)

    # Minimal stdout for back-compat with run_rubric_judge_command.
    print(json.dumps({
        "score": final_score_int,
        "passed": result["passed"],
        "reasoning": result["reasoning"],
    }))


def _write_rubric_result(save_root: Path, result: dict[str, Any]) -> None:
    try:
        (save_root / "rubric_result.json").write_text(
            json.dumps(result, indent=2, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover
        print(f"warning: failed to write rubric_result.json: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
