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
from datetime import datetime

from letta_client import Letta

from agent.emulator import Emulator
from agent.persona import INITIAL_BLOCKS
from config import (
    CONTEXT_WINDOW_LIMIT,
    LETTA_EMBEDDING,
    LETTA_MODEL,
    USE_NAVIGATOR,
    USE_SOFT_RESET,
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
        agent_id: str | None = None,
        session_dir: str | None = None,
        checkpoint_every: int = 5,
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

        if agent_id:
            # Resume: verify the agent exists, then keep its id.
            logger.info(f"Resuming Letta agent {agent_id}")
            self.client.agents.retrieve(agent_id=agent_id)
            self.agent_id = agent_id
            agent_name = f"resumed-{agent_id[:12]}"
        else:
            agent_name = (
                f"Pokemon Agent {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
            )
            logger.info(f"Creating fresh Letta agent: {agent_name}")
            agent_state = self.client.agents.create(
                name=agent_name,
                model=LETTA_MODEL,
                embedding=LETTA_EMBEDDING,
                memory_blocks=INITIAL_BLOCKS,
                context_window_limit=CONTEXT_WINDOW_LIMIT,
                tags=["letta-plays-pokemon"],
            )
            self.agent_id = agent_state.id
        logger.info(f"Agent id: {self.agent_id}")
        logger.info(
            f"Inspect this agent at https://app.letta.com/agents/{self.agent_id}"
        )

        # Session directory: holds agent_id.txt and state.pkl for resume.
        if session_dir is None:
            session_dir = os.path.join("sessions", agent_name)
        self.session_dir = os.path.abspath(session_dir)
        os.makedirs(self.session_dir, exist_ok=True)
        with open(os.path.join(self.session_dir, "agent_id.txt"), "w") as f:
            f.write(self.agent_id + "\n")
        # Update sessions/latest pointer so --resume can find this session.
        latest = os.path.abspath(os.path.join("sessions", "latest"))
        os.makedirs(os.path.dirname(latest), exist_ok=True)
        with open(latest, "w") as f:
            f.write(self.session_dir + "\n")
        logger.info(f"Session directory: {self.session_dir}")
        self.checkpoint_every = checkpoint_every

        self.running = True
        self.step = 0
        # Per-button screenshots captured during the previous action; sent
        # back to the agent on the next turn so it can see progression.
        self._last_action_frames: list[tuple[str, str]] = []  # (label, base64 png)
        self._last_action_summary: str | None = None
        # Track slow-moving state so we only surface the dump when it
        # changes, and identity strings so we only announce them once.
        self._last_party_state: str = ""
        self._last_inventory_state: str = ""
        self._last_badges_state: str = ""
        self._announced_player: str | None = None
        self._announced_rival: str | None = None
        # Track location changes so we can prompt for goal reassessment.
        self._last_location: str | None = None
        # How often to inject a periodic goal check-in.
        self._goal_checkin_every = 10

    # ------------------------------------------------------------------
    # Per-step state collection
    # ------------------------------------------------------------------

    def _build_user_message(self) -> list[dict]:
        """Build the multimodal content list for this turn's user message.

        Goal: only NEW information per step. Anything static lives in
        memory blocks (persona, current_team, etc.) which are already in
        context.
        """
        memory_info = self.emulator.get_state_from_memory()
        collision_map = self.emulator.get_collision_map()

        screenshot = self.emulator.get_screenshot()
        screenshot_b64 = _screenshot_to_base64(screenshot, upscale=2)

        # ---- Slow-moving state: write to memory blocks; echo only on change.
        party_state = self.emulator.get_party_state()
        party_changed = party_state != self._last_party_state
        if party_changed and party_state:
            self._update_block_safe("current_team", party_state)
            self._last_party_state = party_state

        inventory_state = self.emulator.get_inventory_state()
        inventory_changed = inventory_state != self._last_inventory_state
        if inventory_changed:
            self._update_block_safe("inventory", inventory_state)
            self._last_inventory_state = inventory_state

        badges_state = self.emulator.get_badges_state()
        badges_changed = badges_state != self._last_badges_state
        if badges_changed:
            # Allow empty value (no badges) -- still write so the block is
            # consistent. update may reject empty, so substitute a marker.
            self._update_block_safe(
                "badges", badges_state or "(no badges yet)\n"
            )
            self._last_badges_state = badges_state

        # ---- Goal nudges: trigger-based + periodic check-in.
        goal_nudges: list[str] = []
        from agent.memory_reader import PokemonRedReader  # local import; cheap
        location = PokemonRedReader(self.emulator.pyboy.memory).read_location()
        if self._last_location is not None and location != self._last_location:
            goal_nudges.append(
                f"You moved from '{self._last_location}' to '{location}'. "
                "Reassess short_term_goals -- is your current goal still right? "
                "Did you complete one? Update the block now if anything changed."
            )
        self._last_location = location
        if (
            self.step > 1
            and self._goal_checkin_every > 0
            and self.step % self._goal_checkin_every == 0
        ):
            goal_nudges.append(
                "Periodic goal check-in. Re-read short_term_goals and "
                "long_term_goals. Update them if they are stale, completed, "
                "or no longer match what you are actually doing."
            )

        # ---- Player / rival identity: announce once when they change.
        identity_lines: list[str] = []
        player, rival = self.emulator.get_player_identity()
        if player and player != self._announced_player:
            identity_lines.append(f"Player name set: {player}")
            self._announced_player = player
        if rival and rival != self._announced_rival:
            identity_lines.append(f"Rival name set: {rival}")
            self._announced_rival = rival

        content: list[dict] = []

        # Step header first so the agent locates itself immediately.
        content.append({"type": "text", "text": f"Step {self.step}."})

        # Per-button frames from the previous action, if any.
        if self._last_action_frames:
            content.append(
                {
                    "type": "text",
                    "text": f"Last action: {self._last_action_summary}",
                }
            )
            for label, b64 in self._last_action_frames:
                content.append({"type": "text", "text": label})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                            "detail": "auto",
                        },
                    }
                )
            self._last_action_frames = []
            self._last_action_summary = None

        # Identity announcements (one-shot).
        if identity_lines:
            content.append({"type": "text", "text": "\n".join(identity_lines)})

        # Goal nudges (trigger-based + periodic).
        if goal_nudges:
            content.append(
                {"type": "text", "text": "=== Goal check ===\n" + "\n".join(goal_nudges)}
            )

        # Party update (only when changed).
        if party_changed and party_state:
            content.append(
                {
                    "type": "text",
                    "text": f"=== Party update ===\n{party_state}",
                }
            )

        # Inventory update (only when changed -- includes money).
        if inventory_changed:
            content.append(
                {
                    "type": "text",
                    "text": f"=== Inventory update ===\n{inventory_state}",
                }
            )

        # Badges update (only when changed -- a new badge is a big deal).
        if badges_changed and badges_state:
            content.append(
                {
                    "type": "text",
                    "text": f"=== Badges update ===\n{badges_state}",
                }
            )

        # Per-step state.
        text = f"=== State ===\n{memory_info}"
        if collision_map:
            text += f"\n=== Collision map ===\n{collision_map}"
        content.append({"type": "text", "text": text})

        content.append({"type": "text", "text": "Current screen:"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                    "detail": "auto",
                },
            }
        )
        return content

    def _update_block_safe(self, label: str, value: str) -> None:
        """Best-effort memory block update. Logs and continues on failure."""
        try:
            self.client.agents.blocks.update(
                agent_id=self.agent_id, block_label=label, value=value
            )
        except Exception:
            logger.exception(f"Failed to update memory block '{label}'")

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

    MAX_BUTTONS_PER_ACTION = 5

    def _do_press_buttons(self, buttons: list[str], wait: bool = True) -> str:
        logger.info(f"[Action] press_buttons {buttons} (wait={wait})")
        result, frames = self.emulator.press_buttons_with_frames(buttons, wait)

        # Stash per-button frames for the next turn's user message.
        # Drop the last frame -- it's the same as the "Current screen" image
        # captured at the start of the next turn, so it would just duplicate.
        self._last_action_frames = []
        frames_to_send = frames[:-1]
        for i, (button, frame) in enumerate(
            zip(buttons[: len(frames_to_send)], frames_to_send), start=1
        ):
            self._last_action_frames.append(
                (
                    f"After press {i}/{len(frames)}: {button}",
                    _screenshot_to_base64(frame, upscale=2),
                )
            )
        self._last_action_summary = (
            f"You pressed {buttons} (wait={wait}). The frame after your last "
            f"press is shown below as 'Current screen'."
        )
        return result

    def _do_soft_reset(self) -> str:
        logger.warning("[Action] soft_reset -- returning to title screen")
        return self.emulator.soft_reset()

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
            if len(buttons) > self.MAX_BUTTONS_PER_ACTION:
                logger.warning(
                    f"[Action] truncating button list from {len(buttons)} to "
                    f"{self.MAX_BUTTONS_PER_ACTION}"
                )
                buttons = buttons[: self.MAX_BUTTONS_PER_ACTION]
            return self._do_press_buttons(buttons, wait)
        if name == "soft_reset":
            if not USE_SOFT_RESET:
                return "error: soft_reset is disabled in this run"
            return self._do_soft_reset()
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

                if (
                    self.checkpoint_every > 0
                    and steps_completed % self.checkpoint_every == 0
                ):
                    self.checkpoint()

            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, stopping")
                self.running = False
            except Exception:
                logger.exception(f"Error during step {self.step}")
                raise

        if not self.running:
            self.emulator.stop()

        return steps_completed

    def checkpoint(self) -> None:
        """Write the emulator state to the session directory."""
        path = os.path.join(self.session_dir, "state.pkl")
        try:
            self.emulator.save_state(path)
            logger.info(f"Checkpoint saved: {path}")
        except Exception:
            logger.exception("Failed to save checkpoint")

    def stop(self) -> None:
        self.running = False
        try:
            self.checkpoint()
        finally:
            self.emulator.stop()
