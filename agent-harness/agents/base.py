"""Base agent class for MyPCBench (OSWorld-compatible interface)."""

import base64
from typing import Dict, List, Tuple


def encode_image(image_bytes: bytes) -> str:
    """Encode raw PNG bytes to base64 string."""
    return base64.b64encode(image_bytes).decode("utf-8")


class BaseAgent:
    """Base class for all MyPCBench agents.

    Subclasses must implement predict(instruction, obs) -> (response, actions).
    """

    def __init__(self, model: str, screen_size: tuple, client_password: str = "password"):
        self.model = model
        self.screen_width, self.screen_height = screen_size
        self.client_password = client_password
        self.observations = []
        self.thoughts = []
        self.actions_history = []

    def reset(self, logger=None):
        """Reset agent state between tasks."""
        self.observations.clear()
        self.thoughts.clear()
        self.actions_history.clear()

    def predict(self, instruction: str, obs: Dict) -> Tuple[str, List]:
        """Predict next action(s) from instruction and observation.

        Returns:
            (response_text, actions_list)
            - response_text: Raw model response
            - actions_list: List of action strings (pyautogui code) or dicts
        """
        raise NotImplementedError
