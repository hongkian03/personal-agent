# Personal Agent

A modular Python assistant with a conversational LangGraph loop and a replaceable OpenAI
adapter. Use the local CLI or an authenticated HTTP endpoint to chat. Conversation checkpoints
and saved skills persist independently of the model provider.

## Tech stack

| Area | Technologies |
| --- | --- |
| Backend | Python 3.13+, FastAPI, Uvicorn |
| Agent harness | LangGraph, OpenAI Responses API, validated function calling |
| Messaging | TypeScript, Node.js, Spectrum / Photon for iMessage |
| Persistence | SQLAlchemy, SQLite or PostgreSQL, LangGraph checkpoints |
| Portfolio data | Interactive Brokers Flex Web Service, XML reports |
| Deployment | Docker, Docker Compose, Railway |
| Development | uv, pytest, Ruff, TypeScript compiler |

## Capabilities

- **Conversational assistant:** Chat through iMessage, the CLI, or an authenticated HTTP
  API, with persistent conversation history, conversation resets, and access to registered tools.
- **Shared persona:** A Markdown profile defines the agent's name, personality,
  communication preferences, and personal context for conversations and morning encouragement.
- **Reusable skills:** Save, revise, activate, and archive reusable instructions. Relevant
  active skills are retrieved during conversations.
- **IBKR portfolio reporting:** Automatically sync holdings, cash balances, closing prices,
  reported performance, and dividend activity from a saved Activity Flex Query. Dated
  snapshots persist as the account's holdings change.
- **Morning portfolio briefings:** Weekday iMessage summaries of portfolio value,
  performance, major holdings, and P&L contributors, with report dates and freshness information.
- **Morning encouragement:** Short weekday motivational messages guided by the shared
  persona and recent messages for variety.
- **Controlled tool access:** Validated tool arguments, explicit permission scopes,
  owner-scoped storage, and messaging restricted to the configured sender.
