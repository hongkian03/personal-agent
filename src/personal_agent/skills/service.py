"""Validate and manage owner-scoped, immutable instruction revisions."""

import re
from datetime import datetime, timezone

from personal_agent.skills.discovery import available_skills
from personal_agent.skills.models import SkillRevision
from personal_agent.skills.ports import SkillRepository


class SkillService:
    def __init__(self, repository: SkillRepository, available_tool_ids: set[str]) -> None:
        self.repository = repository
        self.available_tool_ids = frozenset(available_tool_ids)

    def validate(self, skill: SkillRevision) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", skill.skill_id):
            raise ValueError(
                "Skill ID must be 1-120 lowercase letters, digits, hyphens or underscores"
            )
        if not skill.owner_id or len(skill.owner_id) > 200:
            raise ValueError("Invalid skill owner")
        for value, limit, field in (
            (skill.name, 200, "name"),
            (skill.description, 1000, "description"),
            (skill.instructions, 12000, "instructions"),
        ):
            if not value.strip() or len(value) > limit:
                raise ValueError(f"Skill {field} must be nonempty and at most {limit} characters")
        if skill.revision < 1:
            raise ValueError("Revision must be positive")
        if skill.created_at.tzinfo is None:
            raise ValueError("Skill creation time must include a timezone")
        if len(set(skill.required_tool_ids)) != len(skill.required_tool_ids):
            raise ValueError("Required tool IDs must be unique")
        unknown = set(skill.required_tool_ids) - self.available_tool_ids
        if unknown:
            raise ValueError(f"Unknown or unavailable tools: {', '.join(sorted(unknown))}")

    async def save_revision(
        self,
        *,
        owner_id: str,
        skill_id: str,
        name: str,
        description: str,
        instructions: str,
        required_tool_ids: tuple[str, ...],
        expected_latest_revision: int | None = None,
    ) -> SkillRevision:
        if expected_latest_revision is not None and expected_latest_revision < 1:
            raise ValueError("Expected revision must be positive, or null for a new skill")
        skill = SkillRevision(
            skill_id=skill_id,
            owner_id=owner_id,
            revision=1 if expected_latest_revision is None else expected_latest_revision + 1,
            name=name,
            description=description,
            instructions=instructions,
            required_tool_ids=required_tool_ids,
            created_at=datetime.now(timezone.utc),
        )
        self.validate(skill)
        await self.repository.save_revision(
            skill, expected_latest_revision=expected_latest_revision
        )
        return skill

    async def activate(self, *, owner_id: str, skill_id: str, revision: int) -> None:
        skill = await self.repository.get_revision(
            owner_id=owner_id, skill_id=skill_id, revision=revision
        )
        self.validate(skill)
        await self.repository.activate(owner_id=owner_id, skill_id=skill_id, revision=revision)

    async def load(
        self, *, owner_id: str, skill_id: str, available_tool_ids: set[str]
    ) -> SkillRevision:
        """Read only the current active, authorized revision without changing activation."""
        skills = available_skills(
            await self.repository.list_active(owner_id=owner_id),
            owner_id=owner_id,
            available_tool_ids=available_tool_ids,
        )
        for skill in skills:
            if skill.skill_id == skill_id:
                return skill
        raise LookupError("Skill is not active or is unavailable for this execution")

    async def archive(self, *, owner_id: str, skill_id: str) -> None:
        await self.repository.archive(owner_id=owner_id, skill_id=skill_id)
