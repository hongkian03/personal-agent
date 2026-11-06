"""Editable, deterministic portfolio briefing format.

Change the constants and ``format_briefing`` here to control the morning message.
Values come from IBKR reports; no model performs financial calculations or invents news.
IBKR's query sections/field mappings are configured separately in ibkr/report_spec.py.
"""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.capabilities.portfolio.workflows import decimal_value, previous_weekday

TOP_HOLDINGS = 3
TOP_MOVERS = 3
MAX_BRIEFING_ACCOUNTS = 3
MAX_BRIEFING_CHARS = 3500
FOCUSED_SYMBOLS: tuple[str, ...] = ()  # Example: ("AAPL", "MSFT")


def money(value, currency: str, *, signed: bool = False) -> str:
    number = decimal_value(value)
    if number is None:
        return "unavailable"
    spec = "+,.2f" if signed else ",.2f"
    return f"{currency} {number:{spec}}"


def price(value, currency: str) -> str:
    """Retain useful precision for small prices instead of rounding them to zero."""
    number = decimal_value(value)
    return "unavailable" if number is None else f"{currency} {number:.8g}"


def label(row: dict) -> str:
    return str(row.get("symbol") or row.get("conid") or "Unnamed holding")[:60]


def format_briefing(
    report: PortfolioReport,
    *,
    today,
    focused_symbols: tuple[str, ...] = (),
) -> str:
    """Render reported values without merging accounts or mixing currencies."""
    focus = {symbol.upper() for symbol in (focused_symbols or FOCUSED_SYMBOLS)}
    lines = [f"IBKR portfolio · report through {report.as_of.isoformat()}"]
    if report.period_start != report.as_of:
        lines.append(
            f"Performance period: {report.period_start.isoformat()}–{report.as_of.isoformat()}."
        )
    if report.as_of < previous_weekday(today):
        lines.append(
            "Latest available report is older than the previous weekday; values may be stale."
        )
    for account in report.accounts[:MAX_BRIEFING_ACCOUNTS]:
        currency = str(account.get("base_currency") or "?")[:10]
        account_id = str(account.get("account_id") or "")
        nav = account.get("nav", {})
        lines.append(
            f"Account …{account_id[-4:]} ({currency}) · "
            f"as of {account.get('as_of') or report.as_of.isoformat()}"
        )
        lines.append(f"Portfolio value: {money(nav.get('ending_value'), currency)}.")
        if nav.get("mtm_pnl") is not None:
            lines.append(
                f"Reported investment P&L: {money(nav['mtm_pnl'], currency, signed=True)}."
            )
        if nav.get("twr") is not None:
            twr = decimal_value(nav["twr"])
            if twr is not None:
                lines.append(f"Reported time-weighted return: {twr:,.2f}%.")
        if nav.get("dividends") is not None:
            lines.append(
                f"Reported dividends/accrual changes: {money(nav['dividends'], currency)}."
            )
        positions = account.get("positions", [])
        if not positions:
            lines.append("No open positions in this report.")
        else:
            # Keep each amount in its own reported currency; ranking absolute values
            # across currencies would give misleading comparisons.
            currencies = sorted({str(row.get("currency") or "?") for row in positions})
            for position_currency in currencies[:5]:
                priced = [
                    row
                    for row in positions
                    if str(row.get("currency") or "?") == position_currency
                    and decimal_value(row.get("market_value")) is not None
                ]
                priced.sort(key=lambda row: abs(decimal_value(row["market_value"])), reverse=True)
                if priced:
                    selected = "; ".join(
                        f"{label(row)} {money(row.get('market_value'), position_currency)}"
                        for row in priced[:TOP_HOLDINGS]
                    )
                    lines.append(f"Largest holdings in {position_currency}: {selected}.")
            watched = [row for row in positions if str(row.get("symbol", "")).upper() in focus]
            for row in watched[:10]:
                lines.append(
                    f"Watching {label(row)}: "
                    f"close {price(row.get('mark_price'), row['currency'])}; "
                    f"quantity {row.get('quantity', 'unavailable')} "
                    f"(price date {row.get('price_as_of') or account.get('as_of')})."
                )
        performance = [
            row
            for row in account.get("performance", [])
            if decimal_value(row.get("mtm_pnl")) is not None
        ]
        # MTM summary amounts are normalized to account base currency by the adapter.
        performance.sort(key=lambda row: abs(decimal_value(row["mtm_pnl"])), reverse=True)
        for row in performance[:TOP_MOVERS]:
            lines.append(
                f"P&L contributor {label(row)}: "
                f"{money(row['mtm_pnl'], str(row.get('currency') or currency), signed=True)}."
            )
    if len(report.accounts) > MAX_BRIEFING_ACCOUNTS:
        lines.append(f"Showing {MAX_BRIEFING_ACCOUNTS} of {len(report.accounts)} accounts.")
    if report.warnings:
        lines.append(
            "Some report sections or fields are unavailable; ask for portfolio status for details."
        )
    lines.append("Prices are report-date closes, not live quotes.")
    text = "\n".join(lines)
    if len(text) > MAX_BRIEFING_CHARS:
        return (
            text[: MAX_BRIEFING_CHARS - 70].rsplit("\n", 1)[0]
            + "\nSummary shortened; ask for portfolio details."
        )
    return text


class PortfolioMorningMessage:
    def __init__(
        self,
        service,
        repository,
        owner_id: str,
        zone: ZoneInfo,
        *,
        focused_symbols: tuple[str, ...] = (),
    ) -> None:
        self.service = service
        self.repository = repository
        self.owner_id = owner_id
        self.zone = zone
        self.focused_symbols = focused_symbols
        self.claim_owner = "portfolio:" + hashlib.sha256(owner_id.encode()).hexdigest()

    async def claim_due(self) -> str | None:
        now = datetime.now(self.zone)
        if now.weekday() >= 5 or not (now.hour == 8 and now.minute < 15):
            return None
        day = now.date()
        if day in await self.repository.recent(self.claim_owner):
            return None
        report = await self.service.latest(owner_id=self.owner_id)
        if report is None:
            return None
        text = format_briefing(report, today=day, focused_symbols=self.focused_symbols)
        after = datetime.now(self.zone)
        if after.date() != day or after.hour != 8 or after.minute >= 15:
            return None
        return text if await self.repository.claim(self.claim_owner, day, text) else None
