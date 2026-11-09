"""Hosted search, readable citations, and chat budgets without external requests."""

import asyncio
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import BadRequestError, Omit
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseIncompleteEvent,
)
from pydantic import ValidationError

from personal_agent import bootstrap
from personal_agent.agent.models import ModelError, ModelReply, ToolCall
from personal_agent.agent.tools import ToolDefinition
from personal_agent.capabilities.motivation import workflows as motivation
from personal_agent.config import Settings
from personal_agent.integrations.llm.provider import OpenAIModel


def message(*parts):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": list(parts),
    }


def output_text(text, *annotations):
    return {"type": "output_text", "text": text, "annotations": list(annotations)}


def citation(text, marker, *, url="https://example.org/report", title="Example report"):
    start = text.index(marker)
    return {
        "type": "url_citation",
        "start_index": start,
        "end_index": start + len(marker),
        "url": url,
        "title": title,
    }


def response_for(output, *, status="completed"):
    return Response.model_validate(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "model": "gpt-6-luna",
            "status": status,
            "output": output,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
        }
    )


def function_tool():
    return ToolDefinition(
        id="portfolio.get_holdings",
        description="Read saved holdings.",
        input_schema={"type": "object", "properties": {}},
        required_scopes=frozenset({"portfolio:read"}),
    )


class ResponseStream:
    """Synthetic SSE events consumed by the actual SDK stream accumulator."""

    def __init__(self, response):
        self.final = response
        self.response = httpx.Response(200)

    async def __aiter__(self):
        yield ResponseCreatedEvent(
            type="response.created",
            sequence_number=0,
            response=self.final.model_copy(update={"status": "in_progress", "output": []}),
        )
        completed = self.final.status == "completed"
        event = ResponseCompletedEvent if completed else ResponseIncompleteEvent
        yield event(
            type="response.completed" if completed else "response.incomplete",
            sequence_number=1,
            response=self.final,
        )


async def invoke_model(response, *, tools=(), **kwargs):
    model = OpenAIModel(api_key="not-a-real-key", model="gpt-6-luna", web_search_context_size="low")
    create = AsyncMock(return_value=ResponseStream(response))
    model.client.responses.create = create
    try:
        reply = await model.respond(
            items=[{"role": "user", "content": "Look up the latest information."}],
            instructions="Use available sources.",
            tools=tools,
            **kwargs,
        )
        return reply, create
    finally:
        await model.aclose()


def test_hosted_search_coexists_with_functions_and_preserves_response_items():
    async def check():
        output = [
            {
                "id": "rs_test",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "opaque-reasoning-context",
            },
            {
                "id": "ws_search",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "company updates"},
            },
            {
                "id": "ws_open",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "open_page", "url": "https://example.org/report"},
            },
            {
                "id": "fc_test",
                "type": "function_call",
                "call_id": "call_holdings",
                "name": "portfolio__get_holdings",
                "arguments": "{}",
                "status": "completed",
            },
        ]
        response = response_for(output)
        expected = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        reply, create = await invoke_model(
            response,
            tools=(function_tool(),),
            hosted_tools=frozenset({"web_search"}),
            max_hosted_tool_calls=2,
        )
        request = create.await_args.kwargs
        assert request["stream"] is True
        assert request["tools"] == [
            {
                "type": "function",
                "name": "portfolio__get_holdings",
                "description": "Read saved holdings.",
                "parameters": {"type": "object", "properties": {}},
                "strict": False,
            },
            {"type": "web_search", "search_context_size": "low"},
        ]
        assert request["max_tool_calls"] == 2
        assert request["store"] is False
        assert "reasoning.encrypted_content" in request["include"]
        assert request["parallel_tool_calls"] is False
        assert reply.items == expected
        assert reply.hosted_tool_calls == 2
        assert reply.tool_calls == [ToolCall("call_holdings", "portfolio__get_holdings", "{}")]

    asyncio.run(check())


def test_no_hosted_tools_leaves_plain_generation_without_search():
    async def check():
        reply, create = await invoke_model(response_for([message(output_text("Hello."))]))
        assert reply.text == "Hello."
        assert reply.hosted_tool_calls == 0
        assert create.await_args.kwargs["tools"] == []
        assert isinstance(create.await_args.kwargs["max_tool_calls"], Omit)

    asyncio.run(check())


def test_citations_are_readable_deduplicated_and_raw_history_is_unchanged():
    async def check():
        marker = "\ue200cite\ue202turn0search0\ue201"
        first = "Café ☕ grew. " + marker
        second = "More details. " + marker
        other_marker = "\ue200cite\ue202turn0search1\ue201"
        third = "A second source. " + other_marker
        response = response_for(
            [
                message(
                    output_text(first, citation(first, marker)),
                    output_text(second, citation(second, marker)),
                    output_text(
                        third,
                        citation(
                            third,
                            other_marker,
                            url="https://example.net/news",
                            title="Another source",
                        ),
                    ),
                )
            ]
        )
        original = deepcopy(response.output[0].model_dump(mode="json", exclude_none=True))
        reply, _ = await invoke_model(response)
        assert "Café ☕ grew." in reply.text
        assert "More details." in reply.text
        assert "A second source." in reply.text
        assert "[1]" in reply.text and "[2]" in reply.text
        assert reply.text.count("https://example.org/report") == 1
        assert reply.text.count("https://example.net/news") == 1
        assert "Example report" in reply.text and "Another source" in reply.text
        assert "\ue200" not in reply.text and "turn0search" not in reply.text
        assert reply.items == [original]

    asyncio.run(check())


def test_citation_on_claim_text_preserves_the_claim_and_adds_a_reference():
    async def check():
        text = "The company announced a dividend."
        annotation = citation(text, "announced a dividend")
        reply, _ = await invoke_model(response_for([message(output_text(text, annotation))]))
        assert "announced a dividend" in reply.text
        assert "[1]" in reply.text and "https://example.org/report" in reply.text

    asyncio.run(check())


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "file:///etc/passwd", "https:///missing-host", "https://bad host/"],
)
def test_unsafe_citation_urls_are_not_emitted(url):
    async def check():
        marker = "\ue200cite\ue202turn0search0\ue201"
        text = "A claim. " + marker
        reply, _ = await invoke_model(
            response_for([message(output_text(text, citation(text, marker, url=url)))])
        )
        assert "A claim." in reply.text
        assert url not in reply.text
        assert "\ue200" not in reply.text

    asyncio.run(check())


def test_uncited_plain_text_and_refusals_remain_visible():
    async def check():
        plain, _ = await invoke_model(response_for([message(output_text("No search needed."))]))
        assert plain.text == "No search needed."
        refusal, _ = await invoke_model(
            response_for([message({"type": "refusal", "refusal": "I cannot help with that."})])
        )
        assert refusal.text == "I cannot help with that."

    asyncio.run(check())


def test_incomplete_response_is_not_presented_as_a_completed_answer():
    async def check():
        with pytest.raises(ModelError, match="did not complete"):
            await invoke_model(
                response_for([message(output_text("Partial answer"))], status="incomplete"),
                hosted_tools=frozenset({"web_search"}),
            )

    asyncio.run(check())


def test_provider_rejection_does_not_silently_retry_without_search():
    async def check():
        model = OpenAIModel(api_key="not-a-real-key", model="gpt-6-luna")
        rejection = BadRequestError(
            "sensitive provider error",
            response=httpx.Response(400, request=httpx.Request("POST", "https://api.test/")),
            body={"message": "sensitive provider error"},
        )
        create = AsyncMock(side_effect=rejection)
        model.client.responses.create = create
        try:
            with pytest.raises(ModelError) as caught:
                await model.respond(
                    items=[{"role": "user", "content": "Search now."}],
                    instructions="Search when needed.",
                    tools=(),
                    hosted_tools=frozenset({"web_search"}),
                )
            assert "sensitive provider error" not in str(caught.value)
            assert create.await_count == 1
        finally:
            await model.aclose()

    asyncio.run(check())


def settings_for(tmp_path, **overrides):
    values = dict(
        _env_file=None,
        model_api_key="not-a-real-key",
        database_url=None,
        data_dir=tmp_path,
        skill_bundle_path=tmp_path / "no-bundle.json",
        owner_id="search-test-owner",
        ibkr_flex_token=None,
        ibkr_flex_query_id=None,
    )
    values.update(overrides)
    return Settings(**values)


def test_chat_search_budget_is_shared_across_tool_rounds_and_resets_next_turn(
    monkeypatch, tmp_path
):
    class Model:
        def __init__(self, **kwargs):
            self.calls = []

        async def respond(self, *, items, instructions, tools, **kwargs):
            index = len(self.calls)
            self.calls.append(kwargs)
            assert "current public information" in instructions
            if index < 2:
                call_id = f"call_time_{index}"
                return ModelReply(
                    items=[
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "runtime__current_time",
                            "arguments": "{}",
                        }
                    ],
                    text="",
                    tool_calls=[ToolCall(call_id, "runtime__current_time", "{}")],
                    hosted_tool_calls=2 if index == 0 else 1,
                )
            return ModelReply(
                items=[{"role": "assistant", "content": "Research complete."}],
                text="Research complete.",
                tool_calls=[],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)

    async def check():
        settings = settings_for(tmp_path, web_search_enabled=True, web_search_max_calls=3)
        async with bootstrap.open_runtime(settings) as runtime:
            await runtime.chat("Research a company.", "research")
            calls = runtime.morning.model.calls
            assert len(calls) == 3
            assert calls[0]["hosted_tools"] == frozenset({"web_search"})
            assert calls[0]["max_hosted_tool_calls"] == 3
            assert calls[1]["hosted_tools"] == frozenset({"web_search"})
            assert calls[1]["max_hosted_tool_calls"] == 1
            assert "web_search" not in calls[2].get("hosted_tools", ())
            await runtime.chat("Research another company.", "research")
            assert calls[3]["hosted_tools"] == frozenset({"web_search"})
            assert calls[3]["max_hosted_tool_calls"] == 3

    asyncio.run(check())


def test_disabled_search_is_not_offered_to_chat(monkeypatch, tmp_path):
    class Model:
        def __init__(self, **kwargs):
            self.calls = []

        async def respond(self, **kwargs):
            self.calls.append(kwargs)
            assert "Web search is disabled" in kwargs["instructions"]
            return ModelReply(
                items=[{"role": "assistant", "content": "Hello."}],
                text="Hello.",
                tool_calls=[],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)

    async def check():
        async with bootstrap.open_runtime(
            settings_for(tmp_path, web_search_enabled=False)
        ) as runtime:
            await runtime.chat("Look something up.")
            assert "web_search" not in runtime.morning.model.calls[0].get("hosted_tools", ())

    asyncio.run(check())


def test_morning_encouragement_does_not_request_hosted_search(monkeypatch, tmp_path):
    class Clock:
        @classmethod
        def now(cls, zone):
            return datetime(2026, 11, 9, 8, 0, tzinfo=zone)

    class Model:
        def __init__(self, **kwargs):
            self.calls = []

        async def respond(self, **kwargs):
            self.calls.append(kwargs)
            return ModelReply(
                items=[{"role": "assistant", "content": "Take one small step today."}],
                text="Take one small step today.",
                tool_calls=[],
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(bootstrap, "OpenAIModel", Model)
    monkeypatch.setattr(motivation, "datetime", Clock)

    async def check():
        async with bootstrap.open_runtime(
            settings_for(tmp_path, web_search_enabled=True)
        ) as runtime:
            assert await runtime.morning.claim_due() == "Take one small step today."
            request = runtime.morning.model.calls[0]
            assert request["tools"] == ()
            assert "web_search" not in request.get("hosted_tools", ())

    asyncio.run(check())


@pytest.mark.parametrize(
    "overrides",
    [
        {"web_search_context_size": "unbounded"},
        {"web_search_max_calls": 0},
        {"web_search_max_calls": 11},
    ],
)
def test_search_settings_validate_limits(tmp_path, overrides):
    with pytest.raises(ValidationError):
        settings_for(tmp_path, **overrides)
