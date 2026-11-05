"""Versioned, sequential compositions of existing registered tools."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SkillReference:
    skill_id: str
    revision: int


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class WorkflowRevision:
    workflow_id: str
    owner_id: str
    revision: int
    name: str
    steps: tuple[WorkflowStep, ...]
    skills: tuple[SkillReference, ...]
    created_at: datetime
