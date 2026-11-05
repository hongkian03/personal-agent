"""Trusted execution context supplied by the application, never by model arguments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    owner_id: str
    run_id: str
    granted_scopes: frozenset[str]
    conversation_id: str | None = None
