"""Atomic, owner-scoped portfolio snapshots on SQLite or PostgreSQL."""

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, MetaData, String, Table, Text, select
from sqlalchemy.engine import Engine

from personal_agent.capabilities.portfolio.models import PortfolioReport

metadata = MetaData()
portfolio_snapshots = Table(
    "portfolio_snapshots",
    metadata,
    Column("owner_id", String(128), primary_key=True),
    Column("as_of", Date, primary_key=True),
    Column("period_start", Date, nullable=False),
    Column("payload", Text, nullable=False),
    Column("saved_at", DateTime(timezone=True), nullable=False),
)


class PortfolioRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        await asyncio.to_thread(metadata.create_all, self.engine)

    async def save(self, owner_id: str, report: PortfolioReport) -> bool:
        """Replace a whole report atomically; retain earlier dates for future history.

        An earlier date may be stored, but can never supersede a newer snapshot. The
        return value says whether this report is the latest report for this owner.
        """
        payload = json.dumps(report.to_dict(), ensure_ascii=False, allow_nan=False)

        def write() -> bool:
            dialect = self.engine.dialect.name
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            elif dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                raise ValueError("Portfolio storage requires SQLite or PostgreSQL")
            with self.engine.begin() as connection:
                statement = insert(portfolio_snapshots).values(
                    owner_id=owner_id,
                    as_of=report.as_of,
                    period_start=report.period_start,
                    payload=payload,
                    saved_at=datetime.now(timezone.utc),
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["owner_id", "as_of"],
                        set_={
                            "period_start": statement.excluded.period_start,
                            "payload": statement.excluded.payload,
                            "saved_at": statement.excluded.saved_at,
                        },
                    )
                )
                latest_date = connection.execute(
                    select(portfolio_snapshots.c.as_of)
                    .where(portfolio_snapshots.c.owner_id == owner_id)
                    .order_by(portfolio_snapshots.c.as_of.desc())
                    .limit(1)
                ).scalar_one()
                return latest_date == report.as_of

        return await asyncio.to_thread(write)

    async def latest(self, owner_id: str) -> PortfolioReport | None:
        def read() -> PortfolioReport | None:
            with self.engine.connect() as connection:
                payload = connection.execute(
                    select(portfolio_snapshots.c.payload)
                    .where(portfolio_snapshots.c.owner_id == owner_id)
                    .order_by(portfolio_snapshots.c.as_of.desc())
                    .limit(1)
                ).scalar_one_or_none()
                return None if payload is None else PortfolioReport.from_dict(json.loads(payload))

        return await asyncio.to_thread(read)
