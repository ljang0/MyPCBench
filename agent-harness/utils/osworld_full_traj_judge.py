#!/usr/bin/env python3
"""Full-trajectory per-rubric judge for MyPCBench (the paper judge).

Ported and adapted from Odysseys' `scripts/python/run_full_trajectory_per_rubric.py`
(https://github.com/ljang0/Odysseys, CC-BY-4.0), wired to the MyPCBench
rubric-bundle plumbing produced by `utils.rubric_judge.build_rubric_bundle()`.
It implements the OSWorld-style full-trajectory evaluation paradigm: one
LLM call per rubric item, reasoning over the entire trajectory.

Pipeline (one invocation per task):

1. Load the bundle JSON path from `MYPCBENCH_RUBRIC_BUNDLE_PATH`
   (set by `rubric_judge.run_rubric_judge_command()`).
2. Read the agent trajectory: every screenshot + every `traj.jsonl` row
   (or the equivalent legacy `context/` layout).
3. For EACH rubric item, issue ONE LLM call that sees:
     - the user task instruction (context only)
     - that single rubric item's criterion + weight
     - the FULL action history (one line per step)
     - up to MAX_IMAGES screenshots (chronological, last N kept)
   The judge returns "Status: success" or "Status: failure" for that
   rubric. This is the OSWorld evaluation paradigm — full-trajectory
   reasoning per rubric item, replacing the earlier per-step
   max-reduce default.
4. The final score = sum(weight_i * pass_i) / sum(weight_i) * 100,
   clamped to [0, 100], same shape as the legacy judge so
   `run_rubric_judge_command()` parses it identically.

Output:
- Writes `osworld_full_traj_result.json` to the save dir with the per-rubric
  breakdown (criterion, weight, success, reasoning) for debugging.
- Prints `{"score": <int 0..100>, "passed": <bool>, "reasoning": <str>}`
  to stdout — same contract as `default_rubric_judge.py`.

Defaults & env knobs:
- Model: `MYPCBENCH_RUBRIC_JUDGE_MODEL` (default `gemini-3.1-flash-lite-preview`).
  Gemini-prefixed models route to `google-genai`; everything else routes
  to OpenAI-compatible chat completions (so `OPENAI_BASE_URL` + a
  Qwen/local key still works as a fallback judge).
- Auth: `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) for Gemini;
  `OPENAI_API_KEY` (+ optional `OPENAI_BASE_URL`) otherwise. If neither
  is present, falls back to `ANTHROPIC_API_KEY` only via the legacy
  per-step judge — set `MYPCBENCH_JUDGE_FLAVOR=per_step` to take that path.
- `MYPCBENCH_OSWORLD_JUDGE_MAX_IMAGES` (default 200): keep the last N
  screenshots per trajectory; 0 = unlimited.
- `MYPCBENCH_OSWORLD_JUDGE_CONCURRENCY` (default 4): parallel rubric
  calls. Each rubric is judged independently so concurrency is safe.
- `MYPCBENCH_RUBRIC_JUDGE_MOCK_SCORE`: bypass the LLM and return a
  fixed score (kept for the same test harness that mocks the legacy
  judge).
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_MAX_IMAGES = 200
DEFAULT_CONCURRENCY = 4
FINAL_JUDGMENT_MAX_COMPLETION_TOKENS = 8192


FULL_TRAJ_JUDGMENT_SYSTEM = """You are an expert evaluator of desktop-agent trajectories.

You will receive:
- The user task (for context).
- ONE specific rubric item with a criterion and (optional) verification description.
- The agent's full action history (one line per step).
- Every screenshot from the trajectory, in chronological order.

Your goal is to decide whether this single rubric item is satisfied by the trajectory.

Evaluation rules:
- Judge ONLY the one rubric item you are given; ignore all other implicit requirements.
- Ground your judgment in what the screenshots and actions actually show. Do not invent state.
- Filtering / sorting / form requirements must be applied AND confirmed (visible) to count as satisfied.
- If the agent was blocked (captcha, access denied, crash, etc.) and therefore could not satisfy the rubric, report failure.
- If a later step UNDID the rubric (e.g. user-visible state was correct, then was overwritten with wrong data), report failure.

Respond in exactly this format:

Thoughts: <your reasoning, citing specific steps/screenshots>
Status: "success" or "failure"
"""


# ---------------------------------------------------------------------------
# Bundle loading + step indexing
# ---------------------------------------------------------------------------


def _load_bundle() -> tuple[Path, dict[str, Any], Path]:
    """Locate the rubric bundle and the trajectory save dir."""
    bundle_path_value = os.environ.get("MYPCBENCH_RUBRIC_BUNDLE_PATH", "").strip()
    if not bundle_path_value:
        print(json.dumps({"error": "missing MYPCBENCH_RUBRIC_BUNDLE_PATH"}))
        raise SystemExit(2)
    bundle_path = Path(bundle_path_value)
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))

    save_dir_env = os.environ.get("MYPCBENCH_RUBRIC_SAVE_DIR", "").strip()
    save_root = Path(save_dir_env) if save_dir_env else bundle_path.parent
    return bundle_path, payload, save_root


def _resolve_screenshot(save_root: Path, rel: str) -> Path | None:
    if not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for guess in (save_root / rel, save_root / candidate.name, candidate):
        if guess.exists():
            return guess
    return None


def _build_history_and_images(
    bundle: dict[str, Any], save_root: Path, max_images: int
) -> tuple[str, int, list[dict[str, Any]]]:
    """Return (action_history_text, num_total_steps, screenshot_assets)."""
    artifacts = bundle.get("artifacts") if isinstance(bundle.get("artifacts"), dict) else {}
    steps = artifacts.get("steps") if isinstance(artifacts.get("steps"), list) else []

    history_lines: list[str] = []
    resolved_paths: list[Path] = []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        parts: list[str] = []
        # Raw response: short reasoning summary if present
        raw = (step.get("raw_response_text") or "").strip()
        if raw:
            # Truncate per-step raw response to keep the prompt tight; the
            # screenshots are the primary evidence.
            parts.append(f"Response: {raw[:600].strip()}")
        parsed = step.get("parsed_action_obj")
        action_text = ""
        if isinstance(parsed, dict):
            action_field = parsed.get("action")
            if isinstance(action_field, str):
                action_text = action_field.strip()
            elif isinstance(action_field, dict):
                action_text = json.dumps(action_field, ensure_ascii=True, sort_keys=True)
            elif action_field is not None:
                action_text = str(action_field).strip()
        if not action_text:
            # context/ layout: parsed_actions is on disk, not inlined
            pa_rel = step.get("parsed_actions")
            if pa_rel:
                pa_path = save_root / pa_rel
                if pa_path.exists():
                    try:
                        action_text = pa_path.read_text(encoding="utf-8", errors="ignore").strip()[:400]
                    except Exception:
                        action_text = ""
        if action_text:
            parts.append(f"Action: {action_text}")
        if parts:
            history_lines.append(f"{i}. " + "\n".join(parts))

        screenshot_rel = step.get("screenshot")
        if isinstance(screenshot_rel, str) and screenshot_rel:
            path = _resolve_screenshot(save_root, screenshot_rel)
            if path is not None:
                resolved_paths.append(path)

    action_history = "\n".join(history_lines) if history_lines else "No actions recorded."

    # Trim to last `max_images` screenshots (OSWorld default = 200, fits Gemini 3.x budget)
    if max_images > 0:
        kept = resolved_paths[-max_images:]
    else:
        kept = resolved_paths

    screenshot_assets: list[dict[str, Any]] = []
    for path in kept:
        try:
            data = path.read_bytes()
        except Exception:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        screenshot_assets.append(
            {
                "bytes": data,
                "mime": mime,
                "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
                "path": str(path),
            }
        )

    return action_history, len(steps), screenshot_assets


# ---------------------------------------------------------------------------
# Rubric extraction
# ---------------------------------------------------------------------------


def _extract_rubrics(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    grading = bundle.get("grading_manifest") if isinstance(bundle.get("grading_manifest"), dict) else {}
    rubrics = grading.get("rubrics")
    if not isinstance(rubrics, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for i, r in enumerate(rubrics):
        if not isinstance(r, dict):
            continue
        rubric_id = str(r.get("id") or r.get("rubric_id") or f"R{i + 1}")
        # MyPCBench rubrics use `criterion`; OSWorld used `requirement`. Accept both.
        requirement = str(r.get("criterion") or r.get("requirement") or "").strip()
        verification = str(r.get("verification") or "").strip()
        weight = r.get("weight")
        try:
            weight_f = float(weight) if weight is not None else 0.0
        except Exception:
            weight_f = 0.0
        cleaned.append(
            {
                "id": rubric_id,
                "requirement": requirement,
                "verification": verification,
                "weight": weight_f if weight_f > 0 else 1.0,
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------


def _is_gemini_model(model: str) -> bool:
    return model.lower().startswith("gemini")


def _make_gemini_client():
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise SystemExit(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is required for the OSWorld full-trajectory "
            "judge. Either export the key, set MYPCBENCH_RUBRIC_JUDGE_MODEL to a non-Gemini "
            "model (e.g. gpt-4o-mini), or set MYPCBENCH_JUDGE_FLAVOR=per_step to use the "
            "legacy per-step judge."
        )
    try:
        from google import genai  # type: ignore
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": (
                        f"google-genai import failed: {exc}. Install with "
                        "`pip install google-genai`, or set MYPCBENCH_JUDGE_FLAVOR=per_step."
                    )
                }
            )
        )
        raise SystemExit(2)
    return genai, genai.Client(api_key=api_key)


def _make_openai_client():
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for non-Gemini judge models.")
    try:
        from openai import AsyncOpenAI  # type: ignore
    except Exception as exc:
        print(json.dumps({"error": f"openai import failed: {exc}"}))
        raise SystemExit(2)
    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Per-rubric judging
# ---------------------------------------------------------------------------


def _user_text_for_rubric(
    task_instruction: str,
    rubric: dict[str, Any],
    action_history: str,
    n_screenshots: int,
    n_steps: int,
) -> str:
    rubric_lines = [
        f"Rubric ID: {rubric['id']}",
        f"Requirement: {rubric['requirement']}",
    ]
    if rubric.get("verification"):
        rubric_lines.append(f"Verification: {rubric['verification']}")
    rubric_lines.append(f"Weight: {rubric['weight']}")
    return (
        f"User Task (context only): {task_instruction}\n\n"
        "Evaluate ONLY this rubric item:\n"
        + "\n".join(rubric_lines)
        + f"\n\nFull Action History:\n{action_history}\n\n"
        f"Screenshots attached below: {n_screenshots} "
        f"(trajectory had {n_steps} total step(s)).\n\n"
        f"Decide whether the rubric ({rubric['id']}) is satisfied. "
        "Use the required 'Thoughts:' / 'Status:' format."
    )


def _parse_judge_response(text: str) -> tuple[bool, str]:
    text = (text or "").strip()
    status_match = re.search(r'Status:\s*["\']?(success|failure)["\']?', text, re.IGNORECASE)
    thoughts_match = re.search(r"Thoughts:\s*(.+?)(?:Status:|$)", text, re.DOTALL)
    success = bool(status_match and status_match.group(1).lower() == "success")
    reasoning = thoughts_match.group(1).strip() if thoughts_match else (text or "Empty judge response.")
    return success, reasoning


async def _judge_one_rubric_gemini(
    genai_module: Any,
    client: Any,
    model: str,
    user_text: str,
    screenshot_assets: list[dict[str, Any]],
) -> tuple[bool, str]:
    types = genai_module.types
    parts = [types.Part.from_text(text=user_text)] + [
        types.Part.from_bytes(data=s["bytes"], mime_type=s["mime"])
        for s in screenshot_assets
    ]
    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=FULL_TRAJ_JUDGMENT_SYSTEM,
            max_output_tokens=FINAL_JUDGMENT_MAX_COMPLETION_TOKENS,
        ),
    )
    return _parse_judge_response(str(response.text or ""))


async def _judge_one_rubric_openai(
    client: Any,
    model: str,
    user_text: str,
    screenshot_assets: list[dict[str, Any]],
) -> tuple[bool, str]:
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for s in screenshot_assets:
        user_content.append(
            {"type": "image_url", "image_url": {"url": s["data_url"], "detail": "high"}}
        )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": FULL_TRAJ_JUDGMENT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=FINAL_JUDGMENT_MAX_COMPLETION_TOKENS,
    )
    text = ""
    try:
        text = str(response.choices[0].message.content or "")
    except Exception:
        text = ""
    return _parse_judge_response(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async() -> None:
    mock_score = (
        os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MOCK_SCORE", "").strip()
        or os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MOCK_RESULT", "").strip()
    )
    if mock_score:
        try:
            score = int(mock_score)
        except ValueError:
            score = 0
        print(json.dumps({"score": score, "passed": score >= 50, "reasoning": "mock score"}))
        return

    bundle_path, bundle, save_root = _load_bundle()
    task_meta = bundle.get("task") if isinstance(bundle.get("task"), dict) else {}
    task_instruction = (task_meta.get("instruction") or "").strip() or "(no instruction provided)"

    rubrics = _extract_rubrics(bundle)
    if not rubrics:
        result = {"score": 0, "passed": False, "reasoning": "no rubrics in bundle"}
        _write_debug(save_root, {**result, "rubric_results": []})
        print(json.dumps(result))
        return

    try:
        max_images = int(os.environ.get("MYPCBENCH_OSWORLD_JUDGE_MAX_IMAGES", str(DEFAULT_MAX_IMAGES)) or DEFAULT_MAX_IMAGES)
    except ValueError:
        max_images = DEFAULT_MAX_IMAGES
    try:
        concurrency = int(os.environ.get("MYPCBENCH_OSWORLD_JUDGE_CONCURRENCY", str(DEFAULT_CONCURRENCY)) or DEFAULT_CONCURRENCY)
    except ValueError:
        concurrency = DEFAULT_CONCURRENCY
    concurrency = max(1, concurrency)

    action_history, n_steps, screenshot_assets = _build_history_and_images(
        bundle, save_root, max_images
    )

    if n_steps == 0 and not screenshot_assets:
        result = {
            "score": 0,
            "passed": False,
            "reasoning": "no per-step artifacts (steps[] empty)",
        }
        _write_debug(save_root, {**result, "rubric_results": []})
        print(json.dumps(result))
        return

    model = (os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MODEL") or DEFAULT_MODEL).strip()

    if _is_gemini_model(model):
        genai_module, client = _make_gemini_client()

        async def judge_one(rubric: dict[str, Any]) -> tuple[bool, str]:
            user_text = _user_text_for_rubric(
                task_instruction, rubric, action_history, len(screenshot_assets), n_steps
            )
            try:
                return await _judge_one_rubric_gemini(
                    genai_module, client, model, user_text, screenshot_assets
                )
            except Exception as exc:
                return False, f"Error judging rubric {rubric['id']}: {exc}"
    else:
        client = _make_openai_client()

        async def judge_one(rubric: dict[str, Any]) -> tuple[bool, str]:
            user_text = _user_text_for_rubric(
                task_instruction, rubric, action_history, len(screenshot_assets), n_steps
            )
            try:
                return await _judge_one_rubric_openai(
                    client, model, user_text, screenshot_assets
                )
            except Exception as exc:
                return False, f"Error judging rubric {rubric['id']}: {exc}"

    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(rubric: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            success, reasoning = await judge_one(rubric)
            return {
                "rubric_id": rubric["id"],
                "criterion": rubric["requirement"],
                "verification": rubric["verification"],
                "weight": rubric["weight"],
                "success": success,
                "score": 1 if success else 0,
                "reasoning": reasoning,
            }

    rubric_results = await asyncio.gather(*[run_one(r) for r in rubrics])

    total_w = sum(r["weight"] for r in rubric_results) or 1.0
    total_s = sum(r["weight"] for r in rubric_results if r["success"])
    final_fraction = max(0.0, min(1.0, total_s / total_w))
    final_score_int = int(round(final_fraction * 100))

    debug_payload = {
        "score": final_score_int,
        # `passed` = perfect rubric satisfaction. Partial credit shows up
        # in `score` (0-100); the pass-rate counter only counts
        # trajectories that satisfied EVERY weighted criterion.
        "passed": final_score_int >= 100,
        "reasoning": (
            f"OSWorld full-trajectory per-rubric: "
            f"{sum(1 for r in rubric_results if r['success'])}/{len(rubric_results)} rubrics passed; "
            f"weighted fraction={final_fraction:.4f}"
        ),
        "model": model,
        "max_images": max_images,
        "concurrency": concurrency,
        "n_steps_total": n_steps,
        "n_screenshots_sent": len(screenshot_assets),
        "final_fraction": round(final_fraction, 4),
        "rubric_results": rubric_results,
        "rubric_count": len(rubric_results),
    }
    _write_debug(save_root, debug_payload)

    # Same minimal stdout shape as default_rubric_judge.py
    print(
        json.dumps(
            {
                "score": final_score_int,
                "passed": debug_payload["passed"],
                "reasoning": debug_payload["reasoning"],
            }
        )
    )


def _write_debug(save_root: Path, payload: dict[str, Any]) -> None:
    try:
        save_root.mkdir(parents=True, exist_ok=True)
        (save_root / "osworld_full_traj_result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover
        print(f"warning: failed to write osworld_full_traj_result.json: {exc}", file=sys.stderr)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
