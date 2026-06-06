"""Claude Computer Use Agent with bash + text_editor tools.

Uses Anthropic's three-tool pattern (industry standard for computer use):
  1. computer — GUI automation (screenshot, click, type, key, scroll)
  2. bash — execute shell commands directly (no GUI needed)
  3. text_editor — view/edit files (str_replace, create, insert)

The bash tool is critical for code-heavy tasks — the agent can run Python
scripts, process files, and create deliverables without needing to open a
terminal window through the GUI.

Prompt caching: based on the canonical OSWorld Anthropic agent pattern —
`cache_control: ephemeral` markers are applied to the system prompt plus the
last two user messages on every call, so the server reuses the previous
turn's KV cache instead of reprocessing the whole history every step. See
`_inject_prompt_caching` below.
"""

import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from anthropic import (
    APIError,
    APIStatusError,
    APIResponseValidationError,
)
from anthropic.types.beta import BetaMessage

from agents.base import BaseAgent, encode_image
from agents.prompts import CLAUDE_CUA_SYSTEM_PROMPT

# Target display size — Anthropic's computer-use models were RLHF'd on
# 1280×720 screenshots. We resize every incoming screenshot to this size
# before base64-encoding and sending it to the model. The tool declaration
# uses the same dimensions. Output coordinates are then scaled back to the
# real VM resolution via `self._resize_factor`. Matches the canonical
# OSWorld pattern (main.py:407-425).
_CLAUDE_DISPLAY_SIZE = (1280, 720)

# Retryable exception types for the Anthropic SDK — matches the canonical
# OSWorld catch list. Bare `except Exception` masked real bugs and
# couldn't special-case 25MB image-size errors cleanly.
_CLAUDE_RETRYABLE = (APIError, APIStatusError, APIResponseValidationError)

# Prompt caching beta flag — required for cache_control breakpoints to take
# effect. Based on the canonical OSWorld Anthropic agent pattern. This is
# threaded through as an HTTP header on the client (not the betas kwarg) so
# it applies to every request even when betas is otherwise empty.
PROMPT_CACHING_BETA_FLAG = "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11"

logger = logging.getLogger("mypcbench.agent.claude_cuabash")


# Infeasibility detection — based on the OSWorld `[INFEASIBLE]` scan but broader
# so the model doesn't have to use the exact token. Compiled once at import.
_INFEASIBLE_RE = re.compile(
    r"\[INFEASIBLE\]|\binfeasible\b|\bunfeasible\b|\bimpossible\b|"
    r"\bcannot be done\b|\bnot feasible\b",
    re.IGNORECASE,
)

# Claude's computer tool emits key names like "page_down", "super_l", "escape"
# that pyautogui does NOT recognize verbatim. Translate them to the canonical
# pyautogui key names. Mirrors the canonical OSWorld `key_conversion` dict.
_CLAUDE_KEY_MAP = {
    "page_down": "pagedown",
    "page_up": "pageup",
    "super_l": "win",
    "super_r": "win",
    "super": "win",
    "cmd": "command",
    "escape": "esc",
    "return": "enter",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "meta_l": "win",
    "meta_r": "win",
    "arrow_down": "down",
    "arrow_up": "up",
    "arrow_left": "left",
    "arrow_right": "right",
    "arrowdown": "down",
    "arrowup": "up",
    "arrowleft": "left",
    "arrowright": "right",
    "kp_enter": "enter",
}


def _claude_map_key(key: str) -> str:
    """Normalize a Claude-emitted key name to pyautogui's canonical form."""
    return _CLAUDE_KEY_MAP.get((key or "").strip().lower(), key)


# ---------------------------------------------------------------------------
# Prompt caching helpers (based on the canonical OSWorld Anthropic agent pattern)
# ---------------------------------------------------------------------------
def _inject_prompt_caching(messages: List[Dict]) -> None:
    """Pin cache breakpoints to maximize hit rate across long trajectories.

    Anthropic allows up to 4 cache_control breakpoints per request; one is
    used by the system block (applied separately by the caller). The
    remaining 3 are placed on user messages as follows:

      1. First user message (task instruction + initial screenshot) with a
         1-hour TTL — stable for the entire task, so the long static
         prefix survives slow tool calls without re-creation.
      2. Second-to-last user message with default 5-min ephemeral —
         bridges across the assistant tool round between consecutive
         predict() calls.
      3. Last user message with default 5-min ephemeral — picks up the
         latest tool_results + screenshot.

    Older breakpoints set on earlier turns are cleared so we never exceed
    the 4-breakpoint budget. Note: an earlier "last 2 only" pattern
    (sliding window) left the static initial context unanchored, causing
    most input tokens to count as cache_creation instead of cache_read.
    Pinning the first user message with a 1-hour TTL fixes that.
    """
    user_msgs = [
        m for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and m.get("content")
    ]
    if not user_msgs:
        return

    # Plan which messages get which TTL. Order matters: 1-hour wins ties
    # so a single-user-message conversation pins to 1h.
    pinned: Dict[int, Dict[str, str]] = {}
    for m in user_msgs[-2:]:
        pinned[id(m)] = {"type": "ephemeral"}
    pinned[id(user_msgs[0])] = {"type": "ephemeral", "ttl": "1h"}

    for m in user_msgs:
        last = m["content"][-1]
        if not isinstance(last, dict):
            continue
        ctrl = pinned.get(id(m))
        if ctrl is not None:
            last["cache_control"] = ctrl
        else:
            last.pop("cache_control", None)


class ClaudeCUAAgent(BaseAgent):
    """Claude CUA agent using computer_20250124 tool."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        screen_size: tuple = (1280, 800),
        client_password: str = "password",
        max_tokens: int = 8192,
        only_n_most_recent_images: int = 20,
        api_key: Optional[str] = None,
        enable_computer: bool = True,
        enable_bash: bool = True,
        enable_editor: bool = True,
        env=None,
        *,
        enable_thinking: bool = True,
        thinking_budget_tokens: int = 3584,
        api_retry_times: int = 5,
        api_retry_interval: float = 5.0,
        effort: str = os.environ.get("MYPCBENCH_CLAUDE_EFFORT", "high"),
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        backup_api_key: Optional[str] = None,
    ):
        super().__init__(model, screen_size, client_password)
        self.max_tokens = max_tokens
        # Bumped from 10 → 20 to match OSWorld's `image_truncation_threshold=20`
        # when prompt caching is enabled. Older images are replaced by text
        # placeholders by `_trim_images()` below.
        self.only_n_most_recent_images = only_n_most_recent_images
        self.env = env
        self.enable_computer = enable_computer

        # Extended-thinking config. Mirrors the canonical OSWorld pattern:
        # `extra_body={"thinking": {"type": "enabled", "budget_tokens": 3584}}`.
        # max_tokens must be > budget_tokens when thinking is enabled, so we
        # auto-bump below if the caller set it too low.
        self.enable_thinking = enable_thinking
        self.thinking_budget_tokens = thinking_budget_tokens
        if self.enable_thinking and self.max_tokens <= self.thinking_budget_tokens:
            self.max_tokens = self.thinking_budget_tokens + 512

        # Retry config — bare `except Exception` was too brittle for long runs.
        self.api_retry_times = max(1, int(api_retry_times))
        self.api_retry_interval = float(api_retry_interval)

        # Reasoning effort — mirrors the OSWorld `output_config={"effort": ...}`.
        # Valid values: "low", "medium", "high", "max". If the installed SDK
        # doesn't accept output_config (TypeError from client.beta.messages
        # .create) we gracefully disable it for subsequent calls and log a
        # warning rather than breaking the run.
        self.effort = effort
        self._output_config_supported = True

        # Sampling params — the canonical OSWorld agent passes these via
        # `_get_sampling_params()`. Default None means "use server default"
        # (no kwarg sent). Users can
        # override in __init__ if they need deterministic vs. exploratory
        # sampling.
        self.temperature = temperature
        self.top_p = top_p

        # Backup API key for fallback when the primary key errors out
        # (rate-limit, quota, auth issues). If None, no fallback occurs.
        self.backup_api_key = backup_api_key or os.environ.get("ANTHROPIC_API_KEY_BACKUP")

        # Anthropic client — max_retries=4 mirrors the canonical OSWorld SDK-side
        # retry budget (on top of our own retry loop below). We also attach
        # the prompt-caching beta flag as a default header so it applies to
        # every call, not just ones we remember to pass `betas=[...]` for.
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(
            api_key=resolved_key,
            max_retries=4,
        ).with_options(
            default_headers={"anthropic-beta": PROMPT_CACHING_BETA_FLAG},
        )
        self.messages: list = []
        # Running text log of prior actions (similar to the OSWorld Qwen agent
        # `previous_actions_str`). Appended to each call's user message so
        # the model always has a monotonically-growing context even when
        # the visible screenshot is pixel-identical to the previous step.
        self.actions_log: List[str] = []

        # Token/cost tracking. Populated from `response.usage` after every
        # successful API call. `cache_creation_input_tokens` shows the first
        # turn's cache write; `cache_read_input_tokens` shows cache hits on
        # subsequent turns and is the signal prompt caching is working.
        self.last_usage: Dict[str, int] = {}
        self.total_usage: Dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        # Compute the current date once in __init__ — the runtime won't span
        # multiple days, so recomputing on every call is wasted work.
        current_date = datetime.today().strftime("%A, %B %d, %Y")

        # Use appropriate system prompt based on mode
        if enable_computer:
            # The shipped claude_cuabash agent always runs with all three
            # tools (computer + bash + str_replace_based_edit_tool), so the
            # prompt's tool list is already accurate as written.
            self.system_prompt = CLAUDE_CUA_SYSTEM_PROMPT.format(
                CLIENT_PASSWORD=self.client_password,
                CURRENT_DATE=current_date,
            )
        else:
            # No-GUI variant (computer tool disabled) — still gets the
            # full MyPCBench context so the model knows the persona, the 17
            # apps, and the benchmark conventions. No `computer` tool so the
            # primer mentions only bash + editor.
            from agents.prompts import MYPCBENCH_CONTEXT as _CTX
            self.system_prompt = (
                "You are Michael Scott's AI coding assistant with bash and "
                "text_editor tools. No GUI / screenshots — execute commands "
                "directly.\n\n"
                "When the task is completed, state your final answer in plain "
                "text, then say ```DONE```. If the task is genuinely infeasible, "
                "say ```FAIL``` or use `[INFEASIBLE]`.\n"
                + _CTX.format(
                    CLIENT_PASSWORD=self.client_password,
                    CURRENT_DATE=current_date,
                )
            )

        # Detect tool versions based on model. Per Anthropic's computer-use
        # docs (https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool):
        #   computer-use-2025-11-24 + computer_20251124  →  Opus 4.5/4.6/4.7, Sonnet 4.6
        #   computer-use-2025-01-24 + computer_20250124  →  Sonnet 4/4.5, Opus 4/4.1,
        #                                                   Haiku 4.5, Sonnet 3.7
        # The earlier gate matched ONLY `claude-sonnet-4-5-20250929`, which
        # meant every other 2025-01-24-era model silently got the 2025-11-24
        # header + computer_20251124 tool — a combination the docs do NOT
        # list as compatible. Broaden the match to cover all legacy-era
        # models. Newer 4.6+/4.7 models keep the new beta.
        ml = model.lower()
        is_legacy = (
            "claude-sonnet-4-5" in ml          # Sonnet 4.5 (any snapshot)
            or "claude-sonnet-4-0" in ml       # Sonnet 4 (any snapshot)
            or ("claude-sonnet-4" in ml        # Sonnet 4 without -4.6 / -4.7
                and "claude-sonnet-4-6" not in ml
                and "claude-sonnet-4-7" not in ml)
            or "claude-opus-4-0" in ml         # Opus 4 (any snapshot)
            or "claude-opus-4-1" in ml         # Opus 4.1
            or ("claude-opus-4" in ml          # Opus 4 without -4.5 / -4.6 / -4.7
                and "claude-opus-4-5" not in ml
                and "claude-opus-4-6" not in ml
                and "claude-opus-4-7" not in ml)
            or "claude-haiku-4-5" in ml        # Haiku 4.5
            or "claude-3-7-sonnet" in ml       # Sonnet 3.7
            or "claude-sonnet-3-7" in ml       # Sonnet 3.7 (alt naming)
        )
        computer_type = "computer_20250124" if is_legacy else "computer_20251124"
        bash_type = "bash_20250124"
        editor_type = "text_editor_20250728"
        beta_flag = "computer-use-2025-01-24" if is_legacy else "computer-use-2025-11-24"

        self.tools = []

        if enable_computer:
            # Declare 1280×720 as the display size — screenshots will be
            # resized to this in predict() before sending. The model's
            # coordinate outputs are in this space; _tool_call_to_pyautogui
            # scales them back to the real VM resolution.
            self.tools.append({
                "type": computer_type,
                "name": "computer",
                "display_width_px": _CLAUDE_DISPLAY_SIZE[0],
                "display_height_px": _CLAUDE_DISPLAY_SIZE[1],
                "display_number": 1,
            })

        if enable_bash:
            self.tools.append({
                "type": bash_type,
                "name": "bash",
            })

        if enable_editor:
            self.tools.append({
                "type": editor_type,
                "name": "str_replace_based_edit_tool",
            })

        # Beta flag needed when any beta tool type is used (computer, bash, editor).
        # CRITICAL: the prompt-caching beta flag MUST be appended to `self.betas`
        # here — the SDK joins `betas` into a per-request `anthropic-beta` header
        # that OVERWRITES any same-named default header on the client. Putting the
        # caching flag only in `default_headers` silently drops it on every call,
        # disabling the entire prompt-caching layer.
        has_beta_tools = any(
            t.get("type", "").startswith(("computer_", "bash_", "text_editor_"))
            for t in self.tools
        )
        self.betas = [beta_flag] if has_beta_tools else []
        self.betas.append(PROMPT_CACHING_BETA_FLAG)

    def reset(self, logger=None):
        super().reset(logger)
        self.messages.clear()
        self.actions_log.clear()

    def _previous_actions_block(self) -> str:
        """Render the prior-actions text log. Matches the OSWorld Qwen agent pattern.

        NOTE: This block is only rendered into the FIRST user message (in
        predict() below). Turns 2+ never see the "Previous actions:" text —
        Anthropic's message cache makes re-inserting it unsafe across turns.
        We keep the log anyway for that first-turn context and for debugging.
        """
        if not self.actions_log:
            return "None"
        return "\n".join(
            f"Step {i + 1}: {act}" for i, act in enumerate(self.actions_log)
        )

    def _resolve_pending_tool_uses(self, b64: Optional[str]) -> None:
        """Resolve any unresolved tool_use blocks in the previous assistant message.

        Based on the canonical OSWorld Anthropic agent pattern. The
        Anthropic Messages API requires that EVERY tool_use block in an
        assistant message be matched by a tool_result block in the following
        user message. If the current turn emits both a computer action AND a
        bash/editor call, the code path at end of predict() only emits
        tool_results for bash/editor — the computer tool_use is then
        orphaned and the server 400s on the next call ("tool_use requires
        tool_result").

        This method scans the last assistant message for unresolved tool_use
        blocks (computer blocks specifically — bash/editor calls are resolved
        inline in the prior predict() call). For each unresolved block it
        emits a tool_result with "Action executed." text. The LAST such
        tool_result also receives the current screenshot so the model can
        see the post-action state.

        The tool_results are appended to the EXISTING user message (if the
        last message is user, which is the case when bash/editor results
        were already pushed) or as a NEW user message (computer-only turn,
        last message is assistant). Either way, role alternation is
        preserved.
        """
        if not self.messages:
            return

        last_msg = self.messages[-1]
        if last_msg.get("role") == "assistant":
            assistant_msg = last_msg
            user_msg = None
        else:
            # Last is user — check previous assistant for pending tool_use
            if len(self.messages) < 2 or self.messages[-2].get("role") != "assistant":
                return
            assistant_msg = self.messages[-2]
            user_msg = last_msg

        assistant_content = assistant_msg.get("content") or []
        tool_use_blocks = [
            b for b in assistant_content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not tool_use_blocks:
            return

        # Compute which tool_use IDs are already resolved in the user message.
        resolved_ids: set = set()
        if user_msg is not None:
            for item in user_msg.get("content", []) or []:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tid = item.get("tool_use_id")
                    if tid:
                        resolved_ids.add(tid)

        unresolved = [b for b in tool_use_blocks if b.get("id") not in resolved_ids]
        if not unresolved:
            return

        # The LAST unresolved tool_use gets the screenshot (only computer
        # blocks benefit from the screenshot; bash/editor wouldn't normally
        # appear here because they're resolved inline — but if they did, the
        # screenshot still attaches harmlessly to the last entry).
        last_idx = len(unresolved) - 1
        new_results: List[dict] = []
        for i, block in enumerate(unresolved):
            content_list: list = [{"type": "text", "text": "Action executed."}]
            if i == last_idx and b64:
                content_list.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                })
            new_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content_list,
                # `is_error` is required on every tool_result block per the
                # Anthropic Messages API contract. The resolver creates a
                # tool_result for an action that hasn't actually been
                # executed yet (the computer block is resolved here, on the
                # NEXT turn, with the post-action screenshot) — so we mark
                # it non-error by default.
                "is_error": False,
            })

        if user_msg is not None:
            existing = user_msg.get("content", [])
            if isinstance(existing, list):
                existing.extend(new_results)
            else:
                user_msg["content"] = new_results
        else:
            self.messages.append({"role": "user", "content": new_results})

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List[str]]:
        """Predict action using Claude tools.

        Returns (response_text, pyautogui_code_list).
        """
        screenshot = obs.get("screenshot") if self.enable_computer else None
        b64 = None
        if screenshot:
            # Resize screenshot to _CLAUDE_DISPLAY_SIZE (1280×720) before
            # sending. The model replies in 1280×720 coords; we scale back
            # in _tool_call_to_pyautogui. Matches the canonical OSWorld
            # pattern (main.py:407-425).
            from PIL import Image
            from io import BytesIO
            import base64 as _b64
            img = Image.open(BytesIO(screenshot))
            real_w, real_h = img.size
            target_w, target_h = _CLAUDE_DISPLAY_SIZE
            if (real_w, real_h) != (target_w, target_h):
                img = img.resize((target_w, target_h), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="PNG")
                b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
                self._resize_factor = (real_w / target_w, real_h / target_h)
            else:
                b64 = encode_image(screenshot)
                self._resize_factor = (1.0, 1.0)

        # Build initial message if first step. Includes the instruction +
        # empty prior-actions block so the prompt shape stays consistent
        # across steps.
        if not self.messages:
            self.messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        f"Task: {instruction}\n\n"
                        f"Previous actions:\n{self._previous_actions_block()}"
                    ),
                }],
            })

        # Resolve any unresolved tool_use blocks in the previous assistant
        # message (H2: mixed computer + bash/editor turns would otherwise
        # leave the computer tool_use orphaned and 400 on the next call).
        # This inserts tool_result blocks with the screenshot attached to
        # the last one. Based on the canonical OSWorld Anthropic agent pattern.
        self._resolve_pending_tool_uses(b64)

        # If the above didn't fire (first-turn case, or no tool_use to
        # resolve), we still need to surface the initial screenshot. The
        # first user message was pure text, so we attach the image here.
        if b64 and self.messages:
            last_msg = self.messages[-1]
            last_content = last_msg.get("content", [])
            if last_msg.get("role") == "user" and isinstance(last_content, list):
                # Check if this user message already has an image or image-
                # bearing tool_result — if not, append one.
                has_image = False
                for item in last_content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "image":
                        has_image = True
                        break
                    if item.get("type") == "tool_result":
                        sub = item.get("content", [])
                        if isinstance(sub, list) and any(
                            isinstance(s, dict) and s.get("type") == "image"
                            for s in sub
                        ):
                            has_image = True
                            break
                if not has_image:
                    last_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    })

        # Trim old images
        self._trim_images()

        # Prompt caching — applied on every call:
        #   * system prompt block gets `cache_control: ephemeral`
        #   * last 2 user messages get breakpoints via _inject_prompt_caching
        # The cache budget is 4 breakpoints/request; we use 1 for system
        # (and the first tool_use context) and 2 for recent user turns.
        _inject_prompt_caching(self.messages)
        system_blocks = [
            {
                "type": "text",
                "text": self.system_prompt,
                # System prompt is stable for the entire run — pin a 1-hour
                # TTL so it survives slow tool rounds (bash commands that
                # take minutes can otherwise blow past the default 5-min
                # ephemeral TTL).
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ]

        # Call Claude (always use beta API when beta tools are present).
        # Retry on transient errors with backoff; on repeated image-size
        # failures, halve `only_n_most_recent_images` and retrim before
        # the next attempt (mirrors the OSWorld 25MB recovery pattern).
        api_kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "tools": self.tools,
            "messages": self.messages,
            "system": system_blocks,
        }
        if self.betas:
            api_kwargs["betas"] = self.betas

        # Opus 4.7 rejects `thinking.type=enabled` with a 400 — it requires
        # `adaptive` (server picks budget) plus `output_config.effort` to
        # control depth. Older Opus 4.x (4.5, 4.6) still accept enabled +
        # budget_tokens; only 4.7 needs the adaptive path. Pass output_config
        # through extra_body for the new path since the SDK kwarg isn't
        # recognized for this model family.
        ml = self.model.lower()
        is_opus_4x = (
            ml.startswith("claude-opus-4-7")
            or ml.startswith("claude-opus-4-8")
            or ml.startswith("claude-opus-4-9")
        )
        extra_body: Dict[str, Any] = {}
        if self.enable_thinking:
            if is_opus_4x:
                extra_body["thinking"] = {"type": "adaptive"}
            else:
                extra_body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens,
                }
        if self._output_config_supported:
            if is_opus_4x:
                extra_body["output_config"] = {"effort": self.effort}
            else:
                # SDK kwarg path for older models. Falls back on TypeError
                # below if the SDK rejects it (see retry loop).
                api_kwargs["output_config"] = {"effort": self.effort}
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        # Sampling params — omitted by default (server default wins), but
        # passed through if the user set them in __init__.
        if self.temperature is not None:
            api_kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            api_kwargs["top_p"] = self.top_p

        # Retry loop. Based on the canonical OSWorld Anthropic agent pattern:
        # - Catches the specific Anthropic exception family (not bare Exception)
        # - Detects 25MB image-payload errors → halves `only_n_most_recent_images`
        #   and re-trims before retrying
        # - Detects `output_config` kwarg rejection → disables it and retries
        # - On total exhaustion, falls back to `backup_api_key` for one final
        #   attempt with a fresh client.
        response = None
        last_error: Optional[BaseException] = None
        for attempt in range(self.api_retry_times):
            try:
                response = self.client.beta.messages.create(**api_kwargs)
                last_error = None
                break
            except TypeError as e:
                # SDK doesn't accept output_config kwarg — disable for the
                # remainder of this run and retry without it immediately.
                if "output_config" in str(e) and "output_config" in api_kwargs:
                    logger.warning(
                        "SDK rejected output_config kwarg; disabling for this run: %s",
                        str(e)[:200],
                    )
                    self._output_config_supported = False
                    api_kwargs.pop("output_config", None)
                    continue
                last_error = e
                if attempt < self.api_retry_times - 1:
                    logger.warning(
                        "Claude API TypeError (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, self.api_retry_times, str(e)[:200],
                        self.api_retry_interval,
                    )
                    time.sleep(self.api_retry_interval)
            except _CLAUDE_RETRYABLE as e:
                last_error = e
                err_msg = str(e)
                # Server-side rejection of `output_config={"effort": ...}` —
                # e.g. "This model does not support the effort parameter."
                # returned as a 400 (APIError subclass, NOT a TypeError). The
                # earlier TypeError-based disable only catches SDK-level
                # rejection; this catches the server-level rejection.
                if (
                    "output_config" in api_kwargs
                    and ("effort parameter" in err_msg
                         or ("effort" in err_msg and "not support" in err_msg))
                ):
                    logger.warning(
                        "Claude API rejected effort/output_config; disabling for this run: %s",
                        err_msg[:200],
                    )
                    self._output_config_supported = False
                    api_kwargs.pop("output_config", None)
                    continue
                # 25MB image payload limit — halve the image budget and retry.
                is_size_error = (
                    "25000000" in err_msg
                    or "Member must have length less than or equal to" in err_msg
                )
                if is_size_error and self.only_n_most_recent_images > 1:
                    new_cap = max(1, self.only_n_most_recent_images // 2)
                    logger.warning(
                        "Claude image payload too large; reducing image cap %d → %d",
                        self.only_n_most_recent_images, new_cap,
                    )
                    self.only_n_most_recent_images = new_cap
                    self._trim_images()
                    api_kwargs["messages"] = self.messages
                if attempt < self.api_retry_times - 1:
                    logger.warning(
                        "Claude API error (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, self.api_retry_times, err_msg[:200],
                        self.api_retry_interval,
                    )
                    time.sleep(self.api_retry_interval)
            except Exception as e:  # pragma: no cover - defensive safety net
                # Final safety net. Without
                # this, any exception NOT in _CLAUDE_RETRYABLE or TypeError
                # (e.g. raw ConnectionError, JSON decode error, asyncio
                # cancellation) escapes the retry loop and crashes the
                # whole trajectory instead of degrading to the synthetic
                # "api error" return used downstream.
                last_error = e
                logger.exception(
                    "Unexpected exception in Claude API retry loop "
                    "(attempt %d/%d)", attempt + 1, self.api_retry_times,
                )
                if attempt < self.api_retry_times - 1:
                    time.sleep(self.api_retry_interval)
        # Backup API key fallback — one last attempt with a fresh client.
        # Based on the canonical OSWorld backup-key fallback. Only fires if
        # the primary key is exhausted AND we have a backup key configured.
        if response is None and self.backup_api_key:
            logger.warning("Claude primary key exhausted, trying backup key")
            try:
                backup_client = anthropic.Anthropic(
                    api_key=self.backup_api_key,
                    max_retries=4,
                ).with_options(
                    default_headers={"anthropic-beta": PROMPT_CACHING_BETA_FLAG},
                )
                response = backup_client.beta.messages.create(**api_kwargs)
                last_error = None
            except _CLAUDE_RETRYABLE as e:
                logger.error("Claude backup key also failed: %s", str(e)[:200])
                last_error = e
        if response is None:
            logger.error("Claude API exhausted retries: %s", last_error)
            return f"(claude api error: {last_error})", []

        # Parse response — handle computer, bash, and text_editor tool calls
        actions = []
        response_text = ""
        assistant_content = []
        tool_results = []
        # Short human-readable summary of what happened this turn — goes
        # into `actions_log` so future turns can see it in the "Previous
        # actions:" block without re-reading the full assistant content.
        step_summary_parts: List[str] = []

        for block in response.content:
            # Preserve thinking blocks (with signature!) in message history.
            # The server requires the signature to validate continuity across
            # turns when extended thinking is enabled — dropping it would
            # invalidate the cache and risk validation errors.
            if getattr(block, "type", None) == "thinking":
                if hasattr(block, "model_dump"):
                    thinking_dict = block.model_dump()
                else:
                    thinking_dict = {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", ""),
                    }
                    sig = getattr(block, "signature", None)
                    if sig is not None:
                        thinking_dict["signature"] = sig
                assistant_content.append(thinking_dict)
                continue

            if hasattr(block, "text") and block.text:
                response_text += block.text
                assistant_content.append({"type": "text", "text": block.text})

                if "```DONE```" in block.text:
                    actions.append("DONE")
                    step_summary_parts.append("DONE")
                elif "```FAIL```" in block.text:
                    actions.append("FAIL")
                    step_summary_parts.append("FAIL")

            elif block.type == "tool_use":
                assistant_content.append(block.model_dump())
                tool_name = block.name

                if tool_name == "computer":
                    # GUI action → convert to pyautogui code for env.step().
                    # `_tool_call_to_pyautogui` returns empty string for the
                    # `screenshot` action (intentional — handled by env) AND
                    # for any UNSUPPORTED action (e.g. a legit Anthropic
                    # action we haven't mapped yet). Distinguish between the
                    # two: if it's an unsupported action, emit an ERROR
                    # tool_result inline so the resolver doesn't mark the
                    # stale tool_use as successful. Mirrors canonical
                    # behavior per the canonical OSWorld agent pattern.
                    action_name = (block.input or {}).get("action", "computer")
                    code = self._tool_call_to_pyautogui(block.input)
                    if code:
                        actions.append(code)
                    elif action_name not in ("screenshot", "wait"):
                        # Unsupported action — emit an error tool_result so
                        # the model sees the failure and tries a different
                        # approach on the next turn.
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": [{
                                "type": "text",
                                "text": (
                                    f"Error: unsupported computer action "
                                    f"`{action_name}`. Supported actions: "
                                    f"screenshot, left_click, right_click, "
                                    f"double_click, triple_click, middle_click, "
                                    f"mouse_move, type, key, hold_key, "
                                    f"scroll, left_click_drag, left_mouse_down, "
                                    f"left_mouse_up, cursor_position, wait."
                                ),
                            }],
                            "is_error": True,
                        })
                        logger.warning(
                            "Claude emitted unsupported computer action: %s",
                            action_name,
                        )
                    coord = (block.input or {}).get("coordinate")
                    text = (block.input or {}).get("text", "")
                    if coord:
                        step_summary_parts.append(
                            f"computer.{action_name}({coord[0]}, {coord[1]})"
                        )
                    elif text:
                        step_summary_parts.append(
                            f"computer.{action_name}({text[:60]!r})"
                        )
                    else:
                        step_summary_parts.append(f"computer.{action_name}")

                elif tool_name == "bash":
                    # Execute bash command in container immediately
                    # Per Anthropic docs: return tool_result with is_error flag
                    command = block.input.get("command", "")
                    restart = block.input.get("restart", False)
                    result_text, is_error = self._execute_bash(command, restart)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [{"type": "text", "text": result_text}],
                        "is_error": is_error,
                    })
                    logger.info("  bash: %s -> %s", command[:80], result_text[:200])

                elif tool_name in ("str_replace_editor", "str_replace_based_edit_tool"):
                    # Execute text editor command in container. `is_error` is
                    # required on every tool_result block per the Anthropic
                    # Messages API contract.
                    result_text, is_editor_error = self._execute_editor(block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [{"type": "text", "text": result_text}],
                        "is_error": is_editor_error,
                    })

        self.messages.append({"role": "assistant", "content": assistant_content})

        # If we have tool results for bash/editor, add them and let the model continue
        if tool_results:
            self.messages.append({"role": "user", "content": tool_results})

        # Broader infeasibility detection — based on the OSWorld [INFEASIBLE]
        # scan but matches any of the related phrases so the model doesn't
        # have to use the exact token. Only fires if no other action was
        # produced this turn (otherwise we defer to the explicit tool call).
        if response_text and "FAIL" not in actions and "DONE" not in actions:
            if _INFEASIBLE_RE.search(response_text):
                logger.info(
                    "Infeasibility phrase detected in response; appending FAIL"
                )
                actions.append("FAIL")
                step_summary_parts.append("FAIL(infeasible-detected)")

        # Fallback DONE — if the model returned TEXT only (no tool_use at
        # all, no tool_results pending) and didn't explicitly say DONE/FAIL,
        # treat it as terminal. Based on the canonical OSWorld agent pattern.
        #
        # CRITICAL: gate on "no tool_use block was seen in the assistant
        # content this turn". Without this, a screenshot-only turn (where
        # the model emits `{"type": "tool_use", "name": "computer", "input":
        # {"action": "screenshot"}}`) would slip through because
        # `_tool_call_to_pyautogui` returns "" for screenshot actions (they
        # resolve via the next turn's env.step flow) and the `screenshot`/
        # `wait` allowlist in the unsupported-action error path suppresses
        # tool_results — making `actions` and `tool_results` both empty
        # even though a legitimate tool_use happened.
        tool_use_seen = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in assistant_content
        )
        if (
            response_text
            and not actions
            and not tool_results
            and not tool_use_seen
        ):
            logger.info("Claude returned text-only response; appending DONE")
            actions.append("DONE")
            step_summary_parts.append("DONE(text-only-fallback)")

        # Record a human-readable summary of what we did this turn for the
        # prior-actions log on the next turn. If nothing happened, record a
        # placeholder so the log keeps growing monotonically (this is what
        # breaks the "same input → same output" loop in temp=0 runs).
        if step_summary_parts:
            self.actions_log.append(" ; ".join(step_summary_parts))
        else:
            self.actions_log.append("(no tool call)")

        # Token/cost tracking — harvest from response.usage. Claude reports:
        #   input_tokens, output_tokens,
        #   cache_creation_input_tokens (first turn's cache write),
        #   cache_read_input_tokens (subsequent cache hits — key signal).
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    "cache_creation_input_tokens": (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    ),
                    "cache_read_input_tokens": (
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    ),
                }
                for k, v in self.last_usage.items():
                    self.total_usage[k] = self.total_usage.get(k, 0) + v
        except Exception as _usage_err:
            logger.debug("Failed to parse response.usage: %s", _usage_err)

        return response_text, actions

    def _execute_bash(self, command: str, restart: bool = False) -> tuple[str, bool]:
        """Execute a bash command in the container via Control API.

        Returns (output_text, is_error) following Anthropic's tool_result convention.
        Output is truncated to ~10000 chars to prevent token limit issues
        (Anthropic docs recommend keeping tool results concise).
        """
        MAX_OUTPUT = 10000

        if not command and not restart:
            return "No command provided.", True
        if restart:
            return "Bash session restarted.", False
        if not self.env:
            return "Error: No environment connected for bash execution.", True

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

            # Truncate long outputs (Anthropic docs recommend ~100 lines)
            if len(text) > MAX_OUTPUT:
                text = text[:MAX_OUTPUT] + f"\n... (truncated, {len(text)} total chars)"

            # is_error fires on ANY of: non-zero exit code, non-empty stderr.
            # The canonical OSWorld agent marks is_error whenever
            # CLIResult.error is non-empty, not just on non-zero rc.
            is_error = (rc != 0) or bool(error)
            return text, is_error
        except Exception as e:
            return f"Error executing command: {e}", True

    def _execute_editor(self, input_dict: dict) -> tuple[str, bool]:
        """Execute a text editor command in the container.

        Returns (output_text, is_error). `is_error` is an authoritative
        signal rather than a prefix-sniffing heuristic — the caller uses it
        directly in the Anthropic tool_result `is_error` field. Mirrors
        `_execute_bash`'s (text, is_error) return shape.
        """
        command = input_dict.get("command", "")
        path = input_dict.get("path", "")

        if not self.env:
            return "Error: No environment connected for editor.", True

        if command == "view":
            result = self.env._execute_command(["cat", "-n", path], shell=False)
            rc = result.get("returncode", -1)
            if rc == 0:
                return result.get("output", ""), False
            err = result.get("error", "") or f"Error reading {path}"
            return err, True

        elif command == "create":
            # The canonical OSWorld editor rejects `create` on existing files
            # to prevent silent overwrites. Use os.path.exists() to check
            # before writing. `pathlib.Path.exists()` returns True even for
            # directories, matching the original behavior.
            file_text = input_dict.get("file_text", "")
            script = (
                f"import pathlib,sys; p=pathlib.Path({repr(path)}); "
                f"sys.exit(2) if p.exists() else None; "
                f"p.parent.mkdir(parents=True, exist_ok=True); "
                f"p.write_text({repr(file_text)}); print('Created')"
            )
            result = self.env._execute_command(["python3", "-c", script], shell=False)
            rc = result.get("returncode", -1)
            if rc == 2:
                return (
                    f"Error: file already exists at {path}. Cannot overwrite "
                    f"with `create`. Use `str_replace` or delete first.",
                    True,
                )
            if rc == 0:
                return f"File created: {path}", False
            return f"Error: {result.get('error', '')}", True

        elif command == "str_replace":
            # The canonical OSWorld editor requires `old_str` to appear
            # EXACTLY ONCE in the file. 0 occurrences → error. >1 occurrences
            # → error (ambiguous). Our previous impl used `t.replace(old, new)`
            # which silently replaced every occurrence.
            old = input_dict.get("old_str", "")
            new = input_dict.get("new_str", "")
            if not old:
                return "Error: old_str parameter is required and cannot be empty", True
            script = (
                f"import pathlib,sys; p=pathlib.Path({repr(path)}); "
                f"t=p.read_text(); "
                f"n=t.count({repr(old)}); "
                f"sys.exit(3) if n==0 else (sys.exit(4) if n>1 else None); "
                f"t=t.replace({repr(old)},{repr(new)}, 1); "
                f"p.write_text(t); print('Replaced')"
            )
            result = self.env._execute_command(["python3", "-c", script], shell=False)
            rc = result.get("returncode", -1)
            if rc == 3:
                return (
                    f"Error: no occurrences of `old_str` found in {path}",
                    True,
                )
            if rc == 4:
                return (
                    f"Error: `old_str` is ambiguous — multiple occurrences "
                    f"found in {path}. Make `old_str` unique.",
                    True,
                )
            if rc == 0:
                return result.get("output", "Replaced"), False
            return f"Error: {result.get('error', '')}", True

        elif command == "insert":
            insert_line = input_dict.get("insert_line", 0)
            new_str = input_dict.get("new_str", "")
            script = f"import pathlib; p=pathlib.Path({repr(path)}); lines=p.read_text().splitlines(True); lines.insert({insert_line},{repr(new_str+chr(10))}); p.write_text(''.join(lines)); print('Inserted')"
            result = self.env._execute_command(["python3", "-c", script], shell=False)
            rc = result.get("returncode", -1)
            if rc == 0:
                return result.get("output", "Inserted"), False
            return f"Error: {result.get('error', '')}", True

        return f"Unknown editor command: {command}", True

    def _tool_call_to_pyautogui(self, input_dict: dict) -> str:
        """Convert Claude computer use tool call to pyautogui code.

        Coordinates from the model are in _CLAUDE_DISPLAY_SIZE space
        (1280×720). We scale them back to the real VM resolution using
        `self._resize_factor` (set during screenshot resize in predict).
        """
        action = input_dict.get("action", "")
        coord = input_dict.get("coordinate")
        text = input_dict.get("text", "")

        # Scale coordinates from 1280×720 model space back to real VM pixels.
        # If no resize happened (VM == 1280×720), factor is (1.0, 1.0).
        if coord:
            fx, fy = getattr(self, "_resize_factor", (1.0, 1.0))
            coord = [int(coord[0] * fx), int(coord[1] * fy)]

        # Helper: wrap a click/scroll statement with keyDown/keyUp for each
        # modifier token in `text` (e.g. "shift" for shift-click,
        # "ctrl+shift" for ctrl-shift-click). Based on the canonical OSWorld
        # Anthropic agent which wraps click actions with the same pattern when the model
        # passes a `text` field alongside the click. Each key goes through
        # `_claude_map_key` for normalization.
        def _wrap_with_modifiers(inner: str, mod_text: str) -> str:
            if not mod_text:
                return inner
            mods = [_claude_map_key(k.strip()) for k in mod_text.split("+") if k.strip()]
            if not mods:
                return inner
            down = "; ".join(f"pyautogui.keyDown({m!r})" for m in mods)
            up = "; ".join(f"pyautogui.keyUp({m!r})" for m in reversed(mods))
            return f"{down}; {inner}; {up}"

        if action == "screenshot":
            # Emit a 100ms no-op sleep so the action list is non-empty —
            # the runner advances and env.get_obs() captures a fresh frame
            # which the resolver attaches to the tool_use on the next turn.
            # Returning empty string could stall loops that short-circuit
            # on actions=[].
            return "pyautogui.sleep(0.1)"
        elif action == "left_click":
            # Coordless fallback: click at current cursor per the canonical
            # OSWorld agent, which emits `pyautogui.click()` with no args.
            # `text` field holds modifier keys (shift/ctrl/etc.) that must
            # be held during the click — wrap via keyDown/keyUp.
            inner = (
                f"pyautogui.click({coord[0]}, {coord[1]})" if coord
                else "pyautogui.click()"
            )
            return _wrap_with_modifiers(inner, text)
        elif action == "right_click":
            inner = (
                f"pyautogui.rightClick({coord[0]}, {coord[1]})" if coord
                else "pyautogui.rightClick()"
            )
            return _wrap_with_modifiers(inner, text)
        elif action == "double_click":
            inner = (
                f"pyautogui.doubleClick({coord[0]}, {coord[1]})" if coord
                else "pyautogui.doubleClick()"
            )
            return _wrap_with_modifiers(inner, text)
        elif action == "triple_click":
            inner = (
                f"pyautogui.tripleClick({coord[0]}, {coord[1]})" if coord
                else "pyautogui.tripleClick()"
            )
            return _wrap_with_modifiers(inner, text)
        elif action == "middle_click":
            inner = (
                f"pyautogui.middleClick({coord[0]}, {coord[1]})" if coord
                else "pyautogui.middleClick()"
            )
            return _wrap_with_modifiers(inner, text)
        elif action == "mouse_move":
            if coord:
                return f"pyautogui.moveTo({coord[0]}, {coord[1]})"
            return "pyautogui.moveTo(0, 0)"
        elif action == "type" and text:
            return f"pyautogui.typewrite({repr(text)}, interval=0.02)"
        elif action == "key" and text:
            # Translate each key through _CLAUDE_KEY_MAP so "page_down" →
            # "pagedown", "super_l" → "win", "escape" → "esc", etc. pyautogui
            # rejects the raw Claude key names with KeyError otherwise.
            # Matches the canonical OSWorld `key_conversion`.
            keys = [_claude_map_key(k.strip()) for k in text.split("+")]
            if len(keys) == 1:
                return f"pyautogui.press({repr(keys[0])})"
            else:
                return f"pyautogui.hotkey({', '.join(repr(k) for k in keys)})"
        elif action == "hold_key" and text:
            # `hold_key` action — press and hold the key for `duration` seconds.
            # pyautogui doesn't have a direct hold_key,
            # but we can simulate with keyDown + sleep + keyUp.
            keys = [_claude_map_key(k.strip()) for k in text.split("+")]
            duration = input_dict.get("duration", 1)
            down = "; ".join(f"pyautogui.keyDown({k!r})" for k in keys)
            up = "; ".join(f"pyautogui.keyUp({k!r})" for k in reversed(keys))
            return f"{down}; import time; time.sleep({duration}); {up}"
        elif action == "left_mouse_down" and coord:
            return f"pyautogui.moveTo({coord[0]}, {coord[1]}); pyautogui.mouseDown()"
        elif action == "left_mouse_down":
            return "pyautogui.mouseDown()"
        elif action == "left_mouse_up" and coord:
            return f"pyautogui.moveTo({coord[0]}, {coord[1]}); pyautogui.mouseUp()"
        elif action == "left_mouse_up":
            return "pyautogui.mouseUp()"
        elif action == "cursor_position":
            # Informational action — return a no-op so the runner advances.
            return "import time\ntime.sleep(0.05)"
        elif action == "scroll":
            # Anthropic's computer tool emits scroll_direction ∈
            # {up, down, left, right} and an optional `text` field holding
            # a modifier key to hold during the scroll (e.g. 'ctrl' for
            # zoom). Sign convention (matches the canonical OSWorld agent):
            #   vertical: up → +amount, down → -amount
            #   horizontal: right → +amount, left → -amount
            # Omit x/y when the model didn't supply a coordinate.
            direction = input_dict.get("scroll_direction", "down")
            amount = input_dict.get("scroll_amount", 3)
            pos = f", x={coord[0]}, y={coord[1]}" if coord else ""
            if direction in ("up", "down"):
                dy = -amount if direction == "down" else amount
                inner = f"pyautogui.scroll({dy}{pos})"
            elif direction in ("left", "right"):
                dx = amount if direction == "right" else -amount
                inner = f"pyautogui.hscroll({dx}{pos})"
            else:
                return None  # unknown direction — flag as unsupported
            return _wrap_with_modifiers(inner, text)
        elif action == "left_click_drag":
            # When ONLY `coordinate` is given, drag
            # from the CURRENT cursor position to the target via dragTo().
            # When `start_coordinate` is given too, moveTo the start first,
            # then dragTo the target. Our previous impl used
            # `get("start_coordinate", coord)` which defaulted start = end
            # when start wasn't supplied — causing a zero-length drag
            # `drag(0, 0, duration=0.5)` which does nothing.
            start = input_dict.get("start_coordinate")
            # Scale start_coordinate too (it's also in model space).
            if start:
                fx, fy = getattr(self, "_resize_factor", (1.0, 1.0))
                start = [int(start[0] * fx), int(start[1] * fy)]
            end = coord  # already scaled above
            duration = input_dict.get("duration", 0.5)
            if end is None:
                return None  # no target — flag as unsupported
            if start:
                return (
                    f"pyautogui.moveTo({start[0]}, {start[1]}); "
                    f"pyautogui.dragTo({end[0]}, {end[1]}, duration={duration}, button='left')"
                )
            return (
                f"pyautogui.dragTo({end[0]}, {end[1]}, duration={duration}, button='left')"
            )
        elif action == "wait":
            # Use pyautogui.sleep (not time.sleep) so the action is
            # self-contained without needing an `import time` prepend.
            # Based on the canonical OSWorld agent which uses pyautogui.sleep(0.5).
            return "pyautogui.sleep(2)"

        logger.warning("Unknown Claude tool action: %s", action)
        return None  # unknown action — flag for unsupported-action error

    def _trim_images(self):
        """Keep only the N most recent images in messages."""
        if self.only_n_most_recent_images <= 0:
            return

        count = 0
        for msg in reversed(self.messages):
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                # Check tool_result content
                if item.get("type") == "tool_result":
                    sub = item.get("content", [])
                    if isinstance(sub, list):
                        for j, sub_item in enumerate(sub):
                            if isinstance(sub_item, dict) and sub_item.get("type") == "image":
                                count += 1
                                if count > self.only_n_most_recent_images:
                                    sub[j] = {"type": "text", "text": "[image removed]"}
                elif item.get("type") == "image":
                    count += 1
                    if count > self.only_n_most_recent_images:
                        item.clear()
                        item.update({"type": "text", "text": "[image removed]"})
