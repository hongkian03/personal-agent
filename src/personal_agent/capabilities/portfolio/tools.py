"""Cached, owner-scoped portfolio read tools exposed to the conversational agent."""

from personal_agent.agent.tools import ToolDefinition, ToolRegistry
from personal_agent.capabilities.portfolio.workflows import PortfolioService
from personal_agent.core.context import ExecutionContext

READ_TOOLS = {
    "portfolio.get_status": "Check IBKR configuration, last sync and report freshness.",
    "portfolio.get_summary": "Read IBKR account values, cash balances and position counts.",
    "portfolio.get_holdings": "Read current IBKR holdings from the latest saved Flex report.",
    "portfolio.get_prices": "Read report-date closing prices for IBKR holdings, not live quotes.",
    "portfolio.get_performance": "Read reported per-instrument P&L for the saved report period.",
    "portfolio.get_dividends": "Read dividend and withholding cash activity in the report period.",
}


def register_portfolio_tools(registry: ToolRegistry, service: PortfolioService) -> None:
    for tool_id, description in READ_TOOLS.items():
        method = getattr(service, tool_id.split(".", 1)[1])
        properties = (
            {}
            if tool_id in {"portfolio.get_status", "portfolio.get_summary"}
            else {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 80},
                "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        )

        async def handler(arguments: dict, context: ExecutionContext, read=method) -> dict:
            return await read(owner_id=context.owner_id, **arguments)

        registry.register(
            ToolDefinition(
                id=tool_id,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": [],
                    "additionalProperties": False,
                },
                required_scopes=frozenset({"portfolio:read"}),
            ),
            handler,
        )
