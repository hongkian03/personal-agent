# Tests

Run `.venv/bin/python -m pytest` (or `uv run pytest`). Tests use synthetic broker XML,
mock HTTP transports, fake model/broker clients and temporary SQLite databases.

- `test_ibkr_adapter.py`: Flex parsing, request pacing/retries and credential redaction.
- `test_portfolio.py`: owner isolation, snapshot replacement, cached reads and daily claims.
- `test_portfolio_runtime.py`: background lifecycle, persisted restart, authentication and scopes.
- `test_skill_catalog.py`: metadata-only catalogs, exact name/ID selection, bounded discovery,
  owner/tool filtering, active revision loading and the conversational tool loop.
- `test_skill_import.py`: CLI packaging, startup imports, atomic revision/activation writes,
  owner isolation, validation, and preservation of chat edits across restarts.
- `test_provider_streaming.py`: provider activity, completed output integrity, interrupted
  SSE streams, model-call deadlines, and cancellation cleanup through the real SDK.
- `test_chat_progress.py`: shared timing, activity isolation, bounded buffering, streaming
  API authentication, compatibility, safe errors, and disconnect cancellation.
- `test_cli_progress.py`: progress display with one final CLI answer.
- `test_progress_graph.py`: live activity across compiled LangGraph rounds, checkpoint
  integrity, and prevention of partial-tool execution after an interrupted provider stream.
- `test_api_logging.py`: descriptive API failure messages, correlation IDs, underlying
  exception locations, and exclusion of private provider/user data from warnings.

The Spectrum bridge has separate SSE parser and delivery tests: run `npm test` and
`npm run typecheck` in `bridges/spectrum`. They use synthetic streams and fake sends.

No real IBKR credentials or brokerage network access are needed. Real-account field
compatibility and a first successful sync must also be checked after configuring the
saved Activity Flex Query described in `docs/ibkr.md`.
