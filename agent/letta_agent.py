"""Letta-driven agent that plays Pokemon Red.

Shape A: this module owns the loop. Each step we send the agent a user message
containing the current screenshot + game state, parse a structured action JSON
from the assistant's reply, and execute it on the local emulator.

Memory (persona, goals, party, trajectory, etc.) lives in Letta-side memory
blocks defined in agent.persona.INITIAL_BLOCKS. The agent updates them itself
as it plays. We do NOT manage message history -- Letta handles that.

Switching to Shape B (Letta tools) later requires only:
  1. Wrap each branch of `_dispatch_action` as a Letta tool.
  2. Drop the JSON-parsing path.
The dispatch handlers are deliberately isolated to make this mechanical.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re

from letta_client import Letta

from agent.emulator import Emulator
from agent.persona import INITIAL_BLOCKS
from config import (
    CONTEXT_WINDOW_LIMIT,
    LETTA_EMBEDDING,
    LETTA_MODEL,
    USE_NAVIGATOR,
)

logger = logging.getLogger(__name__)


def _screenshot_to_base64(screenshot, upscale: int = 2) -> str:
    """Encode a PIL screenshot as a base64 PNG string."""
    if upscale > 1:
        screenshot = screenshot.resize(
            (screenshot.width * upscale, screenshot.height * upscale)
        )
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_action(text: str) -> dict | None:
    """Pull the last fenced ```json block out of an assistant message.

    Returns the parsed dict on success, or None if no valid block found.
    """
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        return None
    # Last block wins -- the persona prompt says so.
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


class LettaAgent:
    """Drives a Letta agent against a local PyBoy emulator."""

    def __init__(
        self,
        rom_path: str,
        headless: bool = True,
        sound: bool = False,
        load_state: str | None = None,
    ):
        self.emulator = Emulator(rom_path, headless, sound)
        self.emulator.initialize()
        if load_state:
            logger.info(f"Loading saved state from {load_state}")
            self.emulator.load_state(load_state)

        api_key = os.environ.get("LETTA_API_KEY")
        if not api_key:
            raise RuntimeError(
                "LETTA_API_KEY is not set. Add it to your .env or export it."
            )

        self.client = Letta(api_key=api_key)

        logger.info("Creating fresh Letta agent...")
        agent_state = self.client.agents.create(
            model=LETTA_MODEL,
            embedding=LETTA_EMBEDDING,
            memory_blocks=INITIAL_BLOCKS,
            context_window_limit=CONTEXT_WINDOW_LIMIT,
            tags=["letta-plays-pokemon"],
        )
        self.agent_id = agent_state.id
        logger.info(f"Created agent {self.agent_id}")
        logger.info(
            f"Inspect this agent at https://app.letta.com/agents/{self.agent_id}"
        )

        self.running = True
        self.step = 0

    # ------------------------------------------------------------------
    # Per-step state collection
    # ------------------------------------------------------------------

    def _build_user_message(self) -> list[dict]:
        """Build the multimodal content list for this turn's user message."""
        screenshot = self.emulator.get_screenshot()
        screenshot_b64 = _screenshot_to_base64(screenshot, upscale=2)

        memory_info = self.emulator.get_state_from_memory()
        collision_map = self.emulator.get_collision_map()
        valid_moves = self.emulator.get_valid_moves()
        valid_moves_str = ", ".join(valid_moves) if valid_moves else "None visible"

        text = (
            f"Step {self.step}.\n\n"
            f"=== Game state from RAM ===\n{memory_info}\n"
            f"=== Valid overworld moves ===\n{valid_moves_str}\n"
        )
        if collision_map:
            text += f"\n=== Collision map ===\n{collision_map}\n"
        text += (
            "\nWhat is your next action?\n"
            "Reminders:\n"
            "- Consult `action_trajectory` first; avoid loops.\n"
            "- Update any memory block where you have something new to record.\n"
            "- Respond with reasoning, then exactly one fenced ```json block.\n"
        )

        return [
            {"type": "text", "text": text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                    "detail": "auto",
                },
            },
        ]

    # ------------------------------------------------------------------
    # Agent round-trip
    # ------------------------------------------------------------------

    def _send(self, content: list[dict] | str) -> str:
        """Send a user message and return the concatenated assistant text."""
        response = self.client.agents.messages.create(
            agent_id=self.agent_id,
            messages=[{"role": "user", "content": content}],
        )

        # Concatenate all assistant_message text blocks. Letta returns a stream
        # of typed messages (reasoning, tool_call, assistant_message, etc.).
        chunks: list[str] = []
        for msg in response.messages:
            msg_type = getattr(msg, "message_type", None) or getattr(msg, "type", None)
            if msg_type == "assistant_message":
                content_field = getattr(msg, "content", None)
                if isinstance(content_field, str):
                    chunks.append(content_field)
                elif isinstance(content_field, list):
                    for block in content_field:
                        text = getattr(block, "text", None) or (
                            block.get("text") if isinstance(block, dict) else None
                        )
                        if text:
                            chunks.append(text)

        return "\n".join(chunks)

    # ------------------------------------------------------------------
    # Action dispatch -- isolated handlers (Shape B-friendly)
    # ------------------------------------------------------------------

    def _do_press_buttons(self, buttons: list[str], wait: bool = True) -> str:
        logger.info(f"[Action] press_buttons {buttons} (wait={wait})")
        result = self.emulator.press_buttons(buttons, wait)
        return result

    def _do_navigate_to(self, row: int, col: int) -> str:
        logger.info(f"[Action] navigate_to ({row}, {col})")
        status, path = self.emulator.find_path(row, col)
        if path:
            for direction in path:
                self.emulator.press_buttons([direction], True)
            return f"{status} -- followed {len(path)} steps"
        return f"navigate failed: {status}"

    def _dispatch_action(self, action: dict) -> str:
        """Run an action dict against the emulator. Returns a short result string."""
        name = action.get("action")
        if name == "press_buttons":
            buttons = action.get("buttons", [])
            wait = action.get("wait", True)
            if not isinstance(buttons, list) or not buttons:
                return "error: 'buttons' must be a non-empty list"
            return self._do_press_buttons(buttons, wait)
        if name == "navigate_to":
            if not USE_NAVIGATOR:
                return "error: navigate_to is disabled in this run"
            try:
                row = int(action["row"])
                col = int(action["col"])
            except (KeyError, TypeError, ValueError):
                return "error: navigate_to needs integer 'row' and 'col'"
            return self._do_navigate_to(row, col)
        return f"error: unknown action '{name}'"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, num_steps: int = 1) -> int:
        logger.info(f"Starting agent loop for {num_steps} steps")
        steps_completed = 0

        while self.running and steps_completed < num_steps:
            self.step = steps_completed + 1
            try:
                # Send current state, get reply.
                content = self._build_user_message()
                reply = self._send(content)
                logger.info(f"[Step {self.step}] Assistant reply:\n{reply}")

                action = _extract_action(reply)

                # One corrective retry if the agent didn't emit a valid action.
                if action is None:
                    logger.warning(
                        f"[Step {self.step}] No parseable json action; retrying once."
                    )
                    reply = self._send(
                        "Your last response did not contain a valid fenced ```json "
                        "action block. Re-read the action protocol and respond with "
                        "exactly one fenced ```json block matching the schema."
                    )
                    logger.info(f"[Step {self.step}] Retry reply:\n{reply}")
                    action = _extract_action(reply)

                if action is None:
                    logger.error(
                        f"[Step {self.step}] Skipping step -- no valid action."
                    )
                else:
                    logger.info(f"[Step {self.step}] Parsed action: {action}")
                    result = self._dispatch_action(action)
                    logger.info(f"[Step {self.step}] Result: {result}")

                steps_completed += 1
                logger.info(f"Completed step {steps_completed}/{num_steps}")

            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, stopping")
                self.running = False
            except Exception:
                logger.exception(f"Error during step {self.step}")
                raise

        if not self.running:
            self.emulator.stop()

        return steps_completed

    def stop(self) -> None:
        self.running = False
        self.emulator.stop()
