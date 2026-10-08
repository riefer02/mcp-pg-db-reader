"""Shared test fixtures + path setup.

Tests live under tests/, source lives at repo root. Prepend repo root to sys.path
so `import database_read` works regardless of where pytest is invoked.

Integration tests require a real Postgres reachable via MCP_TEST_DATABASE_URL.
Without that env var they are skipped automatically.
"""
import os
import sys
import pathlib
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Set a placeholder DATABASE_URL early so importing database_read in unit tests
# doesn't trip on startup-check semantics. The startup exit only runs under
# __main__, but we still want DATABASE_URLS populated for cache tests.
os.environ.setdefault("DATABASE_URL", "postgresql://placeholder:placeholder@localhost:5432/placeholder")


@pytest.fixture(autouse=True)
def reset_tool_rate_limit():
    """Keep rate-limit state isolated between tests."""
    import database_read as db

    with db._TOOL_RATE_LIMIT_LOCK:
        db._TOOL_CALL_TIMESTAMPS.clear()


def _integration_url() -> str | None:
    return os.environ.get("MCP_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def integration_url() -> str:
    url = _integration_url()
    if not url:
        pytest.skip("MCP_TEST_DATABASE_URL not set — skipping integration tests")
    return url


@pytest.fixture(scope="session")
def pg_engine(integration_url):
    """Session-scoped SQLAlchemy engine pointed at the test Postgres."""
    from sqlalchemy import create_engine
    eng = create_engine(integration_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def fresh_schema(pg_engine):
    """
    Create a uniquely-named schema with seed tables. Drops on teardown.
    Yields the schema name.
    """
    from sqlalchemy import text
    import uuid as _uuid
    schema = f"mcp_test_{_uuid.uuid4().hex[:8]}"
    with pg_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE TABLE "{schema}".widgets (id serial PRIMARY KEY, name text NOT NULL, qty int DEFAULT 0)'))
        conn.execute(text(f'CREATE TABLE "{schema}".tags (widget_id int REFERENCES "{schema}".widgets(id), tag text)'))
        conn.execute(text(f'INSERT INTO "{schema}".widgets (name, qty) VALUES (\'a\', 1), (\'b\', 2), (\'c\', 3)'))
        conn.execute(text(f'INSERT INTO "{schema}".tags (widget_id, tag) VALUES (1, \'red\'), (1, \'big\'), (2, \'blue\')'))
    yield schema
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def mcp_env(monkeypatch, integration_url, fresh_schema):
    """
    Configure database_read to point at the integration DB + a freshly-seeded
    schema in the allowlist. Returns a re-imported `database_read` module
    with cleared engine cache.
    """
    monkeypatch.setenv("DATABASE_URL", integration_url)
    monkeypatch.setenv("DB_ALLOWED_SCHEMAS", fresh_schema)
    # Bring down any cached engines from a prior test
    import importlib
    import database_read as _db
    _db._dispose_all_engines()
    importlib.reload(_db)
    yield _db
    _db._dispose_all_engines()
