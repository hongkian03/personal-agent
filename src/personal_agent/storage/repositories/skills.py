"""SQLAlchemy skill revisions and owner-scoped activation pointers.

Tables are created at startup for this initial runtime. Replace create_all with Alembic
migrations before evolving an existing deployed schema.
"""

import asyncio
from datetime import timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from personal_agent.skills.models import SkillRevision

metadata = MetaData()
heads = Table(
    "skill_heads",
    metadata,
    Column("owner_id", String(200), primary_key=True),
    Column("skill_id", String(120), primary_key=True),
    Column("latest_revision", Integer, nullable=False),
    Column("active_revision", Integer, nullable=True),
)
revisions = Table(
    "skill_revisions",
    metadata,
    Column("owner_id", String(200), primary_key=True),
    Column("skill_id", String(120), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("instructions", Text, nullable=False),
    Column("required_tool_ids", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
imports = Table(
    "skill_imports",
    metadata,
    Column("owner_id", String(200), primary_key=True),
    Column("skill_id", String(120), primary_key=True),
    Column("source_digest", String(64), nullable=False),
    Column("revision", Integer, nullable=False),
)


class SkillConflictError(ValueError):
    """The expected latest revision no longer matches the saved skill."""


def _identity(owner_id: str, skill_id: str):
    return and_(heads.c.owner_id == owner_id, heads.c.skill_id == skill_id)


def _deserialize(row) -> SkillRevision:
    values = dict(row)
    values["required_tool_ids"] = tuple(values["required_tool_ids"])
    if values["created_at"].tzinfo is None:
        values["created_at"] = values["created_at"].replace(tzinfo=timezone.utc)
    return SkillRevision(**values)


class SqlSkillRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        await asyncio.to_thread(metadata.create_all, self.engine)

    async def import_revision(
        self, skill: SkillRevision, *, source_digest: str, activate: bool
    ) -> tuple[int, bool]:
        def apply():
            try:
                with self.engine.begin() as connection:
                    identity = _identity(skill.owner_id, skill.skill_id)
                    import_identity = and_(
                        imports.c.owner_id == skill.owner_id,
                        imports.c.skill_id == skill.skill_id,
                    )
                    previous = (
                        connection.execute(select(imports).where(import_identity))
                        .mappings()
                        .first()
                    )
                    if previous is not None and previous["source_digest"] == source_digest:
                        # Preserve conversational edits, rollbacks and archives on restarts.
                        return previous["revision"], False
                    head = connection.execute(select(heads).where(identity)).mappings().first()
                    revision = 1 if head is None else head["latest_revision"] + 1
                    if head is None:
                        connection.execute(
                            insert(heads).values(
                                owner_id=skill.owner_id,
                                skill_id=skill.skill_id,
                                latest_revision=revision,
                                active_revision=revision if activate else None,
                            )
                        )
                    else:
                        values = {"latest_revision": revision}
                        if activate:
                            values["active_revision"] = revision
                        result = connection.execute(
                            update(heads)
                            .where(identity, heads.c.latest_revision == head["latest_revision"])
                            .values(**values)
                        )
                        if result.rowcount != 1:
                            raise SkillConflictError("Skill changed during import; retry")
                    connection.execute(
                        insert(revisions).values(
                            owner_id=skill.owner_id,
                            skill_id=skill.skill_id,
                            revision=revision,
                            name=skill.name,
                            description=skill.description,
                            instructions=skill.instructions,
                            required_tool_ids=list(skill.required_tool_ids),
                            created_at=skill.created_at,
                        )
                    )
                    stamp = {"source_digest": source_digest, "revision": revision}
                    if previous is None:
                        connection.execute(
                            insert(imports).values(
                                owner_id=skill.owner_id, skill_id=skill.skill_id, **stamp
                            )
                        )
                    else:
                        connection.execute(update(imports).where(import_identity).values(**stamp))
                    return revision, True
            except IntegrityError as error:
                raise SkillConflictError("Concurrent skill import; retry") from error

        return await asyncio.to_thread(apply)

    async def list_active(self, *, owner_id: str) -> tuple[SkillRevision, ...]:
        def read():
            statement = (
                select(revisions)
                .join(
                    heads,
                    and_(
                        revisions.c.owner_id == heads.c.owner_id,
                        revisions.c.skill_id == heads.c.skill_id,
                        revisions.c.revision == heads.c.active_revision,
                    ),
                )
                .where(heads.c.owner_id == owner_id)
                .order_by(revisions.c.skill_id)
            )
            with self.engine.connect() as connection:
                return tuple(_deserialize(row) for row in connection.execute(statement).mappings())

        return await asyncio.to_thread(read)

    async def get_revision(self, *, owner_id: str, skill_id: str, revision: int) -> SkillRevision:
        def read():
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        select(revisions).where(
                            revisions.c.owner_id == owner_id,
                            revisions.c.skill_id == skill_id,
                            revisions.c.revision == revision,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise LookupError("Skill revision not found")
                return _deserialize(row)

        return await asyncio.to_thread(read)

    async def save_revision(
        self, skill: SkillRevision, *, expected_latest_revision: int | None
    ) -> None:
        expected_next = 1 if expected_latest_revision is None else expected_latest_revision + 1
        if skill.revision != expected_next:
            raise SkillConflictError(
                "Revision must immediately follow the expected latest revision"
            )

        def save():
            try:
                with self.engine.begin() as connection:
                    if expected_latest_revision is None:
                        connection.execute(
                            insert(heads).values(
                                owner_id=skill.owner_id,
                                skill_id=skill.skill_id,
                                latest_revision=skill.revision,
                                active_revision=None,
                            )
                        )
                    else:
                        result = connection.execute(
                            update(heads)
                            .where(
                                _identity(skill.owner_id, skill.skill_id),
                                heads.c.latest_revision == expected_latest_revision,
                            )
                            .values(latest_revision=skill.revision)
                        )
                        if result.rowcount != 1:
                            raise SkillConflictError("Skill changed; reload before saving")
                    connection.execute(
                        insert(revisions).values(
                            owner_id=skill.owner_id,
                            skill_id=skill.skill_id,
                            revision=skill.revision,
                            name=skill.name,
                            description=skill.description,
                            instructions=skill.instructions,
                            required_tool_ids=list(skill.required_tool_ids),
                            created_at=skill.created_at,
                        )
                    )
            except IntegrityError as error:
                raise SkillConflictError("Skill revision already exists") from error

        await asyncio.to_thread(save)

    async def activate(self, *, owner_id: str, skill_id: str, revision: int) -> None:
        def activate():
            with self.engine.begin() as connection:
                exists = connection.execute(
                    select(revisions.c.revision).where(
                        revisions.c.owner_id == owner_id,
                        revisions.c.skill_id == skill_id,
                        revisions.c.revision == revision,
                    )
                ).first()
                if exists is None:
                    raise LookupError("Skill revision not found")
                result = connection.execute(
                    update(heads)
                    .where(_identity(owner_id, skill_id))
                    .values(active_revision=revision)
                )
                if result.rowcount != 1:
                    raise LookupError("Skill not found")

        await asyncio.to_thread(activate)

    async def archive(self, *, owner_id: str, skill_id: str) -> None:
        def archive():
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(heads).where(_identity(owner_id, skill_id)).values(active_revision=None)
                )
                if result.rowcount != 1:
                    raise LookupError("Skill not found")

        await asyncio.to_thread(archive)
