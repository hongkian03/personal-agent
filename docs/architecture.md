# Architecture

## Dependency boundaries

- `core` contains provider-independent types and protocols, using the standard library.
- Capabilities depend on `core`, not on Photon, Alpaca, the queue, or deployment platforms.
- Adapters implement core protocols and translate provider payloads at their boundary.
- `agent` exposes selected capability tools through LangGraph. Scheduled workflows may
  invoke capability functions directly, without a model choosing the execution path.
- `bootstrap` and entrypoints choose providers, inject dependencies, and register handlers.
- Capabilities own their domain models, workflows, tools, and persistence requirements.
  Storage implements repository interfaces; future migrations identify the owning capability.
- Providers and capabilities are explicitly registered. Importing modules must not send
  messages, open database connections, or start jobs.

Holding and price types and market-data/repository protocols live in the portfolio
capability. Integration adapters import these contracts, while portfolio workflows do not
import concrete providers. Keep capability-specific settings and calculations there too.

## Implemented runtime

CLI or authenticated POST /chat -> trusted configured owner -> conversation lock ->
load checkpoint -> retrieve relevant active skills -> LangGraph model/tool loop ->
persist checkpoints -> return response and conversation ID.

OpenAI uses the Responses API through a model adapter. The registry validates tool
arguments and scopes at invocation, with bounded execution and redacted logs. Available
tools are current time and skill list/save/activate/archive. Portfolio and motivation
manifests are still declarations only. Local state uses SQLite; a configured PostgreSQL
URL selects persistent Postgres checkpoints and skill tables. Startup creates initial
schemas; schema migrations and multi-process conversation locks are still pending.

Failed/interrupted graph executions cannot be continued automatically; callers must start
a new conversation. History has a configured size cap rather than automatic compaction.
Use one process, with HTTPS supplied by the hosting platform for remote API access.

## Future messaging and scheduled execution paths

Inbound: Photon event -> authenticate and normalize -> persist/deduplicate -> queue ->
conversation agent -> capability tool -> persisted outgoing message -> Photon adapter.

Scheduled: due schedule -> queue -> capability workflow -> persist output -> outgoing
message -> Photon adapter. Portfolio arithmetic uses Python Decimal, not model output.

The registry currently stores metadata only. Handler binding, enabling/disabling capabilities,
and runtime dispatch are future work. A missing handler must fail explicitly.

## Reliability and identity

- Persist local recurrence, an IANA timezone, and next due time; dispatch due jobs with
  Procrastinate. Define daylight-saving, late-run, and holiday behavior explicitly.
- Give each occurrence a stable run ID; retries reuse it. Protect inbound event processing
  and outgoing sends against duplicates. Reconcile ambiguous send timeouts before retrying;
  provider idempotency support must be verified before promising duplicate-free delivery.
- Store submitted and delivered as separate states. API acceptance is not delivery.
- Verify incoming provider events and allowlist the configured sender before exposing data.
- Store secrets in environment configuration, redact logs, and inject narrowly scoped clients.
- Record price timestamp, currency, source, and session. Report missing/stale data explicitly.
- Keep holdings, schedules, preferences, and run history in application tables; LangGraph
  checkpoints track conversation execution, not the application's business records.

## Adding a capability

1. Create `capabilities/<name>/` with a manifest and domain/workflow modules.
2. Define any new provider-independent protocols required by that capability.
3. Implement its workflow and expose conversational tools only where useful.
4. Add capability-owned persistence migrations and concrete adapters as needed.
5. Register metadata and bind implementations in the composition root.
6. Verify its actual behavior, failure paths, and duplicate handling before enabling it.

Keep one repository and shared Postgres initially. Process boundaries can change without
changing capability logic. Avoid a dynamic plugin loader or separate service per feature.


## Interactive extension layer

`skills/` manages reusable instruction revisions. `workflows/` manages versioned tool
compositions. These are runtime data stored through owner-scoped repositories, not new
Python packages written into the application image. The existing capability registry
continues to describe developer-provided capabilities; it is not the skill library.

`agent/management.py` reserves tool IDs for authoring skills, versioning workflows and
editing schedules. `agent/tools.py` defines the common invocation boundary. Both agent
conversations and saved workflows must use that same boundary for validation, permissions,
and audit. Skill text does not grant scopes. Skill handlers are wired in bootstrap;
workflow and schedule management handlers remain unimplemented.

For each incoming message: authenticate identity, load conversation and originating run
references, retrieve relevant active skills, expose currently authorized tools, execute
requested calls, persist results, and reply. Proactive messages retain their conversation
and run references so replies can refer to the briefing that prompted them.

The application supplies `ExecutionContext`; the model cannot choose owner IDs or grant
itself scopes. Management handlers scope repository operations to that trusted owner.
New or updated skills become discoverable after activation; each chat turn uses one
retrieved set of instruction revisions. Revisions and their IDs are included in checkpointed
instructions. Future saved workflows and schedules will pin workflow and skill revisions.

Generated scripts are a separate future extension through `execution/SandboxExecutor`.
There is no host-shell tool or implementation. Do not import generated Python into the
API/worker, dynamically install packages there, or inherit application secrets in a sandbox.

See [extension lifecycle](extensions.md) for the workflow and staged implementation.
