"""Generated progress stays optional, request-local, and outside execution history."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.checkpoint.memory import InMemorySaver

from personal_agent.agent.graph import build_graph
from personal_agent.agent.models import ActivityEvent, ModelReply
from personal_agent.agent.progress import ProgressReporter
from personal_agent.agent.progress_messages import ProgressContext, ProgressMessages
from personal_agent.agent.service import AgentRuntime
from personal_agent.agent.tools import ToolRegistry
from personal_agent.config import Settings


def reporter(events, generate, **overrides):
    return ProgressReporter(
        run_id="run", conversation_id="chat", on_event=events.append, enabled=True,
        ack_delay=0.001, interval=1, max_updates=0, timeout_seconds=10,
        message_factory=generate, **overrides,
    )


def test_generator_receives_bounded_context_persona_and_no_tools(tmp_path):
    async def check():
        persona = tmp_path / "persona.md"
        persona.write_text("Use a calm, conversational voice.")
        model = SimpleNamespace(respond=AsyncMock(return_value=SimpleNamespace(
            text="Let me work through that example.", tool_calls=[],
        )))
        generator = ProgressMessages(model, persona)
        assert await generator.generate(
            ProgressContext("x" * 3000, "m" * 3000), "model_working", "Earlier update.",
        ) == "Let me work through that example."
        request = model.respond.await_args.kwargs
        data = json.loads(request["items"][0]["content"])
        assert len(data["request"]) == 2000 and len(data["memory"]) == 1200
        assert data["previous_notification"] == "Earlier update."
        assert "calm, conversational" in request["instructions"]
        assert "Do not invent" in request["instructions"]
        assert request["tools"] == () and request["hosted_tools"] == frozenset()
        assert "on_activity" not in request
        for text in ("", "Same update.", "Too\nmany lines", "x" * 221):
            model.respond.return_value.text = text
            assert await generator.generate(
                ProgressContext("task"), "searching", "Same update.",
            ) is None
        before = model.respond.await_count
        assert await generator.generate(ProgressContext("task"), "waiting", None) is None
        assert model.respond.await_count == before
    asyncio.run(check())


def test_generated_acknowledgment_and_final_priority():
    async def check():
        events = []
        sent = asyncio.Event()

        def observe(event):
            events.append(event)
            if event.kind == "acknowledgment":
                sent.set()

        generate = AsyncMock(return_value="Let me work through your example.")
        progress = reporter(events, generate)
        progress.on_event = observe
        progress.activity(ActivityEvent("model_working"))
        progress.start()
        await asyncio.wait_for(sent.wait(), timeout=1)
        progress.finish("result", text="Done.")
        await progress.aclose()
        assert [event.kind for event in events] == ["started", "acknowledgment", "result"]
        assert events[1].text == "Let me work through your example."
        generate.assert_awaited_once_with("model_working", None)
    asyncio.run(check())


def test_completion_cancels_inflight_generation_without_late_messages():
    async def check():
        events, started, cancelled = [], asyncio.Event(), asyncio.Event()

        async def generate(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        progress = reporter(events, generate)
        progress.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        progress.finish("result", text="Done.")
        await progress.aclose()
        assert cancelled.is_set()
        assert [event.kind for event in events] == ["started", "result"]
    asyncio.run(check())


def test_timeout_failure_and_stale_phase_fall_back_to_current_activity():
    async def check():
        for mode in ("timeout", "failure", "stale"):
            events, sent = [], asyncio.Event()

            async def generate(*args):
                if mode == "timeout":
                    await asyncio.Event().wait()
                if mode == "failure":
                    raise RuntimeError("private provider error")
                progress.activity(ActivityEvent("searching"))
                return "Stale model message."

            def observe(event):
                events.append(event)
                if event.kind == "acknowledgment":
                    sent.set()

            progress = reporter(events, generate, message_timeout_seconds=0.005)
            progress.on_event = observe
            progress.activity(ActivityEvent("model_working"))
            progress.start()
            await asyncio.wait_for(sent.wait(), timeout=1)
            await progress.aclose()
            expected = "I’m searching public sources." if mode == "stale" else (
                "I’m working on your request."
            )
            assert events[-1].text == expected
    asyncio.run(check())


def test_fast_request_does_not_start_generation():
    async def check():
        generate = AsyncMock()
        progress = reporter([], generate)
        progress.start()
        progress.finish("result", text="Fast answer.")
        await progress.aclose()
        generate.assert_not_awaited()
    asyncio.run(check())


def test_runtime_context_is_isolated_and_progress_is_not_checkpointed(tmp_path):
    async def check():
        acknowledged = {name: asyncio.Event() for name in ("alpha", "beta")}
        generated = []

        class MainModel:
            async def respond(self, **kwargs):
                name = kwargs["items"][-1]["content"]
                await acknowledged[name].wait()
                return ModelReply(
                    items=[{"role": "assistant", "content": "Final answer."}],
                    text="Final answer.", tool_calls=[],
                )

        class NotificationModel:
            async def respond(self, **kwargs):
                data = json.loads(kwargs["items"][0]["content"])
                generated.append(data)
                return ModelReply(
                    items=[], text=f"Working through {data['request']}.", tool_calls=[],
                )

        async def recall_context(*, owner_id, query):
            return "Relevant memory for " + query

        saver, tools = InMemorySaver(), ToolRegistry()
        runtime = AgentRuntime(
            settings=Settings(
                _env_file=None, progress_ack_delay_seconds=0.01,
                progress_interval_seconds=1, persona_path=tmp_path / "missing.md",
            ),
            graph=build_graph(model=MainModel(), tools=tools, checkpointer=saver),
            tools=tools, checkpointer=saver,
            skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
            memory=SimpleNamespace(
                recall_context=recall_context, enqueue_exchange=AsyncMock(),
            ),
            progress_messages=ProgressMessages(NotificationModel(), tmp_path / "missing.md"),
        )

        async def chat(name):
            def observe(event):
                if event.kind == "acknowledgment":
                    assert event.text == f"Working through {name}."
                    acknowledged[name].set()
            return await runtime.chat(name, name, on_event=observe)

        async with asyncio.timeout(2):
            await asyncio.gather(chat("alpha"), chat("beta"))
        assert len(generated) == 2
        for data in generated:
            assert data["memory"] == "Relevant memory for " + data["request"]
        records = [record async for record in saver.alist(None)]
        assert "Working through" not in repr(records)
        assert "ProgressContext" not in repr(records)
    asyncio.run(check())
