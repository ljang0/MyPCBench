#!/usr/bin/env python3
"""Verify the published MyPCBench release image is fresh and identical.

This runner-only repo does not build VM images. It consumes the externally
published Docker/HuggingFace artifacts, so release readiness depends on proving
that:

1. Docker Hub has recent publish proof for the current `latest` digest.
2. A dated Michael Scott tag exists for the expected build date.
3. The qcow2 baked inside Docker matches the HuggingFace qcow2 LFS hash.

Docker Hub may leave `latest.last_updated` unchanged when `latest` is recreated
to the same digest, so a matching current dated tag is also treated as freshness
proof.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def dockerhub_tags(repo: str, limit_pages: int = 5) -> dict[str, dict[str, Any]]:
    tags: dict[str, dict[str, Any]] = {}
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100"
    for _ in range(limit_pages):
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
        for row in data.get("results", []):
            name = row.get("name")
            if name:
                tags[name] = {
                    "digest": row.get("digest"),
                    "last_updated": row.get("last_updated"),
                }
        url = data.get("next")
        if not url:
            break
    return tags


def hf_lfs_sha(repo_id: str, filename: str) -> tuple[str, int]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is required; install requirements.txt") from exc

    info = HfApi().dataset_info(repo_id, files_metadata=True)
    for sibling in info.siblings:
        if sibling.rfilename != filename:
            continue
        lfs = sibling.lfs
        if not lfs or not getattr(lfs, "sha256", None):
            raise RuntimeError(f"{repo_id}/{filename} has no LFS sha256 metadata")
        return lfs.sha256, int(lfs.size)
    raise RuntimeError(f"{filename} not found in dataset {repo_id}")


def run_output(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def docker_embedded_qcow_sha(image: str) -> str:
    subprocess.run(["docker", "pull", image], check=True)
    output = run_output(["docker", "run", "--rm", "--entrypoint", "sha256sum", image, "/baseline/mypcbench.qcow2"])
    return output.split()[0]


def resolve_required_date(value: str, now: dt.datetime) -> str | None:
    if value == "none":
        return None
    if value == "today":
        return now.date().isoformat()
    if value == "yesterday":
        return (now.date() - dt.timedelta(days=1)).isoformat()
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--require-date-tag must be none, today, yesterday, or YYYY-MM-DD") from exc
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-repo", default="ljang/mypcbench-qemu")
    parser.add_argument("--docker-image", default="ljang/mypcbench-qemu:latest")
    parser.add_argument("--hf-repo", default="ljang0/mypcbench-qemu-baseline")
    parser.add_argument("--hf-file", default="michael_scott.qcow2")
    parser.add_argument("--date-tag-prefix", default="michael_scott")
    parser.add_argument("--require-date-tag", default="today",
                        help="none, today, yesterday, or YYYY-MM-DD. Default: today.")
    parser.add_argument("--max-latest-age-hours", type=float, default=36.0)
    parser.add_argument("--check-docker-embedded", action="store_true",
                        help="Pull Docker image and sha256sum /baseline/mypcbench.qcow2.")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    now = utcnow()
    required_date = resolve_required_date(args.require_date_tag, now)
    failures: list[str] = []

    tags = dockerhub_tags(args.docker_repo)
    latest = tags.get("latest")
    latest_updated = None
    if not latest:
        failures.append("Docker Hub tag 'latest' is missing")
    else:
        latest_updated = parse_time(latest["last_updated"])

    matching_date_tags = []
    freshness_times = [latest_updated] if latest_updated else []
    if required_date:
        wanted_prefix = f"{args.date_tag_prefix}-{required_date}"
        matching_date_tags = sorted(name for name in tags if name == wanted_prefix or name.startswith(wanted_prefix + "-"))
        if not matching_date_tags:
            failures.append(f"Missing Docker date tag matching {wanted_prefix}")
        elif latest and not any(tags[name].get("digest") == latest.get("digest") for name in matching_date_tags):
            failures.append(
                f"No {wanted_prefix} tag points at latest digest {latest.get('digest')}"
            )
        else:
            freshness_times.extend(
                parse_time(tags[name]["last_updated"])
                for name in matching_date_tags
                if latest and tags[name].get("digest") == latest.get("digest")
            )

    freshness_time = max((t for t in freshness_times if t), default=None)
    freshness_age_hours = round((now - freshness_time).total_seconds() / 3600, 2) if freshness_time else None
    if freshness_age_hours is not None and freshness_age_hours > args.max_latest_age_hours:
        failures.append(
            f"Docker publish proof is stale: age {freshness_age_hours}h > {args.max_latest_age_hours}h"
        )

    hf_sha, hf_size = hf_lfs_sha(args.hf_repo, args.hf_file)
    docker_qcow_sha = None
    if args.check_docker_embedded:
        docker_qcow_sha = docker_embedded_qcow_sha(args.docker_image)
        if docker_qcow_sha != hf_sha:
            failures.append(
                f"Docker embedded qcow2 sha {docker_qcow_sha} != HF {args.hf_file} sha {hf_sha}"
            )

    summary = {
        "checked_at": now.isoformat(),
        "docker_repo": args.docker_repo,
        "docker_image": args.docker_image,
        "docker_latest": latest,
        "docker_latest_age_hours": round((now - latest_updated).total_seconds() / 3600, 2) if latest_updated else None,
        "docker_effective_freshness_age_hours": freshness_age_hours,
        "docker_effective_freshness_time": freshness_time.isoformat() if freshness_time else None,
        "required_date": required_date,
        "matching_date_tags": {name: tags[name] for name in matching_date_tags},
        "hf_repo": args.hf_repo,
        "hf_file": args.hf_file,
        "hf_qcow2_sha256": hf_sha,
        "hf_qcow2_size": hf_size,
        "docker_embedded_qcow2_sha256": docker_qcow_sha,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
