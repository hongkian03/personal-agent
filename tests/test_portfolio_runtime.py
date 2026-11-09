"""Integration checks for the portfolio runtime; no broker or model network calls."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from personal_agent import bootstrap
from personal_agent.agent.tools import ToolError
from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.config import Settings
from personal_agent.core.context import ExecutionContext
from personal_agent.entrypoints import api


def settings_for(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "model_api_key": None,
        "api_token": "runtime-test-token",
        "data_dir": tmp_path,
        "skill_bundle_path": tmp_path / "no-bundle.json",
        "database_url": None,
        "owner_id": "runtime-test-owner",
        "ibkr_flex_token": None,
        "ibkr_flex_query_id": None,
    }
    values.update(overrides)
    return Settings(**values)


def sample_report():
    day = date(2026, 11, 5)
    return PortfolioReport(
        as_of=day,
        period_start=day,
        accounts=(
            {
                "account_id": "U-TEST",
                "base_currency": "USD",
                "as_of": day.isoformat(),
                "period_start": day.isoformat(),
                "positions": [],
                "cash_balances": [{"currency": "USD", "ending_cash": "500.00"}],
                "nav": {"ending_value": "500.00"},
                "performance": [],
                "cash_transactions": [],
            },
        ),
    )


class FakeFlexClient:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def fetch_report(self):
        self.calls += 1
        return sample_report()

    async def aclose(self):
        self.closed = True


async def wait_for_snapshot(runtime):
    async with asyncio.timeout(3):
        while await runtime.portfolio.latest(owner_id=runtime.settings.owner_id) is None:
            await asyncio.sleep(0.01)


def test_runtime_syncs_on_start_and_retains_snapshot_after_restart(tmp_path):
    async def run():
        settings = settings_for(tmp_path)
        client = FakeFlexClient()
        async with bootstrap.open_runtime(settings, ibkr_client=client) as runtime:
            await wait_for_snapshot(runtime)
            assert client.calls == 1
            result = await runtime.portfolio.get_summary(owner_id=settings.owner_id)
            assert result["as_of"] == "2026-11-05"
            assert result["configured"] is True
        assert client.closed
        # A restart without credentials still preserves the report on durable storage.
        async with bootstrap.open_runtime(settings) as runtime:
            stored = await runtime.portfolio.latest(owner_id=settings.owner_id)
            assert stored == sample_report()
            result = await runtime.portfolio.get_status(owner_id=settings.owner_id)
            assert result["configured"] is False
            assert result["as_of"] == "2026-11-05"

    asyncio.run(run())


def test_runtime_cancels_inflight_sync_before_closing_client(tmp_path):
    async def run():
        class BlockingClient:
            def __init__(self):
                self.started = asyncio.Event()
                self.cancelled = False
                self.closed = False

            async def fetch_report(self):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

            async def aclose(self):
                assert self.cancelled
                self.closed = True

        client = BlockingClient()
        async with bootstrap.open_runtime(settings_for(tmp_path), ibkr_client=client):
            await asyncio.wait_for(client.started.wait(), timeout=3)
        assert client.closed
        assert not any(
            task.get_name() == "ibkr-portfolio-sync" and not task.done()
            for task in asyncio.all_tasks()
        )

    asyncio.run(run())


def test_portfolio_api_auth_sync_and_claim(monkeypatch, tmp_path):
    settings = settings_for(tmp_path)
    fake = FakeFlexClient()
    monkeypatch.setattr(
        api, "open_runtime", lambda: bootstrap.open_runtime(settings, ibkr_client=fake)
    )
    headers = {"Authorization": "Bearer runtime-test-token"}
    with TestClient(api.create_app()) as client:
        for method, path in (
            ("get", "/portfolio/status"),
            ("post", "/portfolio/sync"),
            ("post", "/portfolio/morning/claim"),
        ):
            assert getattr(client, method)(path).status_code == 401
            assert (
                getattr(client, method)(
                    path, headers={"Authorization": "Bearer wrong-token"}
                ).status_code
                == 401
            )
        response = client.post("/portfolio/sync", headers=headers)
        assert response.status_code == 200
        assert response.json()["configured"] is True
        assert response.json()["as_of"] == "2026-11-05"
        assert client.get("/portfolio/status", headers=headers).json()["last_error"] is None

        calls = []

        async def due():
            calls.append(True)
            return "IBKR portfolio as of 2026-11-05."

        client.app.state.runtime.portfolio_morning = SimpleNamespace(claim_due=due)
        assert client.post("/portfolio/morning/claim").status_code == 401
        assert calls == []
        response = client.post("/portfolio/morning/claim", headers=headers)
        assert response.json() == {"text": "IBKR portfolio as of 2026-11-05."}
        assert calls == [True]
        client.app.state.runtime.portfolio_morning = None
        assert client.post("/portfolio/morning/claim", headers=headers).status_code == 204
    assert fake.closed


def test_tools_require_portfolio_scope_and_chat_receives_dated_data_guidance(tmp_path):
    async def run():
        settings = settings_for(tmp_path)
        async with bootstrap.open_runtime(settings) as runtime:
            unprivileged = ExecutionContext(
                owner_id=settings.owner_id, run_id="test-run", granted_scopes=frozenset()
            )
            assert not any(
                item.id.startswith("portfolio.") for item in runtime.tools.definitions(unprivileged)
            )
            with pytest.raises(ToolError, match="not authorized"):
                await runtime.tools.invoke(
                    tool_id="portfolio.get_summary", arguments={}, context=unprivileged
                )

            class CapturingGraph:
                config = None

                async def aget_state(self, config):
                    return SimpleNamespace(next=(), values={})

                async def ainvoke(self, inputs, config):
                    self.config = config
                    return {"text": "Portfolio connection is not configured."}

            graph = CapturingGraph()
            runtime.graph = graph
            await runtime.chat("Show my portfolio", conversation_id="test-conversation")
            context = graph.config["configurable"]["context"]
            assert context.owner_id == settings.owner_id
            assert "portfolio:read" in context.granted_scopes
            definitions = runtime.tools.definitions(context)
            assert any(item.id == "portfolio.get_summary" for item in definitions)
            instructions = graph.config["configurable"]["instructions"].lower()
            assert "cached ibkr flex reports" in instructions
            assert "report date" in instructions
            assert "do not provide live quotes" in instructions
            result = await runtime.tools.invoke(
                tool_id="portfolio.get_summary", arguments={}, context=context
            )
            assert result["status"] == "not_configured"

    asyncio.run(run())


def test_flex_settings_fail_closed_for_partial_credentials(tmp_path):
    with pytest.raises(ValidationError, match="together") as error:
        settings_for(tmp_path, ibkr_flex_token="dummy-flex-token")
    assert "dummy-flex-token" not in str(error.value)
    with pytest.raises(ValidationError, match="together"):
        settings_for(tmp_path, ibkr_flex_query_id="12345")
    configured = settings_for(
        tmp_path,
        ibkr_flex_token="dummy-flex-token",
        ibkr_flex_query_id="12345",
        ibkr_account_ids=" U1, U2,U1 ",
        portfolio_focus_symbols=" aapl,MSFT,aapl ",
    )
    assert configured.ibkr_accounts == ("U1", "U2")
    assert configured.focused_symbols == ("AAPL", "MSFT")
    assert "dummy-flex-token" not in repr(configured)
