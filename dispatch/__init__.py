"""Modal sandbox dispatch helpers for AI CLI agents.

Importing this package exposes ``dispatch_agent`` at the top level, so
``from dispatch import dispatch_agent`` works as documented.
"""

from dispatch.dispatch import dispatch_agent

__all__ = ["dispatch_agent"]
