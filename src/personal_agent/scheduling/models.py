"""Persisted schedules pin the definition used to create each occurrence."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ScheduleDefinition:
    schedule_id: str
    owner_id: str
    workflow_id: str
    workflow_revision: int
    cron: str
    timezone: str
    next_due_at: datetime
    missed_run_policy: Literal["skip", "run_latest"] = "skip"
    enabled: bool = True
