"""Portfolio-owned types, independent of market-data vendors."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


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


@dataclass(frozen=True)
class PortfolioReport:
    """A dated, complete snapshot of the accounts included in a broker report.

    Financial values inside account records are decimal strings, not binary floats.
    Each account retains its own report date and base currency.
    """

    as_of: date
    period_start: date
    accounts: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [encode(item) for item in value]
            return value

        return {
            "as_of": self.as_of.isoformat(),
            "period_start": self.period_start.isoformat(),
            "accounts": encode(self.accounts),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortfolioReport":
        return cls(
            as_of=date.fromisoformat(data["as_of"]),
            period_start=date.fromisoformat(data["period_start"]),
            accounts=tuple(data["accounts"]),
            warnings=tuple(data.get("warnings", ())),
        )
