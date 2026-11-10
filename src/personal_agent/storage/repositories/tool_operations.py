"""Durable per-call receipts, scoped to the owner and conversation."""

import asyncio
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import JSON, Boolean, Column, MetaData, String, Table, and_, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
operations = Table(
    "tool_operations", metadata,
    Column("operation_id", String(64), primary_key=True),
    Column("owner_id", String(200), nullable=False, index=True),
    Column("conversation_id", String(128), nullable=False),
    Column("turn_id", String(128), nullable=False),
    Column("call_id", String(256), nullable=False),
    Column("tool_id", String(128), nullable=False),
    Column("arguments_hash", String(64), nullable=False),
    Column("arguments", JSON, nullable=False),
    Column("changes_state", Boolean, nullable=False),
    Column("status", String(20), nullable=False),
    Column("result", JSON, nullable=True),
)


@dataclass(frozen=True)
class ToolOperation:
    operation_id: str
    owner_id: str
    conversation_id: str
    turn_id: str
    call_id: str
    tool_id: str
    arguments_hash: str
    arguments: dict
    changes_state: bool
    status: str
    result: dict | None


def operation_identity(owner_id: str, conversation_id: str, turn_id: str, call_id: str) -> str:
    encoded = json.dumps([owner_id, conversation_id, turn_id, call_id]).encode()
    return hashlib.sha256(encoded).hexdigest()


class ToolOperationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        await asyncio.to_thread(metadata.create_all, self.engine)

    async def begin(
        self, *, owner_id: str, conversation_id: str, turn_id: str, call_id: str,
        tool_id: str, arguments: dict, changes_state: bool, allow_new_mutation: bool = True,
    ) -> tuple[ToolOperation, bool]:
        """Reserve before execution. Existing writes require evidence or owner resolution."""
        identity = operation_identity(owner_id, conversation_id, turn_id, call_id)
        args_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

        def reserve():
            with self.engine.begin() as connection:
                row = connection.execute(
                    select(operations).where(operations.c.operation_id == identity)
                ).mappings().first()
                if row is not None:
                    receipt = ToolOperation(**row)
                    if (
                        receipt.tool_id != tool_id or receipt.arguments_hash != args_hash
                        or receipt.changes_state != changes_state
                    ):
                        raise ValueError("Saved tool call identity does not match its arguments")
                    if receipt.status == "not_applied":
                        changed = connection.execute(update(operations).where(
                            operations.c.operation_id == identity,
                            operations.c.status == "not_applied",
                        ).values(status="started", result=None))
                        if changed.rowcount == 1:
                            return ToolOperation(**{
                                **row, "status": "started", "result": None,
                            }), True
                        current = connection.execute(select(operations).where(
                            operations.c.operation_id == identity
                        )).mappings().one()
                        return ToolOperation(**current), False
                    return receipt, receipt.status == "started" and not receipt.changes_state
                if changes_state:
                    uncertain = connection.execute(select(operations).where(
                        operations.c.owner_id == owner_id,
                        operations.c.conversation_id == conversation_id,
                        operations.c.tool_id == tool_id,
                        operations.c.arguments_hash == args_hash,
                        operations.c.status.in_(("started", "unknown")),
                        operations.c.changes_state.is_(True),
                    )).mappings().first()
                    if uncertain is not None:
                        return ToolOperation(**uncertain), False
                status = "unknown" if changes_state and not allow_new_mutation else "started"
                values = dict(
                    operation_id=identity, owner_id=owner_id, conversation_id=conversation_id,
                    turn_id=turn_id, call_id=call_id, tool_id=tool_id, arguments_hash=args_hash,
                    arguments=arguments, changes_state=changes_state, status=status, result=None,
                )
                connection.execute(insert(operations).values(**values))
                return ToolOperation(**values), status == "started"

        try:
            return await asyncio.to_thread(reserve)
        except IntegrityError:
            # A concurrent reservation must never cause a duplicate mutation.
            return await asyncio.to_thread(reserve)

    async def complete(self, operation: ToolOperation, result: dict) -> None:
        json.dumps(result, allow_nan=False)

        def write():
            with self.engine.begin() as connection:
                connection.execute(update(operations).where(
                    operations.c.operation_id == operation.operation_id,
                    operations.c.owner_id == operation.owner_id,
                    operations.c.status.in_(("started", "unknown")),
                ).values(status="completed", result=result))
        await asyncio.to_thread(write)

    async def get(
        self, *, owner_id: str, conversation_id: str, turn_id: str, call_id: str,
    ) -> ToolOperation | None:
        identity = operation_identity(owner_id, conversation_id, turn_id, call_id)

        def read():
            with self.engine.connect() as connection:
                row = connection.execute(select(operations).where(
                    operations.c.operation_id == identity,
                    operations.c.owner_id == owner_id,
                )).mappings().first()
                return ToolOperation(**row) if row is not None else None
        return await asyncio.to_thread(read)

    async def uncertain(self, *, owner_id: str, conversation_id: str) -> list[ToolOperation]:
        def read():
            with self.engine.connect() as connection:
                rows = connection.execute(select(operations).where(
                    operations.c.owner_id == owner_id,
                    operations.c.conversation_id == conversation_id,
                    operations.c.changes_state.is_(True),
                    operations.c.status.in_(("started", "unknown")),
                ).order_by(operations.c.operation_id)).mappings()
                return [ToolOperation(**row) for row in rows]
        return await asyncio.to_thread(read)

    async def resolve(
        self, *, owner_id: str, conversation_id: str, operation_id: str, outcome: str,
    ) -> ToolOperation:
        if outcome not in {"applied", "not-applied"}:
            raise ValueError("Outcome must be applied or not-applied")
        if not 12 <= len(operation_id) <= 64 or any(
            c not in "0123456789abcdef" for c in operation_id
        ):
            raise ValueError("Use an operation ID or its unique prefix of at least 12 characters")

        def write():
            with self.engine.begin() as connection:
                rows = connection.execute(select(operations).where(
                    operations.c.owner_id == owner_id,
                    operations.c.conversation_id == conversation_id,
                    operations.c.operation_id.startswith(operation_id),
                    operations.c.changes_state.is_(True),
                    operations.c.status.in_(("started", "unknown")),
                )).mappings().all()
                if len(rows) != 1:
                    raise ValueError("No unique unresolved operation matches in this conversation")
                row = rows[0]
                result = {"reconciled": True, "outcome": "owner_confirmed_applied"}
                status = "completed" if outcome == "applied" else "not_applied"
                connection.execute(update(operations).where(and_(
                    operations.c.operation_id == row["operation_id"],
                    operations.c.status.in_(("started", "unknown")),
                )).values(status=status, result=result if outcome == "applied" else None))
                return ToolOperation(**{
                    **row, "status": status,
                    "result": result if outcome == "applied" else None,
                })
        return await asyncio.to_thread(write)
