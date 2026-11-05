"""Allowlisted tool registration, argument validation, and invocation policy."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from personal_agent.core.context import ExecutionContext

logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    description: str
    input_schema: dict[str, Any]
    required_scopes: frozenset[str]
    changes_state: bool = False

    @property
    def model_name(self) -> str:
        return self.id.replace(".", "__")


class ToolInvoker(Protocol):
    async def invoke(
        self, *, tool_id: str, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]: ...


class ToolError(ValueError):
    """A tool request was rejected before execution."""


class ToolRegistry:
    def __init__(self, *, timeout_seconds: float = 30) -> None:
        self._entries: dict[str, tuple[ToolDefinition, ToolHandler]] = {}
        self._names: dict[str, str] = {}
        self.timeout_seconds = timeout_seconds

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", definition.id):
            raise ValueError(
                "Tool identifiers must use letters, numbers, underscores, dots or dashes"
            )
        if len(definition.model_name) > 64:
            raise ValueError("Encoded tool name exceeds 64 characters")
        if definition.id in self._entries or definition.model_name in self._names:
            raise ValueError(f"Duplicate tool or encoded name: {definition.id}")
        Draft202012Validator.check_schema(definition.input_schema)
        if definition.input_schema.get("type") != "object":
            raise ValueError("Tool input schema must describe an object")
        self._entries[definition.id] = (definition, handler)
        self._names[definition.model_name] = definition.id

    def definitions(self, context: ExecutionContext) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition, _ in self._entries.values()
            if definition.required_scopes <= context.granted_scopes
        )

    def resolve_name(self, model_name: str) -> str:
        try:
            return self._names[model_name]
        except KeyError:
            raise ToolError("Unknown tool") from None

    async def invoke(
        self, *, tool_id: str, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        entry = self._entries.get(tool_id)
        if entry is None:
            raise ToolError("Unknown tool")
        definition, handler = entry
        if not definition.required_scopes <= context.granted_scopes:
            raise ToolError("Tool is not authorized for this execution")
        try:
            Draft202012Validator(definition.input_schema).validate(arguments)
        except ValidationError:
            raise ToolError("Arguments do not match the tool input schema") from None
        logger.info("tool_started tool=%s run=%s", tool_id, context.run_id)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await handler(arguments, context)
        except Exception:
            logger.warning("tool_failed tool=%s run=%s", tool_id, context.run_id)
            raise
        logger.info("tool_completed tool=%s run=%s", tool_id, context.run_id)
        return result
