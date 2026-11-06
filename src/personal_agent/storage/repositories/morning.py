"""Persist morning-message claims and the recent text used for variety."""

import asyncio
from datetime import date

from sqlalchemy import Column, Date, MetaData, String, Table, Text, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
morning_messages = Table(
    "morning_messages",
    metadata,
    Column("owner_id", String(128), primary_key=True),
    Column("local_date", Date, primary_key=True),
    Column("text", Text, nullable=False),
)


class MorningMessageRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        await asyncio.to_thread(metadata.create_all, self.engine)

    async def recent(self, owner_id: str) -> dict[date, str]:
        def read() -> dict[date, str]:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    select(morning_messages.c.local_date, morning_messages.c.text)
                    .where(morning_messages.c.owner_id == owner_id)
                    .order_by(morning_messages.c.local_date.desc())
                    .limit(7)
                )
                return {day: text for day, text in rows}

        return await asyncio.to_thread(read)

    async def claim(self, owner_id: str, day: date, text: str) -> bool:
        def write() -> bool:
            try:
                with self.engine.begin() as connection:
                    connection.execute(
                        insert(morning_messages).values(
                            owner_id=owner_id, local_date=day, text=text
                        )
                    )
            except IntegrityError:
                # The primary key is the only integrity constraint for these inputs.
                return False
            return True

        return await asyncio.to_thread(write)
