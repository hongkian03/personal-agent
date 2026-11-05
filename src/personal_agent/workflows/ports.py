"""Persistence contracts; schedules reference a pinned workflow revision."""

from typing import Protocol

from personal_agent.workflows.models import WorkflowRevision


class WorkflowRepository(Protocol):
    async def get_revision(
        self, *, owner_id: str, workflow_id: str, revision: int
    ) -> WorkflowRevision: ...

    async def save_revision(
        self, workflow: WorkflowRevision, *, expected_latest_revision: int | None
    ) -> None:
        """Append atomically; None requires a new workflow ID. Saving does not schedule."""
        ...
