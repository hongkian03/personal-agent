"""Provider SSE activity, final output integrity, and interrupted-stream cleanup."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from personal_agent.agent.models import ActivityEvent, ModelError
from personal_agent.integrations.llm.provider import OpenAIModel


def response_payload(output, *, status="completed"):
    return {
        "id": "resp_stream",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": status,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def lifecycle(kind, output=(), *, status="completed"):
    return {
        "type": kind,
        "sequence_number": 0,
        "response": response_payload(list(output), status=status),
    }


def message(text="", *, status="completed", annotations=()):
    return {
        "id": "msg_stream",
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": [
            {"type": "output_text", "text": text, "annotations": list(annotations)}
        ],
    }


class EventBody(httpx.AsyncByteStream):
    def __init__(self, events, *, wait=False):
        self.events = events
        self.wait = wait
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for event in self.events:
            # Transport chunks need not align with SSE messages or UTF-8 boundaries.
            data = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
            yield data[:13]
            yield data[13:]
        self.waiting.set()
        if self.wait:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def model_for(body, *, timeout_seconds=60):
    model = OpenAIModel(
        api_key="not-a-real-key", model="gpt-6-luna", timeout_seconds=timeout_seconds
    )
    await model.client.close()
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    model.client = AsyncOpenAI(
        api_key="not-a-real-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return model, requests


async def respond(model, **kwargs):
    return await model.respond(
        items=[{"role": "user", "content": "Research this company."}],
        instructions="Use sources.",
        tools=(),
        **kwargs,
    )


def test_real_sdk_stream_emits_coarse_activity_and_preserves_final_items_and_citations():
    async def check():
        search = {
            "id": "ws_stream",
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": "private query never becomes progress"},
        }
        text = "Café grew."
        report = message(
            text,
            annotations=[
                {
                    "type": "url_citation",
                    "start_index": 0,
                    "end_index": len(text),
                    "url": "https://example.org/results",
                    "title": "Results",
                }
            ],
        )
        function_call = {
            "id": "fc_stream",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_stream",
            "name": "runtime__current_time",
            "arguments": "{}",
        }
        reasoning = {
            "id": "rs_stream",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque-context",
        }
        events = [lifecycle("response.created", status="in_progress")]
        for phase in ("in_progress", "searching", "completed"):
            events.append(
                {
                    "type": f"response.web_search_call.{phase}",
                    "item_id": search["id"],
                    "output_index": 0,
                    "sequence_number": len(events),
                }
            )
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "sequence_number": 4,
                    "item": {**message(status="in_progress"), "content": []},
                },
                {
                    "type": "response.content_part.added",
                    "item_id": report["id"],
                    "output_index": 1,
                    "content_index": 0,
                    "sequence_number": 5,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": report["id"],
                    "output_index": 1,
                    "content_index": 0,
                    "sequence_number": 6,
                    "delta": text,
                    "logprobs": [],
                },
                lifecycle("response.completed", [search, report, function_call, reasoning]),
            ]
        )
        body = EventBody(events)
        model, requests = await model_for(body)
        activity = []
        try:
            reply = await respond(
                model, on_activity=activity.append, hosted_tools=frozenset({"web_search"})
            )
        finally:
            await model.aclose()
        assert [event.phase for event in activity] == [
            "model_working", "searching", "search_completed", "response_started"
        ]
        assert all(event.tool_id is None for event in activity)
        assert "private query" not in repr(activity)
        assert "opaque-context" not in repr(activity)
        assert "Café grew.[1]" in reply.text
        assert "https://example.org/results" in reply.text
        assert reply.items[0]["action"]["query"] == search["action"]["query"]
        assert reply.items[-1]["encrypted_content"] == "opaque-context"
        assert reply.tool_calls[0].call_id == "call_stream"
        assert reply.tool_calls[0].arguments == "{}"
        assert reply.hosted_tool_calls == 1
        assert requests[0]["stream"] is True
        assert requests[0]["store"] is False
        assert requests[0]["max_tool_calls"] == 3
        assert body.closed

    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete", "error", None])
def test_failed_or_missing_terminal_events_never_return_partial_function_calls(terminal):
    async def check():
        partial = {
            "type": "response.output_item.added",
            "output_index": 0,
            "sequence_number": 1,
            "item": {
                "id": "fc_partial",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_partial",
                "name": "skills__save",
                "arguments": '{"instructions":',
            },
        }
        events = [lifecycle("response.created", status="in_progress"), partial]
        if terminal == "error":
            events.append(
                {"type": "error", "message": "sensitive provider error", "sequence_number": 2}
            )
        elif terminal is not None:
            events.append(lifecycle(terminal, status=terminal.removeprefix("response.")))
        body = EventBody(events)
        model, _ = await model_for(body)
        try:
            with pytest.raises(ModelError) as caught:
                await respond(model)
            assert "sensitive provider error" not in str(caught.value)
            assert "instructions" not in str(caught.value)
        finally:
            await model.aclose()
        assert body.closed

    asyncio.run(check())


def test_model_wall_timeout_closes_provider_stream():
    async def check():
        body = EventBody([lifecycle("response.created", status="in_progress")], wait=True)
        model, _ = await model_for(body, timeout_seconds=0.02)
        try:
            with pytest.raises(TimeoutError):
                await respond(model)
        finally:
            await model.aclose()
        assert body.closed

    asyncio.run(check())


def test_cancellation_closes_provider_stream():
    async def check():
        body = EventBody([lifecycle("response.created", status="in_progress")], wait=True)
        model, _ = await model_for(body)
        task = asyncio.create_task(respond(model))
        try:
            await asyncio.wait_for(body.waiting.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await model.aclose()
        assert body.closed

    asyncio.run(check())


def test_progress_callback_failure_does_not_fail_the_model_response(caplog):
    async def check():
        body = EventBody(
            [
                lifecycle("response.created", status="in_progress"),
                lifecycle("response.completed", [message("Done.")]),
            ]
        )
        model, _ = await model_for(body)

        def fail(event: ActivityEvent):
            raise RuntimeError("sensitive callback data")

        try:
            reply = await respond(model, on_activity=fail)
        finally:
            await model.aclose()
        assert reply.text == "Done."
        assert "sensitive callback data" not in caplog.text

    asyncio.run(check())
