"""API failures provide actionable context without dumping provider or user data."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from personal_agent.agent.models import ModelError
from personal_agent.agent.service import AgentRuntime, ChatValidationError
from personal_agent.config import Settings
from personal_agent.entrypoints import api


def test_custom_error_message_and_cause_location_are_logged_safely(caplog):
    try:
        try:
            raise OSError("private provider body, credentials, and prompt")
        except OSError as exc:
            raise ModelError("OpenAI stream ended without a completed response") from exc
    except ModelError as exc:
        api.log_failure("chat_stream_failed", exc, run_id="run123", conversation_id="main")
    assert "error_type=ModelError" in caplog.text
    assert 'message="OpenAI stream ended without a completed response"' in caplog.text
    assert "cause_type=OSError" in caplog.text
    assert "source=test_api_logging.py:" in caplog.text
    assert "cause_source=test_api_logging.py:" in caplog.text
    assert "run_id=run123 conversation_id=main" in caplog.text
    assert "private provider body" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_unexpected_errors_and_timeout_messages_do_not_expose_exception_contents(caplog):
    api.log_failure("portfolio_refresh_failed", RuntimeError("secret account details"))
    api.log_failure("chat_failed", TimeoutError("secret timeout request body"))
    assert "Unexpected application error" in caplog.text
    assert "Request exceeded its execution deadline." in caplog.text
    assert "secret" not in caplog.text


def test_application_messages_are_single_line_and_bounded(caplog):
    api.log_failure("chat_failed", ModelError("Line one\nLine two" + "x" * 1000))
    message = caplog.records[-1].getMessage()
    assert "Line one\\nLine two" in message
    assert "\n" not in message
    assert len(message) < 800


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_api_logs_model_explanation_and_keeps_client_errors_generic(tmp_path, caplog, path):
    async def check():
        app = api.create_app()
        app.state.runtime = AgentRuntime(
            settings=Settings(
                _env_file=None, api_token="test-token", progress_enabled=False,
                persona_path=tmp_path / "missing.md",
            ),
            graph=SimpleNamespace(
                aget_state=AsyncMock(return_value=SimpleNamespace(next=(), values={})),
                ainvoke=AsyncMock(side_effect=ModelError("OpenAI returned an empty response")),
            ),
            tools=SimpleNamespace(definitions=lambda _: []),
            skills=SimpleNamespace(list_active=AsyncMock(return_value=[])),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                path, headers={"Authorization": "Bearer test-token"},
                json={"message": "private user prompt", "conversation_id": "main"},
            )
        warnings = [record.getMessage() for record in caplog.records
                    if record.name == api.__name__ and record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "OpenAI returned an empty response" in warnings[0]
        assert "conversation_id=main" in warnings[0]
        assert "private user prompt" not in caplog.text
        assert "test-token" not in caplog.text
        assert "OpenAI returned an empty response" not in response.text
        if path == "/chat/stream":
            frames = [frame for frame in response.text.split("\n\n") if "event: error" in frame]
            terminal = json.loads(frames[0].split("data: ")[1])
            assert f"run_id={terminal['run_id']}" in warnings[0]
            assert response.status_code == 200
        else:
            assert response.status_code == 502
    asyncio.run(check())


def test_validation_logs_explain_rejected_conversations(caplog):
    api.log_failure(
        "chat_stream_rejected",
        ChatValidationError("The previous turn was interrupted. Start a new conversation."),
        conversation_id="main",
    )
    assert "error_type=ChatValidationError" in caplog.text
    assert "previous turn was interrupted" in caplog.text

