"""Reset only the requested conversation and keep the owner's application data."""

import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from personal_agent import bootstrap
from personal_agent.agent.models import ModelReply
from personal_agent.agent.service import ChatValidationError
from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.config import Settings
from personal_agent.entrypoints import api
from personal_agent.skills.models import SkillRevision


class Model:
    def __init__(self, **kwargs):
        self.calls = []

    async def respond(self, *, items, instructions, **kwargs):
        self.calls.append((list(items), instructions))
        if items[-1].get("content") == "interrupt":
            raise RuntimeError("Simulated interrupted turn")
        return ModelReply(
            items=[{"role": "assistant", "content": "Okay."}], text="Okay.", tool_calls=[]
        )

    async def aclose(self):
        pass


def settings_for(tmp_path, owner="alice", **overrides):
    values = dict(
        _env_file=None,
        model_api_key="test-key",
        api_token="test-api-token",
        database_url=None,
        data_dir=tmp_path,
        skill_bundle_path=tmp_path / "no-bundle.json",
        owner_id=owner,
        ibkr_flex_token=None,
        ibkr_flex_query_id=None,
    )
    values.update(overrides)
    return Settings(**values)


def user_messages(runtime):
    return [
        item["content"] for item in runtime.morning.model.calls[-1][0] if item.get("role") == "user"
    ]


def test_reset_is_scoped_and_preserves_saved_data(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)

    async def check():
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:
            await runtime.chat("old main", "main")
            await runtime.chat("another main", "main")
            await runtime.chat("old other", "other")
            day = date(2026, 11, 5)
            snapshot = PortfolioReport(day, day, ())
            await runtime.portfolio.repository.save("alice", snapshot)
            skill = SkillRevision(
                skill_id="test-skill",
                owner_id="alice",
                revision=1,
                name="Test skill",
                description="Saved guidance",
                instructions="Keep this saved instruction.",
                required_tool_ids=(),
                created_at=datetime.now(timezone.utc),
            )
            await runtime.skills.save_revision(skill, expected_latest_revision=None)
            await runtime.skills.activate(owner_id="alice", skill_id="test-skill", revision=1)
            await runtime.morning.repository.claim("alice", day, "Existing encouragement")
        async with bootstrap.open_runtime(settings_for(tmp_path, owner="bob")) as runtime:
            await runtime.chat("bob main", "main")
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:
            reset = await runtime.chat("/new", "main")
            assert "cleared" in reset.text
            assert reset.conversation_id == "main"
            assert runtime.morning.model.calls == []
            await runtime.chat("fresh main", "main")
            assert user_messages(runtime) == ["fresh main"]
            await runtime.chat("continue other", "other")
            assert user_messages(runtime) == ["old other", "continue other"]
            assert await runtime.portfolio.latest(owner_id="alice") == snapshot
            assert len(await runtime.skills.list_active(owner_id="alice")) == 1
            assert (await runtime.morning.repository.recent("alice"))[
                day
            ] == "Existing encouragement"
        async with bootstrap.open_runtime(settings_for(tmp_path, owner="bob")) as runtime:
            await runtime.chat("bob continues", "main")
            assert user_messages(runtime) == ["bob main", "bob continues"]

    asyncio.run(check())


def test_reset_recovers_interrupted_conversation(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)

    async def check():
        async with bootstrap.open_runtime(settings_for(tmp_path)) as runtime:
            with pytest.raises(RuntimeError, match="interrupted"):
                await runtime.chat("interrupt", "main")
            with pytest.raises(ChatValidationError, match="previous turn was interrupted"):
                await runtime.chat("try again", "main")
            await runtime.chat("/new", "main")
            await runtime.chat("recovered", "main")
            assert user_messages(runtime) == ["recovered"]

    asyncio.run(check())


def test_api_reset_requires_authentication(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)
    monkeypatch.setattr(api, "open_runtime", lambda: bootstrap.open_runtime(settings_for(tmp_path)))
    headers = {"Authorization": "Bearer test-api-token"}
    with TestClient(api.create_app()) as client:
        assert (
            client.post(
                "/chat", headers=headers, json={"message": "old", "conversation_id": "main"}
            ).status_code
            == 200
        )
        assert (
            client.post("/chat", json={"message": "/new", "conversation_id": "main"}).status_code
            == 401
        )
        response = client.post(
            "/chat", headers=headers, json={"message": "/new", "conversation_id": "main"}
        )
        assert response.status_code == 200 and "cleared" in response.json()["text"]
        assert (
            client.post(
                "/chat", headers=headers, json={"message": "fresh", "conversation_id": "main"}
            ).status_code
            == 200
        )
        assert user_messages(client.app.state.runtime) == ["fresh"]
