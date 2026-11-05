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

The portfolio and motivation capabilities still reserve names for future functionality.
Photon/iMessage, stock retrieval, reminders, scheduled delivery, saved-workflow execution,
and generated-code execution are not wired. The assistant must not claim those actions
are available. No test suite has been added at this stage.

## Hosting and persistence

Use one API process/worker for this initial implementation; conversation serialization is
within that process. Do not run concurrent CLI and API sessions against the same conversation.
Before scaling to multiple API workers or replicas, add cross-process conversation locking.

Set `PERSONAL_AGENT_DATABASE_URL` to a Postgres connection URL for hosted persistence.
Otherwise attach durable storage for `.data/`; ephemeral container storage loses state on
replacement. Storage initialization is currently performed by the runtime, not Alembic.
Use HTTPS at the hosting ingress for remote access. The service needs outbound access
to OpenAI and access to its database. Hosting remains
undecided, and no platform-specific deployment configuration is included.

## Structure

```text
src/personal_agent/
  agent/                    Conversation loop, tools, and runtime service
  core/                     Shared contracts and trusted execution context
  skills/                   Reusable instructions and discovery
  workflows/                Future saved-tool compositions
  execution/                Future isolated execution boundary
  capabilities/
    portfolio/              Portfolio-owned contracts and future behavior
    motivation/             Future morning-message behavior
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
