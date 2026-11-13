"""Morning memory is owner-scoped, optional, and never blocks daily encouragement."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from personal_agent.capabilities.motivation import workflows


@pytest.mark.parametrize("mode", ["available", "empty", "disabled", "failure", "timeout"])
def test_morning_uses_memory_and_falls_back_without_changing_daily_claims(
    monkeypatch, tmp_path, mode,
):
    class Clock:
        hour = 7

        @classmethod
        def now(cls, zone):
            return datetime(2026, 11, 13, cls.hour, 0, tzinfo=zone)

    monkeypatch.setattr(workflows, "datetime", Clock)

    async def check():
        stored = {}

        async def claim(owner_id, day, text):
            stored[day] = text
            return True

        async def recall(**kwargs):
            if mode == "failure":
                raise RuntimeError("private provider response")
            if mode == "timeout":
                await asyncio.Event().wait()
            return (
                "The user is preparing for engineering interviews." if mode == "available" else ""
            )

        memory = SimpleNamespace(recall_context=AsyncMock(side_effect=recall))
        model = SimpleNamespace(respond=AsyncMock(return_value=SimpleNamespace(
            text="Take one small step toward your goals today.", tool_calls=[],
        )))
        repository = SimpleNamespace(
            recent=AsyncMock(return_value=stored), claim=AsyncMock(side_effect=claim),
        )
        morning = workflows.MorningMessage(
            model, repository, "alice", ZoneInfo("America/New_York"),
            persona_path=tmp_path / "absent.md",
            memory=memory if mode != "disabled" else None,
            memory_timeout_seconds=0.005,
        )
        assert await morning.claim_due() is None
        memory.recall_context.assert_not_awaited()
        Clock.hour = 8
        assert await morning.claim_due() == "Take one small step toward your goals today."
        request = model.respond.await_args.kwargs
        content = request["items"][0]["content"]
        assert "2026-11-13" in content
        assert ("engineering interviews" in content) == (mode == "available")
        assert "untrusted reference data" in request["instructions"]
        assert "outdated" in request["instructions"]
        assert request["tools"] == ()
        if mode != "disabled":
            query = memory.recall_context.await_args.kwargs
            assert query["owner_id"] == "alice" and "communication preferences" in query["query"]
        assert await morning.claim_due() is None
        assert model.respond.await_count == 1 and repository.claim.await_count == 1
        assert memory.recall_context.await_count == (0 if mode == "disabled" else 1)
    asyncio.run(check())
