import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from personal_agent.entrypoints import api, cli
from personal_agent.integrations.cognee import CogneeClient


def test_api_export_uses_trusted_owner_and_download_headers(monkeypatch):
    snapshot = AsyncMock(return_value="<html>graph</html>")
    runtime = SimpleNamespace(
        settings=SimpleNamespace(api_token=SecretStr("test"), owner_id="trusted"),
        memory=SimpleNamespace(graph_snapshot=snapshot),
    )

    @asynccontextmanager
    async def opened():
        yield runtime

    monkeypatch.setattr(api, "open_runtime", opened)
    with TestClient(api.create_app()) as client:
        assert client.get("/memory/graph").status_code == 401
        response = client.get(
            "/memory/graph?full=true&owner_id=someone-else",
            headers={"Authorization": "Bearer test"},
        )
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        snapshot.assert_awaited_once_with(owner_id="trusted", full=True)


def test_client_graph_contract_and_cli_download(tmp_path, monkeypatch):
    async def check():
        def respond(request):
            assert request.headers["authorization"] == "Bearer test"
            return httpx.Response(
                200, text="<html>graph</html>", headers={"content-type": "text/html"},
            )

        constructor = httpx.AsyncClient
        async with constructor(transport=httpx.MockTransport(respond)) as http:
            client = CogneeClient("http://memory", "test", client=http)
            assert await client.graph_snapshot(owner_id="alice") == "<html>graph</html>"
        monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(api_token=SecretStr("test")))
        monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kwargs: constructor(
            transport=httpx.MockTransport(respond), **kwargs,
        ))
        output = tmp_path / "graph.html"
        await cli.export_graph(SimpleNamespace(
            api_url="http://agent", railway=False, full=False, output=output,
        ))
        assert output.read_text() == "<html>graph</html>"
    asyncio.run(check())
