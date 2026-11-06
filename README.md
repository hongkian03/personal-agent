# Personal Agent

A modular Python assistant with a conversational LangGraph loop and a replaceable OpenAI
adapter. Use the local CLI or an authenticated HTTP endpoint to chat. Conversation checkpoints
and saved skills persist independently of the model provider.

## Run locally

```sh
uv sync
cp .env.example .env
```

Set `PERSONAL_AGENT_MODEL_API_KEY` in `.env` (or provide `OPENAI_API_KEY`). Set
`PERSONAL_AGENT_MODEL` to an OpenAI model available to your account; the initial default
is `gpt-4.1-mini`. Credentials stay in your environment and are never supplied as tool arguments.

```sh
uv run personal-agent
```

The CLI prints a conversation ID after each response. `/new` starts a new conversation;
`/exit` quits. Resume a conversation after restarting:

```sh
uv run personal-agent --conversation YOUR_CONVERSATION_ID
uv run personal-agent --message "Hello, what can you do?"
```

Local persistence defaults to `.data/`. Keep that directory to retain conversations and
skills. It is local application state, not a disposable cache.

## HTTP interface

Set `PERSONAL_AGENT_API_TOKEN` to a private bearer token, then start:

```sh
uv run uvicorn personal_agent.entrypoints.api:create_app --factory --host 0.0.0.0 --port 8000
```

`GET /health` is public process liveness. `POST /chat` requires the configured bearer token;
if no token is configured, chat access fails closed. The token represents the configured
single owner; requests cannot select a different owner or grant themselves tool permissions.

```sh
curl http://localhost:8000/chat \
  -H 'Authorization: Bearer YOUR_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Hello!"}'
```

The response contains `text`, `conversation_id`, and `run_id`. Send `conversation_id` with
subsequent requests to continue the conversation. This endpoint returns a complete response;
streaming is not implemented. API errors redact provider details and credentials.

## Runtime boundaries

- LangGraph cycles between the model and registered tools until it produces a response or
  reaches the configured limit. Timeouts bound model/tool execution.
- Only registered, authorized tools are exposed. Tool calls are validated at invocation.
- Active owner-scoped skills provide reusable instructions, not additional permissions.
  Registered tools can list active skills, save revisions, activate/roll back, archive,
  and get the current time. Discovery uses bounded keyword matching.
- Checkpoints preserve conversation state. A skill repository stores instructions separately.
- The API and CLI use the same runtime and configuration.
- Interrupted turns require a new conversation; tools are not automatically replayed.
  Conversations stop at a history-size limit instead of silently dropping old context.

Spectrum connects the agent to iMessage. IBKR Flex supplies dated portfolio holdings,
closing prices, performance and dividend cash activity. General stock quotes, reminders,
user-defined schedules, saved-workflow execution, and generated-code execution remain planned.

## IBKR portfolio

Create an XML Activity Flex Query in IBKR, then set `PERSONAL_AGENT_IBKR_FLEX_TOKEN` and
`PERSONAL_AGENT_IBKR_FLEX_QUERY_ID` together in the API environment. The API syncs reports
in the background and the agent reads saved snapshots through portfolio tools. Keep the
query's symbol filters empty so new purchases are included automatically.

See [IBKR setup and editable report format](docs/ibkr.md) for the exact report sections,
fields and configuration. Edit [the XML field mapping](src/personal_agent/integrations/ibkr/report_spec.py)
to change what the application reads and [the briefing formatter](src/personal_agent/capabilities/portfolio/briefing.py)
to change what the morning message displays. The saved IBKR query is edited in Client
Portal; Python changes do not update it remotely.

Once a report is available, the bridge sends a separate portfolio briefing between
8:00 and 8:15 a.m. on weekdays in `PERSONAL_AGENT_TIMEZONE`. Flex syncing continues
on weekends, while both proactive messages skip Saturdays and Sundays. Disable the briefing with
`PERSONAL_AGENT_PORTFOLIO_MORNING_ENABLED=false` on the API or bridge. Portfolio values
are report-date observations, not live quotes. Without credentials the existing agent
continues to work and portfolio tools identify the connection as unconfigured.

## Morning iMessage

Set `PERSONAL_AGENT_MORNING_ENABLED=true` in `bridges/spectrum/.env` and restart the API and
bridge. The bridge checks for a message every 30 seconds. It sends to its configured
`PERSONAL_AGENT_ALLOWED_SENDER_ID` between 8:00 and 8:15 a.m. on weekdays in the Python API's
`PERSONAL_AGENT_TIMEZONE` (default `America/New_York`). Keep both processes running during
that window. Days missed entirely are skipped.

The API generates short encouragement using recent messages for variety. It records a
claim in the database before the bridge attempts delivery, so an interrupted or ambiguous
send can be missed, but restarting will not automatically send it twice.

## Hosting and persistence

Use one API process/worker for this initial implementation; conversation serialization is
within that process. Do not run concurrent CLI and API sessions against the same conversation.
Before scaling to multiple API workers or replicas, add cross-process conversation locking.

Set `PERSONAL_AGENT_DATABASE_URL` to a Postgres connection URL for hosted persistence.
Otherwise attach durable storage for `.data/`; ephemeral container storage loses state on
replacement. Storage initialization is currently performed by the runtime, not Alembic.
The service needs outbound access to OpenAI and access to its database. Railway hosts
the API and Spectrum bridge as separate Docker services, with one persistent SQLite
volume mounted at `/data` on the API. The bridge reaches the API over Railway's private
network. See [Railway setup and operations](docs/railway.md) for deployment commands,
required variables, and migration back to a local machine.

## Structure

```text
src/personal_agent/
  agent/                    Conversation loop, tools, and runtime service
  core/                     Shared contracts and trusted execution context
  skills/                   Reusable instructions and discovery
  workflows/                Future saved-tool compositions
  execution/                Future isolated execution boundary
  capabilities/
    portfolio/              IBKR snapshots, read tools and morning briefing
    motivation/             Daily morning encouragement
  messaging/                Future Photon transport
  scheduling/               Schedule contracts and future dispatcher
  integrations/
    llm/                    OpenAI adapter
    market_data/            Future market-data adapter
  storage/                  Checkpoints and owner-scoped repositories
  entrypoints/              CLI and HTTP API
  bootstrap.py              Provider/tool wiring and resource lifecycle
  config.py                 Environment configuration
```

See [architecture](docs/architecture.md), [extension lifecycle](docs/extensions.md), and
[next steps](docs/next-steps.md). Never commit `.env`, credentials, or local application data.

OpenAI integration follows the [Responses function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).
