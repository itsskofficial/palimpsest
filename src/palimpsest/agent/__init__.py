"""The agent: one loop, a bounded tool surface, memory, and a gate before Notion.

The agent orchestrates; it never writes to your notes directly. It reads, captures,
sweeps and proposes through a small registry of tools, and any edit it wants to make
goes through `palimpsest.approval` — applied only within the autonomy setting, held for
a tap otherwise. That division is the whole safety story: the agent has judgement over
*what to do* and none over *what reaches Notion*.
"""

from __future__ import annotations

from palimpsest.agent.context import ToolContext
from palimpsest.agent.registry import build_registry

__all__ = ["ToolContext", "build_registry"]
