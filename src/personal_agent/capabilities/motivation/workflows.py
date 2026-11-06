"""Write one short motivational message during the owner's morning window."""

from datetime import date, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from personal_agent.agent.models import ChatModel
from personal_agent.core.persona import with_persona


class MorningMessages(Protocol):
    async def recent(self, owner_id: str) -> dict[date, str]: ...

    async def claim(self, owner_id: str, day: date, text: str) -> bool: ...


def morning_date(now: datetime) -> date | None:
    return now.date() if now.weekday() < 5 and now.hour == 8 and now.minute < 15 else None


class MorningMessage:
    def __init__(
        self,
        model: ChatModel,
        repository: MorningMessages,
        owner_id: str,
        zone: ZoneInfo,
        *,
        persona_path: Path = Path("config/agent.md"),
    ) -> None:
        self.model = model
        self.repository = repository
        self.owner_id = owner_id
        self.zone = zone
        self.persona_path = persona_path

    async def claim_due(self) -> str | None:
        day = morning_date(datetime.now(self.zone))
        if day is None:
            return None
        recent = await self.repository.recent(self.owner_id)
        if day in recent:
            return None
        reply = await self.model.respond(
            items=[
                {
                    "role": "user",
                    "content": "Write today's morning message. Recent messages to vary from:\n"
                    + "\n".join(recent.values()),
                }
            ],
            instructions=with_persona(
                "Write one or two short sentences of warm, practical morning encouragement. "
                "Suggest one small action for today. Avoid quotes, clichés, and assumptions "
                "about the recipient's circumstances. Return only the message.",
                self.persona_path,
            ),
            tools=(),
        )
        text = reply.text.strip()
        if not text or reply.tool_calls:
            return None
        if morning_date(datetime.now(self.zone)) != day:
            return None
        # Claim before handing text to the bridge. An ambiguous send is never retried.
        return text if await self.repository.claim(self.owner_id, day, text) else None
