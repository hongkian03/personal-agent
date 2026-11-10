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
    """Application-authored failure text plus safe recovery and diagnostic metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "model_error",
        retryable: bool = False,
        response_status: str | None = None,
        incomplete_reason: str | None = None,
        provider_request_id: str | None = None,
        response_id: str | None = None,
        http_status: int | None = None,
        hosted_tool_calls: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.response_status = response_status
        self.incomplete_reason = incomplete_reason
        self.provider_request_id = provider_request_id
        self.response_id = response_id
        self.http_status = http_status
        self.hosted_tool_calls = hosted_tool_calls


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
