"""URL derivation and laziness for the Session-8 async database stack.

Pure unit tests: no connection, no engine created by importing the module. They
pin the two properties that make the dual-stack design safe — one
``DATABASE_URL`` configures both drivers, and importing ``app.persistence`` never
opens a database.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.persistence import database


def test_psycopg_url_becomes_asyncpg():
    assert (
        database._async_database_url("postgresql+psycopg://estimator:estimator@db:5432/estimator")
        == "postgresql+asyncpg://estimator:estimator@db:5432/estimator"
    )


def test_bare_postgres_url_becomes_asyncpg():
    assert (
        database._async_database_url("postgresql://u:p@localhost:5432/db")
        == "postgresql+asyncpg://u:p@localhost:5432/db"
    )


def test_postgres_scheme_alias_is_normalised():
    assert (
        database._async_database_url("postgres://u:p@localhost:5432/db")
        == "postgresql+asyncpg://u:p@localhost:5432/db"
    )


def test_an_async_url_is_left_alone():
    url = "postgresql+asyncpg://u:p@localhost:5432/db"
    assert database._async_database_url(url) == url


def test_sqlite_url_passes_through_unchanged():
    # The vector store cannot run on SQLite; the URL is not mangled anyway, so
    # the failure surfaces as an explicit "not available" rather than a bad DSN.
    assert database._async_database_url("sqlite:///var/estimator.db") == "sqlite:///var/estimator.db"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+psycopg://u:p@h:5432/db", True),
        ("postgresql://u:p@h:5432/db", True),
        ("postgresql+asyncpg://u:p@h:5432/db", True),
        ("sqlite:///var/estimator.db", False),
        ("sqlite://", False),
        ("not a url", False),
    ],
)
def test_is_postgres_url(url: str, expected: bool) -> None:
    assert database.is_postgres_url(url) is expected


def test_importing_the_module_creates_no_engine():
    """A fresh interpreter must not build an engine or touch a database."""
    code = (
        "from app.persistence import database as d; "
        "assert not d._ENGINES and not d._ASYNC_ENGINES, 'engine built at import'; "
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_async_factory_is_cached_per_url(monkeypatch):
    database.reset_engines()
    try:
        first = database.get_async_session_factory("postgresql+psycopg://u:p@h:5432/db")
        second = database.get_async_session_factory("postgresql+psycopg://u:p@h:5432/db")
        assert first is second
        # The async and sync caches are independent: no sync engine was built.
        assert database._ASYNC_FACTORIES
        assert not database._ENGINES
    finally:
        database.reset_engines()


# --- why the vector store is unavailable -------------------------------------


class _StubSettings:
    def __init__(self, database_url: str, embedding_model: str = "text-embedding-3-small") -> None:
        self.DATABASE_URL = database_url
        self.EMBEDDING_MODEL = embedding_model
        self.OPENAI_API_KEY = "sk-test"


def _problem_for(monkeypatch, settings: _StubSettings) -> str | None:
    from app import dependencies

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    return dependencies.vector_store_problem()


def test_vector_store_is_available_on_postgres_with_the_right_model(monkeypatch):
    settings = _StubSettings("postgresql+psycopg://u:p@h:5432/db")
    assert _problem_for(monkeypatch, settings) is None


def test_sqlite_disables_the_vector_store(monkeypatch):
    settings = _StubSettings("sqlite:///var/estimator.db")
    assert _problem_for(monkeypatch, settings) == "database_not_postgres"


def test_a_non_1536_model_disables_the_vector_store(monkeypatch):
    settings = _StubSettings(
        "postgresql+psycopg://u:p@h:5432/db", embedding_model="text-embedding-3-large"
    )
    assert _problem_for(monkeypatch, settings) == "embedding_model_dimension_mismatch"


def test_the_factories_return_none_when_the_store_is_unavailable(monkeypatch):
    """The routers rely on this: `None` becomes the generic 500, not a crash."""
    from app import dependencies

    monkeypatch.setattr(
        dependencies, "get_settings", lambda: _StubSettings("sqlite:///var/estimator.db")
    )
    dependencies.get_embedder.cache_clear()
    dependencies.get_rag_ingest_service.cache_clear()
    dependencies.get_semantic_retriever.cache_clear()
    try:
        assert dependencies.get_rag_ingest_service() is None
        assert dependencies.get_semantic_retriever() is None
    finally:
        dependencies.get_rag_ingest_service.cache_clear()
        dependencies.get_semantic_retriever.cache_clear()
        dependencies.get_embedder.cache_clear()

