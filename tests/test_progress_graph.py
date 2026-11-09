"""Live progress across actual LangGraph rounds without checkpoint contamination."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from openai.types.responses import (
    Response,
    ResponseCreatedEvent,
    ResponseOutputItemAddedEvent,
    ResponseWebSearchCallSearchingEvent,
)

from personal_agent.agent.graph import build_graph
from personal_agent.agent.models import ActivityEvent, ModelError, ModelReply, ToolCall
from personal_agent.agent.service import AgentRuntime, ChatValidationError
from personal_agent.agent.tools import ToolDefinition, ToolRegistry
from personal_agent.config import Settings
from personal_agent.integrations.llm.provider import OpenAIModel


def runtime_for(tmp_path, model, handler):
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            id="runtime.lookup",
            description="Look up saved information.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            required_scopes=frozenset({"runtime:read"}),
        ),
        handler,
    )
    checkpointer = InMemorySaver()
    graph = build_graph(model=model, tools=tools, checkpointer=checkpointer)
    runtime = AgentRuntime(
        settings=Settings(
            _env_file=None,
            persona_path=tmp_path / "missing-persona.md",
            progress_ack_delay_seconds=0.005,
            progress_interval_seconds=0.005,
        ),
        graph=graph,
        tools=tools,
        skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
    )
    return runtime, checkpointer


def test_actual_graph_propagates_activity_across_rounds_and_checkpoints_only_execution(tmp_path):
    async def check():
        search_seen, tool_seen, response_seen = (asyncio.Event() for _ in range(3))
        events, invoked = [], []
        first_items = [
            {"id": "rs_test", "type": "reasoning", "summary": [],
             "encrypted_content": "opaque-context"},
            {"id": "ws_test", "type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "public company results"}},
            {"id": "fc_test", "type": "function_call", "status": "completed",
             "call_id": "call_lookup", "name": "runtime__lookup", "arguments": "{}"},
        ]
        final_item = {"role": "assistant", "content": "Complete report."}

        def observe(event):
            events.append(event)
            if event.text == "I’m searching public sources.":
                search_seen.set()
            elif event.text == "I’m checking the available information.":
                tool_seen.set()
            elif event.text == "I’m preparing the response.":
                response_seen.set()

        class Model:
            calls = 0

            async def respond(self, *, items, on_activity, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    on_activity(ActivityEvent("searching"))
                    await search_seen.wait()
                    return ModelReply(
                        items=first_items, text="", hosted_tool_calls=1,
                        tool_calls=[ToolCall("call_lookup", "runtime__lookup", "{}")],
                    )
                assert kwargs["max_hosted_tool_calls"] == 2
                assert items[1:4] == first_items
                assert items[-1]["type"] == "function_call_output"
                assert json.loads(items[-1]["output"]) == {
                    "ok": True, "result": {"checked": True}
                }
                on_activity(ActivityEvent("response_started"))
                await response_seen.wait()
                return ModelReply(items=[final_item], text="Complete report.", tool_calls=[])

        async def lookup(arguments, context):
            invoked.append(context.run_id)
            await tool_seen.wait()
            return {"checked": True}

        model = Model()
        runtime, saver = runtime_for(tmp_path, model, lookup)
        async with asyncio.timeout(1):
            result = await runtime.chat("Research.", "rounds", on_event=observe)
        assert model.calls == 2 and invoked == [result.run_id]
        assert search_seen.is_set() and tool_seen.is_set() and response_seen.is_set()
        assert events[0].kind == "started" and events[-1].kind == "result"
        assert events[-1].text == result.text
        assert all(event.run_id == result.run_id for event in events)
        records = [record async for record in saver.alist(None)]
        latest = max(records, key=lambda record: record.metadata["step"])
        saved = latest.checkpoint["channel_values"]
        assert saved["items"][1:4] == first_items
        assert saved["items"][-1] == final_item
        assert saved["items"][-2]["call_id"] == "call_lookup"
        assert saved["pending_calls"] == []
        assert saved["rounds"] == 1 and saved["hosted_tool_calls"] == 1
        # Actual serialized records include neither request callbacks nor progress copy.
        stored = repr(records)
        assert "ProgressReporter" not in stored and "activity_callback" not in stored
        assert "on_activity" not in stored and "on_event" not in stored
        assert "I’m searching public sources." not in stored
        assert "I’m checking the available information." not in stored

    asyncio.run(check())


def test_interrupted_provider_stream_cannot_execute_partial_calls_in_actual_graph(tmp_path):
    async def check():
        acknowledged = asyncio.Event()
        events, invoked = [], []

        class InterruptedStream:
            response = httpx.Response(200)

            async def __aiter__(self):
                yield ResponseCreatedEvent(
                    type="response.created", sequence_number=0,
                    response=Response.model_validate({
                        "id": "resp_partial", "object": "response", "created_at": 0,
                        "model": "gpt-6-luna", "status": "in_progress", "output": [],
                        "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
                    }),
                )
                yield ResponseWebSearchCallSearchingEvent(
                    type="response.web_search_call.searching", sequence_number=1,
                    item_id="ws_partial", output_index=0,
                )
                yield ResponseOutputItemAddedEvent.model_validate({
                    "type": "response.output_item.added", "sequence_number": 2,
                    "output_index": 1,
                    "item": {"id": "fc_partial", "type": "function_call",
                             "call_id": "call_partial", "name": "runtime__lookup",
                             "arguments": '{"incomplete":', "status": "in_progress"},
                })
                await acknowledged.wait()
                raise httpx.ReadError("sensitive interrupted provider body")

        def observe(event):
            events.append(event)
            if event.kind == "acknowledgment":
                acknowledged.set()

        async def lookup(arguments, context):
            invoked.append(arguments)
            return {"checked": True}

        model = OpenAIModel(api_key="not-a-real-key", model="gpt-6-luna")
        raw_stream = InterruptedStream()
        model.client.responses.create = AsyncMock(return_value=raw_stream)
        runtime, saver = runtime_for(tmp_path, model, lookup)
        try:
            async with asyncio.timeout(1):
                with pytest.raises(ModelError) as caught:
                    await runtime.chat("Research.", "interrupted", on_event=observe)
            assert "sensitive" not in str(caught.value)
            assert invoked == []
            assert events[-1].kind == "error"
            assert sum(event.kind == "error" for event in events) == 1
            assert raw_stream.response.is_closed
            records = [record async for record in saver.alist(None)]
            assert "call_partial" not in repr(records)
            with pytest.raises(ChatValidationError, match="previous turn was interrupted"):
                await runtime.chat("Continue.", "interrupted")
        finally:
            await model.aclose()

    asyncio.run(check())
