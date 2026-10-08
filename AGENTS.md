# Agent instructions

Read [README.md](./README.md) for setup and configuration. For database-role and redaction guidance, see [docs/database-role-setup.md](./docs/database-role-setup.md).

## Project map

- `database_read.py` is the intentionally single-file PostgreSQL MCP server.
- `tests/` contains unit, safety-invariant, and PostgreSQL integration tests.

## Working rules

- Keep changes focused and follow existing patterns. Do not split the server into modules unless asked.
- Treat database access as security-sensitive. Preserve SQL validation, schema allowlisting, read-only transactions, row/timeout limits, and the database-role defense in depth.
- Use bound parameters for values. Never log SQL text or parameter values. `_row_limit` and `_row_offset` are reserved server bind names.
- Schema sample data is opt-in; keep `get_all_schemas` defaulting to `include_samples=False`.
- Never use production data or credentials in tests, examples, or logs. Integration tests create and drop a temporary schema; run them only against a disposable database.
- Keep the MCP SDK on the maintained v1 line (`mcp[cli]>=1.30.0,<2`). Moving to v2 is a breaking migration; do not widen this range without an explicit migration and compatibility tests.
- Update README/configuration docs and tests whenever behavior or environment variables change.

## Verification

```bash
uv sync --locked --all-groups
uv run python -m pytest
uv lock --check
```

The unit suite skips integration tests unless `MCP_TEST_DATABASE_URL` is set. For database changes, run the full suite against a disposable PostgreSQL instance as described in [README.md](./README.md#tests). For SQL-safety changes, include `tests/test_safety_invariants.py` and its PostgreSQL-backed cases.
