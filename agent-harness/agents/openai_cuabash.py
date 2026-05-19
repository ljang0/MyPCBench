"""OpenAI CUA agent — official documented tool surfaces only.

  OpenAICUAToolsAgent — the paper GPT agent. Tools: built-in `computer`
                        + built-in `shell` (environment: local). No
                        custom function tools. References:
                          https://developers.openai.com/api/docs/guides/tools-computer-use
                          https://developers.openai.com/api/docs/guides/tools-shell

Reuses OpenAIBaseAgent's pyautogui conversion, reasoning/usage code, and
the Responses API request loop. The generic `{"type":"function",
"name":"bash"}` from openai_bash.py is replaced by the built-in
`{"type":"shell","environment":{"type":"local"}}` — GPT-5.x is trained
against the `shell` type; `function`-typed bash is OOD. Shell emits
`shell_call` items with `action.commands` (a LIST); we reply with
`shell_call_output` carrying stdout/stderr/exit_code.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agents.openai_base import (
    INFEASIBLE_TOKENS,
    OPERATOR_PROMPT,
    OpenAIBaseAgent,
)
from agents.prompts import GUI_WORKFLOW_HINT

logger = logging.getLogger("mypcbench.agent.openai_cuabash")


SHELL_TOOL = {"type": "shell", "environment": {"type": "local"}}
COMPUTER_TOOL = {"type": "computer"}


def _build_primer(mode: str) -> str:
    """Swap the parent OPERATOR_PROMPT's tools line for the documented tool list,
    then append the GUI_WORKFLOW_HINT — added to this agent's primer ONLY,
    not to the shared OPENAI_CUA_OPERATOR_PROMPT, so the base prompt stays
    byte-identical for reproduction.
    """
    if mode == "cua_tools":
        tools_line = (
            "Your tools are `computer` (GUI mouse/keyboard) and `shell` "
            "(commands run on the Linux VM)."
        )
    else:
        raise ValueError(f"unknown mode {mode!r}")
    base = OPERATOR_PROMPT.replace(
        "Your tools are `computer` and `bash`.",
        tools_line,
    )
    # Append the dual-tool guidance — generic, no env/rubric/curl mentions.
    # Only this agent's primer gets this; the shared base OPERATOR_PROMPT
    # stays identical to the published-baseline run.
    return base + GUI_WORKFLOW_HINT


class _OpenAICUABase(OpenAIBaseAgent):
    """Shared scaffolding for the OpenAI CUA agent.

    Overrides the tools list to use built-in `shell` + `computer` and
    rewrites predict() to parse the documented response item types:
    shell_call, computer_call, message, reasoning.
    """

    _MODE: str = "cua_tools"

    def __init__(
        self,
        model: str = "gpt-5.4",
        screen_size: tuple = (1920, 1080),
        client_password: str = "password",
        enable_computer: bool = True,
        env=None,
        *,
        max_output_tokens: int = 1500,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        reasoning_effort: str = os.environ.get(
            "MYPCBENCH_OPENAI_REASONING_EFFORT", "high"
        ),
        reasoning_summary: str = "auto",
        api_retry_times: int = 5,
    ):
        # enable_computer=False is not a supported mode for this agent —
        # they are CUA-based by design. We still accept the parameter to be
        # compatible with the registry, but enforce True for the CUA path.
        super().__init__(
            model=model,
            screen_size=screen_size,
            client_password=client_password,
            enable_computer=True,
            env=env,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            api_retry_times=api_retry_times,
        )

        # Replace the parent's tools list (which had function-bash) with
        # the documented built-in computer + shell tools.
        self.tools = [COMPUTER_TOOL, SHELL_TOOL]

        # Override the primer so the model sees the documented tool names. The
        # parent OPERATOR_PROMPT text still describes the persona / 17-app
        # catalog / sudo password — only the "Your tools are …" line changes.
        self._operator_prompt = _build_primer(self._MODE)

    def _execute_shell_command(
        self,
        command: str,
        timeout_ms: Optional[int] = None,
        max_output_length: Optional[int] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Run a shell command, returning (stdout, stderr, outcome).

        outcome is the documented shell_call_output outcome shape:
          {"type": "exit", "exit_code": N}   — normal exit (any rc)
          {"type": "timeout"}                — command timed out

        Default routes through env._execute_command (VM control API). When
        env is None (smoke test on the harness host), shell out locally.
        """
        timeout_s = (timeout_ms / 1000.0) if timeout_ms else 120.0
        cap = max_output_length or 8192
        if not command:
            return "", "(empty command)", {"type": "exit", "exit_code": -1}
        if self.env is not None:
            try:
                result = self.env._execute_command(command, shell=True)
                stdout = (result.get("output") or "")[:cap]
                stderr = (result.get("error") or "")[:cap]
                rc = int(result.get("returncode", -1))
                return stdout, stderr, {"type": "exit", "exit_code": rc}
            except Exception as e:
                return "", f"Error: {e}", {"type": "exit", "exit_code": -1}
        import subprocess
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout_s,
            )
            return (
                proc.stdout[:cap],
                proc.stderr[:cap],
                {"type": "exit", "exit_code": proc.returncode},
            )
        except subprocess.TimeoutExpired:
            return "", f"(command timed out after {timeout_s:.0f}s)", {"type": "timeout"}
        except Exception as e:
            return "", f"Error: {e}", {"type": "exit", "exit_code": -1}

    def _dispatch_function_call(self, name: str, args: Dict[str, Any]) -> str:
        """Subclass hook: handle a `function_call` item and return the output string.

        Default returns an error — the base CUA agent declares no
        function tools, so any function_call is unexpected.
        """
        return json.dumps({"error": f"Unknown function: {name}"})

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str]]:
        from datetime import datetime as _dt
        import time as _time
        import re as _re

        from agents.base import encode_image

        screenshot = obs.get("screenshot")
        is_first_call = not self.previous_response_id and not self.pending_items

        if is_first_call:
            primer = self._operator_prompt.format(
                CLIENT_PASSWORD=self.client_password,
                CURRENT_DATE=_dt.today().strftime("%A, %B %d, %Y"),
            )
            task_block = (
                f"Task: {instruction}\n\n"
                f"Previous actions:\n{self._previous_actions_block()}"
            )
            content: List[Dict[str, Any]] = [
                {"type": "input_text", "text": f"{primer}\n\n{task_block}"},
            ]
            if screenshot:
                content.append({
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encode_image(screenshot)}",
                })
            input_items: List[Dict[str, Any]] = [{"role": "user", "content": content}]
        else:
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
                self._last_computer_call_id = None

        self.pending_items.clear()

        if not input_items:
            continue_content: List[Dict[str, Any]] = [
                {
                    "type": "input_text",
                    "text": (
                        "Continue — take the next action toward completing the "
                        "task. If you already have the answer, state it clearly "
                        "and then emit ```DONE```."
                    ),
                }
            ]
            if screenshot:
                continue_content.append({
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{encode_image(screenshot)}",
                })
            input_items = [{"role": "user", "content": continue_content}]

        request: Dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "tools": self.tools,
            "truncation": "auto",
            "max_output_tokens": self.max_output_tokens,
        }
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
        if not is_reasoning_model:
            if self.temperature is not None:
                request["temperature"] = self.temperature
            if self.top_p is not None:
                request["top_p"] = self.top_p
        if self.previous_response_id:
            request["previous_response_id"] = self.previous_response_id

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
                    attempt + 1, self.api_retry_times, str(e)[:300],
                )
                if attempt < self.api_retry_times - 1:
                    _time.sleep(min(5.0, (attempt + 1) * 2.0))
        if response is None:
            self.actions_log.append(f"(api error: {str(last_error)[:80]})")
            return f"(openai api error: {last_error})", []

        actions: List[str] = []
        self._last_computer_call_id = None
        step_summary_parts: List[str] = []
        message_text_parts: List[str] = []
        reasoning_text_parts: List[str] = []

        for item in response.output:
            item_type = getattr(item, "type", None)

            if item_type == "message":
                content = getattr(item, "content", None) or []
                for part in content:
                    if getattr(part, "type", None) == "output_text":
                        txt = getattr(part, "text", "") or ""
                        if txt:
                            message_text_parts.append(txt)

            elif item_type == "reasoning":
                summary = getattr(item, "summary", None)
                if isinstance(summary, list):
                    for block in summary:
                        text = getattr(block, "text", None) or (
                            block.get("text") if isinstance(block, dict) else None
                        )
                        if text:
                            reasoning_text_parts.append(text)

            elif item_type == "shell_call":
                # Per the shell tool docs, action.commands is a LIST.
                action = getattr(item, "action", None)
                commands: List[str] = []
                timeout_ms: Optional[int] = None
                max_output_length: Optional[int] = None
                if action is not None:
                    commands = list(getattr(action, "commands", None) or [])
                    timeout_ms = getattr(action, "timeout_ms", None)
                    max_output_length = getattr(action, "max_output_length", None)

                outputs = []
                for cmd in commands:
                    stdout, stderr, outcome = self._execute_shell_command(
                        cmd, timeout_ms=timeout_ms,
                        max_output_length=max_output_length,
                    )
                    outputs.append({
                        "stdout": stdout,
                        "stderr": stderr,
                        "outcome": outcome,
                    })
                    outcome_repr = (
                        f"exit={outcome.get('exit_code')}"
                        if outcome.get("type") == "exit"
                        else outcome.get("type")
                    )
                    logger.info("  shell: %s -> %s", cmd[:120], outcome_repr)
                    step_summary_parts.append(f"shell: {cmd[:60]}")

                self.pending_items.append({
                    "type": "shell_call_output",
                    "call_id": item.call_id,
                    "output": outputs,
                })

            elif item_type == "function_call":
                # No custom function tools in the paper CUA agent
                # (computer + built-in shell only); dispatch via hook.
                fn_name = getattr(item, "name", "?")
                raw_args = getattr(item, "arguments", "") or "{}"
                try:
                    fn_args = (
                        json.loads(raw_args)
                        if isinstance(raw_args, str) else (raw_args or {})
                    )
                except json.JSONDecodeError as e:
                    fn_args = {}
                    logger.warning(
                        "function_call %s: malformed arguments JSON (%s) — using {}",
                        fn_name, e,
                    )
                output_text = self._dispatch_function_call(fn_name, fn_args)
                self.pending_items.append({
                    "type": "function_call_output",
                    "call_id": getattr(item, "call_id", None),
                    "output": output_text,
                })
                logger.info("  fn: %s -> %s", fn_name, str(output_text)[:200])
                step_summary_parts.append(f"fn.{fn_name}")

            elif item_type == "computer_call":
                raw_actions = item.actions or ([item.action] if item.action else [])
                self._last_computer_call_id = item.call_id
                raw_checks = getattr(item, "pending_safety_checks", None) or []
                pending_checks = []
                for chk in raw_checks:
                    if hasattr(chk, "model_dump"):
                        pending_checks.append(chk.model_dump())
                    elif isinstance(chk, dict):
                        pending_checks.append(chk)
                self._pending_safety_checks = pending_checks
                batch_start = len(actions)
                batch_unsupported = False
                for a in raw_actions:
                    a_type = getattr(a, "type", "?")
                    code = self._action_to_pyautogui(a)
                    if code is None:
                        step_summary_parts.append(f"computer.{a_type}(UNSUPPORTED)")
                        batch_unsupported = True
                        break
                    if code:
                        actions.append(code)
                    step_summary_parts.append(f"computer.{a_type}")
                if batch_unsupported:
                    del actions[batch_start:]
                    self._last_computer_call_id = None
                    self._pending_safety_checks = []

        message_text = "\n".join(message_text_parts)
        if reasoning_text_parts:
            response_text = (
                message_text + "\n" + "\n".join(
                    f"[reasoning] {t}" for t in reasoning_text_parts
                )
            ) if message_text else "\n".join(
                f"[reasoning] {t}" for t in reasoning_text_parts
            )
        else:
            response_text = message_text

        scan_terminal = message_text and not self.pending_items
        if scan_terminal:
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
            if "FAIL" not in actions:
                lower_text = message_text.lower()
                for tok in INFEASIBLE_TOKENS:
                    tok_l = tok.lower()
                    if tok.startswith("[") and tok.endswith("]"):
                        if tok_l in lower_text:
                            actions.append("FAIL")
                            step_summary_parts.append("FAIL (infeasible)")
                            break
                    elif _re.search(r"\b" + _re.escape(tok_l) + r"\b", lower_text):
                        actions.append("FAIL")
                        step_summary_parts.append("FAIL (infeasible)")
                        break

        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                }
                d = getattr(usage, "input_tokens_details", None)
                if d is not None:
                    self.last_usage["cached_tokens"] = getattr(d, "cached_tokens", 0) or 0
                d = getattr(usage, "output_tokens_details", None)
                if d is not None:
                    self.last_usage["reasoning_tokens"] = getattr(d, "reasoning_tokens", 0) or 0
                for k in ("input_tokens", "output_tokens", "total_tokens",
                          "cached_tokens", "reasoning_tokens"):
                    self.total_usage[k] = self.total_usage.get(k, 0) + self.last_usage.get(k, 0)
        except Exception as _e:
            logger.debug("usage parse failed: %s", _e)

        self.actions_log.append(
            " ; ".join(step_summary_parts) if step_summary_parts else "(no tool call)"
        )
        return response_text, actions


class OpenAICUAToolsAgent(_OpenAICUABase):
    """OpenAI CUA-only agent using ONLY the documented built-in tools.

    Tools: {"type":"computer"}, {"type":"shell","environment":{"type":"local"}}.
    No custom function tools. Reference:
      https://developers.openai.com/api/docs/guides/tools-computer-use
      https://developers.openai.com/api/docs/guides/tools-shell
    """
    _MODE = "cua_tools"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.info(
            "OpenAICUAToolsAgent model=%s tools=%s",
            self.model, [t.get("type") for t in self.tools],
        )
