"""Interfaces implemented by infrastructure adapters, injected at startup."""

from typing import Any, Protocol

from personal_agent.core.models import (
    MessageSubmission,
    OutgoingMessage,
    WorkflowRequest,
)


class Messenger(Protocol):
    async def send(self, message: OutgoingMessage) -> MessageSubmission: ...


class PreferencesRepository(Protocol):
    async def get(self, user_id: str, capability_id: str) -> dict[str, Any]: ...


class TextGenerator(Protocol):
    async def generate(self, *, instructions: str, context: str) -> str: ...


class JobDispatcher(Protocol):
    async def enqueue(self, workflow_id: str, request: WorkflowRequest) -> str: ...
