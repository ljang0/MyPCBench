"""OSWorld-parity Qwen3.5-VL agent — the paper Qwen agent.

Wraps the vendored `Qwen35VLAgent` (agents/vendored_paper_results/) verbatim
and exposes it via the MyPCBench agent contract. The vendored class itself is
NOT modified — this wrapper only adapts construction defaults and the
`reset()` signature so the MyPCBench runner can call it.

Two agent types are built from this module (see run_mypcbench.py):
- `qwen_cua` — computer only, defaults matching OSWorld's published
  Qwen3.5-VL args.json verbatim, for apples-to-apples paper comparison.
- `qwen_cuabash` — same, plus an in-VM bash tool (the paper main Qwen run).

OSWorld defaults (from OSWorld's published
runs_journeys_cua_qwen35_35b_round2/pyautogui/screenshot/
Qwen/Qwen3.5-35B-A3B/args.json):
  history_n=100, image_max=20, fold_size=10
  temperature=0.0, top_p=0.9, max_tokens=32768
  coord="relative", add_thought_prefix=False

Deviations (forced by MyPCBench runtime):
  - screen_size: passed through from runner (1280x800 in our VM) vs OSWorld
    1920x1080. Locked by the VM display, not a tunable.
  - max_tokens: OSWorld ships 32768 verbatim. Our local vLLM `max-model-len`
    is also 32768 — meaning a long-history prompt + 32768-token completion
    can exceed the model's context. Override to a smaller budget via
    MYPCBENCH_QWEN_MAX_TOKENS=4096 if vLLM rejects requests; the OSWorld
    default is preserved otherwise.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


def _extract_bash_command(response: str) -> Optional[str]:
    """Find a `<function=bash><parameter=command>...</parameter></function>`
    block inside any `<tool_call>...</tool_call>` and return the command.

    Returns None if no bash tool call is present. Mirrors the parsing
    convention of the vendored paper-results agent (which only handles
    `<function=computer_use>`).
    """
    for tc in re.finditer(r"<tool_call>(.*?)</tool_call>", response, re.DOTALL):
        body = tc.group(1)
        if "<function=bash>" not in body:
            continue
        m = re.search(r"<parameter=command>\s*(.*?)\s*</parameter>", body, re.DOTALL)
        if m:
            return m.group(1).strip()
    return None

# Pull the agent. Default is the vendored `paper-results` HEAD copy at
# `agent-harness/agents/vendored_paper_results/qwen35vl_agent.py` (kept
# in-tree so eval is reproducible without a working clone of the
# upstream OSWorld repo). Set `MYPCBENCH_QWEN_OSWORLD_SOURCE=external_osworld`
# to instead import from `$OSWORLD_ROOT/mm_agents/qwen35vl_agent.py` (an
# older OSWorld fork — predates thinking-mode / presence_penalty).
_AGENT_SOURCE = os.environ.get("MYPCBENCH_QWEN_OSWORLD_SOURCE", "vendored_paper_results")

if _AGENT_SOURCE == "external_osworld":
    _OSWORLD_ROOT = os.environ.get("OSWORLD_ROOT", os.path.expanduser("~/OSWorld"))
    if _OSWORLD_ROOT not in sys.path:
        sys.path.insert(0, _OSWORLD_ROOT)
    from mm_agents.qwen35vl_agent import Qwen35VLAgent  # noqa: E402
else:
    # Vendored copy of `origin/paper-results:osworld/mm_agents/qwen35vl_agent.py`.
    from agents.vendored_paper_results.qwen35vl_agent import Qwen35VLAgent  # noqa: E402

logger = logging.getLogger("mypcbench.agent.qwen_cua")


# Bash tool description appended to the system message for the bash variant.
# The model sees this as part of its tool surface; we post-process responses
# to detect bash actions and execute them in the VM.
_BASH_TOOL_DESCRIPTION = """
## Additional tool: bash

You have access to a bash tool for interacting with the Linux desktop's shell.
Invoke it via the same XML format as `computer_use`, with `function=bash`:

<tool_call>
<function=bash>
<parameter=command>your shell command here</parameter>
</function>
</tool_call>

The command runs in the VM as user `user` (sudo password: {CLIENT_PASSWORD}).
The stdout / stderr will appear in the next user turn wrapped in
<tool_response>...</tool_response>. Use bash for read-only data work —
file inspection (`ls`, `cat`, `find`), querying local SQLite DBs,
parsing or computing over text. Use the GUI tool to actually perform
whatever the user asked you to do in the visible environment; don't
substitute shell exploration for visible action.
""".strip()


class _Qwen35VLPatched(Qwen35VLAgent):
    """Vendored paper-results agent + (1) MYPCBENCH_CONTEXT injected into the
    system message once per call, and (2) optional bash tool description
    appended to the system message.

    The vendored class itself is unchanged. We override only `call_llm` so
    the request-shaping logic (sampling kwargs, retry loop, reasoning parse)
    is inherited verbatim. Context appears once per LLM call inside the
    system role, exactly matching how the other paper agents inject it.
    """

    _mypcbench_context: str = ""
    _bash_tool_description: str = ""

    def call_llm(self, payload: Dict, model: str) -> str:  # type: ignore[override]
        msgs = payload.get("messages") or []
        addendum_parts: List[str] = []
        if self._mypcbench_context:
            addendum_parts.append(self._mypcbench_context)
        if self._bash_tool_description:
            addendum_parts.append(self._bash_tool_description)
        if msgs and addendum_parts and msgs[0].get("role") == "system":
            addendum = "\n\n" + "\n\n".join(addendum_parts)
            content = msgs[0].get("content")
            if isinstance(content, list):
                # Append as a new text block so we don't mutate the existing
                # tools_def JSON that lives inside the first text part.
                msgs[0]["content"] = content + [
                    {"type": "text", "text": addendum}
                ]
            elif isinstance(content, str):
                msgs[0]["content"] = content + addendum
        return super().call_llm(payload, model)


class QwenOSWorldAgent:
    """Thin shim over OSWorld's `Qwen35VLAgent` — zero behavioural changes.

    Exposes `predict(instruction, obs)` and `reset(logger=None)` so the
    MyPCBench `run_single_example()` loop can drive it identically to
    the other paper agents.

    The wrapped agent's `predict()` already returns
    `(response_text, pyautogui_code_list)` where `pyautogui_code_list`
    contains pyautogui call strings plus the literal "DONE" / "WAIT"
    sentinels — exactly what `env.step()` expects. No mapping needed.
    """

    def __init__(
        self,
        model: str = "Qwen/Qwen3.5-35B-A3B",
        screen_size: tuple = (1280, 800),
        client_password: str = "password",
        # OSWorld defaults from args.json — do not override unless an env
        # var explicitly requests it. Kept as ctor kwargs so unit tests can
        # construct with custom values.
        history_n: int = 100,
        image_max: int = 20,
        fold_size: int = 10,
        temperature: float = 0.0,
        top_p: float = 0.9,
        max_tokens: int = 32768,
        coordinate_type: str = "relative",
        add_thought_prefix: bool = False,
        # Paper-results HEAD adds these. Defaults match the agent ctor on
        # `origin/paper-results` (commit 290a4ea); env overrides let
        # parity-eval runs flip them on without code changes.
        presence_penalty: float = 1.5,
        top_k: int = 20,
        min_p: float = 0.0,
        repetition_penalty: float = 1.0,
        enable_thinking: bool = True,
        # When True, advertise a `bash` tool in the system message and
        # post-process responses to detect/execute bash commands. Required
        # `env` reference for bash execution.
        enable_bash: bool = False,
        env: Any = None,
        **_unused: Any,
    ):
        self.model = model
        self.screen_width, self.screen_height = screen_size
        self.client_password = client_password
        # `env` is unused by the computer-only OSWorld agent (no bash,
        # no in-VM screenshot) — capture for parity but ignore.
        self._env = env

        # All ctor kwargs accept env-var overrides. Lets the launcher pin
        # paper-results-parity values without touching code, mirroring how
        # we already handle MYPCBENCH_QWEN_MAX_TOKENS.
        max_tokens_eff = int(os.environ.get("MYPCBENCH_QWEN_MAX_TOKENS", str(max_tokens)))
        history_n_eff = int(os.environ.get("MYPCBENCH_QWEN_HISTORY_N", str(history_n)))
        image_max_eff = int(os.environ.get("MYPCBENCH_QWEN_IMAGE_MAX", str(image_max)))
        fold_size_eff = int(os.environ.get("MYPCBENCH_QWEN_FOLD_SIZE", str(fold_size)))
        temperature_eff = float(os.environ.get("MYPCBENCH_QWEN_TEMPERATURE", str(temperature)))
        top_p_eff = float(os.environ.get("MYPCBENCH_QWEN_TOP_P", str(top_p)))
        presence_penalty_eff = float(os.environ.get("MYPCBENCH_QWEN_PRESENCE_PENALTY", str(presence_penalty)))
        top_k_eff = int(os.environ.get("MYPCBENCH_QWEN_TOP_K", str(top_k)))
        min_p_eff = float(os.environ.get("MYPCBENCH_QWEN_MIN_P", str(min_p)))
        repetition_penalty_eff = float(os.environ.get("MYPCBENCH_QWEN_REPETITION_PENALTY", str(repetition_penalty)))
        enable_thinking_eff = os.environ.get(
            "MYPCBENCH_QWEN_ENABLE_THINKING", "1" if enable_thinking else "0"
        ).lower() not in ("0", "false", "no", "off")
        # Vendored paper-results agent's ctor accepts `max_pixels` (and
        # `min_pixels`); the older OSWorld fork does not. Forward the
        # env override only when the wrapped class accepts it.
        max_pixels_env = os.environ.get("MYPCBENCH_QWEN_MAX_PIXELS")
        max_pixels_eff = int(max_pixels_env) if max_pixels_env else None
        min_pixels_env = os.environ.get("MYPCBENCH_QWEN_MIN_PIXELS")
        min_pixels_eff = int(min_pixels_env) if min_pixels_env else None
        # preserve_reasoning_content keeps the model's <think> block in the
        # rendered chat history (vendored agent default is False, which strips
        # it). Required for Qwen3.5 thinking models so reasoning state carries
        # forward across turns. Set MYPCBENCH_QWEN_PRESERVE_REASONING=1 to enable.
        preserve_reasoning_env = os.environ.get("MYPCBENCH_QWEN_PRESERVE_REASONING")
        preserve_reasoning_eff = (
            preserve_reasoning_env.lower() not in ("0", "false", "no", "off")
            if preserve_reasoning_env is not None
            else None
        )

        # The vendored paper-results agent accepts presence_penalty + top_k
        # + thinking kwargs. The older external OSWorld fork doesn't —
        # forward only the kwargs that exist there to keep both code paths
        # working without per-source branching everywhere else in the shim.
        agent_kwargs: Dict[str, Any] = dict(
            platform="ubuntu",
            model=model,
            max_tokens=max_tokens_eff,
            top_p=top_p_eff,
            temperature=temperature_eff,
            action_space="pyautogui",
            observation_type="screenshot",
            history_n=history_n_eff,
            add_thought_prefix=add_thought_prefix,
            coordinate_type=coordinate_type,
            api_backend="openai",
            image_max=image_max_eff,
            fold_size=fold_size_eff,
        )
        # Probe the wrapped class signature; only pass the new kwargs if
        # the underlying class accepts them.
        import inspect
        _accepted = set(inspect.signature(Qwen35VLAgent.__init__).parameters)
        for name, val in (
            ("presence_penalty", presence_penalty_eff),
            ("top_k", top_k_eff),
            ("min_p", min_p_eff),
            ("repetition_penalty", repetition_penalty_eff),
            ("enable_thinking", enable_thinking_eff),
            ("max_pixels", max_pixels_eff),
            ("min_pixels", min_pixels_eff),
            ("preserve_reasoning_content", preserve_reasoning_eff),
        ):
            if name in _accepted and val is not None:
                agent_kwargs[name] = val

        # Bash mode is independent from context injection. Both flags
        # accept env overrides for run-time flipping without code changes.
        # Resolved BEFORE context build so the context can be capability-aware.
        self._enable_bash = (
            enable_bash
            or os.environ.get("MYPCBENCH_QWEN_OSWORLD_ENABLE_BASH", "0").lower()
            not in ("0", "false", "no", "off")
        )

        # Build MYPCBENCH_CONTEXT block once, like the other paper
        # agents — context lives in the system message, never in user
        # turns. Toggle via env:
        #   MYPCBENCH_QWEN_OSWORLD_INJECT_CONTEXT=0  → strict paper parity (no project context)
        #   MYPCBENCH_QWEN_OSWORLD_INJECT_CONTEXT=1  → with project context (default)
        # The context is rendered with `has_bash=self._enable_bash` so the
        # no-bash variant doesn't get told it has shell/CLI access (which
        # confuses smaller models like 9B into typing bash into the GUI).
        from datetime import datetime as _dt
        inject_ctx_env = os.environ.get(
            "MYPCBENCH_QWEN_OSWORLD_INJECT_CONTEXT", "1"
        ).lower() not in ("0", "false", "no", "off")
        mypcbench_context = ""
        if inject_ctx_env:
            try:
                from agents.prompts import build_mypcbench_context
                mypcbench_context = build_mypcbench_context(
                    has_bash=self._enable_bash
                ).format(
                    CLIENT_PASSWORD=client_password,
                    CURRENT_DATE=_dt.today().strftime("%A, %B %d, %Y"),
                )
            except Exception as e:
                logger.warning("MYPCBENCH_CONTEXT inject failed (continuing without): %s", e)
        bash_tool_desc = ""
        if self._enable_bash:
            if env is None:
                logger.warning(
                    "enable_bash=True but env=None — bash tool will be advertised but commands won't execute."
                )
            bash_tool_desc = _BASH_TOOL_DESCRIPTION.format(
                CLIENT_PASSWORD=client_password
            )

        # Use the patched class so call_llm injects context + bash desc
        # into the system message of every API request (once per call).
        self._inner = _Qwen35VLPatched(**agent_kwargs)
        self._inner._mypcbench_context = mypcbench_context
        self._inner._bash_tool_description = bash_tool_desc

        # Pending bash result — populated when the model emits a bash
        # tool call; injected as a <tool_response> at the head of the
        # next turn's instruction (see _execute_bash below).
        self._pending_bash_result: str = ""

        # Snapshot construction settings for trajectory logs / debugging.
        self.screen_size = screen_size
        self.history_n = history_n_eff
        self.image_max = image_max_eff
        self.fold_size = fold_size_eff
        self.coordinate_type = coordinate_type
        self.max_tokens = max_tokens_eff
        self.temperature = temperature_eff
        self.top_p = top_p_eff
        self.presence_penalty = presence_penalty_eff
        self.enable_thinking = enable_thinking_eff
        self.agent_source = _AGENT_SOURCE
        self.inject_mypcbench_context = bool(mypcbench_context)
        self.enable_bash = self._enable_bash

        # `messages` is read by run_mypcbench.py post-task to persist the
        # full conversation. The OSWorld agent doesn't expose one, so we
        # build a minimal placeholder from its accumulated responses.
        self.messages: List[Dict] = []

    def reset(self, logger: Any = None) -> None:
        """Clear per-trajectory state. Forwarded to the wrapped agent.

        OSWorld's `Qwen35VLAgent.reset()` accepts a logger as a positional
        arg and stores it on a module-level global; we forward it through.
        """
        self._inner.reset(logger)
        self.messages = []
        self._pending_bash_result = ""

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str]]:
        """Forward to the vendored agent's predict.

        Context injection lives in `_inner.call_llm` (system-message
        mutation), so we don't touch `instruction` for that. We DO prepend
        any pending bash result (`<tool_response>...</tool_response>`)
        once. The vendored agent will
        bake that into the first user message of the next call's history
        window. After this turn, the pending result is cleared (the bash
        output appears in the model's context exactly once).
        """
        # Drain any staged bash result into this turn's instruction.
        # The vendored agent's `instruction_prompt` glues `instruction`
        # to its first user message (qwen35vl_agent.py:443-447), so the
        # tool_response is visible to the model alongside the new
        # screenshot for the post-bash turn.
        if self._pending_bash_result:
            staged = self._pending_bash_result
            self._pending_bash_result = ""  # clear so it appears once
            instruction = (
                f"<tool_response>\n{staged}\n</tool_response>\n\n{instruction}"
            )

        response, actions = self._inner.predict(instruction, obs)

        # Bash post-process: scan for `<function=bash><parameter=command>...`
        # in the model's response, execute via env, stage for next turn.
        if self._enable_bash and response:
            bash_cmd = _extract_bash_command(response)
            if bash_cmd:
                logger.info("qwen_osworld bash: %s", bash_cmd[:120])
                self._pending_bash_result = self._execute_bash(bash_cmd)
                # Don't pollute pyautogui actions list with bash; if the
                # agent issued ONLY a bash call (no computer_use), make
                # sure the runner doesn't try to dispatch DONE/FAIL based
                # on stale state. The runner handles empty actions cleanly.

        # Mirror the wrapped agent's running history into a flat list the
        # runner can dump as messages.json for inspection.
        self.messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            }
            for text in self._inner.responses
        ]
        return response, actions

    def _execute_bash(self, command: str) -> str:
        """Run a shell command in the VM via the env's _execute_command."""
        MAX_OUTPUT = 10000
        if not command:
            return "(no command)"
        if not self._env:
            return "Error: No environment connected (env=None)."
        try:
            result = self._env._execute_command(command, shell=True)
            output = result.get("output", "") or ""
            error = result.get("error", "") or ""
            rc = result.get("returncode", -1)
            parts = []
            if output:
                parts.append(output)
            if error:
                parts.append(f"stderr: {error}")
            if rc != 0:
                parts.append(f"(exit code {rc})")
            text = "\n".join(parts) if parts else "(no output)"
            if len(text) > MAX_OUTPUT:
                text = text[:MAX_OUTPUT] + "\n... (truncated)"
            return text
        except Exception as e:
            return f"Error: {e}"
