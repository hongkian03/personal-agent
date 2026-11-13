"""Single-worker durable memory outbox and permanent deletion tombstones."""

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
memory_outbox = Table(
    "memory_outbox", metadata,
    Column("owner_id", String(200), primary_key=True),
    Column("memory_id", String(36), primary_key=True),
    Column("text", Text, nullable=False),
    Column("source", JSON, nullable=False),
    Column("status", String(24), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", Float, nullable=False),
)


@dataclass(frozen=True)
class MemoryWrite:
    owner_id: str
    memory_id: str
    text: str
    source: dict
    status: str
    attempts: int
    available_at: float


class MemoryOutboxRepository:
    """Persist work before HTTP calls. Run only one consumer per application database."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        await asyncio.to_thread(metadata.create_all, self.engine)

    async def get(self, *, owner_id: str, memory_id: str) -> MemoryWrite | None:
        def read():
            with self.engine.connect() as connection:
                row = connection.execute(select(memory_outbox).where(
                    memory_outbox.c.owner_id == owner_id,
                    memory_outbox.c.memory_id == memory_id,
                )).mappings().first()
                return MemoryWrite(**row) if row else None
        return await asyncio.to_thread(read)

    async def enqueue(self, *, owner_id: str, memory_id: str, text: str, source: dict) -> None:
        def write():
            with self.engine.begin() as connection:
                connection.execute(insert(memory_outbox).values(
                    owner_id=owner_id, memory_id=memory_id, text=text, source=source,
                    status="pending", attempts=0, available_at=time.time(),
                ))
        try:
            await asyncio.to_thread(write)
        except IntegrityError:
            # IDs are immutable: neither replay nor an old capture resurrects a tombstone.
            if await self.get(owner_id=owner_id, memory_id=memory_id) is None:
                raise

    async def tombstone(self, *, owner_id: str, memory_id: str) -> None:
        def write():
            with self.engine.begin() as connection:
                row = connection.execute(select(memory_outbox.c.status).where(
                    memory_outbox.c.owner_id == owner_id,
                    memory_outbox.c.memory_id == memory_id,
                )).first()
                if row:
                    if row.status == "forgotten":
                        return
                    connection.execute(update(memory_outbox).where(
                        memory_outbox.c.owner_id == owner_id,
                        memory_outbox.c.memory_id == memory_id,
                    ).values(
                        status="delete_pending", text="", source={},
                        attempts=0, available_at=time.time(),
                    ))
                else:
                    connection.execute(insert(memory_outbox).values(
                        owner_id=owner_id, memory_id=memory_id, text="", source={},
                        status="delete_pending", attempts=0, available_at=time.time(),
                    ))
        try:
            await asyncio.to_thread(write)
        except IntegrityError:
            # A concurrent enqueue may insert the row after our lookup.
            await asyncio.to_thread(write)

    async def next_ready(self) -> MemoryWrite | None:
        def read():
            with self.engine.connect() as connection:
                row = connection.execute(select(memory_outbox).where(
                    memory_outbox.c.status.in_(("pending", "delete_pending")),
                    memory_outbox.c.available_at <= time.time(),
                ).order_by(memory_outbox.c.available_at).limit(1)).mappings().first()
                return MemoryWrite(**row) if row else None
        return await asyncio.to_thread(read)

    async def finish(self, item: MemoryWrite) -> None:
        def write():
            with self.engine.begin() as connection:
                connection.execute(update(memory_outbox).where(
                    memory_outbox.c.owner_id == item.owner_id,
                    memory_outbox.c.memory_id == item.memory_id,
                    memory_outbox.c.status == item.status,
                ).values(
                    status="forgotten" if item.status == "delete_pending" else "completed",
                    text="", source={},
                ))
        await asyncio.to_thread(write)

    async def retry(self, item: MemoryWrite, *, delay_seconds: float) -> None:
        def write():
            with self.engine.begin() as connection:
                connection.execute(update(memory_outbox).where(
                    memory_outbox.c.owner_id == item.owner_id,
                    memory_outbox.c.memory_id == item.memory_id,
                    memory_outbox.c.status == item.status,
                ).values(
                    attempts=item.attempts + 1, available_at=time.time() + delay_seconds,
                ))
        await asyncio.to_thread(write)

    async def deleted_ids(self, *, owner_id: str) -> set[str]:
        def read():
            with self.engine.connect() as connection:
                return set(connection.execute(select(memory_outbox.c.memory_id).where(
                    memory_outbox.c.owner_id == owner_id,
                    memory_outbox.c.status.in_(("delete_pending", "forgotten")),
                )).scalars())
        return await asyncio.to_thread(read)
