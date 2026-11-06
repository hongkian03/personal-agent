"""Editable mapping from your IBKR XML report into the portfolio capability.

The actual report template (sections, fields and period) is saved in IBKR Client
Portal under Reporting > Flex Queries. This file defines the fields this app reads.
Adding an optional mapping here makes it available in stored records; displaying it
in a briefing is controlled separately in capabilities/portfolio/briefing.py.

Each entry maps a normalized field name to accepted IBKR XML attribute names.
Use XML output, Last Business Day and Summary detail for Open Positions.
Never rename the required normalized keys without updating the consuming tools.
"""

REQUIRED_SECTIONS = ("AccountInformation", "OpenPositions")
OPTIONAL_SECTIONS = (
    "CashReport",
    "ChangeInNAV",
    "MTMPerformanceSummaryInBase",
    "CashTransactions",
)

POSITION_FIELDS = {
    "conid": ("conid",),
    "symbol": ("symbol",),
    "description": ("description",),
    "currency": ("currency",),
    "quantity": ("position", "quantity"),
    "mark_price": ("markPrice",),
    "market_value": ("positionValue",),
    "cost_basis": ("costBasisMoney",),
    "unrealized_pnl": ("fifoPnlUnrealized",),
}
POSITION_NUMBERS = frozenset(
    {"quantity", "mark_price", "market_value", "cost_basis", "unrealized_pnl"}
)

CASH_BALANCE_FIELDS = {"currency": ("currency",), "ending_cash": ("endingCash",)}

NAV_FIELDS = {
    "starting_value": ("startingValue",),
    "ending_value": ("endingValue",),
    "mtm_pnl": ("mtm", "markToMarket"),
    "realized_pnl": ("realized",),
    "change_in_unrealized": ("changeInUnrealized",),
    "dividends": ("dividends",),
    "deposits_withdrawals": ("depositsWithdrawals",),
    "twr": ("twr",),
}

PERFORMANCE_FIELDS = {
    "conid": ("conid",),
    "symbol": ("symbol",),
    "currency": ("currency",),
    "previous_close": ("prevClosePrice", "previousClosePrice"),
    "close_price": ("closePrice",),
    "mtm_pnl": ("total", "mtmPnl"),
    "realized_pnl": ("realizedPnl",),
    "unrealized_pnl": ("unrealizedPnl",),
}
PERFORMANCE_NUMBERS = frozenset(
    {"previous_close", "close_price", "mtm_pnl", "realized_pnl", "unrealized_pnl"}
)

CASH_TRANSACTION_FIELDS = {
    "transaction_id": ("transactionID", "transactionId"),
    "date": ("dateTime", "date", "reportDate"),
    "type": ("type",),
    "currency": ("currency",),
    "amount": ("amount",),
    "description": ("description",),
    "symbol": ("symbol",),
}
