"""Read-only IBKR portfolio capability declaration."""

from personal_agent.capabilities.portfolio.tools import READ_TOOLS
from personal_agent.core.registry import CapabilityDefinition

CAPABILITY = CapabilityDefinition(
    id="portfolio",
    description="Read IBKR Flex holdings, closing valuations, performance and dividend activity.",
    workflow_ids=("portfolio.daily_briefing", "portfolio.sync"),
    tool_ids=tuple(READ_TOOLS),
)
