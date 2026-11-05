"""Portfolio dependencies implemented by injected adapters."""

from collections.abc import Sequence
from typing import Protocol

from personal_agent.capabilities.portfolio.models import Holding, PriceObservation


class MarketDataProvider(Protocol):
    async def previous_session_closes(
        self, symbols: Sequence[str]
    ) -> Sequence[PriceObservation]: ...


class HoldingsRepository(Protocol):
    async def list_holdings(self, user_id: str) -> Sequence[Holding]: ...
