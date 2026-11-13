"""Exercise the deployed wrapper's HTTP contract without model/network calls."""

import asyncio
import json

import httpx
import pytest

from personal_agent.integrations.cognee.client import CogneeClient, CogneeError


def test_authenticated_wire_contract_and_stable_ingestion_identity():
    async def scenario():
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            assert request.headers["authorization"] == "Bearer secret"
            assert request.headers["accept"] == "application/json"
            assert body["owner_id"] == "owner"
            if request.url.path == "/remember":
                return httpx.Response(200, json={"status": "remembered", "memory_id": "stable"})
            if request.url.path == "/forget":
                return httpx.Response(200, json={"status": "forgotten", "memory_id": "stable"})
            assert body == {"owner_id": "owner", "query": "What do I prefer?", "limit": 3}
            return httpx.Response(200, json={"results": [{
                "memory_id": "stable", "text": "Short answers", "metadata": {"source": "chat"},
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = CogneeClient("http://memory/", "secret", client=http)
            for _ in range(2):
                await client.remember(
                    owner_id="owner", memory_id="stable", text="Short answers", metadata={},
                )
            memories = await client.recall(owner_id="owner", query="What do I prefer?", limit=3)
            await client.forget(owner_id="owner", memory_id="stable")
        assert requests[0] == requests[1]
        assert memories[0]["text"] == "Short answers"

    asyncio.run(scenario())


def test_retrieval_bounds_and_provenance_allowlist():
    async def scenario():
        def handle(request):
            assert json.loads(request.content)["limit"] == 20
            return httpx.Response(200, json={"results": [{
                "memory_id": str(i), "text": "x" * 10_000,
                "metadata": {"source": "chat", "conversation_id": "c", "secret": "hidden"},
            } for i in range(25)]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = CogneeClient("http://memory", "secret", client=http)
            results = await client.recall(owner_id="owner", query="query", limit=100)
        assert sum(len(entry["text"]) for entry in results) == 20_000
        assert all(len(entry["text"]) <= 8_000 for entry in results)
        assert all(entry["metadata"] == {"source": "chat", "conversation_id": "c"}
                   for entry in results)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 401, 403, 409, 500])
def test_errors_do_not_expose_remote_bodies_or_follow_redirects(status):
    async def scenario():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                status, json={"detail": "private source and token"},
                headers={"location": "https://outside.invalid/"},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True,
        ) as http:
            client = CogneeClient("http://memory", "secret", client=http)
            with pytest.raises(CogneeError) as failure:
                await client.recall(owner_id="owner", query="query")
        assert "private" not in str(failure.value)
        assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"status": "running", "memory_id": "stable"},
    {"status": "errored", "memory_id": "stable"},
    {"status": "remembered", "memory_id": "another"},
])
def test_ingestion_is_not_acknowledged_until_indexing_confirmed(result):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=result),
        )) as http:
            client = CogneeClient("http://memory", "secret", client=http)
            with pytest.raises(CogneeError):
                await client.remember(owner_id="o", memory_id="stable", text="fact", metadata={})

    asyncio.run(scenario())


def test_transport_failure_is_sanitized():
    async def scenario():
        def handle(request):
            raise httpx.ReadTimeout("sensitive request text", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = CogneeClient("http://memory", "secret", client=http)
            with pytest.raises(CogneeError, match="unavailable"):
                await client.recall(owner_id="o", query="query")

    asyncio.run(scenario())
