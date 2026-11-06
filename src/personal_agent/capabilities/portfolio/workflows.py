"""IBKR synchronization and bounded reads of the owner's persisted portfolio."""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from personal_agent.capabilities.portfolio.models import PortfolioReport

MAX_RESULTS = 100
MAX_ACCOUNTS = 20
SAFE_SYNC_ERRORS = {
    "missing_section": "The Flex query is missing a required section; check the setup guide.",
    "invalid_report": "The Flex report fields or date formats do not match the setup guide.",
    "invalid_xml": "IBKR did not return a supported XML report; select XML output in the query.",
    "account_filter": "The report did not contain any configured IBKR accounts.",
    "report_too_large": "The Flex report exceeds this app's supported size.",
    "invalid_response": "IBKR returned an unsupported report-service response.",
    "http_error": "IBKR rejected the report request.",
    "http_temporary": "IBKR reporting is temporarily unavailable.",
    "transport": "Could not connect to IBKR reporting.",
    "not_ready": "IBKR has not finished producing the report; the scheduler will retry.",
    "timeout": "IBKR report retrieval timed out; the scheduler will retry.",
    "1012": "The IBKR Flex token expired; update it in the service environment.",
    "1013": "The Flex token's IP restriction rejected this server.",
    "1014": "The configured Flex query is invalid.",
    "1015": "The configured Flex token is invalid.",
    "1003": "The requested statement is not available yet.",
    "sync_failed": "Portfolio refresh failed; previous saved data is retained.",
}
for _error_code in (
    "1001",
    "1004",
    "1005",
    "1006",
    "1007",
    "1008",
    "1009",
    "1010",
    "1011",
    "1016",
    "1017",
    "1018",
    "1019",
    "1020",
    "1021",
    "1025",
):
    SAFE_SYNC_ERRORS[_error_code] = (
        "IBKR could not produce the report; check the Flex configuration."
    )


def previous_weekday(day: date) -> date:
    """Expected report date, with weekends handled; exchange holidays remain explicit."""
    result = day - timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def decimal_value(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


class PortfolioService:
    def __init__(
        self,
        repository,
        client,
        owner_id: str,
        *,
        timezone: str = "America/New_York",
        sync_interval_seconds: float = 3600,
        retry_interval_seconds: float = 60,
        sync_timeout_seconds: float = 150,
    ) -> None:
        self.repository = repository
        self.client = client
        self.owner_id = owner_id
        self.zone = ZoneInfo(timezone)
        self.sync_interval_seconds = sync_interval_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self.sync_timeout_seconds = sync_timeout_seconds
        self._sync_lock = asyncio.Lock()
        self._next_attempt = 0.0
        self._last_attempt: str | None = None
        self._last_success: str | None = None
        self._last_error: str | None = None

    def _authorize(self, owner_id: str) -> None:
        if owner_id != self.owner_id:
            raise PermissionError("Portfolio access is not authorized")

    async def latest(self, *, owner_id: str) -> PortfolioReport | None:
        self._authorize(owner_id)
        return await self.repository.latest(owner_id)

    async def sync(self, force: bool = False) -> dict[str, Any]:
        if self.client is None:
            return await self.get_status(owner_id=self.owner_id)
        # Concurrent callers share the in-flight attempt, including force refreshes.
        requested_at = time.monotonic()
        was_locked = self._sync_lock.locked()
        async with self._sync_lock:
            if was_locked or (not force and requested_at < self._next_attempt):
                return await self.get_status(owner_id=self.owner_id)
            self._attempt_clock = time.monotonic()
            self._last_attempt = datetime.now(timezone.utc).isoformat()
            self._next_attempt = self._attempt_clock + self.retry_interval_seconds
            try:
                async with asyncio.timeout(self.sync_timeout_seconds):
                    report = await self.client.fetch_report()
                    await self.repository.save(self.owner_id, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never persist exception text: provider exceptions may include credentials.
                code = str(getattr(exc, "code", "sync_failed"))
                self._last_error = (
                    "timeout"
                    if isinstance(exc, TimeoutError)
                    else code
                    if code in SAFE_SYNC_ERRORS
                    else "sync_failed"
                )
            else:
                self._last_success = datetime.now(timezone.utc).isoformat()
                self._last_error = None
                self._next_attempt = time.monotonic() + self.sync_interval_seconds
            return await self.get_status(owner_id=self.owner_id)

    def _metadata(self, report: PortfolioReport | None) -> dict[str, Any]:
        expected = previous_weekday(datetime.now(self.zone).date())
        warnings = list(report.warnings[:20]) if report else []
        if report is not None and self.client is None:
            warnings.append("Automatic refresh is not configured; displaying saved report data.")
        return {
            "source": "IBKR Flex",
            "configured": self.client is not None,
            "status": (
                "no_data"
                if report is None and self.client is not None
                else "not_configured"
                if report is None
                else "stale"
                if report.as_of < expected
                else "ready"
            ),
            "as_of": report.as_of.isoformat() if report else None,
            "period_start": report.period_start.isoformat() if report else None,
            "expected_report_date": expected.isoformat(),
            "freshness_note": "Expected date excludes weekends; exchange holidays may differ.",
            "last_attempt": self._last_attempt,
            "last_success": self._last_success,
            "last_error": self._last_error,
            "last_error_message": SAFE_SYNC_ERRORS.get(self._last_error),
            "warnings": warnings,
        }

    async def get_status(self, *, owner_id: str) -> dict[str, Any]:
        report = await self.latest(owner_id=owner_id)
        return self._metadata(report)

    async def get_summary(self, *, owner_id: str) -> dict[str, Any]:
        report = await self.latest(owner_id=owner_id)
        accounts = report.accounts if report else ()
        return {
            **self._metadata(report),
            "accounts": [
                {
                    "account_id": a["account_id"],
                    "base_currency": a.get("base_currency"),
                    "as_of": a.get("as_of"),
                    "period_start": a.get("period_start"),
                    "position_count": len(a.get("positions", [])),
                    "nav": a.get("nav", {}),
                    "cash_balances": a.get("cash_balances", [])[:MAX_RESULTS],
                }
                for a in accounts[:MAX_ACCOUNTS]
            ],
            "account_count": len(accounts),
            "truncated": len(accounts) > MAX_ACCOUNTS,
            "note": "Account values remain separate in each account's base currency.",
        }

    async def _rows(
        self,
        section: str,
        *,
        owner_id: str,
        symbol: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 0 <= offset <= 100000 or not 1 <= limit <= MAX_RESULTS:
            raise ValueError("Use offset 0–100000 and limit 1–100")
        report = await self.latest(owner_id=owner_id)
        rows = []
        symbol_filter = symbol.upper() if symbol else None
        for account in report.accounts if report else ():
            for row in account.get(section, []):
                if symbol_filter and str(row.get("symbol", "")).upper() != symbol_filter:
                    continue
                rows.append(
                    {
                        **row,
                        "account_id": account["account_id"],
                        "as_of": account.get("as_of", report.as_of.isoformat()),
                        "period_start": account.get(
                            "period_start", report.period_start.isoformat()
                        ),
                    }
                )
        return {
            **self._metadata(report),
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(rows),
        }

    async def get_holdings(self, *, owner_id: str, **options) -> dict[str, Any]:
        return await self._rows("positions", owner_id=owner_id, **options)

    async def get_prices(self, *, owner_id: str, **options) -> dict[str, Any]:
        result = await self._rows("positions", owner_id=owner_id, **options)
        result["items"] = [
            {
                key: row.get(key)
                for key in (
                    "account_id",
                    "conid",
                    "symbol",
                    "currency",
                    "mark_price",
                    "price_as_of",
                    "as_of",
                )
            }
            for row in result["items"]
        ]
        result["price_type"] = "report_date_close"
        result["note"] = "Prices cover reported holdings only; this is not a live quote service."
        return result

    async def get_performance(self, *, owner_id: str, **options) -> dict[str, Any]:
        result = await self._rows("performance", owner_id=owner_id, **options)
        result["note"] = "Figures cover the report period; P&L is not a rate of return."
        return result

    async def get_dividends(self, *, owner_id: str, **options) -> dict[str, Any]:
        result = await self._rows("cash_transactions", owner_id=owner_id, **options)
        # Filtering must precede pagination so dividend pages cannot hide behind other cash rows.
        report = await self.latest(owner_id=owner_id)
        symbol = options.get("symbol")
        rows = []
        for account in report.accounts if report else ():
            for row in account.get("cash_transactions", []):
                kind = str(row.get("type", "")).casefold()
                if "dividend" not in kind and "withholding" not in kind:
                    continue
                if symbol and str(row.get("symbol", "")).upper() != symbol.upper():
                    continue
                rows.append(
                    {
                        **row,
                        "account_id": account["account_id"],
                        "as_of": account.get("as_of", report.as_of.isoformat()),
                        "period_start": account.get(
                            "period_start", report.period_start.isoformat()
                        ),
                    }
                )
        offset, limit = options.get("offset", 0), options.get("limit", 50)
        result.update(
            items=rows[offset : offset + limit],
            total=len(rows),
            has_more=offset + limit < len(rows),
        )
        result["note"] = (
            "Reported dividend cash activity and withholding; not forecast dividend yield."
        )
        return result
