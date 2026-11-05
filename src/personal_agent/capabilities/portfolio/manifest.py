"""Portfolio capability declaration; execution is not implemented yet."""

from personal_agent.core.registry import CapabilityDefinition

CAPABILITY = CapabilityDefinition(
    id="portfolio",
    description="Collate holdings and market data into a daily briefing.",
    workflow_ids=("portfolio.daily_briefing",),
    tool_ids=("portfolio.get_holdings", "portfolio.get_prices"),
)
