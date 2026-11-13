"""Memory augments chat without changing conversation recovery or reset semantics."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from personal_agent.agent.graph import build_graph
from personal_agent.agent.models import ModelError, ModelReply, ToolCall
from personal_agent.agent.service import AgentRuntime
from personal_agent.agent.tools import ToolDefinition, ToolRegistry
from personal_agent.config import Settings


def runtime_for(tmp_path, model, memory, tools=None):
    saver = InMemorySaver()
    tools = tools or ToolRegistry()
    return AgentRuntime(
        settings=Settings(_env_file=None, persona_path=tmp_path / "missing.md"),
        graph=build_graph(model=model, tools=tools, checkpointer=saver),
        checkpointer=saver, tools=tools,
        skills=SimpleNamespace(list_active=AsyncMock(return_value=[])), memory=memory,
    )


class Model:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def respond(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            self.fail = False
            raise ModelError("Temporary failure")
        return ModelReply(
            items=[{"role": "assistant", "content": "Answer."}],
            text="Answer.", tool_calls=[],
        )


def fake_memory():
    return SimpleNamespace(
        recall_context=AsyncMock(return_value='[{"text":"User likes short answers"}]'),
        enqueue_exchange=AsyncMock(),
    )


def test_memory_is_retrieved_across_resets_and_captures_completed_turns(tmp_path):
    async def check():
        model, memory = Model(), fake_memory()
        runtime = runtime_for(tmp_path, model, memory)
        await runtime.chat("Remember my preference.", "main")
        assert "User likes short answers" in model.calls[-1]["instructions"]
        assert "untrusted" in model.calls[-1]["instructions"]
        first_id = memory.enqueue_exchange.await_args.kwargs["turn_id"]
        reset = await runtime.chat("/new", "main")
        assert "Long-term memory is preserved" in reset.text
        assert memory.enqueue_exchange.await_count == 1
        assert memory.recall_context.await_count == 1
        await runtime.chat("What do you know?", "main")
        assert memory.recall_context.await_count == 2
        assert memory.enqueue_exchange.await_args.kwargs["turn_id"] != first_id
        assert [i["content"] for i in model.calls[-1]["items"] if i.get("role") == "user"] == [
            "What do you know?",
        ]
    asyncio.run(check())


def test_failed_turn_is_captured_only_when_retry_completes(tmp_path):
    async def check():
        model, memory = Model(), fake_memory()
        model.fail = True
        runtime = runtime_for(tmp_path, model, memory)
        with pytest.raises(ModelError):
            await runtime.chat("Original question.", "main")
        assert memory.enqueue_exchange.await_count == 0
        await runtime.chat("/retry", "main")
        assert memory.enqueue_exchange.await_count == 1
        assert memory.enqueue_exchange.await_args.kwargs["user_text"] == "Original question."
    asyncio.run(check())


def test_memory_failures_do_not_fail_completed_chat(tmp_path):
    async def check():
        memory = fake_memory()
        memory.recall_context.side_effect = RuntimeError("Offline")
        memory.enqueue_exchange.side_effect = RuntimeError("Disk unavailable")
        runtime = runtime_for(tmp_path, Model(), memory)
        assert (await runtime.chat("Hello.", "main")).text == "Answer."
    asyncio.run(check())


def test_forget_turn_is_not_automatically_reingested(tmp_path):
    class ForgetModel(Model):
        async def respond(self, **kwargs):
            if not self.calls:
                self.calls.append(kwargs)
                return ModelReply(
                    items=[{"type": "function_call", "call_id": "forget1",
                            "name": "memory__forget", "arguments": "{}"}], text="",
                    tool_calls=[ToolCall("forget1", "memory__forget", "{}")],
                )
            return await super().respond(**kwargs)

    async def check():
        tools, memory = ToolRegistry(), fake_memory()
        handler = AsyncMock(return_value={"status": "delete_pending"})
        tools.register(ToolDefinition(
            id="memory.forget", description="Forget an entry.",
            input_schema={"type": "object", "properties": {}},
            required_scopes=frozenset({"memory:write"}), changes_state=True,
        ), handler)
        runtime = runtime_for(tmp_path, ForgetModel(), memory, tools)
        await runtime.chat("Forget this fact.", "main")
        assert handler.await_count == 1
        assert memory.enqueue_exchange.await_count == 0
        await runtime.chat("A different topic.", "main")
        assert memory.enqueue_exchange.await_count == 1
    asyncio.run(check())


def test_enabling_memory_requires_endpoint_and_secret():
    with pytest.raises(ValueError, match="COGNEE_URL"):
        Settings(_env_file=None, memory_enabled=True)
    with pytest.raises(ValueError, match="COGNEE_API_TOKEN"):
        Settings(_env_file=None, memory_enabled=True, cognee_url="http://cognee:8000")
