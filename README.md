# Letta Plays Pokemon

A [Letta](https://letta.com) agent learns to play Pokemon Red on a Game Boy
emulator -- with persistent, editable memory blocks instead of a manually
managed conversation history.

This is a fork of [Claude Plays Pokemon](https://github.com/davidhershey/ClaudePlaysPokemonStarter)
with the Anthropic API call replaced by Letta. The agent starts knowing almost
nothing about Pokemon and is expected to learn by talking to NPCs, reading
signs, and writing what it discovers into its own memory blocks.

## How it works

A simple loop in `main.py` drives the experiment:

1. Capture a screenshot, RAM-derived game state, and a collision map from PyBoy.
2. Send those to the Letta agent as a single user message.
3. The agent reasons, optionally edits its own memory blocks, and replies with
   a fenced ```json block describing its next action.
4. We parse the action and execute it on the emulator.
5. Loop.

The agent has these memory blocks (defined in `agent/persona.py`):

- `persona` -- identity, button semantics, and the action protocol
- `goals` -- what it's trying to do
- `current_team`, `boxed_pokemon`, `items` -- inventory it's learned about
- `active_battle` -- opponent + strategy during battles
- `action_trajectory` -- rolling buffer of recent actions (anti-loop)
- `map_knowledge`, `npc_notes`, `lessons_learned` -- accumulated game knowledge

Most blocks start empty. The agent fills them in as it plays.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and a [Letta Cloud](https://app.letta.com)
API key.

```bash
uv sync
cp .env.example .env
# edit .env and set LETTA_API_KEY
```

Place a Pokemon Red ROM at `pokemon.gb` in the project root (you provide your
own ROM).

## Run

```bash
uv run python main.py --steps 50
```

Optional flags:

- `--rom PATH` -- path to the ROM (default: `pokemon.gb`)
- `--steps N` -- number of agent turns (default: 10)
- `--display` -- show the emulator window
- `--sound` -- enable sound (only with `--display`)
- `--load-state PATH` -- load a PyBoy save state

Each run creates a **fresh** Letta agent. The agent ID is logged at startup;
you can inspect its memory blocks at `https://app.letta.com/agents/<agent_id>`.

## Configuration

`config.py` controls:

- `LETTA_MODEL` -- model handle (default: `anthropic/claude-opus-4-7`; fall
  back to `anthropic/claude-opus-4-6` if 4.7 isn't available on your account)
- `LETTA_EMBEDDING` -- embedding model
- `CONTEXT_WINDOW_LIMIT` -- max in-context size
- `USE_NAVIGATOR` -- enable A\* `navigate_to` action

## Layout

```
agent/
  emulator.py       PyBoy wrapper (button presses, screenshot, collision map)
  memory_reader.py  Pokemon Red RAM lookups
  persona.py        Persona text + INITIAL_BLOCKS for the Letta agent
  letta_agent.py    Main loop driver (Shape A: we drive, agent decides)
config.py           Model and run configuration
main.py             CLI entry point
```

## Future work: Letta tools (Shape B)

Today the agent emits structured JSON which we parse and dispatch. The
dispatch handlers in `letta_agent.py` are deliberately isolated so they can
be wrapped as Letta tools later, letting the agent call them directly with
no JSON parsing on our side.
