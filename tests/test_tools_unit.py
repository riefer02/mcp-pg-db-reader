"""MCP tool behaviors that don't need a live DB — error paths + validation paths.

Engine connect() is monkey-patched to raise so we exercise validator + error
surfacing without hitting Postgres.
"""
import asyncio
from collections import deque
import pytest
import database_read as db


@pytest.fixture(autouse=True)
def _disable_engine(monkeypatch):
    """Force any engine-requiring path to surface a known error."""
    def boom(*a, **kw):
        raise RuntimeError("engine call attempted (test should not reach DB)")
    monkeypatch.setattr(db, "_get_engine", boom)


# --- database_query rejects writes BEFORE touching the engine ---

def test_database_query_blocks_insert_without_db():
    out = db.handle_database_query("INSERT INTO users VALUES (1)")
    assert out["status"] == "error"
    assert "INSERT" in out["message"]


def test_database_query_blocks_drop_without_db():
    out = db.handle_database_query("DROP TABLE users")
    assert out["status"] == "error"
    assert "DROP" in out["message"]


def test_database_query_blocks_multi_statement_without_db():
    out = db.handle_database_query("SELECT 1; DROP TABLE x")
    assert out["status"] == "error"
    assert "Multiple statements" in out["message"]


def test_database_query_rejects_negative_offset():
    out = db.handle_database_query("SELECT 1", offset=-1)
    assert out["status"] == "error"
    assert "offset" in out["message"]


def test_database_query_rejects_zero_max_rows():
    out = db.handle_database_query("SELECT 1", max_rows=0)
    assert out["status"] == "error"
    assert "max_rows" in out["message"]


def test_database_query_forwards_bound_params(monkeypatch):
    captured = {}

    def fake_execute_query(query, params=None, **kwargs):
        captured.update(query=query, params=params, **kwargs)
        return [{"value": "bound value"}], False

    monkeypatch.setattr(db, "execute_query", fake_execute_query)
    content, structured = asyncio.run(
        db.mcp.call_tool(
            "database_query",
            {"query": "SELECT :value AS value", "params": {"value": "bound value"}},
        )
    )

    assert structured["result"]["status"] == "success"
    assert structured["result"]["results"] == [{"value": "bound value"}]
    assert "bound value" in content[0].text
    assert captured["query"] == "SELECT :value AS value"
    assert captured["params"] == {"value": "bound value"}


def test_database_query_clamps_max_rows_to_hard_cap(monkeypatch):
    captured = {}

    def fake_execute_query(query, **kwargs):
        captured.update(kwargs)
        return [], False

    monkeypatch.setattr(db, "execute_query", fake_execute_query)
    out = db.handle_database_query("SELECT 1", max_rows=db.DEFAULT_MAX_ROWS + 1)

    assert out["status"] == "success"
    assert captured["max_rows"] == db.DEFAULT_MAX_ROWS
    assert out["max_rows"] == db.DEFAULT_MAX_ROWS


def test_mcp_tool_calls_are_rate_limited(monkeypatch):
    monkeypatch.setattr(db, "TOOL_CALLS_PER_MINUTE", 1)
    monkeypatch.setattr(db, "_TOOL_CALL_TIMESTAMPS", deque())
    monkeypatch.setattr(db, "execute_query", lambda _query, **_kwargs: ([], False))

    first = db.handle_database_query("SELECT 1")
    second = db.handle_database_query("SELECT 1")

    assert first["status"] == "success"
    assert second["status"] == "error"
    assert "rate limit exceeded" in second["message"]


@pytest.mark.parametrize(
    "timeout_ms,expected_message",
    [
        (0, "must be > 0"),
        (-1, "must be > 0"),
        (db.MAX_STATEMENT_TIMEOUT_MS + 1, "DB_MAX_STATEMENT_TIMEOUT_MS"),
    ],
)
def test_database_query_rejects_out_of_range_timeout(timeout_ms, expected_message):
    out = db.handle_database_query("SELECT 1", statement_timeout_ms=timeout_ms)

    assert out["status"] == "error"
    assert expected_message in out["message"]


def test_execute_query_rejects_reserved_bind_params():
    with pytest.raises(ValueError, match="reserved by the server.*_row_limit"):
        db.execute_query("SELECT :_row_limit", params={"_row_limit": 1})


def test_get_all_schemas_omits_samples_by_default(monkeypatch):
    def fake_execute_query(query, params=None, **kwargs):
        if "information_schema.columns" in query:
            return ([{
                "table_name": "users",
                "column_name": "id",
                "data_type": "integer",
                "is_nullable": "NO",
                "column_default": None,
                "character_maximum_length": None,
                "ordinal_position": 1,
            }], False)
        if "information_schema.table_constraints" in query:
            return ([{"table_name": "users", "column_name": "id", "ordinal_position": 1}], False)
        raise AssertionError("sample query should not run by default")

    monkeypatch.setattr(db, "execute_query", fake_execute_query)
    out = db.handle_get_all_schemas()

    assert out["status"] == "success"
    assert "sample_data" not in out["schemas"]["users"]


@pytest.mark.parametrize("fail", [False, True])
def test_query_logs_do_not_contain_sql_or_driver_error(monkeypatch, fail):
    secret = "private@example.test"
    events = []

    class FakeResult:
        def mappings(self):
            return self

        def fetchmany(self, _size):
            return []

        def close(self):
            pass

    class FakeTransaction:
        def commit(self):
            pass

        def rollback(self):
            pass

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def begin(self):
            return FakeTransaction()

        def execute(self, statement, _params=None):
            if fail and str(statement).startswith("SELECT * FROM ("):
                raise RuntimeError(f"driver rejected value {secret}")
            return FakeResult()

        def execution_options(self, **_kwargs):
            return self

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(db, "_get_engine", lambda _environment=None: FakeEngine())
    monkeypatch.setattr(
        db, "log_event", lambda event_type, **kwargs: events.append({"event": event_type, **kwargs})
    )
    query = f"SELECT '{secret}' AS email"

    if fail:
        with pytest.raises(RuntimeError):
            db.execute_query(query)
    else:
        db.execute_query(query)

    assert secret not in repr(events)
    assert all("query_preview" not in event for event in events)
    assert all("error_message" not in event for event in events)


def test_all_mcp_tools_advertise_read_only_annotation():
    tools = asyncio.run(db.mcp.list_tools())

    assert {tool.name for tool in tools} == {
        "health_check",
        "database_query",
        "explain_query",
        "list_tables",
        "get_table_schema",
        "get_all_schemas",
    }
    assert all(tool.annotations.readOnlyHint is True for tool in tools)
    assert all(tool.outputSchema is not None for tool in tools)
    database_query = next(tool for tool in tools if tool.name == "database_query")
    assert "params" in database_query.inputSchema["properties"]


# --- explain_query rejects writes ---

def test_explain_query_blocks_write():
    out = db.handle_explain_query("DELETE FROM users")
    assert out["status"] == "error"
    assert "DELETE" in out["message"]


# --- schema allowlist ---

def test_list_tables_rejects_outsider_schema():
    out = db.handle_list_tables(schema="information_schema")
    assert out["status"] == "error"
    assert "not in allowlist" in out["message"]


def test_get_table_schema_rejects_outsider_schema():
    out = db.handle_get_table_schema("users", schema="pg_catalog")
    assert out["status"] == "error"
    assert "not in allowlist" in out["message"]


def test_get_all_schemas_rejects_outsider_schema():
    out = db.handle_get_all_schemas(schema="information_schema")
    assert out["status"] == "error"
    assert "not in allowlist" in out["message"]
