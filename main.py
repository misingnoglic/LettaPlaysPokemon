import argparse
import logging
import os

from dotenv import load_dotenv

from agent.letta_agent import LettaAgent
from config import MAX_STEPS_DEFAULT

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Letta Plays Pokemon")
    parser.add_argument(
        "--rom",
        type=str,
        default="pokemon.gb",
        help="Path to the Pokemon ROM file",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=MAX_STEPS_DEFAULT,
        help="Number of agent steps to run",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="Run with display (not headless)",
    )
    parser.add_argument(
        "--sound",
        action="store_true",
        help="Enable sound (only applicable with display)",
    )
    parser.add_argument(
        "--load-state",
        type=str,
        default=None,
        help="Path to a saved state to load",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume the most recent session: reuse its agent_id and load its "
            "saved emulator state from sessions/latest."
        ),
    )
    parser.add_argument(
        "--session-dir",
        type=str,
        default=None,
        help=(
            "Specific session directory to resume from (overrides --resume). "
            "Reads agent_id.txt and state.pkl from this path."
        ),
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=5,
        help="Save emulator state every N steps (0 to disable).",
    )

    args = parser.parse_args()

    if not os.path.isabs(args.rom):
        rom_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.rom)
    else:
        rom_path = args.rom

    if not os.path.exists(rom_path):
        logger.error(f"ROM file not found: {rom_path}")
        print("\nYou need to provide a Pokemon Red ROM file to run this program.")
        print("Place the ROM in the root directory or specify its path with --rom.")
        return

    # Resolve resume target if requested.
    resume_agent_id: str | None = None
    resume_session_dir: str | None = args.session_dir
    if resume_session_dir is None and args.resume:
        latest_pointer = os.path.abspath(os.path.join("sessions", "latest"))
        if not os.path.exists(latest_pointer):
            logger.error(
                f"--resume given but no previous session found at {latest_pointer}"
            )
            return
        with open(latest_pointer) as f:
            resume_session_dir = f.read().strip()

    load_state_path: str | None = args.load_state
    if resume_session_dir:
        agent_id_path = os.path.join(resume_session_dir, "agent_id.txt")
        state_pkl_path = os.path.join(resume_session_dir, "state.pkl")
        if not os.path.exists(agent_id_path):
            logger.error(f"No agent_id.txt in {resume_session_dir}")
            return
        with open(agent_id_path) as f:
            resume_agent_id = f.read().strip()
        if load_state_path is None and os.path.exists(state_pkl_path):
            load_state_path = state_pkl_path
        logger.info(
            f"Resuming session: agent={resume_agent_id} state={load_state_path}"
        )

    agent = LettaAgent(
        rom_path=rom_path,
        headless=not args.display,
        sound=args.sound if args.display else False,
        load_state=load_state_path,
        agent_id=resume_agent_id,
        session_dir=resume_session_dir,
        checkpoint_every=args.checkpoint_every,
    )

    try:
        logger.info(f"Starting agent for {args.steps} steps")
        steps_completed = agent.run(num_steps=args.steps)
        logger.info(f"Agent completed {steps_completed} steps")
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, stopping")
    except Exception as e:
        logger.error(f"Error running agent: {e}")
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
