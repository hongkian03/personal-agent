"""Portfolio replacement, currency boundaries, authorization and daily claims."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine

from personal_agent.agent.tools import ToolError, ToolRegistry
from personal_agent.capabilities.motivation import workflows as motivation
from personal_agent.capabilities.motivation.workflows import MorningMessage, morning_date
from personal_agent.capabilities.portfolio import briefing
from personal_agent.capabilities.portfolio.briefing import (
    PortfolioMorningMessage,
    format_briefing,
    price,
)
from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.capabilities.portfolio.tools import register_portfolio_tools
from personal_agent.capabilities.portfolio.workflows import PortfolioService
from personal_agent.core.context import ExecutionContext
from personal_agent.storage.repositories.morning import MorningMessageRepository
from personal_agent.storage.repositories.portfolio import PortfolioRepository


def report(day=date(2026, 11, 5), symbols=("AAPL",), account_id="U123", currency="USD"):
    positions = [
        {
            "conid": str(index),
            "symbol": symbol,
            "description": symbol,
            "currency": currency,
            "quantity": "2",
            "mark_price": "100.25",
            "market_value": "200.50",
            "cost_basis": "150.00",
            "unrealized_pnl": "50.50",
        }
        for index, symbol in enumerate(symbols)
    ]
    return PortfolioReport(
        day,
        day,
        (
            {
                "account_id": account_id,
                "base_currency": currency,
                "as_of": day.isoformat(),
                "period_start": day.isoformat(),
                "positions": positions,
                "cash_balances": [{"currency": currency, "ending_cash": "10"}],
                "nav": {"ending_value": "210.50", "mtm_pnl": "5.25", "dividends": "1.23"},
                "performance": [
                    {
                        "conid": "0",
                        "symbol": "AAPL",
                        "currency": currency,
                        "mtm_pnl": "5.25",
                        "previous_close": "98",
                        "close_price": "100.25",
                    }
                ],
                "cash_transactions": [],
            },
        ),
    )


@pytest.fixture
def engine(tmp_path):
    result = create_engine(f"sqlite:///{tmp_path / 'portfolio.sqlite'}")
    yield result
    result.dispose()


def run(coro):
    return asyncio.run(coro)


def test_owner_isolation_and_whole_report_replacement(engine):
    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        await repository.save("alice", report(symbols=("AAPL", "MSFT")))
        await repository.save("bob", report(account_id="U456", symbols=("IBM",)))
        replacement = report(symbols=("MSFT",))
        await repository.save("alice", replacement)
        await repository.save("alice", replacement)
        assert (await repository.latest("alice")).accounts[0]["positions"][0]["symbol"] == "MSFT"
        assert len((await repository.latest("alice")).accounts[0]["positions"]) == 1
        assert (await repository.latest("bob")).accounts[0]["positions"][0]["symbol"] == "IBM"
        assert await repository.latest("missing") is None
        newer = report(day=date(2026, 11, 6), symbols=())
        await repository.save("alice", newer)
        assert not await repository.save("alice", report(day=date(2026, 11, 4)))
        latest = await repository.latest("alice")
        assert latest.as_of == date(2026, 11, 6)
        assert latest.accounts[0]["positions"] == []

    run(check())


def test_bounded_reads_and_currency_separation(engine):
    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        first = report(symbols=tuple(f"S{i}" for i in range(125)))
        second = report(account_id="U456", currency="SGD")
        combined = PortfolioReport(
            first.as_of, first.period_start, first.accounts + second.accounts
        )
        await repository.save("alice", combined)
        service = PortfolioService(repository, None, "alice")
        page = await service.get_holdings(owner_id="alice", offset=100, limit=25)
        assert len(page["items"]) == 25 and page["has_more"]
        filtered = await service.get_prices(owner_id="alice", symbol="aapl")
        assert len(filtered["items"]) == 1
        assert filtered["price_type"] == "report_date_close"
        summary = await service.get_summary(owner_id="alice")
        assert [a["base_currency"] for a in summary["accounts"]] == ["USD", "SGD"]
        assert "total_value" not in summary
        with pytest.raises(ValueError):
            await service.get_holdings(owner_id="alice", limit=101)
        with pytest.raises(PermissionError):
            await service.get_summary(owner_id="bob")
        text = format_briefing(combined, today=date(2026, 11, 6))
        assert "USD 210.50" in text and "SGD 210.50" in text
        assert "Largest holdings in USD" in text and "Largest holdings in SGD" in text

    run(check())


def test_dividend_filter_happens_before_pagination(engine):
    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        snapshot = report()
        snapshot.accounts[0]["cash_transactions"] = [
            {"transaction_id": "1", "type": "Deposit", "amount": "100", "currency": "USD"},
            {"transaction_id": "2", "type": "Dividends", "amount": "12", "currency": "USD"},
            {"transaction_id": "3", "type": "Withholding Tax", "amount": "-3", "currency": "USD"},
        ]
        await repository.save("alice", snapshot)
        result = await PortfolioService(repository, None, "alice").get_dividends(
            owner_id="alice",
            limit=1,
        )
        assert result["total"] == 2 and result["has_more"]
        assert result["items"][0]["transaction_id"] == "2"

    run(check())


def test_sync_coalesces_and_retains_last_good_data_on_failure(engine):
    class Client:
        calls = 0
        fail = False

        async def fetch_report(self):
            self.calls += 1
            await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeError("SECRET_TOKEN must never escape")
            return report()

    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        client = Client()
        service = PortfolioService(repository, client, "alice")
        await asyncio.gather(service.sync(force=True), service.sync(force=True))
        assert client.calls == 1
        client.fail = True
        status = await service.sync(force=True)
        assert status["last_error"] == "sync_failed"
        assert "SECRET" not in str(status)
        assert (await service.get_holdings(owner_id="alice"))["total"] == 1
        await service.sync()
        assert client.calls == 2

    run(check())


def test_tools_require_scope_and_trusted_owner(engine):
    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        await repository.save("alice", report())
        service = PortfolioService(repository, None, "alice")
        registry = ToolRegistry()
        register_portfolio_tools(registry, service)
        no_scope = ExecutionContext(owner_id="alice", run_id="test", granted_scopes=frozenset())
        assert not registry.definitions(no_scope)
        with pytest.raises(ToolError):
            await registry.invoke(tool_id="portfolio.get_summary", arguments={}, context=no_scope)
        allowed = ExecutionContext(
            owner_id="alice",
            run_id="test",
            granted_scopes=frozenset({"portfolio:read"}),
        )
        with pytest.raises(ToolError):
            await registry.invoke(
                tool_id="portfolio.get_holdings",
                arguments={"owner_id": "bob"},
                context=allowed,
            )
        wrong_owner = ExecutionContext(
            owner_id="bob",
            run_id="test",
            granted_scopes=frozenset({"portfolio:read"}),
        )
        with pytest.raises(PermissionError):
            await registry.invoke(
                tool_id="portfolio.get_summary", arguments={}, context=wrong_owner
            )

    run(check())


def test_morning_window_no_data_staleness_and_claim_namespace(engine, monkeypatch):
    class Clock:
        current = datetime(2026, 11, 6, 7, 59, tzinfo=ZoneInfo("America/New_York"))

        @classmethod
        def now(cls, zone):
            return cls.current.astimezone(zone)

    monkeypatch.setattr(briefing, "datetime", Clock)

    async def check():
        repository = PortfolioRepository(engine)
        morning = MorningMessageRepository(engine)
        await repository.initialize()
        await morning.initialize()
        service = PortfolioService(repository, None, "alice")
        workflow = PortfolioMorningMessage(service, morning, "alice", ZoneInfo("America/New_York"))
        assert await workflow.claim_due() is None
        Clock.current += timedelta(minutes=1)
        assert await workflow.claim_due() is None
        assert await morning.recent(workflow.claim_owner) == {}
        await repository.save("alice", report(day=date(2026, 11, 2)))
        # A motivational claim must not suppress the portfolio briefing.
        assert await morning.claim("alice", date(2026, 11, 6), "Encouragement")
        text = await workflow.claim_due()
        assert "older than the previous weekday" in text
        assert "2026-11-02" in text
        assert await workflow.claim_due() is None
        Clock.current = datetime(2026, 11, 7, 8, 15, tzinfo=ZoneInfo("America/New_York"))
        assert await workflow.claim_due() is None

    run(check())


def test_small_prices_retain_precision():
    assert price("0.004321", "USD") == "USD 0.004321"
    assert price("0.00000001", "USD") != "USD 0.00"


def test_per_account_and_price_dates_are_preserved(engine):
    async def check():
        repository = PortfolioRepository(engine)
        await repository.initialize()
        snapshot = report(day=date(2026, 11, 4))
        later = report(day=date(2026, 11, 5), account_id="U456")
        later.accounts[0]["positions"][0]["price_as_of"] = "2026-11-03"
        combined = PortfolioReport(
            snapshot.as_of, snapshot.period_start, snapshot.accounts + later.accounts
        )
        await repository.save("alice", combined)
        service = PortfolioService(repository, None, "alice")
        rows = (await service.get_holdings(owner_id="alice"))["items"]
        assert [row["as_of"] for row in rows] == ["2026-11-04", "2026-11-05"]
        prices = (await service.get_prices(owner_id="alice"))["items"]
        assert prices[1]["price_as_of"] == "2026-11-03"
        text = format_briefing(combined, today=date(2026, 11, 6), focused_symbols=("AAPL",))
        assert "as of 2026-11-04" in text and "as of 2026-11-05" in text
        assert "price date 2026-11-03" in text

    run(check())


def test_concurrent_morning_claims_only_return_one_message(engine, monkeypatch):
    class Clock:
        @classmethod
        def now(cls, zone):
            return datetime(2026, 11, 6, 8, 0, tzinfo=zone)

    monkeypatch.setattr(briefing, "datetime", Clock)

    async def check():
        repository = PortfolioRepository(engine)
        morning = MorningMessageRepository(engine)
        await repository.initialize()
        await morning.initialize()
        await repository.save("alice", report())
        service = PortfolioService(repository, None, "alice")
        first = PortfolioMorningMessage(service, morning, "alice", ZoneInfo("America/New_York"))
        second = PortfolioMorningMessage(service, morning, "alice", ZoneInfo("America/New_York"))
        results = await asyncio.gather(first.claim_due(), second.claim_due())
        assert sum(item is not None for item in results) == 1

    run(check())


def test_weekends_send_motivation_only_but_sync_and_monday_continue(engine, monkeypatch):
    zone = ZoneInfo("America/New_York")

    class Clock:
        current = datetime(2026, 11, 7, 8, 0, tzinfo=zone)

        @classmethod
        def now(cls, target_zone):
            return cls.current.astimezone(target_zone)

    class Client:
        calls = 0

        async def fetch_report(self):
            self.calls += 1
            return report(day=date(2026, 11, 6))

    monkeypatch.setattr(briefing, "datetime", Clock)
    monkeypatch.setattr(motivation, "datetime", Clock)

    async def check():
        repository = PortfolioRepository(engine)
        morning = MorningMessageRepository(engine)
        await repository.initialize()
        await morning.initialize()
        client = Client()
        service = PortfolioService(repository, client, "alice")
        workflow = PortfolioMorningMessage(service, morning, "alice", zone)
        model = SimpleNamespace(
            respond=AsyncMock(return_value=SimpleNamespace(text="One small step.", tool_calls=()))
        )
        encouragement = MorningMessage(model, morning, "alice", zone)
        for day in (7, 8):
            Clock.current = datetime(2026, 11, day, 8, 0, tzinfo=zone)
            assert await encouragement.claim_due() == "One small step."
            assert await encouragement.claim_due() is None
            await service.sync(force=True)
            assert await workflow.claim_due() is None
            assert await morning.recent(workflow.claim_owner) == {}
        assert client.calls == 2
        assert model.respond.await_count == 2
        Clock.current = datetime(2026, 11, 9, 8, 0, tzinfo=zone)
        assert morning_date(Clock.current) == date(2026, 11, 9)
        assert await encouragement.claim_due() == "One small step."
        text = await workflow.claim_due()
        assert text is not None and "2026-11-06" in text
        assert await workflow.claim_due() is None

    run(check())


@pytest.mark.parametrize("day", range(2, 9))
def test_daily_motivation_keeps_morning_window(day):
    zone = ZoneInfo("America/New_York")
    for hour, minute, due in ((7, 59, False), (8, 0, True), (8, 14, True), (8, 15, False)):
        now = datetime(2026, 11, day, hour, minute, tzinfo=zone)
        assert morning_date(now) == (now.date() if due else None)
