"""A shared editable persona reaches both runtime generation paths."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from personal_agent import bootstrap
from personal_agent.capabilities.motivation import workflows as motivation
from personal_agent.config import Settings
from personal_agent.core.persona import MAX_PERSONA_CHARS, with_persona


def test_custom_persona_reaches_chat_and_morning_and_reloads(monkeypatch, tmp_path):
    profile = tmp_path / "agent.md"
    profile.write_text("Name: Cedar\nTone: Calm and direct.", encoding="utf-8")

    class Clock:
        @classmethod
        def now(cls, zone):
            return datetime(2026, 11, 9, 8, 0, tzinfo=zone)

    class Model:
        def __init__(self, **kwargs):
            self.instructions = None

        async def respond(self, *, instructions, **kwargs):
            self.instructions = instructions
            return SimpleNamespace(text="One small step today.", tool_calls=())

        async def aclose(self):
            pass

    class Graph:
        instructions = None

        async def aget_state(self, config):
            return SimpleNamespace(next=(), values={})

        async def ainvoke(self, inputs, config):
            self.instructions = config["configurable"]["instructions"]
            return {"text": "Hello."}

    graph = Graph()
    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)
    monkeypatch.setattr(bootstrap, "build_graph", lambda **kwargs: graph)
    monkeypatch.setattr(motivation, "datetime", Clock)

    async def check():
        settings = Settings(
            _env_file=None,
            model_api_key="test-model-key",
            database_url=None,
            ibkr_flex_token=None,
            ibkr_flex_query_id=None,
            data_dir=tmp_path / "data",
            persona_path=profile,
        )
        async with bootstrap.open_runtime(settings) as runtime:
            await runtime.chat("Hello")
            assert "Name: Cedar" in graph.instructions
            assert "Never ask for API keys" in graph.instructions
            assert await runtime.morning.claim_due() == "One small step today."
            assert "Name: Cedar" in runtime.morning.model.instructions
            assert "one or two short sentences" in runtime.morning.model.instructions
            profile.write_text("Name: Birch", encoding="utf-8")
            await runtime.chat("Hello again")
            assert "Name: Birch" in graph.instructions
            assert "Name: Cedar" not in graph.instructions

    asyncio.run(check())


def test_missing_or_blank_persona_preserves_task_instructions(tmp_path):
    path = tmp_path / "agent.md"
    assert with_persona("Task instructions", path) == "Task instructions"
    path.write_text(" \n", encoding="utf-8")
    assert with_persona("Task instructions", path) == "Task instructions"


def test_oversized_persona_is_not_silently_truncated(tmp_path):
    path = tmp_path / "agent.md"
    path.write_text("x" * (MAX_PERSONA_CHARS + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="12000"):
        with_persona("Task instructions", path)
