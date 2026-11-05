"""Reusable instructions are versioned data, separate from executable tools."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SkillRevision:
    skill_id: str
    owner_id: str
    revision: int
    name: str
    description: str
    instructions: str
    required_tool_ids: tuple[str, ...]
    created_at: datetime
