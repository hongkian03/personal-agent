"""Failures retain conversation context and explicit retries resume saved execution."""

import asyncio
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from personal_agent import bootstrap
from personal_agent.agent.graph import build_graph
from personal_agent.agent.models import ModelError, ModelReply, ToolCall
from personal_agent.agent.recovery import original_request, turn_identifier
from personal_agent.agent.service import AgentRuntime, ChatValidationError
from personal_agent.agent.tools import ToolDefinition, ToolRegistry
from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext


def reply(text):
    return ModelReply(items=[{"role": "assistant", "content": text}], text=text, tool_calls=[])


class ScriptedModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def respond(self, **kwargs):
        self.calls.append({**kwargs, "items": deepcopy(kwargs["items"])})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return reply(step) if isinstance(step, str) else step


def runtime_for(tmp_path, model, *, saver=None, tools=None, **overrides):
    saver = saver if saver is not None else InMemorySaver()
    tools = tools if tools is not None else ToolRegistry()
    graph = build_graph(model=model, tools=tools, checkpointer=saver)
    runtime = AgentRuntime(
        settings=Settings(_env_file=None, persona_path=tmp_path / "missing.md", **overrides),
        graph=graph, checkpointer=saver, tools=tools,
        skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
    )
    return runtime, saver


def users(items):
    return [item["content"] for item in items if item.get("role") == "user"]


def test_new_question_after_model_failure_preserves_complete_history(tmp_path):
    async def check():
        model = ScriptedModel("First answer.", ModelError("Provider failed."), "Next answer.")
        runtime, _ = runtime_for(tmp_path, model)
        await runtime.chat("First question.", "main")
        with pytest.raises(ModelError):
            await runtime.chat("Failed question.", "main")
        result = await runtime.chat("Different question.", "main")
        assert result.text == "Next answer."
        items = model.calls[-1]["items"]
        assert users(items) == ["First question.", "Failed question.", "Different question."]
        assert {"role": "assistant", "content": "First answer."} in items
        assert any("previous request stopped" in item.get("content", "") for item in items)
    asyncio.run(check())


def test_explicit_retry_resumes_without_duplicate_user_input_or_completed_tools(tmp_path):
    async def check():
        tools, invoked = ToolRegistry(), []

        async def lookup(arguments, context):
            invoked.append(context.run_id)
            return {"saved": "result"}

        tools.register(ToolDefinition(
            id="runtime.lookup", description="Read saved data.",
            input_schema={"type": "object", "properties": {}},
            required_scopes=frozenset({"runtime:read"}),
        ), lookup)
        calls = ModelReply(
            items=[{"type": "function_call", "call_id": "read1", "name": "runtime__lookup",
                    "arguments": "{}"}], text="",
            tool_calls=[ToolCall("read1", "runtime__lookup", "{}")], hosted_tool_calls=1,
        )
        failure = ModelError("Interrupted after search.")
        failure.hosted_tool_calls = 2
        model = ScriptedModel(calls, failure, "Recovered answer.")
        runtime, saver = runtime_for(tmp_path, model, tools=tools)
        events = []
        with pytest.raises(ModelError):
            await runtime.chat("Research this company.", "main", on_event=events.append)
        original_turn_id = events[0].run_id
        result = await runtime.chat("/retry", "main")
        assert result.text == "Recovered answer."
        assert result.run_id != original_turn_id
        assert invoked == [original_turn_id]
        items = model.calls[-1]["items"]
        assert users(items) == ["Research this company."]
        output = next(item for item in items if item.get("type") == "function_call_output")
        assert json.loads(output["output"]) == {"ok": True, "result": {"saved": "result"}}
        assert model.calls[-1]["hosted_tools"] == frozenset()
        records = [record async for record in saver.alist(None)]
        latest = max(records, key=lambda record: record.metadata["step"])
        assert latest.checkpoint["channel_values"]["turn_id"] == original_turn_id
        assert latest.checkpoint["channel_values"]["rounds"] == 1
    asyncio.run(check())


def test_retry_without_failed_turn_does_not_invoke_model(tmp_path):
    async def check():
        model = ScriptedModel("Answer.")
        runtime, _ = runtime_for(tmp_path, model)
        assert "no interrupted request" in (await runtime.chat("/retry", "main")).text
        await runtime.chat("Question.", "main")
        assert "no interrupted request" in (await runtime.chat("/retry", "main")).text
        assert len(model.calls) == 1
    asyncio.run(check())


def test_timeout_can_be_retried_with_history_intact(tmp_path):
    class SlowOnce(ScriptedModel):
        async def respond(self, **kwargs):
            if not self.calls:
                self.calls.append(deepcopy(kwargs["items"]))
                await asyncio.sleep(10)
            self.calls.append(deepcopy(kwargs["items"]))
            return reply("Recovered after timeout.")

    async def check():
        model = SlowOnce()
        runtime, _ = runtime_for(tmp_path, model, chat_timeout_seconds=0.03)
        events = []
        with pytest.raises(TimeoutError):
            await runtime.chat("Research.", "main", on_event=events.append)
        assert events[-1].code == "timeout"
        assert "conversation is preserved" in events[-1].text
        assert (await runtime.chat("/retry", "main")).text == "Recovered after timeout."
        assert users(model.calls[-1]) == ["Research."]
    asyncio.run(check())


def test_cancelled_model_attempt_retains_observed_search_budget(tmp_path):
    class CancelOnce:
        def __init__(self):
            self.started = asyncio.Event()
            self.calls = []

        async def respond(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    exc.hosted_tool_calls = 2
                    raise
            return reply("Recovered after cancellation.")

    async def check():
        model = CancelOnce()
        runtime, saver = runtime_for(tmp_path, model)
        task = asyncio.create_task(runtime.chat("Research.", "main"))
        await asyncio.wait_for(model.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await runtime.chat("/retry", "main")).text == "Recovered after cancellation."
        assert model.calls[-1]["max_hosted_tool_calls"] == 1
        assert users(model.calls[-1]["items"]) == ["Research."]
        records = [record async for record in saver.alist(None)]
        latest = max(records, key=lambda record: record.metadata["step"])
        assert latest.checkpoint["channel_values"]["hosted_tool_calls"] == 2
    asyncio.run(check())


def test_failure_can_be_retried_after_runtime_restart(monkeypatch, tmp_path):
    class Model:
        failed = False
        calls = []

        def __init__(self, **kwargs):
            pass

        async def respond(self, **kwargs):
            self.calls.append(deepcopy(kwargs["items"]))
            if not type(self).failed:
                type(self).failed = True
                raise ModelError("Temporary failure.")
            return reply("Recovered after restart.")

        async def aclose(self):
            pass

    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)

    async def check():
        settings = Settings(
            _env_file=None, model_api_key="test-key", data_dir=tmp_path,
            skill_bundle_path=tmp_path / "absent.json", persona_path=tmp_path / "absent.md",
            database_url=None, ibkr_flex_token=None, ibkr_flex_query_id=None,
        )
        async with bootstrap.open_runtime(settings) as runtime:
            with pytest.raises(ModelError):
                await runtime.chat("Original research question.", "main")
        async with bootstrap.open_runtime(settings) as runtime:
            assert (await runtime.chat("/retry", "main")).text == "Recovered after restart."
        assert users(Model.calls[-1]) == ["Original research question."]
    asyncio.run(check())


def test_output_limit_failure_gives_targeted_recovery_message(tmp_path):
    async def check():
        error = ModelError("Output limit reached.")
        error.code = "output_limit"
        runtime, _ = runtime_for(tmp_path, ScriptedModel(error, "Short answer."))
        events = []
        with pytest.raises(ModelError):
            await runtime.chat("Long research.", "main", on_event=events.append)
        assert events[-1].code == "output_limit"
        assert "output-token limit" in events[-1].text
        assert "Increase the configured output budget" in events[-1].text
        assert (await runtime.chat("Give a short answer instead.", "main")).text == "Short answer."
    asyncio.run(check())


def test_reconciliation_commands_work_without_model_and_do_not_touch_history(tmp_path):
    async def check():
        tools = SimpleNamespace(
            recovery_status=AsyncMock(return_value="No uncertain operations."),
            resolve_recovery=AsyncMock(return_value="Operation marked not applied."),
        )
        runtime = AgentRuntime(
            settings=Settings(_env_file=None), graph=None, tools=tools, skills=None,
        )
        assert (await runtime.chat("/recovery", "main")).text == "No uncertain operations."
        assert "Use /resolve" in (await runtime.chat("/resolve", "main")).text
        assert "not applied" in (await runtime.chat("/resolve abc123 not-applied", "main")).text
        assert tools.resolve_recovery.await_args.kwargs["operation_id"] == "abc123"
        assert tools.resolve_recovery.await_args.kwargs["context"].conversation_id == "main"
    asyncio.run(check())


def test_legacy_checkpoints_derive_stable_request_and_turn_identity():
    state = {"items": [{"role": "user", "content": "Earlier request."}],
             "pending_calls": [{"call_id": "call1"}]}
    assert original_request(state) == "Earlier request."
    assert turn_identifier(state) == turn_identifier(deepcopy(state))
    assert turn_identifier(state).startswith("legacy-")


def test_legacy_failed_checkpoint_can_retry_without_erasing_history(tmp_path):
    async def check():
        model = ScriptedModel(ModelError("Legacy failure."), "Recovered legacy request.")
        runtime, _ = runtime_for(tmp_path, model)
        owner, conversation = runtime.settings.owner_id, "legacy"
        config = {"configurable": {
            "thread_id": hashlib.sha256(json.dumps([owner, conversation]).encode()).hexdigest(),
            "context": ExecutionContext(owner, "legacy-run", frozenset({"runtime:read"})),
        }}
        with pytest.raises(ModelError):
            await runtime.graph.ainvoke({
                "items": [{"role": "user", "content": "A legacy request."}],
                "rounds": 0, "hosted_tool_calls": 0, "text": "",
            }, config)
        assert (await runtime.chat("/retry", conversation)).text == "Recovered legacy request."
        assert users(model.calls[-1]["items"]) == ["A legacy request."]
    asyncio.run(check())


def test_waiting_new_question_recovers_failed_turn_under_conversation_lock(tmp_path):
    class FailThenAnswer:
        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()
            self.calls = []

        async def respond(self, **kwargs):
            self.calls.append(deepcopy(kwargs["items"]))
            if len(self.calls) == 1:
                self.started.set()
                await self.release.wait()
                raise ModelError("First request failed.")
            return reply("Next question answered.")

    async def check():
        model = FailThenAnswer()
        runtime, _ = runtime_for(tmp_path, model)
        first = asyncio.create_task(runtime.chat("Will fail.", "main"))
        await asyncio.wait_for(model.started.wait(), timeout=1)
        second = asyncio.create_task(runtime.chat("New question.", "main"))
        await asyncio.sleep(0)
        assert len(model.calls) == 1
        model.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], ModelError)
        assert results[1].text == "Next question answered."
        assert users(model.calls[-1]) == ["Will fail.", "New question."]
    asyncio.run(check())


def test_invalid_reconciliation_id_produces_friendly_validation_error():
    async def check():
        runtime = AgentRuntime(
            settings=Settings(_env_file=None), graph=None, skills=None,
            tools=SimpleNamespace(resolve_recovery=AsyncMock(side_effect=ValueError(
                "No unique unresolved operation matches in this conversation"
            ))),
        )
        with pytest.raises(ChatValidationError, match="No unique unresolved operation"):
            await runtime.chat("/resolve invalid applied", "main")
    asyncio.run(check())
