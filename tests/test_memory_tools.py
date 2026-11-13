"""Memory tools preserve owner/scopes, queue truth, and mutation recovery."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine

from personal_agent.agent.tools import ToolError, ToolRegistry
from personal_agent.core.context import ExecutionContext
from personal_agent.integrations.cognee.tools import register_memory_tools
from personal_agent.memory.service import MemoryService
from personal_agent.storage.repositories.memory import MemoryOutboxRepository
from personal_agent.storage.repositories.tool_operations import ToolOperationRepository


def test_explicit_memory_tools_are_durable_and_owner_scoped(tmp_path):
    async def scenario():
        engine = create_engine(f"sqlite:///{tmp_path / 'state.sqlite'}")
        repository = MemoryOutboxRepository(engine)
        operations = ToolOperationRepository(engine)
        await repository.initialize()
        await operations.initialize()
        remote = AsyncMock()
        service = MemoryService(remote, repository)
        tools = ToolRegistry(operations=operations)
        register_memory_tools(tools, service)
        context = ExecutionContext("owner", "run", frozenset({"memory:read", "memory:write"}), "c")

        async def invoke(tool_id, arguments, call_id):
            return await tools.invoke(
                tool_id=tool_id, arguments=arguments, context=context,
                turn_id="turn", call_id=call_id,
            )

        result = await invoke("memory.remember", {"text": "I prefer concise answers."}, "remember")
        assert result["status"] == "pending"
        memory_id = result["memory_id"]
        remote.remember.assert_not_awaited()
        # Same receipt replays the queue acknowledgement without enqueuing another write.
        assert await invoke(
            "memory.remember", {"text": "I prefer concise answers."}, "remember",
        ) == result
        await service.process_once()
        remote.remember.assert_awaited_once()
        assert remote.remember.call_args.kwargs["owner_id"] == "owner"
        remote.recall.return_value = [{"memory_id": memory_id, "text": "concise", "metadata": {}}]
        found = await invoke("memory.search", {"query": "preferences"}, "search")
        assert found["memories"][0]["memory_id"] == memory_id
        forgotten = await invoke("memory.forget", {"memory_id": memory_id}, "forget")
        assert forgotten["status"] == "delete_pending"
        assert await invoke("memory.forget", {"memory_id": memory_id}, "forget") == forgotten
        remote.forget.assert_not_awaited()
        await service.process_once()
        remote.forget.assert_awaited_once_with(owner_id="owner", memory_id=memory_id)
        assert not (await invoke("memory.search", {"query": "preferences"}, "search2"))["memories"]
        denied = ExecutionContext("other", "run", frozenset(), "c")
        with pytest.raises(ToolError, match="authorized"):
            await tools.invoke(
                tool_id="memory.search", arguments={"query": "preferences"}, context=denied,
            )
        assert not tools.definitions(denied)
        engine.dispose()

    asyncio.run(scenario())


def test_mutation_reconciliation_checks_saved_state_without_reexecuting(tmp_path):
    async def scenario():
        engine = create_engine(f"sqlite:///{tmp_path / 'state.sqlite'}")
        repository = MemoryOutboxRepository(engine)
        operations = ToolOperationRepository(engine)
        await repository.initialize()
        await operations.initialize()
        remote = AsyncMock()
        service = MemoryService(remote, repository)
        tools = ToolRegistry(operations=operations)
        register_memory_tools(tools, service)
        context = ExecutionContext("owner", "run", frozenset({"memory:write"}), "c")
        arguments = {"text": "A useful fact"}
        # Simulate successful enqueue followed by interruption before the receipt completes.
        await operations.begin(
            owner_id="owner", conversation_id="c", turn_id="t", call_id="call",
            tool_id="memory.remember", arguments=arguments, changes_state=True,
        )
        queued = await service.enqueue_memory(owner_id="owner", text=arguments["text"])
        recovered = await tools.invoke(
            tool_id="memory.remember", arguments=arguments, context=context,
            turn_id="t", call_id="call",
        )
        assert recovered == queued
        remote.remember.assert_not_awaited()
        engine.dispose()

    asyncio.run(scenario())
