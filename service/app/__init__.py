"""Windy Search — agent-credentialed web search.

Single source of truth for `__version__`. `app/main.py` and the /health
endpoint both import from here so version bumps are a one-file change.
"""

__version__ = "0.1.0"
