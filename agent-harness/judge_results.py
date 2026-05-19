#!/usr/bin/env python3
"""Offline rubric grader for a MyPCBench result directory.

The runner writes a completion marker (result.txt = 1.0 ran / 0.0 errored)
and a self-contained rubric_bundle.json per task. This is the separate
grading step: it runs the judge once per bundle and writes
rubric_judge_result.json plus an aggregate scores.json. Same for the
docker and qemu backends.

    python3 agent-harness/judge_results.py --result_dir results/opus
    python3 agent-harness/judge_results.py --result_dir results/opus --force

Judge selection matches the paper config (GEMINI_API_KEY →
gemini-3.1-flash-lite-preview); see utils.rubric_judge.default_rubric_judge_env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `utils` resolves the same way
# run_mypcbench.py bootstraps it.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.rubric_judge import (  # noqa: E402
    default_rubric_judge_env,
    resolve_rubric_judge_command,
    run_rubric_judge_command,
)

BUNDLE_NAME = "rubric_bundle.json"
JUDGE_RESULT_NAME = "rubric_judge_result.json"


def _score_to_unit(rubric_result) -> float | None:
    """Map a judge return value to a 0..1 score, or None if the judge errored.

    ``run_rubric_judge_command`` returns an int (0..100) on success or a
    list ``[command, code, stdout, stderr]`` on failure/timeout — never a
    bool, so only the numeric and list/None cases are handled here.
    """
    if isinstance(rubric_result, (int, float)):
        return max(0.0, min(1.0, float(rubric_result) / 100.0))
    return None


def judge_one(task_dir: Path, command: str, timeout: int, force: bool) -> dict:
    task_id = task_dir.name
    bundle_path = task_dir / BUNDLE_NAME
    out_path = task_dir / JUDGE_RESULT_NAME

    if not bundle_path.exists():
        return {"task_id": task_id, "status": "no_bundle", "score": None}

    if out_path.exists() and not force:
        try:
            cached = json.loads(out_path.read_text())
            _unit = _score_to_unit(cached.get("result"))
            # A cached result that doesn't map to a score is a cached judge
            # failure — surface it as judge_error so the summary tally is
            # honest (don't let a stale-error result dir read as clean).
            return {
                "task_id": task_id,
                "status": "cached" if _unit is not None else "judge_error",
                "score": _unit,
                "raw": cached.get("result"),
            }
        except (ValueError, OSError):
            pass  # fall through and re-judge

    rubric_result = run_rubric_judge_command(
        command,
        bundle_path=bundle_path,
        save_dir=str(task_dir),
        extra_env=default_rubric_judge_env(),
        timeout=timeout,
    )
    out_path.write_text(
        json.dumps(
            {"bundle_path": str(bundle_path), "result": rubric_result}, indent=2
        )
        + "\n"
    )
    unit = _score_to_unit(rubric_result)
    return {
        "task_id": task_id,
        "status": "judged" if unit is not None else "judge_error",
        "score": unit,
        "raw": rubric_result,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result_dir", required=True,
                    help="Runner output dir (per-task subdirs); crashed/errored tasks score 0.0")
    ap.add_argument("--force", action="store_true",
                    help="Re-judge even if rubric_judge_result.json already exists")
    ap.add_argument("--judge_command", default=None,
                    help="Override the judge command (default: resolve from env)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="Per-task judge subprocess timeout (s)")
    args = ap.parse_args()

    root = Path(args.result_dir)
    if not root.is_dir():
        print(f"error: --result_dir {root} is not a directory", file=sys.stderr)
        return 2

    command = resolve_rubric_judge_command(args.judge_command)
    # Loudly warn if the resolved judge is NOT the paper config. The judge
    # is selected by which API keys are present, so an honest reviewer with
    # only ANTHROPIC_API_KEY set silently grades with the wrong (per-step
    # Sonnet) judge and gets numbers that don't match the paper.
    # default_rubric_judge_env() returns {} when the model is explicitly
    # pinned, so resolve the EFFECTIVE model (explicit env override wins),
    # and treat per_step flavor as not-paper (paper = osworld full-traj).
    _je = default_rubric_judge_env()
    _eff_model = (os.environ.get("MYPCBENCH_RUBRIC_JUDGE_MODEL")
                  or _je.get("MYPCBENCH_RUBRIC_JUDGE_MODEL"))
    _flavor = (os.environ.get("MYPCBENCH_JUDGE_FLAVOR") or "").strip().lower()
    _paper = (_eff_model == "gemini-3.1-flash-lite-preview"
              and _flavor != "per_step")
    if not _paper and not args.judge_command:
        print(
            "WARNING: judge is NOT the paper config "
            "(gemini-3.1-flash-lite-preview full-trajectory). Resolved: "
            f"{_je or 'flavor/model defaults'}. To reproduce the paper "
            "numbers set GEMINI_API_KEY and clear MYPCBENCH_JUDGE_FLAVOR / "
            "MYPCBENCH_RUBRIC_JUDGE_MODEL.",
            file=sys.stderr,
        )
    # Every attempted task counts toward the benchmark denominator. A task
    # subdir is "attempted" if the runner left result.txt, incomplete.txt,
    # or a rubric bundle. Tasks that crashed (no bundle) or whose judge
    # errored score 0.0 — they are NOT silently dropped, or the average
    # would be optimistically biased vs. the paper's all-tasks denominator.
    task_dirs = sorted(
        p for p in root.iterdir()
        if p.is_dir() and any((p / m).exists()
                              for m in (BUNDLE_NAME, "result.txt", "incomplete.txt"))
    )
    if not task_dirs:
        print(f"error: no task result subdirs under {root} — nothing to grade. "
              f"Did the runner write any results?", file=sys.stderr)
        return 2

    def _summary(rows):
        # score=None (no_bundle / judge_error) counts as 0.0 toward the
        # canonical benchmark average over ALL attempted tasks. avg_graded
        # is a secondary diagnostic over the successfully-judged subset.
        # perfect_rate = fraction of tasks where every rubric passed
        # (score==1.0) — the paper's headline "Perfect %" metric.
        n = len(rows)
        graded = [r["score"] for r in rows if r["score"] is not None]
        avg_all = sum((r["score"] or 0.0) for r in rows) / n if n else 0.0
        perfect = sum(1 for r in rows
                      if r["score"] is not None and r["score"] >= 0.999)
        return {
            "result_dir": str(root),
            "tasks": n,
            "graded": len(graded),
            "no_bundle": sum(1 for r in rows if r["status"] == "no_bundle"),
            "judge_errors": sum(1 for r in rows if r["status"] == "judge_error"),
            "perfect": perfect,
            "perfect_rate": (perfect / n) if n else 0.0,  # paper headline
            "avg_score": avg_all,          # canonical: 0.0 for missing/errored
            "avg_graded": (sum(graded) / len(graded)) if graded else None,
            "rows": rows,
        }

    rows = []
    for td in task_dirs:
        try:
            row = judge_one(td, command, args.timeout, args.force)
        except Exception as e:  # never let one task abort the whole run
            row = {"task_id": td.name, "status": "judge_error",
                   "score": None, "error": f"{type(e).__name__}: {e}"}
        rows.append(row)
        s = row["score"]
        print(f"  {row['task_id']:<28} {row['status']:<11} "
              f"score={'0.00*' if s is None else f'{s:.2f}'}")
        # Write scores.json after every task so a crash/Ctrl-C mid-run
        # still leaves a usable partial aggregate (and is resumable).
        (root / "scores.json").write_text(
            json.dumps(_summary(rows), indent=2) + "\n")

    summary = _summary(rows)
    n = summary["tasks"]
    graded = [r["score"] for r in rows if r["score"] is not None]
    avg_all = summary["avg_score"]
    (root / "scores.json").write_text(json.dumps(summary, indent=2) + "\n")

    ag = summary["avg_graded"]
    print(f"\n=== {n} tasks | Perfect {summary['perfect']}/{n} "
          f"({summary['perfect_rate']:.1%}, paper headline) | "
          f"avg_score {avg_all:.3f} (Rubric %, over all {n}) | avg_graded "
          f"{'n/a' if ag is None else f'{ag:.3f}'} over {len(graded)} "
          f"→ {root / 'scores.json'} ===")
    if summary["no_bundle"] or summary["judge_errors"]:
        print(f"    note: {summary['no_bundle']} crashed (no bundle), "
              f"{summary['judge_errors']} judge-errored — counted as 0.0. "
              f"Report avg_score (all {n}) as the benchmark number.")
    # Exit 0 even with per-task errors (recorded, not fatal); only a
    # structural problem (no task dirs / bad path) is a non-zero exit.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
