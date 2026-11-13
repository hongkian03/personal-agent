# Long-term memory

Cognee runs as a separate service. The agent talks to a small authenticated HTTP wrapper
in `services/cognee/app.py`, which calls the pinned `cognee[api]==1.6.1` Python SDK.
The SDK is installed only in that service's container, not in the agent environment.
The wrapper gives our application stable source identifiers for retries and forgetting;
it does not expose Cognee's administrative API or require Cognee Cloud.

## Runtime flow

1. With memory enabled, an ordinary chat turn requests relevant source chunks from Cognee.
   The agent adds bounded results as untrusted reference context alongside its current
   conversation history and persona. Recall has a short timeout; unavailable memory does
   not prevent a reply.
2. After a completed ordinary exchange, the agent writes a source record to a durable
   outbox in its existing application database. It labels user and assistant statements,
   records the conversation/turn and capture date, and truncates oversized exchanges.
3. A single background consumer delivers queued records to Cognee and retries failures
   with backoff. The service builds the graph and embeddings before acknowledging the
   record; stable IDs and persistent receipts prevent duplicate completed writes.
4. New conversations can retrieve those records once indexing completes. `/new` resets
   conversation context but preserves long-term memory. Existing chat history is not
   automatically imported; no migration of checkpoints or skills is required.

The initial recall path uses Cognee's `CHUNKS` retrieval mode. Cognee still builds graph
memory, but the agent receives source chunks rather than a second generated answer.
Graph reasoning, selective fact curation, document ingestion, and onboarding workflows
can be added after evaluating this basic flow.

The model can use `memory.remember`, `memory.search`, and `memory.forget` explicitly.
Turns invoking memory tools are excluded from automatic exchange capture so a forget
request is not immediately stored again. Search returns source IDs that forgetting can
use. Forgetting tombstones the source locally, queues remote deletion, and blocks delayed
retries from restoring it. A queued deletion is not confirmation that remote deletion
has finished. Forgetting one source does not remove the same fact from other sources,
conversation checkpoints, backups, or future messages that repeat it.

Skills, activation pointers, portfolio snapshots, daily claims, conversation checkpoints,
and tool-operation receipts remain authoritative in the application database. In SQLite
mode the memory outbox shares `skills.sqlite`; Cognee's independent data is on its own
volume. Neither database is a cache that can be discarded safely.

## Local setup

Copy `services/cognee/.env.example` to `services/cognee/.env`. Generate a service token
with `openssl rand -hex 32`, then set it as `COGNEE_SERVICE_TOKEN`. Set `LLM_API_KEY` and
choose a model available to the account. The example configures OpenAI extraction and
`text-embedding-3-small` with 1536 dimensions. Keep the embedding model and dimensions
consistent across restarts; changing them requires a planned re-index.

Set these values in the root `.env`:

```dotenv
PERSONAL_AGENT_MEMORY_ENABLED=true
PERSONAL_AGENT_COGNEE_URL=http://cognee:8000
PERSONAL_AGENT_COGNEE_API_TOKEN=<same value as COGNEE_SERVICE_TOKEN>
```

For a host-run CLI/API, use `http://127.0.0.1:8002` instead of `http://cognee:8000`.
Start just memory, or memory with the existing application services:

```sh
docker compose --profile memory up -d --build cognee
docker compose --profile memory up -d --build
```

The optional `memory` profile publishes Cognee only on `127.0.0.1:8002`. The agent can
start before Cognee; queued ingestion resumes when the service becomes available.
Without the profile and enabled settings, the agent behaves as before.

Optional agent settings and defaults:

| Variable suffix (all prefixed `PERSONAL_AGENT_`) | Default | Purpose |
| --- | --- | --- |
| `MEMORY_RECALL_TIMEOUT_SECONDS` | 3 | Bound interactive recall latency |
| `MEMORY_INGEST_TIMEOUT_SECONDS` | 180 | Allow background indexing time |
| `MEMORY_POLL_INTERVAL_SECONDS` | 5 | Poll durable pending work |
| `MEMORY_RETRY_SECONDS` | 30 | Initial retry backoff |
| `MEMORY_MAX_CONTEXT_CHARS` | 6000 | Bound recalled context |

## Railway deployment

`.railway/railway.ts` declares a `cognee` service with a `cognee-data` volume mounted at
`/cognee-storage`. It deliberately uses one replica, one Uvicorn worker, and no overlap
between deployments: SQLite, LanceDB, and the embedded graph store must not be opened
by multiple processes. Use a separate service with external database backends before
scaling memory horizontally. The wrapper serializes writes while allowing recall during indexing.

The volume holds `system/` (Cognee databases), `data/` (ingested source storage), and
`control.sqlite` (write receipts, source mapping, and deletion tombstones). Preserve the
whole volume during redeployment and take a consistent backup before upgrading Cognee.
The initial 500 MB volume fits the current plan; monitor usage and increase storage before
large imports. The agent's `agent-data` volume remains independently required.

On `cognee`, set the variables from `services/cognee/.env.example` plus `PORT=8000`.
The launcher binds a dual-stack IPv6/IPv4 socket so Railway's private network and local health probes can reach it. Its `/health`
route checks that the wrapper started; it does not verify model credentials, successful
indexing, or recall quality. No public domain is needed.

On `agent-api`, set:

```dotenv
PERSONAL_AGENT_MEMORY_ENABLED=true
PERSONAL_AGENT_COGNEE_URL=http://cognee.railway.internal:8000
PERSONAL_AGENT_COGNEE_API_TOKEN=${{cognee.COGNEE_SERVICE_TOKEN}}
```

Provision infrastructure with the existing Railway workflow, then deploy Cognee from
its own directory and the API from the repository root:

```sh
railway config plan
railway config apply
railway up services/cognee --path-as-root --service cognee --environment production --detach
railway up --service agent-api --environment production --detach
railway logs --service cognee --lines 50
```

A completed deployment is not a memory smoke test. Tell the agent a distinct fact, allow
background indexing to finish, start a new conversation, and ask about it. Restart Cognee
and verify recall again. Search for its source ID, forget that source, wait for deletion,
and check that it is absent from subsequent memory searches. Existing conversation
history can still contain the fact, so use a new conversation for that last check.

## Graph snapshots

Download an interactive graph snapshot through the running agent API:

```sh
personal-agent memory export-graph --api-url http://localhost:8000 --output memory-graph.html
# Our private Railway deployment (requires Railway CLI login and linked project):
personal-agent memory export-graph --railway --output memory-graph.html
```

The CLI uses `PERSONAL_AGENT_API_TOKEN` from the local environment or root `.env`.
Use the reachable hosted agent URL for Railway. The authenticated `GET /memory/graph`
route fixes the owner from server configuration and proxies the private Cognee service.
It does not open local databases or start another ingestion worker.
With `--railway`, the CLI makes the download through Railway SSH inside the private
network and saves the HTML locally; the hosted API token stays in the container.

The default shows a bounded neighborhood of at most 500 nodes; add `--full` to request
the whole graph, which can be large. Existing output files are not overwritten. Open
the downloaded HTML in a browser; the SDK viewer may load external JavaScript assets.
An empty store produces an explanatory HTML page. The snapshot includes stored memory
content as of export time and does not update as new memories arrive. Pending indexing
or remote deletions may not yet be reflected. Export generation serializes with writes.

The private service exposes authenticated `POST /graph` with `owner_id` and optional
`full`. The export uses the SDK inside the existing Cognee process; no extra graph
server or shared-file database access is needed.

## Service API

All mutation/retrieval routes require `Authorization: Bearer <COGNEE_SERVICE_TOKEN>`.
The token must be at least 32 characters. Owner IDs are supplied by the trusted agent,
not by the chat model, and mapped to hashed dataset names. Source IDs become deterministic
UUIDs inside Cognee. The wrapper logs operation failures and exception class names without request/response
bodies or exception messages. SDK logging defaults to `WARNING`, telemetry is disabled,
and tracing is disabled. Raising `LOG_LEVEL` to `INFO` or `DEBUG` can expose query/source
text through upstream Cognee logs; upstream warning/error diagnostics may also contain
provider details. Keep those logs access-controlled.

- `GET /health`: process health, without credentials.
- `POST /remember`: `owner_id`, `memory_id`, `text`, optional `metadata`; returns
  `remembered` only after indexing succeeds, or `forgotten` for a tombstoned source.
- `POST /recall`: `owner_id`, `query`, optional `limit`; returns source results with
  `memory_id`, `text`, and original metadata.
- `POST /forget`: `owner_id`, `memory_id`; removes that source's indexed memory and
  prevents late re-ingestion under the same identifier.

The service uses source-level deletion, not semantic deletion of every mention of a fact.
Raw source files and backups need their own retention policy. The pinned SDK's source
semantics are documented in [Cognee's remember operation](https://docs.cognee.ai/core-concepts/main-operations/remember)
and [forget operation](https://docs.cognee.ai/core-concepts/main-operations/forget).
