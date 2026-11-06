"""Lifecycle-managed checkpoint and skill persistence.

SQLite enables a local single-process runtime without a database service. PostgreSQL
uses the same interfaces for a hosted runtime. Startup initializes tables; schema evolution
will require migrations rather than relying on create_all.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from personal_agent.storage.repositories.morning import MorningMessageRepository
from personal_agent.storage.repositories.portfolio import PortfolioRepository
from personal_agent.storage.repositories.skills import SqlSkillRepository


@dataclass(frozen=True)
class Storage:
    checkpointer: Any
    skills: SqlSkillRepository
    morning: MorningMessageRepository
    portfolio: PortfolioRepository


@asynccontextmanager
async def open_storage(*, database_url: str | None, data_dir: Path):
    async with AsyncExitStack() as stack:
        if database_url:
            url = make_url(database_url)
            if url.get_backend_name() not in {"postgres", "postgresql"}:
                raise ValueError("Database URL must use PostgreSQL; omit it for local SQLite")
            raw_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(raw_url))
            engine = create_engine(url.set(drivername="postgresql+psycopg"), pool_pre_ping=True)
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            saver = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(data_dir / "checkpoints.sqlite"))
            )
            engine = create_engine(
                f"sqlite:///{data_dir / 'skills.sqlite'}",
                connect_args={"check_same_thread": False},
            )
        stack.push_async_callback(asyncio.to_thread, engine.dispose)
        await saver.setup()
        skills = SqlSkillRepository(engine)
        await skills.initialize()
        morning = MorningMessageRepository(engine)
        await morning.initialize()
        portfolio = PortfolioRepository(engine)
        await portfolio.initialize()
        yield Storage(checkpointer=saver, skills=skills, morning=morning, portfolio=portfolio)
