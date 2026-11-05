"""Model-neutral conversation and function-call contracts."""

from dataclasses import dataclass
from typing import Any, Protocol

from personal_agent.agent.tools import ToolDefinition


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


class ModelError(RuntimeError):
    """A model request failed or did not produce a complete usable response."""


class ChatModel(Protocol):
    async def respond(
        self,
        *,
        items: list[dict[str, Any]],
        instructions: str,
        tools: tuple[ToolDefinition, ...],
    ) -> ModelReply: ...
