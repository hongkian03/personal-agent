# Personal Agent

A modular AI agent assistant with a harness built on LangGraph and Python, a swappable OpenAI model adapter, and iMessage integration provided with Spectrum. Conversation checkpoints and saved skills persist independently of the model provider. Hosted on Railway. For my personal use and learning of the development and utility of AI agents.

This also serves as an open portfolio project (hi recruiters!) but if anyone for whatever reason feels compelled to fork it, I don't really mind.

## Tech stack

| Area | Technologies |
| --- | --- |
| Backend | Python 3.13+, FastAPI, Uvicorn |
| Agent harness | LangGraph, OpenAI Responses API, hosted web search, validated function calling |
| Messaging | TypeScript, Node.js, Spectrum / Photon for iMessage |
| Persistence | SQLAlchemy, SQLite or PostgreSQL, LangGraph checkpoints |
| Portfolio data | Interactive Brokers Flex Web Service, XML reports |
| Deployment | Docker, Docker Compose, Railway |
| Development | uv, pytest, Ruff, TypeScript compiler |

## Capabilities

- **Conversational assistant:** Chat through iMessage, the CLI, or an authenticated HTTP
  API, with persistent conversation history, conversation resets, and access to registered tools.
  iMessage shows a typing indicator while the agent prepares and sends a reply.
- **Shared persona:** A Markdown profile defines the agent's name, personality,
  communication preferences, and personal context for conversations. Tunable to anyone's preference.
- **Reusable skills:** Save, revise, activate, and archive reusable instructions. Relevant
  active skills are retrieved during conversations.
- **Web research:** Search the public web during conversations through OpenAI's hosted
  search tool, with readable source links and a configurable search budget per turn.
- **IBKR portfolio reporting:** Automatically sync holdings, cash balances, closing prices,
  reported performance, and dividend activity from a saved Activity Flex Query. Dated
  snapshots persist as the account's holdings change. I built it on IBKR just because that's what I use.
  IBKR variables and keys live in Railway. The example env isn't the most updated.
- **Morning portfolio briefings:** Weekday iMessage summaries of portfolio value,
  performance, major holdings, and P&L contributors, with report dates and freshness information.
- **Morning encouragement:** Short weekday motivational messages guided by the shared
  persona and recent messages for variety.
- **Controlled tool access:** Validated tool arguments, explicit permission scopes,
  owner-scoped storage, and messaging restricted to the configured sender.
