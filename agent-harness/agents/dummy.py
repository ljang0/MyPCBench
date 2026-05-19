"""Dummy agent — returns DONE on first prediction.

Used for smoke-testing the harness and runners (run_mypcbench.py,
run_parallel_tasks.py) without requiring API keys
or real LLM inference. Exercises the reset → predict → step →
evaluate loop in a few hundred milliseconds.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


class DummyAgent:
    """Trivial agent that marks the task DONE immediately."""

    def __init__(self, **_ignored) -> None:
        self.messages: list = []
        self.tools: list = []
        self._logger = None

    def reset(self, logger=None) -> None:
        self._logger = logger
        self.messages.clear()

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str]]:
        return "dummy agent: marking task DONE", ["DONE"]
