"""Validate a complete Flex XML report before replacing a portfolio snapshot."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree as ET

from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.integrations.ibkr import report_spec as spec
from personal_agent.integrations.ibkr.errors import FlexError

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20_000


def safe_xml(document: str | bytes) -> ET.Element:
    try:
        raw = document.encode("utf-8") if isinstance(document, str) else document
        if len(raw) > MAX_REPORT_BYTES:
            raise FlexError("report_too_large", "IBKR report exceeds the supported size.")
        # Reject declarations before parsing; ElementTree must never expand entities.
        if re.search(rb"<!\s*(DOCTYPE|ENTITY)\b", raw, re.IGNORECASE) or b"\x00" in raw:
            raise FlexError("invalid_xml", "IBKR report contains unsupported XML declarations.")
        root = ET.fromstring(raw)
        for index, element in enumerate(root.iter()):
            if index > MAX_ROWS * 10:
                raise FlexError("report_too_large", "IBKR report has too many elements.")
            # Namespace handling is independent of how IBKR serializes its XML.
            element.tag = element.tag.rsplit("}", 1)[-1]
        return root
    except (ET.ParseError, UnicodeError, TypeError):
        raise FlexError("invalid_xml", "IBKR did not return a supported XML report.") from None


def _date(value: str | None) -> date:
    if not value:
        raise FlexError("invalid_report", "An IBKR report date is missing.")
    value = re.split(r"[;T ]", value, maxsplit=1)[0]
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise FlexError("invalid_report", "Use yyyyMMdd or yyyy-MM-dd dates in the Flex query.")


def _text(value: str | None, *, required: bool = False) -> str:
    value = (value or "").strip()
    if len(value) > 2000 or (required and not value):
        raise FlexError("invalid_report", "An IBKR report field is missing or too long.")
    return value


def _number(value: str | None, *, required: bool = False) -> str | None:
    value = _text(value)
    if not value:
        if required:
            raise FlexError("invalid_report", "A required IBKR numeric field is missing.")
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or len(value) > 100 or abs(number.adjusted()) > 30:
            raise InvalidOperation
    except InvalidOperation:
        raise FlexError(
            "invalid_report", "IBKR report contains an invalid numeric value."
        ) from None
    return str(number)


def _attribute(element: ET.Element, aliases: tuple[str, ...]) -> str | None:
    return next((element.get(name) for name in aliases if element.get(name)), None)


def _mapped(
    element: ET.Element, fields: dict[str, tuple[str, ...]], numbers: frozenset[str]
) -> dict[str, Any]:
    return {
        name: _number(_attribute(element, aliases))
        if name in numbers
        else _text(_attribute(element, aliases))
        for name, aliases in fields.items()
    }


def _section(statement: ET.Element, *names: str) -> ET.Element | None:
    return next((element for element in statement if element.tag in names), None)


def _rows(section: ET.Element | None, *tags: str) -> list[ET.Element]:
    if section is None:
        return []
    rows = [element for element in section.iter() if element.tag in tags and element.attrib]
    if len(rows) > MAX_ROWS:
        raise FlexError("report_too_large", "IBKR report has too many rows.")
    return rows


def _positions(section: ET.Element, report_date: date) -> list[dict[str, Any]]:
    rows = _rows(section, "OpenPosition")
    result = []
    seen: set[tuple[str, str]] = set()
    lot_rows = False
    for row in rows:
        detail = row.get("levelOfDetail", "SUMMARY").upper()
        if detail in {"LOT", "LOTS"}:
            lot_rows = True
            continue
        if detail != "SUMMARY":
            raise FlexError("invalid_report", "Use Summary detail for IBKR Open Positions.")
        position = _mapped(row, spec.POSITION_FIELDS, spec.POSITION_NUMBERS)
        for name in ("conid", "symbol", "currency"):
            position[name] = _text(position[name], required=True)
        if not position["conid"].isdigit():
            raise FlexError(
                "invalid_report", "IBKR Open Positions must include valid contract IDs."
            )
        position["quantity"] = _number(
            _attribute(row, spec.POSITION_FIELDS["quantity"]), required=True
        )
        position["price_as_of"] = (
            _date(row.get("reportDate")).isoformat()
            if row.get("reportDate")
            else report_date.isoformat()
        )
        key = (position["conid"], position["currency"])
        if key in seen:
            raise FlexError("invalid_report", "IBKR report contains duplicate summary positions.")
        seen.add(key)
        result.append(position)
    if lot_rows and not result:
        raise FlexError(
            "invalid_report", "Select Summary detail instead of Lots for Open Positions."
        )
    return result


def _account(statement: ET.Element, warnings: list[str]) -> dict[str, Any]:
    account_id = _text(statement.get("accountId"), required=True)
    period_start = _date(statement.get("fromDate"))
    as_of = _date(statement.get("toDate"))
    if period_start > as_of:
        raise FlexError("invalid_report", "IBKR report has an invalid date range.")
    for name in spec.REQUIRED_SECTIONS:
        if _section(statement, name) is None:
            raise FlexError("missing_section", f"Add the {name} section to your Flex query.")
    info = _section(statement, "AccountInformation")
    assert info is not None
    base_currency = _text(info.get("currency"), required=True)
    position_section = _section(statement, "OpenPositions")
    assert position_section is not None
    positions = _positions(position_section, as_of)
    if any(position["price_as_of"] != as_of.isoformat() for position in positions):
        warning = "Some position prices have different observation dates from the statement date."
        if warning not in warnings:
            warnings.append(warning)
    sections = {
        name: _section(statement, name)
        for name in ("CashReport", "ChangeInNAV", "CashTransactions")
    }
    performance_section = _section(
        statement, "MTMPerformanceSummaryInBase", "MarkToMarketPerformanceSummaryInBase"
    )
    for name, section in (*sections.items(), ("MTMPerformanceSummaryInBase", performance_section)):
        if section is None:
            warning = f"Optional {name} section is absent; related figures are unavailable."
            if warning not in warnings:
                warnings.append(warning)
    cash_balances = []
    for row in _rows(sections["CashReport"], "CashReportCurrency"):
        balance = _mapped(row, spec.CASH_BALANCE_FIELDS, frozenset({"ending_cash"}))
        if balance["currency"] == "BASE_SUMMARY":
            continue  # An aggregate is not another native-currency cash balance.
        balance["currency"] = _text(balance["currency"], required=True)
        cash_balances.append(balance)
    nav = dict.fromkeys(spec.NAV_FIELDS)
    nav_rows = _rows(sections["ChangeInNAV"], "ChangeInNAV")
    usable_nav = [
        row
        for row in nav_rows
        if any(_attribute(row, aliases) for aliases in spec.NAV_FIELDS.values())
        and row.get("currency", base_currency) in {base_currency, "BASE_SUMMARY"}
    ]
    if len(usable_nav) > 1:
        raise FlexError("invalid_report", "IBKR report contains ambiguous account NAV rows.")
    if usable_nav:
        nav = _mapped(usable_nav[0], spec.NAV_FIELDS, frozenset(spec.NAV_FIELDS))
    performance = []
    native_currencies = {position["conid"]: position["currency"] for position in positions}
    for row in _rows(
        performance_section,
        "MTMPerformanceSummaryUnderlying",
        "MTMPerformanceSummary",
        "MTMPerformanceSummaryInBase",
    ):
        if not row.get("symbol"):
            continue  # Asset-category totals must not duplicate individual instruments.
        item = _mapped(row, spec.PERFORMANCE_FIELDS, spec.PERFORMANCE_NUMBERS)
        item["price_currency"] = native_currencies.get(item["conid"], item["currency"])
        item["currency"] = base_currency
        performance.append(item)
    transactions = []
    for row in _rows(sections["CashTransactions"], "CashTransaction"):
        item = _mapped(row, spec.CASH_TRANSACTION_FIELDS, frozenset({"amount"}))
        item["date"] = _date(item["date"]).isoformat()
        item["currency"] = _text(item["currency"], required=True)
        item["amount"] = _number(row.get("amount"), required=True)
        transactions.append(item)
    return {
        "account_id": account_id,
        "base_currency": base_currency,
        "as_of": as_of.isoformat(),
        "period_start": period_start.isoformat(),
        "positions": positions,
        "cash_balances": cash_balances,
        "nav": nav,
        "performance": performance,
        "cash_transactions": transactions,
    }


def parse_report(xml: str | bytes, account_ids: tuple[str, ...] = ()) -> PortfolioReport:
    """Return only configured accounts, or all accounts in the saved query by default."""
    root = safe_xml(xml)
    if root.tag != "FlexQueryResponse":
        raise FlexError("invalid_report", "Expected an Activity Flex Query XML report.")
    statements = list(root.iter("FlexStatement"))
    if not statements or len(statements) > 50:
        raise FlexError("invalid_report", "IBKR report must contain between one and 50 accounts.")
    warnings: list[str] = []
    accounts = []
    seen = set()
    for statement in statements:
        if account_ids and statement.get("accountId") not in account_ids:
            continue
        account = _account(statement, warnings)
        if account["account_id"] in seen:
            raise FlexError("invalid_report", "IBKR report contains duplicate account statements.")
        seen.add(account["account_id"])
        accounts.append(account)
    if not accounts:
        raise FlexError("account_filter", "The Flex query did not contain any configured account.")
    dates = {account["as_of"] for account in accounts}
    if len(dates) > 1:
        warnings.append("Accounts have different report dates; inspect each account's as_of date.")
    if account_ids and seen != set(account_ids):
        warnings.append("Some configured accounts are absent from this report.")
    return PortfolioReport(
        as_of=min(date.fromisoformat(account["as_of"]) for account in accounts),
        period_start=min(date.fromisoformat(account["period_start"]) for account in accounts),
        accounts=tuple(accounts),
        warnings=tuple(warnings),
    )
