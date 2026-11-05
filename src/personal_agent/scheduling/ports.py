"""Schedule persistence; claiming and enqueueing must avoid duplicate occurrences."""

from typing import Protocol

from personal_agent.scheduling.models import ScheduleDefinition


class ScheduleRepository(Protocol):
    async def save(self, schedule: ScheduleDefinition) -> None: ...

    async def disable(self, *, owner_id: str, schedule_id: str) -> None: ...
