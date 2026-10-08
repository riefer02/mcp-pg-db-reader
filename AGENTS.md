# AGENTS.md

Developer-facing notes for working on this repo. For user-facing setup, configuration, tool list, and env var reference, see [README.md](./README.md).

## Project overview

Read-only PostgreSQL MCP server over stdio. Single-file server at `database_read.py` (~750 lines) plus a `tests/` regression suite.

## Architecture

`database_read.py` is intentionally single-file. Layout:

1. **Logging + config** — JSON log events to stderr; env-driven safety/tuning constants.
2. **Env discovery** (`_discover_database_urls`, `_normalize_env_name`) — turns `DATABASE_URL_<ENV>` vars into a name→URL map, with alias normalization (`dev`→`local`, `prod`→`production`, ...).
3. **Engine cache** (`_get_engine`) — lazy, thread-safe, per-environment SQLAlchemy engines. `atexit` disposes all on shutdown.
4. **SQL safety** (`validate_read_only_sql`) — `sqlparse`-based: strips comments, rejects multi-statement payloads, rejects non-`SELECT`/`WITH` starters, walks all tokens to block any `Keyword.DDL`, write `Keyword.DML` (`INSERT`/`UPDATE`/`DELETE`/`MERGE`/...), or `_BLOCKED_ANY` keyword (`INTO`, which catches `SELECT * INTO new_t FROM ...`). Replaces the old word-boundary regex which false-positived on identifiers like `is_deleted`. Defense in depth: the wrapped query always runs in a `SET TRANSACTION READ ONLY` transaction, so even a bypassed validator cannot mutate state.
5. **Query execution** (`execute_query`) — wraps the user query as `SELECT * FROM (q) AS _mcp_sub LIMIT :n+1 OFFSET :o`, accepts bound params (reserving the pagination bind names), runs inside a `READ ONLY` transaction with bounded `SET LOCAL` timeouts, streams results in batches, enforces `DB_MAX_ROWS` as a hard ceiling, detects truncation via the extra row, and logs query length without SQL text. Signal handlers (`SIGINT`/`SIGTERM`) only install when running on the main thread.
6. **Schema allowlist** (`_validate_schema`, `_quote_ident`) — every tool that takes a `schema` arg goes through the allowlist (`DB_ALLOWED_SCHEMAS`, default `public`). Identifiers used in unparameterized SQL go through `_quote_ident`, which rejects embedded `"` and `\0`.
7. **Row serialization** (`_jsonify_value`) — converts `Decimal`/`UUID`/`datetime`/`date`/`time`/`bytes`/nested containers into JSON-safe primitives before MCP returns them.
8. **MCP tools** — thin wrappers that call `execute_query` and shape responses; all advertise the read-only annotation and share a per-process sliding-window rate limit. Errors are returned as `{"status": "error", "message": ...}` rather than raising.

## Running the server

```bash
uv sync
uv run python database_read.py
```

## Testing

Two tiers:

- **Unit** — no DB. Validators, env discovery, JSON serialization, startup behavior, tool error paths (with mocked engine).
- **Integration** — gated on `MCP_TEST_DATABASE_URL`. Spawns a unique schema per test, runs the real tool functions end-to-end, drops the schema on teardown.
- **Safety invariants** (`tests/test_safety_invariants.py`) — the canonical proof that no write reaches PG. Parametrized over every PG statement form that mutates state (DML, DDL, COPY, CALL, DO, LOCK, BEGIN/COMMIT, LISTEN/NOTIFY, VACUUM, GRANT/REVOKE, SELECT INTO, writable CTEs, comment-injection variants, ...). Includes defense-in-depth integration tests that monkeypatch out the validator and verify the `READ ONLY` transaction still rejects writes and leaves data unchanged.

```bash
# Unit only (always runs; integration auto-skips)
uv run pytest

# Full suite — point at any reachable Postgres
docker run --rm -d --name mcp_test_pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
MCP_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres uv run pytest
docker stop mcp_test_pg
```

The integration fixture (`mcp_env` in `tests/conftest.py`) creates a unique throwaway schema, sets `DB_ALLOWED_SCHEMAS` to it, reloads the module, and clears the engine cache — tests can safely run against any Postgres without touching `public`.

## Operational hardening

See [docs/database-role-setup.md](./docs/database-role-setup.md) for the recommended deployment posture: dedicated `SELECT`-only Postgres role, optional redaction views for PHI/PII, verification queries, and the per-engine portability notes. The MCP's app-layer protections (validator, READ ONLY transaction, function blacklist) are best paired with a DB role that has no write grants — that turns "writes are blocked in four places" into a real four-layer wall.

## Key dependencies

- `mcp[cli]>=1.30.0,<2` — maintained v1 FastMCP line; v2 is intentionally excluded pending migration
- `SQLAlchemy>=2.0.39` — engine + Core
- `psycopg2-binary>=2.9.10` — PG driver
- `sqlparse>=0.4.4` — write/multi-statement detection
- `pytest`, `pytest-cov` (dev) — regression suite
