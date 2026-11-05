"""Provider-independent data exchanged between modules."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    sender_id: str
    conversation_id: str
    text: str
    received_at: datetime


@dataclass(frozen=True)
class OutgoingMessage:
    recipient_id: str
    text: str
    idempotency_key: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class MessageSubmission:
    """Provider acceptance, not proof of delivery."""

    provider_message_id: str


@dataclass(frozen=True)
class Briefing:
    text: str
    as_of: datetime
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowRequest:
    """Stable execution identity, shared by retries of the same scheduled occurrence."""

    run_id: str
    user_id: str
    scheduled_for: datetime
    workflow_revision: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
