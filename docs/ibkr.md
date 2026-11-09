# IBKR portfolio connection

The portfolio capability reads an IBKR **Activity Flex Query**. IBKR stores the query
template; the Python API running on Railway periodically requests and downloads its XML
report, saves a dated snapshot, and exposes portfolio read tools to the agent. The query
follows the selected account's actual holdings as they change. It does not grant order
placement or turn the agent into a live market-data terminal.

## Create the query in IBKR

1. Sign in to IBKR Client Portal. Open **Performance & Reports → Flex Queries**, or
   **Reporting → Flex Queries**, depending on the portal navigation.
2. Create an **Activity Flex Query**, for example `Personal Agent Daily Portfolio`.
3. Select the account or accounts you want the agent to read.
4. Leave the **symbol filters empty**. Filtering the query to today's stocks would prevent
   a newly purchased stock from appearing automatically. The agent's focus-symbol setting
   only controls briefing emphasis; it should not restrict the IBKR report.
5. Select **XML**, **Last Business Day**, and dates in `yyyyMMdd` format. For Date/Time
   fields use `yyyyMMdd;HHmmss`. Use **Summary** level for Open Positions, not tax lots.
6. Include the sections and fields below. Review the query, save it, then use its
   information button to obtain the **Query ID**.

These are IBKR's documented [Activity Flex Query setup steps](https://www.ibkrguides.com/clientportal/performanceandstatements/activityflex.htm)
and [query ID instructions](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query).

| Section | Fields to include | Used for |
| --- | --- | --- |
| Account Information | Account ID, account base currency | Identifying each account and its valuation currency |
| Open Positions | Account ID, Conid, Symbol, Description, Currency, Quantity, Mark Price, Position Value, Cost Basis Money, FIFO Unrealized PNL, Level of Detail | Holdings, closing prices, position values, and unrealized gains |
| Cash Report | Account ID, Currency, Ending Cash | Cash balances per currency |
| Change in NAV | Account ID, Starting Value, Ending Value, Mark-to-Market, Realized, Change in Unrealized, Dividends, Deposits/Withdrawals, TWR | Broker-reported account value and performance, as available in the selected mode |
| Mark-to-Market Performance Summary in Base | Account ID, Conid, Symbol, Currency if offered, Previous Close Price, Close Price, Total | Price changes and reported P&L by instrument |
| Cash Transactions | Account ID, Transaction ID if offered, Date/Time, Type, Currency, Amount, Description, Symbol | Dividend payments and related cash activity |

Account Information and Open Positions are required. The remaining sections enrich the
summary; missing optional sections are identified as unavailable, rather than treated as
zero. Some fields depend on the section mode or account type. Do not change modes merely
to expose both alternatives: for example, Change in NAV offers Mark-to-Market or
Realized & Unrealized modes. Include the available fields for your chosen mode.

The parser's minimum required XML attributes are `accountId`, `fromDate`, and `toDate`
on each `FlexStatement`, `currency` on `AccountInformation`, and `conid`, `symbol`,
`currency`, and `position` (or `quantity`) on each summary `OpenPosition`. An empty
`OpenPositions` section is valid when the account has no open positions. The other
position fields in the table are recommended so valuation and briefing figures can be
shown. Unknown extra XML fields are ignored unless added to the mapping.

IBKR defines the Open Positions **Mark Price** as the closing price on the report date.
It is not a current intraday quote. See the official field references for
[Open Positions](https://ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm),
[Cash Report](https://ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm),
[Change in NAV](https://ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm),
[Mark-to-Market](https://www.ibkrguides.com/reportingreference/reportguide/mark-to-market%20performance%20summary%20in%20base%20fq.htm),
and [Cash Transactions](https://ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm).

## Enable retrieval

In Client Portal's **Flex Web Service Configuration**, enable the service and generate a
token. Choose its expiry and note when it will need renewal. IBKR permits expiries from
six hours to one year and optionally restricts allowed source IP addresses. If using an
IP restriction, it must match Railway's actual outbound address; the public service
domain is not an outbound address. See [IBKR's token configuration instructions](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/enable-and-create-access-token).

Set these variables on Railway's **agent-api** service, or in the root `.env` for local
development. Never send the token to the agent in a chat message or commit it to source.

| Variable | Value |
| --- | --- |
| `PERSONAL_AGENT_IBKR_FLEX_TOKEN` | Your generated Flex token |
| `PERSONAL_AGENT_IBKR_FLEX_QUERY_ID` | The saved query's numeric ID |
| `PERSONAL_AGENT_IBKR_ACCOUNT_IDS` | Optional comma-separated account allowlist; blank includes accounts selected in the report |
| `PERSONAL_AGENT_PORTFOLIO_SYNC_INTERVAL_SECONDS` | Defaults to `3600`; minimum `300` |
| `PERSONAL_AGENT_PORTFOLIO_MORNING_ENABLED` | `true` by default; controls the API's portfolio morning claim |
| `PERSONAL_AGENT_PORTFOLIO_FOCUS_SYMBOLS` | Optional comma-separated symbols to emphasize in the briefing |
| `PERSONAL_AGENT_TIMEZONE` | Morning schedule timezone; currently `America/New_York` |

Both the token and query ID are needed to fetch reports. The configured sender and API
bearer token continue to control access through the existing agent and Spectrum bridge.
The bridge also honors its own `PERSONAL_AGENT_PORTFOLIO_MORNING_ENABLED` switch; leaving
it unset enables portfolio polling, and setting it to `false` disables that polling.
This is independent of the existing motivational-message switch.

After restarting the API with credentials, it syncs in the background and repeats at the
configured interval. You can request a sync or inspect state through authenticated API
endpoints:

| Endpoint | Behavior |
| --- | --- |
| `GET /portfolio/status` | Connection and most recent report status |
| `POST /portfolio/sync` | Request a refresh; response identifies success or a safe error |
| `POST /portfolio/morning/claim` | Claim a due portfolio briefing, or return 204 when none is due |

The API uses the same bearer token as `/chat`. Portfolio tools read the saved data, so a
chat answer does not need to hold open a report-generation request. Useful prompts include
“Show my IBKR holdings,” “What closing prices do you have?”, and “Summarize my dividends.”

## Change the report or briefing yourself

There are two separate editing locations:

- **IBKR's saved query** controls which fields IBKR sends. Edit it in Client Portal.
- [`report_spec.py`](../src/personal_agent/integrations/ibkr/report_spec.py) maps IBKR's
  XML attributes to the portfolio records and declares the expected report sections.
- [`briefing.py`](../src/personal_agent/capabilities/portfolio/briefing.py) controls the
  morning message's wording, displayed figures, and position selection.

In `report_spec.py`, `POSITION_FIELDS`, `CASH_BALANCE_FIELDS`, `NAV_FIELDS`,
`PERFORMANCE_FIELDS`, and `CASH_TRANSACTION_FIELDS` map normalized names to IBKR XML
attribute names. `REQUIRED_SECTIONS` and `OPTIONAL_SECTIONS` describe the sections
expected in the report. Keep the existing required normalized keys when extending these
maps. A new numeric position or performance field also belongs in its corresponding
`POSITION_NUMBERS` or `PERFORMANCE_NUMBERS` set.

In `briefing.py`, edit `format_briefing()` to change the text and displayed metrics.
`TOP_HOLDINGS`, `TOP_MOVERS`, `MAX_BRIEFING_ACCOUNTS`, and `MAX_BRIEFING_CHARS` control
message size. `FOCUSED_SYMBOLS` provides a code default; a nonempty
`PERSONAL_AGENT_PORTFOLIO_FOCUS_SYMBOLS` setting overrides that default.

Changing Python code does not edit the saved query at IBKR. Extra report fields can be
included without being displayed. To use a new field, include it in the IBKR query,
extend its mapping and corresponding portfolio logic, and then redeploy. Keep the
required fields when changing the template. Fixture tests in `tests/test_ibkr_adapter.py`
provide examples of the supported XML structure without containing real account data.

For display-only changes, start in `briefing.py`; no Flex query change is needed. Focus
symbols can be changed through the environment setting without changing either file.

## Dates, failures, and morning delivery

The morning claim uses the 8:00–8:15 a.m. weekday window in `PERSONAL_AGENT_TIMEZONE`.
Portfolio briefings skip Saturday and Sunday; motivational messages run every day,
including weekends, during the same 8:00–8:15 a.m. window.
Background Flex sync continues every day, including weekends. Monday delivery is allowed
even when the latest available report is still Friday's report.
It uses the latest stored report and states its date. It waits for a report before
claiming a message. A claim is persisted before returning text to the bridge; an
interrupted or ambiguous delivery is not automatically retried, matching the existing
morning-message delivery policy.

Weekend and holiday reports can refer to an earlier market session. The app preserves
the actual report period and identifies older data instead of presenting it as today's
live prices. A failed refresh keeps the last valid snapshot available. Removing a
position by selling it is reflected by replacement of the full positions snapshot on
the next valid report; a missing required section is an invalid report, not an empty
portfolio.

Flex reports cover selected account activity and holdings. They do not supply arbitrary
unheld-stock quotes, live streaming prices, company fundamentals, or explanations of
why a stock moved. Values in different currencies are kept separate; report-period
dividends and P&L are not automatically lifetime or year-to-date totals. A different
report period changes what those reported totals mean.

The Flex request workflow is [generate a report](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/generate-the-report),
then [retrieve it by reference code](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/retrieve-the-report).
The adapter handles bounded generation retries and request pacing. Expired tokens,
IP restrictions, invalid queries, and reports still being generated have distinct
[IBKR error codes](https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes).
