"""Shared base for the OpenAI agent — OpenAIBaseAgent.

NOT a registered agent_type. The shipped `openai_cuabash` agent
(`OpenAICUAToolsAgent` in agents/openai_cuabash.py) subclasses
`OpenAIBaseAgent` and overrides the tool list to the built-in `computer`
+ `shell` tools. The function-style `BASH_TOOL` defined here is the base
default and is not used by any shipped agent.

Provides the OpenAI Responses-API request loop, pyautogui action
conversion, reasoning/usage tracking, retry, and the `OPERATOR_PROMPT`
primer, shared by the OpenAI CUA agent.

Based on the OSWorld OpenAI agent pattern. Features that affect
model quality:
- `reasoning.effort` + `reasoning.summary` enable extended thinking.
- `temperature` / `top_p` / `max_output_tokens` set sampling explicitly.
- Server-side prompt caching via `previous_response_id` (automatic).
- A `previous_actions_str` text log is injected on every user turn so the
  conversation context grows monotonically even when screenshots are
  pixel-identical — this prevents a temp-0 loop on weaker models.
- An `OPERATOR_PROMPT` primes the model with the sudo password + task hints.
- Retries with exponential-ish backoff on transient API errors.
"""

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import openai

from agents.base import BaseAgent, encode_image

# Infeasibility token set — matches the canonical OSWorld OpenAI agent.
# "cannot be completed" was removed: it's a common benign phrase in progress
# reports ("Step 3 cannot be completed without first opening the file") and
# caused false-positive FAIL signals. "[INFEASIBLE]" is kept as a deliberate
# sentinel the prompt can request.
INFEASIBLE_TOKENS = [
    "[INFEASIBLE]",
    "infeasible",
    "unfeasible",
    "impossible",
    "cannot be done",
    "not feasible",
]

# Key mapping for `keypress` actions. GPT-5.4 emits keys like "arrowdown",
# "cmd", "esc", "pagedown" that pyautogui does NOT recognize verbatim. We
# translate them to pyautogui's canonical names before building the hotkey
# call. Mirrors the canonical OSWorld OpenAI agent key mapping.
_KEY_MAPPING = {
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "cmd": "command",
    "command": "command",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "meta": "win",
    "win": "win",
    "super": "win",
    "esc": "esc",
    "escape": "esc",
    "enter": "enter",
    "return": "enter",
    "space": "space",
    "tab": "tab",
    "backspace": "backspace",
    "del": "delete",
    "delete": "delete",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "home": "home",
    "end": "end",
    "capslock": "capslock",
    "shift": "shift",
}


def _map_key(key: str) -> str:
    """Normalize a GPT-5.4 key name to pyautogui's canonical form."""
    return _KEY_MAPPING.get((key or "").strip().lower(), key)


def _resolve_drag_points(dump: Dict[str, Any]) -> Optional[list]:
    """Resolve drag path from either `path: [...]` or `{from, to}` shapes."""
    if dump.get("path"):
        return dump["path"]
    if dump.get("from") and dump.get("to"):
        return [dump["from"], dump["to"]]
    return None


def _build_multiline_ascii_type_command(text: str) -> str:
    """Build pyautogui code to type multiline ASCII text, line by line."""
    commands = ["import pyautogui"]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line:
            commands.append(f"pyautogui.typewrite({repr(line)}, interval=0.03)")
        if index < len(lines) - 1:
            commands.append("pyautogui.press('enter')")
    return "\n".join(commands)


def _build_clipboard_paste_command(text: str) -> str:
    """Build pyautogui code that pastes `text` via clipboard (handles non-ASCII)."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return (
        "import base64, time, pyautogui, pyperclip\n"
        f"_text = base64.b64decode('{encoded}').decode('utf-8')\n"
        "pyperclip.copy(_text)\n"
        "time.sleep(0.1)\n"
        "pyautogui.hotkey('ctrl', 'v')\n"
        "time.sleep(0.1)"
    )

logger = logging.getLogger("mypcbench.agent.openai_base")


# Primer injected as a text block at the top of the FIRST user message.
# Based on the canonical OSWorld OPERATOR_PROMPT — tells the model
# the sudo password, the overall task framing, when to use bash vs. the
# computer tool, and the infeasibility protocol.
# OPERATOR_PROMPT + MyPCBench context is imported from the shared prompts
# module so the OpenAI agent (openai_cuabash) and its base
# see the same text. The formatted string carries the
# persona, the 17-app catalog, Firefox preference, and the MyPCBench
# completion discipline — drops the original OSWorld web-agent "stick to the website"
# directives that don't apply to this CUA benchmark.
from agents.prompts import OPENAI_CUA_OPERATOR_PROMPT as _OP_BASE, MYPCBENCH_CONTEXT as _CTX

OPERATOR_PROMPT = _OP_BASE + "\n\n" + _CTX


BASH_TOOL = {
    "type": "function",
    "name": "bash",
    "description": (
        "Execute a shell command on the Linux VM and return stdout/stderr."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute (e.g., 'python3 script.py', 'ls -la', 'pip install pkg')",
            }
        },
        "required": ["command"],
    },
}

COMPUTER_TOOL = {"type": "computer"}


class OpenAIBaseAgent(BaseAgent):
    """OpenAI agent with computer + bash function tools via Responses API.

    The agent can see the screen (computer tool) and run commands (bash tool).
    For code-heavy tasks it primarily uses bash; for GUI tasks it uses the
    computer tool.
    """

    def __init__(
        self,
        model: str = "gpt-5.4",  # was "gpt-5.4-mini" — OSWorld uses full "gpt-5.4" by default
        screen_size: tuple = (1920, 1080),
        client_password: str = "password",
        enable_computer: bool = True,
        env=None,
        max_output_tokens: int = 1500,  # was 4096 — OSWorld uses 1500
        # Temperature/top_p default to None — GPT-5.x reasoning models
        # REJECT non-default sampling (`temperature != 1`) with a 400 error,
        # and the legacy `computer-use-preview` tolerates defaults too. The
        # old defaults (0.5 / 0.9) broke every GPT-5.4 call. The canonical
        # OSWorld OpenAI agent never sends these fields.
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: str = os.environ.get("MYPCBENCH_OPENAI_REASONING_EFFORT", "high"),
        reasoning_summary: str = "auto",
        api_retry_times: int = 5,
    ):
        super().__init__(model, screen_size, client_password)
        self.env = env
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.reasoning_effort = reasoning_effort
        self.reasoning_summary = reasoning_summary
        self.api_retry_times = max(1, int(api_retry_times))

        # Token/cost tracking. Populated from `response.usage` after every
        # successful API call. Runners can harvest `last_usage` per step and
        # `total_usage` at end-of-run to compute cost. Includes `cached_tokens`
        # so we can verify server-side prompt caching is working.
        self.last_usage: Dict[str, int] = {}
        self.total_usage: Dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }

        self.client = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        )

        self.tools = []
        if enable_computer:
            self.tools.append(COMPUTER_TOOL)
        self.tools.append(BASH_TOOL)

        # Per-instance operator prompt. A subclass can override it in its
        # own __init__ to swap in a GUI-only primer that
        # doesn't mention bash. Kept as an instance attribute so the base
        # class doesn't need any subclass-specific knowledge.
        self._operator_prompt = OPERATOR_PROMPT

        self.previous_response_id: Optional[str] = None
        self.pending_items: list = []
        self._last_computer_call_id: Optional[str] = None
        # Safety checks pending from the most recent computer_call — must be
        # echoed back in `acknowledged_safety_checks` on the corresponding
        # computer_call_output, otherwise OpenAI may silently drop the action.
        self._pending_safety_checks: list = []
        # Running log of prior actions, mirrors the OSWorld Qwen agent
        # `previous_actions_str`. Appended as a text block to the FIRST
        # user message each turn so the context grows monotonically.
        self.actions_log: List[str] = []

    def reset(self, logger=None):
        super().reset(logger)
        self.previous_response_id = None
        self.pending_items.clear()
        self._last_computer_call_id = None
        self._pending_safety_checks = []
        self.actions_log.clear()

    def _previous_actions_block(self) -> str:
        if not self.actions_log:
            return "None"
        return "\n".join(
            f"Step {i + 1}: {act}" for i, act in enumerate(self.actions_log)
        )

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str]]:
        """Predict action(s) from instruction and observation.

        Returns (response_text, actions_list).
        Actions can be pyautogui code strings (from computer tool) or empty
        (when bash tool was used — results fed back internally).
        """
        screenshot = obs.get("screenshot")

        # Build input
        is_first_call = not self.previous_response_id and not self.pending_items
        if is_first_call:
            # First call: send primer + instruction + screenshot + prior-actions log.
            # Text FIRST, image SECOND — matches the canonical OSWorld
            # recommended multimodal ordering.
            from datetime import datetime as _dt
            primer = self._operator_prompt.format(
                CLIENT_PASSWORD=self.client_password,
                CURRENT_DATE=_dt.today().strftime("%A, %B %d, %Y"),
            )
            task_block = (
                f"Task: {instruction}\n\n"
                f"Previous actions:\n{self._previous_actions_block()}"
            )
            content: list = [
                {"type": "input_text", "text": f"{primer}\n\n{task_block}"},
            ]
            if screenshot:
                # NOTE: do NOT set `detail: "original"` on input_image blocks.
                # That value is only valid on `computer_screenshot` output
                # types. Setting it here is undocumented behavior on the
                # Responses API (may silently downsample or 400). Mirrors
                # the canonical OSWorld OpenAI agent which omits `detail` here.
                content.append({
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encode_image(screenshot)}",
                })
            input_items = [{"role": "user", "content": content}]
        else:
            # Subsequent call. We must include BOTH:
            #  (a) any pending function_call_output items (bash results) AND
            #  (b) a computer_call_output with the POST-action screenshot if
            #      the last turn's assistant emitted a computer_call.
            # Previously `elif self.pending_items` short-circuited and dropped
            # the computer screenshot entirely when bash + computer were used
            # in the same turn. Mirrors the canonical OSWorld OpenAI agent which
            # always appends the post-action computer_call_output.
            input_items = list(self.pending_items)
            if screenshot and self._last_computer_call_id:
                output_item: Dict[str, Any] = {
                    "type": "computer_call_output",
                    "call_id": self._last_computer_call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": f"data:image/png;base64,{encode_image(screenshot)}",
                        "detail": "original",
                    },
                }
                if self._pending_safety_checks:
                    output_item["acknowledged_safety_checks"] = self._pending_safety_checks
                    self._pending_safety_checks = []
                input_items.append(output_item)
                # Consume _last_computer_call_id so repeated empty turns
                # don't re-send the same screenshot under a stale call_id.
                self._last_computer_call_id = None

        self.pending_items.clear()

        # If the last turn was pure text (no tool_use, no pending outputs) and
        # there's no computer_call to merge, synthesize a "continue" user turn
        # with the current screenshot so the loop can make progress instead of
        # silently no-opping. Mirrors the canonical OSWorld OpenAI agent.
        if not input_items:
            continue_content: list = [
                {
                    "type": "input_text",
                    "text": (
                        "Continue — take the next action toward completing "
                        "the task. If you already have the answer, state it "
                        "clearly and then emit ```DONE```."
                    ),
                },
            ]
            if screenshot:
                continue_content.append({
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encode_image(screenshot)}",
                })
            input_items = [{"role": "user", "content": continue_content}]

        # Build request
        request: Dict = {
            "model": self.model,
            "input": input_items,
            "tools": self.tools,
            "truncation": "auto",
            "max_output_tokens": self.max_output_tokens,
        }
        # Reasoning / extended thinking. GPT-5.x Responses API accepts
        # reasoning.effort ∈ {"low","medium","high"} (and "xhigh" for 5.4).
        # Non-reasoning models (gpt-4.1-mini, gpt-4o, gpt-4o-mini) REJECT
        # the `reasoning` param with a 400 error, so gate on model family.
        # `summary: "auto"` asks the server to emit a short chain-of-thought
        # summary block in the output.
        is_reasoning_model = (
            self.model.startswith("gpt-5")
            or self.model.startswith("o1")
            or self.model.startswith("o3")
            or "gpt-5.4" in self.model
        )
        if self.reasoning_effort and is_reasoning_model:
            request["reasoning"] = {
                "effort": self.reasoning_effort,
                "summary": self.reasoning_summary,
            }
        # Sampling — GPT-5.x reasoning models REJECT `temperature != 1`
        # and `top_p != 1` with a 400 error. We only pass these fields when
        # the user explicitly sets them AND the model is NOT a reasoning
        # variant. The canonical OSWorld OpenAI agent never sends
        # them at all for GPT-5.4.
        if not is_reasoning_model:
            if self.temperature is not None:
                request["temperature"] = self.temperature
            if self.top_p is not None:
                request["top_p"] = self.top_p
        if self.previous_response_id:
            request["previous_response_id"] = self.previous_response_id

        logger.debug(
            "API call: prev_id=%s, %d input items, types=%s",
            self.previous_response_id[:12] if self.previous_response_id else "None",
            len(input_items),
            [i.get("type") or i.get("role", "?") for i in input_items],
        )

        # Retry loop with exponential-ish backoff on transient errors.
        # Mirrors the canonical OSWorld `_create_response` retry pattern.
        response = None
        last_error: Optional[BaseException] = None
        for attempt in range(self.api_retry_times):
            try:
                response = self.client.responses.create(**request)
                self.previous_response_id = response.id
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    "OpenAI API error (attempt %d/%d): %s",
                    attempt + 1, self.api_retry_times, str(e)[:200],
                )
                if attempt < self.api_retry_times - 1:
                    time.sleep(min(5.0, (attempt + 1) * 2.0))
        if response is None:
            logger.error("OpenAI API exhausted retries: %s", last_error)
            self.actions_log.append(f"(api error: {str(last_error)[:80]})")
            return f"(openai api error: {last_error})", []

        # Process response
        actions = []
        response_text = ""
        self._last_computer_call_id = None
        step_summary_parts: List[str] = []

        # Keep message text and reasoning text in separate buffers so the
        # control-flow scans (DONE/FAIL/infeasibility) only run on the
        # model's actual user-visible message, not on the reasoning summary.
        # A reasoning block that mentions "it's almost impossible to know"
        # would otherwise trip the infeasibility scan and mis-FAIL a
        # legitimate trajectory. The canonical OSWorld OpenAI agent inspects
        # only `_message_text` for the FAIL check.
        message_text_parts: List[str] = []
        reasoning_text_parts: List[str] = []
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                # Responses API wraps model-visible text in `message` items
                # whose `content` is a list of parts. The actual text lives
                # in `output_text` parts. Collect into a list and join with
                # newlines.
                content = getattr(item, "content", None) or []
                for part in content:
                    if getattr(part, "type", None) == "output_text":
                        txt = getattr(part, "text", "") or ""
                        if txt:
                            message_text_parts.append(txt)

            elif item_type == "reasoning":
                # Capture reasoning summaries separately so they appear in
                # trajectory logs but are NOT scanned for DONE/FAIL/INFEASIBLE.
                summary = getattr(item, "summary", None)
                if isinstance(summary, list):
                    for block in summary:
                        text = getattr(block, "text", None) or (
                            block.get("text") if isinstance(block, dict) else None
                        )
                        if text:
                            reasoning_text_parts.append(text)

            elif item_type == "function_call":
                fn_name = item.name
                # Mini occasionally emits malformed JSON in `arguments`
                # (observed: "Unterminated string starting at: line 1 column
                # 12 (char 11)"). Don't kill the task on a single bad turn —
                # log, treat the call as a no-op, and let the loop continue.
                try:
                    fn_args = (
                        json.loads(item.arguments)
                        if isinstance(item.arguments, str)
                        else (item.arguments or {})
                    )
                except json.JSONDecodeError as _je:
                    logger.warning(
                        "function_call %s: malformed JSON arguments (%s); "
                        "skipping this call, continuing task.",
                        fn_name, _je,
                    )
                    self.pending_items.append({
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": f"Error: tool arguments could not be parsed ({_je}). Try again.",
                    })
                    continue

                if fn_name == "bash":
                    command = fn_args.get("command", "")
                    result_text = self._execute_bash(command)
                    self.pending_items.append({
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": result_text,
                    })
                    logger.info("  bash: %s -> %s", command[:100], result_text[:200])
                    step_summary_parts.append(f"bash: {command[:80]}")

            elif item_type == "computer_call":
                raw_actions = item.actions or ([item.action] if item.action else [])
                self._last_computer_call_id = item.call_id

                # Capture pending_safety_checks so we can echo them back on
                # the corresponding computer_call_output — failing to do so
                # causes OpenAI to silently drop actions or route them
                # through additional safety gates.
                raw_checks = getattr(item, "pending_safety_checks", None) or []
                pending_checks = []
                for chk in raw_checks:
                    if hasattr(chk, "model_dump"):
                        pending_checks.append(chk.model_dump())
                    elif isinstance(chk, dict):
                        pending_checks.append(chk)
                self._pending_safety_checks = pending_checks

                # The canonical OSWorld OpenAI agent treats unsupported actions as
                # batch-level failures: if ANY action in the batch returns
                # None, the WHOLE batch is rejected and NONE of its actions
                # execute. Track with a flag + `batch_start` and truncate.
                batch_start = len(actions)
                batch_unsupported = False
                for a in raw_actions:
                    a_type = getattr(a, "type", "?")
                    code = self._action_to_pyautogui(a)
                    if code is None:
                        logger.warning(
                            "OpenAI emitted unsupported computer action: %s",
                            a_type,
                        )
                        step_summary_parts.append(
                            f"computer.{a_type} (UNSUPPORTED)"
                        )
                        batch_unsupported = True
                        break  # abort on first unsupported (canonical OSWorld behavior)
                    if code:
                        actions.append(code)
                    step_summary_parts.append(f"computer.{a_type}")
                if batch_unsupported:
                    # Drop any actions we appended from this batch + clear
                    # the computer_call state so the subsequent-call merge
                    # doesn't echo a stale call_id.
                    del actions[batch_start:]
                    self._last_computer_call_id = None
                    self._pending_safety_checks = []
                    # NOTE: No in-loop `computer_call_output` emission here.
                    # The subsequent-call merge block at the top of predict()
                    # handles ALL computer actions (including `screenshot`)
                    # uniformly by emitting a `computer_call_output` with the
                    # POST-action screenshot on the next turn, keyed on
                    # `_last_computer_call_id`. Emitting one here would
                    # double-send the call_id with the stale pre-action
                    # screenshot. Mirrors the canonical OSWorld OpenAI agent which
                    # also defers all output emission to the next turn.

        # Assemble the final text buffers. `message_text` is the user-visible
        # model output used for control-flow scans. `response_text` combines
        # both message and reasoning for trajectory logging and is what the
        # runner sees as the agent's response.
        message_text = "\n".join(message_text_parts)
        if reasoning_text_parts:
            reasoning_text = "\n".join(
                f"[reasoning] {t}" for t in reasoning_text_parts
            )
            response_text = (
                message_text + "\n" + reasoning_text if message_text
                else reasoning_text
            )
        else:
            response_text = message_text

        # Skip control-flow scans (DONE/FAIL/infeasibility) when this turn
        # queued any tool outputs — we want the runner to send the tool
        # results back first instead of terminating mid-batch. Mirrors
        # the canonical OSWorld OpenAI agent which only scans message text on
        # turns without queued tool calls.
        scan_terminal = message_text and not self.pending_items

        if scan_terminal:
            import re as _re
            # Strict DONE/FAIL sentinel detection on message_text ONLY.
            if "DONE" not in actions and (
                "```DONE```" in message_text
                or _re.search(r"(?:^|\n)\s*DONE\s*(?:$|\n)", message_text)
            ):
                actions.append("DONE")
                step_summary_parts.append("DONE")
            if "FAIL" not in actions and (
                "```FAIL```" in message_text
                or _re.search(r"(?:^|\n)\s*FAIL\s*(?:$|\n)", message_text)
            ):
                actions.append("FAIL")
                step_summary_parts.append("FAIL")

        if scan_terminal and "FAIL" not in actions:
            # Word-boundary infeasibility scan on message_text ONLY (not on
            # reasoning text, which often contains tentative phrases like
            # "it's almost impossible to be sure" that would false-FAIL).
            import re as _re
            lower_text = message_text.lower()
            infeasible_hit = False
            for tok in INFEASIBLE_TOKENS:
                tok_l = tok.lower()
                if tok.startswith("[") and tok.endswith("]"):
                    if tok_l in lower_text:
                        infeasible_hit = True
                        break
                else:
                    if _re.search(r"\b" + _re.escape(tok_l) + r"\b", lower_text):
                        infeasible_hit = True
                        break
            if infeasible_hit:
                actions.append("FAIL")
                step_summary_parts.append("FAIL (infeasible)")

        # Token/cost tracking — pull usage from response.usage for the
        # runner to harvest. Handles missing fields gracefully (some
        # non-reasoning models don't emit reasoning tokens).
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                }
                details = getattr(usage, "input_tokens_details", None)
                if details is not None:
                    self.last_usage["cached_tokens"] = (
                        getattr(details, "cached_tokens", 0) or 0
                    )
                reasoning_details = getattr(usage, "output_tokens_details", None)
                if reasoning_details is not None:
                    self.last_usage["reasoning_tokens"] = (
                        getattr(reasoning_details, "reasoning_tokens", 0) or 0
                    )
                self.total_usage["input_tokens"] += self.last_usage["input_tokens"]
                self.total_usage["output_tokens"] += self.last_usage["output_tokens"]
                self.total_usage["total_tokens"] += self.last_usage["total_tokens"]
                self.total_usage["cached_tokens"] += self.last_usage.get("cached_tokens", 0)
                self.total_usage["reasoning_tokens"] += self.last_usage.get("reasoning_tokens", 0)
        except Exception as _usage_err:
            logger.debug("Failed to parse response.usage: %s", _usage_err)

        # Append a human-readable summary to the prior-actions log so the
        # next turn's user message contains a monotonically-growing history.
        if step_summary_parts:
            self.actions_log.append(" ; ".join(step_summary_parts))
        else:
            self.actions_log.append("(no tool call)")

        return response_text, actions

    def _execute_bash(self, command: str) -> str:
        """Execute a bash command in the container via Control API."""
        MAX_OUTPUT = 10000

        if not command:
            return "(no command)"
        if not self.env:
            return "Error: No environment connected."

        try:
            result = self.env._execute_command(command, shell=True)
            output = result.get("output", "")
            error = result.get("error", "")
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
                text = text[:MAX_OUTPUT] + f"\n... (truncated, {len(text)} total chars)"
            return text
        except Exception as e:
            return f"Error: {e}"

    def _action_to_pyautogui(self, action) -> str:
        """Convert OpenAI computer action to pyautogui code.

        Based on the canonical OSWorld OpenAI agent pattern. Key behaviors:
        - click: split moveTo + click (more reliable with PAUSE=0)
        - type: interval=0.03 (matches OSWorld), multiline splits per-line
        - keypress: runs keys through _KEY_MAPPING (arrow→down, cmd→command)
        - scroll: handles scroll_x (hscroll) AND scroll_y (scroll), both sign-flipped
        - drag: iterates ALL path points via dragTo (canonical OSWorld shape)
        - wait: default 1000ms (not 2000ms)
        """
        if hasattr(action, "type"):
            a_type = action.type
            dump = action.model_dump()
        else:
            a_type = action.get("type")
            dump = dict(action)

        if a_type == "click":
            x, y = dump.get("x", 0), dump.get("y", 0)
            button = dump.get("button", "left")
            # Validate button — pyautogui only accepts left/middle/right.
            # The canonical OSWorld OpenAI agent does the same guard.
            if button not in ("left", "middle", "right"):
                button = "left"
            # moveTo FIRST, then click — more reliable when pyautogui.PAUSE=0
            # because the OS needs a moment to register the cursor move before
            # the click event. Mirrors the canonical OSWorld OpenAI agent.
            return (
                f"pyautogui.moveTo({x}, {y})\n"
                f"pyautogui.click(button='{button}')"
            )
        elif a_type == "double_click":
            x, y = dump.get("x", 0), dump.get("y", 0)
            return (
                f"pyautogui.moveTo({x}, {y})\n"
                f"pyautogui.doubleClick()"
            )
        elif a_type == "type":
            text = dump.get("text", "")
            if text == "":
                return "import time\ntime.sleep(0.1)"
            # Non-ASCII — use clipboard paste (handles unicode reliably)
            if not text.isascii():
                return _build_clipboard_paste_command(text)
            # Multiline ASCII — split per-line typewrite + press enter
            if "\n" in text:
                return _build_multiline_ascii_type_command(text)
            return f"pyautogui.typewrite({repr(text)}, interval=0.03)"
        elif a_type == "keypress":
            raw_keys = dump.get("keys", []) or []
            # Map each key through _KEY_MAPPING so "arrowdown" → "down",
            # "cmd" → "command", etc. pyautogui does NOT recognize the raw
            # GPT-5.4 key names.
            keys = [_map_key(k) for k in raw_keys]
            if len(keys) == 1:
                return f"pyautogui.press({repr(keys[0])})"
            return f"pyautogui.hotkey({', '.join(repr(k) for k in keys)})"
        elif a_type == "scroll":
            # CUA convention: positive = down / right.
            # pyautogui convention: positive = up / left.
            # So we sign-flip both scroll_y and scroll_x.
            # Omit x/y kwargs entirely when the model didn't specify them —
            # defaulting to 0,0 scrolls at the top-left corner instead of
            # the cursor position. Mirrors the canonical OSWorld OpenAI agent.
            dy = dump.get("scroll_y", 0) or 0
            dx = dump.get("scroll_x", 0) or 0
            x = dump.get("x")
            y = dump.get("y")
            pos = f", x={x}, y={y}" if x is not None and y is not None else ""
            if dy:
                return f"pyautogui.scroll({-dy}{pos})"
            if dx:
                return f"pyautogui.hscroll({-dx}{pos})"
            return None  # both zero — treat as unsupported (canonical OSWorld returns None)
        elif a_type == "move":
            x, y = dump.get("x", 0), dump.get("y", 0)
            return f"pyautogui.moveTo({x}, {y})"
        elif a_type == "wait":
            # Extract wait duration from ms — default 1000ms (canonical OSWorld default).
            ms = dump.get("ms", 1000)
            try:
                secs = max(0.1, float(ms) / 1000.0)
            except (TypeError, ValueError):
                secs = 1.0
            return f"import time\ntime.sleep({secs})"
        elif a_type == "screenshot":
            # Return a 100ms no-op sleep (NOT empty string). The canonical
            # OSWorld OpenAI agent uses this pattern. An empty string would
            # cause the outer runner loop to bail out on "no action",
            # stalling the agent whenever the model explicitly requests a
            # screenshot. The actual screenshot is fetched by env._get_obs()
            # after the sleep and chained via `_last_computer_call_id` on
            # the next turn.
            return "import time\ntime.sleep(0.1)"
        elif a_type == "drag":
            # Walk EVERY point in the path via dragTo (absolute) — not just
            # start→end. Matters for curved drag paths. Mirrors the canonical
            # OSWorld OpenAI agent.
            path = _resolve_drag_points(dump)
            if not path or len(path) < 2:
                return None  # malformed drag — flag as unsupported
            def _xy(p):
                if isinstance(p, dict):
                    return p.get("x", 0), p.get("y", 0)
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    return p[0], p[1]
                return getattr(p, "x", 0), getattr(p, "y", 0)
            first_x, first_y = _xy(path[0])
            lines = [f"pyautogui.moveTo({first_x}, {first_y})"]
            for pt in path[1:]:
                px, py = _xy(pt)
                lines.append(
                    f"pyautogui.dragTo({px}, {py}, duration=0.2, button='left')"
                )
            return "\n".join(lines)
        # Fall-through: unknown action type. Return None so the computer_call
        # loop can tag it as UNSUPPORTED and log a warning.
        return None
