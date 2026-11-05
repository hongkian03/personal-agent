"""Owner-scoped skill persistence, with explicit activation and rollback."""

from typing import Protocol

from personal_agent.skills.models import SkillRevision


class SkillRepository(Protocol):
    async def list_active(self, *, owner_id: str) -> tuple[SkillRevision, ...]: ...

    async def get_revision(
        self, *, owner_id: str, skill_id: str, revision: int
    ) -> SkillRevision: ...

    async def save_revision(
        self, skill: SkillRevision, *, expected_latest_revision: int | None
    ) -> None:
        """Append atomically; reject stale revisions. None requires a new skill ID."""
        ...

    async def activate(self, *, owner_id: str, skill_id: str, revision: int) -> None:
        """Select an existing revision without rewriting history; supports rollback."""
        ...

    async def archive(self, *, owner_id: str, skill_id: str) -> None: ...
