# Configuration for Letta Plays Pokemon

# Letta model handle. If Opus 4.7 is not yet available on your Letta Cloud
# account, fall back to "anthropic/claude-opus-4-6".
LETTA_MODEL = "anthropic/claude-opus-4-7"

# Embedding model used by Letta for archival memory.
LETTA_EMBEDDING = "openai/text-embedding-3-small"

# Max in-context size for the agent. Letta auto-compacts older messages.
CONTEXT_WINDOW_LIMIT = 32000

# Whether to expose the navigate_to action (A* pathfinder over the collision map).
USE_NAVIGATOR = False

# Whether to expose the soft_reset action. When False, the dispatch path is
# disabled and the persona does not document it.
USE_SOFT_RESET = False

# Default number of agent steps if --steps is not passed.
MAX_STEPS_DEFAULT = 10
