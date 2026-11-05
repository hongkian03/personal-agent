# Next decisions and implementation

Implemented: interactive CLI/API, OpenAI adapter, bounded LangGraph tool loop, tool-schema
validation and scopes, conversation checkpoints, SQLite/Postgres storage, owner-scoped
versioned skills, skill discovery and conversational skill-management tools. Dependencies
are installed and locked. No tests or live-model validation were requested for this stage.

1. Configure a local OpenAI key and choose a model available to the account; try a conversation.
2. Deploy the API and Spectrum bridge to Lightsail with Docker Compose and a persistent
   SQLite volume. Add a background worker when scheduled functionality is implemented.
3. Validate Photon messaging, identity, webhook verification, delivery tracking and idempotency.
4. Add Alembic migrations before evolving deployed schemas; add cross-process locks before
   running multiple workers/replicas. Add tests in the later phase requested by the user.
5. Implement portfolio calculations and motivational messages. Confirm markets, manual/CSV
   holdings input, and whether the briefing includes sourced company news.
6. Wire Procrastinate, persisted schedules, run/step state and outgoing message delivery.
7. Add conversational preference/schedule tools and validated saved-workflow execution.
8. Evaluate sandboxed code generation/integration installation once actual use demands it.

See [extension lifecycle](extensions.md). Brokerage synchronization, a frontend, semantic
search, and additional channels remain optional future extensions.
