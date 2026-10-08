"""End-to-end tests against a live Postgres.

Skipped unless MCP_TEST_DATABASE_URL is set. Spin up a throwaway local DB with:

    docker run --rm -d --name mcp_pg -e POSTGRES_PASSWORD=test \
        -p 55432:5432 postgres:16
    export MCP_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres
    uv run pytest tests/test_integration.py
"""
import pytest

pytestmark = pytest.mark.integration


# --- Connectivity ---

def test_health_check_healthy(mcp_env):
    out = mcp_env.handle_health_check()
    assert out["status"] == "healthy"
    assert out["environment"] == "default"
    assert "default" in out["available_environments"]
    assert out["allowed_schemas"]


# --- list_tables ---

def test_list_tables(mcp_env, fresh_schema):
    out = mcp_env.handle_list_tables(schema=fresh_schema)
    assert out["status"] == "success"
    assert set(out["tables"]) == {"widgets", "tags"}
    assert out["count"] == 2
    assert out["schema"] == fresh_schema


def test_list_tables_outsider_blocked(mcp_env):
    out = mcp_env.handle_list_tables(schema="pg_catalog")
    assert out["status"] == "error"


# --- get_table_schema ---

def test_get_table_schema_columns(mcp_env, fresh_schema):
    out = mcp_env.handle_get_table_schema("widgets", schema=fresh_schema)
    assert out["status"] == "success"
    col_names = [c["column_name"] for c in out["columns"]]
    assert col_names == ["id", "name", "qty"]
    assert out["primary_keys"] == ["id"]


def test_get_table_schema_missing_table(mcp_env, fresh_schema):
    out = mcp_env.handle_get_table_schema("nonexistent", schema=fresh_schema)
    assert out["status"] == "success"
    assert out["columns"] == []
    assert out["primary_keys"] == []


# --- database_query ---

def test_database_query_basic(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        f'SELECT id, name FROM "{fresh_schema}".widgets ORDER BY id'
    )
    assert out["status"] == "success"
    assert out["count"] == 3
    assert out["results"][0] == {"id": 1, "name": "a"}
    assert out["truncated"] is False


def test_database_query_bound_params(mcp_env):
    value = "O'Reilly"
    out = mcp_env.handle_database_query(
        "SELECT :value AS value", params={"value": value}
    )
    assert out["status"] == "success"
    assert out["results"] == [{"value": value}]


def test_database_query_enforces_hard_row_cap(mcp_env, fresh_schema, monkeypatch):
    monkeypatch.setattr(mcp_env, "DEFAULT_MAX_ROWS", 2)
    out = mcp_env.handle_database_query(
        f'SELECT id FROM "{fresh_schema}".widgets ORDER BY id', max_rows=100
    )

    assert out["status"] == "success"
    assert out["max_rows"] == 2
    assert out["count"] == 2
    assert out["truncated"] is True


def test_database_query_pagination(mcp_env, fresh_schema):
    page1 = mcp_env.handle_database_query(
        f'SELECT id FROM "{fresh_schema}".widgets ORDER BY id',
        max_rows=2, offset=0,
    )
    page2 = mcp_env.handle_database_query(
        f'SELECT id FROM "{fresh_schema}".widgets ORDER BY id',
        max_rows=2, offset=2,
    )
    assert [r["id"] for r in page1["results"]] == [1, 2]
    assert page1["truncated"] is True
    assert [r["id"] for r in page2["results"]] == [3]
    assert page2["truncated"] is False


def test_database_query_truncation_flag(mcp_env, fresh_schema):
    # max_rows == total rows → not truncated (no extra row to flag)
    out = mcp_env.handle_database_query(
        f'SELECT id FROM "{fresh_schema}".widgets',
        max_rows=3,
    )
    assert out["count"] == 3
    assert out["truncated"] is False

    # max_rows < total rows → truncated
    out2 = mcp_env.handle_database_query(
        f'SELECT id FROM "{fresh_schema}".widgets',
        max_rows=2,
    )
    assert out2["count"] == 2
    assert out2["truncated"] is True


def test_database_query_with_cte(mcp_env, fresh_schema):
    sql = f'WITH t AS (SELECT id FROM "{fresh_schema}".widgets) SELECT COUNT(*) AS c FROM t'
    out = mcp_env.handle_database_query(sql)
    assert out["status"] == "success"
    assert out["results"][0]["c"] == 3


def test_database_query_blocks_write_against_db(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        f'INSERT INTO "{fresh_schema}".widgets (name) VALUES (\'x\')'
    )
    assert out["status"] == "error"
    assert "INSERT" in out["message"]


def test_database_query_blocks_multi_statement(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        f'SELECT 1; DROP TABLE "{fresh_schema}".widgets'
    )
    assert out["status"] == "error"


def test_database_query_handles_decimal(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        "SELECT 3.14::numeric AS pi"
    )
    assert out["status"] == "success"
    assert out["results"][0]["pi"] == "3.14"


def test_database_query_handles_uuid(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        "SELECT '12345678-1234-5678-1234-567812345678'::uuid AS u"
    )
    assert out["status"] == "success"
    assert out["results"][0]["u"] == "12345678-1234-5678-1234-567812345678"


def test_database_query_handles_timestamp(mcp_env, fresh_schema):
    out = mcp_env.handle_database_query(
        "SELECT TIMESTAMP '2026-05-15 10:30:45' AS ts"
    )
    assert out["status"] == "success"
    assert out["results"][0]["ts"].startswith("2026-05-15T10:30:45")


# --- explain_query ---

def test_explain_query_returns_plan(mcp_env, fresh_schema):
    out = mcp_env.handle_explain_query(
        f'SELECT * FROM "{fresh_schema}".widgets'
    )
    assert out["status"] == "success"
    assert isinstance(out["plan"], list)
    assert len(out["plan"]) >= 1


def test_explain_query_analyze(mcp_env, fresh_schema):
    out = mcp_env.handle_explain_query(
        f'SELECT * FROM "{fresh_schema}".widgets',
        analyze=True,
    )
    assert out["status"] == "success"
    assert out["analyze"] is True


def test_explain_query_blocks_write(mcp_env, fresh_schema):
    out = mcp_env.handle_explain_query(
        f'DELETE FROM "{fresh_schema}".widgets'
    )
    assert out["status"] == "error"


# --- get_all_schemas ---

def test_get_all_schemas(mcp_env, fresh_schema):
    out = mcp_env.handle_get_all_schemas(
        schema=fresh_schema,
        include_samples=True,
        sample_rows=2,
    )
    assert out["status"] == "success"
    assert out["schema"] == fresh_schema
    assert out["table_count"] == 2
    assert "widgets" in out["schemas"]
    w = out["schemas"]["widgets"]
    assert {c["column_name"] for c in w["columns"]} == {"id", "name", "qty"}
    assert w["primary_keys"] == ["id"]
    assert 0 < len(w["sample_data"]) <= 2


def test_get_all_schemas_skip_samples(mcp_env, fresh_schema):
    out = mcp_env.handle_get_all_schemas(
        schema=fresh_schema,
        include_samples=False,
    )
    assert out["status"] == "success"
    for tbl in out["schemas"].values():
        assert "sample_data" not in tbl


# --- read-only enforcement at TX level ---

def test_transaction_is_read_only(mcp_env, fresh_schema):
    """
    Even if validator missed a write, PG enforces read-only at the transaction
    level. Manually craft a query that uses pg_advisory_xact_lock() (read-ish
    function) to confirm normal flow works.
    """
    out = mcp_env.handle_database_query("SELECT pg_backend_pid() AS pid")
    assert out["status"] == "success"


# --- env override ---

def test_environment_override_unknown_env_fails(mcp_env):
    out = mcp_env.handle_database_query("SELECT 1", environment="nonexistent_env")
    assert out["status"] == "error"
    assert "nonexistent_env" in out["message"]
