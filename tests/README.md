# Tests

Run `.venv/bin/python -m pytest` (or `uv run pytest`). Tests use synthetic broker XML,
mock HTTP transports, fake model/broker clients and temporary SQLite databases.

- `test_ibkr_adapter.py`: Flex parsing, request pacing/retries and credential redaction.
- `test_portfolio.py`: owner isolation, snapshot replacement, cached reads and daily claims.
- `test_portfolio_runtime.py`: background lifecycle, persisted restart, authentication and scopes.

No real IBKR credentials or brokerage network access are needed. Real-account field
compatibility and a first successful sync must also be checked after configuring the
saved Activity Flex Query described in `docs/ibkr.md`.
