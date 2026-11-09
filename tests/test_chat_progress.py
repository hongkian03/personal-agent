"""Global progress timing, transport delivery, isolation, and cancellation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from personal_agent.agent.models import ActivityEvent
from personal_agent.agent.progress import (
    ChatEvent,
    ChatEventBuffer,
    ProgressReporter,
    activity_callback,
    report_activity,
)
from personal_agent.agent.service import AgentRuntime
from personal_agent.config import Settings
from personal_agent.entrypoints import api


def reporter(events, clock, **overrides):
    options = dict(
        run_id="run", conversation_id="conversation", on_event=events.append,
        enabled=True, ack_delay=5, interval=30, max_updates=5, timeout_seconds=180,
        clock=lambda: clock[0],
    )
    options.update(overrides)
    return ProgressReporter(**options)


def test_progress_timing_coalesces_activity_and_caps_updates():
    events, clock = [], [0]
    progress = reporter(events, clock, max_updates=2)
    progress.activity(ActivityEvent("model_working"))
    clock[0] = 4.999
    progress.emit_due()
    assert events == []
    progress.activity(ActivityEvent("tool_started", "portfolio.get_holdings"))
    progress.activity(ActivityEvent("searching"))
    clock[0] = 5
    progress.emit_due()
    assert events[-1].kind == "acknowledgment"
    assert events[-1].text == "I’m searching public sources."
    clock[0] = 34.999
    progress.emit_due()
    assert len(events) == 1
    clock[0] = 35
    progress.emit_due()
    assert "taking a little longer" in events[-1].text
    progress.activity(ActivityEvent("response_started"))
    clock[0] = 100
    progress.emit_due()
    assert events[-1].text == "I’m preparing the response."
    assert len(events) == 3  # A delayed timer never sends catch-up bursts.
    clock[0] = 1000
    progress.emit_due()
    assert len(events) == 3
    progress.finish("result", text="Complete.")
    progress.finish("error", text="Late error", code="late")
    progress.emit_due()
    assert [e.kind for e in events] == ["acknowledgment", "status", "status", "result"]
    assert [e.sequence for e in events] == [1, 2, 3, 4]


def test_completion_wins_at_ack_deadline_and_disabled_progress_is_quiet():
    for enabled in (True, False):
        events, clock = [], [0]
        progress = reporter(events, clock, enabled=enabled)
        clock[0] = 5
        progress.finish("result", text="Done.")
        progress.emit_due()
        assert [e.kind for e in events] == ["result"]


def test_notifications_do_not_leak_arguments_and_search_completion_is_not_verification():
    events, clock = [], [0]
    progress = reporter(events, clock)
    progress.activity(ActivityEvent("tool_started", "portfolio.get_holdings"))
    clock[0] = 5
    progress.emit_due()
    assert events[-1].text == "I’m checking the saved portfolio information."
    progress.activity(ActivityEvent("search_completed"))
    clock[0] = 35
    progress.emit_due()
    assert events[-1].text == "I’m working on your request."


def make_runtime(tmp_path, graph, **overrides):
    settings = Settings(
        _env_file=None, api_token="test-token", persona_path=tmp_path / "missing.md",
        progress_ack_delay_seconds=0.005, progress_interval_seconds=0.01,
        **overrides,
    )
    return AgentRuntime(
        settings=settings, graph=graph, tools=SimpleNamespace(definitions=lambda _: []),
        skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
    )


class Graph:
    def __init__(self, delay=0):
        self.delay = delay
        self.cancelled = asyncio.Event()
        self.configs = []

    async def aget_state(self, config):
        return SimpleNamespace(next=(), values={})

    async def ainvoke(self, inputs, config):
        self.configs.append(config)
        report_activity(ActivityEvent("searching"))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return {"text": "Complete report [1]."}


def test_runtime_fast_slow_disabled_and_callback_failure(tmp_path):
    async def check():
        for delay, enabled in ((0, True), (0.03, True), (0.03, False)):
            graph, events = Graph(delay), []
            runtime = make_runtime(tmp_path, graph, progress_enabled=enabled)
            result = await runtime.chat("Research.", "main", on_event=events.append)
            assert events[0].kind == "started" and events[-1].kind == "result"
            assert events[-1].text == result.text
            progress = [e for e in events if e.kind in {"acknowledgment", "status"}]
            assert bool(progress) == (delay > 0 and enabled)
            assert all(e.run_id == result.run_id for e in events)
            assert "on_activity" not in graph.configs[-1]["configurable"]
            assert activity_callback.get() is None
        runtime = make_runtime(tmp_path, Graph(0.01))

        def broken_callback(event):
            raise RuntimeError("private callback information")

        assert (await runtime.chat("Hello", on_event=broken_callback)).text.startswith("Complete")
    asyncio.run(check())


def test_timeout_cancels_execution_and_emits_one_safe_terminal(tmp_path):
    async def check():
        graph, events = Graph(10), []
        runtime = make_runtime(tmp_path, graph, chat_timeout_seconds=0.03)
        with pytest.raises(TimeoutError):
            await runtime.chat("Research", on_event=events.append)
        assert graph.cancelled.is_set()
        assert [e.kind for e in events].count("error") == 1
        assert events[-1].code == "timeout"
        count = len(events)
        await asyncio.sleep(0.02)
        assert len(events) == count
        assert activity_callback.get() is None
    asyncio.run(check())


def test_request_activity_is_isolated_and_not_persisted(tmp_path):
    async def check():
        graph = Graph(0.03)
        runtime = make_runtime(tmp_path, graph)
        first, second = [], []
        await asyncio.gather(
            runtime.chat("One", "one", on_event=first.append),
            runtime.chat("Two", "two", on_event=second.append),
        )
        assert {e.conversation_id for e in first} == {"one"}
        assert {e.conversation_id for e in second} == {"two"}
        assert first[0].run_id != second[0].run_id
        assert activity_callback.get() is None
    asyncio.run(check())


def test_buffer_coalesces_progress_and_prioritizes_terminal():
    async def check():
        buffer = ChatEventBuffer()
        for seq, kind in enumerate(("started", "acknowledgment", "status", "result"), 1):
            buffer.put(ChatEvent(kind, "run", "conversation", seq, text=kind))
        assert (await buffer.get()).kind == "started"
        assert (await buffer.get()).kind == "result"
    asyncio.run(check())


def test_stream_api_auth_validation_order_and_legacy_compatibility(tmp_path):
    async def check():
        app = api.create_app()
        app.state.runtime = make_runtime(tmp_path, Graph(0.03))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"message": "Research", "conversation_id": "main"}
            assert (await client.post("/chat/stream", json=body)).status_code == 401
            headers = {"Authorization": "Bearer test-token"}
            assert (await client.post(
                "/chat/stream", headers=headers, json={"message": " "}
            )).status_code == 422
            response = await client.post("/chat/stream", headers=headers, json=body)
            assert response.headers["content-type"].startswith("text/event-stream")
            frames = [frame for frame in response.text.split("\n\n") if frame.startswith("id:")]
            names = [frame.split("event: ")[1].split("\n")[0] for frame in frames]
            assert names[0] == "started" and names[-1] == "result"
            assert "acknowledgment" in names
            payloads = [json.loads(frame.split("data: ")[1]) for frame in frames]
            assert payloads[0]["timeout_seconds"] == 180
            assert len({p["run_id"] for p in payloads}) == 1
            assert [p["sequence"] for p in payloads] == sorted(p["sequence"] for p in payloads)
            legacy = await client.post("/chat", headers=headers, json=body)
            assert legacy.status_code == 200 and legacy.json()["text"].startswith("Complete")
    asyncio.run(check())


@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
def test_stream_disconnect_cancels_work_after_live_acknowledgment(tmp_path, asgi_spec):
    async def check():
        graph = Graph(10)
        app = api.create_app()
        app.state.runtime = make_runtime(tmp_path, graph)
        disconnect, sent = asyncio.Event(), []
        body = json.dumps({"message": "Research", "conversation_id": "main"}).encode()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if b"event: acknowledgment" in message.get("body", b""):
                disconnect.set()
                if asgi_spec == "2.4":
                    raise OSError("Client disconnected during a send")

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": asgi_spec},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/chat/stream", "raw_path": b"/chat/stream", "query_string": b"",
            "headers": [(b"authorization", b"Bearer test-token"),
                        (b"content-type", b"application/json")],
            "server": ("test", 80), "client": ("test", 123), "root_path": "",
        }
        async with asyncio.timeout(1):
            if asgi_spec == "2.4":
                with pytest.raises(ClientDisconnect):
                    await app(scope, receive, send)
            else:
                await app(scope, receive, send)
        assert disconnect.is_set() and graph.cancelled.is_set()
        assert not any(b"event: result" in m.get("body", b"") for m in sent)
    asyncio.run(check())


def test_stream_failure_heartbeats_and_preflight_errors(tmp_path, monkeypatch):
    class FailingGraph(Graph):
        async def ainvoke(self, inputs, config):
            await asyncio.sleep(0.025)
            raise RuntimeError("sensitive provider body and tool arguments")

    async def check():
        monkeypatch.setattr(api, "STREAM_HEARTBEAT_SECONDS", 0.005)
        app = api.create_app()
        app.state.runtime = make_runtime(tmp_path, FailingGraph())
        headers = {"Authorization": "Bearer test-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/chat/stream", headers=headers, json={"message": "Research"}
            )
            assert response.status_code == 200
            assert ": heartbeat" in response.text
            assert response.text.count("event: error") == 1
            assert "event: result" not in response.text
            assert "sensitive provider" not in response.text
            assert '"code": "request_failed"' in response.text
            app.state.runtime.graph = None
            response = await client.post(
                "/chat/stream", headers=headers, json={"message": "Research"}
            )
            assert response.status_code == 503
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [
    ("progress_ack_delay_seconds", 0), ("progress_interval_seconds", -1),
    ("progress_max_updates", -1),
])
def test_progress_configuration_is_validated(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
