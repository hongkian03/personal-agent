"""Portfolio-owned types, independent of market-data vendors."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: Decimal
    currency: str = "USD"


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    price: Decimal
    currency: str
    observed_at: datetime
    source: str
    session: str
