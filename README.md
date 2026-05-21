# Database Read MCP Server

A Model Context Protocol (MCP) server for read-only PostgreSQL access over stdio.

## What you get

- Read-only DB access over MCP: list tables, inspect schemas, run `SELECT`/`WITH` queries, `EXPLAIN` plans
- Parse-based write protection (rejects `INSERT/UPDATE/DELETE/MERGE/DROP/CREATE/ALTER/TRUNCATE` and multi-statement payloads)
- Per-transaction `READ ONLY` + statement/lock/idle timeouts + hard row caps + streamed batched fetches
- Per-environment connection pools (local/staging/production/...) selectable per call
- Schema allowlist (default: `public`)

## Requirements

- Python 3.10+
- PostgreSQL
- An MCP-compatible client (Cursor, Claude Desktop, Codex, ...)
- [`uv`](https://docs.astral.sh/uv/) for project management

## Install

```bash
uv sync
```

## Connection environment variables

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Default connection string |
| `DATABASE_URL_<ENV>` | Per-environment connection (e.g. `DATABASE_URL_LOCAL`, `DATABASE_URL_STAGING`, `DATABASE_URL_PRODUCTION`) |
| `DATABASE_TARGET_ENV` | Selects active environment (aliases: `DATABASE_ENV`, `DB_ENV`; values like `dev`/`prod`/`stage` normalize to `local`/`production`/`staging`) |

Every MCP tool also accepts an `environment` argument to override per-call without restarting the server.

## Safety / tuning environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_STATEMENT_TIMEOUT_MS` | `60000` | Per-query timeout |
| `DB_LOCK_TIMEOUT_MS` | `15000` | Lock acquisition timeout |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `60000` | Kills idle-in-txn sessions |
| `DB_MAX_ROWS` | `10000` | Hard row cap (truncation flagged in response) |
| `DB_FETCHMANY_SIZE` | `1000` | Batch fetch size while streaming |
| `DB_POOL_SIZE` | `5` | Connections per environment |
| `DB_MAX_OVERFLOW` | `2` | Pool overflow |
| `DB_POOL_TIMEOUT` | `30` | Pool wait timeout (s) |
| `DB_POOL_RECYCLE` | `1800` | Recycle connections after (s) |
| `DB_ALLOWED_SCHEMAS` | `public` | Comma-separated schemas exposed to tools |

## Available tools

| Tool | Purpose |
|------|---------|
| `health_check` | Database + server connectivity check |
| `database_query` | Run a read-only SQL query (SELECT/WITH); supports `max_rows`, `offset`, `statement_timeout_ms`, `environment` |
| `explain_query` | `EXPLAIN [ANALYZE]` for a query, JSON plan |
| `list_tables` | Tables in the chosen schema |
| `get_table_schema` | Columns + primary keys for one table |
| `get_all_schemas` | Bulk dump: columns + primary keys (2 queries total) and optional `sample_data` |

`database_query` response shape:

```json
{
  "status": "success",
  "results": [...],
  "count": 42,
  "truncated": false,
  "offset": 0,
  "max_rows": 10000,
  "statement_timeout_ms": 60000,
  "environment": "default"
}
```

## Client setup

### Cursor

Cursor reads MCP config from `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global).

```json
{
  "mcpServers": {
    "database-reader": {
      "command": "uv",
      "args": ["--directory", "${workspaceFolder}", "run", "database_read.py"],
      "env": {
        "DATABASE_TARGET_ENV": "local",
        "DATABASE_URL_LOCAL": "${env:DATABASE_URL_LOCAL}",
        "DATABASE_URL_STAGING": "${env:DATABASE_URL_STAGING}",
        "DATABASE_URL_PRODUCTION": "${env:DATABASE_URL_PRODUCTION}"
      }
    }
  }
}
```

### Claude Desktop

Config path: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%AppData%\Claude\claude_desktop_config.json` (Windows). Use **absolute paths**.

```json
{
  "mcpServers": {
    "database-reader": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/mcp-prototype", "run", "database_read.py"],
      "env": {
        "DATABASE_TARGET_ENV": "local",
        "DATABASE_URL_LOCAL": "postgresql://user:password@localhost:5432/db_name"
      }
    }
  }
}
```

### OpenAI Codex

```bash
codex mcp add database-reader \
  --env DATABASE_TARGET_ENV=local \
  --env DATABASE_URL_LOCAL='postgresql://user:password@localhost:5432/db_name' \
  -- uv --directory /ABSOLUTE/PATH/TO/mcp-prototype run database_read.py
```

Or `~/.codex/config.toml`:

```toml
[mcp_servers.database-reader]
command = "uv"
args = ["--directory", "/ABSOLUTE/PATH/TO/mcp-prototype", "run", "database_read.py"]

[mcp_servers.database-reader.env]
DATABASE_TARGET_ENV = "local"
DATABASE_URL_LOCAL = "postgresql://user:password@localhost:5432/db_name"
```

### Other MCP clients

Any stdio MCP client accepts the same fields: `command = "uv"`, `args = ["--directory", "<repo>", "run", "database_read.py"]`, plus `env` entries for connection URLs.

## Switching environments per call

```json
{
  "name": "database_query",
  "arguments": {
    "query": "SELECT * FROM users LIMIT 5",
    "environment": "staging"
  }
}
```

The server keeps a separate connection pool per environment, so switching does not require a restart.

## Hardening: dedicated read-only role

The MCP enforces read-only at the application layer (parse-time validator, `SET TRANSACTION READ ONLY`, dangerous-function blacklist). The strongest fourth layer is a Postgres role with `SELECT`-only grants — the database itself refuses writes even if every line of Python were replaced. See [docs/database-role-setup.md](./docs/database-role-setup.md) for the minimal setup, triage query examples, and the optional view-based pattern for redacting sensitive columns without changing MCP code.

## Tests

See [CLAUDE.md](./CLAUDE.md#testing) for the regression suite (unit + integration).

## Notes

- SQLAlchemy requires `postgresql://` (not `postgres://`).
- Restart the MCP client after editing its config.
- Never commit real credentials; use shell env vars or a secret manager.
