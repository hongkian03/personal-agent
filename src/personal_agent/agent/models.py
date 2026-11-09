"""Model-neutral conversation and function-call contracts."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from personal_agent.agent.tools import ToolDefinition


@dataclass(frozen=True)
class ActivityEvent:
    """Coarse execution activity, without provider payloads or user data."""

    phase: str
    tool_id: str | None = None


ActivityCallback = Callable[[ActivityEvent], None]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    items: list[dict[str, Any]]
    text: str
    tool_calls: list[ToolCall]
    # Count returned hosted-tool records conservatively, including unfinished attempts.
    hosted_tool_calls: int = 0


class ModelError(RuntimeError):
    """A model request failed or did not produce a complete usable response."""


class ChatModel(Protocol):
    async def respond(
        self,
        *,
        items: list[dict[str, Any]],
        instructions: str,
        tools: tuple[ToolDefinition, ...],
        hosted_tools: frozenset[str] = frozenset(),
        max_hosted_tool_calls: int = 3,
        on_activity: ActivityCallback | None = None,
    ) -> ModelReply: ...
