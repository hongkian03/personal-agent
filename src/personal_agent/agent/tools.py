"""Allowlisted tool registration, argument validation, and invocation policy."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from personal_agent.core.context import ExecutionContext
from personal_agent.storage.repositories.tool_operations import ToolOperationRepository

logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any]]]
ToolReconciler = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any] | None]]


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


class ToolOutcomeUnknown(ToolError):
    """An interrupted mutation needs reconciliation before the workflow can resume."""

    def __init__(self, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id


class ToolRegistry:
    def __init__(
        self, *, timeout_seconds: float = 30, operations: ToolOperationRepository | None = None,
    ) -> None:
        self._entries: dict[str, tuple[ToolDefinition, ToolHandler]] = {}
        self._names: dict[str, str] = {}
        self.timeout_seconds = timeout_seconds
        self.operations = operations
        self._reconcilers: dict[str, ToolReconciler] = {}

    def register(
        self, definition: ToolDefinition, handler: ToolHandler,
        *, reconcile: ToolReconciler | None = None,
    ) -> None:
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
        if reconcile is not None:
            self._reconcilers[definition.id] = reconcile

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
        self, *, tool_id: str, arguments: dict[str, Any], context: ExecutionContext,
        turn_id: str | None = None, call_id: str | None = None,
        allow_new_mutation: bool = True, timeout_seconds: float | None = None,
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
        operation = None
        if self.operations is not None and turn_id is not None and call_id is not None:
            operation, execute = await self.operations.begin(
                owner_id=context.owner_id, conversation_id=context.conversation_id or "",
                turn_id=turn_id, call_id=call_id, tool_id=tool_id,
                arguments=arguments, changes_state=definition.changes_state,
                allow_new_mutation=allow_new_mutation,
            )
            if not execute:
                result = await self._reconcile(operation, context)
                if result is not None:
                    return result
                raise ToolOutcomeUnknown(
                    self._uncertain_message(operation.operation_id, tool_id),
                    operation_id=operation.operation_id,
                )
        elif definition.changes_state and not allow_new_mutation:
            raise ToolOutcomeUnknown(
                "An interrupted legacy action has an unknown outcome; it was not repeated."
            )
        logger.info("tool_started tool=%s run=%s", tool_id, context.run_id)
        try:
            async with asyncio.timeout(
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ):
                result = await handler(arguments, context)
                if operation is not None:
                    await self.operations.complete(operation, result)
        except Exception as exc:
            logger.warning("tool_failed tool=%s run=%s", tool_id, context.run_id)
            if operation is not None and definition.changes_state:
                raise ToolOutcomeUnknown(
                    self._uncertain_message(operation.operation_id, tool_id),
                    operation_id=operation.operation_id,
                ) from exc
            raise
        logger.info("tool_completed tool=%s run=%s", tool_id, context.run_id)
        return result

    @staticmethod
    def _uncertain_message(operation_id: str, tool_id: str) -> str:
        return (
            f"The outcome of {tool_id} is uncertain (operation {operation_id[:12]}). "
            "It was not repeated. Ask the owner to inspect /recovery and resolve that action "
            "with /resolve before retrying it. Do not issue the same action again."
        )

    async def _reconcile(self, operation, context: ExecutionContext) -> dict | None:
        if operation.status == "completed":
            return operation.result
        reconcile = self._reconcilers.get(operation.tool_id)
        if reconcile is not None:
            try:
                result = await reconcile(operation.arguments, context)
                if result is not None:
                    await self.operations.complete(operation, result)
                    return result
            except Exception:
                logger.warning("tool_reconciliation_failed tool=%s", operation.tool_id)
        return None

    async def recover_outputs(
        self, *, context: ExecutionContext, turn_id: str, calls: list,
    ) -> list:
        """Close abandoned tool calls with receipts or explicit skipped/unknown outcomes."""
        outputs = []
        for call in calls:
            result = None
            error = "The previous attempt was closed; this tool was not replayed."
            try:
                tool_id = self.resolve_name(call["name"])
                definition, _ = self._entries[tool_id]
                if not definition.required_scopes <= context.granted_scopes:
                    raise ToolError("Tool is not authorized for this execution")
                if self.operations is not None:
                    operation = await self.operations.get(
                        owner_id=context.owner_id, conversation_id=context.conversation_id or "",
                        turn_id=turn_id, call_id=call["call_id"],
                    )
                    if (
                        operation is None and definition.changes_state
                        and turn_id.startswith("legacy-")
                    ):
                        arguments = json.loads(call["arguments"])
                        if not isinstance(arguments, dict):
                            raise ToolError("Tool arguments must be an object")
                        Draft202012Validator(definition.input_schema).validate(arguments)
                        operation, _ = await self.operations.begin(
                            owner_id=context.owner_id,
                            conversation_id=context.conversation_id or "",
                            turn_id=turn_id, call_id=call["call_id"], tool_id=tool_id,
                            arguments=arguments, changes_state=True,
                            allow_new_mutation=False,
                        )
                    if operation is not None:
                        if operation.status == "not_applied":
                            error = (
                                "The owner confirmed this action was not applied. It was not "
                                "replayed when the previous attempt was closed."
                            )
                        else:
                            result = await self._reconcile(operation, context)
                        if (
                            result is None and operation.changes_state
                            and operation.status != "not_applied"
                        ):
                            error = self._uncertain_message(operation.operation_id, tool_id)
            except (ToolError, KeyError, ValueError, ValidationError):
                error = "The previous tool call could not be recovered and was not replayed."
            output = {"ok": True, "result": result} if result is not None else {
                "ok": False, "error": error,
            }
            outputs.append({
                "type": "function_call_output", "call_id": call["call_id"],
                "output": json.dumps(output, allow_nan=False),
            })
        return outputs

    async def recovery_status(self, *, context: ExecutionContext) -> str:
        if self.operations is None:
            return "No durable tool recovery journal is configured."
        pending = []
        for operation in await self.operations.uncertain(
            owner_id=context.owner_id, conversation_id=context.conversation_id or "",
        ):
            entry = self._entries.get(operation.tool_id)
            if entry is None or not entry[0].required_scopes <= context.granted_scopes:
                continue
            if await self._reconcile(operation, context) is None:
                subject = (
                    operation.arguments.get("skill_id")
                    if isinstance(operation.arguments, dict) else None
                )
                summary = ""
                if isinstance(subject, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", subject):
                    summary = f" (skill {subject}"
                    revision = operation.arguments.get("revision")
                    if operation.tool_id == "skills.save_revision":
                        expected = operation.arguments.get("expected_latest_revision")
                        if expected is None:
                            revision = 1
                        elif isinstance(expected, int):
                            revision = expected + 1
                        else:
                            revision = None
                    if isinstance(revision, int) and revision > 0:
                        summary += f", revision {revision}"
                    summary += ")"
                pending.append(f"{operation.operation_id[:12]} — {operation.tool_id}{summary}")
        if not pending:
            return "There are no uncertain tool actions in this conversation."
        return (
            "These actions have an uncertain outcome and will not be repeated automatically:\n"
            + "\n".join(pending)
            + "\nInspect the actual saved state first. Then use "
            "/resolve <operation-id> applied or /resolve <operation-id> not-applied. "
            "Resolution records your confirmation; it does not execute an action."
        )

    async def resolve_recovery(
        self, *, context: ExecutionContext, operation_id: str, outcome: str,
    ) -> str:
        if self.operations is None:
            raise ToolError("No durable tool recovery journal is configured")
        if "skills:write" not in context.granted_scopes:
            raise ToolError("Action reconciliation is not authorized")
        operation = await self.operations.resolve(
            owner_id=context.owner_id, conversation_id=context.conversation_id or "",
            operation_id=operation_id, outcome=outcome,
        )
        return (
            f"Recorded {operation.operation_id[:12]} as {outcome}. No action was executed. "
            "Use /retry if the interrupted turn is still pending."
        )
