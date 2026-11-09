# Stock research skill

## Files to edit

- [`metadata.json`](../skill_templates/stock-research/metadata.json): skill ID, display
  name, catalog description, and required application tool IDs.
- [`instructions.md`](../skill_templates/stock-research/instructions.md): research
  procedure, optional portfolio context, and default report outline.

Start by editing the report outline and source preferences. The description helps the
agent choose this skill from its catalog, so keep it aligned with the instructions.
The instructions must stay within the saved-skill limit of 12,000 characters.

## Package and use

From the repository root:

```sh
uv run personal-agent skills import skill_templates/stock-research --activate
```

Commit the templates and generated `config/skills.json`, then push/deploy. On startup
the app saves and activates the package in its own database. For local testing, restart
the local CLI or API after packaging. Then ask: "Use Stock research on AMD."
See [file-based skill development](skills.md) for updates, inactive revisions, and
restart behavior. The skill is interactive; packaging does not schedule reports.

## Research behavior and tools

The skill guides the conversational model to identify the company and exchange, research
current public sources, and explain its business, latest results, recent developments,
and risks with dated citations. It keeps facts and interpretation separate and discloses
missing information. It only adds personal portfolio context when requested.

This is natural-language guidance, not a fixed Python call sequence. The model chooses
which tools to invoke based on the question and available permissions:

| Tool | Purpose | Implementation |
| --- | --- | --- |
| `runtime.current_time` | Establish the research date | `current_time` handler in `bootstrap.py` |
| Hosted `web_search` | Retrieve public sources and citations | `OpenAIModel.respond` in `integrations/llm/provider.py` |
| `portfolio.get_status` | Check saved data freshness | `PortfolioService.get_status` |
| `portfolio.get_holdings` | Find a requested position | `PortfolioService.get_holdings` |
| `portfolio.get_prices` | Read report-date closing prices | `PortfolioService.get_prices` |
| `portfolio.get_performance` | Read report-period P&L | `PortfolioService.get_performance` |

Portfolio methods live in `capabilities/portfolio/workflows.py`; tool handlers are bound
by `register_portfolio_tools` in `capabilities/portfolio/tools.py`. The portfolio tools
are optional and are not hard requirements in this template. They read saved reports;
the research skill does not force a broker refresh or place trades.

Exact skill names/IDs preload the active instructions through `discover_skills`.
Otherwise the agent can select it from the catalog and call `skills.load`, which uses
`SkillService.load`. Loading guidance does not execute research by itself.

Hosted `web_search` is supplied by the model adapter, not registered as an application
function tool. Do not add `web_search` or an invented `web.search` ID to
`required_tool_ids`: the validator would reject it. Search depends on the runtime's
web-search configuration and per-turn budget; instructions require disclosure when
current research cannot be verified. See [web search](web-search.md) and
[IBKR reporting](ibkr.md) for the available data and its limits.
