# Database Role Setup

This guide covers the recommended way to wire this MCP server into a Postgres database safely: a dedicated **read-only role** for the MCP, with optional **views** to redact sensitive columns. It is aimed at engineers running triage / health-check / data-exploration workloads where the LLM agent should be able to look around freely but never write anything, and never see fields like email or SSN unless explicitly opted in.

The application code already enforces read-only at three layers (parse-time validator, `SET TRANSACTION READ ONLY`, dangerous-function blacklist). A dedicated DB role is the fourth and most durable layer: it works even if every line of Python were replaced, because the database itself refuses the write.

## Why a dedicated role

| Approach | Where the wall lives | What it survives |
|----------|---------------------|------------------|
| App-layer redaction (Python lists) | The MCP process | Anything that goes through the MCP |
| Validator (`sqlparse` parse-time) | The MCP process | Single-statement bypass, multi-statement smuggling |
| `SET TRANSACTION READ ONLY` | Postgres session | Validator bypass; still inside the same connection |
| **Dedicated DB role** | Postgres catalog | Anything connecting with that role — MCP, psql, BI tools, cron, future agents |

The role-level wall is the only one that protects you against "we forgot to use the MCP" — e.g. a teammate copies the connection string into a notebook and connects directly. With a properly scoped role, the worst that notebook can do is read what the role can read.

## Minimal setup

This is the entire setup for a triage / health-check use case where you do **not** have PHI to hide and just want read-only.

```sql
-- 1. Create the role
CREATE ROLE mcp_reader LOGIN PASSWORD 'change-me-and-store-securely';

-- 2. Let it connect to the database and see the schema
GRANT CONNECT ON DATABASE myapp TO mcp_reader;
GRANT USAGE   ON SCHEMA   public TO mcp_reader;

-- 3. Read everything in public, including any tables created later
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO mcp_reader;
```

Then point the MCP at this role:

```bash
DATABASE_URL=postgresql://mcp_reader:change-me-and-store-securely@host:5432/myapp
```

That's it. The MCP can run any `SELECT` / `WITH` query against any current or future table, and the database itself will refuse anything else.

## Verifying the role really is read-only

Run these from any client (psql, the MCP, your shell) **as `mcp_reader`**. Each one should fail.

```sql
-- Should fail: permission denied
INSERT INTO users (name) VALUES ('attempt');
UPDATE users SET name = 'x' WHERE id = 1;
DELETE FROM users;
CREATE TABLE attempt (id int);
ALTER TABLE users ADD COLUMN x text;
DROP TABLE users;
TRUNCATE users;
GRANT SELECT ON users TO PUBLIC;
```

And these should succeed:

```sql
SELECT 1;
SELECT current_user;             -- → mcp_reader
SELECT current_setting('is_superuser');  -- → off
SELECT * FROM users LIMIT 1;
```

You can also inspect the grants directly:

```sql
SELECT table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'mcp_reader'
ORDER BY table_schema, table_name;
```

If `privilege_type` is anything other than `SELECT` for any row, you have more access than you want.

## Triage / health-check examples

These are queries the MCP can run against the role above. They cover the common "is anything broken" use cases without touching sensitive data.

### Connection + version sanity

```sql
SELECT current_database()  AS db,
       current_user        AS role,
       version()           AS pg_version,
       now()               AS server_time;
```

### Table sizes (where is the data?)

```sql
SELECT schemaname,
       relname                                                AS table,
       n_live_tup                                             AS row_estimate,
       pg_size_pretty(pg_total_relation_size(relid))          AS total_size
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;
```

### Recently inserted rows (is the pipeline alive?)

```sql
SELECT relname AS table,
       n_tup_ins         AS inserts_since_stats_reset,
       last_autoanalyze,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY n_tup_ins DESC
LIMIT 20;
```

### Long-running queries (is anything stuck?)

```sql
SELECT pid,
       state,
       now() - query_start  AS running_for,
       left(query, 120)     AS query_preview
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '30 seconds'
ORDER BY query_start;
```

### Replication lag (for read replicas)

```sql
SELECT now() - pg_last_xact_replay_timestamp() AS replication_lag;
```

### Failing constraints / dead-tuple bloat

```sql
SELECT schemaname,
       relname        AS table,
       n_dead_tup     AS dead_rows,
       n_live_tup     AS live_rows,
       round(100.0 * n_dead_tup / NULLIF(n_live_tup, 0), 2) AS dead_pct
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY dead_pct DESC NULLS LAST
LIMIT 20;
```

All of the above run fine under `mcp_reader` and never expose individual user records.

## Adding views for sensitive tables (optional)

If a specific table has columns you do not want the MCP (or whatever agent calls it) to read, **do not change the MCP code**. Add a view and shift the grants. The MCP stays dumb.

### Pattern A — `_safe` suffix (explicit; LLM sees both names)

```sql
-- Build the redacted view
CREATE VIEW public.patients_safe AS
SELECT
    id,
    status,
    created_at,
    date_trunc('year', dob)                                       AS dob_year,
    regexp_replace(email, '(.).*@', '\1***@')                     AS email_masked,
    encode(digest(ssn::text, 'sha256'), 'hex')                    AS ssn_hash
FROM public.patients;

-- Hide the raw table; expose the view
REVOKE SELECT ON public.patients         FROM mcp_reader;
GRANT  SELECT ON public.patients_safe    TO   mcp_reader;
```

The MCP's `list_tables` will now show `patients_safe` but not `patients`. The LLM is free to query `patients_safe` like any other table.

`encode(digest(...))` requires the `pgcrypto` extension:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

### Pattern B — same name, hidden schema (LLM only sees the view name)

If you want the LLM to keep writing `SELECT … FROM patients` without learning a new name:

```sql
-- Move the raw table into a private schema only your admin can see
CREATE SCHEMA private;
ALTER TABLE public.patients SET SCHEMA private;

-- Recreate `public.patients` as a redacted view over the private table
CREATE VIEW public.patients AS
SELECT id, status, created_at,
       date_trunc('year', dob)                        AS dob_year,
       regexp_replace(email, '(.).*@', '\1***@')      AS email_masked,
       encode(digest(ssn::text, 'sha256'), 'hex')     AS ssn_hash
FROM private.patients;

-- Lock the private schema away from the MCP
REVOKE USAGE ON SCHEMA private FROM mcp_reader;
GRANT  SELECT ON public.patients TO mcp_reader;
```

Now `mcp_reader` queries `patients` and transparently gets the redacted view. Your admin role (with `USAGE` on `private`) still gets the real table.

### Quick recipes by column type

```sql
-- Email: keep domain, hash the local part
regexp_replace(email, '(.).*@', '\1***@')                AS email_masked

-- Phone: last 4 only
'***-***-' || right(phone, 4)                            AS phone_masked

-- SSN: deterministic hash (joins still work, raw is gone)
encode(digest(ssn::text, 'sha256'), 'hex')               AS ssn_hash

-- Date of birth: keep year only
date_trunc('year', dob)                                  AS dob_year

-- Credit card: last 4 only
'**** **** **** ' || right(card_number, 4)               AS card_masked

-- Full name: first initial only
left(first_name, 1) || '. ' || last_name                 AS name_short
```

Deterministic hashes (same input → same output) are useful because they preserve joinability and grouping without exposing the raw value. If you don't need to join on a column at all, drop it from the view entirely.

## Pitfalls

- **`ALTER DEFAULT PRIVILEGES`** matters. Without it, any table you `CREATE` *after* the initial setup will not be visible to `mcp_reader`. The default-privileges grant fixes that going forward — but only for tables created by the role that ran the `ALTER`. If your team uses multiple roles to create tables (migration role, dev role, etc.), repeat the `ALTER DEFAULT PRIVILEGES` for each, or scope by `FOR ROLE`.
- **Sequences are separate.** `GRANT SELECT ON ALL TABLES` does not grant on sequences. The MCP doesn't need sequences for reads, so this is usually fine — but if you query `nextval(...)` (which the validator already blocks anyway) the role lacks the grant.
- **Views inherit the underlying table's permissions by default.** A view created by a superuser, then granted to `mcp_reader`, runs as the view's owner — not as `mcp_reader`. That is what makes the "expose a redacted view, revoke the underlying table" pattern work. Don't `CREATE VIEW … SECURITY INVOKER` unless you mean to.
- **Functions are not covered by `GRANT SELECT`.** PG ships with `pg_terminate_backend`, `pg_cancel_backend`, etc., which are dangerous regardless of role grants. The MCP validator blocks these by name; for additional belt-and-suspenders, `REVOKE EXECUTE ON FUNCTION pg_terminate_backend(integer) FROM PUBLIC` (and from `mcp_reader` if explicit grants exist).
- **Connection limits.** Production roles usually want `ALTER ROLE mcp_reader CONNECTION LIMIT 5` to prevent runaway pool growth.
- **Password rotation.** Treat the role's password like a credential. Rotate with `ALTER ROLE mcp_reader PASSWORD '...'` and update `DATABASE_URL`. The MCP picks up the new value on next start.

## Portability

This pattern is standard SQL, not Postgres-specific. The exact syntax varies slightly:

| Database | Role / user keyword | Notes |
|----------|--------------------|-------|
| **PostgreSQL** | `CREATE ROLE … LOGIN` | What this doc covers |
| **MySQL** / MariaDB | `CREATE USER 'mcp_reader'@'%' IDENTIFIED BY '…'` | Same `GRANT SELECT` semantics, no `ALTER DEFAULT PRIVILEGES` (use repeat-grant scripts) |
| **SQL Server** | `CREATE LOGIN … CREATE USER … FROM LOGIN …` | Two-step (login + user) |
| **Oracle** | `CREATE USER … GRANT CREATE SESSION` | Same view-based redaction works |
| **Amazon RDS / Aurora** | Same as the underlying engine | RDS is just managed Postgres / MySQL / etc |
| **SQLite** | n/a — no user system | Cannot enforce read-only at the DB layer |

For any of the above, the strategy is identical: dedicated role, `GRANT SELECT` only, views for redaction.

## Putting it all together

For a triage / health-check deployment with no PHI to hide:

1. Run the [Minimal setup](#minimal-setup) once.
2. Verify it (see [Verifying the role really is read-only](#verifying-the-role-really-is-read-only)).
3. Set `DATABASE_URL` to the new role and start the MCP.

For deployments with sensitive columns:

1. Do the minimal setup.
2. For each sensitive table, build a view ([Pattern A](#pattern-a--_safe-suffix-explicit-llm-sees-both-names) or [Pattern B](#pattern-b--same-name-hidden-schema-llm-only-sees-the-view-name)) and shift the `GRANT`.
3. Restart nothing — the MCP picks up the new visibility on its next query because grants are evaluated per session.

The MCP code does not change in either case.
