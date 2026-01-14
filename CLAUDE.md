# py-lazer-server

Python implementation of osu! lazer server.

## Development

```bash
# Activate virtual environment
source .venv/bin/activate

# Run server
python -m app.main

# Run pre-commit
pre-commit run --all-files
```

## Code Style

### Imports

Always import directly from source modules, never from package `__init__.py` re-exports:

```python
# Good
from app.protocol.models import SpectatorState
from app.protocol.enums import UserStatus
from app.api.hubs.spectator import send_user_score_processed

# Bad - don't import from package level
from app.protocol import SpectatorState
from app.api.hubs import send_user_score_processed
```

Keep `__init__.py` files minimal - only use them for:
- Package docstrings
- Constructing routers/registries that combine submodules
- Never for re-exporting symbols from submodules

### Formatting

Pre-commit hooks handle formatting. Key settings:
- Line length: 110 characters
- isort with `force_single_line = true`
- Trailing commas enforced
