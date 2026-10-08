from typing import Any, Dict, List, Optional, Tuple
import os
import re
import sys
import logging
import json
import atexit
import base64
import threading
from collections import deque
from datetime import datetime, timezone, date, time as dtime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
import signal
import sqlparse
from sqlparse import tokens as T
import time

# Structured logging to stderr
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("database_read")


def log_event(event_type: str, **kwargs):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event_type,
        **kwargs,
    }
    logger.info(json.dumps(entry, default=str))


mcp = FastMCP("database_read")

# Safety configuration
DEFAULT_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "60000"))
DEFAULT_LOCK_TIMEOUT_MS = int(os.getenv("DB_LOCK_TIMEOUT_MS", "15000"))
DEFAULT_IDLE_IN_TXN_TIMEOUT_MS = int(
    os.getenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "60000")
)
DEFAULT_MAX_ROWS = int(os.getenv("DB_MAX_ROWS", "10000"))
DEFAULT_FETCHMANY_SIZE = int(os.getenv("DB_FETCHMANY_SIZE", "1000"))
MAX_STATEMENT_TIMEOUT_MS = int(
    os.getenv(
        "DB_MAX_STATEMENT_TIMEOUT_MS",
        str(max(DEFAULT_STATEMENT_TIMEOUT_MS, 300000)),
    )
)
TOOL_CALLS_PER_MINUTE = int(os.getenv("DB_TOOL_CALLS_PER_MINUTE", "120"))

if DEFAULT_STATEMENT_TIMEOUT_MS <= 0:
    raise ValueError("DB_STATEMENT_TIMEOUT_MS must be > 0")
if DEFAULT_MAX_ROWS <= 0:
    raise ValueError("DB_MAX_ROWS must be > 0")
if MAX_STATEMENT_TIMEOUT_MS < DEFAULT_STATEMENT_TIMEOUT_MS:
    raise ValueError(
        "DB_MAX_STATEMENT_TIMEOUT_MS must be >= DB_STATEMENT_TIMEOUT_MS"
    )
if TOOL_CALLS_PER_MINUTE <= 0:
    raise ValueError("DB_TOOL_CALLS_PER_MINUTE must be > 0")

_READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(readOnlyHint=True)
_TOOL_CALL_TIMESTAMPS = deque()
_TOOL_RATE_LIMIT_LOCK = threading.Lock()


def _enforce_tool_rate_limit() -> None:
    """Apply a process-local sliding-window limit to exposed MCP tool calls."""
    now = time.monotonic()
    cutoff = now - 60.0
    with _TOOL_RATE_LIMIT_LOCK:
        while _TOOL_CALL_TIMESTAMPS and _TOOL_CALL_TIMESTAMPS[0] <= cutoff:
            _TOOL_CALL_TIMESTAMPS.popleft()
        if len(_TOOL_CALL_TIMESTAMPS) >= TOOL_CALLS_PER_MINUTE:
            raise ValueError(
                "Tool call rate limit exceeded; try again after the current "
                "one-minute window"
            )
        _TOOL_CALL_TIMESTAMPS.append(now)

POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "2"))
POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))
POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))


def _parse_allowed_schemas(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw:
        return ("public",)
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or ("public",)


ALLOWED_SCHEMAS: Tuple[str, ...] = _parse_allowed_schemas(os.getenv("DB_ALLOWED_SCHEMAS"))
DEFAULT_SCHEMA = ALLOWED_SCHEMAS[0]

ENV_SELECTOR_VARS = ("DATABASE_TARGET_ENV", "DATABASE_ENV", "DB_ENV")
ENV_ALIAS_MAP = {
    "dev": "local",
    "development": "local",
    "stage": "staging",
    "stg": "staging",
    "prod": "production",
    "production": "production",
    "local": "local",
    "staging": "staging",
    "default": "default",
}
DATABASE_URL_PREFIX = "DATABASE_URL_"


def _normalize_env_name(env_name: Optional[str]) -> str:
    if not env_name:
        return "default"
    cleaned = env_name.strip().lower()
    if not cleaned:
        return "default"
    return ENV_ALIAS_MAP.get(cleaned, cleaned)


def _discover_database_urls(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build map of envs → URLs from environment variables."""
    env = environ if environ is not None else os.environ
    urls: Dict[str, str] = {}
    default_url = env.get("DATABASE_URL")
    if default_url:
        urls["default"] = default_url
    for key, value in env.items():
        if not key.startswith(DATABASE_URL_PREFIX) or not value:
            continue
        suffix = key[len(DATABASE_URL_PREFIX):].strip()
        if not suffix:
            continue
        urls[_normalize_env_name(suffix)] = value
    return urls


DATABASE_URLS: Dict[str, str] = _discover_database_urls()
_ENGINE_CACHE: Dict[str, Engine] = {}
_ENGINE_LOCK = threading.Lock()


def _available_envs_description() -> str:
    if not DATABASE_URLS:
        return "none configured"
    return ", ".join(sorted(DATABASE_URLS.keys()))


def _resolve_requested_environment(requested_env: Optional[str]) -> str:
    env_candidate = requested_env
    if not env_candidate:
        for var in ENV_SELECTOR_VARS:
            if os.getenv(var):
                env_candidate = os.getenv(var)
                break
    return _normalize_env_name(env_candidate)


def _create_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        connect_args={
            "application_name": "mcp_read_only",
            "options": (
                f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={DEFAULT_LOCK_TIMEOUT_MS} "
                f"-c idle_in_transaction_session_timeout={DEFAULT_IDLE_IN_TXN_TIMEOUT_MS}"
            ),
        },
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
        pool_recycle=POOL_RECYCLE,
    )


def _get_engine(requested_env: Optional[str] = None) -> Engine:
    target_env = _resolve_requested_environment(requested_env)
    database_url = DATABASE_URLS.get(target_env)
    if database_url is None:
        raise ValueError(
            f"No database URL for environment '{target_env}'. "
            f"Available: {_available_envs_description()}. "
            f"Set DATABASE_URL_{target_env.upper()} or check spelling."
        )
    with _ENGINE_LOCK:
        if target_env not in _ENGINE_CACHE:
            _ENGINE_CACHE[target_env] = _create_engine(database_url)
        return _ENGINE_CACHE[target_env]


def _dispose_all_engines() -> None:
    with _ENGINE_LOCK:
        for env_name, engine in list(_ENGINE_CACHE.items()):
            try:
                engine.dispose()
            except Exception:
                pass
            _ENGINE_CACHE.pop(env_name, None)


atexit.register(_dispose_all_engines)


# SQL safety: parse-based validation
_BLOCKED_DML = {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "UPSERT"}
# Keywords that indicate state change even though they aren't classified
# as DDL or write-DML by sqlparse. Walk every token and reject these
# whenever the statement starts with SELECT/WITH.
#   - INTO: traps `SELECT * INTO new_table FROM ...` (creates a table)
#   - FOR UPDATE / FOR SHARE: row-level locks (state change on rows)
_BLOCKED_ANY = {"INTO"}

# Function calls that mutate server state, signal sessions, touch the
# filesystem, or escape into another database. PG's READ ONLY transaction
# does not block these because they're function calls, not DML. So we
# refuse them at parse time. The detection is structural: a name token
# (case-insensitive, with quotes stripped) followed by `(` inside the
# top-level statement. Identifiers, columns, and matching substrings
# inside string literals are intentionally NOT matched.
_DANGEROUS_FUNCS = {
    # Backend signaling
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_signal_backend",
    # Server-level admin
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    "pg_create_restore_point",
    "pg_switch_wal",
    "pg_switch_xlog",
    "pg_replication_origin_create",
    "pg_replication_origin_drop",
    # Filesystem access
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    # Large object I/O
    "lo_creat", "lo_create", "lo_unlink", "lo_import", "lo_export",
    "lo_open", "lo_close", "lo_read", "lo_write", "lo_truncate",
    "lo_put", "lo_get", "lo_from_bytea",
    # Cross-DB escapes
    "dblink", "dblink_exec", "dblink_connect", "dblink_disconnect",
    "dblink_open", "dblink_fetch", "dblink_close",
    # Config flip
    "set_config",
    # Snapshot export
    "pg_export_snapshot",
}

_TRAILING_SEMI_RE = re.compile(r";\s*$")
_RESERVED_QUERY_PARAMS = {"_row_limit", "_row_offset"}


def _normalize_ident_value(value: str) -> str:
    """Strip surrounding double-quotes and lowercase a name-like token."""
    v = value.strip()
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1].replace('""', '"')
    return v.lower()


def _is_skippable(tok) -> bool:
    if tok.is_whitespace:
        return True
    if tok.ttype in (T.Comment, T.Comment.Single, T.Comment.Multiline):
        return True
    return False


def _check_dangerous_function_calls(stmt) -> None:
    """Walk tokens; reject `<dangerous_name> (` patterns. Skips strings/numbers."""
    toks = [t for t in stmt.flatten() if not _is_skippable(t)]
    for i, tok in enumerate(toks):
        # Skip string literals + numbers + punctuation.
        # NOTE: T.String.Symbol is sqlparse's tag for double-quoted IDENTIFIERS
        # (e.g. "pg_terminate_backend"), so we do NOT skip it — we want those
        # checked against the blacklist after dequoting.
        if tok.ttype in (
            T.String, T.String.Single,
            T.Number, T.Number.Integer, T.Number.Float,
            T.Punctuation,
        ):
            continue
        name = _normalize_ident_value(tok.value)
        if name not in _DANGEROUS_FUNCS:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is not None and nxt.ttype is T.Punctuation and nxt.value == "(":
            raise ValueError(
                f"Dangerous function '{name}' not allowed in read-only query"
            )


def _strip_trailing_semicolon(sql: str) -> str:
    return _TRAILING_SEMI_RE.sub("", sql)


def validate_read_only_sql(sql: str) -> None:
    """
    Reject anything that is not a single read-only SELECT/WITH...SELECT.
    Raises ValueError on violation.
    """
    if not sql or not sql.strip():
        raise ValueError("Empty query")

    cleaned = sqlparse.format(sql, strip_comments=True).strip()
    cleaned = _strip_trailing_semicolon(cleaned).strip()
    if not cleaned:
        raise ValueError("Empty query")

    parsed = [s for s in sqlparse.parse(cleaned) if str(s).strip()]
    if len(parsed) != 1:
        raise ValueError(
            "Multiple statements not allowed; submit one query at a time"
        )
    stmt = parsed[0]

    first_kw = None
    for tok in stmt.flatten():
        if tok.is_whitespace:
            continue
        if tok.ttype in (T.Comment, T.Comment.Single, T.Comment.Multiline):
            continue
        if tok.ttype is T.Punctuation:
            continue
        first_kw = tok.normalized.upper()
        break

    if first_kw not in ("SELECT", "WITH"):
        raise ValueError(
            f"Only SELECT/WITH queries allowed (got '{first_kw}')"
        )

    for tok in stmt.flatten():
        if tok.ttype is T.Keyword.DDL:
            raise ValueError(
                f"DDL '{tok.normalized.upper()}' not allowed"
            )
        if tok.ttype is T.Keyword.DML:
            op = tok.normalized.upper()
            if op in _BLOCKED_DML:
                raise ValueError(f"Write operation '{op}' not allowed")
        if tok.ttype is T.Keyword:
            kw = tok.normalized.upper()
            if kw in _BLOCKED_ANY:
                raise ValueError(
                    f"Disallowed keyword '{kw}' in read-only query"
                )

    _check_dangerous_function_calls(stmt)


def _wrap_select_with_limit_offset(query: str) -> str:
    """Wrap query with parameterized LIMIT/OFFSET. Values bound separately."""
    inner = _strip_trailing_semicolon(query)
    return (
        f"SELECT * FROM ({inner}) AS _mcp_sub "
        f"LIMIT :_row_limit OFFSET :_row_offset"
    )


# Value serialization for JSON-safe MCP responses
def _jsonify_value(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, (list, tuple)):
        return [_jsonify_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonify_value(val) for k, val in v.items()}
    if isinstance(v, set):
        return [_jsonify_value(x) for x in v]
    return str(v)


def _jsonify_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _jsonify_value(v) for k, v in row.items()}


class QueryCancelled(Exception):
    pass


def _can_install_signal_handlers() -> bool:
    """Signals can only be installed from the main thread of the main interpreter."""
    return threading.current_thread() is threading.main_thread()


def _install_cancellation_handlers():
    if not _can_install_signal_handlers():
        return None, None, False
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)

    def _cancel(_signum, _frame):
        raise QueryCancelled("Operation cancelled by signal")

    signal.signal(signal.SIGINT, _cancel)
    signal.signal(signal.SIGTERM, _cancel)
    return prev_int, prev_term, True


def _restore_signal_handlers(prev_int, prev_term, installed):
    if not installed:
        return
    signal.signal(signal.SIGINT, prev_int)
    signal.signal(signal.SIGTERM, prev_term)


def _attempt_cancel(connection) -> None:
    try:
        dbapi_conn = getattr(getattr(connection, "connection", None), "connection", None)
        if dbapi_conn and hasattr(dbapi_conn, "cancel"):
            dbapi_conn.cancel()
    except Exception:
        pass


def _validate_schema(schema: Optional[str]) -> str:
    target = schema or DEFAULT_SCHEMA
    if target not in ALLOWED_SCHEMAS:
        raise ValueError(
            f"Schema '{target}' not in allowlist {list(ALLOWED_SCHEMAS)}. "
            f"Set DB_ALLOWED_SCHEMAS to extend."
        )
    return target


def _quote_ident(ident: str) -> str:
    """Conservative PostgreSQL identifier quoting. Rejects identifiers with `\"` to prevent injection."""
    if not isinstance(ident, str) or not ident:
        raise ValueError("Invalid identifier")
    if '"' in ident or "\x00" in ident:
        raise ValueError(f"Invalid identifier: {ident!r}")
    return '"' + ident + '"'


def _effective_max_rows(requested: Optional[int]) -> int:
    """Validate a requested row count and enforce DB_MAX_ROWS as a hard cap."""
    if requested is None:
        return DEFAULT_MAX_ROWS
    if requested <= 0:
        raise ValueError("max_rows must be > 0")
    return min(requested, DEFAULT_MAX_ROWS)


def _effective_statement_timeout(requested: Optional[int]) -> int:
    """Validate a per-query timeout against the configured hard ceiling."""
    timeout_ms = (
        requested if requested is not None else DEFAULT_STATEMENT_TIMEOUT_MS
    )
    if timeout_ms <= 0:
        raise ValueError("statement_timeout_ms must be > 0")
    if timeout_ms > MAX_STATEMENT_TIMEOUT_MS:
        raise ValueError(
            "statement_timeout_ms must be <= "
            f"DB_MAX_STATEMENT_TIMEOUT_MS ({MAX_STATEMENT_TIMEOUT_MS})"
        )
    return timeout_ms


def execute_query(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    environment: Optional[str] = None,
    max_rows: Optional[int] = None,
    offset: int = 0,
    statement_timeout_ms: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Execute a SELECT/WITH query. Returns (rows, truncated).
    truncated == True when result hit max_rows ceiling.
    """
    validate_read_only_sql(query)

    if offset < 0:
        raise ValueError("offset must be >= 0")

    effective_max_rows = _effective_max_rows(max_rows)
    effective_timeout_ms = _effective_statement_timeout(statement_timeout_ms)

    exec_params = dict(params or {})
    reserved_params = sorted(_RESERVED_QUERY_PARAMS.intersection(exec_params))
    if reserved_params:
        raise ValueError(
            "Query parameter names are reserved by the server: "
            + ", ".join(reserved_params)
        )

    engine = _get_engine(environment)
    target_env = _resolve_requested_environment(environment)
    start_time = time.monotonic()

    # Fetch max_rows+1 to detect truncation
    fetch_limit = effective_max_rows + 1
    safe_query = _wrap_select_with_limit_offset(query)
    exec_params["_row_limit"] = fetch_limit
    exec_params["_row_offset"] = offset

    prev_int, prev_term, installed = _install_cancellation_handlers()

    with engine.connect() as connection:
        trans = connection.begin()
        result = None
        try:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(
                text("SET LOCAL statement_timeout = :t"),
                {"t": int(effective_timeout_ms)},
            )
            connection.execute(
                text("SET LOCAL lock_timeout = :t"),
                {"t": int(DEFAULT_LOCK_TIMEOUT_MS)},
            )
            connection.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = :t"),
                {"t": int(DEFAULT_IDLE_IN_TXN_TIMEOUT_MS)},
            )

            result = connection.execution_options(stream_results=True).execute(
                text(safe_query), exec_params
            )

            rows: List[Dict[str, Any]] = []
            batch_size = max(1, DEFAULT_FETCHMANY_SIZE)
            deadline = time.monotonic() + (int(effective_timeout_ms) / 1000.0)

            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Client-side timeout exceeded while fetching results")
                batch = result.mappings().fetchmany(batch_size)
                if not batch:
                    break
                for row in batch:
                    rows.append(_jsonify_row(dict(row)))
                    if len(rows) >= fetch_limit:
                        break
                if len(rows) >= fetch_limit:
                    break

            trans.commit()

            truncated = len(rows) > effective_max_rows
            if truncated:
                rows = rows[:effective_max_rows]

            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            log_event(
                "query_executed",
                environment=target_env,
                duration_ms=elapsed_ms,
                row_count=len(rows),
                truncated=truncated,
                query_length=len(query),
            )
            return rows, truncated
        except (QueryCancelled, TimeoutError) as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            log_event(
                "query_failed",
                environment=target_env,
                duration_ms=elapsed_ms,
                error_type=type(e).__name__,
                query_length=len(query),
            )
            try:
                _attempt_cancel(connection)
            finally:
                trans.rollback()
            raise
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            log_event(
                "query_failed",
                environment=target_env,
                duration_ms=elapsed_ms,
                error_type=type(e).__name__,
                query_length=len(query),
            )
            trans.rollback()
            raise
        finally:
            try:
                if result is not None:
                    result.close()
            finally:
                _restore_signal_handlers(prev_int, prev_term, installed)


def get_table_names(*, environment: Optional[str] = None, schema: Optional[str] = None) -> List[str]:
    target_schema = _validate_schema(schema)
    rows, _ = execute_query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = :s ORDER BY table_name",
        {"s": target_schema},
        environment=environment,
    )
    return [row["table_name"] for row in rows]


def get_table_schema(
    table_name: str, *, environment: Optional[str] = None, schema: Optional[str] = None
) -> List[Dict[str, Any]]:
    target_schema = _validate_schema(schema)
    rows, _ = execute_query(
        """
        SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t
        ORDER BY ordinal_position
        """,
        {"s": target_schema, "t": table_name},
        environment=environment,
    )
    return rows


def get_primary_keys(
    table_name: str, *, environment: Optional[str] = None, schema: Optional[str] = None
) -> List[str]:
    target_schema = _validate_schema(schema)
    rows, _ = execute_query(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = :s
            AND tc.table_name = :t
        ORDER BY kcu.ordinal_position
        """,
        {"s": target_schema, "t": table_name},
        environment=environment,
    )
    return [row["column_name"] for row in rows]


# ---------- MCP Tools ----------


@mcp.tool("health_check", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_health_check(environment: Optional[str] = None) -> Dict[str, Any]:
    """Check database connectivity and server health."""
    target_env = _resolve_requested_environment(environment)
    try:
        _enforce_tool_rate_limit()
        engine = _get_engine(environment)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).fetchone()
        return {
            "status": "healthy",
            "environment": target_env,
            "available_environments": sorted(DATABASE_URLS.keys()),
            "pool_size": POOL_SIZE,
            "statement_timeout_ms": DEFAULT_STATEMENT_TIMEOUT_MS,
            "max_statement_timeout_ms": MAX_STATEMENT_TIMEOUT_MS,
            "tool_calls_per_minute": TOOL_CALLS_PER_MINUTE,
            "max_rows": DEFAULT_MAX_ROWS,
            "allowed_schemas": list(ALLOWED_SCHEMAS),
        }
    except Exception as e:
        return {"status": "unhealthy", "environment": target_env, "error": str(e)}


@mcp.tool("database_query", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_database_query(
    query: str,
    environment: Optional[str] = None,
    max_rows: Optional[int] = None,
    offset: int = 0,
    statement_timeout_ms: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a read-only SQL query (SELECT/WITH only), with optional bind params."""
    try:
        _enforce_tool_rate_limit()
        effective_max = _effective_max_rows(max_rows)
        effective_timeout = _effective_statement_timeout(statement_timeout_ms)
        rows, truncated = execute_query(
            query,
            params=params,
            environment=environment,
            max_rows=effective_max,
            offset=offset,
            statement_timeout_ms=effective_timeout,
        )
        return {
            "status": "success",
            "results": rows,
            "count": len(rows),
            "truncated": truncated,
            "offset": offset,
            "max_rows": effective_max,
            "statement_timeout_ms": effective_timeout,
            "environment": _resolve_requested_environment(environment),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool("explain_query", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_explain_query(
    query: str,
    analyze: bool = False,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    EXPLAIN [ANALYZE] a query. analyze=True runs query for real timings.
    Always read-only; validator rejects writes.
    """
    try:
        _enforce_tool_rate_limit()
        validate_read_only_sql(query)
        inner = _strip_trailing_semicolon(query)
        prefix = "EXPLAIN (FORMAT JSON, ANALYZE)" if analyze else "EXPLAIN (FORMAT JSON)"
        explain_sql = f"{prefix} {inner}"

        engine = _get_engine(environment)
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(
                    text("SET LOCAL statement_timeout = :t"),
                    {"t": DEFAULT_STATEMENT_TIMEOUT_MS},
                )
                result = conn.execute(text(explain_sql))
                rows = result.fetchall()
                plan = [_jsonify_value(r[0]) for r in rows]
                trans.commit()
                return {
                    "status": "success",
                    "analyze": analyze,
                    "plan": plan,
                    "environment": _resolve_requested_environment(environment),
                }
            except Exception:
                trans.rollback()
                raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool("list_tables", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_list_tables(
    environment: Optional[str] = None, schema: Optional[str] = None
) -> Dict[str, Any]:
    """List tables in the given schema (defaults to first allowed schema)."""
    try:
        _enforce_tool_rate_limit()
        target_schema = _validate_schema(schema)
        tables = get_table_names(environment=environment, schema=target_schema)
        return {
            "status": "success",
            "schema": target_schema,
            "tables": tables,
            "count": len(tables),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool("get_table_schema", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_get_table_schema(
    table_name: str,
    environment: Optional[str] = None,
    schema: Optional[str] = None,
) -> Dict[str, Any]:
    """Get column metadata + primary keys for a table."""
    try:
        _enforce_tool_rate_limit()
        target_schema = _validate_schema(schema)
        cols = get_table_schema(table_name, environment=environment, schema=target_schema)
        pks = get_primary_keys(table_name, environment=environment, schema=target_schema)
        return {
            "status": "success",
            "schema": target_schema,
            "table": table_name,
            "columns": cols,
            "primary_keys": pks,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool("get_all_schemas", annotations=_READ_ONLY_TOOL_ANNOTATIONS)
def handle_get_all_schemas(
    environment: Optional[str] = None,
    schema: Optional[str] = None,
    include_samples: bool = False,
    sample_rows: int = 5,
) -> Dict[str, Any]:
    """
    Get column + PK info for every table in the schema in 2 queries.
    Optionally include LIMIT-N samples (one query per table; set include_samples=False to skip).
    """
    try:
        _enforce_tool_rate_limit()
        target_schema = _validate_schema(schema)

        cols, _ = execute_query(
            """
            SELECT table_name, column_name, data_type, is_nullable,
                   column_default, character_maximum_length, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = :s
            ORDER BY table_name, ordinal_position
            """,
            {"s": target_schema},
            environment=environment,
        )

        pks, _ = execute_query(
            """
            SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = :s
            ORDER BY tc.table_name, kcu.ordinal_position
            """,
            {"s": target_schema},
            environment=environment,
        )

        schemas: Dict[str, Dict[str, Any]] = {}
        for c in cols:
            t = c["table_name"]
            schemas.setdefault(t, {"columns": [], "primary_keys": []})
            schemas[t]["columns"].append(
                {k: v for k, v in c.items() if k != "table_name"}
            )
        for p in pks:
            t = p["table_name"]
            schemas.setdefault(t, {"columns": [], "primary_keys": []})
            schemas[t]["primary_keys"].append(p["column_name"])

        if include_samples:
            if sample_rows <= 0 or sample_rows > 100:
                raise ValueError("sample_rows must be in (0, 100]")
            qschema = _quote_ident(target_schema)
            for table_name in list(schemas.keys()):
                try:
                    qtable = _quote_ident(table_name)
                    sample, _ = execute_query(
                        f"SELECT * FROM {qschema}.{qtable}",
                        environment=environment,
                        max_rows=sample_rows,
                    )
                    schemas[table_name]["sample_data"] = sample
                except Exception:
                    schemas[table_name]["sample_data"] = []

        return {
            "status": "success",
            "schema": target_schema,
            "table_count": len(schemas),
            "schemas": schemas,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _startup_check_or_exit() -> None:
    if not DATABASE_URLS:
        log_event("startup_failed", reason="no_database_urls")
        print(
            "ERROR: No database URLs configured.\n"
            "Set DATABASE_URL or DATABASE_URL_<ENV> environment variables.\n"
            "Example: DATABASE_URL_LOCAL=postgresql://user:pass@localhost:5432/db",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    _startup_check_or_exit()
    log_event(
        "server_starting",
        environments=sorted(DATABASE_URLS.keys()),
        statement_timeout_ms=DEFAULT_STATEMENT_TIMEOUT_MS,
        max_statement_timeout_ms=MAX_STATEMENT_TIMEOUT_MS,
        tool_calls_per_minute=TOOL_CALLS_PER_MINUTE,
        max_rows=DEFAULT_MAX_ROWS,
        pool_size=POOL_SIZE,
        allowed_schemas=list(ALLOWED_SCHEMAS),
    )
    print("Starting Database Read MCP Server...", file=sys.stderr, flush=True)
    print("Available tools:", file=sys.stderr, flush=True)
    for tool in (
        "health_check",
        "database_query",
        "explain_query",
        "list_tables",
        "get_table_schema",
        "get_all_schemas",
    ):
        print(f"  - {tool}", file=sys.stderr, flush=True)
    mcp.run(transport="stdio")
