"""Bounded HTTPS client for IBKR's read-only Flex Web Service."""

import asyncio
import contextvars
import logging
import time
from xml.etree import ElementTree as ET

import httpx

from personal_agent.capabilities.portfolio.models import PortfolioReport
from personal_agent.integrations.ibkr.errors import FlexError
from personal_agent.integrations.ibkr.parser import MAX_REPORT_BYTES, parse_report, safe_xml

FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_active_request = contextvars.ContextVar("ibkr_flex_http_request", default=False)
_RETRYABLE = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}
_SAFE_ERRORS = {
    "1003": "IBKR statement is not yet available.",
    "1012": "The IBKR Flex token has expired; update it in the service environment.",
    "1013": "The IBKR Flex token's IP restriction rejected this server.",
    "1014": "The configured IBKR Flex query is invalid.",
    "1015": "The configured IBKR Flex token is invalid.",
    "1016": "The IBKR account is unavailable for this report.",
    "1025": "IBKR rejected the request after too many failed attempts.",
}
_KNOWN_ERRORS = _RETRYABLE | set(_SAFE_ERRORS) | {"1010", "1011", "1017", "1020"}


class _TransportLogFilter(logging.Filter):
    """Suppress credential-bearing transport details only in this request's context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _active_request.get()


def _install_log_filters() -> None:
    # Logger levels and handlers stay unchanged. Concurrent non-Flex traffic keeps
    # normal logging; only requests in this ContextVar's task are filtered.
    names = {
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.connection_pool",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    }
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name.startswith(("httpx.", "httpcore."))
    )
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, _TransportLogFilter) for item in logger.filters):
            logger.addFilter(_TransportLogFilter())


class IBKRFlexClient:
    def __init__(
        self,
        token: str,
        query_id: str,
        account_ids: tuple[str, ...] = (),
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120,
        max_attempts: int = 8,
    ) -> None:
        if not token.strip() or len(token) > 2048 or not query_id.isdigit():
            raise ValueError("Configure a nonempty IBKR Flex token and numeric query ID.")
        if not 1 <= max_attempts <= 20 or not 0 < timeout_seconds <= 600:
            raise ValueError("Invalid IBKR Flex request bounds.")
        self._token = token.strip()
        self._query_id = query_id
        self._account_ids = tuple(account_ids)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._last_request: float | None = None
        self._request_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()
        _install_log_filters()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _pace(self) -> None:
        if self._last_request is not None:
            await asyncio.sleep(max(0, 6.1 - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()

    async def _request(self, endpoint: str, query: str) -> ET.Element:
        async with self._request_lock:
            await self._pace()
            active = _active_request.set(True)
            try:
                async with self._client.stream(
                    "GET",
                    f"{FLEX_BASE_URL}/{endpoint}",
                    params={"t": self._token, "q": query, "v": "3"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise FlexError(
                            "http_temporary", "IBKR reporting is temporarily unavailable."
                        )
                    if response.status_code != 200:
                        raise FlexError("http_error", "IBKR rejected the reporting request.")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > MAX_REPORT_BYTES:
                            raise FlexError(
                                "report_too_large", "IBKR report exceeds the supported size."
                            )
                        chunks.extend(chunk)
                return safe_xml(bytes(chunks))
            except httpx.HTTPError:
                raise FlexError("transport", "Could not connect to IBKR reporting.") from None
            finally:
                _active_request.reset(active)

    @staticmethod
    def _error(root: ET.Element) -> FlexError | None:
        if root.tag == "FlexQueryResponse":
            return None
        status = (root.findtext("Status") or "").strip()
        if status == "Success":
            return None
        code = (root.findtext("ErrorCode") or "").strip()
        if code not in _KNOWN_ERRORS:
            code = "invalid_response"
        message = _SAFE_ERRORS.get(code, "IBKR could not generate the requested report.")
        return FlexError(code, message)

    async def _attempt(self, endpoint: str, query: str) -> ET.Element:
        for attempt in range(self._max_attempts):
            try:
                root = await self._request(endpoint, query)
                error = self._error(root)
                if error:
                    raise error
                return root
            except FlexError as error:
                if error.code not in _RETRYABLE | {"http_temporary", "transport"}:
                    raise
                if attempt == self._max_attempts - 1:
                    raise FlexError(
                        "not_ready", "IBKR report is not ready; try again later."
                    ) from None
        raise AssertionError("unreachable")

    async def fetch_report(self) -> PortfolioReport:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._fetch_lock:
                    generated = await self._attempt("SendRequest", self._query_id)
                    reference = generated.findtext("ReferenceCode") or ""
                    if not reference.isdigit() or len(reference) > 100:
                        raise FlexError(
                            "invalid_response", "IBKR did not return a valid report reference."
                        )
                    # Never follow the URL in the broker response.
                    report = await self._attempt("GetStatement", reference)
                    return parse_report(ET.tostring(report), self._account_ids)
        except TimeoutError:
            raise FlexError(
                "timeout", "IBKR report retrieval timed out; try again later."
            ) from None


__all__ = ["FlexError", "IBKRFlexClient"]
