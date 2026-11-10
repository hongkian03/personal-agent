"""Durable tool receipts prevent duplicate mutations while preserving recovery."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine

from personal_agent.agent.graph import build_graph
from personal_agent.agent.models import ModelReply, ToolCall
from personal_agent.agent.service import AgentRuntime
from personal_agent.agent.tools import ToolDefinition, ToolOutcomeUnknown, ToolRegistry
from personal_agent.bootstrap import build_tools
from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext
from personal_agent.storage.database import open_storage
from personal_agent.storage.repositories.tool_operations import ToolOperationRepository


def definition(name="skills.write", *, mutates=True):
    return ToolDefinition(
        id=name, description="Test operation.",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                      "required": ["value"], "additionalProperties": False},
        required_scopes=frozenset({"skills:write"}), changes_state=mutates,
    )


def context(owner="alice", conversation="main"):
    return ExecutionContext(owner, "request", frozenset({"skills:write"}), conversation)


async def setup_registry(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}", connect_args={"check_same_thread": False},
    )
    repository = ToolOperationRepository(engine)
    await repository.initialize()
    return ToolRegistry(operations=repository), repository, engine


def test_completed_receipts_survive_restart_and_reuse_results(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        invoked = []

        async def handler(arguments, ctx):
            invoked.append(ctx.owner_id)
            return {"saved": arguments["value"]}

        try:
            registry.register(definition(), handler)
            arguments = dict(tool_id="skills.write", arguments={"value": "data"},
                             context=context(), turn_id="turn", call_id="call")
            assert await registry.invoke(**arguments) == {"saved": "data"}
            restarted = ToolRegistry(operations=ToolOperationRepository(engine))
            restarted.register(definition(), handler)
            assert await restarted.invoke(**arguments) == {"saved": "data"}
            assert invoked == ["alice"]
            assert await restarted.invoke(**{**arguments, "context": context("bob")}) == {
                "saved": "data"
            }
            assert invoked == ["alice", "bob"]
            with pytest.raises(ValueError, match="identity"):
                await restarted.invoke(**{**arguments, "arguments": {"value": "different"}})
        finally:
            engine.dispose()
    asyncio.run(check())


def test_unknown_mutation_blocks_matching_new_calls_until_explicit_resolution(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        invoked = []

        async def handler(arguments, ctx):
            invoked.append(arguments)
            if len(invoked) == 1:
                raise RuntimeError("private provider body")
            return {"saved": True}

        registry.register(definition(), handler)
        arguments = dict(tool_id="skills.write", arguments={"value": "private instructions"},
                         context=context(), turn_id="turn", call_id="call")
        try:
            with pytest.raises(ToolOutcomeUnknown) as caught:
                await registry.invoke(**arguments)
            operation_id = caught.value.operation_id
            with pytest.raises(ToolOutcomeUnknown):
                await registry.invoke(**{**arguments, "turn_id": "newturn", "call_id": "newcall"})
            assert len(invoked) == 1
            status = await registry.recovery_status(context=context())
            assert operation_id[:12] in status and "skills.write" in status
            assert "private instructions" not in status and "private provider body" not in status
            with pytest.raises(ValueError):
                await registry.resolve_recovery(
                    context=context("bob"), operation_id=operation_id, outcome="not-applied",
                )
            await registry.resolve_recovery(
                context=context(), operation_id=operation_id[:12], outcome="not-applied",
            )
            assert len(invoked) == 1  # Resolution itself never invokes a handler.
            assert await registry.invoke(**arguments) == {"saved": True}
            assert len(invoked) == 2
        finally:
            engine.dispose()
    asyncio.run(check())


def test_legacy_unknown_writes_require_resolution_and_owner_confirmation_is_reused(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        handler = AsyncMock(return_value={"saved": True})
        registry.register(definition(), handler)
        arguments = dict(tool_id="skills.write", arguments={"value": "data"},
                         context=context(), turn_id="legacy-turn", call_id="oldcall",
                         allow_new_mutation=False)
        try:
            with pytest.raises(ToolOutcomeUnknown) as caught:
                await registry.invoke(**arguments)
            await registry.resolve_recovery(
                context=context(), operation_id=caught.value.operation_id, outcome="applied",
            )
            result = await registry.invoke(**arguments)
            assert result["outcome"] == "owner_confirmed_applied"
            handler.assert_not_awaited()
        finally:
            engine.dispose()
    asyncio.run(check())


def test_skill_writes_reconcile_against_saved_database_evidence(tmp_path):
    async def check():
        async with open_storage(database_url=None, data_dir=tmp_path) as storage:
            tools = build_tools(
                repository=storage.skills, settings=Settings(_env_file=None),
                operations=storage.tool_operations,
            )
            ctx = context()
            save_args = {
                "skill_id": "test", "name": "Test", "description": "Description",
                "instructions": "Private saved instructions", "required_tool_ids": [],
                "expected_latest_revision": None,
            }
            for tool_id, arguments in (
                ("skills.save_revision", save_args),
                ("skills.activate", {"skill_id": "test", "revision": 1}),
                ("skills.archive", {"skill_id": "test"}),
            ):
                await storage.tool_operations.begin(
                    owner_id=ctx.owner_id, conversation_id=ctx.conversation_id,
                    turn_id="turn", call_id=tool_id, tool_id=tool_id, arguments=arguments,
                    changes_state=True,
                )
                # Simulate a committed handler whose receipt acknowledgement was lost.
                await tools.invoke(tool_id=tool_id, arguments=arguments, context=ctx)
                result = await tools.invoke(
                    tool_id=tool_id, arguments=arguments, context=ctx,
                    turn_id="turn", call_id=tool_id,
                )
                assert result.get("skill_id") == "test"
                receipt = await storage.tool_operations.get(
                    owner_id=ctx.owner_id, conversation_id=ctx.conversation_id,
                    turn_id="turn", call_id=tool_id,
                )
                assert receipt.status == "completed"
            assert (await storage.skills.get_revision(
                owner_id="alice", skill_id="test", revision=1
            )).instructions == save_args["instructions"]
            assert await tools.recovery_status(context=ctx) == (
                "There are no uncertain tool actions in this conversation."
            )
    asyncio.run(check())


def test_real_graph_interrupted_batch_reuses_completed_read_and_reconciles_write(tmp_path):
    async def check():
        tools, repository, engine = await setup_registry(tmp_path)
        writing = asyncio.Event()
        counts = {"read": 0, "write": 0}

        async def read(arguments, ctx):
            counts["read"] += 1
            return {"cached": True}

        async def write(arguments, ctx):
            counts["write"] += 1
            writing.set()
            await asyncio.Event().wait()

        tools.register(definition("skills.read", mutates=False), read)
        tools.register(definition(), write)

        class Model:
            calls = 0

            async def respond(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    calls = [ToolCall("read", "skills__read", '{"value":"x"}'),
                             ToolCall("write", "skills__write", '{"value":"x"}')]
                    return ModelReply(items=[{
                        "type": "function_call", "call_id": call.call_id,
                        "name": call.name, "arguments": call.arguments,
                    } for call in calls], text="", tool_calls=calls)
                outputs = [json.loads(item["output"]) for item in kwargs["items"]
                           if item.get("type") == "function_call_output"]
                assert outputs[0] == {"ok": True, "result": {"cached": True}}
                assert outputs[1]["result"]["outcome"] == "owner_confirmed_applied"
                return ModelReply(items=[{"role": "assistant", "content": "Recovered."}],
                                  text="Recovered.", tool_calls=[])

        saver = InMemorySaver()
        runtime = AgentRuntime(
            settings=Settings(_env_file=None, persona_path=tmp_path / "missing.md"),
            graph=build_graph(model=Model(), tools=tools, checkpointer=saver),
            checkpointer=saver, tools=tools,
            skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
        )
        task = asyncio.create_task(runtime.chat("Save it", "main"))
        try:
            await asyncio.wait_for(writing.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ToolOutcomeUnknown):
                await runtime.chat("/retry", "main")
            assert counts == {"read": 1, "write": 1}
            status = await runtime.chat("/recovery", "main")
            operation_id = status.text.splitlines()[1].split(" — ")[0]
            await runtime.chat(f"/resolve {operation_id} applied", "main")
            assert (await runtime.chat("/retry", "main")).text == "Recovered."
            assert counts == {"read": 1, "write": 1}
        finally:
            task.cancel()
            engine.dispose()
    asyncio.run(check())


def test_timed_out_mutation_requires_attention_and_unrelated_questions_still_work(tmp_path):
    async def check():
        tools, repository, engine = await setup_registry(tmp_path)
        applied = []

        async def write(arguments, ctx):
            applied.append(arguments)
            await asyncio.sleep(10)

        tools.register(definition(), write)

        class Model:
            calls = 0

            async def respond(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ModelReply(items=[{
                        "type": "function_call", "call_id": "write", "name": "skills__write",
                        "arguments": '{"value":"x"}',
                    }], text="", tool_calls=[ToolCall("write", "skills__write", '{"value":"x"}')])
                assert any("uncertain" in item.get("output", "") for item in kwargs["items"])
                return ModelReply(items=[{"role": "assistant", "content": "Next answer."}],
                                  text="Next answer.", tool_calls=[])

        model, saver = Model(), InMemorySaver()
        runtime = AgentRuntime(
            settings=Settings(_env_file=None, persona_path=tmp_path / "missing.md"),
            graph=build_graph(model=model, tools=tools, checkpointer=saver,
                              tool_timeout_seconds=0.01),
            checkpointer=saver, tools=tools,
            skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
        )
        try:
            events = []
            with pytest.raises(ToolOutcomeUnknown):
                await runtime.chat("Save it", "main", on_event=events.append)
            assert events[-1].kind == "error" and events[-1].code == "action_unknown"
            assert model.calls == 1 and len(applied) == 1
            assert (await runtime.chat("Different question", "main")).text == "Next answer."
            assert len(applied) == 1
            assert len(await repository.uncertain(owner_id="personal", conversation_id="main")) == 1
            await runtime.chat("/new", "main")
            assert "skills.write" in (await runtime.chat("/recovery", "main")).text
        finally:
            engine.dispose()
    asyncio.run(check())


def test_changed_mutation_classification_cannot_replay_old_uncertain_write(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        try:
            kwargs = dict(owner_id="alice", conversation_id="main", turn_id="turn",
                          call_id="call", tool_id="skills.write", arguments={"value": "x"})
            await repository.begin(**kwargs, changes_state=True)
            with pytest.raises(ValueError, match="identity"):
                await repository.begin(**kwargs, changes_state=False)
        finally:
            engine.dispose()
    asyncio.run(check())


def test_concurrent_claims_of_owner_released_mutation_execute_only_once(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        kwargs = dict(owner_id="alice", conversation_id="main", turn_id="turn",
                      call_id="call", tool_id="skills.write", arguments={"value": "x"},
                      changes_state=True)
        try:
            receipt, _ = await repository.begin(**kwargs)
            await repository.resolve(owner_id="alice", conversation_id="main",
                                     operation_id=receipt.operation_id, outcome="not-applied")
            claims = await asyncio.gather(repository.begin(**kwargs), repository.begin(**kwargs))
            assert sum(execute for _, execute in claims) == 1
        finally:
            engine.dispose()
    asyncio.run(check())


def test_abandoning_malformed_legacy_call_does_not_create_uncertain_mutation(tmp_path):
    async def check():
        registry, repository, engine = await setup_registry(tmp_path)
        handler = AsyncMock()
        registry.register(definition(), handler)
        try:
            outputs = await registry.recover_outputs(
                context=context(), turn_id="legacy-turn", calls=[{
                    "call_id": "bad", "name": "skills__write", "arguments": "[]",
                }],
            )
            assert json.loads(outputs[0]["output"])["ok"] is False
            assert await repository.uncertain(owner_id="alice", conversation_id="main") == []
            handler.assert_not_awaited()
        finally:
            engine.dispose()
    asyncio.run(check())
