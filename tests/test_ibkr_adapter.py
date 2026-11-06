"""IBKR report parsing, credential redaction and bounded HTTP behavior."""

import asyncio
import logging
from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest

from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.integrations.ibkr.client import FLEX_BASE_URL, FlexError, IBKRFlexClient
from personal_agent.integrations.ibkr.parser import MAX_REPORT_BYTES, parse_report


def statement(account="U123", day="20261104", positions=None):
    if positions is None:
        positions = (
            '<OpenPosition conid="12345" symbol="TEST" description="Example" currency="EUR" '
            'quantity="2.5" markPrice="10.20" positionValue="25.50" costBasisMoney="20" '
            'fifoPnlUnrealized="5.50" levelOfDetail="SUMMARY" reportDate="20261104"/>'
        )
    return f'''<FlexStatement accountId="{account}" fromDate="{day}" toDate="{day}">
      <AccountInformation currency="USD"/>
      <OpenPositions>{positions}</OpenPositions>
      <CashReport>
        <CashReportCurrency currency="BASE_SUMMARY" endingCash="120"/>
        <CashReportCurrency currency="USD" endingCash="100"/>
        <CashReportCurrency currency="EUR" endingCash="17"/>
      </CashReport>
      <ChangeInNAV startingValue="1000" endingValue="1015" mtm="10"
        dividends="5" depositsWithdrawals="0" twr="1.5"/>
      <MTMPerformanceSummaryInBase>
        <MTMPerformanceSummaryUnderlying conid="12345" symbol="TEST" currency="EUR"
          prevClosePrice="9" closePrice="10.20" total="3.3"/>
      </MTMPerformanceSummaryInBase>
      <CashTransactions>
        <CashTransaction transactionID="42" dateTime="20261104;120000" currency="EUR"
          amount="5" type="Dividends" description="TEST dividend" symbol="TEST"/>
      </CashTransactions>
    </FlexStatement>'''


def report(*statements):
    return (
        "<FlexQueryResponse><FlexStatements>"
        + "".join(statements)
        + ("</FlexStatements></FlexQueryResponse>")
    )


def test_realistic_report_preserves_decimal_currency_and_report_period():
    parsed = parse_report(report(statement()))
    assert parsed.as_of == date(2026, 11, 4)
    account = parsed.accounts[0]
    assert account["positions"][0]["quantity"] == "2.5"
    assert account["positions"][0]["mark_price"] == "10.20"
    assert account["positions"][0]["currency"] == "EUR"
    assert account["cash_balances"] == [
        {"currency": "USD", "ending_cash": "100"},
        {"currency": "EUR", "ending_cash": "17"},
    ]
    assert account["performance"][0]["currency"] == "USD"
    assert account["performance"][0]["previous_close"] == "9"
    assert account["performance"][0]["price_currency"] == "EUR"
    assert account["nav"]["ending_value"] == "1015"
    assert account["cash_transactions"][0]["date"] == "2026-11-04"
    assert PortfolioReport.from_dict(parsed.to_dict()) == parsed


def test_previous_close_price_alias_supported():
    xml = report(statement()).replace("prevClosePrice", "previousClosePrice")
    assert parse_report(xml).accounts[0]["performance"][0]["previous_close"] == "9"


def test_optional_sections_absent_are_unknown_not_zero():
    parsed = parse_report(
        report("""<FlexStatement accountId="U123" fromDate="2026-11-04"
      toDate="2026-11-04"><AccountInformation currency="SGD"/><OpenPositions/>
      </FlexStatement>""")
    )
    assert parsed.accounts[0]["positions"] == []
    assert parsed.accounts[0]["nav"]["ending_value"] is None
    assert len(parsed.warnings) == 4


def test_missing_required_section_and_invalid_snapshot_are_rejected():
    for xml in (
        report(
            statement()
            .replace("<OpenPositions>", "<Missing>")
            .replace("</OpenPositions>", "</Missing>")
        ),
        report(statement().replace('quantity="2.5"', 'quantity="NaN"')),
        report(statement().replace('markPrice="10.20"', 'markPrice="Infinity"')),
        report(statement().replace('conid="12345"', 'conid=""', 1)),
        report(statement().replace('fromDate="20261104"', 'fromDate="04/11/2026"')),
        "account,symbol,quantity\nU123,TEST,5",
    ):
        with pytest.raises(FlexError):
            parse_report(xml)


def test_lots_do_not_duplicate_summary_positions():
    summary = '<OpenPosition conid="1" symbol="T" currency="USD" quantity="5"/>'
    lot = '<OpenPosition conid="1" symbol="T" currency="USD" quantity="5" levelOfDetail="LOT"/>'
    assert (
        len(parse_report(report(statement(positions=summary + lot))).accounts[0]["positions"]) == 1
    )
    with pytest.raises(FlexError, match="Summary"):
        parse_report(report(statement(positions=lot)))
    with pytest.raises(FlexError, match="duplicate"):
        parse_report(report(statement(positions=summary + summary)))


def test_account_filter_and_mixed_dates_preserve_oldest_freshness():
    xml = report(statement(), statement("U456", "20261103", positions=""))
    all_accounts = parse_report(xml)
    assert all_accounts.as_of == date(2026, 11, 3)
    assert len(all_accounts.accounts) == 2
    selected = parse_report(xml, ("U123",))
    assert [account["account_id"] for account in selected.accounts] == ["U123"]
    with pytest.raises(FlexError, match="configured account"):
        parse_report(xml, ("U999",))


def test_child_price_date_is_preserved_instead_of_becoming_statement_date():
    parsed = parse_report(report(statement(day="20261105")))
    assert parsed.as_of == date(2026, 11, 5)
    assert parsed.accounts[0]["positions"][0]["price_as_of"] == "2026-11-04"
    assert any("observation dates" in warning for warning in parsed.warnings)


def test_nested_nav_variant_and_namespace_supported():
    xml = (
        report(statement())
        .replace("<ChangeInNAV startingValue=", "<ChangeInNAV><ChangeInNAV startingValue=")
        .replace('twr="1.5"/>', 'twr="1.5"/></ChangeInNAV>')
    )
    xml = xml.replace("<FlexQueryResponse>", '<FlexQueryResponse xmlns="urn:ibkr">')
    assert parse_report(xml).accounts[0]["nav"]["twr"] == "1.5"


def test_entity_declarations_and_large_documents_rejected():
    with pytest.raises(FlexError, match="declarations"):
        parse_report('<!DOCTYPE x [<!ENTITY s "sensitive">]>' + report(statement()))
    with pytest.raises(FlexError, match="size"):
        parse_report(b"x" * (MAX_REPORT_BYTES + 1))


def test_client_retries_pending_and_ignores_broker_supplied_url(caplog):
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            logging.getLogger("httpx").warning("sensitive URL %s", request.url)
            if request.url.path.endswith("SendRequest"):
                body = """<FlexStatementResponse><Status>Success</Status>
                    <ReferenceCode>123456</ReferenceCode><url>https://evil.example/</url>
                    </FlexStatementResponse>"""
            elif len(requests) == 2:
                body = """<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode>
                    <ErrorMessage>secret-token pending</ErrorMessage></FlexStatementResponse>"""
            else:
                body = report(statement())
            return httpx.Response(200, text=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client = IBKRFlexClient("secret-token", "123", client=transport)
            client._pace = AsyncMock()
            parsed = await client.fetch_report()
            assert parsed.accounts[0]["account_id"] == "U123"
            assert len(requests) == 3
            assert all(str(request.url).startswith(FLEX_BASE_URL) for request in requests)
            assert requests[1].url.params["q"] == "123456"
            await client.aclose()
            assert not transport.is_closed  # An injected client's owner manages its lifetime.

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    logging.getLogger("httpx").warning("normal unrelated traffic")
    assert "secret-token" not in caplog.text
    assert "FlexWebService" not in caplog.text
    assert "normal unrelated traffic" in caplog.text


@pytest.mark.parametrize("error_code", ["1012", "1015", "1013"])
def test_client_auth_errors_do_not_retry_or_expose_response(error_code):
    async def run():
        handler = AsyncMock(
            return_value=httpx.Response(
                200,
                text=f"""
          <FlexStatementResponse><Status>Fail</Status><ErrorCode>{error_code}</ErrorCode>
          <ErrorMessage>secret-token secret-account</ErrorMessage></FlexStatementResponse>""",
            )
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client = IBKRFlexClient("secret-token", "123", client=transport)
            client._pace = AsyncMock()
            with pytest.raises(FlexError) as caught:
                await client.fetch_report()
            assert caught.value.code == error_code
            assert "secret" not in str(caught.value)
            assert handler.call_count == 1

    asyncio.run(run())


def test_client_network_error_and_redirect_are_sanitized():
    async def run():
        def error(request):
            raise httpx.ConnectError(f"Cannot connect to {request.url}", request=request)

        for handler in (
            error,
            lambda request: httpx.Response(302, headers={"Location": "https://evil.example"}),
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
                client = IBKRFlexClient("secret-token", "123", client=transport, max_attempts=1)
                with pytest.raises(FlexError) as caught:
                    await client.fetch_report()
                assert "secret-token" not in str(caught.value)
                assert "https://" not in str(caught.value)

    asyncio.run(run())


def test_request_pacing_survives_multiple_fetches(monkeypatch):
    async def run():
        client = IBKRFlexClient("secret-token", "123")
        sleep = AsyncMock()
        monkeypatch.setattr("personal_agent.integrations.ibkr.client.time.monotonic", lambda: 100)
        monkeypatch.setattr("personal_agent.integrations.ibkr.client.asyncio.sleep", sleep)
        await client._pace()
        await client._pace()
        assert sleep.call_args.args[0] == 6.1
        await client.aclose()

    asyncio.run(run())
