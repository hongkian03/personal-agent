# Next decisions and implementation

Implemented: interactive CLI/API, OpenAI adapter, bounded LangGraph tool loop, tool-schema
validation and scopes, conversation checkpoints, SQLite/Postgres storage, owner-scoped
versioned skills, skill discovery and conversational skill-management tools. Dependencies
are installed and locked. IBKR Flex reporting, cached portfolio tools, background sync,
and a separate morning portfolio briefing are implemented with fixture and integration tests.
The first live broker sync needs the user's Flex token and XML query ID; see [IBKR setup](ibkr.md).

1. Configure a local OpenAI key and choose a model available to the account; try a conversation.
2. Host the API and Spectrum bridge on Railway with a persistent SQLite volume; see
   [Railway operations](railway.md). Keep Docker Compose for a future local host.
   Add a background worker when scheduled functionality outgrows bridge polling.
3. Validate Photon messaging, identity, webhook verification, delivery tracking and idempotency.
4. Add Alembic migrations before evolving deployed schemas; add cross-process locks before
   running multiple workers/replicas. Add tests in the later phase requested by the user.
5. Validate the configured IBKR report against the user's actual holdings. Add general
   stock prices or sourced company news separately if those are wanted later.
6. Wire Procrastinate, persisted schedules, run/step state and outgoing message delivery.
7. Add conversational preference/schedule tools and validated saved-workflow execution.
8. Evaluate sandboxed code generation/integration installation once actual use demands it.

See [extension lifecycle](extensions.md). Brokerage synchronization, a frontend, semantic
search, and additional channels remain optional future extensions.
